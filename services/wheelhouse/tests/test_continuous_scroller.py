"""The Input-side repeating wheel timer (wh-voice-access-parity.2.3.3).

``ui/continuous_scroll.py`` holds the one timer that turns the mouse wheel
over and over after "start scrolling down", until "stop scrolling" arrives or
the time limit ends it.

WHY THE TIMER IS A THREAD AND NOT A LOOP INSIDE THE HANDLER. The Input
process runs ONE command loop. A handler that scrolled in a loop would hold
that loop for as long as the scroll lasted, and the stop word arrives through
the same loop -- the scroll could never be stopped by voice. So the start
handler starts a thread and returns, and the loop is free again immediately.
The test ``test_start_returns_while_the_wheel_call_is_still_running`` measures
exactly that.

WHAT THE TESTS INJECT. The wheel call itself is a seam, so no test sends real
input. The clock is a seam too, so the auto-stop test can move time forward by
minutes without waiting for them. The tick interval is a constructor argument,
so the tests use a tiny one and finish in milliseconds.

THE ONE-AT-A-TIME RULE. A second start must replace the first, and after the
switch no wheel event may carry the old direction. The switch stops the old
run and waits for its thread, so the old direction cannot arrive late.
"""
import logging
import sys
import threading
import time
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from ui.continuous_scroll import (
    AUTO_STOP_MESSAGE,
    ContinuousScroller,
    NOTICE_TITLE,
    REFUSALS_BEFORE_STOPPING,
    START_FAILED_MESSAGE,
)

# Long enough that a scheduling hiccup cannot fail a test, short enough that a
# genuinely stuck timer fails fast.
_WAIT_TIMEOUT_S = 10.0

# The tick interval every test uses. Small so a test that waits for several
# ticks finishes in milliseconds.
_FAST_TICK_S = 0.002


def _wait_until(condition, timeout_s=_WAIT_TIMEOUT_S):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.001)
    return condition()


class _RecordingSeam:
    """Stands in for utils.win_input_sender.scroll_wheel."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = []

    def __call__(self, direction, clicks):
        with self._lock:
            self.calls.append((direction, clicks))
        return (True, None)

    @property
    def count(self):
        with self._lock:
            return len(self.calls)

    def directions(self):
        with self._lock:
            return [direction for direction, _ in self.calls]


class _RecordingNotifier:
    def __init__(self):
        self._lock = threading.Lock()
        self.notices = []

    def __call__(self, title, message):
        with self._lock:
            self.notices.append((title, message))

    @property
    def count(self):
        with self._lock:
            return len(self.notices)


class _CountingNotifierWorker:
    """Stands in for utils.notifier_worker.NotifierWorker.

    ErrorNotificationHandler submits a payload here for every ERROR record
    it accepts, which is exactly the generic notification the user would
    see beside the purpose-written one.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.payloads = []

    def submit(self, payload):
        with self._lock:
            self.payloads.append(payload)
        return True

    @property
    def count(self):
        with self._lock:
            return len(self.payloads)

    def messages(self):
        with self._lock:
            return [payload.message for payload in self.payloads]


class _FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self):
        self._lock = threading.Lock()
        self._now = 1000.0

    def __call__(self):
        with self._lock:
            return self._now

    def advance(self, seconds):
        with self._lock:
            self._now += seconds


@pytest.fixture
def seam():
    return _RecordingSeam()


@pytest.fixture
def notifier():
    return _RecordingNotifier()


@pytest.fixture
def error_toasts():
    """The real ErrorNotificationHandler, listening on the root logger.

    input_proc.py calls setup_logging, and setup_logging attaches this
    handler to the root logger, so this fixture reproduces what the Input
    process actually does. It yields the stand-in worker the handler
    submits to, so a test can count the generic notifications an ERROR
    record would show. The rate limit is set to zero seconds on purpose:
    a test must not pass because a limit hid the second notification.
    """
    from utils.error_notifier import ErrorNotificationHandler

    worker = _CountingNotifierWorker()
    handler = ErrorNotificationHandler(
        rate_limit_seconds=0, notifier_worker=worker,
    )
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield worker
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


@pytest.fixture
def scroller(seam, notifier):
    made = ContinuousScroller(
        seam, notifier, tick_seconds=_FAST_TICK_S, maximum_seconds=3600.0,
    )
    try:
        yield made
    finally:
        made.stop()


