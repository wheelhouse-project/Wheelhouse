"""Sounddevice-based audio capture for STT services.

This module wraps the MicrophoneStream implementation to provide
the AudioProvider interface, enabling use as a fallback when WinRT is
not available.

OVERVIEW
--------
This is an adapter layer that:
1. Wraps the MicrophoneStream class
2. Provides the AudioProvider protocol interface
3. Maintains full backward compatibility

The actual audio capture logic is in the microphone module.
This wrapper just bridges the interface.
"""

import logging
from typing import Optional, Callable

from .base import AudioConfig, AudioStats

logger = logging.getLogger(__name__)


def _is_sounddevice_available() -> bool:
    """Check if sounddevice is available."""
    try:
        import sounddevice  # noqa: F401
        return True
    except ImportError:
        return False


SOUNDDEVICE_AVAILABLE = _is_sounddevice_available()


class SounddeviceAudioCapture:
    """Audio capture using sounddevice (PortAudio).

    Wraps the MicrophoneStream class to provide the AudioProvider
    interface. Use this as fallback when WinRT is not available.

    Args:
        config: Audio configuration (rate, channels, chunk_ms).
        overflow_callback: Optional callback when buffer overflows.

    Example:
        ```python
        config = AudioConfig(rate=16000, chunk_ms=30)
        capture = SounddeviceAudioCapture(config)
        capture.start()

        while running:
            audio = capture.read(timeout=1.0)
            if audio:
                process(audio)

        capture.stop()
        ```
    """

    def __init__(
        self,
        config: Optional[AudioConfig] = None,
        overflow_callback: Optional[Callable] = None
    ):
        """Initialize sounddevice audio capture.

        Args:
            config: Audio configuration. Defaults to 16kHz mono 30ms.
            overflow_callback: Called when audio buffer overflows.
        """
        self.config = config or AudioConfig()
        self.overflow_callback = overflow_callback
        self._stream = None

    def start(self) -> None:
        """Start audio capture."""
        if self._stream is not None:
            return

        if not SOUNDDEVICE_AVAILABLE:
            raise RuntimeError("sounddevice not available")

        # Import here to avoid import errors when not available
        from ..microphone import MicrophoneStream

        self._stream = MicrophoneStream(
            rate=self.config.rate,
            channels=self.config.channels,
            chunk_ms=self.config.chunk_ms,
            device_index=self.config.device_index,
            overflow_callback=self.overflow_callback
        )
        self._stream.start()

        logger.debug(
            f"sounddevice audio started: {self.config.rate}Hz, "
            f"{self.config.channels}ch, {self.config.chunk_ms}ms chunks"
        )

    def stop(self) -> None:
        """Stop audio capture."""
        if self._stream:
            self._stream.stop()
            self._stream = None
        logger.debug("sounddevice audio stopped")

    def read(self, timeout: float = 1.0) -> Optional[bytes]:
        """Read audio chunk from capture queue.

        Args:
            timeout: Maximum seconds to wait for audio.

        Returns:
            Audio bytes (int16 PCM) or None if timeout.
        """
        if self._stream:
            return self._stream.read(timeout=timeout)
        return None

    def get_stats(self) -> AudioStats:
        """Get capture statistics."""
        if self._stream:
            return self._stream.get_stats_snapshot()
        return {'captured': 0, 'drops': 0, 'qsize': 0, 'max_q': 0}

    def get_queue_size(self) -> int:
        """Get current queue depth."""
        if self._stream:
            return self._stream.get_queue_size()
        return 0

    @property
    def overflow_monitor(self):
        """Access underlying overflow monitor from MicrophoneStream."""
        if self._stream:
            return self._stream.overflow_monitor
        return None

    def reset_overflow_monitor(self) -> None:
        """Reset overflow monitoring state after restart."""
        if self._stream:
            self._stream.reset_overflow_monitor()

    def get_overflow_status(self) -> dict:
        """Get current overflow monitoring status for debugging."""
        if self._stream:
            return self._stream.get_overflow_status()
        return {}

    def list_audio_devices(self):
        """List available audio input devices."""
        if self._stream:
            return self._stream.list_audio_devices()

        # Create temporary stream just to list devices
        if not SOUNDDEVICE_AVAILABLE:
            return []

        from ..microphone import MicrophoneStream
        temp = MicrophoneStream()
        devices = temp.list_audio_devices()
        return devices
