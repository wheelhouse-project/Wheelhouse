"""Clipboard preservation context manager.

Saves and restores clipboard text content around operations that temporarily
modify the clipboard (e.g., clipboard-based text insertion).

Uses pyperclip for both save and restore. pyperclip.copy() writes a fresh
Python string to the clipboard, avoiding the native heap corruption
(0xC0000374) that occurs with win32clipboard.SetClipboardData when the
heap has been damaged by concurrent Win32 hook activity.

Typical Usage:
    from utils.clipboard_manager import clipboard_context

    with clipboard_context():
        pyperclip.copy("temporary text")
        perform_paste_operation()

    # Original clipboard text automatically restored
"""
import pyperclip
import logging

from utils.redact import redact_transcript
import time

logger = logging.getLogger(__name__)


class clipboard_context:
    """Context manager that saves and restores clipboard text content.

    Args:
        restore_delay: Delay before restoring clipboard on exit (seconds).
                      Provides buffer for paste operations to complete
                      before clipboard content changes back.
    """
    def __init__(self, restore_delay=0.0, **kwargs):
        self.restore_delay = restore_delay
        self._saved_text = None

    def __enter__(self):
        """Save clipboard text content."""
        try:
            self._saved_text = pyperclip.paste()
            logger.debug(
                "Clipboard SAVE len=%d text=%r",
                len(self._saved_text) if self._saved_text else 0,
                redact_transcript(self._saved_text) if self._saved_text else "",
            )
        except Exception as e:
            logger.warning(f"Could not save clipboard text: {e}")
            self._saved_text = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Restore clipboard text content."""
        if self.restore_delay > 0:
            time.sleep(self.restore_delay)

        if self._saved_text is None:
            return

        try:
            pyperclip.copy(self._saved_text)
            logger.debug(
                "Clipboard RESTORE len=%d text=%r",
                len(self._saved_text),
                redact_transcript(self._saved_text) if self._saved_text else "",
            )
        except Exception as e:
            logger.warning(f"Could not restore clipboard text: {e}")
