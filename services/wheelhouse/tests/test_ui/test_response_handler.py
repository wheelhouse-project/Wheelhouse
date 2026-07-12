"""Tests for ResponseHandler abstraction.

ResponseHandler emits the single UI action response schema consumed by the
app.py demuxer (wh-lla5d). Schema B ack/response messages were removed
because they caused two responses per request.
"""
import pytest
from unittest.mock import MagicMock


class TestResponseHandler:
    """Tests for the ResponseHandler class."""

    # ========================================================================
    # Constructor tests
    # ========================================================================

    def test_constructor_stores_queue(self):
        """Constructor should store the queue."""
        from ui.response_handler import ResponseHandler

        mock_queue = MagicMock()
        handler = ResponseHandler(mock_queue)
        assert handler.queue is mock_queue

    # ========================================================================
    # send_success tests (Schema A format)
    # ========================================================================

    def test_send_success_enqueues_correct_format(self):
        """send_success should enqueue correct format."""
        from ui.response_handler import ResponseHandler

        mock_queue = MagicMock()
        handler = ResponseHandler(mock_queue)

        handler.send_success("req-123", "hotkey_action", "heuristic_done")

        mock_queue.put.assert_called_once()
        msg = mock_queue.put.call_args[0][0]
        assert msg["request_id"] == "req-123"
        assert msg["status"] == "ok"
        assert msg["action"] == "hotkey_action"
        assert msg["path"] == "heuristic_done"

    def test_send_success_with_extra_data(self):
        """send_success should include extra keyword arguments."""
        from ui.response_handler import ResponseHandler

        mock_queue = MagicMock()
        handler = ResponseHandler(mock_queue)

        handler.send_success("req-123", "hotkey_action", "done", extra_field="value")

        msg = mock_queue.put.call_args[0][0]
        assert msg["extra_field"] == "value"

    def test_send_success_does_nothing_without_request_id(self):
        """send_success should not enqueue if request_id is None."""
        from ui.response_handler import ResponseHandler

        mock_queue = MagicMock()
        handler = ResponseHandler(mock_queue)

        handler.send_success(None, "hotkey_action", "done")

        mock_queue.put.assert_not_called()

    # ========================================================================
    # send_error tests (Schema A format)
    # ========================================================================

    def test_send_error_enqueues_correct_format(self):
        """send_error should enqueue error format."""
        from ui.response_handler import ResponseHandler

        mock_queue = MagicMock()
        handler = ResponseHandler(mock_queue)

        handler.send_error("req-456", "press_key", "Window not found")

        mock_queue.put.assert_called_once()
        msg = mock_queue.put.call_args[0][0]
        assert msg["request_id"] == "req-456"
        assert msg["error"] is True
        assert msg["action"] == "press_key"
        assert msg["message"] == "Window not found"

    def test_send_error_does_nothing_without_request_id(self):
        """send_error should not enqueue if request_id is None."""
        from ui.response_handler import ResponseHandler

        mock_queue = MagicMock()
        handler = ResponseHandler(mock_queue)

        handler.send_error(None, "press_key", "Error message")

        mock_queue.put.assert_not_called()

    # ========================================================================
    # Path constants tests
    # ========================================================================

    def test_path_constants_defined(self):
        """Common path constants should be defined."""
        from ui.response_handler import ResponseHandler

        assert ResponseHandler.PATH_CLIPBOARD_DONE == "clipboard_done"
        assert ResponseHandler.PATH_HEURISTIC_DONE == "heuristic_done"
        assert ResponseHandler.PATH_FOREGROUND_DONE == "foreground_done"

    # ========================================================================
    # Edge cases
    # ========================================================================

    def test_send_success_empty_string_request_id(self):
        """Empty string request_id should still be treated as valid."""
        from ui.response_handler import ResponseHandler

        mock_queue = MagicMock()
        handler = ResponseHandler(mock_queue)

        # Empty string is technically a value, so we allow it
        handler.send_success("", "action", "path")

        # This is a design decision - empty string could be treated as falsy
        # For now, we treat empty string as a valid (if unusual) request_id
        mock_queue.put.assert_called_once()

    def test_queue_error_not_swallowed(self):
        """Queue errors should propagate."""
        from ui.response_handler import ResponseHandler

        mock_queue = MagicMock()
        mock_queue.put.side_effect = RuntimeError("Queue full")
        handler = ResponseHandler(mock_queue)

        with pytest.raises(RuntimeError, match="Queue full"):
            handler.send_success("req-123", "action", "path")