class TestStartAndStop:
    @pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
    def test_starting_turns_the_wheel_over_and_over(
        self, scroller, seam, direction
    ):
        assert scroller.start(direction) is True
        assert _wait_until(lambda: seam.count >= 3), (
            "the timer sent fewer than three wheel turns; a continuous scroll "
            "must keep going with no further speech"
        )
        assert set(seam.directions()) == {direction}

    def test_every_turn_carries_the_configured_notch_count(self, seam, notifier):
        scroller = ContinuousScroller(
            seam, notifier, notches_per_tick=4, tick_seconds=_FAST_TICK_S,
        )
        try:
            scroller.start("down")
            assert _wait_until(lambda: seam.count >= 2)
        finally:
            scroller.stop()
        assert {clicks for _, clicks in seam.calls} == {4}

    def test_stopping_ends_the_turns(self, scroller, seam):
        scroller.start("down")
        assert _wait_until(lambda: seam.count >= 2)

        assert scroller.stop() is True
        settled = seam.count
        # One tick interval is what the acceptance allows; this waits far
        # longer, so a timer that kept going is certain to be seen.
        time.sleep(_FAST_TICK_S * 50)
        assert seam.count == settled, (
            "the wheel kept turning after the stop command"
        )
        assert scroller.is_running() is False

    def test_stopping_a_scroll_that_never_started_is_harmless(self, scroller):
        """A user who says the stop words twice must not be refused."""
        assert scroller.stop() is False
        assert scroller.stop() is False
        assert scroller.is_running() is False

    def test_is_running_reports_the_state(self, scroller, seam):
        assert scroller.is_running() is False
        scroller.start("up")
        assert scroller.is_running() is True
        scroller.stop()
        assert scroller.is_running() is False

    @pytest.mark.parametrize("direction", ["sideways", "", None, 7])
    def test_an_unusable_direction_starts_nothing(
        self, scroller, seam, direction
    ):
        assert scroller.start(direction) is False
        time.sleep(_FAST_TICK_S * 20)
        assert seam.count == 0
        assert scroller.is_running() is False


class TestTheCommandLoopStaysFree:
    """Acceptance item 2, measured at this level.

    The start call must return while the wheel call it began is still
    running. If it did not, the Input command loop would be held for the
    whole scroll and the stop word could never reach it.
    """

    def test_start_returns_while_the_wheel_call_is_still_running(self, notifier):
        inside_the_seam = threading.Event()
        release = threading.Event()

        def blocking_seam(direction, clicks):
            inside_the_seam.set()
            release.wait(timeout=_WAIT_TIMEOUT_S)
            return (True, None)

        scroller = ContinuousScroller(
            blocking_seam, notifier, tick_seconds=_FAST_TICK_S,
        )
        try:
            scroller.start("down")
            assert inside_the_seam.wait(timeout=_WAIT_TIMEOUT_S), (
                "the timer never reached the wheel call, so this test would "
                "prove nothing about the start call returning early"
            )
            # Reaching this line at all is the measurement: start() returned
            # while the wheel call above is still blocked.
            assert scroller.is_running() is True
        finally:
            release.set()
            scroller.stop()


class TestOneScrollAtATime:
    """Acceptance item 5."""

    def test_a_second_start_replaces_the_first(self, scroller, seam):
        scroller.start("down")
        assert _wait_until(lambda: seam.count >= 2)

        scroller.start("up")
        seen_after_switch = seam.count
        assert _wait_until(lambda: seam.count >= seen_after_switch + 3)

        after = seam.directions()[seen_after_switch:]
        assert set(after) == {"up"}, (
            f"the old direction was still being sent after the switch: {after}"
        )

    def test_the_switch_leaves_exactly_one_timer_thread(self, scroller, seam):
        def scroller_threads():
            return [
                thread for thread in threading.enumerate()
                if thread.name == ContinuousScroller.THREAD_NAME
                and thread.is_alive()
            ]

        scroller.start("down")
        assert _wait_until(lambda: seam.count >= 2)
        scroller.start("up")
        assert _wait_until(lambda: len(scroller_threads()) == 1), (
            f"expected one timer thread after the switch, found "
            f"{len(scroller_threads())}"
        )

    def test_the_timer_thread_dies_with_the_process(self, scroller, seam):
        """A daemon thread cannot keep a shutting-down process alive."""
        scroller.start("down")
        assert _wait_until(lambda: seam.count >= 1)
        alive = [
            thread for thread in threading.enumerate()
            if thread.name == ContinuousScroller.THREAD_NAME and thread.is_alive()
        ]
        assert alive, "the timer thread was never started"
        assert all(thread.daemon for thread in alive)


