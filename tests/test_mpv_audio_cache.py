"""Real mpv IPC checks using isolated sockets and generated audio without edge silence."""
import math
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import backend
from backend.audio_cache import AudioCache, AudioSource, CacheRequest


@unittest.skipUnless(shutil.which("mpv") and shutil.which("ffmpeg"), "mpv and ffmpeg required")
class MpvAudioCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.socket_patch = patch.object(backend, "MPV_SOCKET", self.root / "mpv.sock")
        self.socket_patch.start()
        self.player = backend.Player.__new__(backend.Player)
        player = self.player
        player.lock = threading.RLock()
        player.control_lock = threading.RLock()
        player.api_lock = threading.Lock()
        player.client = SimpleNamespace(account_uid="integration")
        player.session_generation = 1
        player.queue_generation = 1
        player.queue_revision = 1
        player.play_generation = 0
        player.completed_generation = -1
        player.queue = []
        player.index = 0
        player.detached_track = None
        player.queue_source = []
        player.queue_artist_id = ""
        player.queue_artist_has_more = False
        player.radio_station = ""
        player.liked_ids = set()
        player.disliked_ids = set()
        player.muted = False
        player.volume = 70
        player.had_file = False
        player.consecutive_failures = 0
        player.preferences = {"audioQuality": "best", "playbackMode": "order"}
        player.state = {"playing": False, "stopped": True, "loading": False,
                        "loadingKind": "", "position": 0, "duration": 1}
        player.preload_candidate = None
        player.prepared_entry = None
        player.active_entry_id = None
        player.active_cache_path = None
        player.mpv_events_connected = threading.Event()
        player.mpv_event_stop = threading.Event()
        player.mpv_events_thread = None
        player.audio_cache = AudioCache(self.root / "cache")
        player._save_state = lambda *args: None
        player._publish_mpris = lambda *args: None
        player._notify_track = lambda *args: None
        player._maybe_extend_collection = lambda: None
        player._finish_playback_reporting = lambda **kwargs: None
        player._begin_playback_reporting = lambda *args: None
        player._update_playback_clock_locked = lambda *args: None
        player._url = lambda *args, **kwargs: self.fail("cached playback requested audio network")
        self.events = []
        original_end, original_loaded = player._mpv_end_file, player._mpv_file_loaded
        def end(event):
            self.events.append(("end", event.get("playlist_entry_id"), time.monotonic()))
            original_end(event)
        def loaded(entry):
            self.events.append(("loaded", entry, time.monotonic()))
            original_loaded(entry)
        player._mpv_end_file = end
        player._mpv_file_loaded = loaded
        player.mpv = subprocess.Popen([shutil.which("mpv"), "--no-config", "--idle=yes",
            "--no-video", "--no-terminal", "--load-scripts=no", "--gapless-audio=yes",
            "--prefetch-playlist=yes", "--ao=null", "--ao-null-untimed=no",
            f"--input-ipc-server={backend.MPV_SOCKET}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_for(lambda: backend.MPV_SOCKET.exists())

    def tearDown(self):
        self.player.shutdown()
        self.player.mpv.wait(timeout=5)
        if self.player.mpv_events_thread: self.player.mpv_events_thread.join(2)
        self.player.audio_cache.worker.join(2)
        self.socket_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def wait_for(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.005)
        raise AssertionError("timed out waiting for mpv")

    def audio(self, index, codec="wav"):
        wav = self.root / f"{index}.wav"
        with wave.open(str(wav), "wb") as output:
            output.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            # Continuous cosine has nonzero samples at both ends.
            output.writeframes(b"".join(struct.pack("<h", round(10000 * math.cos(2 * math.pi *
                (440 + index * 110) * sample / 48000))) for sample in range(57600)))
        source = wav
        if codec != "wav":
            source = self.root / f"{index}.{codec}"
            subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(wav), str(source)], check=True, capture_output=True)
        track = SimpleNamespace(id=str(index), title=f"Fixture {index}", artists=[], albums=[], duration_ms=1200)
        self.player.queue.append(track)
        request = CacheRequest(self.player._cache_identity(track),
            lambda: AudioSource("https://fixture.test/audio", codec, 320), lambda: True, lambda path: None)
        response = SimpleNamespace(headers={}, raise_for_status=lambda: None, close=lambda: None,
                                   iter_content=lambda chunk_size: iter([source.read_bytes()]))
        with patch("backend.audio_cache.requests.get", return_value=response):
            self.assertIsNotNone(self.player.audio_cache._download(request, threading.Event()))
        return source

    def test_automatic_cached_transition_uses_events_without_monitor_or_network(self):
        self.audio(1)
        self.audio(2)
        with patch("backend.audio_cache.requests.get", side_effect=AssertionError("network disconnected")):
            self.player._play_current()
            self.wait_for(lambda: self.player.prepared_entry is not None)
            old_id = self.player.active_entry_id
            next_id = self.player.prepared_entry["id"]
            self.wait_for(lambda: self.player.state.get("trackId") == "2")
        ended = next(t for event, entry, t in self.events if event == "end" and entry == old_id)
        loaded = next(t for event, entry, t in self.events if event == "loaded" and entry == next_id)
        self.assertLessEqual(loaded - ended, .1)
        self.assertEqual(self.player.index, 1)
        print(f"mpv automatic cached event handoff: {(loaded - ended) * 1000:.1f} ms")

    def test_manual_cached_transition_is_under_200ms_and_can_repeat_rapidly(self):
        for index in (1, 2, 3): self.audio(index)
        self.player._play_current()
        self.wait_for(lambda: self.player.prepared_entry is not None)
        started = time.monotonic()
        self.player.next()
        self.wait_for(lambda: self.player.state.get("trackId") == "2")
        elapsed = time.monotonic() - started
        self.assertLessEqual(elapsed, .2)
        self.wait_for(lambda: self.player.prepared_entry is not None)
        self.player.next()
        self.wait_for(lambda: self.player.state.get("trackId") == "3")
        print(f"mpv manual cached switch: {elapsed * 1000:.1f} ms")

    def test_mp3_aac_and_flac_cached_playlist_plays_all_formats(self):
        for index, codec in ((1, "mp3"), (2, "aac"), (3, "flac")): self.audio(index, codec)
        self.player._play_current()
        self.wait_for(lambda: self.player.state.get("trackId") == "3", timeout=8)
        self.assertFalse(self.player.state.get("error"))

    def test_pcm_output_adds_no_silence_between_equal_format_fixtures(self):
        first, second = self.audio(1), self.audio(2)
        output = self.root / "joined.wav"
        subprocess.run([shutil.which("mpv"), "--no-config", "--no-video", "--no-terminal",
            "--load-scripts=no", "--gapless-audio=yes", "--prefetch-playlist=yes",
            "--ao=pcm", f"--ao-pcm-file={output}", "--audio-format=s16", "--audio-channels=mono",
            str(first), str(second)], check=True, capture_output=True, timeout=10)
        with wave.open(str(output), "rb") as joined:
            self.assertEqual(joined.getnframes(), 115200)
            self.assertEqual(joined.getframerate(), 48000)
            frames = joined.readframes(joined.getnframes())
        with wave.open(str(first), "rb") as audio1, wave.open(str(second), "rb") as audio2:
            self.assertEqual(frames, audio1.readframes(57600) + audio2.readframes(57600))
