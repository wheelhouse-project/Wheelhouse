"""
Tests for AzureSTTProvider.

Tests initialization, capabilities, availability, and transcription.
All Azure Speech SDK calls are mocked.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stt.base import (
    TranscriptEvent,
    STTProviderNotAvailableError,
    STTProviderStartError,
    STTProviderError,
)


# Test fixtures


@pytest.fixture
def mock_azure_key():
    """Set up mock Azure subscription key."""
    with patch.dict(os.environ, {"AZURE_SPEECH_KEY": "test-subscription-key"}):
        yield


@pytest.fixture
def provider(mock_azure_key):
    """Create an AzureSTTProvider with mock credentials."""
    # Patch the azure imports
    with patch.dict("sys.modules", {
        "azure": MagicMock(),
        "azure.cognitiveservices": MagicMock(),
        "azure.cognitiveservices.speech": MagicMock(),
    }):
        from stt.providers.azure_provider import AzureSTTProvider
        return AzureSTTProvider(
            subscription_key="test-key",
            region="eastus",
            boost_words=["test", "hello"],
        )


# Test initialization


class TestAzureProviderInit:
    """Tests for AzureSTTProvider initialization."""

    def test_init_with_defaults(self, mock_azure_key):
        """Test basic initialization with defaults."""
        with patch.dict("sys.modules", {
            "azure": MagicMock(),
            "azure.cognitiveservices": MagicMock(),
            "azure.cognitiveservices.speech": MagicMock(),
        }):
            from stt.providers.azure_provider import AzureSTTProvider

            # Use environment variable
            provider = AzureSTTProvider()

            assert provider.region == "eastus"
            assert provider.language == "en-US"
            assert provider.enable_punctuation is True
            assert provider._boost_words == []

    def test_init_with_custom_options(self, mock_azure_key):
        """Test initialization with custom options."""
        with patch.dict("sys.modules", {
            "azure": MagicMock(),
            "azure.cognitiveservices": MagicMock(),
            "azure.cognitiveservices.speech": MagicMock(),
        }):
            from stt.providers.azure_provider import AzureSTTProvider
            provider = AzureSTTProvider(
                subscription_key="custom-key",
                region="westus2",
                language="es-ES",
                enable_punctuation=False,
                boost_words=["word1", "word2"],
            )

            assert provider.subscription_key == "custom-key"
            assert provider.region == "westus2"
            assert provider.language == "es-ES"
            assert provider.enable_punctuation is False
            assert provider._boost_words == ["word1", "word2"]

    def test_init_uses_env_var_for_key(self):
        """Test initialization uses AZURE_SPEECH_KEY env var."""
        with patch.dict(os.environ, {"AZURE_SPEECH_KEY": "env-key"}), \
             patch.dict("sys.modules", {
                 "azure": MagicMock(),
                 "azure.cognitiveservices": MagicMock(),
                 "azure.cognitiveservices.speech": MagicMock(),
             }):
            from stt.providers.azure_provider import AzureSTTProvider
            provider = AzureSTTProvider()
            
            assert provider.subscription_key == "env-key"

    def test_name_property(self, provider):
        """Test name property returns 'azure'."""
        assert provider.name == "azure"


# Test capabilities


class TestAzureProviderCapabilities:
    """Tests for AzureSTTProvider capabilities."""

    def test_capabilities(self, provider):
        """Test capability flags."""
        caps = provider.get_capabilities()

        assert caps.streaming is True
        assert caps.boost_list is True  # Azure supports phrase list
        assert caps.offline is False  # Requires internet
        assert caps.hot_reload_config is True
        assert "en-US" in caps.languages


# Test availability


class TestAzureProviderAvailability:
    """Tests for provider availability checks."""

    def test_not_available_without_sdk(self):
        """Test is_available returns False when SDK not installed."""
        with patch("stt.providers.azure_provider.AZURE_SPEECH_AVAILABLE", False):
            from stt.providers.azure_provider import AzureSTTProvider
            provider = AzureSTTProvider(subscription_key="test")
            assert provider.is_available is False

    def test_not_available_without_key(self):
        """Test is_available returns False when key not set."""
        with patch.dict(os.environ, {}, clear=True), \
             patch("stt.providers.azure_provider.AZURE_SPEECH_AVAILABLE", True):
            from stt.providers.azure_provider import AzureSTTProvider
            provider = AzureSTTProvider()  # No key passed
            assert provider.is_available is False

    def test_available_with_sdk_and_key(self, mock_azure_key):
        """Test is_available returns True when SDK installed and key set."""
        with patch("stt.providers.azure_provider.AZURE_SPEECH_AVAILABLE", True):
            from stt.providers.azure_provider import AzureSTTProvider
            provider = AzureSTTProvider(subscription_key="test-key")
            assert provider.is_available is True


# Test start/stop lifecycle


class TestAzureProviderStartStop:
    """Tests for provider start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_without_sdk_raises(self):
        """Test start raises when SDK not installed."""
        with patch("stt.providers.azure_provider.AZURE_SPEECH_AVAILABLE", False):
            from stt.providers.azure_provider import AzureSTTProvider
            provider = AzureSTTProvider(subscription_key="test")

            with pytest.raises(STTProviderNotAvailableError, match="azure-cognitiveservices-speech"):
                await provider.start()

    @pytest.mark.asyncio
    async def test_start_without_key_raises(self):
        """Test start raises when key not set."""
        with patch("stt.providers.azure_provider.AZURE_SPEECH_AVAILABLE", True), \
             patch.dict(os.environ, {}, clear=True):
            from stt.providers.azure_provider import AzureSTTProvider
            provider = AzureSTTProvider()  # No key

            with pytest.raises(STTProviderNotAvailableError, match="subscription key"):
                await provider.start()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(self, provider):
        """Test stop is safe to call without start."""
        await provider.stop()  # Should not raise
        assert provider._running is False