class TestAutoStop:
    """Acceptance item 4: a missed stop word cannot scroll forever."""

    def test_the_scroll_ends_at_the_maximum_duration(self, seam, notifier):
        clock = _FakeClock()
        scroller = ContinuousScroller(
            seam, notifier, tick_seconds=_FAST_TICK_S, maximum_seconds=30.0,
            monotonic=clock,
        )
        try:
            scroller.start("down")
            assert _wait_until(lambda: seam.count >= 2)

            clock.advance(30.0)
            assert _wait_until(lambda: not scroller.is_running()), (
                "the scroll was still running past its maximum duration"
            )
            settled = seam.count
            time.sleep(_FAST_TICK_S * 50)
            assert seam.count == settled, (
                "the wheel kept turning after the maximum duration"
            )
        finally:
            scroller.stop()

    def test_the_auto_stop_tells_the_user(self, seam, notifier):
        clock = _FakeClock()
        scroller = ContinuousScroller(
            seam, notifier, tick_seconds=_FAST_TICK_S, maximum_seconds=30.0,
            monotonic=clock,
        )
        try:
            scroller.start("down")
            assert _wait_until(lambda: seam.count >= 2)
            clock.advance(30.0)
            assert _wait_until(lambda: notifier.count >= 1), (
                "the scroll stopped itself and said nothing; a user who "
                "cannot see the screen would not know why it stopped"
            )
        finally:
            scroller.stop()
        title, message = notifier.notices[0]
        assert title == NOTICE_TITLE
        assert message == AUTO_STOP_MESSAGE

    def test_a_spoken_stop_says_nothing(self, scroller, seam, notifier):
        """The user who said the words already knows the scroll stopped."""
        scroller.start("down")
        assert _wait_until(lambda: seam.count >= 2)
        scroller.stop()
        time.sleep(_FAST_TICK_S * 20)
        assert notifier.count == 0


