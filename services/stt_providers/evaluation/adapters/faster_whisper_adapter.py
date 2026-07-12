"""Faster-whisper adapter for the benchmark harness.

Uses CTranslate2-based Whisper inference to transcribe pre-recorded WAV
files. Supports both GPU (float16) and CPU (int8) compute modes.

Reference: docs/design/local_stt_decision_framework.md (Section 4.3)
"""

import logging
import time

import numpy as np
from faster_whisper import WhisperModel

from .base import TranscriptionResult

log = logging.getLogger(__name__)

# Provider -> (device, compute_type) mapping
_PROVIDER_DEFAULTS = {
    "cuda": ("cuda", "float16"),
    "cpu": ("cpu", "int8"),
}


class FasterWhisperAdapter:
    """Benchmark adapter for faster-whisper (CTranslate2 Whisper)."""

    def __init__(
        self,
        model_size_or_path: str = "large-v3-turbo",
        provider: str = "cpu",
        name: str | None = None,
    ):
        # Sanitize slashes in the adapter name so downstream write_results()
        # can use it as a single filename on Windows. Without this, an HF
        # repo id like 'Systran/faster-distil-whisper-small.en' produces
        # 'faster-whisper-Systran/faster-distil-whisper-small.en' which the
        # results writer tries to open as results/faster-whisper-Systran/
        # <name>.json -- a subdirectory that doesn't exist.
        safe_id = model_size_or_path.replace("/", "_").replace("\\", "_")
        self.name = name or f"faster-whisper-{safe_id}"
        device, compute_type = _PROVIDER_DEFAULTS.get(provider, ("cpu", "int8"))
        self._model = WhisperModel(model_size_or_path, device=device, compute_type=compute_type)

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """Transcribe audio using faster-whisper.

        Passes float32 samples directly to the model (no PCM conversion
        needed -- CTranslate2 accepts float32 natively). Concatenates all
        output segments into a single transcript string.
        """
        t0 = time.perf_counter()
        segments, _info = self._model.transcribe(samples, language="en", beam_size=5)

        # Consume the segment generator and join text
        text = "".join(seg.text for seg in segments).strip()
        elapsed_ms = (time.perf_counter() - t0) * 1000

        return TranscriptionResult(
            text=text,
            elapsed_ms=elapsed_ms,
            interim_results=[],  # Batch model has no interims
        )

    def reset(self) -> None:
        """No-op: each transcribe() is a standalone call."""
        pass
