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
1. **Protocol over ABC** - Protocol was chosen so an existing class could
   satisfy the interface without inheriting from it. The class that made
   the argument, sounddevice's MicrophoneStream, is deleted
   (wh-portaudio-capture-removal); WinRTAudioCapture is now the only
   implementation, and it also satisfies the Protocol without inheriting.

2. **Immutable config** - AudioConfig is frozen to prevent runtime changes.
   To change settings, create a new provider.

3. **Stats for monitoring** - get_stats() returns a standardized dict for
   health monitoring, useful for detecting buffer issues.

4. **Readiness handshake** - start() alone does not prove the microphone
   opened: an asynchronous backend (WinRT) builds its graph on a
   background thread and start() returns first. wait_ready() is the
   handshake every provider must answer, so a caller can tell a live
   microphone from a denied one before announcing a working service
   (streaming-provider review .1.22). Because this is a Protocol, not a
   base class, the trivial answer cannot be inherited: a synchronous
   provider has to implement it directly. SounddeviceAudioCapture was
   the one that did, and it is deleted
   (wh-portaudio-capture-removal); every provider left is asynchronous.

   It answers for the present moment, not once. A microphone that
   opened and later died -- unplugged, or a graph whose frame polls
   have started raising -- must stop answering ready, or a caller that
   trusts the handshake reports a healthy capture path for a service
   that has been silent for an hour (wh-stt-load-metrics.2).

   Each provider can only report a death through the signals its own
   backend gives it, and neither one covers a device that stays alive
   by every such measure while delivering nothing; that case belongs to
   the load reporter's frame counter. What each provider does detect is
   named in its own wait_ready() (wh-stt-load-metrics.2.1.1).
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
    status_flags: int   # Frames that arrived with any backend status flag


@runtime_checkable
class AudioProvider(Protocol):
    """Protocol defining the audio capture interface.

    All audio providers must implement these methods. Using Protocol
    allows a class to be compatible without inheriting from this one.
    WinRTAudioCapture is the only implementation.

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

            def wait_ready(self, timeout: float = 15.0) -> bool:
                # Synchronous backend: start() already succeeded or raised
                return True
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

    def wait_ready(self, timeout: float = 15.0) -> bool:
        """Whether capture is running now, waiting out setup first.

        Call after start(). A provider whose setup runs on a background
        thread returns from start() before the microphone is known to
        work, so a caller that announces a working service on start()
        alone announces one that will never produce audio
        (streaming-provider review .1.22).

        Safe to keep calling. The answer is about this moment, so a
        provider whose device died after opening must report that here
        rather than repeating the setup result forever
        (wh-stt-load-metrics.2). Callers that poll pass timeout=0.0.

        Args:
            timeout: Maximum seconds to wait for setup to finish. Only
                setup is waited for; an outcome the provider already
                knows is returned immediately.

        Returns:
            True only while capture is running. False when setup failed
            (the provider's `setup_error` attribute then carries the
            reason as text), when the wait timed out with setup still
            unfinished (`setup_error` stays None), and when capture ran
            and has since stopped.
        """
        ...
