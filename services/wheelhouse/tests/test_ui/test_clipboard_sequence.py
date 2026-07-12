"""Tests for clipboard sequence number polling.

Covers:
- get_sequence_number() - Win32 API wrapper
- wait_for_clipboard_write() - Adaptive polling with timeout
- Edge cases: immediate change, no change, multiple polls
"""
import pytest
from unittest.mock import patch, MagicMock

_MOD = "ui.clipboard_sequence"


class TestGetSequenceNumber:
    """get_sequence_number() wraps Win32 GetClipboardSequenceNumber."""

    @patch(f"{_MOD}._GetClipboardSequenceNumber", return_value=42)
    def test_returns_current_sequence(self, mock_api):
        """Should return the value from Win32 API."""
        from ui.clipboard_sequence import get_sequence_number
        assert get_sequence_number() == 42
        mock_api.assert_called_once()

    @patch(f"{_MOD}._GetClipboardSequenceNumber", return_value=0)
    def test_returns_zero_for_fresh_session(self, mock_api):
        """Sequence can be zero at session start."""
        from ui.clipboard_sequence import get_sequence_number
        assert get_sequence_number() == 0


class TestWaitForClipboardWrite:
    """wait_for_clipboard_write() polls until sequence changes or times out."""

    @patch(f"{_MOD}.get_sequence_number")
    def test_returns_true_when_sequence_changes(self, mock_seq):
        """Should detect clipboard write when sequence number increments."""
        mock_seq.side_effect = [10, 10, 11]
        from ui.clipboard_sequence import wait_for_clipboard_write
        assert wait_for_clipboard_write(10, timeout_s=1.0, poll_interval_s=0.001) is True

    @patch(f"{_MOD}.get_sequence_number", return_value=10)
    def test_returns_false_on_timeout(self, mock_seq):
        """Should return False when timeout expires without change."""
        from ui.clipboard_sequence import wait_for_clipboard_write
        assert wait_for_clipboard_write(10, timeout_s=0.01, poll_interval_s=0.002) is False

    @patch(f"{_MOD}.get_sequence_number", return_value=11)
    def test_returns_true_immediately_if_already_changed(self, mock_seq):
        """Should succeed on first poll when sequence already differs."""
        from ui.clipboard_sequence import wait_for_clipboard_write
        assert wait_for_clipboard_write(10, timeout_s=0.01, poll_interval_s=0.001) is True

    @patch(f"{_MOD}.get_sequence_number")
    def test_polls_multiple_times_before_detecting_change(self, mock_seq):
        """Should poll repeatedly until change detected."""
        mock_seq.side_effect = [5, 5, 5, 5, 6]
        from ui.clipboard_sequence import wait_for_clipboard_write
        assert wait_for_clipboard_write(5, timeout_s=1.0, poll_interval_s=0.001) is True
        assert mock_seq.call_count == 5