class TestABrokenWheelCall:
    def test_a_raising_wheel_call_ends_the_scroll_instead_of_spinning(
        self, notifier
    ):
        """A seam that raises every tick would otherwise log forever."""
        calls = []

        def raising_seam(direction, clicks):
            calls.append(direction)
            raise RuntimeError("the wheel call failed")

        scroller = ContinuousScroller(
            raising_seam, notifier, tick_seconds=_FAST_TICK_S,
        )
        try:
            scroller.start("down")
            assert _wait_until(lambda: not scroller.is_running()), (
                "the timer kept running after the wheel call raised"
            )
            assert len(calls) == 1
        finally:
            scroller.stop()

    def test_a_refused_wheel_call_ends_the_scroll(self, notifier):
        """A refused wheel call RETURNS its failure; it does not raise.

        ``utils.win_input_sender.scroll_wheel`` reports ``(False,
        "sendinput_short")`` when SendInput accepts fewer events than asked,
        which is what Windows does while input injection is blocked -- against
        an elevated foreground window, on the secure desktop, or when the
        input queue is full. A tick loop that discarded that result would keep
        "scrolling" for the whole maximum duration without moving the wheel
        once (wh-voice-access-parity.2.3.3.1.2).
        """
        calls = []

        def refusing_seam(direction, clicks):
            calls.append(direction)
            return (False, "sendinput_short")

        scroller = ContinuousScroller(
            refusing_seam, notifier, tick_seconds=_FAST_TICK_S,
        )
        try:
            scroller.start("down")
            assert _wait_until(lambda: not scroller.is_running()), (
                "the timer kept running after every wheel call was refused"
            )
        finally:
            scroller.stop()
        assert len(calls) == REFUSALS_BEFORE_STOPPING, (
            "a refused scroll must end after a small fixed number of "
            f"refusals, not keep trying; it made {len(calls)} calls"
        )

    def test_a_refused_scroll_tells_the_user(self, notifier):
        """And it must not claim the two-minute limit stopped it."""

        def refusing_seam(direction, clicks):
            return (False, "sendinput_short")

        scroller = ContinuousScroller(
            refusing_seam, notifier, tick_seconds=_FAST_TICK_S,
        )
        try:
            scroller.start("down")
            assert _wait_until(lambda: notifier.count >= 1), (
                "the scroll stopped because the wheel never turned and said "
                "nothing; nothing moved on screen, so a user who cannot see "
                "it has no other sign of what happened"
            )
        finally:
            scroller.stop()
        title, message = notifier.notices[0]
        assert title == NOTICE_TITLE
        assert message == "Scrolling failed"
        assert message != AUTO_STOP_MESSAGE, (
            "a scroll that never moved the wheel must not report that it "
            "scrolled for two minutes"
        )

    def test_one_refused_tick_does_not_end_the_scroll(self, notifier):
        """A momentarily full input queue must not end a scroll."""
        lock = threading.Lock()
        calls = []

        def sometimes_refusing_seam(direction, clicks):
            with lock:
                calls.append(direction)
                first = len(calls) == 1
            if first:
                return (False, "sendinput_short")
            return (True, None)

        scroller = ContinuousScroller(
            sometimes_refusing_seam, notifier, tick_seconds=_FAST_TICK_S,
        )
        try:
            scroller.start("down")
            assert _wait_until(
                lambda: len(calls) > REFUSALS_BEFORE_STOPPING + 2
            ), "the scroll ended after a single refused tick"
            assert scroller.is_running()
            assert notifier.count == 0
        finally:
            scroller.stop()

    def test_a_seam_that_reports_nothing_keeps_scrolling(self, notifier):
        """Only the documented failure shape ends a scroll.

        The seam is injectable, and a caller that returns None -- or anything
        that is not a ``(succeeded, reason)`` pair -- has not reported a
        failure. Reading an unknown shape as failure would end every scroll
        such a seam ran.
        """
        calls = []

        def silent_seam(direction, clicks):
            calls.append(direction)
            return None

        scroller = ContinuousScroller(
            silent_seam, notifier, tick_seconds=_FAST_TICK_S,
        )
        try:
            scroller.start("down")
            assert _wait_until(
                lambda: len(calls) > REFUSALS_BEFORE_STOPPING + 2
            ), "a seam that returned None was read as a refusal"
            assert scroller.is_running()
        finally:
            scroller.stop()


