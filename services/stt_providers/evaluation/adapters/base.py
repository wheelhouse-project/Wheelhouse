"""Model adapter protocol for STT benchmark harness.

Reference: docs/design/benchmark_harness_design.md (Section 2.1)
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass
class TranscriptionResult:
    """Result from a model adapter's transcribe() call."""

    text: str  # Final transcription (raw, before normalization)
    elapsed_ms: float  # Wall-clock inference time
    interim_results: list[str] = field(default_factory=list)  # Partials during processing


@runtime_checkable
class ModelAdapter(Protocol):
    """Protocol that all benchmark adapters must implement."""

    name: str  # Human-readable model name

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """Feed audio samples, return transcription with timing and interim results.

        Args:
            samples: float32 numpy array of audio samples
            sample_rate: sample rate (always 16000 for this corpus)

        Returns:
            TranscriptionResult with final text, elapsed time, and interim results
        """
        ...

    def reset(self) -> None:
        """Reset model state between utterances."""
        ...
