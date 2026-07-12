"""Tests for ClipboardPoller abstraction.

ClipboardPoller consolidates duplicated clipboard polling logic from ui_action_handler.py:
- transform_selection (lines 368-376): Poll for clipboard change after Ctrl+C
- wrap_or_insert (lines 476-486): Poll for selection copy result
"""
import pytest
from unittest.mock import MagicMock, patch
import time


class TestClipboardPoller:
    """Tests for the ClipboardPoller class."""

    # ========================================================================
    # Constructor tests
    # ========================================================================

    def test_default_timeout(self):
        """Default timeout should be 100ms."""
        from ui.clipboard_poller import ClipboardPoller
        poller = ClipboardPoller()
        assert poller.timeout == 0.1  # 100ms in seconds

    def test_default_poll_interval(self):
        """Default poll interval should be 5ms."""
        from ui.clipboard_poller import ClipboardPoller
        poller = ClipboardPoller()
        assert poller.poll_interval == 0.005  # 5ms in seconds

    def test_custom_timeout(self):
        """Custom timeout should be applied."""
        from ui.clipboard_poller import ClipboardPoller
        poller = ClipboardPoller(timeout_ms=200)
        assert poller.timeout == 0.2  # 200ms in seconds

    def test_custom_poll_interval(self):
        """Custom poll interval should be applied."""
        from ui.clipboard_poller import ClipboardPoller
        poller = ClipboardPoller(poll_interval_ms=10)
        assert poller.poll_interval == 0.01  # 10ms in seconds

    # ========================================================================
    # wait_for_change tests - successful change detection
    # ========================================================================

    @patch("ui.clipboard_poller.pyperclip")
    def test_wait_for_change_returns_new_value_immediately(self, mock_pyperclip):
        """Should return new value when clipboard changes immediately."""
        from ui.clipboard_poller import ClipboardPoller

        mock_pyperclip.paste.return_value = "new content"
        poller = ClipboardPoller(timeout_ms=100)

        result = poller.wait_for_change("original content")
        assert result == "new content"

    @patch("ui.clipboard_poller.pyperclip")
    @patch("ui.clipboard_poller.time")
    def test_wait_for_change_polls_until_change(self, mock_time, mock_pyperclip):
        """Should poll until clipboard changes."""
        from ui.clipboard_poller import ClipboardPoller

        # Simulate time passing
        mock_time.time.side_effect = [0, 0.01, 0.02, 0.03]
        mock_time.sleep = MagicMock()

        # First 2 calls return original, then return new value
        mock_pyperclip.paste.side_effect = ["original", "original", "changed"]

        poller = ClipboardPoller(timeout_ms=100, poll_interval_ms=10)
        result = poller.wait_for_change("original")

        assert result == "changed"
        assert mock_pyperclip.paste.call_count == 3

    # ========================================================================
    # wait_for_change tests - timeout behavior
    # ========================================================================

    @patch("ui.clipboard_poller.pyperclip")
    @patch("ui.clipboard_poller.time")
    def test_wait_for_change_returns_none_on_timeout(self, mock_time, mock_pyperclip):
        """Should return None when timeout expires without change."""
        from ui.clipboard_poller import ClipboardPoller

        # Simulate time passing beyond timeout
        mock_time.time.side_effect = [0, 0.05, 0.15]  # 0ms, 50ms, 150ms (past 100ms timeout)
        mock_time.sleep = MagicMock()

        # Clipboard never changes
        mock_pyperclip.paste.return_value = "original"

        poller = ClipboardPoller(timeout_ms=100)
        result = poller.wait_for_change("original")

        assert result is None

    @patch("ui.clipboard_poller.pyperclip")
    def test_wait_for_change_timeout_zero_returns_immediately(self, mock_pyperclip):
        """Zero timeout should check once and return."""
        from ui.clipboard_poller import ClipboardPoller

        mock_pyperclip.paste.return_value = "same"
        poller = ClipboardPoller(timeout_ms=0)

        result = poller.wait_for_change("same")
        assert result is None

    # ========================================================================
    # wait_for_change tests - edge cases
    # ========================================================================

    @patch("ui.clipboard_poller.pyperclip")
    def test_wait_for_change_empty_string_change(self, mock_pyperclip):
        """Should detect change to empty string."""
        from ui.clipboard_poller import ClipboardPoller

        mock_pyperclip.paste.return_value = ""
        poller = ClipboardPoller(timeout_ms=100)

        result = poller.wait_for_change("some content")
        assert result == ""

    @patch("ui.clipboard_poller.pyperclip")
    def test_wait_for_change_from_empty_string(self, mock_pyperclip):
        """Should detect change from empty string."""
        from ui.clipboard_poller import ClipboardPoller

        mock_pyperclip.paste.return_value = "new content"
        poller = ClipboardPoller(timeout_ms=100)

        result = poller.wait_for_change("")
        assert result == "new content"

    @patch("ui.clipboard_poller.pyperclip")
    def test_wait_for_change_whitespace_difference(self, mock_pyperclip):
        """Should detect whitespace-only differences."""
        from ui.clipboard_poller import ClipboardPoller

        mock_pyperclip.paste.return_value = "content "  # trailing space
        poller = ClipboardPoller(timeout_ms=100)

        result = poller.wait_for_change("content")
        assert result == "content "

    # ========================================================================
    # wait_for_sentinel_change tests
    # ========================================================================

    @patch("ui.clipboard_poller.pyperclip")
    def test_wait_for_sentinel_change_returns_new_content(self, mock_pyperclip):
        """Should return new content when sentinel changes."""
        from ui.clipboard_poller import ClipboardPoller

        mock_pyperclip.paste.return_value = "selected text"
        poller = ClipboardPoller(timeout_ms=100)

        result = poller.wait_for_sentinel_change("__SENTINEL__123")
        assert result == "selected text"

    @patch("ui.clipboard_poller.pyperclip")
    @patch("ui.clipboard_poller.time")
    def test_wait_for_sentinel_change_returns_none_on_timeout(self, mock_time, mock_pyperclip):
        """Should return None when sentinel never changes."""
        from ui.clipboard_poller import ClipboardPoller

        mock_time.time.side_effect = [0, 0.15]  # Start, past timeout
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "__SENTINEL__123"

        poller = ClipboardPoller(timeout_ms=100)
        result = poller.wait_for_sentinel_change("__SENTINEL__123")

        assert result is None

    # ========================================================================
    # create_sentinel tests
    # ========================================================================

    def test_create_sentinel_contains_prefix(self):
        """Sentinel should contain identifying prefix."""
        from ui.clipboard_poller import ClipboardPoller

        sentinel = ClipboardPoller.create_sentinel()
        assert sentinel.startswith("__SENTINEL__")

    def test_create_sentinel_unique(self):
        """Each sentinel should be unique."""
        from ui.clipboard_poller import ClipboardPoller

        sentinel1 = ClipboardPoller.create_sentinel()
        sentinel2 = ClipboardPoller.create_sentinel()
        assert sentinel1 != sentinel2

    # ========================================================================
    # set_sentinel tests
    # ========================================================================

    @patch("ui.clipboard_poller.pyperclip")
    def test_set_sentinel_copies_to_clipboard(self, mock_pyperclip):
        """Should copy sentinel value to clipboard."""
        from ui.clipboard_poller import ClipboardPoller

        poller = ClipboardPoller()
        sentinel = poller.set_sentinel()

        mock_pyperclip.copy.assert_called_once_with(sentinel)
        assert sentinel.startswith("__SENTINEL__")

    @patch("ui.clipboard_poller.pyperclip")
    def test_set_sentinel_returns_sentinel_value(self, mock_pyperclip):
        """Should return the sentinel value."""
        from ui.clipboard_poller import ClipboardPoller

        poller = ClipboardPoller()
        sentinel = poller.set_sentinel()

        assert isinstance(sentinel, str)
        assert len(sentinel) > 12  # At least prefix length