class TestAStopTheUserAskedForStaysSilent:
    """A stop the user asked for says nothing, even mid-tick.

    Every automatic end runs on the timer thread. That thread can be inside a
    wheel call, or inside the clock reading, at the moment the user's stop
    arrives on the Input command loop. The stop takes the run and signals it,
    but the timer thread is already past its own check of that signal, so it
    goes on to report an end the user did not ask for. The user then hears
    "the wheel would not turn", or "two minutes are up", for a scroll they
    stopped themselves (wh-voice-access-parity.2.3.3.2.5).

    Each test here blocks the timer thread at one such point, stops the
    scroll from another thread, and then releases it. The stop must run on
    another thread because ``stop`` waits for the timer thread it is ending.

    This is a different case from the crewcut: comment in ``_stop_and_say``.
    That one is about a notice the user SHOULD get, arriving late enough to
    read as a statement about a replacement scroll. These are notices the
    user should never get at all.
    """

    def _timer_thread(self, before):
        """This test's own timer thread: the one that was not there before.

        Finding it by name alone would find any timer thread in the process,
        and a test earlier in the same run can leave one going -- a mutation
        that stops a scroll from being replaced leaves the first one turning
        the wheel for its whole maximum duration. Waiting on that thread
        would fail this test for another test's mutation
        (wh-voice-access-parity.2.3.3.2.6).
        """
        started = [
            thread for thread in threading.enumerate()
            if thread.name == ContinuousScroller.THREAD_NAME
            and thread not in before
        ]
        assert len(started) == 1, (
            f"expected exactly one new timer thread, found {len(started)}"
        )
        return started[0]

    def _stop_from_another_thread(self, scroller):
        """Start the stop, and wait until it has taken the run.

        The stop needs its own thread because ``stop`` waits for the timer
        thread it is ending, and this test is what releases that thread.
        """
        stopper = threading.Thread(target=scroller.stop, name="test-stopper")
        stopper.start()
        assert _wait_until(lambda: not scroller.is_running()), (
            "the stop never took the run away from the timer"
        )
        return stopper

    def _settle(self, stopper, timer):
        """Wait for the stop and this test's timer thread to both finish."""
        stopper.join(_WAIT_TIMEOUT_S)
        assert not stopper.is_alive(), "the stop never returned"
        timer.join(_WAIT_TIMEOUT_S)
        assert not timer.is_alive(), "the timer thread outlived the stop"

    def test_a_stop_during_the_last_refused_tick_says_nothing(self, notifier):
        """The refusal counter reaches its limit after the user stopped."""
        lock = threading.Lock()
        calls = []
        reached_the_last = threading.Event()
        released = threading.Event()

        def refusing_seam(direction, clicks):
            with lock:
                calls.append(direction)
                nth = len(calls)
            if nth == REFUSALS_BEFORE_STOPPING:
                reached_the_last.set()
                released.wait(_WAIT_TIMEOUT_S)
            return (False, "sendinput_short")

        before = set(threading.enumerate())
        scroller = ContinuousScroller(
            refusing_seam, notifier, tick_seconds=_FAST_TICK_S,
        )
        try:
            scroller.start("down")
            assert reached_the_last.wait(_WAIT_TIMEOUT_S), (
                "the seam never reached the refusal that ends a scroll"
            )
            timer = self._timer_thread(before)
            stopper = self._stop_from_another_thread(scroller)
            released.set()
            self._settle(stopper, timer)
        finally:
            released.set()
            scroller.stop()

        assert notifier.notices == [], (
            "the user said the stop word and was told the wheel would not "
            f"turn: {notifier.notices}"
        )

    def test_a_stop_at_the_maximum_duration_says_nothing(self, seam, notifier):
        """The time limit is reached after the user stopped."""
        clock = _FakeClock()
        lock = threading.Lock()
        readings = []
        reached_the_check = threading.Event()
        released = threading.Event()

        def blocking_clock():
            with lock:
                readings.append(1)
                nth = len(readings)
            # Reading one is the start time. Reading two is the timer thread
            # comparing the elapsed time against the maximum, just after it
            # checked its own stop signal.
            if nth == 2:
                reached_the_check.set()
                released.wait(_WAIT_TIMEOUT_S)
                clock.advance(10_000.0)
            return clock()

        before = set(threading.enumerate())
        scroller = ContinuousScroller(
            seam, notifier, tick_seconds=_FAST_TICK_S,
            maximum_seconds=120.0, monotonic=blocking_clock,
        )
        try:
            scroller.start("down")
            assert reached_the_check.wait(_WAIT_TIMEOUT_S), (
                "the timer never reached its duration check"
            )
            timer = self._timer_thread(before)
            stopper = self._stop_from_another_thread(scroller)
            released.set()
            self._settle(stopper, timer)
        finally:
            released.set()
            scroller.stop()

        assert notifier.notices == [], (
            "the user said the stop word and was told the scroll ran for "
            f"its full time: {notifier.notices}"
        )

    def test_a_stop_while_the_wheel_call_raises_says_nothing(self, notifier):
        """The wheel call raises after the user stopped."""
        reached_the_call = threading.Event()
        released = threading.Event()

        def raising_seam(direction, clicks):
            reached_the_call.set()
            released.wait(_WAIT_TIMEOUT_S)
            raise RuntimeError("the wheel call failed")

        before = set(threading.enumerate())
        scroller = ContinuousScroller(
            raising_seam, notifier, tick_seconds=_FAST_TICK_S,
        )
        try:
            scroller.start("down")
            assert reached_the_call.wait(_WAIT_TIMEOUT_S), (
                "the seam was never called"
            )
            timer = self._timer_thread(before)
            stopper = self._stop_from_another_thread(scroller)
            released.set()
            self._settle(stopper, timer)
        finally:
            released.set()
            scroller.stop()

        assert notifier.notices == [], (
            "the user said the stop word and was told the wheel would not "
            f"turn: {notifier.notices}"
        )


