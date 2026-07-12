"""Audio provider factory for STT services.

This module provides factory functions to create audio providers,
handling backend selection and fallback logic.

OVERVIEW
--------
Use get_audio_provider() to get an audio capture instance. The factory:

1. Checks WinRT availability (preferred on Windows 10+)
2. Falls back to sounddevice if WinRT unavailable
3. Allows explicit backend selection via parameter

This abstraction allows the STT service to work regardless of which
audio backend is available.
"""

import logging
from typing import Optional, Literal

from .base import AudioProvider, AudioConfig
from .winrt_capture import WinRTAudioCapture, WINRT_AUDIO_AVAILABLE
from .sounddevice_capture import SounddeviceAudioCapture, SOUNDDEVICE_AVAILABLE

logger = logging.getLogger(__name__)

AudioBackend = Literal['winrt', 'sounddevice', 'auto']


def get_available_providers() -> list[str]:
    """Get list of available audio backends.

    Returns:
        List of available backend names ('winrt', 'sounddevice').
    """
    available = []
    if WINRT_AUDIO_AVAILABLE:
        available.append('winrt')
    if SOUNDDEVICE_AVAILABLE:
        available.append('sounddevice')
    return available


def get_audio_provider(
    config: Optional[AudioConfig] = None,
    backend: AudioBackend = 'auto',
    overflow_callback=None
) -> AudioProvider:
    """Create an audio provider instance.

    Factory function that creates the appropriate audio capture backend
    based on availability and preference.

    Args:
        config: Audio configuration. Defaults to 16kHz mono 30ms.
        backend: Which backend to use:
            - 'auto': Try WinRT first, fall back to sounddevice
            - 'winrt': Use WinRT only (raises if unavailable)
            - 'sounddevice': Use sounddevice only (raises if unavailable)
        overflow_callback: Called when audio queue overflows.

    Returns:
        AudioProvider instance ready for use.

    Raises:
        RuntimeError: If requested backend is not available.

    Example:
        ```python
        # Auto-select best available backend
        provider = get_audio_provider()

        # Force specific backend
        provider = get_audio_provider(backend='winrt')

        # Custom configuration
        config = AudioConfig(rate=16000, chunk_ms=20)
        provider = get_audio_provider(config=config)
        ```
    """
    config = config or AudioConfig()

    if backend == 'winrt':
        if not WINRT_AUDIO_AVAILABLE:
            raise RuntimeError("WinRT audio not available")
        logger.info("Using WinRT audio capture")
        return WinRTAudioCapture(config, overflow_callback)

    elif backend == 'sounddevice':
        if not SOUNDDEVICE_AVAILABLE:
            raise RuntimeError("sounddevice not available")
        logger.info("Using sounddevice audio capture")
        return SounddeviceAudioCapture(config, overflow_callback)

    else:  # auto
        if WINRT_AUDIO_AVAILABLE:
            logger.info("Using WinRT audio capture (auto-selected)")
            return WinRTAudioCapture(config, overflow_callback)
        elif SOUNDDEVICE_AVAILABLE:
            logger.info("Using sounddevice audio capture (fallback)")
            return SounddeviceAudioCapture(config, overflow_callback)
        else:
            raise RuntimeError(
                "No audio backend available. Install winsdk or sounddevice."
            )
