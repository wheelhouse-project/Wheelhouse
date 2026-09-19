"""Input-side containment of the single command loop
(wh-overlay-slow-uia-stale-badges.11).

Three halves share one harness here, because all three need the same thing: the
real reader loop, actually blocked. The first half is stale-command expiry,
which stops a command the Logic process already gave up on from running late.
The second half is the dispatch watchdog, which makes a stall visible while it
is still happening. The third half is shutdown while the loop is blocked
(wh-voice-access-parity.2.3.3.2.10): the continuous scroll runs on its own
timer thread, and a blocked loop can neither read the shutdown signal nor run
the cleanup that would stop it, so the timer has to read that signal itself.

THE HALF NEITHER OF THEM CAN DO. Nothing here cancels the running call. Windows
offers no way to interrupt a cross-process call that a provider never answers, so
the provider holds the loop until it returns. The watchdog reports the stall; it
does not end it.

WHY THIS TEST DRIVES THE REAL LOOP. Codex asked for exactly this shape in the
2026-08-29 comment on the bead: let the Input process actually DEQUEUE a blocked
request, then deliver the next command, then assert what the loop does with it.
A test that replaces the transport with a mock cannot show this. The two guard
tests on the settle-repaint branch replace app.send_request with an async mock,
so they prove that the Logic side entered the mock twice and nothing about
delivery or the reader loop. This test therefore runs ``input_process_main``
itself, against a real shared-memory segment and a real ready event, and writes
its frames with the same 4-byte-size-prefix protocol the sender uses.

THE SEQUENCE IT REPRODUCES, which is the production failure from the bead:

  1. The loop dequeues a walk, clears command_ready_event, and dispatches it.
     The provider never answers, so the handler blocks and holds the one loop.
  2. The Logic process gives up. The frame is already free, so the next command
     is written and announced normally.
  3. The provider finally answers. The loop takes its next turn and dequeues the
     command from step 2 -- which by now is older than its own delivery deadline.

Step 3 is the defect: nothing consults the deadline, so a command that Logic
already gave up on runs against a screen that has changed since.

WHAT THE TEST DOES NOT COVER. It does not cross a real process boundary; the
loop runs in a thread with the same shared memory and the same event objects.
The cross-process journey belongs to wh-overlay-slow-uia-stale-badges.12, which
another session owns. The clock comparison this feature rests on was measured
separately: time.get_clock_info("monotonic") reports GetTickCount64() on this
platform, which counts system uptime and is shared by every process, and its
resolution is 15.6 ms. No assertion here depends on a finer interval, which is
why every deadline below is either far in the future or already in the past.
"""

import faulthandler
import logging
import pickle
import queue
import struct
import threading
import time
from multiprocessing import shared_memory
from unittest.mock import MagicMock, patch

import pytest

import input_proc

# Comfortably larger than any frame this test writes, and the same order of
# magnitude as the production segment.
_SHM_BYTES = 64 * 1024

# How long any wait in this test may take before it is called a failure. The
# loop polls the ready event every 10 ms, so this is three orders of magnitude
# of headroom and still fails fast when the loop never gets there.
_WAIT_TIMEOUT_S = 10.0

# The name input_proc gives its watchdog thread. Matched rather than imported
# because the name is what a reader of a stack dump sees, so a test that
# pinned it by reference would not notice the name changing.
_WATCHDOG_THREAD_NAME = "input-dispatch-watchdog"


class _FakeHandler:
    """Stands in for UIActionHandler with one deliberately blocked action.

    ``start_overlay_walk`` blocks until the test releases it, which is how the
    test holds the single reader loop the way a provider that never answers
    holds it in production. Every other action records that it ran, in order.
    """

    def __init__(self):
        self.release = threading.Event()
        self.entered_walk = threading.Event()
        self.entered_stall = threading.Event()
        # Which actions hold the loop. A test that needs the loop held by an
        # action the generic emitter answers replaces this set, because
        # start_overlay_walk is one of the self-responding actions listed at
        # the top of input_proc: the real handler emits its own reply, so a
        # stand-in that returns None produces no response at all.
        self.stall = {"start_overlay_walk"}
        self.executed = []
        self._lock = threading.Lock()

    def start_overlay_walk(self, **kwargs):
        self._record("start_overlay_walk")

    def start_utterance(self, **kwargs):
        self._record("start_utterance")

    def end_utterance(self, **kwargs):
        self._record("end_utterance")

    def invalidate_buffer(self, **kwargs):
        pass

    # wh-voice-access-parity.2.3.3. The continuous-scroll actions record the
    # same way every other action here does. The expired-stop test below needs
    # to see that the loop reached stop_continuous_scroll on a command it
    # dropped as expired.
    def start_continuous_scroll(self, **kwargs):
        self._record("start_continuous_scroll")

    def stop_continuous_scroll(self, **kwargs):
        self._record("stop_continuous_scroll")

    def _record(self, action):
        with self._lock:
            self.executed.append(action)
        if action not in self.stall:
            return
        if action == "start_overlay_walk":
            self.entered_walk.set()
        self.entered_stall.set()
        # Bounded so a failing test cannot hang the suite: release is what a
        # test uses, and the timeout is only a backstop.
        self.release.wait(timeout=_WAIT_TIMEOUT_S)

    def ran(self, action):
        with self._lock:
            return action in self.executed


class _Harness:
    """Runs the real reader loop in a thread and writes real frames to it."""

    def __init__(self):
        self.shm = shared_memory.SharedMemory(create=True, size=_SHM_BYTES)
        self.command_ready = threading.Event()
        self.input_ready = threading.Event()
        self.shutdown = threading.Event()
        self.responses = queue.Queue()
        self.handler = _FakeHandler()
        self._thread = None
        self._patches = []

    def start(self):
        # Recorded before the loop starts, so watchdog_thread can tell this
        # harness's watchdog from a previous test's. stop() does not join the
        # watchdog, and it can still be inside its final wait when the next
        # test begins.
        self._other_watchdogs = _watchdog_threads_now()
        # input_process_main imports these four inside the function, so the
        # patches must land on the SOURCE modules; patching attributes of
        # input_proc would leave the function-local imports untouched.
        config = {"ui_actions": {"timing": {}}}
        config_service = MagicMock()
        config_service.get_config.return_value = config
        handler_patch = patch(
            "ui.ui_actions.UIActionHandler", return_value=self.handler,
        )
        patches = [
            patch("services.wheelhouse.config_service.ConfigService",
                  return_value=config_service),
            patch("utils.logging_setup.setup_logging"),
            patch("utils.process_priority.elevate_process_priority"),
            # The loop points the process-global fault handler at a
            # crash-dump file, and closes that file itself in its outer
            # cleanup finally. Correct in the spawned Input process, wrong
            # here: the loop is a thread in pytest's own interpreter, so the
            # real call would rebind PYTEST's handler to a descriptor that
            # the loop then closes and the operating system is free to hand
            # to something else. Nothing calls faulthandler.disable in
            # teardown on purpose -- that would switch off whatever handler
            # the pytest host configured for itself (codex round 1, finding
            # .11.2.2; the lifetime corrected in round 3, finding .11.2.9).
            patch("faulthandler.enable"),
            handler_patch,
            patch.object(input_proc.mouse, "Listener", MagicMock()),
            patch.object(input_proc.keyboard, "Listener", MagicMock()),
        ]
        for p in patches:
            started = p.start()
            if p is handler_patch:
                # The MagicMock that replaced ui.ui_actions.UIActionHandler.
                # The shutdown half reads the keyword arguments the loop
                # constructed it with (wh-voice-access-parity.2.3.3.2.10).
                # Recorded by identity rather than by list position, so
                # adding a patch above cannot silently point this at the
                # wrong mock.
                self.handler_class = started
        self._patches = patches

        def run():
            input_proc.input_process_main(
                self.shm.name, self.command_ready, self.input_ready,
                self.responses, self.shutdown,
            )

        self._thread = threading.Thread(target=run, name="input-loop-under-test")
        self._thread.daemon = True
        self._thread.start()
        assert self.input_ready.wait(timeout=_WAIT_TIMEOUT_S), (
            "the Input loop never signalled that it was ready"
        )

    def send(
        self, action, deadline, request_id=None, params=None,
        awaited_window=None,
    ):
        """Write one frame the way app.py's _frame_and_write writes it.

        Waits for the previous frame to be taken first, exactly as the sender's
        own pre-write wait does, so a test can never overwrite an unread frame
        and silently lose the command it meant to measure.

        ``params`` defaults to an empty dict, which is what every test in the
        first half of this file sends. The continuous-scroll tests pass a
        direction (wh-voice-access-parity.2.3.3).

        ``awaited_window`` is the Logic-side awaited window app.py stamps for
        the click and walk actions (wh-watchdog-stall-window). Omitted, the
        frame carries no stamp, which is what every other action sends and
        what the watchdog's fixed limit answers for.
        """
        deadline_reached = _wait_until(
            lambda: not self.command_ready.is_set(), _WAIT_TIMEOUT_S,
        )
        assert deadline_reached, "the previous frame was never taken by the loop"
        message = {
            "action": action,
            "params": {} if params is None else params,
            "trace_id": "T-EXPIRY",
            "_delivery_deadline_monotonic": deadline,
        }
        if awaited_window is not None:
            message["_awaited_window_s"] = awaited_window
        if request_id is not None:
            message["request_id"] = request_id
        data = pickle.dumps(message)
        self.shm.buf[:4] = struct.pack(">I", len(data))
        self.shm.buf[4:4 + len(data)] = data
        self.command_ready.set()

    def watchdog_thread(self):
        """This harness's own watchdog thread, once it is actually running.

        Thread.start() puts the thread in limbo before the operating system
        spawns it, and enumerate() lists it there with no ident and is_alive
        False. The loop signals input_ready right after starting it, so a test
        that looks immediately can find the object before it runs.
        """
        found = []
        assert _wait_until(
            lambda: found.append(_new_watchdog_thread(self._other_watchdogs))
            or found[-1] is not None,
            _WAIT_TIMEOUT_S,
        ), "the loop never started its dispatch watchdog thread"
        return found[-1]

    def loop_alive(self):
        """True while the thread running the real reader loop is still going.

        Codex round 2, finding .11.2.6: several assertions in this file
        observe an ABSENCE -- a command that did not run, a report that was
        not made. A dead reader loop satisfies every one of them, so an
        absence on its own cannot tell a correct refusal from a crash.
        """
        return self._thread is not None and self._thread.is_alive()

    def stop(self):
        self.shutdown.set()
        self.handler.release.set()
        if self._thread is not None:
            self._thread.join(timeout=_WAIT_TIMEOUT_S)
        for p in getattr(self, "_patches", []):
            p.stop()
        self.shm.close()
        self.shm.unlink()


