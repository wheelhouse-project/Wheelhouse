"""The repeating mouse-wheel timer behind "start scrolling"
(wh-voice-access-parity.2.3.3).

WHY A THREAD. The Input process runs ONE command loop. Every spoken command
is dispatched on it, one after another, and a handler holds it until the
handler returns. A scroll that ran inside its handler would therefore hold
the loop for the whole scroll -- and the stop word arrives through that same
loop, so the scroll could never be stopped by voice. The start handler
instead starts the thread here and returns at once, which leaves the loop
free for the next command.

WHY ONE TIMER AND NOT SEVERAL. Two scrolls at once would fight over the same
wheel, and a user has one stop word. A second start therefore replaces the
first: the old run is stopped and its thread is waited for BEFORE the new one
begins, so no wheel event can arrive late in the old direction. The discrete
scroll commands stop it too (ui/ui_action_handler.py), for the same reason: a
user who says "scroll down 3" during a continuous scroll wants three notches,
not three notches added to a scroll still running.

WHY THERE IS A TIME LIMIT. The stop word can be missed -- the speech provider
mishears it, the microphone is muted, the user walks away. Without a limit
the wheel would turn until the process ended. The limit ends the scroll and
tells the user why, because a scroll that simply stopped would look like a
fault.

WHY A REFUSED WHEEL CALL ENDS THE SCROLL. The wheel call reports a refusal
by RETURNING ``(False, reason)``; it does not raise. Windows refuses injected
input against an elevated foreground window, on the secure desktop, and when
the input queue is full. A tick loop that ignored that result would keep
"scrolling" for the whole maximum duration without moving the wheel once, and
would then report that it had scrolled. So the loop reads the result, and a
run of refusals ends the scroll and says why.

WHY THE THREE AUTOMATIC ENDS LOG AT WARNING AND NOT AT ERROR. input_proc.py
calls setup_logging before it builds the UIActionHandler, and setup_logging
attaches ErrorNotificationHandler to the ROOT logger
(utils/logging_setup.py). That handler turns every ERROR record into a
Windows notification, through the same NotifierWorker send_notice uses, and
this module's logger propagates to root. An ERROR record here would therefore
show a generic "[ERROR] continuous_scroll" box, and _stop_and_say would then
send the purpose-written one: two boxes for one failure, with a screen reader
reading the generic one first. wh-wheel-refusal-notice later applied the same
reasoning to the wheel primitive itself: utils/win_input_sender.scroll_wheel
logged its refusal at ERROR, which showed a second box for every refused tick
no matter what this module logged, so that record is a WARNING now too. The three ends that report themselves --- a
refused timer thread, a wheel call that raised, and a run of refused wheel
calls --- log at WARNING so only the written notice reaches the user
(wh-voice-access-parity.2.3.3.2.9). The accepted cost: an operator grepping
wheelhouse.log for ERROR no longer finds these three, so the log line has to
be found at WARNING. The same trade-off is already made and explained in
ui/phrase_select_notice.py and inside send_notice below.

WHY THE SCROLL WATCHES THE PROCESS'S SHUTDOWN SIGNAL. The timer thread is a
daemon, so it cannot hold a shutting-down Input process open, and a restart
therefore starts with no scroll running. That is a statement about the END of
shutdown, and it was once the whole of what this module said about shutdown.
It is not enough. The launcher signals shutdown, waits SHUTDOWN_GRACE_PERIOD_S
-- five seconds, launcher.py -- for each process to leave, and only then
terminates it. The Input process reads that signal in its command loop's
condition (input_proc.py), so a handler that is blocked keeps the loop from
ever reaching it, and the loop's cleanup never calls stop_continuous_scroll
either. A timer that watched only its own stop event would therefore go on
turning the wheel for the whole of that window: the user asks WheelHouse to
close or restart, and the screen keeps moving for seconds afterwards
(wh-voice-access-parity.2.3.3.2.10). So the timer reads the shared signal
itself, before each wheel call, on a thread that no blocked handler can hold.
The check costs one read per tick, which makes SCROLL_TICK_SECONDS the longest
the wheel can turn after the signal.

That end is the ONE automatic end that shows no notice. The user asked for the
shutdown, so a box explaining why the wheel stopped tells them nothing they do
not know -- and NotifierWorker is being torn down in the same seconds, so the
notice might never arrive, or might arrive as one more thing to dismiss on the
way out.
"""
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# crewcut: the three numbers below are fixed in this first version. They belong
# in config.toml under a [scroll] section, read the way ClickConfig.from_raw
# reads [click] (ui/click_config.py), so a user can match the speed to their
# own hardware. Nothing here reads config today; a later change adds the
# section, passes the values into UIActionHandler's scroller, and keeps these
# constants as the defaults for a missing or unusable value.