class TestAThreadTheMachineWillNotStart:
    """A machine out of threads must not leave the scroller unable to start.

    threading.Thread.start raises RuntimeError when the operating system
    refuses a new thread, and start() installs the run in self._run BEFORE it
    starts the thread. Without a rollback the scroller keeps a run whose
    thread never ran: is_running() answers True, nothing turns the wheel, and
    the NEXT command reaches _end_current_run, which joins a thread that was
    never started and raises again. The handler swallows both, so the user
    gets two accepted commands and no scrolling
    (wh-voice-access-parity.2.3.3.2.8).

    The refusal is applied to threading.Thread.start itself rather than to a
    substitute object, because the failure this guards against is the real
    one: a real Thread whose real start fails. It is filtered by the timer
    thread's name and refuses once, so no other thread in the test process is
    affected.
    """

    @staticmethod
    def _refuse_the_next_timer_thread(monkeypatch):
        real_start = threading.Thread.start
        refused = []

        def refusing_start(self):
            if not refused and self.name == ContinuousScroller.THREAD_NAME:
                refused.append(self)
                raise RuntimeError("can't start new thread")
            return real_start(self)

        monkeypatch.setattr(threading.Thread, "start", refusing_start)
        return refused

    def test_a_refused_thread_start_leaves_nothing_running(
        self, scroller, seam, monkeypatch,
    ):
        refused = self._refuse_the_next_timer_thread(monkeypatch)

        started = scroller.start("down")

        assert refused, "the test never reached the timer thread's start"
        assert started is False, (
            "start() answered True for a scroll that never began"
        )
        assert scroller.is_running() is False, (
            "the scroller still reports a running scroll after the operating "
            "system refused its thread"
        )
        assert seam.count == 0, (
            f"a scroll that never started turned the wheel: {seam.calls}"
        )

    def test_the_next_start_works_after_a_refused_thread_start(
        self, scroller, seam, monkeypatch,
    ):
        self._refuse_the_next_timer_thread(monkeypatch)
        scroller.start("down")

        started = scroller.start("up")

        assert started is True, (
            "the start after a refused one was refused as well; the run left "
            "behind by the first is poisoning the second"
        )
        assert _wait_until(lambda: seam.count >= 1), (
            "the second start never turned the wheel"
        )
        assert seam.directions()[0] == "up"

    def test_a_refused_thread_start_tells_the_user(
        self, scroller, notifier, monkeypatch,
    ):
        self._refuse_the_next_timer_thread(monkeypatch)

        scroller.start("down")

        assert notifier.notices == [(NOTICE_TITLE, START_FAILED_MESSAGE)], (
            "the user asked for a scroll, got none, and was told nothing: "
            f"{notifier.notices}"
        )

    def test_a_stop_in_the_gap_before_the_thread_runs_does_not_raise(
        self, scroller, monkeypatch,
    ):
        """The stop that lands between installing the run and starting it.

        start() installs the run under the lock, RELEASES the lock, then
        starts the thread. A stop arriving in that gap takes the run and
        reaches the join with a thread that has not been started, which
        raises RuntimeError("cannot join thread before it is started").

        The gap is reproduced exactly rather than approximately: the patched
        start calls stop() itself, at the one instant the real gap exists,
        and then delegates to the real start. There is no deadlock, because
        start() is outside the lock by then.
        """
        real_start = threading.Thread.start
        stopped = []

        def stopping_start(self):
            if not stopped and self.name == ContinuousScroller.THREAD_NAME:
                stopped.append(scroller.stop())
            return real_start(self)

        monkeypatch.setattr(threading.Thread, "start", stopping_start)

        started = scroller.start("down")

        assert stopped, "the test never reached the timer thread's start"
        assert started is True, (
            "start() reported a failure for a thread that did start"
        )
        assert scroller.is_running() is False, (
            "the stop in the gap took the run, so nothing should be running"
        )


