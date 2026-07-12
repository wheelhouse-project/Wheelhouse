"""Tests for clipboard_context pyperclip-based save/restore.

Validates that clipboard_context uses pyperclip (not win32clipboard
SetClipboardData) for restoration, preventing native heap corruption
(0xC0000374) from concurrent Win32 hook activity.

Also tests thread safety of UtteranceClipboardManager end_utterance.
"""
import threading
import pytest
from unittest.mock import patch, MagicMock

_MOD = "utils.clipboard_manager"


@pytest.fixture
def mock_pyperclip():
    """Mock pyperclip for unit testing without real clipboard access."""
    with patch(f"{_MOD}.pyperclip") as mock_pp:
        mock_pp.paste = MagicMock(return_value="saved text")
        mock_pp.copy = MagicMock()
        yield mock_pp


class TestPyperclipSaveRestore:
    """clipboard_context should use pyperclip for save and restore."""

    def test_save_uses_pyperclip_paste(self, mock_pyperclip):
        """__enter__ should call pyperclip.paste() to save text."""
        from utils.clipboard_manager import clipboard_context

        ctx = clipboard_context()
        ctx.__enter__()

        mock_pyperclip.paste.assert_called_once()
        assert ctx._saved_text == "saved text"

    def test_restore_uses_pyperclip_copy(self, mock_pyperclip):
        """__exit__ should call pyperclip.copy() to restore text."""
        from utils.clipboard_manager import clipboard_context

        ctx = clipboard_context()
        ctx.__enter__()
        ctx.__exit__(None, None, None)

        mock_pyperclip.copy.assert_called_once_with("saved text")

    def test_no_win32clipboard_used(self, mock_pyperclip):
        """clipboard_context should not import or use win32clipboard."""
        from utils.clipboard_manager import clipboard_context
        import utils.clipboard_manager as mod

        assert not hasattr(mod, 'win32clipboard'), \
            "clipboard_manager should not use win32clipboard (heap corruption risk)"

    def test_save_failure_prevents_restore(self, mock_pyperclip):
        """If pyperclip.paste() fails, __exit__ should not call copy()."""
        from utils.clipboard_manager import clipboard_context

        mock_pyperclip.paste.side_effect = Exception("locked")

        ctx = clipboard_context()
        ctx.__enter__()
        ctx.__exit__(None, None, None)

        mock_pyperclip.copy.assert_not_called()


class TestUtteranceClipboardThreadSafety:
    """Thread safety of UtteranceClipboardManager.end_utterance."""

    def test_concurrent_end_utterance_safe(self):
        """Two threads calling end_utterance should not corrupt state."""
        from ui.utterance_clipboard_manager import UtteranceClipboardManager

        mgr = UtteranceClipboardManager(timeout_seconds=10.0)

        with patch("ui.utterance_clipboard_manager.threading") as mock_threading:
            mock_threading.Timer.return_value = MagicMock()
            mock_threading.current_thread.return_value = threading.main_thread()
            mock_threading.main_thread.return_value = threading.main_thread()

            with patch("ui.utterance_clipboard_manager.pyperclip") as mock_pp:
                mock_pp.paste.return_value = "saved"
                mock_pp.copy = MagicMock()

                mgr.start_utterance(100)
                mgr._saved_text = "saved"
                mgr.mark_clipboard_dirty()

                barrier = threading.Barrier(2)

                def call_end():
                    barrier.wait()
                    mgr.end_utterance(100)

                t1 = threading.Thread(target=call_end)
                t2 = threading.Thread(target=call_end)
                t1.start()
                t2.start()
                t1.join(timeout=5)
                t2.join(timeout=5)

                assert mgr.is_in_utterance() is False
