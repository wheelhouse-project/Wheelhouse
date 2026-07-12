"""Silero VAD wrapper for voice activity detection.

This module provides a wrapper around the Silero VAD neural network model
using the pysilero-vad package. It offers significantly better accuracy
than WebRTC VAD for distinguishing speech from environmental noise.

Key Classes:
  - SileroVAD: Neural network-based VAD with configurable threshold.

Typical Usage:
  from shared.audio import SileroVAD

  vad = SileroVAD(threshold=0.5)

  for audio_chunk in audio_stream:
      if vad.is_speech(audio_chunk):
          process_speech(audio_chunk)
"""

import time

# Lazy import to avoid loading model at module import time
_silero_detector = None


def _get_silero_detector():
    """Lazy-load the Silero VAD detector."""
    global _silero_detector
    if _silero_detector is None:
        from pysilero_vad import SileroVoiceActivityDetector
        _silero_detector = SileroVoiceActivityDetector()
    return _silero_detector


class SileroVAD:
    """Neural network-based Voice Activity Detector using Silero VAD.

    Provides the same interface as VoiceActivityDetector for drop-in replacement.
    Uses ONNX runtime internally for efficient inference.

    Attributes:
        threshold: Confidence threshold (0.0-1.0) for speech detection.
        sample_rate: Audio sample rate (must be 16000).
    """

    # pysilero-vad requires exactly 512 samples = 1024 bytes of 16-bit PCM
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

            # pysilero-vad expects raw 16-bit PCM bytes
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