class TestOneNotificationPerFailure:
    """An automatic failure must show ONE notification, not two.

    input_proc.py calls setup_logging before it builds the UIActionHandler,
    and setup_logging attaches ErrorNotificationHandler to the ROOT logger
    (utils/logging_setup.py). That handler turns every ERROR record into a
    Windows notification through the same NotifierWorker send_notice uses.
    This module's logger propagates to root and sets no level of its own,
    so an ERROR record here reaches the user as a generic
    "[ERROR] continuous_scroll" box, and _stop_and_say then sends the
    purpose-written one. Two boxes appear for one failure, and a screen
    reader reads the generic one first
    (wh-voice-access-parity.2.3.3.2.9).

    The three automatic ends go through the REAL ErrorNotificationHandler
    rather than through a level check, because the property being pinned is
    what that handler does with the record. A test that only read the
    record's level would keep passing if the handler later listened at
    WARNING as well.

    A spoken stop is not tested here. It writes no record at ERROR and
    sends no notice at all, which TestAStopTheUserAskedForStaysSilent
    already covers.
    """

    def test_a_refused_thread_start_shows_one_notification(
        self, scroller, notifier, error_toasts, monkeypatch,
    ):
        # The refusal helper belongs to the class that introduced this
        # failure path; calling it here keeps one definition of what a
        # refused timer thread looks like.
        TestAThreadTheMachineWillNotStart._refuse_the_next_timer_thread(
            monkeypatch
        )

        scroller.start("down")

        assert notifier.notices == [(NOTICE_TITLE, START_FAILED_MESSAGE)], (
            "the purpose-written notice did not go out: "
            f"{notifier.notices}"
        )
        assert error_toasts.count == 0, (
            "a refused timer thread showed a second, generic notification "
            "beside the written one; a screen reader reads this one first: "
            f"{error_toasts.messages()}"
        )

    def test_a_raising_wheel_call_shows_one_notification(
        self, notifier, error_toasts,
    ):
        def raising_seam(direction, clicks):
            raise RuntimeError("the wheel call failed")

        scroller = ContinuousScroller(
            raising_seam, notifier, tick_seconds=_FAST_TICK_S,
        )
        try:
            scroller.start("down")
            assert _wait_until(lambda: notifier.count >= 1), (
                "the scroll ended without telling the user anything"
            )
        finally:
            scroller.stop()

        assert notifier.notices == [(NOTICE_TITLE, "Scrolling failed")], (
            f"the purpose-written notice did not go out: {notifier.notices}"
        )
        assert error_toasts.count == 0, (
            "a wheel call that raised showed a second, generic notification "
            "beside the written one: "
            f"{error_toasts.messages()}"
        )

    def test_a_refused_wheel_call_shows_one_notification(
        self, notifier, error_toasts,
    ):
        def refusing_seam(direction, clicks):
            return (False, "sendinput_short")

        scroller = ContinuousScroller(
            refusing_seam, notifier, tick_seconds=_FAST_TICK_S,
        )
        try:
            scroller.start("down")
            assert _wait_until(lambda: notifier.count >= 1), (
                "the scroll ended without telling the user anything"
            )
        finally:
            scroller.stop()

        assert notifier.notices == [(NOTICE_TITLE, "Scrolling failed")], (
            f"the purpose-written notice did not go out: {notifier.notices}"
        )
        assert error_toasts.count == 0, (
            f"{REFUSALS_BEFORE_STOPPING} refused wheel calls showed a "
            "second, generic notification beside the written one: "
            f"{error_toasts.messages()}"
        )