# Test boost word (phrase list) management


class TestAzureProviderBoostWords:
    """Tests for phrase list management."""

    @pytest.mark.asyncio
    async def test_add_boost_word(self, provider):
        """Test adding a phrase."""
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
        """Test removing a phrase."""
        result = await provider.remove_boost_word("test")

        assert result is True
        assert "test" not in provider._boost_words

    @pytest.mark.asyncio
    async def test_remove_nonexistent_word(self, provider):
        """Test removing phrase not in list returns False."""
        result = await provider.remove_boost_word("nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_clear_boost_list(self, provider):
        """Test clearing the phrase list."""
        await provider.clear_boost_list()

        assert provider._boost_words == []

    def test_get_boost_words(self, provider):
        """Test getting the phrase list."""
        words = provider.get_boost_words()

        assert words == ["test", "hello"]
        # Should be a copy, not the original
        words.append("modified")
        assert "modified" not in provider._boost_words


# Test STTManager integration


class TestSTTManagerAzureIntegration:
    """Tests for AzureSTTProvider integration with STTManager."""

    def test_create_azure_provider(self, mock_azure_key):
        """Test STTManager can create AzureSTTProvider."""
        with patch.dict("sys.modules", {
            "azure": MagicMock(),
            "azure.cognitiveservices": MagicMock(),
            "azure.cognitiveservices.speech": MagicMock(),
        }):
            from stt.stt_manager import STTManager
            from stt.providers.azure_provider import AzureSTTProvider

            manager = STTManager()
            provider = manager._create_provider(
                "azure",
                subscription_key="test-key",
            )

            assert isinstance(provider, AzureSTTProvider)

    def test_create_azure_provider_with_options(self, mock_azure_key):
        """Test STTManager passes options to AzureSTTProvider."""
        with patch.dict("sys.modules", {
            "azure": MagicMock(),
            "azure.cognitiveservices": MagicMock(),
            "azure.cognitiveservices.speech": MagicMock(),
        }):
            from stt.stt_manager import STTManager
            from stt.providers.azure_provider import AzureSTTProvider

            manager = STTManager()
            provider = manager._create_provider(
                "azure",
                subscription_key="test-key",
                region="westus2",
                language="es-ES",
                boost_words=["hola"],
            )

            assert provider.region == "westus2"
            assert provider.language == "es-ES"
            assert "hola" in provider._boost_words
