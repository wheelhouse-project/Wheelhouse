"""Tests for shared_audio.capture.factory.

The factory has exactly one path. David ruled on 2026-09-05: "The rule is
we always use WinRT." So there is no backend argument, no selection, and
no fallback to sounddevice. A provider whose venv cannot load winsdk gets
a RuntimeError carrying the approved refusal text, and refuses to start
(wh-capture-winrt-required).

The tests that used to live here pinned the opposite behaviour: they
required a sounddevice fallback and a backend parameter. They are gone
because the behaviour they protected is gone.
"""

import builtins
import contextlib
import inspect
from pathlib import Path

import pytest
from unittest.mock import Mock, patch, MagicMock

from shared_audio.capture import factory
from shared_audio.capture.factory import (
    WINRT_REQUIRED_MESSAGE,
    get_audio_provider,
)
from shared_audio.capture.base import AudioConfig
from shared_audio.capture import winrt_capture
from shared_audio.capture.winrt_capture import WinRTAudioCapture


# The refusal text David approved, written out here rather than imported,
# so a change to the constant fails this test instead of hiding behind it.
APPROVED_REFUSAL = (
    "The speech service cannot start: the audio package winsdk is not "
    "installed. Re-run the WheelHouse installer. Developers: run "
    "bootstrap.ps1."
)


@contextlib.contextmanager
def _winsdk_not_installed():
    """Every import of winsdk raises, as it does without the wheel.

    builtins.__import__ is patched rather than sys.modules, because the
    import statement calls __import__ before the default implementation
    consults sys.modules -- and by this point in a run sys.modules can hold
    either the real winsdk or the stand-in that tests/test_winrt_capture.py
    installs.
    """
    real_import = builtins.__import__

    def _refuse(name, *args, **kwargs):
        if name == "winsdk" or name.startswith("winsdk."):
            raise ImportError("No module named 'winsdk'")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", _refuse):
        yield


@contextlib.contextmanager
def _winsdk_installed():
    """Every import of winsdk succeeds.

    winsdk is a required dependency of this shared package since
    wh-capture-winrt-required, so the real import already succeeds in this
    venv. The stand-in stays because the test must hold whether or not the
    venv was synced, and because it keeps this half symmetric with
    _winsdk_not_installed.
    """
    real_import = builtins.__import__

    def _supply(name, *args, **kwargs):
        if name == "winsdk" or name.startswith("winsdk."):
            return MagicMock(name=name)
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", _supply):
        yield


class TestTheFactoryReturnsWinRTAndNothingElse:
    """The one path."""

    def test_returns_a_winrt_capture_when_winrt_loads(self):
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.WinRTAudioCapture') as MockWinRT:
                mock_instance = Mock()
                MockWinRT.return_value = mock_instance

                config = AudioConfig(rate=16000, channels=1)
                provider = get_audio_provider(config=config)

                MockWinRT.assert_called_once_with(config, None)
                assert provider is mock_instance

    def test_passes_the_overflow_callback_through(self):
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.WinRTAudioCapture') as MockWinRT:
                callback = Mock()
                config = AudioConfig()

                get_audio_provider(config=config, overflow_callback=callback)

                MockWinRT.assert_called_once_with(config, callback)

    def test_uses_a_default_config_when_none_is_given(self):
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.WinRTAudioCapture') as MockWinRT:
                get_audio_provider()

                args, _kwargs = MockWinRT.call_args
                config = args[0]
                assert isinstance(config, AudioConfig)
                assert config.rate == 16000
                assert config.channels == 1
                assert config.chunk_ms == 30

    def test_does_not_mutate_the_config_it_is_given(self):
        original_config = AudioConfig(rate=8000, channels=2, chunk_ms=20)

        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.WinRTAudioCapture'):
                get_audio_provider(config=original_config)

        assert original_config.rate == 8000
        assert original_config.channels == 2
        assert original_config.chunk_ms == 20