class TestShutdownStopsTheScroll:
    """The scroll ends when the Input process is asked to close
    (wh-voice-access-parity.2.3.3.2.10).

    The timer is a daemon thread, so it cannot hold the process open. That is
    a statement about the END of shutdown, not about the seconds before it.
    The launcher signals shutdown, waits SHUTDOWN_GRACE_PERIOD_S (five
    seconds, launcher.py) for each process to leave, and only then terminates
    it -- and the Input loop reads that signal in its loop condition, which a
    blocked handler can keep it from reaching. Without a check of its own the
    timer keeps turning the wheel for the whole of that window, so the screen
    goes on moving after the user asked WheelHouse to close.

    The shutdown end is the one automatic end that says NOTHING. Every other
    one shows a notice because the user needs to know why the wheel stopped.
    Here the user already knows: they asked for the shutdown. The notifier
    worker is also being torn down at the same moment, so a notice sent now
    may never be delivered anyway.
    """

    def test_a_signalled_shutdown_ends_the_scroll(self, seam, notifier):
        shutdown = threading.Event()
        scroller = ContinuousScroller(
            seam, notifier, tick_seconds=_FAST_TICK_S,
            maximum_seconds=3600.0, shutdown_event=shutdown,
        )
        try:
            assert scroller.start("down") is True
            assert _wait_until(lambda: seam.count >= 2), (
                "the scroll never started, so this test proves nothing about "
                "stopping it"
            )
            shutdown.set()
            assert _wait_until(lambda: not scroller.is_running()), (
                "the scroll was still running after shutdown was signalled"
            )
            # Read AFTER the run is forgotten. The timer clears the run as the
            # last thing it does before returning, so no wheel call can follow
            # this reading, and the sleep below would catch one that did.
            settled = seam.count
            time.sleep(_FAST_TICK_S * 20)
            assert seam.count == settled, (
                "the wheel turned again after shutdown was signalled: "
                f"{settled} calls at the stop, {seam.count} after"
            )
        finally:
            scroller.stop()

    def test_a_shutdown_end_says_nothing_to_the_user(self, seam, notifier):
        shutdown = threading.Event()
        scroller = ContinuousScroller(
            seam, notifier, tick_seconds=_FAST_TICK_S,
            maximum_seconds=3600.0, shutdown_event=shutdown,
        )
        try:
            assert scroller.start("down") is True
            assert _wait_until(lambda: seam.count >= 2)
            shutdown.set()
            assert _wait_until(lambda: not scroller.is_running())
        finally:
            scroller.stop()

        assert notifier.notices == [], (
            "the shutdown end showed the user a notice; the user asked for "
            "the shutdown and the notifier worker is closing with the "
            f"process: {notifier.notices}"
        )

    def test_a_start_after_shutdown_turns_no_wheel(self, seam, notifier):
        shutdown = threading.Event()
        shutdown.set()
        scroller = ContinuousScroller(
            seam, notifier, tick_seconds=_FAST_TICK_S,
            maximum_seconds=3600.0, shutdown_event=shutdown,
        )
        try:
            assert scroller.start("down") is False, (
                "a start during shutdown reported success"
            )
            time.sleep(_FAST_TICK_S * 20)
            assert seam.count == 0, (
                "a start during shutdown turned the wheel "
                f"{seam.count} times"
            )
            assert scroller.is_running() is False
        finally:
            scroller.stop()

        assert notifier.notices == [], (
            f"a start during shutdown showed the user a notice: "
            f"{notifier.notices}"
        )

    def test_a_shutdown_signal_that_raises_ends_the_scroll(
        self, seam, notifier,
    ):
        """A signal this class cannot read is treated as shutdown.

        The event crosses a process boundary, and the one realistic way
        is_set() raises is that the process is already being torn down
        underneath it. Stopping is the answer that is right in that case and
        harmless in every other: the user can start the scroll again, and the
        alternative -- reading the failure as "not shutting down" -- would
        leave the wheel turning with no way to notice.
        """
        class _RaisingEvent:
            def __init__(self):
                self.reads = 0

            def is_set(self):
                self.reads += 1
                raise OSError("the event handle is closed")

        shutdown = _RaisingEvent()
        scroller = ContinuousScroller(
            seam, notifier, tick_seconds=_FAST_TICK_S,
            maximum_seconds=3600.0, shutdown_event=shutdown,
        )
        try:
            assert scroller.start("down") is False, (
                "a start whose shutdown signal raised reported success"
            )
            time.sleep(_FAST_TICK_S * 20)
            assert seam.count == 0, (
                "the wheel turned although the shutdown signal could not be "
                f"read: {seam.count} calls"
            )
        finally:
            scroller.stop()
        assert shutdown.reads >= 1, (
            "the shutdown signal was never read at all"
        )

    def test_no_shutdown_signal_leaves_the_scroll_running(self, seam, notifier):
        """The default. Nothing passes an event in the tests that predate this.

        Guards the None branch directly, so a change that treated "no signal"
        as "shutting down" fails here by name rather than as a scattering of
        unrelated failures across the file.
        """
        scroller = ContinuousScroller(
            seam, notifier, tick_seconds=_FAST_TICK_S, maximum_seconds=3600.0,
        )
        try:
            assert scroller.start("down") is True
            assert _wait_until(lambda: seam.count >= 3), (
                "a scroller built with no shutdown signal stopped anyway"
            )
            assert scroller.is_running() is True
        finally:
            scroller.stop()
