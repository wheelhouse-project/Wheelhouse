"""Queue-based logging primitives for WheelHouse.

Implements the producer/listener split described in wh-rus5u: the producer
thread enqueues records non-blockingly and a dedicated listener thread owns
the actual file and stream handlers. The producer is shielded from rotation
locks, AV-scan reopens, and other slow file I/O.

Components:
  - _DroppingQueueHandler: QueueHandler subclass that uses put_nowait and
    counts drops on overflow instead of blocking the producer.
  - WheelHouseQueueListener: QueueListener subclass that isolates per-handler
    exceptions, survives prepare() failures, and exposes drain counters used
    by the watchdog.
  - _ListenerWatchdog: daemon thread that detects a stalled listener and
    writes a warning to guarded stderr.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
import threading
import time
from typing import Optional

DEFAULT_LOG_QUEUE_MAXSIZE = 10_000
DROP_SUMMARY_INTERVAL = 100
WATCHDOG_CHECK_INTERVAL_S = 30.0
WATCHDOG_STALL_THRESHOLD_S = 30.0


def _safe_stderr_write(message: str) -> None:
    """Write to the original stderr without raising if it is closed."""
    try:
        stream = sys.__stderr__
        if stream is None:
            return
        stream.write(message)
        stream.flush()
    except Exception:
        pass


class _DroppingQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler with non-blocking enqueue and drop accounting.

    Calling put_nowait on a bounded queue returns immediately on overflow.
    The default QueueHandler.handleError would route to logging.lastResort
    which writes synchronously to stderr on the producer thread; we override
    handleError to a no-op and increment a drop counter instead.
    """

    def __init__(self, log_queue: "queue.Queue[logging.LogRecord]") -> None:
        super().__init__(log_queue)
        self.drop_count = 0
        self.enqueue_count = 0

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # Stdlib QueueHandler.prepare() formats the message and clears
        # args/exc_info on the producer thread (so the record can be
        # pickled across processes). The WheelHouse log queue is in-
        # process; pickling is unnecessary, and formatting the message
        # plus stringifying tracebacks on the dispatch thread is exactly
        # the kind of producer-thread work the design budget excludes.
        # Defer formatting to the listener-thread handlers.
        return record

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
            self.enqueue_count += 1
        except queue.Full:
            self.drop_count += 1

    def handleError(self, record: logging.LogRecord) -> None:
        # Suppress logging.lastResort dispatch; producer thread must not
        # do file/stderr I/O from inside a logging call.
        del record
        return


class WheelHouseQueueListener(logging.handlers.QueueListener):
    """QueueListener with per-handler exception isolation and drop reporting.

    Wraps prepare() and per-handler dispatch in try/except blocks so a
    misbehaving handler cannot kill the listener thread or starve other
    handlers. Tracks a drained-record counter that the watchdog reads.
    Periodically synthesises a drop-summary LogRecord and dispatches it
    inline through its owned handlers (NOT through the QueueHandler).
    """

    def __init__(
        self,
        log_queue: "queue.Queue[logging.LogRecord]",
        *handlers: logging.Handler,
        drop_handler_ref: "_DroppingQueueHandler | None" = None,
    ) -> None:
        super().__init__(log_queue, *handlers, respect_handler_level=False)
        self._handler_errors = 0
        self._drained_count = 0
        self._drop_handler_ref = drop_handler_ref
        self._last_drop_summary = 0

    @property
    def drained_count(self) -> int:
        return self._drained_count

    @property
    def handler_errors(self) -> int:
        return self._handler_errors

    @property
    def is_running(self) -> bool:
        """True while the monitor thread is alive (post start, pre stop)."""
        thread = getattr(self, "_thread", None)
        return thread is not None and thread.is_alive()

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        try:
            return super().prepare(record)
        except Exception as exc:
            _safe_stderr_write(
                f"[wheelhouse-queuelistener] prepare() failed for {record.name}: {exc!r}\n"
            )
            return record

    def handle(self, record: logging.LogRecord) -> None:
        prepared = self.prepare(record)
        for handler in self.handlers:
            try:
                if not self.respect_handler_level or prepared.levelno >= handler.level:
                    handler.handle(prepared)
            except Exception as exc:
                self._handler_errors += 1
                _safe_stderr_write(
                    f"[wheelhouse-queuelistener] handler "
                    f"{type(handler).__name__} raised: {exc!r}\n"
                )
        self._drained_count += 1
        self._maybe_emit_drop_summary()

    def _monitor(self) -> None:
        # Narrow copy of CPython logging.handlers.QueueListener._monitor.
        # Loop semantics verified against CPython 3.12 and 3.13 stdlib
        # source. test_monitor_loop_matches_stdlib in test_queued_logging.py
        # asserts sys.version_info[:2] is in {(3, 12), (3, 13)}; running on
        # any Python past 3.13 fails the test and forces a maintainer to
        # re-read the stdlib loop and bump the allow-list.
        try:
            q = self.queue
            has_task_done = hasattr(q, "task_done")
            while True:
                try:
                    record = self.dequeue(True)
                    if record is self._sentinel:  # type: ignore[attr-defined]
                        if has_task_done:
                            q.task_done()  # type: ignore[attr-defined]
                        break
                    self.handle(record)
                    if has_task_done:
                        q.task_done()  # type: ignore[attr-defined]
                except queue.Empty:
                    break
        except Exception as exc:
            _safe_stderr_write(
                f"[wheelhouse-queuelistener] _monitor died: {exc!r}\n"
            )

    def stop(self, timeout: float = 5.0) -> None:  # type: ignore[override]
        """Bounded stop: enqueue sentinel then join with a deadline.

        Stdlib QueueListener.stop() joins indefinitely, which lets a
        slow file rotation lock or AV-scan-blocked flush hang shutdown
        forever. This override:

        - Tries put_nowait of the sentinel; if the queue is full it
          falls back to a bounded blocking put so the sentinel always
          reaches the listener.
        - Joins the monitor thread with the supplied deadline.
        - On timeout, writes a guarded sys.__stderr__ warning and
          returns without altering thread state, so callers can still
          observe is_running and decide what to do.
        """
        thread = getattr(self, "_thread", None)
        if thread is None:
            return
        deadline = time.monotonic() + timeout
        try:
            try:
                sentinel = self._sentinel  # type: ignore[attr-defined]
                self.queue.put_nowait(sentinel)
            except queue.Full:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    self.queue.put(sentinel, timeout=remaining)  # type: ignore[attr-defined]
                except queue.Full:
                    _safe_stderr_write(
                        "[wheelhouse-queuelistener] stop: queue full and "
                        "sentinel could not be enqueued within deadline; "
                        "monitor thread may not exit cleanly\n"
                    )
        except Exception as exc:
            _safe_stderr_write(
                f"[wheelhouse-queuelistener] stop: sentinel enqueue raised: {exc!r}\n"
            )

        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)
        if thread.is_alive():
            _safe_stderr_write(
                f"[wheelhouse-queuelistener] stop: monitor thread did not exit "
                f"within {timeout:.1f}s; leaving as-is for diagnostics\n"
            )
            return
        # Clean exit: clear thread reference so the listener can be re-started.
        self._thread = None

    def _maybe_emit_drop_summary(self) -> None:
        ref = self._drop_handler_ref
        if ref is None:
            return
        drops = ref.drop_count
        if drops - self._last_drop_summary < DROP_SUMMARY_INTERVAL:
            return
        self._last_drop_summary = drops
        record = logging.LogRecord(
            name="utils.queue_logging",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg=(
                "Log queue dropped %d records since startup "
                "(current depth: %d)"
            ),
            args=(drops, self.queue.qsize()),  # type: ignore[attr-defined]
            exc_info=None,
        )
        record.trace_id = ""  # type: ignore[attr-defined]
        for handler in self.handlers:
            try:
                handler.handle(record)
            except Exception:
                _safe_stderr_write(
                    f"[wheelhouse-queuelistener] failed to dispatch drop summary "
                    f"({drops} drops)\n"
                )


