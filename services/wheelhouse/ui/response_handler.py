"""Response queue handling abstraction.

Emits the single UI action response schema used by the app.py demuxer:
``{request_id, status|error, action, path|message, ...}`` (wh-lla5d).

Every UI action request produces exactly one response through this handler
so the demuxer always resolves the matching Future on the first message and
never logs "unknown/timed-out response" warnings.
"""
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ResponseHandler:
    """Emits Schema A responses for UI actions."""

    # Path constants for success responses
    PATH_CLIPBOARD_DONE = "clipboard_done"
    PATH_HEURISTIC_DONE = "heuristic_done"
    PATH_FOREGROUND_DONE = "foreground_done"
    # wh-d43oi: intelligent_insert_text emits this path when the strategy
    # returned True (paste verified or accepted optimistic fallback).
    PATH_INSERT_VERIFIED = "insert_verified"
    # wh-zndq / wh-fc1x: intelligent_insert_text emits this path when the
    # router-level text-target predicate refused the dictation
    # (no text-input control focused). The Future resolves cleanly so
    # the caller does not see a "strategy returned False" traceback;
    # downstream paths can branch on the path string if they need to
    # distinguish a successful delivery from a silent no-op.
    PATH_INSERT_REJECTED = "insert_rejected"

    def __init__(self, queue: Any):
        """Initialize response handler.

        Args:
            queue: The multiprocessing queue for sending responses
        """
        self.queue = queue

    def send_success(
        self,
        request_id: Optional[str],
        action: str,
        path: str,
        **extra: Any
    ) -> None:
        """Send a success response.

        Args:
            request_id: Request ID for correlation (None = no-op)
            action: The action that completed
            path: The completion path (e.g., "clipboard_done", "heuristic_done")
            **extra: Additional fields to include in the response
        """
        if request_id is None:
            return

        msg = {
            "request_id": request_id,
            "status": "ok",
            "path": path,
            "action": action,
        }
        msg.update(extra)
        self.queue.put(msg)

    def send_error(
        self,
        request_id: Optional[str],
        action: str,
        message: str
    ) -> None:
        """Send an error response.

        Args:
            request_id: Request ID for correlation (None = no-op)
            action: The action that failed
            message: Error description
        """
        if request_id is None:
            return

        self.queue.put({
            "request_id": request_id,
            "error": True,
            "action": action,
            "message": message,
        })
