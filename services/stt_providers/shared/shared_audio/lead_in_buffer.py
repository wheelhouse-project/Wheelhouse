"""Lead-in buffer for capturing pre-speech audio.

This module provides a circular buffer that captures audio before speech
detection triggers, ensuring the beginning of speech is not cut off.
This is critical for STT accuracy as words are often clipped without
a lead-in buffer.

Key Classes:
  - LeadInBuffer: Circular buffer that stores the most recent N seconds of audio.

Typical Usage:
  from shared.audio import LeadInBuffer

  buffer = LeadInBuffer(lead_time_s=0.3, sample_rate=16000)

  # During audio capture (before speech detected)
  for chunk in audio_stream:
      buffer.add(chunk)
      if vad.is_speech(chunk):
          # Get lead-in audio to include beginning of speech
          lead_in = buffer.get_lead_in()
          send_to_stt(lead_in + chunk)
          break
"""

from collections import deque
from typing import Optional


class LeadInBuffer:
    """Circular buffer for capturing pre-speech audio.

    This buffer maintains a sliding window of the most recent audio,
    allowing retrieval of audio from just before speech was detected.

    Attributes:
        lead_time_s: Duration of audio to buffer (in seconds).
        sample_rate: Audio sample rate in Hz.
        capacity_bytes: Maximum buffer size in bytes.
    """

    def __init__(
        self,
        lead_time_s: float = 0.3,
        sample_rate: int = 16000,
        bytes_per_sample: int = 2
    ):
        """Initialize the lead-in buffer.

        Args:
            lead_time_s: Duration of audio to keep (in seconds).
            sample_rate: Audio sample rate in Hz.
            bytes_per_sample: Bytes per audio sample (2 for 16-bit PCM).
        """
        self.lead_time_s = lead_time_s
        self.sample_rate = sample_rate
        self.bytes_per_sample = bytes_per_sample
        self.capacity_bytes = int(lead_time_s * sample_rate * bytes_per_sample)

        # Use deque of bytes chunks for efficient append/pop
        self._chunks: deque[bytes] = deque()
        self._total_bytes = 0

    @property
    def current_size(self) -> int:
        """Current amount of audio in the buffer (bytes)."""
        return self._total_bytes

    def add(self, pcm_bytes: bytes) -> None:
        """Add audio to the buffer.

        If adding this audio would exceed capacity, oldest audio is removed.

        Args:
            pcm_bytes: Raw PCM audio bytes to add.
        """
        if not pcm_bytes:
            return

        self._chunks.append(pcm_bytes)
        self._total_bytes += len(pcm_bytes)

        # Remove oldest chunks until we're under capacity
        while self._total_bytes > self.capacity_bytes and self._chunks:
            oldest = self._chunks.popleft()
            self._total_bytes -= len(oldest)

    def get_lead_in(self) -> bytes:
        """Get the buffered lead-in audio.

        Returns all buffered audio in chronological order (oldest first).
        Does not clear the buffer.

        Returns:
            Concatenated PCM audio bytes, or empty bytes if buffer is empty.
        """
        if not self._chunks:
            return b''

        return b''.join(self._chunks)

    def clear(self) -> None:
        """Clear all buffered audio."""
        self._chunks.clear()
        self._total_bytes = 0