class TestTheFactoryRefusesWhenWinRTCannotLoad:
    """No fallback, and the refusal says what to do about it."""

    def test_raises_runtime_error_when_winrt_does_not_load(self):
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', False):
            with pytest.raises(RuntimeError):
                get_audio_provider()

    def test_the_refusal_text_is_exactly_the_approved_wording(self):
        """The text reaches the user through the provider startup notice,
        so a reworded message is a user-visible change, not a detail.
        """
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', False):
            with pytest.raises(RuntimeError) as excinfo:
                get_audio_provider()

        assert str(excinfo.value) == APPROVED_REFUSAL
        assert WINRT_REQUIRED_MESSAGE == APPROVED_REFUSAL

    def test_builds_no_capture_at_all_when_winrt_does_not_load(self):
        """The refusal must be a refusal. A factory that raised after
        constructing something would leave a microphone open.
        """
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', False):
            with patch('shared_audio.capture.factory.WinRTAudioCapture') as MockWinRT:
                with pytest.raises(RuntimeError):
                    get_audio_provider()

        MockWinRT.assert_not_called()


class TestTheSelectionApiIsGone:
    """A2 asks for these absences by name, so each one is a test.

    A caller that still passes backend= must fail loudly rather than have
    the argument ignored, and no import of the sounddevice capture may
    remain in this module.
    """

    def test_there_is_no_backend_parameter(self):
        parameters = inspect.signature(get_audio_provider).parameters
        assert list(parameters) == ["config", "overflow_callback"]

    def test_a_backend_argument_is_rejected(self):
        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE', True):
            with patch('shared_audio.capture.factory.WinRTAudioCapture'):
                with pytest.raises(TypeError):
                    get_audio_provider(backend='sounddevice')

    def test_the_module_exposes_no_selection_helpers(self):
        assert not hasattr(factory, "get_available_providers")
        assert not hasattr(factory, "AudioBackend")
        assert not hasattr(factory, "SounddeviceAudioCapture")
        assert not hasattr(factory, "SOUNDDEVICE_AVAILABLE")

    def test_the_source_imports_no_sounddevice_capture(self):
        """The grep A2 asks for, run as a test so it cannot be forgotten.

        An import of sounddevice_capture in this module would load
        PortAudio in every provider, which is the dependency the WinRT
        rule exists to remove. Only import lines are examined: the module
        docstring names sounddevice on purpose, to record why the second
        path went away.

        Each line is stripped before the check, so an import INSIDE a
        function counts too. It used to test the raw line, which starts
        with spaces when the import is indented, and a function-local
        "from .sounddevice_capture import ..." therefore passed unseen --
        while loading PortAudio just as surely once that function ran.
        The gap was found by the mutation
        factory-falls-back-to-sounddevice in
        tests/mutation_gate_winrt_capture_required.py, which restores the
        old fallback with exactly such an import.
        """
        source = Path(factory.__file__).read_text(encoding="utf-8")
        import_lines = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        assert import_lines, "no import lines found; the check read nothing"
        assert not [line for line in import_lines if "sounddevice" in line]
        assert "AudioBackend" not in source
        assert "get_available_providers" not in source


class TestTheWinsdkImportDecidesTheAnswer:
    """What makes WINRT_AUDIO_AVAILABLE true or false in the first place.

    Every other test in this file sets WINRT_AUDIO_AVAILABLE by hand, so
    all of them would keep passing if the check that computes it stopped
    working (wh-stt-load-metrics.3).
    """

    def test_a_failed_winsdk_import_makes_winrt_unavailable(self):
        with _winsdk_not_installed():
            assert winrt_capture._is_winrt_available() is False

    def test_an_installed_winsdk_makes_winrt_available(self):
        with _winsdk_installed():
            assert winrt_capture._is_winrt_available() is True

    def test_a_missing_winsdk_makes_the_factory_refuse(self):
        """End to end, with the flag computed rather than asserted. This is
        the case David's machine hit on 2026-09-05: winsdk was absent from
        the Parakeet venv, and the old factory chose sounddevice silently.
        """
        with _winsdk_not_installed():
            available = winrt_capture._is_winrt_available()

        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE',
                   available):
            with pytest.raises(RuntimeError) as excinfo:
                get_audio_provider()

        assert str(excinfo.value) == APPROVED_REFUSAL

    def test_a_present_winsdk_makes_the_factory_build_winrt(self):
        """The other half, and the one that catches a factory that refuses
        whatever happens.
        """
        with _winsdk_installed():
            available = winrt_capture._is_winrt_available()

        with patch('shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE',
                   available):
            provider = get_audio_provider()

        assert isinstance(provider, WinRTAudioCapture)
