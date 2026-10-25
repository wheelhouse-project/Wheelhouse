"""transcribe.cpp adapter for the benchmark harness.

Runs GGUF speech models (here Parakeet TDT 0.6B v3) through the
transcribe.cpp library on the ggml runtime, on the CPU or on a GPU
through Vulkan (AMD, Intel, and NVIDIA). Evaluation only: no shipped
provider uses transcribe.cpp (wh-transcribe-cpp-eval-adapter).

Manual install, as with pywhispercpp (docs/design/stt-and-ai.md
Section 4). The PyPI package transcribe-cpp==0.2.4 is pure Python and
declares a native provider package as a dependency, so install it with
--no-deps into the shared venv:

    cd services/stt_providers/shared
    uv pip install --no-deps <download-dir>/transcribe_cpp-0.2.4-py3-none-any.whl

The native library comes from GitHub release v0.2.4 of
handy-computer/transcribe.cpp, asset
transcribe-native-0.2.4-windows-x86_64-cpu-vulkan.tar.gz. Extract it and
set TRANSCRIBE_LIBRARY to the transcribe.dll inside it before the run.
The binding loads the ggml CPU and Vulkan modules from that same folder.
Any later uv sync in the shared venv removes the package; reinstall
afterward, and start runs with the venv's python.exe, not "uv run".

Backend check: a request for one backend that silently runs on another
makes every latency in the run wrong, the same failure the wh-kft check
in whisper_cpp_adapter.py guards. The adapter therefore refuses to run
when the device the model loaded on is not the requested backend.
"""

import logging
import time
from pathlib import Path

import numpy as np

from .base import TranscriptionResult

log = logging.getLogger(__name__)

# The two backends the windows-x86_64-cpu-vulkan release asset carries.
BACKENDS = ("cpu", "vulkan")

# Matches num_threads in the shipped Parakeet provider
# (services/stt_providers/sherpa_offline_parakeet_stt_server/config.toml).
DEFAULT_THREADS = 4

# transcribe.cpp native log levels (TRANSCRIBE_LOG_LEVEL_*): 1 info,
# 2 warn, 3 error, 4 debug, 5 continuation. Info prints one decoder line
# per run, so it goes to DEBUG to keep the benchmark log readable.
_NATIVE_LOG_LEVELS = {2: logging.WARNING, 3: logging.ERROR}


def _route_native_log(level: int, message: str) -> None:
    """Send one native transcribe.cpp log line to the logging module."""
    log.log(_NATIVE_LOG_LEVELS.get(level, logging.DEBUG), "native: %s", message.rstrip())


def _list_devices() -> list:
    """The compute devices that transcribe_cpp.backends() reports."""
    import transcribe_cpp

    return transcribe_cpp.backends()


def _load_model(model_path: str, backend: str):
    """Load a GGUF model with transcribe.cpp on the requested backend."""
    import transcribe_cpp

    transcribe_cpp.set_log_callback(_route_native_log)
    return transcribe_cpp.Model(model_path, backend=backend)


def _check_backend(requested: str, in_use: str) -> None:
    """Refuse a model that does not run on the requested backend.

    *in_use* is the kind of the device the model loaded on (model.device.kind).
    model.backend is not usable here: it gives the device name, such as
    "Vulkan0" or "CPU".
    """
    if in_use == requested:
        return
    raise RuntimeError(
        f"transcribe.cpp backend {requested!r} was requested, but the model "
        f"runs on {in_use!r}. The benchmark will not run, because every "
        f"latency would describe the wrong backend. The two backends are "
        f"cpu and vulkan; check that the Vulkan driver is installed and that "
        f"TRANSCRIBE_LIBRARY points at the windows-x86_64-cpu-vulkan build."
    )


class TranscribeCppAdapter:
    """Benchmark adapter for transcribe.cpp (via the transcribe_cpp binding)."""

    def __init__(
        self,
        model_path: str,
        backend: str = "vulkan",
        n_threads: int = DEFAULT_THREADS,
        name: str | None = None,
    ):
        if backend not in BACKENDS:
            raise ValueError(
                f"transcribe-cpp backend must be one of {', '.join(BACKENDS)}, "
                f"not {backend!r}"
            )
        if n_threads < 1:
            raise ValueError(f"n_threads must be 1 or more, not {n_threads}")
        self.name = name or f"transcribe-cpp-{Path(model_path).stem}-{backend}_t{n_threads}"

        for device in _list_devices():
            log.info(
                "transcribe.cpp device: %s (%s, kind %s, %.1f GB)",
                device.name, device.description, device.kind,
                device.memory_total / 1e9,
            )

        self._model = _load_model(model_path, backend)
        device = self._model.device
        log.info(
            "transcribe.cpp model %s %s loaded on %s (%s, kind %s), %d threads",
            self._model.arch, self._model.variant, device.name,
            device.description, device.kind, n_threads,
        )
        _check_backend(backend, device.kind)
        self._session = self._model.session(n_threads=n_threads)

        # The first run on a backend compiles and caches its compute
        # pipelines (4.9 s on Vulkan the first time on Ikon, against
        # 65 ms afterwards). Run one second of silence here so that cost
        # stays out of the first utterance's elapsed_ms.
        t0 = time.perf_counter()
        self._session.run(np.zeros(16000, dtype=np.float32))
        log.info("transcribe.cpp warm-up run: %.0f ms", (time.perf_counter() - t0) * 1000)

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """Transcribe one utterance of 16 kHz mono float32 samples."""
        if sample_rate != 16000:
            raise ValueError(f"transcribe.cpp needs 16000 Hz audio, not {sample_rate} Hz")
        t0 = time.perf_counter()
        result = self._session.run(samples)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        return TranscriptionResult(
            text=result.text.strip(),
            elapsed_ms=elapsed_ms,
            interim_results=[],
        )

    def reset(self) -> None:
        """No-op: each run() call is a separate utterance."""
        pass
