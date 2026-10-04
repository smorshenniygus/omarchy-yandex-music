import errno
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.audio_cache import AudioCache, AudioIdentity, AudioSource, CacheRequest


class Response:
    def __init__(self, chunks, length=None, error=None):
        self.chunks = chunks
        self.headers = {} if length is None else {"content-length": str(length)}
        self.error = error
        self.closed = False

    def raise_for_status(self):
        if self.error: raise self.error

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


class AudioCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cache = AudioCache(Path(self.temporary.name), budget=14000, max_file=10000)
        self.identity = AudioIdentity.for_track("account", "track", "best")

    def tearDown(self):
        self.cache.close()
        self.cache.worker.join(2)
        self.temporary.cleanup()

    def request(self, identity=None, ready=lambda path: None, valid=lambda: True):
        return CacheRequest(identity or self.identity,
            lambda: AudioSource("https://audio.test/expired-signed-url", "mp3", 320), valid, ready)

    def download(self, data=b"audio", identity=None, length=None):
        response = Response([data], len(data) if length is None else length)
        with patch("backend.audio_cache.requests.get", return_value=response):
            path = self.cache._download(self.request(identity), threading.Event())
        self.assertTrue(response.closed)
        return path

    def test_miss_hit_and_account_quality_isolation(self):
        self.assertIsNone(self.cache.lookup(self.identity))
        path = self.download()
        self.assertEqual(self.cache.lookup(self.identity), path)
        self.assertIsNone(self.cache.lookup(AudioIdentity.for_track("other", "track", "best")))
        self.assertIsNone(self.cache.lookup(AudioIdentity.for_track("account", "track", "economy")))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_variant_codec_and_bitrate_are_part_of_key(self):
        first = self.cache._name(self.identity, AudioSource("url1", "mp3", 320))
        self.assertEqual(first, self.cache._name(self.identity, AudioSource("url2", "mp3", 320)))
        self.assertNotEqual(first, self.cache._name(self.identity, AudioSource("url1", "aac", 192)))

    def test_partial_file_is_never_visible_and_final_publish_is_atomic(self):
        def chunks():
            yield b"first"
            self.assertIsNone(self.cache.lookup(self.identity))
            self.assertTrue(list(self.cache.directory.glob("*.part")))
            yield b"second"
        with patch("backend.audio_cache.requests.get", return_value=Response(chunks(), 11)):
            path = self.cache._download(self.request(), threading.Event())
        self.assertEqual(path.read_bytes(), b"firstsecond")
        self.assertFalse(list(self.cache.directory.glob("*.part")))

    def test_truncated_empty_oversized_and_declared_oversized_are_rejected(self):
        for response in (Response([b"short"], 8), Response([], 0),
                         Response([b"a" * 10001]), Response([], 10001)):
            with patch("backend.audio_cache.requests.get", return_value=response):
                self.assertIsNone(self.cache._download(self.request(), threading.Event()))
            self.assertFalse(list(self.cache.directory.glob("*.audio")))
            self.assertFalse(list(self.cache.directory.glob("*.part")))

    def test_disconnect_429_expired_link_and_no_space_are_best_effort(self):
        def disconnected():
            yield b"partial"
            raise ConnectionError("network disconnected")
        for response in (Response(disconnected()), Response([], error=RuntimeError("HTTP 429")),
                         Response([], error=RuntimeError("HTTP 403"))):
            with patch("backend.audio_cache.requests.get", return_value=response):
                self.assertIsNone(self.cache._download(self.request(), threading.Event()))
            self.assertTrue(response.closed)
        with patch("backend.audio_cache.requests.get", return_value=Response([b"audio"])), \
             patch("backend.audio_cache.os.open", side_effect=OSError(errno.ENOSPC, "no space")):
            self.assertIsNone(self.cache._download(self.request(), threading.Event()))
        self.assertFalse(list(self.cache.directory.glob("*.part")))

    def test_corruption_is_detected_even_with_same_file_size(self):
        path = self.download()
        path.write_bytes(b"xxxxx")
        self.assertIsNone(self.cache.lookup(self.identity))
        self.assertFalse(path.exists())

    def test_lru_eviction_and_pins_include_partial_budget(self):
        identities = [AudioIdentity.for_track("account", str(i), "best") for i in range(3)]
        first = self.download(b"a" * 4500, identities[0])
        second = self.download(b"b" * 4500, identities[1])
        self.cache.protect({identities[0]})
        third = self.download(b"c" * 4500, identities[2])
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(third.exists())
        self.assertLessEqual(sum(p.stat().st_size for p in self.cache.directory.iterdir()), self.cache.budget)
        self.cache.protect({identities[0], identities[2]})
        self.assertIsNone(self.download(b"d" * 4500, identities[1]))

    def test_cancel_during_download_removes_partial(self):
        cancel = threading.Event()
        def chunks():
            yield b"first"
            cancel.set()
            yield b"second"
        with patch("backend.audio_cache.requests.get", return_value=Response(chunks())):
            self.assertIsNone(self.cache._download(self.request(), cancel))
        self.assertFalse(list(self.cache.directory.iterdir()))

    def test_download_has_total_deadline_even_when_chunks_keep_arriving(self):
        response = Response([b"audio"])
        with patch("backend.audio_cache.requests.get", return_value=response), \
             patch("backend.audio_cache.time.monotonic", side_effect=[0, 121]):
            self.assertIsNone(self.cache._download(self.request(), threading.Event()))
        self.assertFalse(list(self.cache.directory.iterdir()))

    def test_protected_playing_path_survives_quality_change(self):
        old = self.download(b"a" * 4500)
        economy = AudioIdentity.for_track("account", "track", "economy")
        next_identity = AudioIdentity.for_track("account", "next", "economy")
        self.cache.protect({economy, next_identity}, {old})
        self.assertIsNotNone(self.download(b"b" * 4500, next_identity))
        self.assertIsNone(self.download(b"c" * 4500, economy))
        self.assertTrue(old.exists())

    def test_signout_cancels_inflight_download_before_publication(self):
        entered, release = threading.Event(), threading.Event()
        def chunks():
            entered.set()
            release.wait(2)
            yield b"audio"
        with patch("backend.audio_cache.requests.get", return_value=Response(chunks())):
            self.cache.schedule([self.request()])
            self.assertTrue(entered.wait(2))
            self.cache.clear_account("account")
            release.set()
            self.cache.close()
            self.cache.worker.join(2)
        self.assertFalse(list(self.cache.directory.iterdir()))

    def test_invalidated_session_does_not_publish(self):
        valid = [True]
        def chunks():
            yield b"audio"
            valid[0] = False
        with patch("backend.audio_cache.requests.get", return_value=Response(chunks())):
            self.assertIsNone(self.cache._download(
                self.request(valid=lambda: valid[0]), threading.Event()))
        self.assertFalse(list(self.cache.directory.iterdir()))

    def test_identical_inflight_requests_are_coalesced(self):
        entered, release, ready = threading.Event(), threading.Event(), threading.Event()
        def chunks():
            entered.set()
            release.wait(2)
            yield b"audio"
        with patch("backend.audio_cache.requests.get", return_value=Response(chunks())) as get:
            self.cache.schedule([self.request(ready=lambda path: ready.set())])
            self.assertTrue(entered.wait(2))
            self.cache.schedule([self.request(ready=lambda path: ready.set()), self.request()])
            release.set()
            self.assertTrue(ready.wait(2))
            self.assertEqual(get.call_count, 1)

    def test_next_request_preempts_current_fill_with_one_downloader(self):
        entered, release, next_ready = threading.Event(), threading.Event(), threading.Event()
        next_identity = AudioIdentity.for_track("account", "next", "best")
        def chunks():
            entered.set()
            release.wait(2)
            yield b"current"
        with patch("backend.audio_cache.requests.get", side_effect=[Response(chunks()), Response([b"next"])]) as get:
            self.cache.schedule([self.request()])
            self.assertTrue(entered.wait(2))
            self.cache.schedule([self.request(next_identity, ready=lambda path: next_ready.set())])
            release.set()
            self.assertTrue(next_ready.wait(2))
            self.assertEqual(get.call_count, 2)
        self.assertIsNone(self.cache.lookup(self.identity))
        self.assertIsNotNone(self.cache.lookup(next_identity))

    def test_restart_cleans_partial_and_validates_persistent_files(self):
        path = self.download()
        part = self.cache.directory / ("a" * 64 + ".part")
        part.write_bytes(b"partial")
        self.cache.close()
        self.cache.worker.join(2)
        self.cache = AudioCache(self.cache.directory)
        self.assertFalse(part.exists())
        self.assertEqual(self.cache.lookup(self.identity), path)
        self.cache.close()
        self.cache.worker.join(2)
        path.write_bytes(b"broken")
        self.cache = AudioCache(self.cache.directory)
        self.assertIsNone(self.cache.lookup(self.identity))

    def test_signout_removes_only_account_files_and_cancels_publication(self):
        other = AudioIdentity.for_track("other", "track", "best")
        own_path = self.download()
        other_path = self.download(identity=other)
        self.cache.clear_account("account")
        self.assertFalse(own_path.exists())
        self.assertTrue(other_path.exists())

    def test_unavailable_directory_does_not_disable_network_playback(self):
        self.cache.close()
        self.cache.worker.join(2)
        occupied = Path(self.temporary.name) / "file"
        occupied.write_text("occupied")
        self.cache = AudioCache(occupied)
        self.assertFalse(self.cache.enabled)
        self.assertIsNone(self.cache.lookup(self.identity))


if __name__ == "__main__":
    unittest.main()
