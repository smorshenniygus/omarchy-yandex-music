"""Compare local fixtures against the previous 1 s polling / replace behavior.

Run with: PYTHONPATH=. <venv>/bin/python tests/benchmark_audio_transitions.py
This uses an isolated mpv null output; no account or desktop player is touched.
"""
import time

from test_mpv_audio_cache import MpvAudioCacheTests


def legacy(case):
    player = case.player
    paths = {str(i): case.audio(i) for i in (1, 2)}
    player._schedule_preload = lambda: None
    player._url = lambda track, **kwargs: str(paths[str(track.id)])
    cache = player.audio_cache
    player.audio_cache = None
    def ready():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                idle = player._mpv_command(["get_property", "idle-active"], False)
                duration = player._mpv_command(["get_property", "duration"], False)
                if not idle and duration: return
            except Exception: pass
            time.sleep(.15)
        raise RuntimeError("legacy mpv load timed out")
    player._wait_mpv_ready = ready
    player._mpv_end_file = lambda event: case.events.append(
        ("end", event.get("playlist_entry_id"), time.monotonic()))
    player._play_current()
    case.wait_for(lambda: player.state.get("trackId") == "1")
    return cache


def measure(automatic, before):
    case = MpvAudioCacheTests()
    case.setUp()
    cache = case.player.audio_cache
    try:
        player = case.player
        if before:
            cache = legacy(case)
        else:
            for i in (1, 2): case.audio(i)
            player._play_current()
            case.wait_for(lambda: player.prepared_entry is not None)
        if automatic:
            if before:
                while True:
                    time.sleep(1)
                    if player._mpv_command(["get_property", "idle-active"], False):
                        player.next(automatic=True)
                        break
            case.wait_for(lambda: player.state.get("trackId") == "2")
            ended = next(t for event, _, t in case.events if event == "end")
            loaded = [t for event, _, t in case.events if event == "loaded"][-1]
            return (loaded - ended) * 1000
        started = time.monotonic()
        player.next()
        case.wait_for(lambda: player.state.get("trackId") == "2")
        return (time.monotonic() - started) * 1000
    finally:
        case.player.audio_cache = cache
        case.tearDown()


if __name__ == "__main__":
    for automatic, label in ((False, "manual"), (True, "automatic event handoff")):
        before = [measure(automatic, True) for _ in range(3)]
        after = [measure(automatic, False) for _ in range(3)]
        print(f"{label}: before {min(before):.1f}–{max(before):.1f} ms; "
              f"after {min(after):.1f}–{max(after):.1f} ms")
