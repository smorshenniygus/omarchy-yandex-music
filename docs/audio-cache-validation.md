# Audio cache validation

Run the Python suite using an environment with the locked application dependencies:

```bash
<venv>/bin/python -m unittest discover -s tests -v
PYTHONPATH=. <venv>/bin/python tests/benchmark_audio_transitions.py
bash -n install.sh uninstall.sh
systemd-analyze --user verify systemd/omarchy-yandex-music.service
```

Real mpv integration tests require `mpv` and `ffmpeg`. They use separate processes, temporary sockets/cache directories and a null audio output; they do not control the desktop player. Fixtures are 48 kHz cosine waves with nonzero samples at their boundaries. The PCM test compares the combined output sample for sample with the input WAV files, separating inserted silence from silence inside a recording. Codec coverage includes generated MP3, AAC and FLAC.

Measured on the development machine with mpv 0.41.0 (three repetitions):

| Measurement | Previous polling / replace behavior | Prepared playlist / events |
| --- | --- | --- |
| Manual switch to a local fixture | 151.7–152.0 ms | 10.4 ms |
| `end-file` to next `file-loaded` | 453.5–454.1 ms | 0.5–0.6 ms |
| Added PCM samples between equal-format fixtures | Not measured | 0 |

The baseline reproduces the previous one-second idle poll and 150 ms readiness poll using local sources, so it excludes URL resolution and network delays. Event timing measures the mpv core handoff, not sound reaching a physical output device. The null/PCM results meet the 100 ms automatic and 200 ms manual test targets; physical-device buffering and format resampling can differ.

A separate read-only smoke check downloaded two real tracks from the existing Yandex queue into a temporary cache and played them through an isolated mpv null output with audio network access disabled after preparation. Both available downloads were MP3; the cached event handoff was 0.6 ms. No listening feedback was sent and the installed application was unchanged.

Unit checks cover hits/misses, account and quality isolation, codec keys, atomic publication, truncation/corruption, LRU/pinning and total/individual limits, cancellation and request coalescing, interrupted downloads, 403/429 responses, disk exhaustion and unavailable cache directories. Backend checks cover repeat/shuffle/order boundaries, detached liked tracks, stale URL resolution, rapid switching, stop/sign-out, radio reporting order, recovery of interrupted streams, and duplicate event/monitor transitions. Installer checks cover XDG paths with spaces and percent signs, the update fast path, module delivery and uninstall after changing XDG_CACHE_HOME.
