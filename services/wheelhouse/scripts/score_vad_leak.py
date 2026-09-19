"""Score a recording with Wheelhouse's own speech detector.

Why this exists, and why it is a separate script from the recorder.
wh-screen-reader-audio-suppression-conflict records one number as the thing
that decides the whole plan: how much of NVDA's speech Wheelhouse's own
detector calls speech. The baseline on that bead was measured on 2026-08-15
with services/wheelhouse/stt/vad.py at threshold 0.5, so a comparison is only
valid if it uses the same detector. That detector lives in this service and
this virtual environment, while the shipped capture path lives in the
stt_providers shared package with its own environment. Two environments, two
scripts.

Record first, from services/stt_providers/shared:

    uv run python tools/record_capture.py --seconds 45 --out C:/tmp/nvda-winrt.wav

That tool records the one shipped capture path, the WinRT AudioGraph. It
offered a second recording through sounddevice as the comparison case until
the PortAudio capture path was deleted (wh-portaudio-capture-removal), so a
comparison recording now has to be made outside this repository.

Then score here, from services/wheelhouse:

    uv run python scripts/score_vad_leak.py C:/tmp/nvda-winrt.wav

A lower speech percentage means less of NVDA's voice reaches the recogniser.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

# The script lives at services/wheelhouse/scripts/score_vad_leak.py and
# imports the detector from services/wheelhouse/stt/vad.py, so the service
# root has to be on sys.path the way it is inside the running process. Same
# idiom as scripts/capture_rejected_controls.py.
SCRIPT_DIR = Path(__file__).resolve().parent
WHEELHOUSE_ROOT = SCRIPT_DIR.parent  # services/wheelhouse
if str(WHEELHOUSE_ROOT) not in sys.path:
    sys.path.insert(0, str(WHEELHOUSE_ROOT))

from stt.vad import SileroVAD  # noqa: E402

SAMPLE_RATE = 16000
# Silero scores 512 samples at a time, which is 32 ms at 16 kHz. Feeding
# exactly that much per call makes one call score one chunk, which is how the
# 2026-08-15 baseline counted chunks.
SAMPLES_PER_CHUNK = 512
BYTES_PER_CHUNK = SAMPLES_PER_CHUNK * 2

# Measured 2026-08-15 on Ikon, NVDA 2026.1.1 reading aloud through the
# speakers, captured on the path in use at that time.
BASELINE = {
    "speech_percent": 87.5,
    "peak_confidence": 1.000,
    "mean_confidence": 0.839,
    "peak_dbfs": -37.1,
    "rms_dbfs": -57.5,
}
# The silent-room control from the same session.
SILENT_CONTROL_MEAN_CONFIDENCE = 0.033


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
    parser.add_argument("wav", help="Recording written by tools/record_capture.py")
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()

    path = Path(args.wav)
    if not path.exists():
        print(f"[x] no such file: {path}")
        return 1

    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != SAMPLE_RATE:
            print(f"[x] {path} is {handle.getframerate()} Hz; the detector needs {SAMPLE_RATE}")
            return 1
        if handle.getnchannels() != 1:
            print(f"[x] {path} has {handle.getnchannels()} channels; the detector needs mono")
            return 1
        if handle.getsampwidth() != 2:
            print(f"[x] {path} is not 16-bit")
            return 1
        audio = handle.readframes(handle.getnframes())

    samples = np.frombuffer(audio, dtype=np.int16)
    peak_db, rms_db = _dbfs(samples)

    vad = SileroVAD(threshold=args.threshold, sample_rate=SAMPLE_RATE)
    confidences: list[float] = []
    for offset in range(0, len(audio) - BYTES_PER_CHUNK + 1, BYTES_PER_CHUNK):
        vad.is_speech(audio[offset : offset + BYTES_PER_CHUNK])
        confidences.append(vad.get_confidence())

    if not confidences:
        print(f"[x] {path} is shorter than one 32 ms chunk")
        return 1

    scores = np.array(confidences, dtype=np.float64)
    speech_chunks = int(np.count_nonzero(scores >= args.threshold))
    speech_percent = 100.0 * speech_chunks / scores.size

    print(f"file            {path}")
    print(f"duration        {samples.size / SAMPLE_RATE:.1f} s over {scores.size} chunks of 32 ms")
    print(f"threshold       {args.threshold}")
    print()
    print(f"{'':<18}{'this recording':>16}{'2026-08-15 baseline':>24}")
    print(f"{'speech chunks':<18}{speech_percent:>15.1f}%{BASELINE['speech_percent']:>23.1f}%")
    print(f"{'peak confidence':<18}{scores.max():>16.3f}{BASELINE['peak_confidence']:>24.3f}")
    print(f"{'mean confidence':<18}{scores.mean():>16.3f}{BASELINE['mean_confidence']:>24.3f}")
    print(f"{'peak level':<18}{peak_db:>13.1f} dBFS{BASELINE['peak_dbfs']:>19.1f} dBFS")
    print(f"{'RMS level':<18}{rms_db:>13.1f} dBFS{BASELINE['rms_dbfs']:>19.1f} dBFS")
    print()
    print(f"The silent-room control that day scored {SILENT_CONTROL_MEAN_CONFIDENCE} mean confidence")
    print("and never crossed the threshold. A recording near that number means the")
    print("echo canceller removed the screen reader. A recording near the baseline")
    print("means it did not.")
    print()
    print("[!] This script does not decide the bead. It reports one number. The")
    print("    decision belongs to the repository owner.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
