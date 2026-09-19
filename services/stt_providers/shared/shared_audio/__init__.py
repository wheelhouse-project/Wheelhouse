"""Shared audio processing components."""

#: Root of the capture-path logger tree. Every module in this package
#: logs to logging.getLogger(__name__), so a handler on this one name
#: sees the whole capture path -- the device-open error, the overflow
#: warning, and OverflowMonitor's summary. A provider forwards this to
#: make the capture path readable off its own console
#: (wh-stt-load-metrics.2). Derived from __name__ so a package rename
#: cannot leave a forwarder pointed at a logger nobody writes to.
CAPTURE_LOGGER_NAME = __name__
from .agc import SmartAGC, AGCConfig
from .silero_vad import SileroVAD
from .lead_in_buffer import LeadInBuffer
from .overflow_monitor import OverflowMonitor, OverflowConfig
from .diagnostics import run_mic_check

# Re-export capture module components
from .capture import (
    AudioProvider,
    AudioConfig,
    AudioStats,
    get_audio_provider,
    WINRT_REQUIRED_MESSAGE,
    AUDIO_DEVICE_MISSING_MESSAGE,
    AUDIO_DEVICE_MISSING_LOG_LINE,
    CAPTURE_BACKEND_NAME,
)

__all__ = [
    # Logging
    "CAPTURE_LOGGER_NAME",
    # Audio processing
    "SmartAGC",
    "AGCConfig",
    "SileroVAD",
    "LeadInBuffer",
    # Overflow monitoring
    "OverflowMonitor",
    "OverflowConfig",
    # Audio capture abstraction
    "AudioProvider",
    "AudioConfig",
    "AudioStats",
    "get_audio_provider",
    "WINRT_REQUIRED_MESSAGE",
    "AUDIO_DEVICE_MISSING_MESSAGE",
    "AUDIO_DEVICE_MISSING_LOG_LINE",
    "CAPTURE_BACKEND_NAME",
    # Diagnostics
    "run_mic_check",
]
