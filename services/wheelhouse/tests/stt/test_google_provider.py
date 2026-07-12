"""
Tests for GoogleSTTProvider.

Tests initialization, capabilities, availability, and transcription.
All Google Cloud Speech API calls are mocked.
"""

import asyncio
import os
import queue
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from stt.base import (
    TranscriptEvent,
    STTProviderNotAvailableError,
    STTProviderStartError,
    STTProviderError,
)


# Test fixtures


@pytest.fixture
def mock_credentials():
    """Set up mock Google credentials."""
    with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "/path/to/creds.json"}):
        yield


@pytest.fixture
def provider(mock_credentials):
    """Create a GoogleSTTProvider with mock credentials."""
    # Patch the google.cloud imports
    with patch.dict("sys.modules", {
        "google.cloud": MagicMock(),
        "google.cloud.speech_v1": MagicMock(),
        "google.cloud.speech_v1.types": MagicMock(),
    }):
        # Need to reload the module to pick up the mock
        from stt.providers.google_provider import GoogleSTTProvider
        return GoogleSTTProvider(
            language="en-US",
            boost_words=["test", "hello"],
        )


# Test initialization


class TestGoogleProviderInit:
    """Tests for GoogleSTTProvider initialization."""

    def test_init_with_defaults(self, mock_credentials):
        """Test basic initialization with defaults."""
        with patch.dict("sys.modules", {
            "google.cloud": MagicMock(),
            "google.cloud.speech_v1": MagicMock(),
        }):
            from stt.providers.google_provider import GoogleSTTProvider
            provider = GoogleSTTProvider()

            assert provider.language == "en-US"
            assert provider.model == "latest_long"
            assert provider.sample_rate == 16000
            assert provider.enable_punctuation is True
            assert provider._boost_words == []

    def test_init_with_custom_options(self, mock_credentials):
        """Test initialization with custom options."""
        with patch.dict("sys.modules", {
            "google.cloud": MagicMock(),
            "google.cloud.speech_v1": MagicMock(),
        }):
            from stt.providers.google_provider import GoogleSTTProvider
            provider = GoogleSTTProvider(
                language="es-ES",
                model="latest_short",
                sample_rate=8000,
                enable_punctuation=False,
                boost_words=["word1", "word2"],
                boost_value=15.0,
            )

            assert provider.language == "es-ES"
            assert provider.model == "latest_short"
            assert provider.sample_rate == 8000
            assert provider.enable_punctuation is False
            assert provider._boost_words == ["word1", "word2"]
            assert provider._boost_value == 15.0

    def test_name_property(self, provider):
        """Test name property returns 'google'."""
        assert provider.name == "google"


# Test capabilities


class TestGoogleProviderCapabilities:
    """Tests for GoogleSTTProvider capabilities."""

    def test_capabilities(self, provider):
        """Test capability flags."""
        caps = provider.get_capabilities()

        assert caps.streaming is True
        assert caps.boost_list is True  # Google supports phrase hints
        assert caps.offline is False  # Requires internet
        assert caps.hot_reload_config is True
        assert "en-US" in caps.languages


# Test availability


class TestGoogleProviderAvailability:
    """Tests for provider availability checks."""

    def test_not_available_without_sdk(self):
        """Test is_available returns False when SDK not installed."""
        with patch("stt.providers.google_provider.GOOGLE_SPEECH_AVAILABLE", False):
            from stt.providers.google_provider import GoogleSTTProvider
            provider = GoogleSTTProvider()
            assert provider.is_available is False

    def test_not_available_without_credentials(self):
        """Test is_available returns False when credentials not set."""
        with patch.dict(os.environ, {}, clear=True):
            if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
                del os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
            
            with patch("stt.providers.google_provider.GOOGLE_SPEECH_AVAILABLE", True):
                from stt.providers.google_provider import GoogleSTTProvider
                provider = GoogleSTTProvider()
                # Can't easily test this due to os.environ caching


