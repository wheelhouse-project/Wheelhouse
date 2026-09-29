"""Tests for clipboard_manager.py - Clipboard context manager with save/restore.

Tests cover:
- clipboard_context saves and restores clipboard text via pyperclip
- Restore delay behavior
- Error handling for unreadable clipboard
"""

from unittest.mock import Mock, patch, MagicMock

import pytest

_MOD = "utils.clipboard_manager"


@pytest.fixture
def mock_pyperclip():
    with patch(f"{_MOD}.pyperclip") as m:
        m.paste = Mock(return_value="original text")
        m.copy = Mock()
        yield m


class TestClipboardContext:
    """Tests for the clipboard_context context manager."""

    def test_saves_and_restores_text(self, mock_pyperclip):
        from utils.clipboard_manager import clipboard_context

        mock_pyperclip.paste.return_value = "hello"

        with clipboard_context():
            pass

        mock_pyperclip.copy.assert_called_once_with("hello")

    def test_restore_delay(self, mock_pyperclip):
        from utils.clipboard_manager import clipboard_context

        with patch(f"{_MOD}.time.sleep") as mock_sleep:
            with clipboard_context(restore_delay=0.5):
                pass

        mock_sleep.assert_any_call(0.5)

    def test_no_restore_when_save_failed(self, mock_pyperclip):
        from utils.clipboard_manager import clipboard_context

        mock_pyperclip.paste.side_effect = Exception("clipboard locked")

        with clipboard_context():
            pass

        mock_pyperclip.copy.assert_not_called()

    def test_context_returns_self(self, mock_pyperclip):
        from utils.clipboard_manager import clipboard_context

        with clipboard_context() as ctx:
            assert ctx is not None
            assert isinstance(ctx, clipboard_context)

    def test_restore_failure_logs_warning(self, mock_pyperclip):
        from utils.clipboard_manager import clipboard_context

        mock_pyperclip.paste.return_value = "text"
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")

        # Should not raise
        with clipboard_context():
            pass

    def test_empty_string_clipboard_still_restored(self, mock_pyperclip):
        from utils.clipboard_manager import clipboard_context

        mock_pyperclip.paste.return_value = ""

        with clipboard_context():
            pass

        mock_pyperclip.copy.assert_called_once_with("")

    def test_kwargs_accepted_for_backwards_compat(self, mock_pyperclip):
        """Old callers may pass retries/delay -- should not error."""
        from utils.clipboard_manager import clipboard_context

        with clipboard_context(retries=5, delay=0.05):
            pass


class TestSelectWindowsClipboard:
    """The process-start step that skips pyperclip's OS detection.

    Without it, the first copy() or paste() in each process runs
    pyperclip.determine_clipboard, which calls platform.system(); on Windows
    that can start a "cmd ver" child process. On a slow host that start
    held the Input command loop for 12.4 s and a spoken command expired
    (wh-input-first-paste-stall).
    """

    def test_paste_after_start_step_runs_no_os_detection(self, monkeypatch):
        import platform
        import subprocess

        import pyperclip

        from utils.clipboard_manager import select_windows_clipboard

        def _forbidden(*args, **kwargs):
            raise AssertionError("OS detection ran after the start step")

        # A fresh process: both pyperclip functions are still the lazy stubs.
        monkeypatch.setattr(pyperclip, "copy", pyperclip.lazy_load_stub_copy)
        monkeypatch.setattr(pyperclip, "paste", pyperclip.lazy_load_stub_paste)
        # platform.system itself must raise: uname() is cached, and
        # _win32_ver tries WMI before _syscmd_ver, so patching only
        # _syscmd_ver and Popen passes without the start step.
        monkeypatch.setattr(platform, "system", _forbidden)
        monkeypatch.setattr(platform, "_syscmd_ver", _forbidden)
        monkeypatch.setattr(platform, "_uname_cache", None, raising=False)
        monkeypatch.setattr(subprocess, "Popen", _forbidden)

        select_windows_clipboard()
        pyperclip.paste()

        # No real copy(): the developer's clipboard is never replaced.
        assert pyperclip.copy.__name__ == "copy_windows"
