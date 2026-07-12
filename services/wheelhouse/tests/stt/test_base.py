"""Tests for STT base classes and types."""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
from dataclasses import asdict

from stt.base import (
    TranscriptEvent,
    ProviderCapabilities,
    STTProvider,
    STTProviderError,
    STTProviderNotAvailableError,
    STTProviderStartError,
)


class TestTranscriptEvent:
    """Tests for TranscriptEvent dataclass."""

    def test_create_basic_event(self):
        """Test creating a basic transcript event."""
        event = TranscriptEvent(
            text="hello world",
            is_final=True,
            utterance_id=1,
        )
        assert event.text == "hello world"
        assert event.is_final is True
        assert event.utterance_id == 1
        assert event.confidence == 1.0  # Default

    def test_create_event_with_confidence(self):
        """Test creating event with explicit confidence."""
        event = TranscriptEvent(
            text="partial",
            is_final=False,
            utterance_id=2,
            confidence=0.75,
        )
        assert event.confidence == 0.75

    def test_event_is_dataclass(self):
        """Test that TranscriptEvent works as a dataclass."""
        event = TranscriptEvent("test", True, 1, 0.9)
        data = asdict(event)
        assert data == {
            "text": "test",
            "is_final": True,
            "utterance_id": 1,
            "confidence": 0.9,
        }


class TestProviderCapabilities:
    """Tests for ProviderCapabilities dataclass."""

    def test_default_capabilities(self):
        """Test default capability values."""
        caps = ProviderCapabilities()
        assert caps.streaming is True
        assert caps.boost_list is False
        assert caps.offline is False
        assert caps.hot_reload_config is False
        assert caps.languages == ["en-US"]

    def test_custom_capabilities(self):
        """Test creating capabilities with custom values."""
        caps = ProviderCapabilities(
            streaming=True,
            boost_list=True,
            offline=False,
            hot_reload_config=True,
            languages=["en-US", "en-GB", "es-ES"],
        )
        assert caps.boost_list is True
        assert caps.hot_reload_config is True
        assert len(caps.languages) == 3

    def test_offline_provider_capabilities(self):
        """Test capabilities for offline providers."""
        caps = ProviderCapabilities(
            streaming=True,
            boost_list=False,
            offline=True,
            hot_reload_config=False,
        )
        assert caps.offline is True
        assert caps.boost_list is False


class TestSTTProviderABC:
    """Tests for STTProvider abstract base class."""

    def test_cannot_instantiate_abstract(self):
        """Test that STTProvider cannot be instantiated directly."""
        with pytest.raises(TypeError):
            STTProvider()

    def test_boost_word_default_returns_false(self):
        """Test that default boost word methods return False."""

        class MinimalProvider(STTProvider):
            async def start(self):
                pass

            async def stop(self):
                pass

            async def transcribe_stream(self, audio_stream, callback):
                pass

            def get_capabilities(self):
                return ProviderCapabilities()

            @property
            def name(self):
                return "minimal"

            @property
            def is_available(self):
                return True

        provider = MinimalProvider()

        # Run async methods synchronously for testing
        import asyncio

        result = asyncio.run(provider.add_boost_word("test"))
        assert result is False

        result = asyncio.run(provider.remove_boost_word("test"))
        assert result is False


class TestSTTProviderExceptions:
    """Tests for STT provider exception hierarchy."""

    def test_exception_hierarchy(self):
        """Test exception class hierarchy."""
        assert issubclass(STTProviderNotAvailableError, STTProviderError)
        assert issubclass(STTProviderStartError, STTProviderError)
        assert issubclass(STTProviderError, Exception)

    def test_exception_messages(self):
        """Test exception message propagation."""
        error = STTProviderNotAvailableError("Model not found")
        assert str(error) == "Model not found"

        error = STTProviderStartError("Failed to initialize")
        assert str(error) == "Failed to initialize"