def _watchdog_threads_now():
    """The watchdog threads that exist at this moment, as objects."""
    return {
        thread for thread in threading.enumerate()
        if thread.name == _WATCHDOG_THREAD_NAME
    }


def _new_watchdog_thread(already_running):
    """The live watchdog thread that is not one of *already_running*.

    Compared by object identity rather than by thread.ident. Python documents
    that a thread identifier may be reused once its thread has exited, and
    _Harness.stop() does not join the watchdog: a previous test's watchdog can
    still be alive at the moment the next harness looks, exit while the new
    loop is starting, and have its identifier handed to the new watchdog. An
    identifier filter would then reject the very thread it is looking for
    (codex round 2, finding .11.2.5).
    """
    for thread in threading.enumerate():
        if (thread.name == _WATCHDOG_THREAD_NAME and thread.is_alive()
                and thread not in already_running):
            return thread
    return None


class _WatchedDispatch(list):
    """The watch's in-flight dispatch, with its own reading recorded.

    _DispatchWatch.poll takes _current under the lock and then UNPACKS it
    into five names. Every decision it makes about the dispatch -- in flight
    at all, held past the limit, reported recently enough to suppress --
    comes after that unpack, so observing the unpack is observing the
    watchdog reach the dispatch. The event belongs to ONE dispatch, because
    begin() builds a new list for each.
    """

    def __init__(self, values):
        super().__init__(values)
        self.read_by_a_poll = threading.Event()

    def __iter__(self):
        self.read_by_a_poll.set()
        return super().__iter__()


class _DispatchDecisions:
    """Keeps the dispatch that begin() made most recently."""

    def __init__(self):
        self._lock = threading.Lock()
        self._newest = None

    def note(self, dispatch):
        with self._lock:
            self._newest = dispatch

    @property
    def newest(self):
        with self._lock:
            return self._newest


def _record_the_dispatch_decision(monkeypatch):
    """Make the watchdog's own reading of the in-flight dispatch observable.

    Each of the silence tests below asserts an ABSENCE -- no stall line, no
    recovery line -- and an absence is also what a watchdog that never looked
    produces. Two earlier attempts at closing that hole were both too weak.
    Thread.is_alive() proves only that the thread had not ENDED (round 2,
    .11.2.6). Counting completed poll calls proves only that calls happened:
    a poll that returns before reading _current still returns, and still
    raises any count kept outside it, so all three tests passed against
    exactly that no-op (round 3, .11.2.8; the hole codex found in round 4,
    .11.2.10).

    This wraps begin() instead, and swaps the dispatch the loop just
    registered for a list that records its own unpacking. Nothing about
    poll() is replaced, so the decision under observation is the production
    one. Only the watchdog can trip the record while the handler is still
    blocked: end() runs at the top of the loop's next turn, which cannot
    begin until the handler returns.
    """
    decisions = _DispatchDecisions()
    real_begin = input_proc._DispatchWatch.begin

    def begin(self, *args):
        real_begin(self, *args)
        with self._lock:
            current = self._current
            if current is not None:
                watched = _WatchedDispatch(current)
                self._current = watched
                decisions.note(watched)

    monkeypatch.setattr(input_proc._DispatchWatch, "begin", begin)
    return decisions


def _assert_the_watchdog_read_the_dispatch(decisions, why):
    """Require that a poll reached the dispatch that is in flight now."""
    dispatch = decisions.newest
    assert dispatch is not None, (
        "the loop never registered a dispatch, so there was nothing for the "
        "watchdog to read: " + why
    )
    assert dispatch.read_by_a_poll.wait(timeout=_WAIT_TIMEOUT_S), why


