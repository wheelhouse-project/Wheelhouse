"""
STT (Speech-to-Text) provider abstraction layer.

:flow: AudioCapture -> STTProvider -> TranscriptEvent -> STTManager -> LogicProcess
"""

from stt.base import (
    STTProvider,
    TranscriptEvent,
    ProviderCapabilities,
)
from stt.stt_manager import STTManager

__all__ = [
    "STTProvider",
    "TranscriptEvent",
    "ProviderCapabilities",
    "STTManager",
]
