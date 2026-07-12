"""Record the Zwicky integration fixture (wh-mu6l5).

Run from the service directory:

    uv run python tests/fixtures/record_zwicky_fixture.py

Speak a natural sentence containing the word 'Zwicky' when prompted, e.g.
"the Zwicky telescope is in the Canary Islands". Writes
tests/fixtures/zwicky_dictation.wav (16 kHz mono, 6 seconds).
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

RATE = 16000
SECONDS = 6
DEST = Path(__file__).parent / "zwicky_dictation.wav"

print(f"Recording {SECONDS}s at {RATE} Hz. Speak a sentence with 'Zwicky'...")
audio = sd.rec(int(SECONDS * RATE), samplerate=RATE, channels=1, dtype="int16")
sd.wait()
print("Done.")

with wave.open(str(DEST), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(RATE)
    w.writeframes(np.asarray(audio).tobytes())

print(f"Wrote {DEST}")
