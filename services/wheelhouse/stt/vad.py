"""Voice Activity Detection wrapper using Silero VAD.

Ported from google_stt_server for use with in-process STT providers.
Uses pysilero-vad package which provides a pre-trained neural network
for accurate speech detection.

:flow: AudioCapture -> VAD -> STTProvider
:flow: Filters silent audio to prevent unnecessary processing.
"""

import logging

logger = logging.getLogger(__name__)

# Lazy import to avoid loading model at module import time
_silero_detector = None


def _get_silero_detector():
    """Lazy-load the Silero VAD detector."""
    global _silero_detector
    if _silero_detector is None:
        from pysilero_vad import SileroVoiceActivityDetector
        _silero_detector = SileroVoiceActivityDetector()
        logger.debug("Silero VAD model loaded")
    return _silero_detector


class SileroVAD:
    """Neural network-based Voice Activity Detector using Silero VAD.
    
    Uses ONNX runtime internally for efficient inference.
    Requires 16kHz mono audio.
    
    :flow: Accepts PCM bytes, returns speech/silence decision.
    :flow: Buffers internally to 512-sample chunks required by model.
    
    Example:
        vad = SileroVAD(threshold=0.5)
        for audio_chunk in audio_stream:
            if vad.is_speech(audio_chunk):
                process_speech(audio_chunk)
    """
    
    # pysilero-vad requires exactly 512 samples = 1024 bytes of 16-bit PCM
    REQUIRED_BYTES = 1024
    
    def __init__(self, threshold: float = 0.5, sample_rate: int = 16000):
        """Initialize Silero VAD.
        
        Args:
            threshold: Confidence threshold for speech detection (0.0-1.0).
                      Higher = fewer false positives, may miss quiet speech.
                      Lower = catch more speech, more false positives.
                      Default 0.5 is a balanced starting point.
            sample_rate: Audio sample rate. Must be 16000 Hz.
            
        Raises:
            ValueError: If sample_rate is not 16000.
        """
        if sample_rate != 16000:
            raise ValueError(f"Silero VAD requires 16000 Hz sample rate, got {sample_rate}")
        
        self.threshold = threshold
        self.sample_rate = sample_rate
        self._detector = _get_silero_detector()
        self._buffer = b''  # Buffer to accumulate audio to 1024 bytes
        self._last_confidence = 0.0
    
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
        
        return self._last_confidence >= self.threshold
    
    def get_confidence(self) -> float:
        """Get the last computed speech confidence score.
        
        Returns:
            Confidence score from 0.0 (silence/noise) to 1.0 (definite speech).
        """
        return self._last_confidence
    
    def reset(self):
        """Reset internal state.
        
        Call this between utterances to clear the model's hidden state.
        """
        self._buffer = b''
        self._last_confidence = 0.0
        if hasattr(self._detector, 'reset'):
            self._detector.reset()
