"""Centralized logging configuration and setup utilities.

Each WheelHouse process (Logic, Input, GUI, in-process STT) calls
`setup_logging` early at startup. The producer thread enqueues records
through a `_DroppingQueueHandler`, and a `WheelHouseQueueListener` thread
owns the `ConcurrentRotatingFileHandler` and stderr `StreamHandler` so
file rotation locks and AV-scan reopens cannot block the dispatch path.

ERROR+ records also go through an `ErrorNotificationHandler` attached to
root. That handler builds a payload on the producer thread and submits
it to a separate `NotifierWorker`, which calls plyer.notification.notify
on its own thread.

Public API:
  - setup_logging(config): idempotent. Tears down a prior listener and
    notifier worker before installing fresh ones.
  - shutdown_logging(): drains the queue, joins the listener thread,
    closes downstream handlers, and stops the notifier worker. Registered
    with atexit. Each process should call it explicitly before exit.
"""
import atexit
import logging
import multiprocessing
import os
import sys
from typing import Optional

from concurrent_log_handler import ConcurrentRotatingFileHandler

from utils.error_notifier import ErrorNotificationHandler
from utils.notifier_worker import NotifierWorker
from utils.queue_logging import (
    WheelHouseQueueListener,
    _DroppingQueueHandler,
    _ListenerWatchdog,
    make_log_queue,
)
from utils.trace_context import TraceIdFilter

logger = logging.getLogger(__name__)

_LISTENER: Optional[WheelHouseQueueListener] = None
_WATCHDOG: Optional[_ListenerWatchdog] = None
_NOTIFIER_WORKER: Optional[NotifierWorker] = None
_QUEUE_HANDLER: Optional[_DroppingQueueHandler] = None
_FILE_HANDLER: Optional[ConcurrentRotatingFileHandler] = None
_ERROR_NOTIFICATION_HANDLER: Optional[ErrorNotificationHandler] = None
_ATEXIT_REGISTERED = False


def get_listener() -> Optional[WheelHouseQueueListener]:
    """Return the active QueueListener (test/debug introspection)."""
    return _LISTENER


def get_file_handler() -> Optional[ConcurrentRotatingFileHandler]:
    """Return the listener-owned file handler (test introspection)."""
    return _FILE_HANDLER


def get_notifier_worker() -> Optional[NotifierWorker]:
    """Return the active notifier worker (test/debug introspection)."""
    return _NOTIFIER_WORKER


