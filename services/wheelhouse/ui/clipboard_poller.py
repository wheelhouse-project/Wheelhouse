"""Clipboard polling abstraction for UI operations.

Consolidates duplicated clipboard polling logic from ui_action_handler.py:
- transform_selection (lines 368-376): Poll for clipboard change after Ctrl+C
- wrap_or_insert (lines 476-486): Poll for selection copy result

This class provides a unified, testable interface for:
- Setting sentinel values to detect clipboard changes
- Polling clipboard until it changes or times out
- Handling timeout edge cases
"""
import time
import logging
from typing import Optional

import pyperclip

logger = logging.getLogger(__name__)

# Module-level counter for unique sentinel generation
_sentinel_counter = 0


class ClipboardPoller:
    """Polls clipboard for changes with configurable timeout.

    Used to detect when an application has copied content to the clipboard
    after a Ctrl+C command, by setting a sentinel value first and waiting
    for it to change.
    """

    def __init__(self, timeout_ms: float = 100, poll_interval_ms: float = 5):
        """Initialize clipboard poller.

        Args:
            timeout_ms: Maximum time to wait for clipboard change (milliseconds)
            poll_interval_ms: Time between clipboard checks (milliseconds)
        """
        self.timeout = timeout_ms / 1000.0
        self.poll_interval = poll_interval_ms / 1000.0

    @staticmethod
    def create_sentinel() -> str:
        """Create a unique sentinel value for clipboard change detection.

        Returns:
            A unique string that's unlikely to match real clipboard content
        """
        global _sentinel_counter
        _sentinel_counter += 1
        return f"__SENTINEL__{time.time()}_{_sentinel_counter}"

    def set_sentinel(self) -> str:
        """Set a sentinel value on the clipboard.

        Returns:
            The sentinel value that was set
        """
        sentinel = self.create_sentinel()
        pyperclip.copy(sentinel)
        return sentinel

    def wait_for_change(self, original_value: str) -> Optional[str]:
        """Poll clipboard until it changes from original_value.

        Args:
            original_value: The value to wait for clipboard to differ from

        Returns:
            The new clipboard content if it changed, None on timeout
        """
        start = time.time()

        while True:
            current = pyperclip.paste()
            if current != original_value:
                return current

            elapsed = time.time() - start
            if elapsed >= self.timeout:
                return None

            time.sleep(self.poll_interval)

    def wait_for_sentinel_change(self, sentinel: str) -> Optional[str]:
        """Wait for clipboard to change from a sentinel value.

        This is a convenience wrapper around wait_for_change for the
        common pattern of setting a sentinel and waiting for it to change.

        Args:
            sentinel: The sentinel value previously set on clipboard

        Returns:
            The new clipboard content if it changed, None on timeout
        """
        return self.wait_for_change(sentinel)