def _wait_until(condition, timeout_s):
    """Poll *condition* until it holds or the timeout passes."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


@pytest.fixture
def harness():
    h = _Harness()
    h.start()
    try:
        yield h
    finally:
        h.stop()


def test_a_command_that_went_stale_while_the_loop_was_blocked_is_not_executed(harness):
    """The whole defect, in one run of the real loop.

    The walk blocks the loop. A second command is delivered while it blocks and
    its deadline passes in the meantime. When the walk finally returns, the loop
    must drop that command instead of acting on it.
    """
    harness.send("start_overlay_walk", deadline=time.monotonic() + 3600.0)
    assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S), (
        "the loop never dequeued and dispatched the blocking walk, so the rest "
        "of this test would prove nothing"
    )

    # Delivered while the loop is held, with a deadline that has already passed.
    # An already-passed deadline is used rather than a short wait because the
    # platform clock resolution is 15.6 ms.
    harness.send("start_utterance", deadline=time.monotonic() - 1.0)

    harness.handler.release.set()

    assert _wait_until(lambda: not harness.command_ready.is_set(), _WAIT_TIMEOUT_S), (
        "the loop never took the second frame"
    )
    # Give the loop room to do the wrong thing, so a pass means the command was
    # really refused rather than merely slow to run.
    time.sleep(0.2)
    assert not harness.handler.ran("start_utterance"), (
        "the loop executed a command whose delivery deadline had already "
        "passed; a stale command must expire instead of running late against "
        "a screen that has changed"
    )

    # Codex round 2, finding .11.2.6. Everything above this line is an
    # ABSENCE, and a reader loop that died during the drop satisfies it just
    # as well as a correct refusal does -- codex demonstrated exactly that by
    # making the drop's own report raise, leaving the assertion true and the
    # loop thread dead. A fresh command through the SAME harness is what
    # separates a refusal from a crash.
    harness.send("end_utterance", deadline=time.monotonic() + 3600.0)
    assert _wait_until(
        lambda: harness.handler.ran("end_utterance"), _WAIT_TIMEOUT_S,
    ), (
        "the loop never ran a fresh command after dropping the expired one, "
        "so the drop above cannot be told apart from a dead reader loop"
    )
    assert harness.loop_alive(), "the reader loop ended after dropping a command"


def test_the_expired_command_is_reported_and_not_dropped_in_silence(harness):
    """A drop the caller never hears about is the failure this bead names.

    The bead's acceptance requires the drop to be logged and reported, not
    silent. A request that carried a request_id must get an answer, so the
    waiting caller learns why rather than only seeing its own timeout.
    """
    harness.send("start_overlay_walk", deadline=time.monotonic() + 3600.0)
    assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)

    harness.send(
        "start_utterance", deadline=time.monotonic() - 1.0,
        request_id="stale-request-id",
    )
    harness.handler.release.set()

    def answered():
        return not harness.responses.empty()

    assert _wait_until(answered, _WAIT_TIMEOUT_S), (
        "the expired command produced no response at all; the caller would "
        "wait for its own timeout with no reason recorded"
    )
    message = harness.responses.get_nowait()
    assert message["request_id"] == "stale-request-id"
    assert message.get("error") is True, (
        f"an expired command must be answered as an error, got {message!r}"
    )


def test_a_fresh_command_still_runs_after_the_block_clears(harness):
    """The expiry must not become a blanket refusal.

    Codex's recipe asks for exactly this half: after the blocked handler
    returns, a command that is still within its deadline must be EXECUTED, not
    merely accepted and forgotten.
    """
    harness.send("start_overlay_walk", deadline=time.monotonic() + 3600.0)
    assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)

    harness.send("end_utterance", deadline=time.monotonic() + 3600.0)
    harness.handler.release.set()

    assert _wait_until(lambda: harness.handler.ran("end_utterance"), _WAIT_TIMEOUT_S), (
        "a command still inside its deadline was not executed after the "
        "blocked handler returned"
    )


def test_a_command_without_a_deadline_is_still_executed(harness):
    """Absence of a deadline is not staleness.

    Nothing in production omits the field -- app.py stamps it before the frame
    is frozen -- but a direct unit test of a handler can, and refusing those
    would turn this containment into a silent regression somewhere else.
    """
    harness.send("start_overlay_walk", deadline=time.monotonic() + 3600.0)
    assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)
    harness.handler.release.set()

    assert _wait_until(lambda: not harness.command_ready.is_set(), _WAIT_TIMEOUT_S)
    message = {"action": "end_utterance", "params": {}, "trace_id": "T-EXPIRY"}
    data = pickle.dumps(message)
    harness.shm.buf[:4] = struct.pack(">I", len(data))
    harness.shm.buf[4:4 + len(data)] = data
    harness.command_ready.set()

    assert _wait_until(lambda: harness.handler.ran("end_utterance"), _WAIT_TIMEOUT_S), (
        "a command carrying no delivery deadline was refused; only an expired "
        "deadline may stop a command here"
    )


# ---------------------------------------------------------------------------
# The dispatch watchdog. Same harness, second half of the bead.
# ---------------------------------------------------------------------------


def _shorten_the_watchdog(monkeypatch, report_s=0.05):
    """Make the stall limit small enough to test in under a second.

    The watchdog reads these module globals on every poll, so patching them
    after the loop has already started still takes effect.
    """
    monkeypatch.setattr(input_proc, "_DISPATCH_STALL_REPORT_S", report_s)
    monkeypatch.setattr(input_proc, "_DISPATCH_STALL_REPEAT_S", report_s)
    monkeypatch.setattr(input_proc, "_WATCHDOG_POLL_S", 0.01)


def test_a_stalled_dispatch_is_reported_while_it_is_still_stalled(
    harness, monkeypatch, caplog,
):
    """A hung provider must not stop the loop in silence.

    The value here is that the report arrives DURING the stall, not after it.
    A message that only appears once the provider finally answers cannot tell
    anyone why the application stopped responding while it was happening.
    """
    _shorten_the_watchdog(monkeypatch)
    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send("start_overlay_walk", deadline=time.monotonic() + 3600.0)
        assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)

        def reported():
            return any(
                "start_overlay_walk" in record.getMessage()
                and "stalled" in record.getMessage().lower()
                for record in caplog.records
            )

        # Deliberately asserted BEFORE the handler is released, so a pass
        # cannot come from a message logged after the stall ended.
        assert _wait_until(reported, _WAIT_TIMEOUT_S), (
            "the loop was held inside a handler well past the stall limit and "
            "nothing said so; that is the silent freeze this bead exists to "
            "remove"
        )


def test_the_watchdog_says_nothing_about_a_dispatch_that_finishes_in_time(
    harness, monkeypatch, caplog,
):
    """No stall report for ordinary work.

    A watchdog that cries on every command teaches its readers to ignore it,
    which costs the report all of its value on the one occasion it matters.
    """
    # The limit is left LONG here and the dispatch is held for a fraction of
    # it. That combination is what gives this test teeth: the dispatch is in
    # flight while the watchdog polls, so a watchdog that reported without
    # regard to the limit would be seen doing it. A dispatch that merely
    # returned quickly would prove nothing, because the watchdog would almost
    # never observe it in flight at all.
    #
    # The hold below must outlast the watchdog's FIRST sleep, which is the one
    # interval this test cannot shorten: the thread reads the poll interval
    # after each poll, so the first wait is already under way at the
    # unshortened 0.25s by the time monkeypatch lands. A shorter hold can fall
    # entirely between two polls and the test then passes no matter what the
    # watchdog does -- which is precisely how the mutation gate caught an
    # earlier 0.15s version of this test surviving the always-report mutation.
    watchdog = harness.watchdog_thread()
    decisions = _record_the_dispatch_decision(monkeypatch)
    _shorten_the_watchdog(monkeypatch, report_s=5.0)
    harness.handler.stall = {"end_utterance"}
    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "end_utterance", deadline=time.monotonic() + 3600.0,
            request_id="prompt-request-id",
        )
        assert harness.handler.entered_stall.wait(timeout=_WAIT_TIMEOUT_S)
        time.sleep(0.5)
        # .11.2.8 and .11.2.10: the silence below means nothing unless the
        # watchdog reached THIS dispatch while it was in flight.
        _assert_the_watchdog_read_the_dispatch(
            decisions,
            "the watchdog never read the dispatch while it was in flight, so "
            "the absence of a stall report says nothing about the limit",
        )
        harness.handler.release.set()
        assert _wait_until(lambda: not harness.responses.empty(), _WAIT_TIMEOUT_S)
        assert not any(
            "stalled" in record.getMessage().lower() for record in caplog.records
        ), "a dispatch that stayed well inside the limit was reported as a stall"

    # .11.2.6: silence from a watchdog that died is not the silence this test
    # is asking for.
    assert watchdog.is_alive(), (
        "the watchdog said nothing because its thread had ended, not because "
        "the dispatch stayed inside the limit"
    )


def test_a_walk_inside_its_awaited_window_is_not_reported_as_a_stall(
    harness, monkeypatch, caplog,
):
    """The limit follows the window the Logic side is actually waiting out.

    wh-watchdog-stall-window. A walk is awaited for ``[click]
    screen_read_timeout_ms``, which ships at 10000 and validates up to 60000,
    so a healthy read can hold this loop far longer than the fixed limit and
    still be inside the window nobody has given up on yet. Reporting there is
    true but not news, and a watchdog whose reports are expected is a
    watchdog nobody reads.

    The fixed limit is made SHORT here and the stamped window LONG, which is
    the arrangement that gives the test teeth: a watchdog still judging by the
    fixed limit reports within 0.05s of the dispatch, so the silence below can
    only come from the stamp being read.
    """
    watchdog = harness.watchdog_thread()
    decisions = _record_the_dispatch_decision(monkeypatch)
    _shorten_the_watchdog(monkeypatch, report_s=0.05)
    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "start_overlay_walk", deadline=time.monotonic() + 3600.0,
            awaited_window=5.0,
        )
        assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)
        time.sleep(0.5)
        # .11.2.8 and .11.2.10: the silence below means nothing unless the
        # watchdog reached THIS dispatch while it was in flight.
        _assert_the_watchdog_read_the_dispatch(
            decisions,
            "the watchdog never read the dispatch while it was in flight, so "
            "the absence of a stall report says nothing about the window",
        )
        assert not any(
            "stalled" in record.getMessage().lower() for record in caplog.records
        ), (
            "a walk still well inside its own awaited window was reported as "
            "a stall; the limit is still the fixed constant"
        )
        harness.handler.release.set()

    # .11.2.6: silence from a watchdog that died is not the silence this test
    # is asking for.
    assert watchdog.is_alive(), (
        "the watchdog said nothing because its thread had ended, not because "
        "the dispatch stayed inside its awaited window"
    )


def test_a_walk_past_its_awaited_window_is_still_reported_as_a_stall(
    harness, monkeypatch, caplog,
):
    """Tracking the window must not cost the report itself.

    wh-watchdog-stall-window. The opposite arrangement to the test above: the
    fixed limit is LONG -- longer than this test will ever wait -- and the
    stamped window SHORT, so only a watchdog that reads the stamp can report
    at all here. That is what stops the fix from
    being written as "never report a stamped command".

    ``raising=False`` on the margin: before the fix the constant does not
    exist, and the red this test owes is a missing REPORT, not a missing
    attribute.
    """
    _shorten_the_watchdog(monkeypatch, report_s=60.0)
    monkeypatch.setattr(
        input_proc, "_DISPATCH_STALL_WINDOW_MARGIN_S", 0.05, raising=False,
    )
    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "start_overlay_walk", deadline=time.monotonic() + 3600.0,
            awaited_window=0.05,
        )
        assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)

        def reported():
            return any(
                "start_overlay_walk" in record.getMessage()
                and "stalled" in record.getMessage().lower()
                for record in caplog.records
            )

        # Asserted BEFORE the handler is released, so a pass cannot come from
        # a message logged after the stall ended.
        # The budget here is DELIBERATELY far below the 60s fixed limit
        # patched above: a report that only arrives after it would be the
        # fixed limit answering, which is the thing this test rules out.
        assert _wait_until(reported, 2.0), (
            "a walk held well past its own awaited window was not reported; "
            "reading the stamp has silenced the watchdog instead of retuning "
            "it"
        )
        harness.handler.release.set()


def test_a_walk_is_given_a_margin_beyond_its_awaited_window(
    harness, monkeypatch, caplog,
):
    """The window alone is not the limit; the margin is part of it.

    wh-watchdog-stall-window. The window is charged from this process's
    dequeue instant, so a dispatch that reaches it has not necessarily
    outlived the Logic-side awaiter that started earlier. The margin is the
    slack that makes the report safe to believe, and without something
    holding it here a limit of "the window exactly" would read as correct.

    The hold below is PAST the window and INSIDE the margin, which is the
    only band that separates the two.
    """
    watchdog = harness.watchdog_thread()
    decisions = _record_the_dispatch_decision(monkeypatch)
    _shorten_the_watchdog(monkeypatch, report_s=60.0)
    monkeypatch.setattr(input_proc, "_DISPATCH_STALL_WINDOW_MARGIN_S", 3.0)
    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "start_overlay_walk", deadline=time.monotonic() + 3600.0,
            awaited_window=1.0,
        )
        assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)
        time.sleep(1.5)
        _assert_the_watchdog_read_the_dispatch(
            decisions,
            "the watchdog never read the dispatch while it was in flight, so "
            "the absence of a stall report says nothing about the margin",
        )
        assert not any(
            "stalled" in record.getMessage().lower() for record in caplog.records
        ), (
            "a walk past its awaited window but still inside the margin was "
            "reported; the limit is the window with no slack on it"
        )
        harness.handler.release.set()

    assert watchdog.is_alive(), (
        "the watchdog said nothing because its thread had ended, not because "
        "the dispatch was inside the margin"
    )


def test_an_unusable_margin_leaves_a_stamped_dispatch_still_reported(
    harness, monkeypatch, caplog,
):
    """A margin this module never chose must not silence the watchdog.

    wh-watchdog-stall-window, the sibling of the bad-stall-limit test above.
    The margin is a module global read at use, so it can hold a value this
    module never chose, and the arithmetic that consumes it runs inside
    poll() -- where a raise is swallowed by _run_dispatch_watchdog's wrapper
    and costs every later report, not just this one. Reading it through
    _watchdog_seconds is what keeps a bad value to a fallback.
    """
    _shorten_the_watchdog(monkeypatch, report_s=60.0)
    monkeypatch.setattr(
        input_proc, "_DISPATCH_STALL_WINDOW_MARGIN_S", "not a number",
    )
    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "start_overlay_walk", deadline=time.monotonic() + 3600.0,
            awaited_window=0.05,
        )
        assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)

        def reported():
            return any(
                "start_overlay_walk" in record.getMessage()
                and "stalled" in record.getMessage().lower()
                for record in caplog.records
            )

        # The 1.0s fallback margin puts the report at about 1.05s. The budget
        # is far below the 60s fixed limit patched above, so a report that
        # arrives here came from the window and the fallback, not from the
        # fixed limit.
        assert _wait_until(reported, 5.0), (
            "an unusable margin stopped the watchdog reporting at all; the "
            "value is reaching the arithmetic without being judged first"
        )
        harness.handler.release.set()


def test_the_watchdog_does_not_answer_the_request_it_reports(
    harness, monkeypatch, caplog,
):
    """One request_id still yields exactly one response.

    The wh-lla5d contract is one response per request_id. If the watchdog
    answered the stalled request as well, the handler's own later response
    would be the second answer to the same id, and the demuxer would settle a
    caller's future with whichever arrived first. The watchdog therefore
    reports to the log and leaves the answering alone; the Logic process
    already has its own timeout and its own late-response handling.
    """
    _shorten_the_watchdog(monkeypatch)
    # The request must actually STALL and actually be REPORTED, or this test
    # would pass against a build with no watchdog in it at all. The stalled
    # action is start_utterance rather than the walk because it is the generic
    # emitter's single reply that is being counted here, and the walk answers
    # itself.
    harness.handler.stall = {"start_utterance"}
    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "start_utterance", deadline=time.monotonic() + 3600.0,
            request_id="watched-request-id",
        )
        assert harness.handler.entered_stall.wait(timeout=_WAIT_TIMEOUT_S)
        assert _wait_until(
            lambda: any(
                "stalled" in record.getMessage().lower()
                for record in caplog.records
            ),
            _WAIT_TIMEOUT_S,
        ), "the stall was never reported, so this test proves nothing"

    # The handler records itself on ENTRY, so it cannot signal completion.
    # The arrival of the handler's own response is the completion signal, and
    # the pause afterwards is the window in which a second answer, if the
    # watchdog produced one, would show up.
    harness.handler.release.set()
    assert _wait_until(lambda: not harness.responses.empty(), _WAIT_TIMEOUT_S)
    time.sleep(0.2)

    answers = []
    while not harness.responses.empty():
        answers.append(harness.responses.get_nowait())
    for_this_request = [
        a for a in answers if a.get("request_id") == "watched-request-id"
    ]
    assert len(for_this_request) == 1, (
        f"expected exactly one response for one request_id, got "
        f"{for_this_request!r}"
    )


# ---------------------------------------------------------------------------
# The watchdog's own inputs: the three timing globals, and the deadline reader.
# Both are read from values the module does not control, so both need their
# malformed cases pinned (deepseek round 1, findings .11.1.2 and .11.1.3).
# ---------------------------------------------------------------------------


_UNUSABLE_SECONDS = [
    None, "6.0", (), float("nan"), float("inf"), float("-inf"),
    0, 0.0, -1.0, True, False,
]


@pytest.mark.parametrize("bad", _UNUSABLE_SECONDS)
def test_an_unusable_timing_value_falls_back_to_the_shipped_default(bad):
    """Every bad shape resolves to the fallback rather than reaching a compare.

    A non-numeric limit used to raise inside poll on every call. The wrapper
    caught it, so the thread survived and the watchdog reported NOTHING, ever
    -- the same silence this bead exists to remove, produced by the watchdog
    itself. NaN, zero and negative limits fail the other way and report every
    ordinary command. Bools are rejected because True would read as 1.0.
    """
    assert input_proc._watchdog_seconds(bad, 6.0) == 6.0


@pytest.mark.parametrize("good", [0.01, 0.25, 6.0, 10, 3600.0])
def test_a_usable_timing_value_is_returned_unchanged(good):
    """The guard must not quietly replace a value a caller meant."""
    assert input_proc._watchdog_seconds(good, 6.0) == good


def test_a_bad_stall_limit_leaves_the_watchdog_reporting_at_the_fallback(
    harness, monkeypatch, caplog,
):
    """The observable half of the guard: the alarm survives a bad limit.

    With the limit unreadable the watchdog must fall back to 6.0s, so a
    dispatch held well under that is still not reported. Before the guard the
    same dispatch raised TypeError on every poll instead, which the wrapper
    swallowed into a warning, leaving the watchdog permanently silent.

    The limit below is a NUMERIC string on purpose, and it is the whole point
    of the shape check. A limit of "not a number" is now stopped twice over --
    by the shape check and again by float() raising -- so it can no longer
    show whether the shape check is there at all; the gate's
    timing-guard-passthrough mutation survived against exactly that value.
    Text that reads as a number passes float() cleanly and becomes a live
    0.01s limit, which reports every ordinary command as a stall. A quoted
    number is also the realistic way this global goes wrong, since that is
    what a TOML value written with quotes would arrive as.
    """
    watchdog = harness.watchdog_thread()
    decisions = _record_the_dispatch_decision(monkeypatch)
    monkeypatch.setattr(input_proc, "_DISPATCH_STALL_REPORT_S", "0.01")
    monkeypatch.setattr(input_proc, "_WATCHDOG_POLL_S", 0.01)
    harness.handler.stall = {"end_utterance"}
    with caplog.at_level(logging.WARNING, logger=input_proc.logger.name):
        harness.send(
            "end_utterance", deadline=time.monotonic() + 3600.0,
            request_id="bad-limit-request-id",
        )
        assert harness.handler.entered_stall.wait(timeout=_WAIT_TIMEOUT_S)
        # Long enough to cover many polls at the shortened interval, and far
        # short of the 6.0s fallback limit.
        time.sleep(0.5)
        # .11.2.8 and .11.2.10: both silences asserted below are also what a
        # watchdog that never reached the guard produces.
        _assert_the_watchdog_read_the_dispatch(
            decisions,
            "the watchdog never read the dispatch while it was in flight, so "
            "neither silence below shows the fallback limit was applied",
        )
        harness.handler.release.set()
        assert _wait_until(lambda: not harness.responses.empty(), _WAIT_TIMEOUT_S)

    assert not any(
        "watchdog poll failed" in record.getMessage() for record in caplog.records
    ), "an unreadable stall limit reached a comparison instead of the fallback"
    assert not any(
        "stalled" in record.getMessage().lower() for record in caplog.records
    ), "the fallback limit was not applied; an ordinary dispatch was reported"
    # .11.2.6: both silences above are also what a dead watchdog produces.
    assert watchdog.is_alive(), (
        "the watchdog thread ended on the unreadable limit instead of falling "
        "back to it, so the two silences above prove nothing"
    )


class _KeyThatRaisesOnCompare(str):
    """A key whose equality test raises, as .14.38's poisoned envelope does.

    A dict carrying this key makes ``.get`` raise during the hash-bucket
    comparison. The reader runs outside the per-action try, so an unguarded
    lookup would end the Input process over one bad envelope.
    """

    def __eq__(self, other):
        raise RuntimeError("poisoned key compared")

    def __hash__(self):
        # Matching the real key's hash is what forces the equality test.
        # Hashing this instance's own text rather than one hard-coded key
        # lets the second reader's test poison ``_awaited_window_s`` with
        # the same class; for the delivery-deadline key the value is
        # unchanged (review finding .1.2).
        return str.__hash__(self)


@pytest.mark.parametrize("bad", [
    True, False, float("nan"), float("inf"), float("-inf"),
    "1234.5", None, (), [], {},
])
def test_a_malformed_deadline_reads_as_no_deadline(bad):
    """Malformed means "do not judge this command's age", never "expired".

    Each shape here defeats the age check in its own way if the guard for it
    is removed. True is the sharpest: bool is a subclass of int, so without
    the bool rejection it reads as a deadline of 1.0 -- long past -- and every
    command carrying it is dropped as stale. NaN is the opposite: every
    comparison against it is False, so nothing ever expires.
    """
    message = {"action": "end_utterance", "_delivery_deadline_monotonic": bad}
    assert input_proc._read_delivery_deadline(message) is None


@pytest.mark.parametrize("good", [0, 1, -1, 1234.5, 1e12])
def test_a_usable_deadline_is_returned_as_a_float(good):
    """The guards must not swallow a deadline the sender really stamped."""
    message = {"action": "end_utterance", "_delivery_deadline_monotonic": good}
    result = input_proc._read_delivery_deadline(message)
    assert result == float(good)
    assert isinstance(result, float)


def test_an_envelope_that_is_not_a_dict_reads_as_no_deadline():
    """The type gate runs before any lookup, so no attribute access can raise."""
    for not_a_dict in (None, "payload", 42, [("_delivery_deadline_monotonic", 1)]):
        assert input_proc._read_delivery_deadline(not_a_dict) is None


def test_a_poisoned_key_does_not_escape_the_reader(caplog):
    """The protective except is the containment .14.38 established.

    Without it the raise crosses out of the reader, past the dispatch (this
    runs outside the per-action try), and ends the Input process -- the whole
    failure mode the surrounding chokepoints exist to prevent.
    """
    message = {_KeyThatRaisesOnCompare("_delivery_deadline_monotonic"): 1.0}
    with caplog.at_level(logging.WARNING, logger=input_proc.logger.name):
        assert input_proc._read_delivery_deadline(message) is None
    assert any(
        "delivery deadline could not be read" in record.getMessage()
        for record in caplog.records
    ), "the poisoned envelope was swallowed without a word in the log"


def test_an_envelope_that_is_not_a_dict_reads_as_no_awaited_window():
    """The type gate runs before any lookup, so no attribute access can raise."""
    for not_a_dict in (None, "payload", 42, [("_awaited_window_s", 1)]):
        assert input_proc._read_awaited_window(not_a_dict) is None


def test_a_poisoned_window_key_does_not_escape_the_reader(caplog):
    """The second reader owes the containment the first one was given.

    Review finding .1.2. _read_awaited_window is a second copy of the
    decision .14.38 established for _read_delivery_deadline, and it runs at
    the same place: outside the per-action try, before any dispatch. Without
    the protective except a poisoned key ends the Input process over one bad
    envelope. Nothing pinned that until this test, so a reader that re-raised
    would have passed the whole suite -- neither walk test sends hostile
    input, and the outcomes they assert do not depend on this branch.
    """
    message = {_KeyThatRaisesOnCompare("_awaited_window_s"): 1.0}
    with caplog.at_level(logging.WARNING, logger=input_proc.logger.name):
        assert input_proc._read_awaited_window(message) is None
    assert any(
        "awaited window could not be read" in record.getMessage()
        for record in caplog.records
    ), "the poisoned envelope was swallowed without a word in the log"


def test_a_command_carrying_a_bool_deadline_is_still_executed(harness):
    """The observable half of the bool guard, driven through the real loop.

    A direct reader test says the value resolves to None. This says what that
    buys: the command RUNS. Without the bool rejection True reads as 1.0, the
    command is judged long expired, and the caller gets an error instead of
    the work it asked for.
    """
    harness.send("start_overlay_walk", deadline=time.monotonic() + 3600.0)
    assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)
    harness.handler.release.set()
    assert _wait_until(lambda: not harness.command_ready.is_set(), _WAIT_TIMEOUT_S)

    message = {
        "action": "end_utterance", "params": {}, "trace_id": "T-BOOL",
        "_delivery_deadline_monotonic": True,
    }
    data = pickle.dumps(message)
    harness.shm.buf[:4] = struct.pack(">I", len(data))
    harness.shm.buf[4:4 + len(data)] = data
    harness.command_ready.set()

    assert _wait_until(lambda: harness.handler.ran("end_utterance"), _WAIT_TIMEOUT_S), (
        "a command carrying a bool deadline was refused; a malformed deadline "
        "must mean 'do not judge the age', not 'expired'"
    )


def _stall_reports(caplog):
    """Every stall report in the captured records, in order."""
    return [
        record for record in caplog.records
        if "stalled" in record.getMessage().lower()
    ]


def test_a_continuing_stall_is_repeated_but_not_on_every_poll(
    harness, monkeypatch, caplog,
):
    """The repeat interval is what keeps a long stall readable.

    A stall that reported once and never again reads in the log as though it
    ended. A stall that reported on every poll produces four ERROR records a
    second for as long as the freeze lasts, and every ERROR record feeds the
    Windows notification handler. The interval is the difference between the
    two, and nothing pinned it before deepseek round 1 filed .11.1.1.
    """
    # Report at once, then repeat no faster than every 0.4s. The hold below
    # covers several repeat intervals and many more polls, so a suppression
    # that regressed to "report every poll" is far outside the allowed count.
    monkeypatch.setattr(input_proc, "_DISPATCH_STALL_REPORT_S", 0.05)
    monkeypatch.setattr(input_proc, "_DISPATCH_STALL_REPEAT_S", 0.4)
    monkeypatch.setattr(input_proc, "_WATCHDOG_POLL_S", 0.01)
    harness.handler.stall = {"start_utterance"}

    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "start_utterance", deadline=time.monotonic() + 3600.0,
            request_id="repeating-request-id",
        )
        assert harness.handler.entered_stall.wait(timeout=_WAIT_TIMEOUT_S)
        assert _wait_until(lambda: _stall_reports(caplog), _WAIT_TIMEOUT_S), (
            "the stall was never reported at all, so this test proves nothing"
        )
        # Held across roughly three repeat intervals and about 130 polls.
        time.sleep(1.3)
        reports = len(_stall_reports(caplog))
        harness.handler.release.set()

    assert reports >= 2, (
        f"a stall held far past the repeat interval was reported {reports} "
        "time(s); after the first report the log would read as though the "
        "freeze had ended"
    )
    # The hold spans at most four intervals; the ceiling is generous enough
    # that scheduling jitter cannot reach it, and far under the ~130 reports
    # a lost suppression would produce.
    assert reports <= 8, (
        f"the stall was reported {reports} times in 1.3s; the repeat "
        "suppression is not holding, so a freeze floods the log and the "
        "Windows notification handler"
    )


def test_a_stall_that_ends_is_reported_as_recovered(
    harness, monkeypatch, caplog,
):
    """A reported stall must be closed in the log, with the total hold.

    Without this line every stall reads as though it never ended, and a
    reader cannot tell a provider that eventually answered from one that
    never did.
    """
    _shorten_the_watchdog(monkeypatch)
    harness.handler.stall = {"start_utterance"}

    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "start_utterance", deadline=time.monotonic() + 3600.0,
            request_id="recovering-request-id",
        )
        assert harness.handler.entered_stall.wait(timeout=_WAIT_TIMEOUT_S)
        assert _wait_until(lambda: _stall_reports(caplog), _WAIT_TIMEOUT_S), (
            "the stall was never reported, so there is no recovery to observe"
        )
        harness.handler.release.set()

        def recovered():
            return any(
                "recovered" in record.getMessage().lower()
                and "start_utterance" in record.getMessage()
                for record in caplog.records
            )

        assert _wait_until(recovered, _WAIT_TIMEOUT_S), (
            "the provider finally answered and nothing said so; every stall "
            "in the log would read as though it never ended"
        )


def test_a_dispatch_that_was_never_reported_is_not_announced_as_recovered(
    harness, monkeypatch, caplog,
):
    """The recovery line belongs only to a stall that was actually reported.

    end() runs for every dispatch, so a recovery line that ignored whether a
    report happened would announce a recovery for ordinary work -- the same
    cry-wolf failure as reporting every command, moved one line down.
    """
    watchdog = harness.watchdog_thread()
    decisions = _record_the_dispatch_decision(monkeypatch)
    _shorten_the_watchdog(monkeypatch, report_s=5.0)
    harness.handler.stall = {"end_utterance"}

    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "end_utterance", deadline=time.monotonic() + 3600.0,
            request_id="quiet-request-id",
        )
        assert harness.handler.entered_stall.wait(timeout=_WAIT_TIMEOUT_S)
        # Outlasts the watchdog's first unshortened 0.25s sleep, so the
        # dispatch is genuinely observed in flight, and stays far under the
        # 5.0s limit. The same reasoning as the no-false-report test above.
        time.sleep(0.5)
        # .11.2.8 and .11.2.10: and the recorded read turns that reasoning
        # into an assertion, since a watchdog that never reached the dispatch
        # also announces no recovery.
        _assert_the_watchdog_read_the_dispatch(
            decisions,
            "the watchdog never read the dispatch while it was in flight, so "
            "the absence of a recovery line says nothing about the gate",
        )
        harness.handler.release.set()
        assert _wait_until(lambda: not harness.responses.empty(), _WAIT_TIMEOUT_S)
        time.sleep(0.2)

    assert not any(
        "recovered" in record.getMessage().lower() for record in caplog.records
    ), "an ordinary dispatch that was never reported was announced as recovered"
    # .11.2.6: a watchdog that died also announces nothing.
    assert watchdog.is_alive(), (
        "the watchdog thread ended, so the absence of a recovery line says "
        "nothing about the gate that is supposed to withhold it"
    )


def test_a_stall_report_cannot_be_overtaken_by_its_own_recovery_line(
    monkeypatch,
):
    """The two log lines of one dispatch must arrive in the order they mean.

    poll() decides the stall under the lock and then releases the lock before
    writing its line; end() takes the same lock, clears the dispatch and
    writes the recovery line. Nothing ordered the two writes against each
    other, so a scheduler pause between poll's lock release and its logger
    call let the recovery line land first. An operator then reads a recovery
    followed by a stall for the same action, with no later recovery -- the
    log's last word says the loop is wedged when the action has already
    returned, which misdirects worse than silence (codex round 3, finding
    .11.2.7).

    This drives _DispatchWatch on its own rather than through the loop,
    because the race is between two of its own methods and nothing else takes
    part. The logging shim holds poll() at exactly the post-lock boundary the
    defect lives at, so the interleaving is forced rather than waited for.
    """
    watch = input_proc._DispatchWatch()
    watch.begin("end_utterance", "rid-1", "trace-1", time.monotonic() - 60.0)

    written = []
    write_lock = threading.Lock()
    poll_reached_its_write = threading.Event()
    let_the_stall_line_through = threading.Event()

    def shim(message, *args, **kwargs):
        text = message % args if args else message
        line = "stalled" if "stalled" in text.lower() else "recovered"
        if line == "stalled":
            poll_reached_its_write.set()
            assert let_the_stall_line_through.wait(timeout=_WAIT_TIMEOUT_S)
        with write_lock:
            written.append(line)

    monkeypatch.setattr(input_proc.logger, "error", shim)

    poller = threading.Thread(target=watch.poll, name="test-poller")
    poller.start()
    assert poll_reached_its_write.wait(timeout=_WAIT_TIMEOUT_S), (
        "the watchdog never reached its stall line, so this test observed "
        "nothing about the order of the two writes"
    )

    ender = threading.Thread(target=watch.end, name="test-ender")
    ender.start()
    # Long enough for an unordered end() to run the whole way through its own
    # write. This interval is the window the defect used.
    time.sleep(0.2)
    let_the_stall_line_through.set()
    poller.join(timeout=_WAIT_TIMEOUT_S)
    ender.join(timeout=_WAIT_TIMEOUT_S)
    assert not poller.is_alive() and not ender.is_alive(), (
        "one of the two writers never finished; the recorded order below "
        "would be an artefact of that rather than of the ordering under test"
    )

    assert written == ["stalled", "recovered"], (
        "the recovery line for a dispatch was written before the stall line "
        "it answers; the log's last word about this action says it is still "
        "holding the single command loop"
    )


def test_the_expired_command_drop_is_written_to_the_log(harness, caplog):
    """The bead's acceptance is "logged AND reported"; this is the logged half.

    The response half is covered by
    test_the_expired_command_is_reported_and_not_dropped_in_silence, which
    reads the response queue and never looks at the log. Deleting the log
    record survived that test, so the drop could have become invisible to
    anyone reading the log after the fact.
    """
    harness.send("start_overlay_walk", deadline=time.monotonic() + 3600.0)
    assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)

    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "start_utterance", deadline=time.monotonic() - 1.0,
            request_id="logged-drop-request-id",
        )
        harness.handler.release.set()
        assert _wait_until(lambda: not harness.responses.empty(), _WAIT_TIMEOUT_S)

        def logged():
            return any(
                "Dropped expired command" in record.getMessage()
                and "start_utterance" in record.getMessage()
                for record in caplog.records
            )

        assert _wait_until(logged, _WAIT_TIMEOUT_S), (
            "an expired command was dropped with nothing written to the log; "
            "the drop is invisible to anyone reading the log afterwards"
        )


class _RaisingFloat(float):
    """A float subclass whose ordering comparison raises.

    ``isinstance`` passes for it, so a guard that only checks the type hands
    the object straight back, and the raise happens later, in whichever
    comparison the caller makes.
    """

    def __le__(self, other):
        raise RuntimeError("poisoned float compared")


# Codex round 1, finding .11.2.1. Every one of these IS a number, so the
# round-1 type check accepted all of them, and each still defeats the
# watchdog: 10 ** 400 is an int too large to convert to float, so
# math.isfinite raises on it; 1e308 and 1e20 are finite and positive but far
# above threading.TIMEOUT_MAX, which is 4294967.0 on this platform, so
# Event.wait raises when either is used as a timeout.
_OVERFLOWING_SECONDS = [10 ** 400, -(10 ** 400), 1e308, 1e20]


class _ExplodingFloat(float):
    """A float subclass whose conversion to a plain float raises.

    Nothing stops a subclass implementing __float__, and nothing says what it
    may raise. This one raises RuntimeError, which is neither OverflowError,
    ValueError nor TypeError -- the three the round-1 fix caught.
    """

    def __float__(self):
        raise RuntimeError("float conversion exploded")


class _ExplodingInt(int):
    """The same hazard on the int side of the isinstance check."""

    def __float__(self):
        raise RuntimeError("float conversion exploded")


@pytest.mark.parametrize("bad", [_ExplodingFloat(1.0), _ExplodingInt(1)])
def test_a_value_whose_conversion_raises_falls_back(bad):
    """Codex round 2, finding .11.2.3. The catch was too narrow.

    isinstance(value, (int, float)) admits subclasses, so the round-1 fix's
    float() call runs the subclass's own __float__. Catching three named
    exception types left every other one to escape the single function whose
    whole purpose is to choose the fallback -- and for the poll interval
    there is nothing further out to catch it.
    """
    assert input_proc._watchdog_seconds(bad, 6.0) == 6.0


@pytest.mark.parametrize("bad", _OVERFLOWING_SECONDS)
def test_a_numeric_value_the_platform_cannot_use_falls_back(bad):
    """A number is not automatically a usable number of seconds.

    The round-1 guard rejected the wrong SHAPES and let these through. They
    matter most for the poll interval: _run_dispatch_watchdog reads it and
    then waits on it, and BOTH of those sit outside the try that wraps poll,
    so an exception there ends the watchdog thread for the rest of the run
    instead of being logged and survived.
    """
    assert input_proc._watchdog_seconds(bad, 6.0) == 6.0


def test_the_ceiling_is_the_platforms_own_wait_limit():
    """The ceiling must track threading.TIMEOUT_MAX, not a number picked here.

    Event.wait raises above that limit, and the limit is a property of the
    platform rather than of this module, so a hard-coded ceiling would be
    wrong on the first machine whose limit differs.
    """
    assert input_proc._watchdog_seconds(threading.TIMEOUT_MAX, 6.0) == (
        threading.TIMEOUT_MAX
    )
    assert input_proc._watchdog_seconds(threading.TIMEOUT_MAX * 2, 6.0) == 6.0


def test_the_returned_interval_is_a_plain_float_in_the_usable_range():
    """What the helper hands back must be safe to compare and safe to wait on.

    Returning the caller's own object is what leaves the raise for later: a
    float subclass passes every isinstance check and can still raise from the
    comparison ``held_s < report_s`` or from the wait. Normalising to a plain
    float ends that whole class of problem at the guard, which is the only
    place equipped to fall back.
    """
    for value in (
        _RaisingFloat(1.0), 10 ** 400, 1e308, 0.25, 6, True, "0.25", None,
    ):
        seconds = input_proc._watchdog_seconds(value, 6.0)
        assert type(seconds) is float, (
            f"{value!r} was returned as {type(seconds).__name__}, so whatever "
            "it does on comparison is now the watchdog's problem"
        )
        assert 0 < seconds <= threading.TIMEOUT_MAX


def _assert_the_watchdog_survives(harness, monkeypatch, caplog, bad_interval):
    """Install *bad_interval* as the poll interval and require the thread to live.

    Shared by the two tests below because the observation is the same for
    both kinds of bad value: the read of the interval and the wait on it both
    sit AFTER the try that wraps poll(), so nothing contains an exception
    there and the watchdog thread ends for the rest of the run.
    """
    watchdog = harness.watchdog_thread()
    monkeypatch.setattr(input_proc, "_DISPATCH_STALL_REPORT_S", 0.05)
    monkeypatch.setattr(input_proc, "_DISPATCH_STALL_REPEAT_S", 0.05)
    monkeypatch.setattr(input_proc, "_WATCHDOG_POLL_S", bad_interval)

    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "start_overlay_walk", deadline=time.monotonic() + 3600.0,
            request_id="overflow-poll-request-id",
        )
        assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)
        # Three reports, not one. A watchdog that died of the bad interval
        # can still emit the single report from the poll that was already
        # under way when this test replaced the interval, so one report
        # proves nothing here. Only a thread that keeps waking up can
        # produce three of them a repeat interval apart.
        kept_reporting = _wait_until(
            lambda: len(_stall_reports(caplog)) >= 3, _WAIT_TIMEOUT_S,
        )
        harness.handler.release.set()

    assert kept_reporting, (
        "the watchdog thread did not survive an unusable poll interval, so "
        "the command loop was left unwatched in silence"
    )
    assert watchdog.is_alive(), (
        "the watchdog thread ended instead of falling back to a usable "
        "poll interval"
    )
    assert harness.loop_alive(), "the reader loop ended as well"


def test_a_poll_interval_the_platform_rejects_does_not_end_the_watchdog(
    harness, monkeypatch, caplog,
):
    """Codex round 1, finding .11.2.1: a number Event.wait cannot accept.

    1e308 is finite and positive and 22 orders of magnitude above
    threading.TIMEOUT_MAX, so the wait raises instead of waiting.
    """
    _assert_the_watchdog_survives(harness, monkeypatch, caplog, 1e308)


def test_a_poll_interval_that_cannot_be_converted_does_not_end_the_watchdog(
    harness, monkeypatch, caplog,
):
    """Codex round 2, finding .11.2.3: a subclass whose conversion raises.

    isinstance admits subclasses, so a float or int subclass reaches the
    float() call, and its __float__ can raise anything at all.
    """
    _assert_the_watchdog_survives(
        harness, monkeypatch, caplog, _ExplodingFloat(0.01),
    )


def test_the_harness_does_not_rebind_the_fault_handler_of_the_test_process(
    monkeypatch,
):
    """Codex round 1, finding .11.2.2. The loop is a thread here, not a process.

    input_process_main opens a crash-dump file and points the process-global
    fault handler at it. In the spawned production Input process that is
    right: the handler belongs to that process, and the process exits soon
    after. In this file the loop runs inside pytest's own interpreter, so the
    same call rebinds PYTEST's fault handler. The loop's own cleanup then
    closes that file (input_proc.py, the finally at the end of
    input_process_main), while faulthandler still holds its descriptor
    NUMBER -- which the next file opened in the run is free to be given. A
    native crash later in the suite would then write its traceback into an
    unrelated file.

    The recorder below stands in for the real function in both states, so this
    test never rebinds anything itself, whether it passes or fails.
    """
    calls = []
    monkeypatch.setattr(
        faulthandler, "enable", lambda *args, **kwargs: calls.append(kwargs),
    )
    harness = _Harness()
    harness.start()
    harness.stop()

    assert calls == [], (
        "the harness let the loop enable faulthandler in the test process; "
        "the crash-dump file it passed dies with the call and leaves the "
        f"handler on a reusable descriptor (call arguments: {calls})"
    )


class _StandInThread:
    """A thread-shaped object for testing the watchdog-thread filter.

    Real identifier reuse cannot be provoked on demand, so the test builds
    the collision directly.
    """

    def __init__(self, ident):
        self.name = _WATCHDOG_THREAD_NAME
        self.ident = ident

    def is_alive(self):
        return True


def test_the_watchdog_filter_survives_a_reused_thread_identifier(monkeypatch):
    """Codex round 2, finding .11.2.5.

    Python documents that a thread identifier may be reused once its thread
    has exited, and _Harness.stop() deliberately does not join the watchdog.
    So the sequence is reachable: a previous test's watchdog is alive when
    the next harness snapshots, exits while the new loop is starting, and the
    operating system hands its identifier to the new watchdog. A filter that
    compares identifiers then rejects the thread it is looking for, and the
    caller waits the full timeout and fails on a thread that is running
    correctly the whole time.
    """
    retired = _StandInThread(4242)
    replacement = _StandInThread(4242)
    assert retired.ident == replacement.ident, "the collision is the test"

    monkeypatch.setattr(threading, "enumerate", lambda: [replacement])
    assert _new_watchdog_thread({retired}) is replacement, (
        "the filter rejected a live watchdog because a retired thread had "
        "held the same identifier"
    )


def test_the_watchdog_filter_still_rejects_a_thread_it_was_given(monkeypatch):
    """The other half: the filter must not simply return anything alive.

    Without this, a filter that ignored its argument entirely would pass the
    test above.
    """
    already_running = _StandInThread(4242)
    monkeypatch.setattr(threading, "enumerate", lambda: [already_running])
    assert _new_watchdog_thread({already_running}) is None, (
        "the filter returned a watchdog thread that was already running "
        "before this harness started its own"
    )


def test_a_stall_that_shutdown_interrupts_is_still_reported_as_recovered(
    harness, monkeypatch, caplog,
):
    """Codex round 2, finding .11.2.4.

    end() runs at the TOP of the next loop turn, which is the placement that
    covers every dispatch branch with one call. The cost is that a shutdown
    arriving while a handler is blocked ends the loop before that turn ever
    happens, so the closing line is never written. The operator is then left
    with a stall record and nothing saying the provider came back, which is
    the reading the recovery line exists to prevent. The Launcher sets this
    same shutdown event when a peer process dies, so the sequence is an
    ordinary one rather than a contrived one.
    """
    _shorten_the_watchdog(monkeypatch)
    harness.handler.stall = {"start_utterance"}

    with caplog.at_level(logging.ERROR, logger=input_proc.logger.name):
        harness.send(
            "start_utterance", deadline=time.monotonic() + 3600.0,
            request_id="shutdown-during-stall",
        )
        assert harness.handler.entered_stall.wait(timeout=_WAIT_TIMEOUT_S)
        assert _wait_until(lambda: _stall_reports(caplog), _WAIT_TIMEOUT_S), (
            "the stall was never reported, so there is no recovery to observe"
        )
        # Shutdown while the handler is STILL inside the blocked call, which
        # is the whole sequence. Releasing it afterwards is the provider
        # finally answering.
        harness.shutdown.set()
        harness.handler.release.set()
        assert _wait_until(lambda: not harness.loop_alive(), _WAIT_TIMEOUT_S), (
            "the reader loop never finished after shutdown was set"
        )

    recoveries = [
        record for record in caplog.records
        if "recovered" in record.getMessage().lower()
    ]
    assert len(recoveries) == 1, (
        f"a reported stall that ended during shutdown produced "
        f"{len(recoveries)} recovery records; the log reads as though the "
        "provider never came back"
    )
    assert "start_utterance" in recoveries[0].getMessage()


# ---------------------------------------------------------------------------
# Continuous scrolling (wh-voice-access-parity.2.3.3)
#
# These use the same harness for the same reason the tests above do: each
# claim is about what the REAL reader loop does, and none of them can be shown
# with a mocked transport. The first is the loop staying free while a scroll
# runs. The second is the one place this feature touches the containment code
# -- a stop command dropped as expired must still stop the scroll. The third
# holds that special case to the stop alone.
# ---------------------------------------------------------------------------


class _ScrollingHandler(_FakeHandler):
    """A fake handler whose scroll actions drive a REAL repeating timer.

    Everything else behaves as _FakeHandler does, including the blocked walk.
    The timer is the production ``ContinuousScroller`` with a tick interval
    small enough that a test sees several turns in milliseconds, and a wheel
    call that counts instead of sending input.
    """

    # Small enough to finish fast, large enough that a scheduling hiccup does
    # not starve the loop this test is measuring.
    _TICK_S = 0.005

    def __init__(self):
        super().__init__()
        from ui.continuous_scroll import ContinuousScroller

        self._turns = 0
        self._turn_lock = threading.Lock()
        self.scroller = ContinuousScroller(
            self._turn_the_wheel,
            notifier=lambda title, message: None,
            tick_seconds=self._TICK_S,
            maximum_seconds=3600.0,
        )

    def _turn_the_wheel(self, direction, clicks):
        with self._turn_lock:
            self._turns += 1
        return (True, None)

    @property
    def turns(self):
        with self._turn_lock:
            return self._turns

    def start_continuous_scroll(self, direction=None, **kwargs):
        self._record("start_continuous_scroll")
        self.scroller.start(direction)

    def stop_continuous_scroll(self, **kwargs):
        self._record("stop_continuous_scroll")
        self.scroller.stop()


def test_the_command_loop_stays_free_while_a_continuous_scroll_runs():
    """Acceptance item 2: a discrete command runs while the timer ticks.

    The whole reason the timer is a thread is that the Input process has ONE
    command loop, and the stop word has to reach it. A start handler that
    scrolled in place would hold the loop for the entire scroll. This drives
    the real loop, starts a real repeating timer through it, and then requires
    the loop to dispatch and complete a second command while the timer is
    still turning the wheel.
    """
    harness = _Harness()
    harness.handler = _ScrollingHandler()
    harness.start()
    try:
        harness.send(
            "start_continuous_scroll", deadline=time.monotonic() + 3600.0,
            params={"direction": "down"},
        )
        assert _wait_until(
            lambda: harness.handler.turns >= 2, _WAIT_TIMEOUT_S,
        ), (
            "the repeating timer never turned the wheel twice, so there was "
            "no running scroll for the loop to stay free during"
        )

        harness.send("end_utterance", deadline=time.monotonic() + 3600.0)
        assert _wait_until(
            lambda: harness.handler.ran("end_utterance"), _WAIT_TIMEOUT_S,
        ), (
            "the Input command loop never dispatched a second command while a "
            "continuous scroll was running; the start handler is holding the "
            "loop, and the stop word could never reach it"
        )
        assert harness.handler.scroller.is_running(), (
            "the scroll had already ended, so the second command proves "
            "nothing about the loop staying free DURING a scroll"
        )
    finally:
        harness.handler.scroller.stop()
        harness.stop()


def test_a_stop_command_dropped_as_expired_still_stops_the_scroll():
    """Acceptance item 3, the one piece that touches the containment code.

    The containment drops a command whose delivery deadline passed while the
    loop was blocked. That is right for every command that acts on a screen
    which has since changed -- and wrong for a stop. A dropped stop leaves the
    wheel turning with the user's only stop word already spent. Stopping is
    the same act whenever it happens, so an expired stop still stops, and it
    still reports the expiry so the drop is not silent.

    This drives a REAL ``ContinuousScroller`` rather than the recording fake,
    and it builds its own harness to do so, because the shared fixture wires
    up ``_FakeHandler`` before the loop starts. The claim is that a turning
    wheel STOPS, and a handler that only writes down the name of the call it
    received cannot show that: the test would pass unchanged if the stop
    reached a handler that did nothing with it
    (wh-voice-access-parity.2.3.3.2.2).
    """
    harness = _Harness()
    harness.handler = _ScrollingHandler()
    harness.start()
    try:
        harness.send(
            "start_continuous_scroll", deadline=time.monotonic() + 3600.0,
            params={"direction": "down"},
        )
        assert _wait_until(
            lambda: harness.handler.turns >= 2, _WAIT_TIMEOUT_S,
        ), (
            "the repeating timer never turned the wheel twice, so there was "
            "no running scroll for the expired stop to stop"
        )

        harness.send("start_overlay_walk", deadline=time.monotonic() + 3600.0)
        assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S), (
            "the loop never dequeued and dispatched the blocking walk, so the "
            "rest of this test would prove nothing"
        )

        # Delivered while the loop is held, with a deadline that has already
        # passed -- exactly the sequence the containment drops.
        harness.send(
            "stop_continuous_scroll", deadline=time.monotonic() - 1.0,
            request_id="expired-stop",
        )
        harness.handler.release.set()

        assert _wait_until(
            lambda: not harness.handler.scroller.is_running(), _WAIT_TIMEOUT_S,
        ), (
            "the expired stop never stopped the running scroll; the wheel "
            "would keep turning after the user had already said the stop word"
        )

        # stop() joins the timer thread, so by here nothing should be turning
        # the wheel. Count the turns, wait several tick intervals, and require
        # the count to hold: a scroller that reported itself stopped while its
        # thread kept sending wheel input would leave the user in exactly the
        # position this whole special case exists to prevent.
        settled = harness.handler.turns
        time.sleep(_ScrollingHandler._TICK_S * 20)
        assert harness.handler.turns == settled, (
            f"the wheel kept turning after the scroll reported itself "
            f"stopped: {settled} turns became {harness.handler.turns}"
        )

        assert _wait_until(
            lambda: not harness.responses.empty(), _WAIT_TIMEOUT_S,
        ), (
            "the expired stop produced no response at all; the drop must "
            "still be reported, not made silent by the stop being honoured"
        )
        message = harness.responses.get_nowait()
        assert message["request_id"] == "expired-stop"
        assert message.get("error") is True, (
            f"an expired command must still be answered as an error, got {message!r}"
        )
        assert message.get("action") == "stop_continuous_scroll"
    finally:
        harness.handler.scroller.stop()
        harness.stop()


def test_an_expired_start_scroll_command_is_still_dropped(harness):
    """The stop is the special case, and only the stop.

    A start that went stale while the loop was blocked must NOT begin
    scrolling late. Without this, the special case above could be written as
    "run any expired scroll command", which would start a scroll the user
    asked for long enough ago that the Logic process gave up on it.
    """
    harness.send("start_overlay_walk", deadline=time.monotonic() + 3600.0)
    assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S)

    harness.send(
        "start_continuous_scroll", deadline=time.monotonic() - 1.0,
        params={"direction": "down"},
    )
    harness.handler.release.set()

    assert _wait_until(lambda: not harness.command_ready.is_set(), _WAIT_TIMEOUT_S)
    # Room to do the wrong thing, so a pass means a real refusal.
    time.sleep(0.2)
    assert not harness.handler.ran("start_continuous_scroll"), (
        "an expired start command began a scroll the Logic process had "
        "already given up on"
    )

    harness.send("end_utterance", deadline=time.monotonic() + 3600.0)
    assert _wait_until(
        lambda: harness.handler.ran("end_utterance"), _WAIT_TIMEOUT_S,
    ), (
        "the loop never ran a fresh command after dropping the expired start, "
        "so the drop cannot be told apart from a dead reader loop"
    )
    assert harness.loop_alive(), "the reader loop ended after dropping a command"


# ---------------------------------------------------------------------------
# THIRD HALF: shutdown reaches the continuous scroll even while the loop is
# blocked (wh-voice-access-parity.2.3.3.2.10).
# ---------------------------------------------------------------------------


def test_the_loop_hands_its_shutdown_signal_to_the_handler(harness):
    """The wiring, asserted where it is actually done.

    The scroller can only watch the signal if the loop passes it down, and
    that handoff is one keyword argument in one call. A test of the scroller
    alone would keep passing after someone removed it.
    """
    assert harness.handler_class.call_count == 1, (
        "the loop did not build the handler exactly once: "
        f"{harness.handler_class.call_args_list}"
    )
    _, kwargs = harness.handler_class.call_args
    assert kwargs.get("shutdown_event") is harness.shutdown, (
        "the loop built the handler without its own shutdown signal, so the "
        "continuous scroll cannot see a shutdown: "
        f"{kwargs!r}"
    )


def test_a_blocked_loop_does_not_stop_a_scroll_from_ending_at_shutdown(harness):
    """The failure the finding describes, end to end.

    The scroller here is REAL and it reads the harness's own shutdown event --
    the same object the loop was given. The loop is genuinely blocked inside a
    handler for the whole assertion, which is what makes the test worth
    running: it proves the timer stops itself, not that some other part of the
    process stopped it. The loop cannot have helped, because it is still
    inside start_overlay_walk when the wheel stops, and its cleanup has not
    run.
    """
    from ui.continuous_scroll import ContinuousScroller

    turns = []
    turns_lock = threading.Lock()

    def wheel(direction, clicks):
        with turns_lock:
            turns.append((direction, clicks))
        return (True, None)

    def count():
        with turns_lock:
            return len(turns)

    notices = []
    scroller = ContinuousScroller(
        wheel,
        lambda title, message: notices.append((title, message)),
        tick_seconds=0.002,
        maximum_seconds=3600.0,
        shutdown_event=harness.shutdown,
    )
    try:
        assert scroller.start("down") is True
        assert _wait_until(lambda: count() >= 2, _WAIT_TIMEOUT_S), (
            "the scroll never started, so this test proves nothing"
        )

        # Block the single reader loop, the way a provider that never answers
        # blocks it in production.
        harness.send("start_overlay_walk", deadline=time.monotonic() + 3600.0)
        assert harness.handler.entered_walk.wait(timeout=_WAIT_TIMEOUT_S), (
            "the loop never entered the blocking handler"
        )

        harness.shutdown.set()

        assert _wait_until(lambda: not scroller.is_running(), _WAIT_TIMEOUT_S), (
            "the scroll was still running after shutdown was signalled while "
            "the loop was blocked -- the wheel would keep turning for the "
            "launcher's whole grace period"
        )
        settled = count()
        # The block is STILL held here. If the loop had somehow ended the
        # scroll, it could only have done so by leaving the handler.
        assert not harness.handler.release.is_set(), (
            "the test released the block itself; the assertion above would "
            "then prove nothing about a blocked loop"
        )
        assert harness.loop_alive(), (
            "the reader loop ended while its handler was still blocked, so "
            "the scroll could have been stopped by the loop's cleanup"
        )
        time.sleep(0.2)
        assert count() == settled, (
            "the wheel turned again after shutdown was signalled: "
            f"{settled} calls at the stop, {count()} after"
        )
        assert notices == [], (
            f"the shutdown end showed the user a notice: {notices}"
        )
    finally:
        scroller.stop()
