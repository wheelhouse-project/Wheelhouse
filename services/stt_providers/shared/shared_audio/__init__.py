"""Shared audio processing components."""
from .agc import SmartAGC, AGCConfig
from .silero_vad import SileroVAD
from .lead_in_buffer import LeadInBuffer
from .overflow_monitor import OverflowMonitor, OverflowConfig
from .microphone import MicrophoneStream
from .diagnostics import run_mic_check

# Re-export capture module components
from .capture import (
    AudioProvider,
    AudioConfig,
    AudioStats,
    get_audio_provider,
    get_available_providers,
)

__all__ = [
    # Audio processing
    "SmartAGC",
    "AGCConfig",
    "SileroVAD",
    "LeadInBuffer",
    # Overflow monitoring
    "OverflowMonitor",
    "OverflowConfig",
    # Microphone capture
    "MicrophoneStream",
    # Audio capture abstraction
    "AudioProvider",
    "AudioConfig",
    "AudioStats",
    "get_audio_provider",
    "get_available_providers",
    # Diagnostics
    "run_mic_check",
]