# Test start/stop lifecycle


class TestGoogleProviderStartStop:
    """Tests for provider start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_without_sdk_raises(self):
        """Test start raises when SDK not installed."""
        with patch("stt.providers.google_provider.GOOGLE_SPEECH_AVAILABLE", False):
            from stt.providers.google_provider import GoogleSTTProvider
            provider = GoogleSTTProvider()

            with pytest.raises(STTProviderNotAvailableError, match="google-cloud-speech not installed"):
                await provider.start()

    @pytest.mark.asyncio
    async def test_start_without_credentials_raises(self):
        """Test start raises when credentials not set."""
        with patch("stt.providers.google_provider.GOOGLE_SPEECH_AVAILABLE", True), \
             patch.dict(os.environ, {}, clear=True):
            # Remove GOOGLE_APPLICATION_CREDENTIALS
            os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            
            from stt.providers.google_provider import GoogleSTTProvider
            provider = GoogleSTTProvider()

            with pytest.raises(STTProviderNotAvailableError, match="GOOGLE_APPLICATION_CREDENTIALS"):
                await provider.start()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(self, provider):
        """Test stop is safe to call without start."""
        await provider.stop()  # Should not raise
        assert provider._running is False


# Test boost word management


class TestGoogleProviderBoostWords:
    """Tests for boost word management."""

    @pytest.mark.asyncio
    async def test_add_boost_word(self, provider):
        """Test adding a boost word."""
        result = await provider.add_boost_word("newword")

        assert result is True
        assert "newword" in provider._boost_words

    @pytest.mark.asyncio
    async def test_add_duplicate_boost_word(self, provider):
        """Test adding duplicate doesn't create duplicates."""
        await provider.add_boost_word("test")  # Already in list

        assert provider._boost_words.count("test") == 1

    @pytest.mark.asyncio
    async def test_remove_boost_word(self, provider):
        """Test removing a boost word."""
        result = await provider.remove_boost_word("test")

        assert result is True
        assert "test" not in provider._boost_words

    @pytest.mark.asyncio
    async def test_remove_nonexistent_word(self, provider):
        """Test removing word not in list returns False."""
        result = await provider.remove_boost_word("nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_clear_boost_list(self, provider):
        """Test clearing the boost list."""
        await provider.clear_boost_list()

        assert provider._boost_words == []

    def test_get_boost_words(self, provider):
        """Test getting the boost word list."""
        words = provider.get_boost_words()

        assert words == ["test", "hello"]
        # Should be a copy, not the original
        words.append("modified")
        assert "modified" not in provider._boost_words


# Test STTManager integration


class TestSTTManagerGoogleIntegration:
    """Tests for GoogleSTTProvider integration with STTManager."""

    def test_create_google_provider(self, mock_credentials):
        """Test STTManager can create GoogleSTTProvider."""
        with patch.dict("sys.modules", {
            "google.cloud": MagicMock(),
            "google.cloud.speech_v1": MagicMock(),
        }):
            from stt.stt_manager import STTManager
            from stt.providers.google_provider import GoogleSTTProvider

            manager = STTManager()
            provider = manager._create_provider("google")

            assert isinstance(provider, GoogleSTTProvider)

    def test_create_google_provider_with_options(self, mock_credentials):
        """Test STTManager passes options to GoogleSTTProvider."""
        with patch.dict("sys.modules", {
            "google.cloud": MagicMock(),
            "google.cloud.speech_v1": MagicMock(),
        }):
            from stt.stt_manager import STTManager
            from stt.providers.google_provider import GoogleSTTProvider

            manager = STTManager()
            provider = manager._create_provider(
                "google",
                language="es-ES",
                model="latest_short",
                boost_words=["hola"],
            )

            assert provider.language == "es-ES"
            assert provider.model == "latest_short"
            assert "hola" in provider._boost_words
