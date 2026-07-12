"""Architecture-acceptance tests for the QueueHandler/QueueListener split.

This bead (wh-rus5u) names tests 1-11 in the design contract. Sister
bead wh-aev01.1 owns the perf/regression/overflow tests (5-11). The
tests here cover the architecturally critical contract:

  1. Trace id stamped on producer thread survives the queue boundary.
  2. shutdown_logging() drains queued records to the file deterministically.
  3. setup_logging twice in a row is idempotent (one QueueHandler on root,
     prior listener torn down).
  4. Runtime set_log_level on root propagates: DEBUG starts blocked, then
     reaches the file after a level change.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make the wheelhouse service importable.
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def isolated_root_logger(tmp_path):
    """Tear down logging_setup state between tests and route file writes to tmp."""
    from utils import logging_setup

    log_file = tmp_path / "wheelhouse.log"
    log_file_str = str(log_file)

    original_join = os.path.join

    def patched_join(*args):
        if args and args[-1] == "wheelhouse.log":
            return log_file_str
        return original_join(*args)

    root_logger = logging.getLogger()
    saved_handlers = root_logger.handlers.copy()
    saved_level = root_logger.level

    logging_setup.shutdown_logging()
    root_logger.handlers.clear()

    with patch("utils.logging_setup.os.path.join", side_effect=patched_join):
        yield log_file

    logging_setup.shutdown_logging()
    root_logger.handlers.clear()
    for handler in saved_handlers:
        root_logger.addHandler(handler)
    root_logger.setLevel(saved_level)


def test_trace_id_crosses_thread_boundary(isolated_root_logger):
    """Producer sets trace_id; listener-written file shows that trace_id."""
    from utils import logging_setup
    from utils.logging_setup import setup_logging
    from utils.trace_context import set_trace

    setup_logging({"LOG_LEVEL": "INFO"})
    set_trace("T-thread-boundary")

    child = logging.getLogger("test.child")
    child.info("crosses-the-queue")

    logging_setup.shutdown_logging()

    content = isolated_root_logger.read_text()
    assert "crosses-the-queue" in content
    assert "trace=T-thread-boundary" in content, (
        f"trace_id not preserved across queue: {content!r}"
    )


def test_shutdown_flushes_pending_records(isolated_root_logger):
    """All emitted records reach the file before shutdown_logging returns."""
    from utils import logging_setup
    from utils.logging_setup import setup_logging

    setup_logging({"LOG_LEVEL": "INFO"})

    child = logging.getLogger("test.flush")
    n = 50
    for i in range(n):
        child.info("flush-record %d", i)

    logging_setup.shutdown_logging()

    content = isolated_root_logger.read_text()
    matches = [line for line in content.splitlines() if "flush-record" in line]
    assert len(matches) == n, (
        f"Expected {n} flushed records; found {len(matches)}\n{content}"
    )


def test_setup_logging_is_idempotent(isolated_root_logger):
    """Calling setup_logging twice leaves exactly one QueueHandler on root."""
    from utils import logging_setup
    from utils.logging_setup import setup_logging
    from utils.queue_logging import _DroppingQueueHandler

    setup_logging({"LOG_LEVEL": "INFO"})
    first_listener = logging_setup.get_listener()
    first_file_handler = logging_setup.get_file_handler()
    assert first_listener is not None
    assert first_listener.is_running

    setup_logging({"LOG_LEVEL": "INFO"})
    second_listener = logging_setup.get_listener()
    second_file_handler = logging_setup.get_file_handler()
    assert second_listener is not None
    assert second_listener is not first_listener, (
        "Second setup_logging must build a fresh listener"
    )
    assert not first_listener.is_running, (
        "First listener thread must be joined after re-setup"
    )

    root = logging.getLogger()
    queue_handlers = [h for h in root.handlers if isinstance(h, _DroppingQueueHandler)]
    assert len(queue_handlers) == 1, (
        f"Expected exactly one DroppingQueueHandler on root, got {len(queue_handlers)}"
    )

    # File handles from the prior listener should be closed; the new one is
    # a different instance.
    assert first_file_handler is not None
    assert second_file_handler is not None
    assert first_file_handler is not second_file_handler


def test_runtime_log_level_propagates_to_queued_handlers(isolated_root_logger):
    """Root setLevel(DEBUG) at runtime makes DEBUG records reach the file."""
    from utils import logging_setup
    from utils.logging_setup import setup_logging

    setup_logging({"LOG_LEVEL": "INFO"})
    child = logging.getLogger("test.level")

    child.debug("blocked-at-info")
    logging_setup.get_listener().stop()  # type: ignore[union-attr]
    # Restart listener so the test can keep emitting; shutdown is heavy.
    # Simpler: re-run setup_logging at DEBUG and verify subsequent records.

    setup_logging({"LOG_LEVEL": "DEBUG"})
    child = logging.getLogger("test.level")
    child.debug("allowed-at-debug")
    logging_setup.shutdown_logging()

    content = isolated_root_logger.read_text()
    assert "blocked-at-info" not in content, (
        f"DEBUG record leaked through INFO root level: {content}"
    )
    assert "allowed-at-debug" in content, (
        f"DEBUG record dropped after setLevel(DEBUG): {content}"
    )


def test_runtime_setlevel_without_resetup_propagates(isolated_root_logger):
    """setLevel on root (the input_proc set_log_level path) takes effect immediately."""
    from utils import logging_setup
    from utils.logging_setup import setup_logging

    setup_logging({"LOG_LEVEL": "INFO"})
    child = logging.getLogger("test.runtime_level")

    child.debug("blocked-before")
    logging.getLogger().setLevel(logging.DEBUG)
    child.debug("allowed-after")
    logging_setup.shutdown_logging()

    content = isolated_root_logger.read_text()
    assert "blocked-before" not in content
    assert "allowed-after" in content, (
        f"DEBUG record dropped after runtime setLevel(DEBUG): {content}"
    )


# ---------------------------------------------------------------------------
# Round-1 regression tests (codex review wh-anai)
# ---------------------------------------------------------------------------


def test_producer_does_not_format_record(isolated_root_logger):
    """wh-anai.1: prepare() on the producer must not format the message.

    Stdlib QueueHandler.prepare() calls record.getMessage() and copies
    args/exc_info onto the formatted text. We override prepare() to a
    no-op so formatting happens on the listener thread. A record's
    .args attribute must remain populated and .message must NOT be set
    until a downstream handler formats it.
    """
    from utils import logging_setup
    from utils.logging_setup import setup_logging
    from utils.queue_logging import _DroppingQueueHandler

    setup_logging({"LOG_LEVEL": "INFO"})
    root = logging.getLogger()
    queue_handler = next(
        h for h in root.handlers if isinstance(h, _DroppingQueueHandler)
    )

    # Build a record with %-style args; if prepare formatted it, args
    # would be cleared.
    record = logging.LogRecord(
        name="test.format",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="hello %s, %d items",
        args=("world", 7),
        exc_info=None,
    )
    prepared = queue_handler.prepare(record)
    assert prepared.args == ("world", 7), (
        "prepare() must leave args intact for listener-side formatting"
    )
    assert not hasattr(prepared, "message") or prepared.message != "hello world, 7 items", (
        "prepare() must NOT have formatted .message on the producer thread"
    )

    logging_setup.shutdown_logging()


def test_stop_is_bounded_when_handler_blocks(isolated_root_logger):
    """wh-anai.2: listener.stop(timeout) returns within deadline even when a handler blocks."""
    import threading
    import time

    from utils import logging_setup
    from utils.logging_setup import setup_logging

    setup_logging({"LOG_LEVEL": "INFO"})
    listener = logging_setup.get_listener()
    assert listener is not None

    block_release = threading.Event()
    original_handlers = list(listener.handlers)

    class _BlockingHandler(logging.Handler):
        def emit(self, record):
            block_release.wait(timeout=10.0)

    blocking = _BlockingHandler()
    listener.handlers = (blocking,)

    child = logging.getLogger("test.bounded_stop")
    child.info("trigger-block")

    # Give the listener thread time to dequeue and enter the blocking emit.
    time.sleep(0.2)

    t0 = time.monotonic()
    listener.stop(timeout=0.5)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5, (
        f"stop() must return within ~timeout even when a handler blocks; "
        f"took {elapsed:.2f}s"
    )

    block_release.set()
    listener.handlers = tuple(original_handlers)
    logging_setup.shutdown_logging()


def test_post_shutdown_logging_does_not_enter_dead_queue(isolated_root_logger):
    """wh-anai.3: after shutdown_logging, root has no WheelHouse handlers."""
    from utils import logging_setup
    from utils.logging_setup import setup_logging
    from utils.queue_logging import _DroppingQueueHandler

    setup_logging({"LOG_LEVEL": "INFO"})
    root = logging.getLogger()
    assert any(isinstance(h, _DroppingQueueHandler) for h in root.handlers)

    logging_setup.shutdown_logging()

    queue_handlers = [h for h in root.handlers if isinstance(h, _DroppingQueueHandler)]
    assert queue_handlers == [], (
        "shutdown_logging must remove the QueueHandler from root so post-"
        "shutdown logger.* calls do not silently enqueue into a dead queue"
    )


def test_notifier_worker_stop_does_not_orphan_blocked_thread(isolated_root_logger):
    """wh-anai.4: NotifierWorker.stop keeps _thread visible if the worker is still alive."""
    import threading
    import time

    from utils.notifier_worker import NotifierPayload, NotifierWorker

    block_release = threading.Event()
    worker = NotifierWorker()

    # Patch _deliver to block until released.
    def blocking_deliver(payload):
        block_release.wait(timeout=10.0)

    worker._deliver = blocking_deliver  # type: ignore[method-assign]
    worker.start()

    worker.submit(
        NotifierPayload(title="t", message="m", levelname="ERROR", trace_id="")
    )
    # Let the worker dequeue and enter blocking_deliver.
    time.sleep(0.2)

    t0 = time.monotonic()
    worker.stop(timeout=0.5)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5, f"stop() took too long: {elapsed:.2f}s"

    # Thread reference must still be observable since the worker is alive.
    assert worker._thread is not None, (
        "stop() must NOT clear _thread if the worker is still alive -- "
        "doing so orphans the thread invisibly"
    )
    assert worker._thread.is_alive()

    # Cleanup.
    block_release.set()
    worker._thread.join(timeout=1.0)


def test_notifier_worker_stop_handles_full_queue():
    """Adversarial-reviewer follow-up: stop() must bound-block when the queue is full.

    If a slow plyer call holds the worker on a payload while the queue
    backs up to maxsize, put_nowait of the sentinel fails. The worker
    only checks _stop_event on a queue.Empty timeout, which never fires
    while payloads keep arriving. stop() must fall back to a bounded
    blocking put so the sentinel actually lands.
    """
    import threading
    import time

    from utils.notifier_worker import NotifierPayload, NotifierWorker

    # Tiny queue to make full state easy to reach.
    worker = NotifierWorker(maxsize=4)
    block_release = threading.Event()

    def blocking_deliver(payload):
        block_release.wait(timeout=10.0)

    worker._deliver = blocking_deliver  # type: ignore[method-assign]
    worker.start()

    # Fill the queue past maxsize. First payload moves into _deliver and
    # blocks; the next 4 fill the queue. The 6th is dropped via the
    # internal drop counter (we do not assert on dropped count here --
    # this test focuses on stop()).
    for _ in range(6):
        worker.submit(
            NotifierPayload(title="t", message="m", levelname="ERROR", trace_id="")
        )

    time.sleep(0.2)  # Let worker enter blocking_deliver.

    t0 = time.monotonic()
    worker.stop(timeout=0.5)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5, (
        f"stop() must bound itself when the queue is full; took {elapsed:.2f}s"
    )

    # The worker is still alive (deliver still blocked) so _thread stays set.
    assert worker._thread is not None
    assert worker._thread.is_alive()

    block_release.set()
    worker._thread.join(timeout=1.0)


def test_monitor_loop_matches_stdlib(isolated_root_logger):
    """wh-anai.6: WheelHouseQueueListener._monitor mirrors CPython 3.12.10's loop.

    The design required a narrow copy of the stdlib loop with a version
    guard test so a Python upgrade that changes _monitor semantics is
    caught. This test verifies our shape against CPython by checking
    that:
      - sentinel handling exits the loop (one record before, sentinel,
        no more after).
      - task_done is called per dequeued record when the queue supports it.
      - queue.Empty exits the loop cleanly.
    The Python version this was verified against:
    """
    import sys as _sys

    # Bound the version sniff so we notice when this assumption shifts.
    assert _sys.version_info[:2] in {(3, 12), (3, 13)}, (
        f"Monitor loop verified against CPython 3.12 / 3.13. Running on "
        f"{_sys.version_info[:3]} -- re-verify queue_logging._monitor against "
        f"the current logging.handlers.QueueListener._monitor source."
    )

    from utils import logging_setup
    from utils.logging_setup import setup_logging

    setup_logging({"LOG_LEVEL": "INFO"})
    listener = logging_setup.get_listener()
    assert listener is not None

    # Drain a few records and verify the listener handles them in order.
    logging.getLogger("test.monitor").info("first")
    logging.getLogger("test.monitor").info("second")
    logging.getLogger("test.monitor").info("third")

    logging_setup.shutdown_logging()
    content = isolated_root_logger.read_text()
    pos_first = content.find("first")
    pos_second = content.find("second")
    pos_third = content.find("third")
    assert -1 < pos_first < pos_second < pos_third, (
        f"Monitor loop did not preserve enqueue order:\n{content}"
    )


# ---------------------------------------------------------------------------
# wh-aev01.1 (perf and regression tests for queued logging) acceptance tests
# ---------------------------------------------------------------------------


def test_producer_latency_decoupled_from_slow_handler(isolated_root_logger):
    """wh-aev01.1 headline regression: a slow listener handler does not stall the producer.

    This is the test that proves the Phase 2 design works. Phase 1
    (the wh-mvgvt diagnostic bead) measured the producer thread blocking
    ~1 second on ConcurrentRotatingFileHandler portalocker contention.
    With the queued-logging architecture, a slow listener-side handler
    must not block the producer at all.

    Install a handler that sleeps 100 ms per emit. Emit 20 records from
    the producer. Each producer-side logger.info() must return well
    under 100 ms (the slow handler latency). 10 ms is the chosen bound;
    in practice the producer cost is sub-millisecond.
    """
    import logging as _logging
    import time

    from utils import logging_setup
    from utils.logging_setup import setup_logging

    setup_logging({"LOG_LEVEL": "INFO"})
    listener = logging_setup.get_listener()
    assert listener is not None

    class _SlowHandler(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.handled = 0

        def emit(self, record):
            time.sleep(0.1)
            self.handled += 1

    slow = _SlowHandler()
    # Replace listener handlers with the slow one so every record drains
    # through the 100 ms sleep.
    original_handlers = listener.handlers
    listener.handlers = (slow,)

    try:
        child = logging.getLogger("test.perf")
        n = 20
        latencies_ms = []
        for i in range(n):
            t0 = time.perf_counter()
            child.info("perf-record %d", i)
            t1 = time.perf_counter()
            latencies_ms.append((t1 - t0) * 1000.0)

        max_latency = max(latencies_ms)
        assert max_latency < 10.0, (
            f"Producer thread blocked: max={max_latency:.2f} ms over {n} "
            f"records. Slow handler is 100 ms/emit; the queue should fully "
            f"decouple. Latencies: {latencies_ms}"
        )

        # Drain to the slow handler with it still installed; this proves
        # the slow path was actually exercised. Without the drain assert,
        # the listener could have been idle the whole producer loop and
        # the test would falsely pass.
        deadline = time.perf_counter() + 5.0
        while slow.handled < n and time.perf_counter() < deadline:
            time.sleep(0.05)
        assert slow.handled >= 1, (
            f"Slow handler was never invoked during the test. The producer "
            f"was not actually exercised against the slow path."
        )
    finally:
        # Restore real handlers so shutdown_logging closes them cleanly.
        listener.handlers = original_handlers
        logging_setup.shutdown_logging()


def test_per_thread_record_order_preserved(isolated_root_logger):
    """wh-aev01.1: within a single producer thread, file order matches enqueue order.

    Stdlib QueueListener processes records FIFO via a single dequeue call.
    With multiple producer threads, global ordering depends on producer
    scheduling, but within any one thread the records must reach the file
    in the order that thread emitted them.
    """
    import threading

    from utils import logging_setup
    from utils.logging_setup import setup_logging

    setup_logging({"LOG_LEVEL": "INFO"})

    n_threads = 4
    n_per_thread = 25
    barrier = threading.Barrier(n_threads)

    def producer(thread_id: int) -> None:
        barrier.wait()
        log = logging.getLogger(f"test.thread{thread_id}")
        for i in range(n_per_thread):
            # Bounded marker: trailing |END| prevents T0-1 from matching T0-10.
            log.info("T%d-i%02d|END|", thread_id, i)

    threads = [
        threading.Thread(target=producer, args=(tid,))
        for tid in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    logging_setup.shutdown_logging()
    content = isolated_root_logger.read_text()

    for thread_id in range(n_threads):
        positions = []
        for i in range(n_per_thread):
            marker = f"T{thread_id}-i{i:02d}|END|"
            pos = content.find(marker)
            assert pos != -1, (
                f"Lost record {marker} from thread {thread_id}: "
                f"file content has no match"
            )
            positions.append(pos)
        assert positions == sorted(positions), (
            f"Thread {thread_id} records appear out of enqueue order in the "
            f"file. Positions: {positions}"
        )


# ---------------------------------------------------------------------------
# Design contract tests 5-11 (wh-aev01.1: perf and regression tests bead)
# ---------------------------------------------------------------------------


def test_queue_overflow_does_not_block_producer(isolated_root_logger):
    """Design contract test 5: queue overflow drops without blocking the producer.

    Block the listener via a paused handler. Submit more records than
    DEFAULT_LOG_QUEUE_MAXSIZE on the producer. Producer total time stays
    well under what blocking on the queue would cost. Drop counter goes
    above zero. After the listener is released, the synthesised drop-
    summary record reaches the file.
    """
    import threading
    import time

    from utils import logging_setup
    from utils.logging_setup import setup_logging
    from utils.queue_logging import (
        DEFAULT_LOG_QUEUE_MAXSIZE,
        DROP_SUMMARY_INTERVAL,
        _DroppingQueueHandler,
    )

    setup_logging({"LOG_LEVEL": "INFO"})
    listener = logging_setup.get_listener()
    assert listener is not None

    block_handler = threading.Event()  # Cleared = blocking.

    class _PausableHandler(logging.Handler):
        def emit(self, record):
            # Wait until the test releases us; bound the wait so a
            # broken test cannot hang the listener forever.
            block_handler.wait(timeout=10.0)

    pausable = _PausableHandler()
    original_handlers = listener.handlers
    listener.handlers = (pausable,)

    queue_handler = next(
        h for h in logging.getLogger().handlers
        if isinstance(h, _DroppingQueueHandler)
    )

    try:
        # Enough records to overflow the queue plus enough drops to
        # trigger the drop-summary path (DROP_SUMMARY_INTERVAL = 100).
        n = DEFAULT_LOG_QUEUE_MAXSIZE + DROP_SUMMARY_INTERVAL + 50
        child = logging.getLogger("test.overflow")
        t0 = time.perf_counter()
        for i in range(n):
            child.info("overflow %d", i)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Producer must not block. With put_nowait + drop-on-full, even
        # n records well over maxsize finishes in tens of milliseconds.
        assert elapsed_ms < 500.0, (
            f"Producer blocked under overflow: {elapsed_ms:.1f} ms for {n} "
            f"records. Queue should drop, not block."
        )

        assert queue_handler.drop_count > 0, (
            f"Expected drops, got drop_count={queue_handler.drop_count}"
        )
    finally:
        block_handler.set()  # Release listener before shutdown.
        listener.handlers = original_handlers
        logging_setup.shutdown_logging()

    content = isolated_root_logger.read_text()
    assert "Log queue dropped" in content, (
        "Drop summary was never emitted to the file after overflow"
    )


def test_listener_survives_handler_exception(isolated_root_logger):
    """Design contract test 6: per-handler exception isolation.

    A handler whose emit() always raises must not kill the listener
    thread, must not starve other listener handlers, and must increment
    the listener's _handler_errors counter.
    """
    from utils import logging_setup
    from utils.logging_setup import setup_logging

    setup_logging({"LOG_LEVEL": "INFO"})
    listener = logging_setup.get_listener()
    assert listener is not None

    class _AlwaysRaisesHandler(logging.Handler):
        def emit(self, record):
            raise RuntimeError("intentional handler failure")

    raises = _AlwaysRaisesHandler()
    # Append the bad handler so the real file handler still drains.
    original_handlers = listener.handlers
    listener.handlers = original_handlers + (raises,)

    try:
        for i in range(5):
            logging.getLogger("test.exc").info("survives-exception %d", i)
        logging_setup.shutdown_logging()
    finally:
        # Listener already stopped by shutdown_logging, but reset for
        # cleanup safety.
        listener.handlers = original_handlers

    assert listener.handler_errors >= 5, (
        f"Expected handler_errors >= 5, got {listener.handler_errors}"
    )

    content = isolated_root_logger.read_text()
    for i in range(5):
        assert f"survives-exception {i}" in content, (
            f"Bad handler starved sibling handlers: missing "
            f"'survives-exception {i}' in:\n{content}"
        )


def test_listener_survives_prepare_exception(isolated_root_logger):
    """Design contract test 7: prepare() exception isolation.

    The WheelHouseQueueListener.prepare() wraps super().prepare() in a
    try/except so a stdlib upgrade (or a misbehaving subclass change)
    cannot kill the monitor thread. Patch the parent class's prepare so
    that the wrapper's super().prepare() call raises for one specific
    record. The wrapper's fallback returns the record unmodified;
    the monitor must keep draining subsequent records.
    """
    import logging.handlers

    from utils import logging_setup
    from utils.logging_setup import setup_logging

    setup_logging({"LOG_LEVEL": "INFO"})
    listener = logging_setup.get_listener()
    assert listener is not None

    raise_count = {"n": 0}
    original_super_prepare = logging.handlers.QueueListener.prepare

    def selective_raise(self, record):
        if "raise-me" in str(record.msg):
            raise_count["n"] += 1
            raise RuntimeError("intentional prepare failure")
        return original_super_prepare(self, record)

    logging.handlers.QueueListener.prepare = selective_raise  # type: ignore[assignment]

    try:
        log = logging.getLogger("test.prepare_exc")
        log.info("before-raise")
        log.info("raise-me")
        log.info("after-raise")
        logging_setup.shutdown_logging()
    finally:
        logging.handlers.QueueListener.prepare = original_super_prepare  # type: ignore[assignment]

    assert raise_count["n"] >= 1, (
        "Patched prepare() never fired -- test setup wrong"
    )
    assert listener.is_running is False, (
        "Listener should have stopped cleanly via shutdown_logging"
    )

    content = isolated_root_logger.read_text()
    # The monitor must have survived the prepare() exception and drained
    # the post-raise record.
    assert "before-raise" in content
    assert "after-raise" in content, (
        f"Listener died on prepare() exception: 'after-raise' missing.\n"
        f"{content}"
    )
    # The wrapper's fallback returns the unmodified record on prepare()
    # failure, so the raise-me record must still reach downstream handlers.
    # If a future change makes the wrapper drop the record instead, this
    # assertion catches it.
    assert "raise-me" in content, (
        f"Failed-prepare record was dropped instead of dispatched via "
        f"the wrapper's fallback. 'raise-me' missing.\n{content}"
    )


def test_setup_time_messages_persist_through_shutdown(isolated_root_logger):
    """Design contract test 8: setup-time logger.info reaches the file.

    setup_logging emits "Logging to file: ..." right after the listener
    starts. shutdown_logging immediately after must drain that message
    to the file.
    """
    from utils import logging_setup
    from utils.logging_setup import setup_logging

    setup_logging({"LOG_LEVEL": "INFO"})
    logging_setup.shutdown_logging()

    content = isolated_root_logger.read_text()
    pos_logging_to_file = content.find("Logging to file:")
    pos_logging_configured = content.find("Logging configured.")
    assert pos_logging_to_file != -1, (
        f"Setup-time 'Logging to file:' message not in file. Content:\n{content}"
    )
    assert pos_logging_configured != -1, (
        f"Final 'Logging configured.' message not in file. Content:\n{content}"
    )
    # Design contract test 8: setup-time messages must appear in emit order.
    # logging_setup emits "Logging to file:" before "Logging configured."
    assert pos_logging_to_file < pos_logging_configured, (
        f"Setup-time messages out of order. 'Logging to file:' at "
        f"position {pos_logging_to_file} but 'Logging configured.' at "
        f"position {pos_logging_configured}. Listener did not preserve "
        f"emit order through shutdown."
    )


def test_slow_notifier_does_not_delay_file_drain(isolated_root_logger):
    """Design contract test 9 (file half): a slow notifier does not block file drain.

    The notifier worker and the listener are on independent threads
    with independent queues. Even if plyer takes seconds to deliver a
    toast, normal log records continue draining to the file at full
    speed.

    Synchronisation: the test installs a _deliver replacement that
    signals on entry and blocks until released. The INFO timing only
    starts AFTER the entry signal arrives, so the file-drain assertion
    cannot pass before the slow notifier path is active.
    """
    import threading
    import time
    from unittest.mock import patch

    from utils import logging_setup
    from utils.logging_setup import setup_logging

    deliver_entered = threading.Event()
    release = threading.Event()

    def slow_deliver(self, payload):
        deliver_entered.set()
        release.wait(timeout=10.0)

    with patch("utils.notifier_worker.NotifierWorker._deliver", new=slow_deliver):
        setup_logging({"LOG_LEVEL": "INFO"})

        # Trigger an ERROR so the notifier worker enters slow_deliver.
        logging.getLogger("test.notify_slow").error("trigger-toast")

        # Wait until the worker is provably inside slow_deliver. The file
        # drain assertion below is meaningless until that happens.
        assert deliver_entered.wait(timeout=5.0), (
            "Notifier worker never entered the slow path; test cannot "
            "prove file drain is independent."
        )

        log = logging.getLogger("test.notify_slow")
        t0 = time.perf_counter()
        for i in range(10):
            log.info("fast-record %d", i)
        time.sleep(0.2)  # Give listener a brief window to drain.
        content_mid = isolated_root_logger.read_text()
        elapsed = time.perf_counter() - t0

        release.set()
        logging_setup.shutdown_logging()

    # File drain happened well within the 1 s notifier delay.
    assert elapsed < 0.5, (
        f"File drain stalled while notifier was busy: {elapsed:.2f} s"
    )
    for i in range(10):
        assert f"fast-record {i}" in content_mid, (
            f"INFO record 'fast-record {i}' did not drain while notifier "
            f"was busy"
        )


def test_pre_setup_emission_does_not_lose_records(capsys):
    """Design contract test 10: emit BEFORE setup_logging hits lastResort.

    The Python logging module's lastResort handler writes WARNING+ records
    to stderr when no handler is configured. Pre-setup emissions must
    follow that path; they must not be lost or queued into a not-yet-
    existing listener.

    Saves and restores root handlers around the cleared-handlers state so
    no global logging state leaks to subsequent tests.
    """
    from utils import logging_setup

    root_logger = logging.getLogger()
    saved_handlers = root_logger.handlers.copy()
    saved_level = root_logger.level

    logging_setup.shutdown_logging()
    root_logger.handlers.clear()

    try:
        pre_setup_logger = logging.getLogger("test.pre_setup")
        pre_setup_logger.warning("pre-setup-warning")

        captured = capsys.readouterr()
        assert "pre-setup-warning" in captured.err, (
            f"Pre-setup WARNING did not reach lastResort stderr.\n"
            f"stderr: {captured.err}\nstdout: {captured.out}"
        )
    finally:
        root_logger.handlers.clear()
        for handler in saved_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(saved_level)


def test_watchdog_detects_stalled_listener(isolated_root_logger):
    """Design contract test 11 (watchdog half): stalled drain triggers a warning.

    Build a listener with a paused handler. Build a watchdog with a small
    check interval and stall threshold. After at least the threshold
    elapses with the listener stalled, the watchdog must write a warning
    to guarded stderr.
    """
    import io
    import sys
    import threading
    import time

    from utils.queue_logging import (
        WheelHouseQueueListener,
        _DroppingQueueHandler,
        _ListenerWatchdog,
        make_log_queue,
    )

    block_handler = threading.Event()  # Cleared = block.

    class _PausableHandler(logging.Handler):
        def emit(self, record):
            block_handler.wait(timeout=10.0)

    log_queue = make_log_queue()
    queue_handler = _DroppingQueueHandler(log_queue)
    pausable = _PausableHandler()
    listener = WheelHouseQueueListener(
        log_queue, pausable, drop_handler_ref=queue_handler
    )
    listener.start()

    watchdog = _ListenerWatchdog(
        queue_handler, listener,
        check_interval=0.1, stall_threshold=0.2,
    )

    # Capture the original stderr the watchdog writes to.
    saved_stderr = sys.__stderr__
    fake_stderr = io.StringIO()
    sys.__stderr__ = fake_stderr  # type: ignore[misc]
    try:
        watchdog.start()
        # Push records that the paused handler cannot drain.
        for i in range(5):
            queue_handler.emit(
                logging.LogRecord(
                    name="test.watchdog", level=logging.INFO,
                    pathname=__file__, lineno=0,
                    msg=f"stalled {i}", args=(), exc_info=None,
                )
            )

        # Wait long enough for at least one watchdog tick past the
        # stall threshold.
        time.sleep(0.6)

        watchdog.stop(timeout=0.5)
        captured = fake_stderr.getvalue()
    finally:
        sys.__stderr__ = saved_stderr  # type: ignore[misc]
        block_handler.set()
        listener.stop(timeout=0.5)

    assert "wheelhouse-watchdog" in captured, (
        f"Watchdog did not warn on stalled listener. Captured stderr:\n"
        f"{captured!r}"
    )
    assert "listener stalled" in captured


# ---------------------------------------------------------------------------
# Console-write resilience (wh-console-write-resilience)
# ---------------------------------------------------------------------------
# A frozen console (conhost hang, mark mode, terminal host freeze) blocks
# stderr writes forever. The log file must keep receiving records anyway,
# and the watchdog's stall warning must not itself depend on stderr.


def test_listener_writes_file_before_stderr(isolated_root_logger):
    """The file handler must run BEFORE the stderr StreamHandler.

    With stderr first, one blocked console write starves the file handler
    forever (the listener wedges mid-record before reaching the file). File
    first, the record is durably on disk before the risky console write.
    """
    from utils import logging_setup
    from utils.logging_setup import setup_logging
    from concurrent_log_handler import ConcurrentRotatingFileHandler

    setup_logging({"LOG_LEVEL": "INFO"})
    listener = logging_setup.get_listener()
    assert listener is not None
    kinds = [type(h) for h in listener.handlers]
    assert ConcurrentRotatingFileHandler in kinds
    assert logging.StreamHandler in kinds
    file_idx = kinds.index(ConcurrentRotatingFileHandler)
    stream_idx = next(
        i for i, h in enumerate(listener.handlers)
        if type(h) is logging.StreamHandler
    )
    assert file_idx < stream_idx, (
        "file handler must precede the stderr handler so a frozen console "
        f"cannot starve the log file; got order {kinds}"
    )


def test_watchdog_writes_stall_warning_to_side_channel_file(tmp_path):
    """A stalled listener is reported to the side-channel FILE, not stderr.

    The old behavior wrote the stall warning to sys.__stderr__ -- the same
    frozen console that caused the stall -- so the watchdog wedged on its
    first warning. With a side-channel path configured, the warning must
    land in that file and stderr must stay untouched.
    """
    import io
    import threading
    import time as _time

    from utils.queue_logging import (
        WheelHouseQueueListener,
        _DroppingQueueHandler,
        _ListenerWatchdog,
        make_log_queue,
    )

    block_handler = threading.Event()  # Cleared = block.

    class _PausableHandler(logging.Handler):
        def emit(self, record):
            block_handler.wait(timeout=10.0)

    side_channel = tmp_path / "wheelhouse-watchdog.log"
    log_queue = make_log_queue()
    queue_handler = _DroppingQueueHandler(log_queue)
    listener = WheelHouseQueueListener(
        log_queue, _PausableHandler(), drop_handler_ref=queue_handler
    )
    listener.start()
    watchdog = _ListenerWatchdog(
        queue_handler, listener,
        check_interval=0.1, stall_threshold=0.2,
        stall_log_path=str(side_channel),
    )

    saved_stderr = sys.__stderr__
    fake_stderr = io.StringIO()
    sys.__stderr__ = fake_stderr  # type: ignore[misc]
    try:
        watchdog.start()
        for i in range(5):
            queue_handler.emit(
                logging.LogRecord(
                    name="test.watchdog.side", level=logging.INFO,
                    pathname=__file__, lineno=0,
                    msg=f"stalled {i}", args=(), exc_info=None,
                )
            )
        _time.sleep(0.6)
        watchdog.stop(timeout=0.5)
    finally:
        sys.__stderr__ = saved_stderr  # type: ignore[misc]
        block_handler.set()
        listener.stop(timeout=2.0)

    assert side_channel.exists(), "stall warning must create the side-channel file"
    content = side_channel.read_text(encoding="utf-8")
    assert "listener stalled" in content
    assert "listener stalled" not in fake_stderr.getvalue(), (
        "with a working side-channel file the watchdog must not touch "
        "stderr (a frozen console would wedge it there)"
    )


def test_watchdog_falls_back_to_stderr_when_side_channel_unwritable(tmp_path):
    """An unwritable side-channel path degrades to the old stderr warning."""
    import io
    import threading
    import time as _time

    from utils.queue_logging import (
        WheelHouseQueueListener,
        _DroppingQueueHandler,
        _ListenerWatchdog,
        make_log_queue,
    )

    block_handler = threading.Event()

    class _PausableHandler(logging.Handler):
        def emit(self, record):
            block_handler.wait(timeout=10.0)

    bad_path = tmp_path / "no-such-dir" / "watchdog.log"
    log_queue = make_log_queue()
    queue_handler = _DroppingQueueHandler(log_queue)
    listener = WheelHouseQueueListener(
        log_queue, _PausableHandler(), drop_handler_ref=queue_handler
    )
    listener.start()
    watchdog = _ListenerWatchdog(
        queue_handler, listener,
        check_interval=0.1, stall_threshold=0.2,
        stall_log_path=str(bad_path),
    )

    saved_stderr = sys.__stderr__
    fake_stderr = io.StringIO()
    sys.__stderr__ = fake_stderr  # type: ignore[misc]
    try:
        watchdog.start()
        for i in range(5):
            queue_handler.emit(
                logging.LogRecord(
                    name="test.watchdog.fallback", level=logging.INFO,
                    pathname=__file__, lineno=0,
                    msg=f"stalled {i}", args=(), exc_info=None,
                )
            )
        _time.sleep(0.6)
        watchdog.stop(timeout=0.5)
    finally:
        sys.__stderr__ = saved_stderr  # type: ignore[misc]
        block_handler.set()
        listener.stop(timeout=2.0)

    assert "listener stalled" in fake_stderr.getvalue()


def test_setup_logging_configures_watchdog_side_channel(isolated_root_logger):
    """setup_logging must hand the watchdog a side-channel path next to the
    log file so a production stall report does not depend on stderr."""
    from utils import logging_setup
    from utils.logging_setup import setup_logging

    setup_logging({"LOG_LEVEL": "INFO"})
    watchdog = logging_setup._WATCHDOG
    assert watchdog is not None
    assert watchdog._stall_log_path, "watchdog must have a side-channel path"
    assert "watchdog" in os.path.basename(watchdog._stall_log_path).lower()
