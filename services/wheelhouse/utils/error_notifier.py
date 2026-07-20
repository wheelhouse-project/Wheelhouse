"""Error notification logging handler for WheelHouse.

This module provides a custom logging handler that sends Windows toast
notifications for ERROR-level log messages. So critical errors are
visible to the user without monitoring log files.

Key Classes:
  - ErrorNotificationHandler: Logging handler that builds notification
    payloads on the producer thread and either submits them to a
    NotifierWorker (queued path) or delivers them inline via plyer
    (legacy path -- still used when no worker is wired in).

Producer-thread cost (queued path): rate-limit dict lookup + put_nowait
on a bounded notifier queue. No COM init, no toast service round-trip.
The actual plyer.notification.notify call runs on the NotifierWorker
thread.
"""

import logging
import time
from typing import Dict, Optional

from plyer import notification

from utils.notifier_worker import NotifierPayload, NotifierWorker


class ErrorNotificationHandler(logging.Handler):
    """
    Custom logging handler for ERROR+ Windows notifications.

    Implements rate limiting so multiple errors in rapid succession do
    not produce notification spam. Same error within the rate-limit
    window is dropped.

    When a NotifierWorker is wired in (via setup_logging), emit() builds
    a payload and submits it. When no worker is wired (e.g. unit tests
    that construct the handler directly), emit() delivers via plyer
    inline -- so the existing test surface keeps working.
    """

    def __init__(
        self,
        rate_limit_seconds: int = 10,
        level: int = logging.ERROR,
        notifier_worker: Optional[NotifierWorker] = None,
    ):
        """
        Initialise the error notification handler.

        Args:
            rate_limit_seconds: Minimum seconds between notifications
                for the same error key.
            level: Minimum log level to trigger notifications.
            notifier_worker: Optional worker to receive payloads. When
                None, emit() calls plyer inline (legacy behaviour).
        """
        super().__init__(level)
        self.rate_limit_seconds = rate_limit_seconds
        self._last_notification_times: Dict[str, float] = {}
        self._notifier_worker = notifier_worker

    def set_notifier_worker(self, worker: Optional[NotifierWorker]) -> None:
        """Wire a worker after construction (used by setup_logging)."""
        self._notifier_worker = worker

    def emit(self, record: logging.LogRecord):
        """
        Process a log record and dispatch a notification if appropriate.

        Args:
            record: The log record to process.
        """
        try:
            error_key = f"{record.name}:{record.levelname}:{record.getMessage()}"

            current_time = time.time()
            last_time = self._last_notification_times.get(error_key, 0)

            if current_time - last_time < self.rate_limit_seconds:
                return

            self._last_notification_times[error_key] = current_time

            self._cleanup_old_entries(current_time)

            if self._notifier_worker is not None:
                payload = self._build_payload(record)
                self._notifier_worker.submit(payload)
            else:
                self._send_notification(record)

        except Exception:
            # Notification failures must never break logging.
            self.handleError(record)

    def _build_payload(self, record: logging.LogRecord) -> NotifierPayload:
        """Build an immutable payload for the worker thread."""
        title = self._format_title(record)
        message = self._format_message(record)
        trace_id = getattr(record, "trace_id", "")
        return NotifierPayload(
            title=title,
            message=message,
            levelname=record.levelname,
            trace_id=trace_id,
        )

    def _format_title(self, record: logging.LogRecord) -> str:
        level_indicators = {
            "ERROR": "[ERROR]",
            "CRITICAL": "[CRITICAL]",
        }
        indicator = level_indicators.get(record.levelname, "[WARN]")
        source = (
            record.name.split(".")[-1]
            if record.name and record.name != "root"
            else ""
        )
        return f"{indicator} {source}" if source else f"{indicator} Wheelhouse"

    def _format_message(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        max_length = 180

        if len(message) <= max_length:
            return message

        for delimiter in [". ", ": ", " - "]:
            if delimiter in message[:max_length]:
                parts = message.split(delimiter, 1)
                if len(parts[0]) < max_length - 20:
                    return parts[0] + delimiter.rstrip() + "\n(See log for details)"
        return message[: max_length - 17] + "...\n(See log)"

    def _send_notification(self, record: logging.LogRecord):
        """Inline plyer call (legacy/unwired path)."""
        try:
            title = self._format_title(record)
            message = self._format_message(record)
            if hasattr(notification, "notify") and callable(notification.notify):
                notification.notify(
                    title=title,
                    message=message,
                    app_name="Wheelhouse",
                    timeout=10,
                )
        except Exception:
            # Silently fail -- notification is best-effort.
            pass

    def _cleanup_old_entries(self, current_time: float):
        """Remove rate-limit cache entries older than 2x the rate window."""
        cutoff_time = current_time - (self.rate_limit_seconds * 2)
        keys_to_remove = [
            key
            for key, timestamp in self._last_notification_times.items()
            if timestamp < cutoff_time
        ]
        for key in keys_to_remove:
            del self._last_notification_times[key]