# Wheel notches per tick. Three notches is what a short flick of a real wheel
# sends, and it is the same order as the discrete commands a user already
# reaches for.
SCROLL_NOTCHES_PER_TICK = 3

# Seconds between ticks. At three notches this scrolls at roughly the speed of
# holding a scrollbar, and it is slow enough that a stop word arriving between
# two ticks stops the scroll inside one tick.
SCROLL_TICK_SECONDS = 0.12

# Seconds a continuous scroll may run before it stops itself. Two minutes is
# far longer than any real reading pass and short enough that a missed stop
# word costs a page rather than a session.
SCROLL_MAXIMUM_SECONDS = 120.0

# The four directions utils/win_input_sender.scroll_wheel accepts. Named here
# rather than imported so this module can be read and tested without the
# Win32 import graph.
_DIRECTIONS = ("up", "down", "left", "right")

# The notification title, matching ui/phrase_select_notice.py. Plain, because
# a screen reader reads it out before the message.
NOTICE_TITLE = "Wheelhouse"

AUTO_STOP_MESSAGE = "Scrolling stopped after two minutes"

WHEEL_FAILED_MESSAGE = "Scrolling failed"

# Shown when the operating system refuses the timer thread, which is the
# only way start() can fail after the direction is accepted. Without it the
# user asks for a scroll, the handler reports success, and nothing moves.
START_FAILED_MESSAGE = "Scrolling could not start"

# How many wheel calls in a row may be refused before the scroll ends. Not one:
# a refusal is not always permanent, because SendInput also reports a short send
# when the input queue is momentarily full, and one refused tick must not end a
# scroll the user asked for. A refusal Windows will keep making -- input blocked
# against an elevated foreground window, or the secure desktop -- repeats every
# tick, so three ends that scroll inside half a second.
REFUSALS_BEFORE_STOPPING = 3


def _refusal_reason(result) -> Optional[str]:
    """The reason a wheel call reports, or None when it turned the wheel.

    The seam contract is ``(succeeded, reason)``
    (:func:`utils.win_input_sender.scroll_wheel`), and a refused call RETURNS
    ``(False, "sendinput_short")`` rather than raising. Any other shape is read
    as success on purpose: the seam is injectable, and a caller that returns
    ``None`` has reported nothing, not a failure.
    """
    if isinstance(result, tuple) and len(result) == 2:
        succeeded, reason = result
        if not succeeded:
            return str(reason)
    return None


