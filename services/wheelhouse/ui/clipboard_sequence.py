"""Win32 clipboard sequence number wrapper for adaptive polling.

GetClipboardSequenceNumber returns a DWORD that increments each time
any process writes to the clipboard. This replaces fixed-time sleeps
after Ctrl+C with event-driven polling (~1us per check vs 50ms sleep).

Usage:
    seq = get_sequence_number()
    press_keys('ctrl', 'c')
    if wait_for_clipboard_write(seq, timeout_s=0.15):
        text = pyperclip.paste()  # Clipboard has been updated
"""
import ctypes
import time
import logging

logger = logging.getLogger(__name__)

_GetClipboardSequenceNumber = ctypes.windll.user32.GetClipboardSequenceNumber
_GetClipboardSequenceNumber.restype = ctypes.c_uint32
_GetClipboardSequenceNumber.argtypes = []


def get_sequence_number() -> int:
    """Return the current clipboard sequence number.

    Increments each time any process writes to the clipboard.
    Cost: ~1us.
    """
    return _GetClipboardSequenceNumber()


def wait_for_clipboard_write(
    initial_seq: int,
    timeout_s: float = 0.15,
    poll_interval_s: float = 0.002,
) -> bool:
    """Poll until clipboard sequence number changes from initial_seq.

    Args:
        initial_seq: Sequence number captured before the expected write.
        timeout_s: Maximum wait time in seconds (default 150ms).
        poll_interval_s: Sleep between polls (default 2ms).

    Returns:
        True if clipboard was written to, False on timeout.
    """
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if get_sequence_number() != initial_seq:
            return True
        time.sleep(poll_interval_s)
    return False