class _ListenerWatchdog(threading.Thread):
    """Detects a stalled QueueListener and writes a warning to stderr.

    Polls the listener's drained_count every WATCHDOG_CHECK_INTERVAL_S
    seconds. If the queue is non-empty AND drained_count has not advanced
    for WATCHDOG_STALL_THRESHOLD_S seconds, writes a warning to guarded
    stderr. This is the only signal of a dead listener that does not itself
    depend on logging working.
    """

    def __init__(
        self,
        queue_handler: _DroppingQueueHandler,
        listener: WheelHouseQueueListener,
        check_interval: float = WATCHDOG_CHECK_INTERVAL_S,
        stall_threshold: float = WATCHDOG_STALL_THRESHOLD_S,
        stall_log_path: Optional[str] = None,
    ) -> None:
        super().__init__(name="WheelHouseListenerWatchdog", daemon=True)
        self._queue_handler = queue_handler
        self._listener = listener
        self._check_interval = check_interval
        self._stall_threshold = stall_threshold
        # wh-console-write-resilience: side-channel file for stall warnings.
        # The most likely cause of a stalled listener is a FROZEN CONSOLE
        # (conhost hang, mark mode, terminal host freeze) blocking the
        # stderr StreamHandler -- so a warning written to sys.__stderr__
        # wedges the watchdog on its very first report. A plain append to
        # this file shares no lock with the wedged listener or the rotating
        # file handler, so the watchdog stays alive and keeps reporting.
        # None (or a failed write) falls back to guarded stderr.
        self._stall_log_path = stall_log_path
        self._stop_event = threading.Event()
        self._last_drained = 0
        self._last_drained_time = time.monotonic()

    def _report_stall(self, message: str) -> None:
        """Write a stall warning without depending on stderr.

        Side-channel file first (plain append, no logging locks); guarded
        stderr only when no path is configured or the file write fails.
        """
        if self._stall_log_path:
            try:
                with open(
                    self._stall_log_path, "a", encoding="utf-8"
                ) as sink:
                    sink.write(message)
                return
            except OSError:
                pass
        _safe_stderr_write(message)

    def run(self) -> None:
        while not self._stop_event.wait(self._check_interval):
            try:
                depth = self._queue_handler.queue.qsize()  # type: ignore[attr-defined]
            except (NotImplementedError, OSError):
                depth = 0
            drained = self._listener.drained_count
            now = time.monotonic()
            if drained != self._last_drained:
                self._last_drained = drained
                self._last_drained_time = now
                continue
            if depth > 0 and (now - self._last_drained_time) >= self._stall_threshold:
                self._report_stall(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"[wheelhouse-watchdog] listener stalled: depth={depth}, "
                    f"no drain for {now - self._last_drained_time:.1f}s\n"
                )
                self._last_drained_time = now

    def stop(self, timeout: float = 1.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=timeout)


def make_log_queue(maxsize: int = DEFAULT_LOG_QUEUE_MAXSIZE) -> "queue.Queue[logging.LogRecord]":
    """Build the bounded log queue used by the producer/listener split."""
    return queue.Queue(maxsize=maxsize)
