"""Base audio provider protocol and configuration.

This module defines the common interface that all audio providers must implement.
It uses Python's Protocol for structural subtyping - providers don't need to
explicitly inherit, they just need to implement the required methods.

GLOSSARY
--------
- **Protocol** - Python typing construct for structural (duck) typing
- **AudioConfig** - Configuration dataclass for audio capture settings
- **AudioStats** - Monitoring statistics returned by get_stats()

KEY INSIGHTS
------------
1. **Protocol over ABC** - Using Protocol allows sounddevice's MicrophoneStream
   to be used without modification (it already implements the interface).

2. **Immutable config** - AudioConfig is frozen to prevent runtime changes.
   To change settings, create a new provider.

3. **Stats for monitoring** - get_stats() returns a standardized dict for
   health monitoring, useful for detecting buffer issues.
"""

from dataclasses import dataclass
from typing import Protocol, Optional, TypedDict, runtime_checkable


@dataclass(frozen=True)
class AudioConfig:
    """Configuration for audio capture.

    Attributes:
        rate: Sample rate in Hz. Default 16000 for STT.
        channels: Number of audio channels. Default 1 (mono).
        chunk_ms: Audio chunk duration in milliseconds. Default 30ms.
        device_index: Specific audio device, or None for system default.
    """
    rate: int = 16000
    channels: int = 1
    chunk_ms: int = 30
    device_index: Optional[int] = None

    @property
    def chunk_size(self) -> int:
        """Number of samples per chunk."""
        return int(self.rate * self.chunk_ms / 1000)

    @property
    def bytes_per_chunk(self) -> int:
        """Bytes per chunk (int16 = 2 bytes per sample)."""
        return self.chunk_size * self.channels * 2


class AudioStats(TypedDict, total=False):
    """Statistics from audio capture for monitoring.

    All fields are optional - providers report what they can.
    """
    captured: int       # Total frames captured
    drops: int          # Frames dropped due to queue full
    qsize: int          # Current queue depth
    max_q: int          # Maximum queue depth seen
    overflow_count: int # Hardware overflow events


@runtime_checkable
class AudioProvider(Protocol):
    """Protocol defining the audio capture interface.

    All audio providers must implement these methods. Using Protocol
    allows existing classes (like MicrophoneStream) to be compatible
    without modification.

    Example Implementation:
        ```python
        class MyAudioProvider:
            def start(self) -> None:
                # Begin capturing audio
                pass

            def stop(self) -> None:
                # Stop capturing and release resources
                pass

            def read(self, timeout: float = 1.0) -> Optional[bytes]:
                # Return audio chunk or None on timeout
                pass

            def get_stats(self) -> AudioStats:
                # Return capture statistics
                return {'captured': 0, 'drops': 0}
        ```
    """

    def start(self) -> None:
        """Start audio capture.

        Opens the audio device and begins capturing to internal queue.
        Subsequent calls should be no-ops if already started.
        """
        ...

    def stop(self) -> None:
        """Stop audio capture.

        Stops capture, closes device, and clears internal queue.
        Safe to call multiple times.
        """
        ...

    def read(self, timeout: float = 1.0) -> Optional[bytes]:
        """Read audio chunk from capture queue.

        Args:
            timeout: Maximum seconds to wait for audio. Default 1.0.

        Returns:
            Audio bytes (int16 PCM) or None if timeout elapsed.
        """
        ...

    def get_stats(self) -> AudioStats:
        """Get capture statistics for monitoring.

        Returns:
            Dict with capture stats (frames, drops, queue depth, etc.)
        """
        ...