def send_notice(
    title: str, message: str, *, source: str = "continuous scroll",
) -> None:
    """Show a Windows notification, the way the select-phrase report does.

    The Input process already owns a NotifierWorker, because input_proc.py
    calls setup_logging, so the message never crosses a process boundary.
    A missing worker is not an error: the scroll still stopped, and only the
    report is lost. Every path that shows nothing writes a line, so a scroll
    that reported nothing does not read like a scroll that reported.

    Args:
        source: which scroll is reporting, used to open every log line below.
            The discrete spoken scroll calls this too since
            wh-wheel-refusal-notice, so a line that always said "continuous
            scroll" would name the wrong caller half the time. The default
            keeps this module's own calls reading exactly as before.
    """
    try:
        from utils.logging_setup import get_notifier_worker
        from utils.notifier_worker import NotifierPayload

        worker = get_notifier_worker()
        if worker is None:
            logger.info(
                "%s: no notifier worker, %r not shown", source, message
            )
            return
        payload = NotifierPayload(
            title=title,
            # Not ERROR. An ERROR record would show a SECOND notification
            # through ErrorNotificationHandler, and that one would say
            # [ERROR] for something that is not a fault.
            message=message,
            levelname="INFO",
            trace_id="",
        )
        if not worker.submit(payload):
            logger.info("%s: the notifier refused %r", source, message)
    except Exception as exc:  # noqa: BLE001 -- a report must never raise
        # WARNING, not ERROR, because an ERROR here pops its own notification.
        logger.warning(
            "%s: the notification failed: %s", source, type(exc).__name__
        )


class _Run:
    """One continuous scroll, from a start command to whatever ends it."""

    def __init__(self, direction: str, started_at: float):
        self.direction = direction
        self.started_at = started_at
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None


