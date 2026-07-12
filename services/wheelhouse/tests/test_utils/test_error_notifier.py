"""Tests for error_notifier.py - Error notification logging handler.

Tests cover:
- ErrorNotificationHandler rate limiting
- Message truncation for Windows notification limits
- Notification title formatting by log level
- Cleanup of old rate limit entries
- Error resilience (notification failure doesn't break logging)
"""

import logging
import time
from unittest.mock import Mock, patch, MagicMock

import pytest


class TestErrorNotificationHandler:
    """Tests for the ErrorNotificationHandler logging handler."""

    def _make_handler(self, rate_limit=10, level=logging.ERROR):
        from utils.error_notifier import ErrorNotificationHandler
        return ErrorNotificationHandler(rate_limit_seconds=rate_limit, level=level)

    def _make_record(self, name="test.module", level=logging.ERROR, msg="Test error message"):
        record = logging.LogRecord(
            name=name,
            level=level,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )
        return record

    # --- Rate limiting ---

    @patch("utils.error_notifier.notification")
    def test_first_error_sends_notification(self, mock_notif):
        handler = self._make_handler()
        record = self._make_record()

        handler.emit(record)

        mock_notif.notify.assert_called_once()

    @patch("utils.error_notifier.notification")
    def test_duplicate_within_rate_limit_suppressed(self, mock_notif):
        handler = self._make_handler(rate_limit=60)
        record = self._make_record()

        handler.emit(record)
        handler.emit(record)

        assert mock_notif.notify.call_count == 1

    @patch("utils.error_notifier.notification")
    def test_different_errors_not_rate_limited(self, mock_notif):
        handler = self._make_handler(rate_limit=60)
        record1 = self._make_record(msg="Error A")
        record2 = self._make_record(msg="Error B")

        handler.emit(record1)
        handler.emit(record2)

        assert mock_notif.notify.call_count == 2

    @patch("utils.error_notifier.notification")
    def test_same_error_after_rate_limit_sends_again(self, mock_notif):
        handler = self._make_handler(rate_limit=0)  # Effectively no limit
        record = self._make_record()

        handler.emit(record)
        handler.emit(record)

        assert mock_notif.notify.call_count == 2

    @patch("utils.error_notifier.notification")
    def test_rate_limit_key_includes_name_and_level(self, mock_notif):
        handler = self._make_handler(rate_limit=60)
        # Same message, different logger names
        record1 = self._make_record(name="module_a", msg="Error")
        record2 = self._make_record(name="module_b", msg="Error")

        handler.emit(record1)
        handler.emit(record2)

        assert mock_notif.notify.call_count == 2

    # --- Message truncation ---

    @patch("utils.error_notifier.notification")
    def test_short_message_not_truncated(self, mock_notif):
        handler = self._make_handler()
        record = self._make_record(msg="Short error")

        handler.emit(record)

        call_kwargs = mock_notif.notify.call_args[1]
        assert call_kwargs["message"] == "Short error"

    @patch("utils.error_notifier.notification")
    def test_long_message_truncated(self, mock_notif):
        handler = self._make_handler()
        long_msg = "x" * 300
        record = self._make_record(msg=long_msg)

        handler.emit(record)

        call_kwargs = mock_notif.notify.call_args[1]
        assert len(call_kwargs["message"]) < 300

    @patch("utils.error_notifier.notification")
    def test_long_message_with_delimiter_truncated_cleanly(self, mock_notif):
        handler = self._make_handler()
        # Message with a period delimiter early enough
        msg = "First sentence error occurred. " + "x" * 300
        record = self._make_record(msg=msg)

        handler.emit(record)

        call_kwargs = mock_notif.notify.call_args[1]
        assert "See log" in call_kwargs["message"]

    # --- Title formatting ---

    @patch("utils.error_notifier.notification")
    def test_error_level_title(self, mock_notif):
        handler = self._make_handler()
        record = self._make_record(name="speech.processor", level=logging.ERROR)

        handler.emit(record)

        call_kwargs = mock_notif.notify.call_args[1]
        assert "[ERROR]" in call_kwargs["title"]
        assert "processor" in call_kwargs["title"]

    @patch("utils.error_notifier.notification")
    def test_critical_level_title(self, mock_notif):
        handler = self._make_handler()
        record = self._make_record(name="app", level=logging.CRITICAL)

        handler.emit(record)

        call_kwargs = mock_notif.notify.call_args[1]
        assert "[CRITICAL]" in call_kwargs["title"]

    @patch("utils.error_notifier.notification")
    def test_root_logger_title(self, mock_notif):
        handler = self._make_handler()
        record = self._make_record(name="root")

        handler.emit(record)

        call_kwargs = mock_notif.notify.call_args[1]
        assert "WheelHouse" in call_kwargs["title"]

    # --- Cleanup ---

    @patch("utils.error_notifier.notification")
    def test_cleanup_removes_old_entries(self, mock_notif):
        handler = self._make_handler(rate_limit=1)
        # Manually add old entries
        handler._last_notification_times["old_key"] = time.time() - 100

        record = self._make_record()
        handler.emit(record)

        # Old entry should be cleaned up
        assert "old_key" not in handler._last_notification_times

    # --- Error resilience ---

    @patch("utils.error_notifier.notification")
    def test_notification_failure_doesnt_raise(self, mock_notif):
        handler = self._make_handler()
        mock_notif.notify.side_effect = RuntimeError("notification system broken")

        record = self._make_record()
        # Should not raise
        handler.emit(record)

    @patch("utils.error_notifier.notification")
    def test_notify_not_callable_handled(self, mock_notif):
        handler = self._make_handler()
        # Make notify not callable
        mock_notif.notify = "not a function"

        record = self._make_record()
        # Should not raise
        handler.emit(record)

    def test_handler_level_defaults_to_error(self):
        handler = self._make_handler()
        assert handler.level == logging.ERROR

    def test_handler_is_logging_handler(self):
        handler = self._make_handler()
        assert isinstance(handler, logging.Handler)