def setup_logging(config) -> None:
    """Configure application-wide logging based on config dict.

    Replaces any prior listener/notifier worker so calling twice in the
    same process is safe. Records emitted before this returns hit
    Python's lastResort (stderr) -- there is no production code path
    that emits before setup_logging in a WheelHouse entry point.
    """
    global _LISTENER, _WATCHDOG, _NOTIFIER_WORKER
    global _QUEUE_HANDLER, _FILE_HANDLER, _ERROR_NOTIFICATION_HANDLER
    global _ATEXIT_REGISTERED

    log_level_str = _resolve_log_level(config)
    log_level = getattr(logging, log_level_str, logging.INFO)

    _suppress_third_party_libraries()

    # Tear down any prior setup so re-entering is safe (test fixtures,
    # fork+spawn flows, hot reload).
    _teardown_existing()

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    trace_filter = TraceIdFilter()
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(processName)s - "
        "%(filename)s:%(lineno)d - trace=%(trace_id)s - %(message)s"
    )

    # --- Build downstream handlers (owned by listener thread) ---
    # NB: TraceIdFilter is NOT attached to listener-side handlers. It must
    # run on the producer thread (where current_trace_id is meaningful).
    # Records synthesised inside the listener (e.g., drop-summary records)
    # set record.trace_id manually before dispatch so the formatter never
    # KeyErrors on a missing attribute.
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.NOTSET)
    stream_handler.setFormatter(formatter)

    file_handler: Optional[ConcurrentRotatingFileHandler] = None
    log_file_path: Optional[str] = None
    _file_handler_error: Optional[BaseException] = None
    try:
        project_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..")
        )
        log_file_path = os.path.join(project_root, "wheelhouse.log")
        rotate_on_startup = (
            os.path.exists(log_file_path) and os.path.getsize(log_file_path) > 0
        )
        file_handler = ConcurrentRotatingFileHandler(
            log_file_path,
            mode="a",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        if rotate_on_startup:
            # Synchronous rollover on the calling thread BEFORE handing
            # the handler to the listener so the new session's first
            # record writes to a known-fresh file.
            file_handler.doRollover()
        file_handler.setLevel(logging.NOTSET)
        file_handler.setFormatter(formatter)
    except Exception as exc:
        # File handler is best-effort; the listener can run without it.
        # Log a warning AFTER root handlers are wired so it shows up.
        file_handler = None
        _file_handler_error = exc

    # --- Build listener and notifier worker ---
    # wh-console-write-resilience: file handler FIRST. The listener writes
    # handlers in order; a frozen console (conhost hang, mark mode) blocks
    # the stderr StreamHandler's write forever, so with stderr first one
    # blocked console write starves the file handler and silences the log
    # file. File first, every record is durably on disk before the risky
    # console write.
    listener_handlers: list = []
    if file_handler is not None:
        listener_handlers.append(file_handler)
    listener_handlers.append(stream_handler)

    log_queue = make_log_queue()
    queue_handler = _DroppingQueueHandler(log_queue)
    queue_handler.setLevel(logging.NOTSET)
    queue_handler.addFilter(trace_filter)

    listener = WheelHouseQueueListener(
        log_queue, *listener_handlers, drop_handler_ref=queue_handler
    )

    notifier_worker = NotifierWorker()

    error_notification_handler: Optional[ErrorNotificationHandler]
    try:
        error_notification_handler = ErrorNotificationHandler(
            rate_limit_seconds=10, notifier_worker=notifier_worker
        )
        error_notification_handler.addFilter(trace_filter)
    except Exception:
        error_notification_handler = None

    # --- Start background threads BEFORE attaching handlers to root ---
    notifier_worker.start()
    listener.start()

    # wh-console-write-resilience: the watchdog's stall warning goes to a
    # side-channel file next to the main log, NOT stderr -- the most likely
    # stall cause is a frozen console, and a stderr warning would wedge the
    # watchdog on its first report. Falls back to guarded stderr when the
    # main log path is unavailable.
    stall_log_path = (
        os.path.join(
            os.path.dirname(log_file_path), "wheelhouse-watchdog.log"
        )
        if log_file_path is not None
        else None
    )
    watchdog = _ListenerWatchdog(
        queue_handler, listener, stall_log_path=stall_log_path
    )
    watchdog.start()

    # --- Attach root handlers ---
    root_logger.addHandler(queue_handler)
    if error_notification_handler is not None:
        root_logger.addHandler(error_notification_handler)

    _LISTENER = listener
    _WATCHDOG = watchdog
    _NOTIFIER_WORKER = notifier_worker
    _QUEUE_HANDLER = queue_handler
    _FILE_HANDLER = file_handler
    _ERROR_NOTIFICATION_HANDLER = error_notification_handler

    if not _ATEXIT_REGISTERED:
        atexit.register(shutdown_logging)
        _ATEXIT_REGISTERED = True

    # Emit setup-time messages now that the listener is draining.
    if file_handler is not None and log_file_path is not None:
        logger.info("Logging to file: %s", log_file_path)
    elif _file_handler_error is not None:
        logger.warning("Failed to create log file handler: %s", _file_handler_error)

    if error_notification_handler is not None:
        logger.info(
            "Error notification handler enabled "
            "(ERROR+ logs will trigger Windows notifications)"
        )
    else:
        logger.warning("Failed to add error notification handler")

    # Re-apply third-party suppression after handler swap; previously some
    # libraries imported between rounds and added their own handlers.
    _suppress_third_party_libraries()

    logger.info(
        "Logging configured. Level: %s, Process: %s",
        log_level_str,
        multiprocessing.current_process().name,
    )


def shutdown_logging(timeout: float = 5.0) -> None:
    """Flush and stop the listener and notifier worker.

    Safe to call multiple times. Each WheelHouse process should call this
    in its shutdown path; atexit registers it as a fallback. On hard
    kill / native crash records still in the queue are lost.
    """
    global _LISTENER, _WATCHDOG, _NOTIFIER_WORKER
    global _QUEUE_HANDLER, _FILE_HANDLER, _ERROR_NOTIFICATION_HANDLER

    listener = _LISTENER
    watchdog = _WATCHDOG
    notifier_worker = _NOTIFIER_WORKER
    file_handler = _FILE_HANDLER
    queue_handler = _QUEUE_HANDLER
    error_notification_handler = _ERROR_NOTIFICATION_HANDLER

    _LISTENER = None
    _WATCHDOG = None
    _NOTIFIER_WORKER = None
    _QUEUE_HANDLER = None
    _FILE_HANDLER = None
    _ERROR_NOTIFICATION_HANDLER = None

    # Detach root handlers FIRST so post-shutdown logger.* calls fall through
    # to logging.lastResort (synchronous stderr) rather than enqueueing into
    # a queue with no consumer or submitting to a stopped notifier worker.
    root_logger = logging.getLogger()
    if queue_handler is not None:
        try:
            root_logger.removeHandler(queue_handler)
        except Exception:
            pass
    if error_notification_handler is not None:
        try:
            root_logger.removeHandler(error_notification_handler)
        except Exception:
            pass

    if watchdog is not None:
        try:
            watchdog.stop(timeout=1.0)
        except Exception:
            pass

    if listener is not None:
        try:
            listener.stop(timeout=timeout)
        except Exception:
            pass
        for handler in listener.handlers:
            try:
                handler.close()
            except Exception:
                pass

    if file_handler is not None and (
        listener is None or file_handler not in listener.handlers
    ):
        try:
            file_handler.close()
        except Exception:
            pass

    # Close the detached root handlers too (queue handler holds a queue ref;
    # error notification handler holds a worker ref).
    if queue_handler is not None:
        try:
            queue_handler.close()
        except Exception:
            pass
    if error_notification_handler is not None:
        try:
            error_notification_handler.close()
        except Exception:
            pass

    if notifier_worker is not None:
        try:
            notifier_worker.stop(timeout=timeout)
        except Exception:
            pass


def _teardown_existing() -> None:
    """Stop any prior listener/notifier worker before fresh setup."""
    if (
        _LISTENER is None
        and _WATCHDOG is None
        and _NOTIFIER_WORKER is None
    ):
        return
    shutdown_logging(timeout=5.0)


def _resolve_log_level(config) -> str:
    """Read LOG_LEVEL from a dict or a config_service-like object."""
    if hasattr(config, "get") and callable(config.get):
        try:
            return str(config.get("LOG_LEVEL", "INFO")).upper()
        except TypeError:
            pass
    return "INFO"


def _suppress_third_party_libraries() -> None:
    """Quiet noisy third-party libraries to keep logs readable."""
    for name in (
        "websockets",
        "websockets.protocol",
        "websockets.connection",
        "soco",
        "urllib3",
        "comtypes",
        "asyncio",
        "services.wheelhouse.integrations.sonos_control",
    ):
        logging.getLogger(name).setLevel(logging.INFO)