class ContinuousScroller:
    """The one continuous scroll the Input process may have running.

    Args:
        scroll_seam: called as ``seam(direction, clicks)`` for each tick.
            Production passes the same Win32-backed wrapper the discrete
            scroll handler uses, so no test here sends real input.
        notifier: called as ``notifier(title, message)`` when the scroll
            stops itself. Defaults to the Windows notification above.
        notches_per_tick: wheel notches each tick sends.
        tick_seconds: seconds between ticks.
        maximum_seconds: how long a scroll may run before it stops itself.
        monotonic: the clock, injectable so a test can reach the maximum
            duration without waiting for it.
    """

    # The timer thread's name. A stack dump names the thread that was
    # scrolling, and the tests find the thread by this name.
    THREAD_NAME = "input-continuous-scroll"

    # How long a start or stop waits for the previous timer thread to finish.
    # The thread's wait ends the moment its stop event is set, so the only
    # thing left to finish is one wheel call, which is a single SendInput
    # batch. The timeout is a backstop, not an expected cost.
    _JOIN_TIMEOUT_S = 2.0

    def __init__(
        self,
        scroll_seam: Callable[[str, int], object],
        notifier: Optional[Callable[[str, str], None]] = None,
        *,
        notches_per_tick: int = SCROLL_NOTCHES_PER_TICK,
        tick_seconds: float = SCROLL_TICK_SECONDS,
        maximum_seconds: float = SCROLL_MAXIMUM_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        shutdown_event=None,
    ):
        self._seam = scroll_seam
        self._notifier = notifier if notifier is not None else send_notice
        self._notches_per_tick = notches_per_tick
        self._tick_seconds = tick_seconds
        self._maximum_seconds = maximum_seconds
        self._monotonic = monotonic
        # The Input process's shared shutdown signal, or None when nothing
        # passed one. See the module docstring: without it the wheel keeps
        # turning through the launcher's shutdown grace period
        # (wh-voice-access-parity.2.3.3.2.10). Optional because every test
        # that predates this builds a scroller with no process around it.
        self._shutdown_event = shutdown_event
        # Guards _run only. It is never held across a wheel call, so a slow
        # SendInput cannot block a start or a stop.
        self._lock = threading.Lock()
        self._run: Optional[_Run] = None

    def is_running(self) -> bool:
        """True while a continuous scroll is going."""
        with self._lock:
            return self._run is not None

    def _shutting_down(self) -> bool:
        """True once the Input process has been asked to close.

        False when nothing passed a shutdown signal, which is every test that
        predates this and any caller that builds a scroller on its own.

        A signal that RAISES is read as shutting down. The event is a
        multiprocessing.Event owned by the launcher, and the one realistic way
        is_set() raises is that the process is already being torn down
        underneath it. Stopping is right in that case and harmless in every
        other -- the user can start the scroll again -- while reading the
        failure as "not shutting down" would leave the wheel turning with
        nothing left to notice it.
        """
        event = self._shutdown_event
        if event is None:
            return False
        try:
            return bool(event.is_set())
        except Exception:  # noqa: BLE001 -- see the docstring; stop, do not raise
            # WARNING, not ERROR: this ends the scroll silently during a
            # shutdown, and an ERROR record would show a notification box as
            # the process closes. See the module docstring.
            logger.warning(
                "continuous scroll: the shutdown signal could not be read; "
                "stopping the scroll", exc_info=True,
            )
            return True

    def start(self, direction) -> bool:
        """Start scrolling in *direction*, replacing any scroll already going.

        Returns True when a scroll started. An unusable direction returns
        False and starts nothing: a timer running in some arbitrary default
        direction would keep scrolling until the user found the words to stop
        it.

        Returns as soon as the thread is started. It never waits for a tick,
        which is what keeps the Input command loop free for the stop word.
        """
        # Before the direction is even looked at: once the process is closing,
        # nothing this class does can be worth starting, and the direction
        # warnings below would only be noise in a shutting-down log
        # (wh-voice-access-parity.2.3.3.2.10). Silent, like the shutdown end
        # itself -- the user asked for the shutdown.
        if self._shutting_down():
            logger.info(
                "continuous scroll: the process is closing; starting nothing"
            )
            return False
        if not isinstance(direction, str):
            logger.warning(
                "continuous scroll: non-text direction %r; starting nothing",
                direction,
            )
            return False
        normalized = direction.strip().lower()
        if normalized not in _DIRECTIONS:
            logger.warning(
                "continuous scroll: unknown direction %r; starting nothing",
                direction,
            )
            return False

        # Stop the previous run BEFORE the new one starts, so the two can
        # never send wheel events at the same time.
        self._end_current_run()

        run = _Run(normalized, self._monotonic())
        run.thread = threading.Thread(
            target=self._tick_until_stopped,
            args=(run,),
            name=self.THREAD_NAME,
            daemon=True,
        )
        with self._lock:
            self._run = run
        try:
            run.thread.start()
        except Exception:  # noqa: BLE001 -- see below; the roll-back matters
            # The run is installed BEFORE the thread starts, because the
            # thread reads self._run through _clear_if_current the moment it
            # begins. So a refused start -- RuntimeError("can't start new
            # thread") when the machine is out of them -- leaves a run whose
            # thread will never run, and that state is worse than the failure
            # itself: is_running() answers True, nothing turns the wheel, and
            # the NEXT command reaches _end_current_run, which joins a thread
            # that was never started and raises there too. The caller in
            # ui_action_handler catches every exception and the action is not
            # in input_proc's _HANDLES_OWN_RESPONSE, so the user would get two
            # accepted commands and no scrolling
            # (wh-voice-access-parity.2.3.3.2.8).
            # WARNING, not ERROR: the notice below is the one the user
            # should get. See the module docstring.
            logger.warning(
                "continuous scroll: the timer thread would not start (%s)",
                normalized, exc_info=True,
            )
            self._stop_and_say(run, START_FAILED_MESSAGE)
            return False
        logger.info("continuous scroll started: %s", normalized)
        return True

    def stop(self) -> bool:
        """Stop the running scroll.

        Returns True when a scroll was running and is now stopped, False when
        nothing was running. False is not a failure: stopping a scroll that
        already stopped changes nothing, and the user who says the stop words
        twice must not be refused the second time. The Input process relies on
        that, because it also calls this when it drops an expired stop
        command.
        """
        stopped = self._end_current_run()
        if stopped:
            logger.info("continuous scroll stopped")
        return stopped

    def _end_current_run(self) -> bool:
        """Take the current run, signal it, and wait for its thread."""
        with self._lock:
            run = self._run
            self._run = None
        if run is None:
            return False
        run.stop_event.set()
        thread = run.thread
        # Never join the timer thread from inside itself: the auto-stop path
        # runs on that thread and would deadlock for the whole timeout.
        #
        # Never join a thread that has not been started either: start()
        # installs the run under the lock, releases it, and only then starts
        # the thread, so a stop arriving in that gap gets here with an
        # unstarted thread and join raises RuntimeError. Thread.ident is None
        # for exactly that state and for no other -- a finished thread keeps
        # its ident and joins immediately -- so this is a test of the state,
        # not a guess at it (wh-voice-access-parity.2.3.3.2.8). Skipping the
        # join is right rather than merely safe: stop_event is already set
        # above, and _tick_until_stopped reads it before its first wheel call,
        # so the thread this run is about to start turns nothing.
        if (
            thread is not None
            and thread.ident is not None
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=self._JOIN_TIMEOUT_S)
            if thread.is_alive():
                # The generation check inside the loop still stops it from
                # scrolling, because its own stop event is set.
                logger.warning(
                    "continuous scroll: the timer thread did not finish "
                    "within %.1fs", self._JOIN_TIMEOUT_S,
                )
        return True

    def _tick_until_stopped(self, run: _Run) -> None:
        """Turn the wheel every tick until something ends this run."""
        refusals = 0
        while not run.stop_event.is_set():
            # BEFORE the wheel call, so no wheel event follows the signal.
            # The check costs one read per tick, and SCROLL_TICK_SECONDS
            # (0.12) is therefore the longest the wheel can turn after
            # shutdown is signalled -- against the launcher's five-second
            # grace period (wh-voice-access-parity.2.3.3.2.10).
            if self._shutting_down():
                self._stop_for_shutdown(run)
                return
            if self._monotonic() - run.started_at >= self._maximum_seconds:
                self._auto_stop(run)
                return
            try:
                result = self._seam(run.direction, self._notches_per_tick)
            except Exception:  # noqa: BLE001 -- a broken seam ends the scroll
                # Ending beats retrying. A seam that raises once raises every
                # tick, and a scroll that cannot move the wheel is only
                # filling the log.
                # WARNING, not ERROR: the notice below is the one the user
                # should get. See the module docstring.
                logger.warning(
                    "continuous scroll: the wheel call failed; stopping the "
                    "scroll", exc_info=True,
                )
                self._stop_and_say(run, WHEEL_FAILED_MESSAGE)
                return
            reason = _refusal_reason(result)
            if reason is None:
                refusals = 0
            else:
                refusals += 1
                if refusals >= REFUSALS_BEFORE_STOPPING:
                    # WARNING, not ERROR: the notice below is the one the
                    # user should get. See the module docstring.
                    logger.warning(
                        "continuous scroll: %d refused wheel calls in a row "
                        "(%s); stopping the scroll", refusals, reason,
                    )
                    self._stop_and_say(run, WHEEL_FAILED_MESSAGE)
                    return
                if refusals == 1:
                    # Only the first refusal of a run. A scroll that recovers
                    # from one refused tick must not fill the log, and the
                    # wheel call writes its own WARNING line for every refusal
                    # anyway (it was an ERROR until wh-wheel-refusal-notice
                    # demoted it, so grep for it at WARNING).
                    logger.warning(
                        "continuous scroll: the wheel call was refused (%s); "
                        "stopping after %d in a row",
                        reason, REFUSALS_BEFORE_STOPPING,
                    )
            if run.stop_event.wait(timeout=self._tick_seconds):
                return

    def _stop_for_shutdown(self, run: _Run) -> None:
        """End a scroll because the Input process is closing, and say nothing.

        The one automatic end with no notice
        (wh-voice-access-parity.2.3.3.2.10). Every other end shows one because
        a scroll that simply stopped looks like a fault. This end does not:
        the user asked for the shutdown, so they already know why the wheel
        stopped. The notifier worker is also being torn down at the same
        moment, so a notice sent here may never be delivered, and a delivered
        one would be a box the user has to dismiss on their way out.

        Goes through _clear_if_current rather than _end_current_run, for the
        same reason _stop_and_say does: this runs ON the timer thread, and
        _end_current_run would join that thread from inside itself.
        """
        logger.info(
            "continuous scroll: stopping because the process is closing"
        )
        self._clear_if_current(run)

    def _auto_stop(self, run: _Run) -> None:
        """End a scroll that reached its maximum duration, and say so."""
        logger.info(
            "continuous scroll: stopping after the maximum duration of %.0fs",
            self._maximum_seconds,
        )
        self._stop_and_say(run, AUTO_STOP_MESSAGE)

    def _stop_and_say(self, run: _Run, message: str) -> None:
        """End *run* and show *message*: every end the user did not ask for.

        A spoken stop says nothing, because the user who said the words
        already knows. Every other end needs the notice: a scroll that simply
        stopped looks like a fault, and a scroll the wheel refused moved
        nothing on the screen, so a user who cannot see the screen has no
        other sign of what happened.

        The notice goes out only when this run was still the current one.
        Every caller but one reaches here from the timer thread, which can be
        inside a wheel call, or inside the clock reading, at the moment
        something else ends the run -- a spoken stop, a discrete scroll, the Input
        process dropping an expired stop, or a replacement start. The thread
        is past its own check of the stop signal by then, so without the
        ownership check it would report an end the user had already asked
        for: "the wheel would not turn", or "two minutes are up", for a
        scroll the user stopped themselves
        (wh-voice-access-parity.2.3.3.2.5).

        The one caller that is NOT the timer thread is start(), when the
        machine refuses the thread. The same ownership check is right there:
        a run whose thread never started must not report a failure a
        replacement start has already made irrelevant.
        """
        if not self._clear_if_current(run):
            logger.info(
                "continuous scroll: this run was already stopped, so it says "
                "nothing about the end it reached (%s)", message,
            )
            return
        # crewcut: this notice can reach the user after a REPLACEMENT scroll
        # has already started, and it then reads as a statement about the
        # scroll now running rather than the one that ended
        # (wh-voice-access-parity.2.3.3.2.1, agreed and deliberately not
        # fixed here). The ownership check above closes the half where the
        # replacement start reaches _end_current_run FIRST: this run is no
        # longer the current one, so it says nothing. What remains is the
        # half where the notice goes out first and the start follows it, and
        # that half has two windows. The queue window: the notifier only
        # enqueues, and one worker thread delivers later, so the gap is that
        # worker's delivery latency plus every notice queued ahead. The
        # display window: the toast is shown with a ten-second timeout, so a
        # notice displayed one instant before the replacement scroll starts
        # stays on screen beside it. Closing the first needs
        # NotifierPayload to carry an optional identity, NotifierWorker to
        # drop a stale payload at delivery, and this class to carry a
        # generation counter it stamps on each notice. The second cannot be
        # closed from here at all: a displayed toast cannot be recalled.
        # NotifierWorker carries every notice in the system, so that change
        # belongs to its own piece of work, not to this one.
        try:
            self._notifier(NOTICE_TITLE, message)
        except Exception:  # noqa: BLE001 -- a report must never raise
            logger.warning(
                "continuous scroll: the stop report failed", exc_info=True,
            )

    def _clear_if_current(self, run: _Run) -> bool:
        """Forget *run*, unless something else already took it.

        Called from the timer thread. Without the identity check, a run that
        ended just as a new one started would clear the NEW run's record and
        leave the process believing nothing was scrolling while the new timer
        kept going.

        Returns True when this run was still the current one and is now
        forgotten, and False when a stop, a discrete scroll or a replacement
        start had already taken it. The answer is what tells the caller
        whether the end is this run's to report.
        """
        run.stop_event.set()
        with self._lock:
            if self._run is run:
                self._run = None
                return True
        return False
