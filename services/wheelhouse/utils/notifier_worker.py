"""Notifier worker thread for WheelHouse error notifications.

The notifier worker owns a small bounded queue and a daemon thread. The
ErrorNotificationHandler (running on the producer thread) builds a payload
and put_nowait's it onto this queue. The worker thread dequeues and calls
plyer.notification.notify, which can be slow on Windows (COM init, toast
service round-trip). Keeping that off the producer thread is the whole
point of this split.

The worker is independent from the QueueListener: a slow plyer call cannot
delay file/stream draining, and a slow file write cannot delay toasts.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)

DEFAULT_NOTIFIER_QUEUE_MAXSIZE = 64
# Poll interval for the worker's queue.get. Bounds shutdown latency on an
# empty queue (the worker only checks _stop_event when get() times out).
_QUEUE_POLL_INTERVAL_S = 1.0


def _safe_stderr_write(message: str) -> None:
    try:
        stream = sys.__stderr__
        if stream is None:
            return
        stream.write(message)
        stream.flush()
    except Exception:
        pass


@dataclass(frozen=True)
class NotifierPayload:
    """Immutable snapshot of a notification request.

    Built on the producer thread so the worker thread does not need to
    inspect the LogRecord (which may be mutated or recycled).
    """

    title: str
    message: str
    levelname: str
    trace_id: str


class NotifierWorker:
    """Background worker that delivers NotifierPayloads via plyer."""

    _SENTINEL = object()

    def __init__(self, maxsize: int = DEFAULT_NOTIFIER_QUEUE_MAXSIZE) -> None:
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=maxsize)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._dropped = 0
        self._delivered = 0

    @property
    def queue(self) -> "queue.Queue[object]":
        return self._queue

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def delivered(self) -> int:
        return self._delivered

    def submit(self, payload: NotifierPayload) -> bool:
        """Producer-thread submission. Returns True on enqueue, False on drop."""
        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            self._dropped += 1
            return False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="WheelHouseNotifierWorker",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Bounded stop. Does NOT clear self._thread if the worker is still alive.

        plyer.notification.notify can block on Windows COM init or toast
        service round-trips. If the deadline elapses while the worker
        is mid-delivery, we leave self._thread visible so the orphaned
        worker is observable through get_notifier_worker(). Clearing
        the reference unconditionally would let a follow-up
        setup_logging build a second worker while the first keeps
        consuming queue entries, which violates the idempotent-teardown
        contract.
        """
        self._stop_event.set()
        deadline = time.monotonic() + timeout
        try:
            self._queue.put_nowait(self._SENTINEL)
        except queue.Full:
            # Queue full of payloads + slow plyer call could leave the
            # worker stuck draining payloads before noticing stop_event.
            # Block briefly for room so the sentinel actually reaches
            # the worker thread.
            remaining = max(0.0, deadline - time.monotonic())
            try:
                self._queue.put(self._SENTINEL, timeout=remaining)
            except queue.Full:
                _safe_stderr_write(
                    "[wheelhouse-notifier] stop: queue full and sentinel "
                    "could not be enqueued within deadline; worker may "
                    "drain remaining payloads before exiting\n"
                )
        thread = self._thread
        if thread is None:
            return
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)
        if thread.is_alive():
            _safe_stderr_write(
                f"[wheelhouse-notifier] stop: worker thread did not exit "
                f"within {timeout:.1f}s; leaving thread reference visible "
                f"for diagnostics\n"
            )
            return
        self._thread = None

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=_QUEUE_POLL_INTERVAL_S)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            if item is self._SENTINEL:
                return
            if not isinstance(item, NotifierPayload):
                continue
            self._deliver(item)

    def _deliver(self, payload: NotifierPayload) -> None:
        try:
            # wh-notice-length-guard: send_notice measures both fields
            # first, and returns False when there is no backend -- which
            # is what keeps the delivered count honest.
            from utils.notice_text import send_notice

            if send_notice(
                payload.title,
                payload.message,
                app_name="Wheelhouse",
                timeout=10,
            ):
                self._delivered += 1
        except Exception as exc:
            _safe_stderr_write(
                f"[wheelhouse-notifier] plyer notify failed: {exc!r}\n"
            )
            # The stderr write above is first because it is the one of the
            # two that still works when the logging system has already been
            # torn down. The log record is what a packaged run can read
            # afterwards: a packaged run has no console, so stderr goes
            # nowhere, and wh-parakeet-crash-windows10 is the case that
            # needed it -- a notice was built, queued and refused, and the
            # log could not say whether the backend raised or the computer
            # simply showed nothing.
            #
            # WARNING, and the level is load bearing. ErrorNotificationHandler
            # takes level=logging.ERROR by default (utils/error_notifier.py)
            # and setup_logging constructs it without overriding that level
            # (utils/logging_setup.py), so an ERROR record here would build
            # another payload, submit it to THIS worker, and fail the same
            # way. A WARNING reaches the log file and never reaches that
            # handler.
            logger.warning(
                "Could not deliver a notice: %s", payload.message, exc_info=True
            )
