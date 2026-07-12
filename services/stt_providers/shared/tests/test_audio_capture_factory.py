"""Tests for shared_audio.capture.factory module.

Tests the factory functions that create audio capture providers with
backend selection and fallback logic.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from shared_audio.capture.factory import (
    get_available_providers,
    get_audio_provider,
)
from shared_audio.capture.base import AudioConfig


class TestGetAvailableProviders:
    """Test provider availability detection."""

    def test_returns_winrt_when_available(self):
        """Should include 'winrt' when WinRT is available."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.SOUNDDEVICE_AVAILABLE', False):
                providers = get_available_providers()
                assert 'winrt' in providers
                assert 'sounddevice' not in providers

    def test_returns_sounddevice_when_available(self):
        """Should include 'sounddevice' when sounddevice is available."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', False):
            with patch('shared_audio.capture.factory.SOUNDDEVICE_AVAILABLE', True):
                providers = get_available_providers()
                assert 'sounddevice' in providers
                assert 'winrt' not in providers

    def test_returns_both_when_both_available(self):
        """Should include both backends when both are available."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.SOUNDDEVICE_AVAILABLE', True):
                providers = get_available_providers()
                assert 'winrt' in providers
                assert 'sounddevice' in providers
                assert len(providers) == 2

    def test_returns_empty_when_none_available(self):
        """Should return empty list when no backends available."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', False):
            with patch('shared_audio.capture.factory.SOUNDDEVICE_AVAILABLE', False):
                providers = get_available_providers()
                assert providers == []


class TestGetAudioProviderWinRT:
    """Test factory with explicit WinRT backend."""

    def test_creates_winrt_provider_when_available(self):
        """Should create WinRTAudioCapture when backend='winrt' and available."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.WinRTAudioCapture') as MockWinRT:
                mock_instance = Mock()
                MockWinRT.return_value = mock_instance

                config = AudioConfig(rate=16000, channels=1)
                provider = get_audio_provider(config=config, backend='winrt')

                MockWinRT.assert_called_once_with(config, None)
                assert provider == mock_instance

    def test_raises_when_winrt_not_available(self):
        """Should raise RuntimeError when backend='winrt' but WinRT unavailable."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', False):
            with pytest.raises(RuntimeError, match="WinRT audio not available"):
                get_audio_provider(backend='winrt')

    def test_passes_overflow_callback_to_winrt(self):
        """Should pass overflow_callback to WinRT provider."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.WinRTAudioCapture') as MockWinRT:
                callback = Mock()
                config = AudioConfig()

                get_audio_provider(config=config, backend='winrt', overflow_callback=callback)

                MockWinRT.assert_called_once_with(config, callback)


class TestGetAudioProviderSounddevice:
    """Test factory with explicit sounddevice backend."""

    def test_creates_sounddevice_provider_when_available(self):
        """Should create SounddeviceAudioCapture when backend='sounddevice'."""
        with patch('shared_audio.capture.factory.SOUNDDEVICE_AVAILABLE', True):
            with patch('shared_audio.capture.factory.SounddeviceAudioCapture') as MockSD:
                mock_instance = Mock()
                MockSD.return_value = mock_instance

                config = AudioConfig(rate=16000, channels=1)
                provider = get_audio_provider(config=config, backend='sounddevice')

                MockSD.assert_called_once_with(config, None)
                assert provider == mock_instance

    def test_raises_when_sounddevice_not_available(self):
        """Should raise RuntimeError when backend='sounddevice' but unavailable."""
        with patch('shared_audio.capture.factory.SOUNDDEVICE_AVAILABLE', False):
            with pytest.raises(RuntimeError, match="sounddevice not available"):
                get_audio_provider(backend='sounddevice')

    def test_passes_overflow_callback_to_sounddevice(self):
        """Should pass overflow_callback to sounddevice provider."""
        with patch('shared_audio.capture.factory.SOUNDDEVICE_AVAILABLE', True):
            with patch('shared_audio.capture.factory.SounddeviceAudioCapture') as MockSD:
                callback = Mock()
                config = AudioConfig()

                get_audio_provider(config=config, backend='sounddevice', overflow_callback=callback)

                MockSD.assert_called_once_with(config, callback)


class TestGetAudioProviderAuto:
    """Test factory with auto backend selection."""

    def test_prefers_winrt_when_both_available(self):
        """Should prefer WinRT over sounddevice when both available."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.SOUNDDEVICE_AVAILABLE', True):
                with patch('shared_audio.capture.factory.WinRTAudioCapture') as MockWinRT:
                    with patch('shared_audio.capture.factory.SounddeviceAudioCapture') as MockSD:
                        mock_instance = Mock()
                        MockWinRT.return_value = mock_instance

                        provider = get_audio_provider(backend='auto')

                        MockWinRT.assert_called_once()
                        MockSD.assert_not_called()
                        assert provider == mock_instance

    def test_falls_back_to_sounddevice_when_winrt_unavailable(self):
        """Should use sounddevice when WinRT not available."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', False):
            with patch('shared_audio.capture.factory.SOUNDDEVICE_AVAILABLE', True):
                with patch('shared_audio.capture.factory.SounddeviceAudioCapture') as MockSD:
                    mock_instance = Mock()
                    MockSD.return_value = mock_instance

                    provider = get_audio_provider(backend='auto')

                    MockSD.assert_called_once()
                    assert provider == mock_instance

    def test_raises_when_no_backend_available(self):
        """Should raise RuntimeError when no backends available."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', False):
            with patch('shared_audio.capture.factory.SOUNDDEVICE_AVAILABLE', False):
                with pytest.raises(RuntimeError, match="No audio backend available"):
                    get_audio_provider(backend='auto')

    def test_uses_default_config_when_none_provided(self):
        """Should create default AudioConfig when config=None."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.WinRTAudioCapture') as MockWinRT:
                get_audio_provider()  # No config arg

                # Should be called with a default AudioConfig instance
                args, kwargs = MockWinRT.call_args
                config = args[0]
                assert isinstance(config, AudioConfig)
                assert config.rate == 16000
                assert config.channels == 1
                assert config.chunk_ms == 30


class TestGetAudioProviderAdversarial:
    """Adversarial tests for edge cases and unexpected inputs."""

    def test_handles_invalid_backend_string(self):
        """Should treat unknown backend as 'auto' (falls through to auto logic)."""
        # The factory doesn't validate backend strings, so invalid values
        # fall through to the 'else' (auto) branch
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.WinRTAudioCapture') as MockWinRT:
                mock_instance = Mock()
                MockWinRT.return_value = mock_instance

                # Invalid backend string should fall through to auto logic
                provider = get_audio_provider(backend='invalid_backend')

                MockWinRT.assert_called_once()
                assert provider == mock_instance

    def test_config_parameter_immutability(self):
        """Should not mutate the input config object."""
        original_config = AudioConfig(rate=8000, channels=2, chunk_ms=20)

        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.WinRTAudioCapture'):
                get_audio_provider(config=original_config, backend='winrt')

                # Config should remain unchanged (frozen dataclass)
                assert original_config.rate == 8000
                assert original_config.channels == 2
                assert original_config.chunk_ms == 20

    def test_overflow_callback_none_is_valid(self):
        """Should accept None as overflow_callback."""
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.WinRTAudioCapture') as MockWinRT:
                mock_instance = Mock()
                MockWinRT.return_value = mock_instance

                provider = get_audio_provider(backend='winrt', overflow_callback=None)

                MockWinRT.assert_called_once_with(AudioConfig(), None)
                assert provider == mock_instance
