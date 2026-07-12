"""
STT Provider base classes and types.

:flow: Defines STTProvider ABC that all providers implement.
:flow: TranscriptEvent is the unified output format from any provider.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Awaitable


@dataclass
class TranscriptEvent:
    """A transcript event from an STT provider.

    Attributes:
        text: The transcribed text.
        is_final: True if this is a final transcript (vs interim/partial).
        utterance_id: Unique ID for this utterance/segment.
        confidence: Confidence score 0.0-1.0 (default 1.0 if not provided).
    """

    text: str
    is_final: bool
    utterance_id: int
    confidence: float = 1.0


@dataclass
class ProviderCapabilities:
    """Describes what an STT provider can do.

    Used by the UI to show/hide features and by STTManager
    for fallback decisions.
    """

    streaming: bool = True
    """True if provider supports real-time streaming transcription."""

    boost_list: bool = False
    """True if provider supports dynamic word boosting/hints."""

    offline: bool = False
    """True if provider works without internet connection."""

    hot_reload_config: bool = False
    """True if provider can reload config without restart."""

    languages: list[str] = field(default_factory=lambda: ["en-US"])
    """List of supported language codes."""
    
    has_internal_vad: bool = False
    """True if provider handles VAD internally (skip external AudioCapture VAD)."""


# Type alias for transcript callbacks
TranscriptCallback = Callable[[TranscriptEvent], Awaitable[None]]


class STTProvider(ABC):
    """Abstract base class for STT providers.

    All providers (Google, Azure) implement this interface.
    The STTManager uses this to provide a unified API regardless of backend.

    :flow: STTManager creates appropriate provider based on config.
    :flow: Provider receives audio from AudioCapture, emits TranscriptEvents.
    """

    @abstractmethod
    async def start(self) -> None:
        """Start the STT provider.

        This should initialize any resources (models, connections, subprocesses)
        needed for transcription. Called once before transcription begins.

        Raises:
            STTProviderError: If provider fails to start.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the STT provider.

        Clean up resources, close connections, terminate subprocesses.
        Should be safe to call multiple times.
        """

    @abstractmethod
    async def transcribe_stream(
        self,
        audio_stream: AsyncIterator[bytes],
        callback: TranscriptCallback,
    ) -> None:
        """Transcribe an audio stream, invoking callback for each transcript.

        Args:
            audio_stream: Async iterator yielding audio chunks (16kHz, 16-bit PCM).
            callback: Called with each TranscriptEvent (interim and final).

        This method runs until the audio stream is exhausted or stop() is called.
        """

    @abstractmethod
    def get_capabilities(self) -> ProviderCapabilities:
        """Return provider capabilities.

        Used by UI to conditionally show features and by STTManager
        for intelligent fallback decisions.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short name for this provider (e.g., 'google', 'azure')."""

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """Check if provider is ready to use.

        Returns False if required resources (models, API keys, etc.) are missing.
        """

    # Optional boost list support - override in providers that support it

    async def add_boost_word(self, word: str, boost: float = 10.0) -> bool:
        """Add word to boost list. Returns False if unsupported."""
        return False

    async def remove_boost_word(self, word: str) -> bool:
        """Remove word from boost list. Returns False if unsupported."""
        return False

    async def clear_boost_list(self) -> None:
        """Clear all boost words."""


class STTProviderError(Exception):
    """Base exception for STT provider errors."""


class STTProviderNotAvailableError(STTProviderError):
    """Raised when a provider is not available (missing model, no API key, etc.)."""


class STTProviderStartError(STTProviderError):
    """Raised when a provider fails to start."""
