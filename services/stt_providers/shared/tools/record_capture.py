"""Record the microphone through the same capture path a provider server uses.

Why this exists. wh-screen-reader-audio-suppression-conflict has to decide
whether Windows' acoustic echo canceller already removes NVDA speech from
Wheelhouse's microphone. The shipped providers do not capture through
services/wheelhouse/stt/audio_capture.py; every one of them calls
shared_audio.capture.get_audio_provider.

Every shipped provider now captures through the WinRT AudioGraph, and only
through it. winsdk is a required dependency of the shared package, so all
three provider environments have it, and get_audio_provider raises rather
than choosing anything else (wh-capture-winrt-required). Before that change
only google_stt_server and the Parakeet provider declared winsdk, and the
rest captured through sounddevice without saying so. Re-check the
dependency with:

    grep -rn "winsdk" --include=pyproject.toml services/

This tool records ONE path, the shipped one: the WinRT AudioGraph asking
for AudioRenderCategory.COMMUNICATIONS and MediaCategory.COMMUNICATIONS
(winrt_capture.py, the two calls in _setup_graph). AudioEffectsManager
answered that capture category with acoustic echo cancellation, noise
suppression, automatic gain control and deep noise suppression on this
machine on 2026-08-27, and answered MediaCategory.SPEECH, which the code
asked for until wh-screen-reader-audio-suppression-conflict step 1, with
the same four. A recording made with this tool while NVDA reads aloud is
what settles whether the pair actually removes the screen reader's
speech.

It used to offer a second recording through sounddevice (PortAudio with
no effects requested) as the comparison case, built directly rather than
through the factory. That option went with the PortAudio capture path
itself (wh-portaudio-capture-removal): SounddeviceAudioCapture and
MicrophoneStream are deleted, so there is nothing left to compare
against inside this package. A future comparison needs a recording made
outside it. tools/nvda-leak-measurement.md says the same thing at the
step that used to run the sounddevice recording.

Run it from services/stt_providers/shared:

    uv run python tools/record_capture.py --seconds 45 --out C:/tmp/nvda-winrt.wav

This script only records. Scoring is a separate step, because the number the
bead compares against was produced by Wheelhouse's own detector, which lives
in the other service and the other virtual environment:

    services/wheelhouse/scripts/score_vad_leak.py
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

from shared_audio.capture import get_audio_provider, AudioConfig

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_MS = 30
SAMPLE_WIDTH_BYTES = 2


def _dbfs(samples: np.ndarray) -> tuple[float, float]:
    """Return the peak and the root-mean-square level, both in dBFS."""
    if samples.size == 0:
        return float("-inf"), float("-inf")
    scaled = samples.astype(np.float64) / 32768.0
    peak = float(np.max(np.abs(scaled)))
    rms = float(np.sqrt(np.mean(np.square(scaled))))
    to_db = lambda v: 20.0 * np.log10(v) if v > 0 else float("-inf")
    return to_db(peak), to_db(rms)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--out", required=True, help="Path of the WAV file to write.")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    config = AudioConfig(rate=SAMPLE_RATE, channels=CHANNELS, chunk_ms=CHUNK_MS)
    # Through the factory, which returns the WinRT capture or raises, by
    # rule (wh-capture-winrt-required). This tool used to build a second
    # provider directly for the comparison recording; that provider no
    # longer exists (wh-portaudio-capture-removal).
    provider = get_audio_provider(config=config)
    print(f"[+] provider in use:   {type(provider).__name__}")

    provider.start()
    if not provider.wait_ready(timeout=15.0):
        provider.stop()
        print("[x] the capture path never became ready within 15 seconds")
        return 1

    print(f"[+] recording {args.seconds:.0f} seconds -- start the reading now")
    frames: list[bytes] = []
    started = time.monotonic()
    silent_reads = 0
    while time.monotonic() - started < args.seconds:
        chunk = provider.read(timeout=1.0)
        if chunk:
            frames.append(chunk)
        else:
            silent_reads += 1
    elapsed = time.monotonic() - started
    provider.stop()

    if not frames:
        print("[x] no audio arrived; nothing written")
        return 1

    audio = b"".join(frames)
    with wave.open(str(out_path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(audio)

    samples = np.frombuffer(audio, dtype=np.int16)
    peak_db, rms_db = _dbfs(samples)
    captured_seconds = samples.size / SAMPLE_RATE

    print(f"[+] wrote {out_path}")
    print(f"    wall clock      {elapsed:.1f} s")
    print(f"    audio captured  {captured_seconds:.1f} s over {len(frames)} chunks")
    print(f"    empty reads     {silent_reads}")
    print(f"    peak level      {peak_db:.1f} dBFS")
    print(f"    RMS level       {rms_db:.1f} dBFS")
    print(f"    provider stats  {provider.get_stats()}")
    print()
    print("Now score it from services/wheelhouse:")
    print(f"    uv run python scripts/score_vad_leak.py {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
