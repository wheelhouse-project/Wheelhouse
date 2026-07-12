"""whisper.cpp adapter for the benchmark harness.

Uses pywhispercpp Python bindings for whisper.cpp inference. Accepts
GGML model files directly, enabling testing of different quantization
levels. GPU support depends on the pywhispercpp build (CUDA/Vulkan).

Production parity: when an initial_prompt is provided, the adapter
forwards the same decoder knobs that the production Vulkan Small path
uses in services/stt_providers/shared/vulkan_engine/vulkan_engine.py
(temperature=0, no_context, single_segment, tightened thresholds, and
initial_prompt for command-vocabulary priming). This is the calibrated
mode that Stage B uses to benchmark Vulkan Small against the harness.

Without initial_prompt, the adapter runs vanilla whisper.cpp with the
library defaults -- useful for reproducing bare-whisper behavior.

Reference: docs/superpowers/plans/2026-03-18-whisper-cpp-benchmark.md
wh-fnx: initial_prompt forwarding for production-calibrated baseline.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import numpy as np

from .base import TranscriptionResult

log = logging.getLogger(__name__)

# Register CUDA DLL directory if present (needed for CUDA builds only).
# Vulkan builds use the GPU driver's built-in Vulkan loader -- no setup needed.
_CUDA_BIN = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6\bin"
if os.path.isdir(_CUDA_BIN):
    try:
        os.add_dll_directory(_CUDA_BIN)
    except OSError:
        pass


def _load_model(model_path: str, n_threads: int = 8):
    """Load a whisper.cpp model via pywhispercpp."""
    from pywhispercpp.model import Model

    return Model(model_path, n_threads=n_threads, print_progress=False)


def _vulkan_wheel_installed() -> bool:
    """True when the installed pywhispercpp ships a Vulkan ggml backend.

    The vendored Vulkan wheel installs ggml-vulkan-<hash>.dll into
    site-packages next to the other ggml DLLs; the CPU-only PyPI wheel
    does not (wh-kft)."""
    import pywhispercpp

    pkg_file = getattr(pywhispercpp, "__file__", None)
    if not pkg_file:
        return True  # layout undeterminable; do not cry wolf
    site_dir = Path(pkg_file).resolve().parent.parent
    return bool(list(site_dir.glob("ggml-vulkan*.dll")))


def _warn_if_cpu_only() -> None:
    """wh-kft: a silently-substituted CPU-only pywhispercpp build makes
    every whisper-cpp benchmark latency wrong without any visible sign.
    Warn loudly at adapter init so the run's log carries the evidence."""
    if _vulkan_wheel_installed():
        return
    log.warning(
        "pywhispercpp has no ggml-vulkan DLL -- CPU-only build (PyPI "
        "wheel?). Benchmark latencies will NOT reflect the Vulkan "
        "production path. Reinstall the vendored wheel from "
        "services/stt_providers/shared/vendor/wheels/ (see the Vulkan "
        "Wheel Protection section of CLAUDE.md)."
    )


def _name_from_path(model_path: str) -> str:
    """Derive a human-readable name from a GGML model filename.

    Examples:
        /models/ggml-small.en.bin -> whisper-cpp-small.en
        /models/ggml-medium.en-q5_0.bin -> whisper-cpp-medium.en-q5_0
    """
    stem = Path(model_path).stem
    name = re.sub(r"^ggml-", "", stem)
    return f"whisper-cpp-{name}"


class WhisperCppAdapter:
    """Benchmark adapter for whisper.cpp (via pywhispercpp)."""

    def __init__(
        self,
        model_path: str,
        n_threads: int = 8,
        name: str | None = None,
        initial_prompt: str = "",
        language: str = "en",
    ):
        self.name = name or _name_from_path(model_path)
        if initial_prompt:
            self.name = f"{self.name}-primed"
        _warn_if_cpu_only()
        self._model = _load_model(model_path, n_threads=n_threads)
        self._initial_prompt = initial_prompt
        self._language = language

    def _transcribe_kwargs(self) -> dict[str, Any]:
        """Kwargs passed to pywhispercpp's transcribe().

        Mirrors the production Vulkan Small settings from
        services/stt_providers/shared/vulkan_engine/vulkan_engine.py:123.
        Only applied when initial_prompt is set (calibrated mode); without
        a prompt we use library defaults for a bare-whisper baseline.
        """
        if not self._initial_prompt:
            return {"language": self._language}
        return {
            "language": self._language,
            "temperature": 0.0,
            "temperature_inc": 0.0,
            "no_context": True,
            "single_segment": True,
            "greedy": {"best_of": 1},
            "no_speech_thold": 0.7,
            "logprob_thold": -0.7,
            "entropy_thold": 2.0,
            "initial_prompt": self._initial_prompt,
        }

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """Transcribe audio using whisper.cpp.

        pywhispercpp's Model.transcribe() accepts numpy arrays directly,
        so no temp-file conversion is needed. The samples must be float32.
        """
        t0 = time.perf_counter()
        segments = self._model.transcribe(samples, **self._transcribe_kwargs())
        elapsed_ms = (time.perf_counter() - t0) * 1000

        text = " ".join(seg.text.strip() for seg in segments).strip()

        return TranscriptionResult(
            text=text,
            elapsed_ms=elapsed_ms,
            interim_results=[],
        )

    def reset(self) -> None:
        """No-op: each transcribe() is a standalone call."""
        pass
