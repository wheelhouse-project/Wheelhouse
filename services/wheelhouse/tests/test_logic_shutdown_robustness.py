"""Shutdown-robustness tests for LogicController (wh-log-crash-fixes).

Two defects found by the 2026-07-10 wheelhouse.log investigation:

* wh-handler-shutdown-policy: ``async_exception_handler`` called
  ``request_shutdown()`` for ANY non-CancelledError context, including
  the "exception was never retrieved" reports asyncio produces from a
  future's finalizer at garbage collection. Those reports describe an
  already-completed background probe or task that nothing awaits --
  often long after the fact -- and must be logged, not treated as
  fatal. On 2026-07-10 a transient console-probe timeout leaked
  through a fire-and-forget pre-warm future and the unconditional
  shutdown ended an 18-hour session while the user was idle.

* wh-logic-exit-hang: ``_listen_for_gui_commands`` parked a
  default-executor worker thread in an unbounded
  ``commands_from_gui_queue.get()``. Once the GUI process (the only
  producer) exits during shutdown, nothing can unblock that thread;
  ``asyncio.run()``'s teardown then hangs joining the default
  executor, and the launcher hard-terminates the Logic process after
  its 5-second grace period (observed 15:12:00 -> 15:12:05 in the
  same log).

These tests build a ``LogicController`` via ``object.__new__`` to skip
the heavy ``__init__`` (the wh-n29v test precedent) and inject only
the attributes each method touches.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from unittest.mock import Mock

import pytest

from services.wheelhouse.main import LogicController


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bare_controller() -> LogicController:
    controller = object.__new__(LogicController)
    controller.request_shutdown = Mock()  # type: ignore[method-assign]
    return controller


# ---------------------------------------------------------------------------
# async_exception_handler shutdown policy (wh-handler-shutdown-policy)
# ---------------------------------------------------------------------------


def test_never_retrieved_future_report_does_not_shut_down():
    """A GC-time 'Future exception was never retrieved' report is logged
    but must NOT shut the app down."""
    controller = _bare_controller()
    context = {
        "message": "Future exception was never retrieved",
        "exception": RuntimeError("helper read timed out"),
        "future": Mock(),
    }

    controller.async_exception_handler(Mock(), context)

    controller.request_shutdown.assert_not_called()


def test_never_retrieved_task_report_still_shuts_down():
    """wh-log-crash-fixes.3.1: a GC-time 'Task exception was never
    retrieved' report means a raw asyncio.create_task died with nobody
    awaiting it -- its work is gone (a dead STT loop, a dead state
    broadcast) and the app is silently degraded. That stays fatal so
    the launcher restores full service; only the executor-Future
    variant (the fire-and-forget probe path) is non-fatal."""
    controller = _bare_controller()
    context = {
        "message": "Task exception was never retrieved",
        "exception": RuntimeError("background task failed"),
        "future": Mock(spec=asyncio.Task),
    }

    controller.async_exception_handler(Mock(), context)

    controller.request_shutdown.assert_called_once()


def test_live_exception_report_still_shuts_down():
    """A live (non-GC) handler report keeps the shutdown-by-design
    behavior for real unhandled failures."""
    controller = _bare_controller()
    context = {
        "message": "Exception in callback something()",
        "exception": RuntimeError("real failure"),
    }

    controller.async_exception_handler(Mock(), context)

    controller.request_shutdown.assert_called_once()


def test_message_less_live_report_still_shuts_down():
    """wh-log-crash-fixes.1.3: asyncio always sets ``message``, but
    third-party ``loop.call_exception_handler`` callers are not forced
    to. The handler must not raise KeyError on a message-less context
    -- that would send the report to asyncio's default handler as
    'Unhandled error in exception handler' and skip the shutdown a
    live failure requires."""
    controller = _bare_controller()
    context = {"exception": RuntimeError("real failure, no message key")}

    controller.async_exception_handler(Mock(), context)

    controller.request_shutdown.assert_called_once()


def test_cancelled_error_report_does_not_shut_down():
    """CancelledError reports were already non-fatal; keep it that way."""
    controller = _bare_controller()
    context = {
        "message": "Exception in callback",
        "exception": asyncio.CancelledError(),
    }

    controller.async_exception_handler(Mock(), context)

    controller.request_shutdown.assert_not_called()


# ---------------------------------------------------------------------------
# GUI command listener exit latency (wh-logic-exit-hang)
# ---------------------------------------------------------------------------


class _RecordingQueue:
    """queue.Queue wrapper that records when a get() call RETURNS.

    The defect is not the coroutine (cancellation always unblocked it,
    and the 2026-07-10 log shows 'GUI command listener shutting down.'
    during the failed shutdown). The defect is the executor WORKER
    THREAD: an unbounded ``get()`` keeps it parked after the GUI (the
    sole producer) exits, and ``asyncio.run()``'s teardown then hangs
    joining the default executor. This wrapper makes the thread's
    release observable.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self.get_returned = threading.Event()

    def get(self, *args, **kwargs):
        try:
            return self._queue.get(*args, **kwargs)
        finally:
            self.get_returned.set()

    def put(self, item) -> None:
        self._queue.put(item)


@pytest.mark.asyncio
async def test_gui_listener_worker_thread_releases_queue_after_shutdown():
    """After shutdown with an idle queue, the executor thread running
    the queue read must return within a bounded time (the launcher's
    grace period is 5 s), not stay parked until queue traffic that will
    never come."""
    controller = object.__new__(LogicController)
    controller.shutdown_event = threading.Event()
    commands = _RecordingQueue()

    task = asyncio.create_task(
        LogicController._listen_for_gui_commands(controller, commands)
    )
    try:
        # Let the listener start and park its worker on the empty queue.
        await asyncio.sleep(0.2)
        controller.shutdown_event.set()

        # The worker thread's get() must return on its own (bounded
        # poll timeout), without cancellation and without traffic.
        deadline = asyncio.get_running_loop().time() + 3.0
        while (
            not commands.get_returned.is_set()
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.05)

        assert commands.get_returned.is_set(), (
            "the queue read must use a bounded timeout so the executor "
            "thread can observe shutdown; an unbounded get() parks the "
            "thread forever once the GUI producer is gone and stalls "
            "asyncio.run() teardown past the launcher's 5s grace period"
        )
        # And the listener coroutine itself finishes without needing
        # to be cancelled.
        await asyncio.wait_for(task, timeout=3.0)
    finally:
        # On failure, unpark the leaked worker thread so it does not
        # outlive the test.
        commands.put({"action": "noop"})
        if not task.done():
            task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
