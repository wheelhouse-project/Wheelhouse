"""Silero VAD wrapper for voice activity detection.

This module runs the Silero VAD neural network model (upstream v6.2 ONNX
file, snakers4/silero-vad tag v6.2.3) through onnxruntime. It offers
significantly better accuracy than WebRTC VAD for distinguishing speech
from environmental noise.

WHY ONNXRUNTIME ON ONE THREAD (wh-vad-releases-gil). Until 2026-09-25 the
model ran through pysilero-vad 3.2.0, which calls ggml without releasing
the GIL and hard-codes a pool of 4 threads with barrier synchronization.
Under Parakeet decode load one 30 ms chunk took up to 2 s, and the capture
thread waited for the GIL the whole time, which tore down live microphones.
onnxruntime's InferenceSession.run releases the GIL (v1.25.1,
onnxruntime/python/onnxruntime_pybind_state.cc:2820, py::gil_scoped_release),
and with one intra-op and one inter-op thread it creates no thread pool
(onnxruntime/core/util/thread_utils.cc:122-124), so the model runs on the
calling thread. Measured on Ikon under Parakeet load: 13-16 ms per second of
audio, worst chunk 1.5 ms. Decisions agree with pysilero-vad on 99.91
percent of 22,492 evaluation-corpus chunks at threshold 0.5.

Key Classes:
  - SileroVAD: Neural network-based VAD with configurable threshold.

Typical Usage:
  from shared.audio import SileroVAD

  vad = SileroVAD(threshold=0.5)

  for audio_chunk in audio_stream:
      if vad.is_speech(audio_chunk):
          process_speech(audio_chunk)
"""

import threading
import time
from pathlib import Path

MODEL_PATH = Path(__file__).parent / "vad_model" / "silero_vad_v6.2.3.onnx"

# Samples of the previous chunk the model sees in front of each new chunk,
# as upstream utils_vad.OnnxWrapper feeds it at 16 kHz.
_CONTEXT_SAMPLES = 64

# One session per process. InferenceSession.run is safe to call from several
# threads; the per-stream state lives in each _OnnxSileroDetector.
_session = None
_session_lock = threading.Lock()


class SileroModelError(RuntimeError):
    """The Silero model file is missing or onnxruntime cannot load it."""


def _create_session(path: Path):
    """Load the model on one thread, on the CPU, with no worker pool."""
    if not path.is_file():
        raise SileroModelError(f"Silero VAD model file not found: {path}")
    # Imported here, not at module import, so each entry point's
    # add_runtime_dll_directory() call runs before onnxruntime's DLLs load
    # (services/runtime_dll_directory.py).
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.log_severity_level = 3
    try:
        return ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
    except Exception as exc:
        raise SileroModelError(
            f"Silero VAD model file could not be loaded: {path}: {exc}"
        ) from exc


def _get_session():
    """Return the process's one Silero session, creating it on first use."""
    global _session
    with _session_lock:
        if _session is None:
            _session = _create_session(MODEL_PATH)
        return _session


class _OnnxSileroDetector:
    """One audio stream's model state over the shared session.

    Called with 1024 bytes of 16-bit PCM (512 samples), returns the speech
    probability. Feeds the model as upstream utils_vad.OnnxWrapper does: the
    previous chunk's last 64 samples in front of the chunk, and the state
    tensor the previous call returned.
    """

    def __init__(self, session):
        import numpy as np

        self._np = np
        self._session = session
        self._sample_rate = np.array(16000, dtype=np.int64)
        self.reset()

    def reset(self):
        np = self._np
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(_CONTEXT_SAMPLES, dtype=np.float32)

    def __call__(self, pcm_bytes: bytes) -> float:
        np = self._np
        chunk = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
        model_input = np.concatenate([self._context, chunk])[None, :]
        output, self._state = self._session.run(
            None,
            {"input": model_input, "state": self._state, "sr": self._sample_rate},
        )
        self._context = chunk[-_CONTEXT_SAMPLES:]
        return float(output.ravel()[0])


def _get_silero_detector():
    """Return a new detector with its own state over the shared session."""
    return _OnnxSileroDetector(_get_session())


class SileroVAD:
    """Neural network-based Voice Activity Detector using Silero VAD.

    Provides the same interface as VoiceActivityDetector for drop-in replacement.
    Runs the model through onnxruntime on the calling thread.

    Attributes:
        threshold: Confidence threshold (0.0-1.0) for speech detection.
        sample_rate: Audio sample rate (must be 16000).
    """

    # The model takes exactly 512 samples = 1024 bytes of 16-bit PCM
    REQUIRED_BYTES = 1024

    def __init__(self, threshold: float = 0.5, sample_rate: int = 16000):
        """Initialize Silero VAD.

        Args:
            threshold: Confidence threshold for speech detection.
                      Higher = fewer false positives, may miss quiet speech.
                      Lower = catch more speech, more false positives.
                      Default 0.5 is a balanced starting point.
            sample_rate: Audio sample rate. Must be 16000 Hz.
        """
        if sample_rate != 16000:
            raise ValueError(f"Silero VAD requires 16000 Hz sample rate, got {sample_rate}")

        self.threshold = threshold
        self.sample_rate = sample_rate
        self._detector = _get_silero_detector()
        self._buffer = b''  # Buffer to accumulate audio to 1024 bytes
        self._last_confidence = 0.0

        # Diagnostic tracking
        self._last_speech_time = time.time()
        self._inference_count = 0
        self._speech_count = 0
        self._peak_confidence = 0.0  # Highest confidence since last speech
        self._stall_warned = False    # Avoid repeat warnings

    def is_speech(self, pcm_bytes: bytes) -> bool:
        """Determine if audio chunk contains speech.

        Args:
            pcm_bytes: Raw PCM audio bytes (16-bit signed, mono).
                      Any size accepted - internally buffers to 512 samples.

        Returns:
            True if speech confidence exceeds threshold.
        """
        # Add incoming audio to buffer
        self._buffer += pcm_bytes

        # Process when we have enough data (1024 bytes = 512 samples)
        if len(self._buffer) >= self.REQUIRED_BYTES:
            # Take exactly 1024 bytes
            chunk = self._buffer[:self.REQUIRED_BYTES]
            self._buffer = self._buffer[self.REQUIRED_BYTES:]

            # The detector takes raw 16-bit PCM bytes
            self._last_confidence = self._detector(chunk)
            self._inference_count += 1

            # Track peak confidence for diagnostics
            if self._last_confidence > self._peak_confidence:
                self._peak_confidence = self._last_confidence

        is_speech = self._last_confidence >= self.threshold
        if is_speech:
            self._last_speech_time = time.time()
            self._speech_count += 1
            self._peak_confidence = 0.0
            self._stall_warned = False

        return is_speech

    def get_confidence(self) -> float:
        """Get the last computed speech confidence score.

        Returns:
            Confidence score from 0.0 (silence/noise) to 1.0 (definite speech).
        """
        return self._last_confidence

    @property
    def diagnostics(self) -> dict:
        """Get VAD diagnostic state for periodic logging.

        Returns a dict with:
            - confidence: last raw score from the model
            - threshold: the detection threshold
            - idle_s: seconds since last speech detection
            - peak_confidence: highest score since last speech
            - inferences: total inference count
            - speech_frames: total frames detected as speech
        """
        return {
            "confidence": self._last_confidence,
            "threshold": self.threshold,
            "idle_s": time.time() - self._last_speech_time,
            "peak_confidence": self._peak_confidence,
            "inferences": self._inference_count,
            "speech_frames": self._speech_count,
        }

    @property
    def is_stalled(self) -> bool:
        """True if no speech detected for >30s despite audio flowing."""
        return (time.time() - self._last_speech_time) > 30.0

    def check_stall(self) -> str | None:
        """Check for VAD stall and return a warning message if stalled.

        Returns a log message string on first stall detection (>30s),
        None otherwise. Resets after speech resumes.
        """
        if self.is_stalled and not self._stall_warned:
            self._stall_warned = True
            idle_s = time.time() - self._last_speech_time
            return (
                f"[vad] WARNING: no speech detected for {idle_s:.0f}s "
                f"(confidence={self._last_confidence:.3f}, "
                f"peak={self._peak_confidence:.3f}, "
                f"threshold={self.threshold})"
            )
        return None

    def reset(self):
        """Reset internal state.

        Call this between utterances to clear the model's hidden state.
        """
        self._buffer = b''
        self._last_confidence = 0.0
        self._peak_confidence = 0.0
        if hasattr(self._detector, 'reset'):
            self._detector.reset()
