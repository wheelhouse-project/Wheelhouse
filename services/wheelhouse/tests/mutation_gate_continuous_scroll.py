"""Mutation gate for continuous scrolling (wh-voice-access-parity.2.3.3).

WHY THIS GATE EXISTS. Every test in this feature was written before its
implementation and was seen to fail. That is the first half of the rule. The
second half is that a test can fail for the wrong reason, so each protected
behaviour is broken here at the level of the INPUT the code depends on -- the
membership set the expiry consults, the direction check, the anchor in the
shipped pattern -- and not only by deleting the fix. Unlike a captured log of
a failing run, this file can be re-run later to show the tests still catch
what they claim.

WHAT IT COVERS, grouped by the acceptance item each mutation defends:

  item 1 (the wheel keeps turning, at the configured rate)
      no-wheel-turn                 the tick loop stops calling the wheel
      one-notch-per-tick            the configured notch count is ignored
  item 3 (an expired stop still stops, and the drop is still reported)
      expired-stop-not-run          the special case is deleted
      expired-stop-set-empty        the membership set is emptied
      expired-set-admits-the-start  the set is widened to the start command,
                                    which must NOT run late
      expired-drop-not-reported     the stop runs but the drop goes unsaid
  item 4 (a scroll the user did not stop ends with a notice)
      no-maximum-duration           the limit becomes infinite
      a-stop-says-nothing           the report is dropped, for BOTH the
                                    maximum duration and a refused wheel
  item 5 (one scroll at a time)
      start-does-not-replace        a second start leaves the first running
      stop-does-not-signal          stop forgets the run without ending it
      discrete-leaves-it-running    a discrete scroll no longer stops it
  item 6 (dictated prose is still typed)
      stop-pattern-unanchored       the stop entry matches mid-sentence
      stop-entry-not-whole-only     the whole-utterance flag is removed
  a wheel that will not turn (wh-voice-access-parity.2.3.3.1.2)
      refusal-ignored               the returned failure is discarded, so a
                                    refused scroll runs the full two minutes
      one-refusal-ends-the-scroll   a single refused tick ends the scroll
      unknown-shape-is-a-refusal    a seam that reports nothing ends it
      a-raise-keeps-spinning        a raising wheel call no longer ends it
  the refusals on each side
      start-accepts-any-direction   Logic sends an unknown direction
      direction-type-unchecked      a non-text direction picks one instead
      scroller-accepts-any-direction  the timer starts on one
      stop-returns-nothing          the stop command sends no payload
  the payload the spoken command builds
      direction-not-normalised      "UP" and " Down " are passed through
      every-direction-sends-down    every direction builds the same payload
      start-registered-under-another-name  the registry name stops matching
                                    the name patterns.toml calls
      stop-registered-under-another-name  the same, one action over
  the shipped entries beyond the anchor and the flag
      stop-entry-needs-a-hotword    the stop word needs a prefix
      start-entry-passes-the-wrong-direction  an entry scrolls the wrong way
      a-start-entry-swallows-other-phrases  a start entry matches anything
                                    ending in "scrolling", so the stop word
                                    reaches it first
      stop-entry-takes-an-argument  the stop entry gains a capture group
      stop-entry-appears-twice      the entry is duplicated under one doc_id
  what the scroller reports about itself
      no-run-stop-claims-success    stopping nothing answers True
      is-running-inverted           the running state is reported backwards
  who may report an end (wh-voice-access-parity.2.3.3.2.5)
      a-stopped-run-still-reports   the ownership check is deleted, so a
                                    stop the user asked for is announced as
                                    a refused wheel or a time limit
      ownership-always-granted      the check stays but always answers yes
  the handler wiring
      handler-does-not-start        the start handler starts no timer
      handler-does-not-stop         the stop handler stops no timer
      handler-builds-no-scroller    the handler holds no scroller at all
      scroller-gets-its-own-seam    the scroller reaches the wheel past the
                                    handler's own seam attribute
      a-failing-stop-loses-the-scroll  a raising stop costs the user the
                                    discrete notches asked for
      timer-thread-outlives-shutdown  the timer stops being a daemon
  the remembered text
      start-skips-the-invalidation  a wheel turn leaves a stale copy
  a timer thread the machine refuses (wh-voice-access-parity.2.3.3.2.8)
      start-failure-not-rolled-back  the dead run stays installed
      start-failure-says-nothing    the refused start tells the user nothing
      start-failure-reports-success  start() answers True for a scroll that
                                    never began
      join-an-unstarted-thread      the join stops skipping a thread that
                                    was never started
      start-failure-neither-half    both halves at once, which is what the
                                    recovery test needs to be broken at all
  one notification per failure (wh-voice-access-parity.2.3.3.2.9)
      start-failure-logs-at-error   a refused timer thread logs at ERROR
      wheel-raise-logs-at-error     a raising wheel call logs at ERROR
      wheel-refusal-logs-at-error   a run of refusals logs at ERROR

This list is a reading aid, not the authority: it groups the mutations by
the behaviour each defends, which ``MUTATIONS`` itself does not record. The
list of mutations that actually run is ``MUTATIONS``. Round 5 of the codex
review added five mutations and this list was not extended with them, which
is the ordinary way such a list goes stale.

WHAT IS NOT MUTATED. The ``NOT_MUTATED`` mapping below names every test in
the five feature test files that no mutation breaks, each with the reason it
has none, and ``_coverage_errors`` refuses to start the run when a collected
test appears in neither that mapping nor a mutation's ``expect`` list. Read
the mapping, not this docstring, for the boundary: prose was tried twice and
filed as an over-claim both times (wh-voice-access-parity.2.3.3.1.1, then
.2.3.3.2.3 after the prose had been rewritten to be complete). Every reason
states a property of the behaviour itself -- silence cannot be broken by
deleting a call, the only mutation would not compile, the only mutation would
never terminate, or the behaviour belongs to code outside this bead. A reason
may NOT say that some other mutation already covers the test, and
``_coverage_errors`` refuses the run when one names a mutation. Round 2 of
the codex review found eight reasons of exactly that shape and all eight were
false, because the cited mutant changed a line the named test never reaches
(wh-voice-access-parity.2.3.3.2.4). Such a claim is unverifiable where it is
written and unchecked where it is read.

WHAT ``caught`` MEANS. Each mutation's ``expect`` list is the EXACT set of
tests that must fail, not a lower bound. The run refuses when a test fails
that is in neither ``expect`` nor the mutation's ``also_fails`` mapping.
Each mutation runs all five feature test files in ONE pytest process, not
the one file its behaviour lives in. Codex round 4 found that the exact
check was exact only inside a single file, so a scroller mutation could
break the containment file's tests -- they drive a real ContinuousScroller
through the real command loop -- and still print a clean "caught"
(wh-voice-access-parity.2.3.3.2.7). ``also_fails`` names a test that DOES
fail under the mutation without proving
anything by it -- it fell over on a shared precondition, or it waits for a
wheel turn that no longer comes -- and gives the reason. Round 3 of the codex
review found that the run only checked that every expected name failed, so a
mutation could break tests nobody had looked at and still print a clean
"caught", and a reader counting failures would credit those tests with
proving their own behaviour (wh-voice-access-parity.2.3.3.2.6). An
``also_fails`` name is NOT counted as covered: a test that only ever falls
over still owes a mutation of its own or a NOT_MUTATED entry.

Two things worth stating here are about what a mutation gate CANNOT show
rather than about what this one skipped.

``ContinuousScroller._end_current_run`` joins the previous timer thread
before a new one starts. Removing the join leaves a race rather than a
defect: the old thread's stop event is already set, so it exits after at most
one more wheel call, and whether that call lands is a matter of scheduling. A
deterministic test cannot pin it, so a mutation there would report a survivor
that means nothing.

Acceptance item 2 -- the Input command loop stays free while a scroll runs --
has no bounded mutation either. Its guarantee is that the start handler
returns instead of turning the wheel itself, so every way of breaking it
(running the tick loop inline, joining the new thread) makes the mutant sit
in that loop until the maximum duration. That is the never-terminates failure
the mutation-gate skill warns about: it proves nothing and costs the rest of
the run. Item 2 rests on its two red-first tests, named in NOT_MUTATED.

Run it from services/wheelhouse, and never while a test suite is running in
the same worktree -- this file rewrites the sources that suite is importing:

    uv run python tests/mutation_gate_continuous_scroll.py
"""

import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE_DIR = Path(__file__).resolve().parents[1]

INPUT_PROC = SERVICE_DIR / "input_proc.py"
SCROLLER = SERVICE_DIR / "ui" / "continuous_scroll.py"
HANDLER = SERVICE_DIR / "ui" / "ui_action_handler.py"
ACTIONS = SERVICE_DIR / "speech" / "actions.py"
PATTERNS = SERVICE_DIR / "speech" / "config" / "patterns.toml"

EXPIRY_TESTS = "tests/test_input_proc_stale_command_expiry.py"
SCROLLER_TESTS = "tests/test_continuous_scroller.py"
HANDLER_TESTS = "tests/test_ui/test_continuous_scroll_handler.py"
ACTION_TESTS = "tests/test_continuous_scroll_action.py"
PATTERN_TESTS = "tests/test_continuous_scroll_patterns.py"

# Every mutation runs all five feature test files in one process, which
# takes about eleven seconds clean. The expensive mutations are the ones
# that stop the wheel from turning: twenty test node ids then wait out
# _WAIT_TIMEOUT_S, ten seconds each, before failing at their own wait, so
# no-wheel-turn alone measures 241.6 seconds. The old limit of 240 sat
# just under that and reported the mutation as one that never terminated,
# which is a false error of exactly the kind this limit exists to catch
# honestly. 600 is far above the measured worst case and far below
# anything a reader would sit through, and it leaves room for the next
# waiting test rather than needing another rise with each one.
RUN_TIMEOUT_S = 600

MUTATIONS = [
    # ---------------------------------------------------------------
    # Acceptance item 3: a stop command dropped as expired still stops.
    # ---------------------------------------------------------------
    {
        "name": "expired-stop-not-run",
        "target": INPUT_PROC,
        "old": "                _run_expired_stop(action, ui_handler)\n",
        "new": "                pass\n",
        "expect": [
            "test_a_stop_command_dropped_as_expired_still_stops_the_scroll",
        ],
    },
    {
        # The input-level form of the mutation above. Deleting the call is
        # the obvious break; emptying the set it consults is the one that
        # shows the test is reading the behaviour rather than the call.
        "name": "expired-stop-set-empty",
        "target": INPUT_PROC,
        "old": '_STOPS_EVEN_WHEN_EXPIRED = frozenset({"stop_continuous_scroll"})\n',
        "new": "_STOPS_EVEN_WHEN_EXPIRED = frozenset()\n",
        "expect": [
            "test_a_stop_command_dropped_as_expired_still_stops_the_scroll",
        ],
    },
    {
        # The other direction, and the one that matters most: the special
        # case must cover the stop and nothing else. A start the Logic
        # process already gave up on must not begin scrolling late.
        "name": "expired-set-admits-the-start",
        "target": INPUT_PROC,
        "old": '_STOPS_EVEN_WHEN_EXPIRED = frozenset({"stop_continuous_scroll"})\n',
        "new": (
            "_STOPS_EVEN_WHEN_EXPIRED = frozenset("
            '{"stop_continuous_scroll", "start_continuous_scroll"})\n'
        ),
        "expect": [
            "test_an_expired_start_scroll_command_is_still_dropped",
        ],
    },
    # ---------------------------------------------------------------
    # Acceptance item 4: the scroll stops itself, and says so.
    # ---------------------------------------------------------------
    {
        # The value, not the comparison. A mutated comparison in this loop
        # could change how the loop advances; a mutated limit cannot.
        "name": "no-maximum-duration",
        "target": SCROLLER,
        "old": "        self._maximum_seconds = maximum_seconds\n",
        "new": '        self._maximum_seconds = float("inf")\n',
        "expect": [
            "TestAutoStop::test_the_scroll_ends_at_the_maximum_duration",
            "TestAutoStop::test_the_auto_stop_tells_the_user",
        ],
    },
    {
        # One notifier call now serves every end the user did not ask for,
        # so this one mutation covers the maximum duration AND the refused
        # wheel. The pattern was refreshed when .1.2 moved the call into
        # _stop_and_say; the behaviour it breaks is the same one.
        "name": "a-stop-says-nothing",
        "target": SCROLLER,
        "old": "            self._notifier(NOTICE_TITLE, message)\n",
        "new": "            pass\n",
        "expect": [
            "TestAutoStop::test_the_auto_stop_tells_the_user",
            "TestABrokenWheelCall::test_a_refused_scroll_tells_the_user",
            "TestAThreadTheMachineWillNotStart::test_a_refused_thread_start_tells_the_user",
        ],
        "also_fails": {
            "TestOneNotificationPerFailure::test_a_refused_thread_start_shows_one_notification": (
                "this mutation removes the notice itself, so the test "
                "loses the one box it asserts on before it can say "
                "anything about the second one"
            ),
            "TestOneNotificationPerFailure::test_a_raising_wheel_call_shows_one_notification": (
                "same: the notice this test waits for is gone, so it "
                "never reaches the question of a second box"
            ),
            "TestOneNotificationPerFailure::test_a_refused_wheel_call_shows_one_notification": (
                "same: the notice this test waits for is gone, so it "
                "never reaches the question of a second box"
            ),
        },
    },
    # ---------------------------------------------------------------
    # Acceptance item 5: exactly one continuous scroll at a time.
    # ---------------------------------------------------------------
    {
        "name": "start-does-not-replace",
        "target": SCROLLER,
        "old": "        self._end_current_run()\n",
        "new": "        pass\n",
        "expect": [
            "TestOneScrollAtATime::test_a_second_start_replaces_the_first",
            "TestOneScrollAtATime::test_the_switch_leaves_exactly_one_timer_thread",
        ],
    },
    {
        # Forgetting the run without signalling it: is_running() answers
        # False while the wheel keeps turning, which is the worst of both.
        "name": "stop-does-not-signal",
        "target": SCROLLER,
        "old": "        run.stop_event.set()\n        thread = run.thread\n",
        "new": "        thread = run.thread\n",
        # The signal this deletes is what BOTH a stop and a replacement
        # start use, so the one-at-a-time tests catch it for their own
        # reason: the old timer keeps turning the wheel. The expired-stop
        # test in the containment file catches it for the same reason one
        # process boundary out: it records the turn count after the stop,
        # sleeps twenty tick intervals and requires the count to hold.
        "expect": [
            "TestOneScrollAtATime::test_a_second_start_replaces_the_first",
            "TestOneScrollAtATime::test_the_switch_leaves_exactly_one_timer_thread",
            "TestStartAndStop::test_stopping_ends_the_turns",
            "test_a_stop_command_dropped_as_expired_still_stops_the_scroll",
        ],
    },
    {
        "name": "discrete-leaves-it-running",
        "target": HANDLER,
        "old": (
            '        self._stop_continuous_scroll('
            '"a discrete scroll command arrived")\n'
        ),
        "new": "        pass\n",
        "expect": [
            "TestADiscreteScrollStopsTheContinuousOne::test_a_discrete_scroll_stops_a_running_continuous_scroll",
        ],
    },
    # ---------------------------------------------------------------
    # Acceptance item 6: dictated prose holding these words is typed.
    # ---------------------------------------------------------------
    {
        # The anchor is the first guard. Unanchored, "stop scrolling and
        # read it to me" reaches the command and the rest of the sentence
        # is dropped.
        "name": "stop-pattern-unanchored",
        "target": PATTERNS,
        "old": "pattern = '''^stop scrolling$'''\n",
        "new": "pattern = '''\\bstop scrolling\\b'''\n",
        "expect": [
            "TestContinuousScrollDoesNotShadowDictation::test_a_longer_phrase_does_not_reach_a_continuous_entry",
        ],
    },
    {
        # The flag is the second guard: without it the router runs the
        # command as soon as the matching words arrive.
        "name": "stop-entry-not-whole-only",
        "target": PATTERNS,
        "old": (
            "pattern = '''^stop scrolling$'''\n"
            'doc_id = "scroll-stop"\n'
            "whole_utterance_only = true\n"
        ),
        "new": (
            "pattern = '''^stop scrolling$'''\n"
            'doc_id = "scroll-stop"\n'
        ),
        "expect": [
            "TestContinuousScrollFlagsAndActions::test_the_entry_is_whole_utterance_only",
        ],
    },
    # ---------------------------------------------------------------
    # The Logic-side refusals.
    # ---------------------------------------------------------------
    {
        # Two lines, because the discrete scroll function checks the same
        # four directions one screen above and a one-line pattern would
        # match there instead.
        "name": "start-accepts-any-direction",
        "target": ACTIONS,
        "old": (
            '        if normalized not in ("up", "down", "left", "right"):\n'
            "            logger.warning(\n"
        ),
        "new": (
            "        if False:\n"
            "            logger.warning(\n"
        ),
        "expect": [
            "TestStartPayload::test_an_unknown_direction_sends_nothing",
        ],
    },
    {
        "name": "stop-returns-nothing",
        "target": ACTIONS,
        "old": '        return {"action": "stop_continuous_scroll", "params": {}}\n',
        "new": "        return None\n",
        "expect": [
            "TestStopPayload::test_stop_builds_a_payload_with_no_parameters",
            "TestStopPayload::test_stop_never_refuses",
        ],
    },
    # ---------------------------------------------------------------
    # The remembered copy of the focused control's text.
    # ---------------------------------------------------------------
    {
        # The comment lines are part of the pattern because
        # self.buffer_manager.invalidate() appears many times in this file.
        "name": "start-skips-the-invalidation",
        "target": HANDLER,
        "old": (
            "        # Same reason as the discrete handler: a wheel notch can "
            "change a\n"
            "        # control's value, so the remembered copy cannot survive "
            "it.\n"
            "        self.buffer_manager.invalidate()\n"
        ),
        "new": (
            "        # Same reason as the discrete handler: a wheel notch can "
            "change a\n"
            "        # control's value, so the remembered copy cannot survive "
            "it.\n"
            "        pass\n"
        ),
        "expect": [
            "TestStartHandler::test_it_invalidates_the_remembered_text",
        ],
    },
    # ---------------------------------------------------------------
    # Acceptance item 1: the wheel keeps turning, at the configured rate.
    # ---------------------------------------------------------------
    {
        # The seam call is replaced by a success result rather than deleted,
        # so the mutant still compiles and still terminates: the loop keeps
        # waiting on the stop event and the tests fail at their own wait.
        "name": "no-wheel-turn",
        "also_fails": {
            "TestShutdownStopsTheScroll::test_a_signalled_shutdown_ends_the_scroll": (
                "the test waits for at least one wheel turn before it signals a "
                "shutdown, so the assertion that fires is that wait and nothing "
                "about shutdown is reached"
            ),
            "TestShutdownStopsTheScroll::test_a_shutdown_end_says_nothing_to_the_user": (
                "the test waits for at least one wheel turn before it signals a "
                "shutdown, so the assertion that fires is that wait and nothing "
                "about shutdown is reached"
            ),
            "TestShutdownStopsTheScroll::test_no_shutdown_signal_leaves_the_scroll_running": (
                "the test waits for at least one wheel turn before it signals a "
                "shutdown, so the assertion that fires is that wait and nothing "
                "about shutdown is reached"
            ),
            "TestTheHandlerPassesItsShutdownSignal::test_a_shutdown_stops_a_scroll_the_handler_started": (
                "the test waits for at least one wheel turn before it signals a "
                "shutdown, so the assertion that fires is that wait and nothing "
                "about shutdown is reached"
            ),
            "test_a_blocked_loop_does_not_stop_a_scroll_from_ending_at_shutdown": (
                "the test waits for at least one wheel turn before it signals a "
                "shutdown, so the assertion that fires is that wait and nothing "
                "about shutdown is reached"
            ),
            "TestOneNotificationPerFailure::test_a_raising_wheel_call_shows_one_notification": (
                "the seam never raises in this mutant, so the failure path "
                "this test is about is never reached and no notice goes out"
            ),
            "TestOneNotificationPerFailure::test_a_refused_wheel_call_shows_one_notification": (
                "the seam never refuses in this mutant, so the failure path "
                "this test is about is never reached and no notice goes out"
            ),
            "test_a_stop_command_dropped_as_expired_still_stops_the_scroll": (
            "the test waits for at least one wheel turn before it reaches its own "
            "subject, so the assertion that fires is that wait"
            ),
            "test_the_command_loop_stays_free_while_a_continuous_scroll_runs": (
            "the test waits for at least one wheel turn before it reaches its own "
            "subject, so the assertion that fires is that wait"
            ),
            "TestABrokenWheelCall::test_a_raising_wheel_call_ends_the_scroll_instead_of_spinning": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestABrokenWheelCall::test_a_refused_scroll_tells_the_user": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestABrokenWheelCall::test_a_refused_wheel_call_ends_the_scroll": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestABrokenWheelCall::test_a_seam_that_reports_nothing_keeps_scrolling": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestABrokenWheelCall::test_one_refused_tick_does_not_end_the_scroll": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestAStopTheUserAskedForStaysSilent::test_a_stop_during_the_last_refused_tick_says_nothing": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestAStopTheUserAskedForStaysSilent::test_a_stop_while_the_wheel_call_raises_says_nothing": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestAutoStop::test_a_spoken_stop_says_nothing": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestAutoStop::test_the_auto_stop_tells_the_user": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestAutoStop::test_the_scroll_ends_at_the_maximum_duration": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestAThreadTheMachineWillNotStart::test_the_next_start_works_after_a_refused_thread_start": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestOneScrollAtATime::test_a_second_start_replaces_the_first": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestOneScrollAtATime::test_the_switch_leaves_exactly_one_timer_thread": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestOneScrollAtATime::test_the_timer_thread_dies_with_the_process": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestStartAndStop::test_stopping_ends_the_turns": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
            "TestTheCommandLoopStaysFree::test_start_returns_while_the_wheel_call_is_still_running": (
                "the test waits for at least one wheel turn before it reaches its own "
                "subject, so the assertion that fires is that wait"
            ),
        },
        "target": SCROLLER,
        "old": (
            "                result = self._seam("
            "run.direction, self._notches_per_tick)\n"
        ),
        "new": "                result = (True, None)\n",
        "expect": [
            "TestStartAndStop::test_starting_turns_the_wheel_over_and_over",
            "TestStartAndStop::test_every_turn_carries_the_configured_notch_count",
        ],
    },
    {
        # The input-level form: the wheel still turns, but not by the amount
        # the caller configured.
        "name": "one-notch-per-tick",
        "target": SCROLLER,
        "old": "        self._notches_per_tick = notches_per_tick\n",
        "new": "        self._notches_per_tick = 1\n",
        "expect": [
            "TestStartAndStop::test_every_turn_carries_the_configured_notch_count",
        ],
    },
    # ---------------------------------------------------------------
    # Acceptance item 3, the other half: the drop is still reported.
    # ---------------------------------------------------------------
    {
        # The special case must not swallow the containment's report. A stop
        # that ran and was never reported leaves the Logic process waiting
        # for a reply that is not coming.
        "name": "expired-drop-not-reported",
        "target": INPUT_PROC,
        "old": (
            "                _report_expired_command(\n"
            "                    action, request_id, trace_id, "
            "deadline_monotonic,\n"
            "                    command_dequeue_monotonic, response_queue,\n"
            "                )\n"
        ),
        "new": "                pass\n",
        # Both of the other two assert the report itself, one from the
        # response queue and one from the log, so both catch this for
        # their own named behaviour.
        "expect": [
            "test_a_stop_command_dropped_as_expired_still_stops_the_scroll",
            "test_the_expired_command_drop_is_written_to_the_log",
            "test_the_expired_command_is_reported_and_not_dropped_in_silence",
        ],
    },
    # ---------------------------------------------------------------
    # A wheel that will not turn (wh-voice-access-parity.2.3.3.1.2).
    # ---------------------------------------------------------------
    {
        # The defect the finding reported: the seam RETURNS its refusal, so
        # discarding the result leaves a scroll that never moves the wheel
        # running for the full maximum duration and then reports that it
        # scrolled.
        "name": "refusal-ignored",
        "target": SCROLLER,
        "old": "            reason = _refusal_reason(result)\n",
        "new": "            reason = None\n",
        "expect": [
            "TestABrokenWheelCall::test_a_refused_wheel_call_ends_the_scroll",
            "TestABrokenWheelCall::test_a_refused_scroll_tells_the_user",
        ],
        "also_fails": {
            "TestOneNotificationPerFailure::test_a_refused_wheel_call_shows_one_notification": (
                "the mutant reads every refusal as a success, so the scroll "
                "never ends and the notice this test waits for never goes out"
            ),
        },
    },
    {
        # The opposite error, and the reason the count is not one: a short
        # send also happens when the input queue is momentarily full, and a
        # scroll the user asked for must survive that.
        "name": "one-refusal-ends-the-scroll",
        "target": SCROLLER,
        "old": "REFUSALS_BEFORE_STOPPING = 3\n",
        "new": "REFUSALS_BEFORE_STOPPING = 1\n",
        "expect": [
            "TestABrokenWheelCall::test_one_refused_tick_does_not_end_the_scroll",
        ],
    },
    {
        # Only the documented failure shape ends a scroll. Reading an unknown
        # shape as a refusal would end every scroll an injected seam ran.
        # The success branch gets its own return, so this mutation changes
        # ONLY the unknown shape: a (True, None) result still reads as a
        # success, and the one test that returns None is the one that fails.
        # Written this way after the first run, where moving the fallthrough
        # alone made EVERY result a refusal and the mutation caught three
        # tests under a name that claimed one.
        "name": "unknown-shape-is-a-refusal",
        "target": SCROLLER,
        "old": (
            "        if not succeeded:\n"
            "            return str(reason)\n"
            "    return None\n"
        ),
        "new": (
            "        if not succeeded:\n"
            "            return str(reason)\n"
            "        return None\n"
            '    return "unknown"\n'
        ),
        "expect": [
            "TestABrokenWheelCall::test_a_seam_that_reports_nothing_keeps_scrolling",
        ],
    },
    {
        # The raise path shares the refusal path's ending. Made to fall
        # through as a success rather than to loop without a wait, so the
        # mutant ticks at the normal rate instead of spinning.
        "name": "a-raise-keeps-spinning",
        "target": SCROLLER,
        "old": (
            "                self._stop_and_say(run, WHEEL_FAILED_MESSAGE)\n"
            "                return\n"
            "            reason = _refusal_reason(result)\n"
        ),
        "new": (
            "                result = (True, None)\n"
            "            reason = _refusal_reason(result)\n"
        ),
        "expect": [
            "TestABrokenWheelCall::test_a_raising_wheel_call_ends_the_scroll_instead_of_spinning",
        ],
        "also_fails": {
            "TestOneNotificationPerFailure::test_a_raising_wheel_call_shows_one_notification": (
                "the mutant treats the raise as a success and keeps "
                "ticking, so the scroll never ends and the notice this "
                "test waits for never goes out"
            ),
        },
    },
    # ---------------------------------------------------------------
    # The Input-side refusal, matching the Logic-side one above.
    # ---------------------------------------------------------------
    {
        # Two lines, so the pattern cannot match the isinstance check above
        # it, which reports the same way.
        "name": "scroller-accepts-any-direction",
        "target": SCROLLER,
        "old": (
            "        if normalized not in _DIRECTIONS:\n"
            "            logger.warning(\n"
        ),
        "new": (
            "        if False:\n"
            "            logger.warning(\n"
        ),
        "expect": [
            "TestStartAndStop::test_an_unusable_direction_starts_nothing",
        ],
    },
    # ---------------------------------------------------------------
    # The handler wiring.
    # ---------------------------------------------------------------
    {
        "name": "handler-does-not-start",
        "also_fails": {
            "TestTheHandlerPassesItsShutdownSignal::test_a_shutdown_stops_a_scroll_the_handler_started": (
                "the handler reports a start it never made, so no scroll is "
                "running and the test's first wait for a wheel turn is what "
                "fails"
            ),
        },
        "target": HANDLER,
        "old": (
            "            started = self._continuous_scroller.start(direction)\n"
        ),
        "new": "            started = False\n",
        "expect": [
            "TestStartHandler::test_it_starts_the_scroller_in_the_given_direction",
            # Also asserts on the start call, so it fails here too. It was
            # listed as unmutated with a false reason until
            # wh-voice-access-parity.2.3.3.2.4.
            "TestStartHandler::test_it_ignores_unexpected_extra_fields",
        ],
    },
    {
        "name": "handler-does-not-stop",
        "target": HANDLER,
        "old": (
            "        self._stop_continuous_scroll("
            '"the stop command arrived")\n'
        ),
        "new": "        pass\n",
        "expect": [
            "TestStopHandler::test_it_stops_the_scroller",
            # Both count calls on the stand-in scroller, so both fail when
            # the call is gone. Both were listed as unmutated with a false
            # reason until wh-voice-access-parity.2.3.3.2.4.
            "TestStopHandler::test_stopping_with_nothing_running_is_harmless",
            "TestStopHandler::test_it_ignores_unexpected_extra_fields",
        ],
    },
    {
        # A timer that is not a daemon holds a shutting-down Input process
        # open, and the restart then begins with a scroll still running.
        "name": "timer-thread-outlives-shutdown",
        "target": SCROLLER,
        "old": "            daemon=True,\n",
        "new": "            daemon=False,\n",
        "expect": [
            "TestOneScrollAtATime::test_the_timer_thread_dies_with_the_process",
        ],
    },
    # ---------------------------------------------------------------
    # The payload the spoken command builds
    # (added for wh-voice-access-parity.2.3.3.2.3).
    # ---------------------------------------------------------------
    {
        # The non-text branch stops returning None and picks a direction
        # instead of crashing. A mutation that let ``.strip()`` run on None
        # would fail the test with an AttributeError raised one line above
        # the behaviour under test, which the mutation-gate skill counts as a
        # verdict earned by an unrelated failure.
        "name": "direction-type-unchecked",
        "target": ACTIONS,
        "old": (
            "            return None\n"
            '        normalized = direction.strip().lower()\n'
            '        if normalized not in ("up", "down", "left", "right"):\n'
            "            logger.warning(\n"
            '                "start_continuous_scroll: unknown direction %r; '
            'sending "\n'
        ),
        "new": (
            '            direction = "down"\n'
            '        normalized = direction.strip().lower()\n'
            '        if normalized not in ("up", "down", "left", "right"):\n'
            "            logger.warning(\n"
            '                "start_continuous_scroll: unknown direction %r; '
            'sending "\n'
        ),
        "expect": [
            "TestStartPayload::test_a_direction_that_is_not_text_sends_nothing",
        ],
    },
    {
        # The trailing lines are part of the pattern because the discrete
        # scroll function one screen above normalises its direction the same
        # way, and a one-line pattern would match there instead.
        "name": "direction-not-normalised",
        "target": ACTIONS,
        "old": (
            "        normalized = direction.strip().lower()\n"
            '        if normalized not in ("up", "down", "left", "right"):\n'
            "            logger.warning(\n"
            '                "start_continuous_scroll: unknown direction %r; '
            'sending "\n'
        ),
        "new": (
            "        normalized = direction\n"
            '        if normalized not in ("up", "down", "left", "right"):\n'
            "            logger.warning(\n"
            '                "start_continuous_scroll: unknown direction %r; '
            'sending "\n'
        ),
        "expect": [
            "TestStartPayload::test_case_and_spacing_are_normalised",
        ],
    },
    {
        "name": "every-direction-sends-down",
        "also_fails": {
            "TestStartPayload::test_case_and_spacing_are_normalised": (
                "the test reads the direction back out of the payload to check "
                "its normalised form, so a payload that always says down fails "
                "it without showing whether case and spacing are handled"
            ),
        },
        "target": ACTIONS,
        "old": '            "params": {"direction": normalized},\n',
        "new": '            "params": {"direction": "down"},\n',
        "expect": [
            "TestStartPayload::test_each_direction_builds_its_own_payload",
        ],
    },
    {
        # The name in the registry is the name patterns.toml calls, so a
        # rename here is a silent break of the contract between the two
        # files rather than a visible error.
        "name": "start-registered-under-another-name",
        "target": ACTIONS,
        "old": (
            '        self._functions["start_continuous_scroll"] = '
            "self.start_continuous_scroll\n"
        ),
        "new": (
            '        self._functions["start_scrolling"] = '
            "self.start_continuous_scroll\n"
        ),
        "expect": [
            "TestContinuousScrollRegistration::test_start_is_registered_under_the_name_patterns_use",
        ],
    },
    # ---------------------------------------------------------------
    # The shipped pattern entries, beyond the anchor and the flag
    # (added for wh-voice-access-parity.2.3.3.2.3).
    # ---------------------------------------------------------------
    {
        # A hotword requirement would put a prefix in front of every scroll
        # command, which is the thing this feature exists to avoid.
        "name": "stop-entry-needs-a-hotword",
        "target": PATTERNS,
        "old": (
            "pattern = '''^stop scrolling$'''\n"
            'doc_id = "scroll-stop"\n'
            "whole_utterance_only = true\n"
        ),
        "new": (
            "pattern = '''^stop scrolling$'''\n"
            'doc_id = "scroll-stop"\n'
            "whole_utterance_only = true\n"
            "requires_hotword = true\n"
        ),
        "expect": [
            "TestContinuousScrollFlagsAndActions::test_the_entry_needs_no_hotword",
        ],
    },
    {
        # The direction each start entry passes is the only thing that tells
        # the four of them apart, so a copied argument scrolls the wrong way
        # with nothing else looking wrong.
        "name": "start-entry-passes-the-wrong-direction",
        "target": PATTERNS,
        "old": (
            '    { function = "start_continuous_scroll", params = ["left"] }\n'
        ),
        "new": (
            '    { function = "start_continuous_scroll", params = ["down"] }\n'
        ),
        "expect": [
            "TestContinuousScrollFlagsAndActions::test_each_start_entry_passes_its_own_direction",
        ],
    },
    # ---------------------------------------------------------------
    # The discrete scroll's stop, and what it must not cost
    # (added for wh-voice-access-parity.2.3.3.2.3).
    # ---------------------------------------------------------------
    {
        # The helper is what makes the stop never raise. Calling the scroller
        # directly puts a raising stop in front of the notches the user asked
        # for, and there is no outer try in scroll_wheel to catch it.
        "name": "a-failing-stop-loses-the-scroll",
        "target": HANDLER,
        "old": (
            "        self._stop_continuous_scroll("
            '"a discrete scroll command arrived")\n'
        ),
        "new": "        self._continuous_scroller.stop()\n",
        "expect": [
            "TestADiscreteScrollStopsTheContinuousOne::test_a_failing_stop_does_not_lose_the_discrete_scroll",
        ],
    },
    # ---------------------------------------------------------------
    # Stopping when nothing is running, and reporting the state
    # (added for wh-voice-access-parity.2.3.3.2.4, which showed the
    # exclusions here cited a mutation that cannot reach these tests).
    # ---------------------------------------------------------------
    {
        # The second spoken stop must answer False. Answering True makes the
        # Input process log a stop that did not happen.
        "name": "no-run-stop-claims-success",
        "target": SCROLLER,
        "old": (
            "            return False\n"
            "        run.stop_event.set()\n"
        ),
        "new": (
            "            return True\n"
            "        run.stop_event.set()\n"
        ),
        "expect": [
            "TestStartAndStop::test_stopping_a_scroll_that_never_started_is_harmless",
        ],
    },
    {
        "name": "is-running-inverted",
        "also_fails": {
            "TestShutdownStopsTheScroll::test_a_start_after_shutdown_turns_no_wheel": (
                "the test reads is_running() as a plain statement of fact on "
                "either side of the shutdown, so an inverted answer fails it "
                "whatever the shutdown behaviour is"
            ),
            "TestShutdownStopsTheScroll::test_no_shutdown_signal_leaves_the_scroll_running": (
                "the test reads is_running() as a plain statement of fact on "
                "either side of the shutdown, so an inverted answer fails it "
                "whatever the shutdown behaviour is"
            ),
            "TestAThreadTheMachineWillNotStart::test_a_refused_thread_start_leaves_nothing_running": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "TestAThreadTheMachineWillNotStart::test_a_stop_in_the_gap_before_the_thread_runs_does_not_raise": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "test_the_command_loop_stays_free_while_a_continuous_scroll_runs": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "TestABrokenWheelCall::test_a_refused_wheel_call_ends_the_scroll": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "TestABrokenWheelCall::test_a_seam_that_reports_nothing_keeps_scrolling": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "TestABrokenWheelCall::test_one_refused_tick_does_not_end_the_scroll": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "TestAStopTheUserAskedForStaysSilent::test_a_stop_at_the_maximum_duration_says_nothing": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "TestAStopTheUserAskedForStaysSilent::test_a_stop_during_the_last_refused_tick_says_nothing": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "TestAStopTheUserAskedForStaysSilent::test_a_stop_while_the_wheel_call_raises_says_nothing": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "TestStartAndStop::test_an_unusable_direction_starts_nothing": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "TestStartAndStop::test_stopping_a_scroll_that_never_started_is_harmless": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "TestStartAndStop::test_stopping_ends_the_turns": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
            "TestTheCommandLoopStaysFree::test_start_returns_while_the_wheel_call_is_still_running": (
                "the test calls is_running() as a wait condition or as a closing sanity "
                "check, never as its own subject"
            ),
        },
        "target": SCROLLER,
        "old": "            return self._run is not None\n",
        "new": "            return self._run is None\n",
        "expect": [
            "TestStartAndStop::test_is_running_reports_the_state",
        ],
    },
    # ---------------------------------------------------------------
    # What the handler builds at construction
    # (added for wh-voice-access-parity.2.3.3.2.4).
    # ---------------------------------------------------------------
    {
        "name": "handler-builds-no-scroller",
        "also_fails": {
            "TestTheHandlerPassesItsShutdownSignal::test_a_shutdown_stops_a_scroll_the_handler_started": (
                "the handler holds no scroller, so its start does nothing and "
                "the test's first wait for a wheel turn is what fails; the "
                "signal handoff is never reached"
            ),
            "TestTheHandlerOwnsOneScroller::test_the_scroller_turns_the_wheel_through_the_same_seam": (
                "there is no scroller to reach the seam through, so the test "
                "falls over on the missing object and says nothing about which "
                "seam a scroller would have used"
            ),
        },
        "target": HANDLER,
        # Refreshed for wh-voice-access-parity.2.3.3.2.10, which put the
        # shutdown signal into this same call and so moved it onto three
        # lines. The behaviour this mutation removes is unchanged, which is
        # why the pattern was refreshed rather than the mutation dropped.
        "old": (
            "        self._continuous_scroller = ContinuousScroller(\n"
            "            self._turn_the_wheel, shutdown_event=shutdown_event,\n"
            "        )\n"
        ),
        "new": "        self._continuous_scroller = None\n",
        "expect": [
            "TestTheHandlerOwnsOneScroller::test_the_handler_builds_a_scroller_at_construction",
        ],
    },
    {
        # The scroller must reach the wheel through the handler's own seam
        # attribute, so a test that replaces that attribute is not silently
        # bypassed by the continuous path.
        "name": "scroller-gets-its-own-seam",
        "also_fails": {
            "TestTheHandlerPassesItsShutdownSignal::test_a_shutdown_stops_a_scroll_the_handler_started": (
                "the scroller turns a stand-in wheel instead of the handler's "
                "own, so this test's recording wheel never sees a call and its "
                "first wait is what fails"
            ),
        },
        "target": HANDLER,
        # Refreshed with the mutation above, for the same reason: the call
        # gained the shutdown signal and moved onto three lines
        # (wh-voice-access-parity.2.3.3.2.10). The signal is kept in the
        # replacement so this mutation still changes only the seam.
        "old": (
            "        self._continuous_scroller = ContinuousScroller(\n"
            "            self._turn_the_wheel, shutdown_event=shutdown_event,\n"
            "        )\n"
        ),
        "new": (
            "        self._continuous_scroller = ContinuousScroller(\n"
            "            lambda direction, clicks: (True, None),\n"
            "            shutdown_event=shutdown_event,\n"
            "        )\n"
        ),
        "expect": [
            "TestTheHandlerOwnsOneScroller::test_the_scroller_turns_the_wheel_through_the_same_seam",
        ],
    },
    {
        "name": "stop-registered-under-another-name",
        "target": ACTIONS,
        "old": (
            '        self._functions["stop_continuous_scroll"] = '
            "self.stop_continuous_scroll\n"
        ),
        "new": (
            '        self._functions["stop_scrolling"] = '
            "self.stop_continuous_scroll\n"
        ),
        "expect": [
            "TestContinuousScrollRegistration::test_stop_is_registered_under_the_name_patterns_use",
        ],
    },
    # ---------------------------------------------------------------
    # Which entry a spoken phrase reaches, and how many entries there are
    # (added for wh-voice-access-parity.2.3.3.2.4).
    # ---------------------------------------------------------------
    {
        # Mutating the STOP entry cannot catch the first-match test: that
        # test reads its expected value from the same entry, so both sides
        # move together. Loosening a START entry is what separates them --
        # "stop scrolling" then reaches the up entry first, while the
        # expected value still comes from the stop entry.
        "name": "a-start-entry-swallows-other-phrases",
        "target": PATTERNS,
        "old": "pattern = '''^start scrolling up$'''\n",
        "new": "pattern = '''.*scrolling'''\n",
        # The loosened entry really does swallow a dictated sentence that
        # ends in "scrolling", so the dictation test fails for its own
        # named behaviour rather than as collateral.
        "expect": [
            "TestContinuousScrollDoesNotShadowDictation::test_a_longer_phrase_does_not_reach_a_continuous_entry",
            "TestContinuousScrollFirstMatchOrder::test_the_spoken_form_reaches_its_own_entry",
        ],
    },
    {
        "name": "stop-entry-takes-an-argument",
        "target": PATTERNS,
        "old": (
            '    { function = "stop_continuous_scroll", params = [] }\n'
        ),
        "new": (
            '    { function = "stop_continuous_scroll", params = ["down"] }\n'
        ),
        "expect": [
            "TestContinuousScrollFlagsAndActions::test_the_stop_entry_takes_no_arguments",
        ],
    },
    {
        # _entry asserts exactly one match per doc_id, so a duplicated entry
        # is what that test is for. A duplicate is also the realistic edit: a
        # copied block with its doc_id left alone.
        "name": "stop-entry-appears-twice",
        "also_fails": {
            "TestContinuousScrollDoesNotShadowDictation::test_a_longer_phrase_does_not_reach_a_continuous_entry": (
                "the test calls the _entry helper, whose assert of exactly one match "
                "per doc_id fires on the duplicate before the test reaches its own "
                "subject (tests/test_continuous_scroll_patterns.py:73)"
            ),
            "TestContinuousScrollFirstMatchOrder::test_the_spoken_form_reaches_its_own_entry": (
                "the test calls the _entry helper, whose assert of exactly one match "
                "per doc_id fires on the duplicate before the test reaches its own "
                "subject (tests/test_continuous_scroll_patterns.py:73)"
            ),
            "TestContinuousScrollFlagsAndActions::test_the_entry_is_whole_utterance_only": (
                "the test calls the _entry helper, whose assert of exactly one match "
                "per doc_id fires on the duplicate before the test reaches its own "
                "subject (tests/test_continuous_scroll_patterns.py:73)"
            ),
            "TestContinuousScrollFlagsAndActions::test_the_entry_needs_no_hotword": (
                "the test calls the _entry helper, whose assert of exactly one match "
                "per doc_id fires on the duplicate before the test reaches its own "
                "subject (tests/test_continuous_scroll_patterns.py:73)"
            ),
            "TestContinuousScrollFlagsAndActions::test_the_stop_entry_takes_no_arguments": (
                "the test calls the _entry helper, whose assert of exactly one match "
                "per doc_id fires on the duplicate before the test reaches its own "
                "subject (tests/test_continuous_scroll_patterns.py:73)"
            ),
        },
        "target": PATTERNS,
        "old": (
            "[[pattern]]\n"
            "pattern = '''^stop scrolling$'''\n"
            'doc_id = "scroll-stop"\n'
            "whole_utterance_only = true\n"
            "actions = [\n"
            '    { function = "stop_continuous_scroll", params = [] }\n'
            "]\n"
        ),
        "new": (
            "[[pattern]]\n"
            "pattern = '''^stop scrolling$'''\n"
            'doc_id = "scroll-stop"\n'
            "whole_utterance_only = true\n"
            "actions = [\n"
            '    { function = "stop_continuous_scroll", params = [] }\n'
            "]\n"
            "\n"
            "[[pattern]]\n"
            "pattern = '''^stop scrolling$'''\n"
            'doc_id = "scroll-stop"\n'
            "whole_utterance_only = true\n"
            "actions = [\n"
            '    { function = "stop_continuous_scroll", params = [] }\n'
            "]\n"
        ),
        "expect": [
            "TestContinuousScrollEntriesExist::test_each_command_has_exactly_one_entry",
        ],
    },
    # ---------------------------------------------------------------
    # Who is allowed to report an end
    # (added for wh-voice-access-parity.2.3.3.2.5).
    # ---------------------------------------------------------------
    {
        # Deleting the ownership check is the plain revert: the timer
        # thread reports every automatic end it reaches, including the
        # ones it reaches after the user's own stop took the run.
        "name": "a-stopped-run-still-reports",
        "target": SCROLLER,
        "old": (
            "        if not self._clear_if_current(run):\n"
            "            logger.info(\n"
            "                \"continuous scroll: this run was already stopped, so it says \"\n"
            "                \"nothing about the end it reached (%s)\", message,\n"
            "            )\n"
            "            return\n"
        ),
        "new": "        self._clear_if_current(run)\n",
        "expect": [
            "TestAStopTheUserAskedForStaysSilent::test_a_stop_at_the_maximum_duration_says_nothing",
            "TestAStopTheUserAskedForStaysSilent::test_a_stop_during_the_last_refused_tick_says_nothing",
            "TestAStopTheUserAskedForStaysSilent::test_a_stop_while_the_wheel_call_raises_says_nothing",
        ],
    },
    {
        # And the input-level break: the ownership answer itself. The
        # check stays, the run is still cleared when it is current, but
        # the answer is always yes, so every end is reported as this
        # run's to report.
        "name": "ownership-always-granted",
        "target": SCROLLER,
        "old": (
            "            if self._run is run:\n"
            "                self._run = None\n"
            "                return True\n"
            "        return False\n"
        ),
        "new": (
            "            if self._run is run:\n"
            "                self._run = None\n"
            "        return True\n"
        ),
        "expect": [
            "TestAStopTheUserAskedForStaysSilent::test_a_stop_at_the_maximum_duration_says_nothing",
            "TestAStopTheUserAskedForStaysSilent::test_a_stop_during_the_last_refused_tick_says_nothing",
            "TestAStopTheUserAskedForStaysSilent::test_a_stop_while_the_wheel_call_raises_says_nothing",
        ],
    },
    # ---------------------------------------------------------------
    # wh-voice-access-parity.2.3.3.2.8: a thread the machine refuses.
    # The run is installed BEFORE the thread starts, so both halves of
    # the answer need their own mutation: the roll-back, and the join
    # that must not touch a thread which never started.
    # ---------------------------------------------------------------
    {
        # The roll-back removed, the notice kept. Splitting it this way
        # is what proves the tests read two behaviours rather than one
        # call: this mutant still tells the user, so the notice test
        # stays green while the two state tests go red.
        "name": "start-failure-not-rolled-back",
        "target": SCROLLER,
        "old": "            self._stop_and_say(run, START_FAILED_MESSAGE)\n",
        "new": "            self._notifier(NOTICE_TITLE, START_FAILED_MESSAGE)\n",
        "expect": [
            "TestAThreadTheMachineWillNotStart::test_a_refused_thread_start_leaves_nothing_running",
        ],
    },
    {
        # The other half: the roll-back kept, the notice removed. The
        # user asked for a scroll, got none, and is told nothing.
        "name": "start-failure-says-nothing",
        "target": SCROLLER,
        "old": "            self._stop_and_say(run, START_FAILED_MESSAGE)\n",
        "new": "            self._clear_if_current(run)\n",
        "expect": [
            "TestAThreadTheMachineWillNotStart::test_a_refused_thread_start_tells_the_user",
        ],
        "also_fails": {
            "TestOneNotificationPerFailure::test_a_refused_thread_start_shows_one_notification": (
                "this mutation removes the start-failure notice, so the "
                "test loses the one box it asserts on before it can say "
                "anything about the second one"
            ),
        },
    },
    {
        # The answer the handler acts on. A refused start that reports
        # success is the state the finding described: the action is not
        # in input_proc's _HANDLES_OWN_RESPONSE, so the Input process
        # sends the generic success and nothing scrolls.
        "name": "start-failure-reports-success",
        "target": SCROLLER,
        "old": (
            "            self._stop_and_say(run, START_FAILED_MESSAGE)\n"
            "            return False\n"
        ),
        "new": (
            "            self._stop_and_say(run, START_FAILED_MESSAGE)\n"
            "            return True\n"
        ),
        "expect": [
            "TestAThreadTheMachineWillNotStart::test_a_refused_thread_start_leaves_nothing_running",
        ],
    },
    {
        # The join guard. Thread.ident is None for a thread that has
        # not been started and for no other state, so dropping this
        # term is exactly the RuntimeError the gap test reproduces.
        # The term is replaced rather than deleted, because deleting
        # the only line of a boolean chain would be a syntax edit.
        "name": "join-an-unstarted-thread",
        "target": SCROLLER,
        "old": "            and thread.ident is not None\n",
        "new": "            and True\n",
        "expect": [
            "TestAThreadTheMachineWillNotStart::test_a_stop_in_the_gap_before_the_thread_runs_does_not_raise",
        ],
    },
    {
        # The only two-site mutation in this gate, and the reason the
        # form exists. Recovery after a refused thread is guarded
        # twice: the roll-back never leaves the dead run behind, and
        # the join skips a thread that was never started. Remove one
        # and the other still recovers, so the recovery test survives
        # both single-site mutations while being genuinely protected.
        # Removing both restores the state the finding described: the
        # second start reaches _end_current_run, the join raises, and
        # the RuntimeError leaves start() through a path its own
        # try block does not cover.
        "name": "start-failure-neither-half",
        "edits": [
            {
                "target": SCROLLER,
                "old": "            self._stop_and_say(run, START_FAILED_MESSAGE)\n",
                "new": "            self._notifier(NOTICE_TITLE, START_FAILED_MESSAGE)\n",
            },
            {
                "target": SCROLLER,
                "old": "            and thread.ident is not None\n",
                "new": "            and True\n",
            },
        ],
        "expect": [
            "TestAThreadTheMachineWillNotStart::test_the_next_start_works_after_a_refused_thread_start",
        ],
        "also_fails": {
            "TestAThreadTheMachineWillNotStart::test_a_refused_thread_start_leaves_nothing_running": (
                "the missing roll-back alone breaks this one, and its "
                "own mutation says so; here it falls over on the same "
                "state without adding anything"
            ),
            "TestAThreadTheMachineWillNotStart::test_a_stop_in_the_gap_before_the_thread_runs_does_not_raise": (
                "the missing join guard alone breaks this one, and its "
                "own mutation says so; here the same RuntimeError "
                "reaches it through start()"
            ),
        },
    },
    # ---------------------------------------------------------------
    # wh-voice-access-parity.2.3.3.2.9: one notification per failure.
    #
    # Each of the three automatic ends logs its line and then sends the
    # purpose-written notice. At ERROR the record ALSO reaches
    # ErrorNotificationHandler, which is attached to the root logger by
    # setup_logging and submits to the same NotifierWorker send_notice
    # uses, so the user gets a generic box beside the written one and a
    # screen reader reads the generic one first. Each mutation below puts
    # one call back to ERROR. The level is the whole behaviour here, so
    # the level is what is mutated.
    # ---------------------------------------------------------------
    {
        "name": "start-failure-logs-at-error",
        "target": SCROLLER,
        "old": (
            "            logger.warning(\n"
            '                "continuous scroll: the timer thread would not '
            'start (%s)",\n'
        ),
        "new": (
            "            logger.error(\n"
            '                "continuous scroll: the timer thread would not '
            'start (%s)",\n'
        ),
        "expect": [
            "TestOneNotificationPerFailure::test_a_refused_thread_start_shows_one_notification",
        ],
    },
    {
        "name": "wheel-raise-logs-at-error",
        "target": SCROLLER,
        "old": (
            "                logger.warning(\n"
            '                    "continuous scroll: the wheel call failed; '
            'stopping the "\n'
        ),
        "new": (
            "                logger.error(\n"
            '                    "continuous scroll: the wheel call failed; '
            'stopping the "\n'
        ),
        "expect": [
            "TestOneNotificationPerFailure::test_a_raising_wheel_call_shows_one_notification",
        ],
    },
    {
        "name": "wheel-refusal-logs-at-error",
        "target": SCROLLER,
        "old": (
            "                    logger.warning(\n"
            '                        "continuous scroll: %d refused wheel '
            'calls in a row "\n'
        ),
        "new": (
            "                    logger.error(\n"
            '                        "continuous scroll: %d refused wheel '
            'calls in a row "\n'
        ),
        "expect": [
            "TestOneNotificationPerFailure::test_a_refused_wheel_call_shows_one_notification",
        ],
    },
    # ---------------------------------------------------------------
    # wh-voice-access-parity.2.3.3.2.10: the scroll ends when the Input
    # process is asked to close. The timer thread is a daemon, so it dies
    # with the process -- but the launcher gives the process
    # SHUTDOWN_GRACE_PERIOD_S (five seconds) to leave first, and the
    # command loop reads the shutdown signal only in its loop condition,
    # which a blocked handler keeps it from reaching. Without a check of
    # its own the wheel keeps turning through that whole window.
    # ---------------------------------------------------------------
    {
        "name": "shutdown-ignored-in-the-tick",
        "target": SCROLLER,
        "old": (
            "            if self._shutting_down():\n"
            "                self._stop_for_shutdown(run)\n"
            "                return\n"
        ),
        "new": (
            "            if False:\n"
            "                self._stop_for_shutdown(run)\n"
            "                return\n"
        ),
        "expect": [
            "TestShutdownStopsTheScroll::test_a_signalled_shutdown_ends_the_scroll",
            "TestShutdownStopsTheScroll::test_a_shutdown_end_says_nothing_to_the_user",
            "TestTheHandlerPassesItsShutdownSignal::test_a_shutdown_stops_a_scroll_the_handler_started",
            "test_a_blocked_loop_does_not_stop_a_scroll_from_ending_at_shutdown",
        ],
    },
    {
        "name": "start-during-shutdown-allowed",
        "target": SCROLLER,
        "old": (
            "        if self._shutting_down():\n"
            "            logger.info(\n"
            '                "continuous scroll: the process is closing; '
            'starting nothing"\n'
        ),
        "new": (
            "        if False:\n"
            "            logger.info(\n"
            '                "continuous scroll: the process is closing; '
            'starting nothing"\n'
        ),
        "expect": [
            "TestShutdownStopsTheScroll::test_a_start_after_shutdown_turns_no_wheel",
            "TestShutdownStopsTheScroll::test_a_shutdown_signal_that_raises_ends_the_scroll",
        ],
    },
    {
        "name": "unreadable-shutdown-signal-ignored",
        "target": SCROLLER,
        "old": (
            '                "stopping the scroll", exc_info=True,\n'
            "            )\n"
            "            return True\n"
        ),
        "new": (
            '                "stopping the scroll", exc_info=True,\n'
            "            )\n"
            "            return False\n"
        ),
        "expect": [
            "TestShutdownStopsTheScroll::test_a_shutdown_signal_that_raises_ends_the_scroll",
        ],
    },
    {
        "name": "shutdown-end-says-something",
        "target": SCROLLER,
        "old": (
            "        logger.info(\n"
            '            "continuous scroll: stopping because the process is '
            'closing"\n'
            "        )\n"
            "        self._clear_if_current(run)\n"
        ),
        "new": (
            "        logger.info(\n"
            '            "continuous scroll: stopping because the process is '
            'closing"\n'
            "        )\n"
            "        self._stop_and_say(run, AUTO_STOP_MESSAGE)\n"
        ),
        "expect": [
            "TestShutdownStopsTheScroll::test_a_shutdown_end_says_nothing_to_the_user",
            "test_a_blocked_loop_does_not_stop_a_scroll_from_ending_at_shutdown",
        ],
    },
    {
        "name": "loop-keeps-its-shutdown-signal",
        "target": INPUT_PROC,
        "old": (
            "        ui_handler = UIActionHandler(\n"
            "            response_queue, config, shutdown_event=shutdown_event,\n"
            "        )\n"
        ),
        "new": (
            "        ui_handler = UIActionHandler(\n"
            "            response_queue, config,\n"
            "        )\n"
        ),
        "expect": [
            "test_the_loop_hands_its_shutdown_signal_to_the_handler",
        ],
    },
    {
        "name": "handler-keeps-its-shutdown-signal",
        "target": HANDLER,
        "old": (
            "        self._continuous_scroller = ContinuousScroller(\n"
            "            self._turn_the_wheel, shutdown_event=shutdown_event,\n"
            "        )\n"
        ),
        "new": (
            "        self._continuous_scroller = ContinuousScroller(\n"
            "            self._turn_the_wheel,\n"
            "        )\n"
        ),
        "expect": [
            "TestTheHandlerPassesItsShutdownSignal::test_a_shutdown_stops_a_scroll_the_handler_started",
        ],
    },
]

# Every test in the five files below that NO mutation breaks, with the reason
# it has none. The runner refuses to start when a collected test appears in
# neither a mutation's ``expect`` list nor this mapping, and when a name here
# no longer exists or is also expected by a mutation.
#
# This is machine-checked on purpose. Two review rounds in a row filed the
# same finding against a PROSE version of this list -- it named some of the
# unmutated tests and read as though it named all of them
# (wh-voice-access-parity.2.3.3.1.1, then .2.3.3.2.3 after the prose was
# rewritten). A sentence promising completeness cannot be wrong loudly. This
# mapping can, and it does so before a single mutation runs.
NOT_MUTATED = {
    SCROLLER_TESTS: {
        "TestTheCommandLoopStaysFree::test_start_returns_while_the_wheel_call_is_still_running": (
            "every way of breaking this -- ticking inline, joining the new "
            "thread -- leaves a mutant sitting in the tick loop until the "
            "maximum duration, which is the never-terminates failure the "
            "mutation-gate skill warns about: it proves nothing and costs the "
            "rest of the run"
        ),
        "TestAutoStop::test_a_spoken_stop_says_nothing": (
            "the test asserts a notifier call was NOT made, and no deletion "
            "can break an assertion about something not happening; the only "
            "mutation would ADD a call, which is not a mutation of shipped "
            "behaviour"
        ),
        "TestShutdownStopsTheScroll::test_no_shutdown_signal_leaves_the_scroll_running": (
            "the branch it guards is the default every other test in this "
            "file relies on, so the one edit that would break it on purpose "
            "-- reading a missing signal as a shutdown -- also breaks nearly "
            "every test here, and its failure set would say almost nothing "
            "about this test. Two mutations already break it by accident and "
            "declare it as collateral. It is named so a reader can see the "
            "default is deliberate rather than accidental"
        ),
    },
    HANDLER_TESTS: {
        "TestStartHandler::test_it_emits_no_response": (
            "silence asserted by assert_not_called, for the same reason as "
            "the spoken stop above: there is no call to delete"
        ),
        "TestStopHandler::test_it_emits_no_response": (
            "silence asserted by assert_not_called; no call to delete"
        ),
        "TestStartHandler::test_it_never_raises_when_the_scroller_fails": (
            "the behaviour is a try block, and deleting one is a syntax edit "
            "rather than a behaviour edit: the mutant does not compile, which "
            "the skill counts as an error, not a catch"
        ),
        "TestStopHandler::test_it_never_raises_when_the_scroller_fails": (
            "the same try block one handler over; deleting it does not compile"
        ),
        "TestStartHandler::test_it_never_raises_on_a_malformed_message": (
            "the same try block; deleting it does not compile"
        ),
        "TestADiscreteScrollStopsTheContinuousOne::test_the_discrete_notches_are_still_sent": (
            "the discrete wheel call and its notch count are behaviour this "
            "feature preserved rather than wrote, so a mutation of them would "
            "be about code outside this bead"
        ),
    },
    EXPIRY_TESTS: {
        "test_the_command_loop_stays_free_while_a_continuous_scroll_runs": (
            "the same never-terminates hazard as the scroller-level test of "
            "acceptance item 2 above"
        ),
    },
}

# The expiry file belongs to the input-loop containment work and holds many
# tests this feature never touched. These three are the feature's, and the
# runner refuses to start if a fourth test whose name mentions scrolling
# appears there without being listed -- otherwise a new scroll test could be
# added to that file and escape the completeness check above.
EXPIRY_FEATURE_TESTS = {
    "test_the_command_loop_stays_free_while_a_continuous_scroll_runs",
    "test_a_stop_command_dropped_as_expired_still_stops_the_scroll",
    "test_an_expired_start_scroll_command_is_still_dropped",
    # wh-voice-access-parity.2.3.3.2.10. The second name carries no "scroll",
    # so the stray check above would not have caught its absence and it would
    # have escaped the coverage requirement in silence. Listed for that
    # reason as much as for the coverage itself.
    "test_a_blocked_loop_does_not_stop_a_scroll_from_ending_at_shutdown",
    "test_the_loop_hands_its_shutdown_signal_to_the_handler",
}

FEATURE_TEST_FILES = (
    SCROLLER_TESTS, HANDLER_TESTS, ACTION_TESTS, PATTERN_TESTS, EXPIRY_TESTS,
)


def _edits(mutation):
    """Every (target, old, new) this mutation applies, in order.

    Most mutations are one edit and carry target/old/new directly. A
    mutation may instead carry "edits", a list of dicts of the same
    three keys, when the behaviour it removes is guarded in two places
    at once. One edit could then not break the test at all, because the
    other guard still holds, and the gate would report a survivor for a
    test that is protected twice over rather than untested
    (wh-voice-access-parity.2.3.3.2.8).

    Reach for it only for that case. A mutation that edits two places
    because it is really two mutations tells the reader less, not more:
    the failures it produces can no longer be attributed to one change.
    """
    if "edits" in mutation:
        return [
            (e["target"], e["old"], e["new"])
            for e in mutation["edits"]
        ]
    return [(mutation["target"], mutation["old"], mutation["new"])]


def _run_pytest(test_files):
    """Run every named test file in ONE pytest process.

    Every mutation runs the whole feature suite rather than the one file
    the behaviour lives in. A mutation of the scroller breaks tests in
    the containment file too, because the expiry tests drive a real
    ContinuousScroller through the real command loop, and a single-file
    run cannot see that. Codex round 4 found three mutations whose real
    failure sets already reached across files, so the exact-set check was
    exact only inside one file (wh-voice-access-parity.2.3.3.2.7).
    """
    env = dict(os.environ)
    # Python decides whether cached bytecode is current from the source
    # file's (mtime, size). Several mutations here keep the file the same
    # length, so the cache is disabled rather than relied on.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", *test_files, "-q", "-rf", "-p",
         "no:cacheprovider"],
        cwd=SERVICE_DIR, env=env, capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S,
    )


def _node_key(node_id):
    """The part of a pytest node id after the file path, without parameters.

    A key is ``Class::method`` for a class-based test and ``method`` for a
    module-level one. The bare method name is NOT enough: this feature's
    handler tests put test_it_emits_no_response,
    test_it_never_raises_when_the_scroller_fails and
    test_it_ignores_unexpected_extra_fields in two classes each, so keying by
    the last segment made one entry stand for two different tests
    (wh-voice-access-parity.2.3.3.2.4).
    """
    return node_id.split("::", 1)[1].split("[")[0].strip()


def _failed_names(output):
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            node_id = line.split(" ", 1)[1].split(" ")[0]
            if "::" in node_id:
                names.add(_node_key(node_id))
    return names


def _coverage_errors(existing):
    """Check that every feature test is either mutated or excluded by name.

    This is the machine-checked half of the honest-boundary rule. A gate can
    only claim to know what it does NOT cover if something refuses to run
    when that claim stops being true.

    Eight ways it can stop being true, all reported here:

    1. A test exists that no mutation breaks and NOT_MUTATED does not name.
       That is the over-claim itself.
    2. NOT_MUTATED names a test that no longer exists, so the reason is now
       about nothing and the list reads longer than it is.
    3. A name is in NOT_MUTATED and in a mutation's ``expect`` list, so the
       file says both "covered" and "deliberately not covered".
    4. The expiry file grew a scroll test that EXPIRY_FEATURE_TESTS does not
       name, which would let a feature test live outside this check.
    5. A reason cites another mutation by name. Codex round 2 found eight
       reasons of the form "already broken by <mutation>" that were simply
       false -- the cited mutant could not fail the named test, because it
       changed a line that test never reaches
       (wh-voice-access-parity.2.3.3.2.4). That claim is unverifiable where it
       is written and unchecked where it is read, so the shape is banned
       rather than the eight instances corrected. A reason must state a
       property of the behaviour itself: silence cannot be broken by deleting
       a call, the only mutation would not compile, the only mutation would
       never terminate, or the behaviour belongs to code outside this bead.
    6. An ``also_fails`` key names a test that does not exist, or carries an
       empty reason.
    7. A name is in a mutation's ``also_fails`` and in the same mutation's
       ``expect``. The two mean opposite things about the same run: one says
       the test proves the behaviour, the other says it only fell over.
    8. The same ``Class::method`` key exists in two feature test files.
       Every mutation now runs all five files in one pytest process, and
       a failure is reported by that key with the path stripped, so a
       shared key would make one failure unattributable. No such pair
       exists today; this refuses the run if one is ever added.

    ``also_fails`` names do NOT count as covered here, and that is the whole
    point of keeping them out of ``expect``. A test that only ever fails as
    collateral has not been exercised by this gate, so it still owes either a
    mutation that breaks it for its own behaviour or a NOT_MUTATED entry
    (wh-voice-access-parity.2.3.3.2.6). A name may therefore sit in
    NOT_MUTATED and in some mutation's ``also_fails`` at once, and
    test_the_command_loop_stays_free_while_a_continuous_scroll_runs does:
    two mutations knock it over without exercising what it names, and no
    mutation exercises what it names, which is exactly what both entries
    say.
    """
    errors = []
    covered = {name for m in MUTATIONS for name in m["expect"]}
    mutation_names = {m["name"] for m in MUTATIONS}
    every_test = set().union(*existing.values())

    # A run over all five files reports a failure by its Class::method
    # key with the path stripped, so two files holding the same key would
    # make one failure unattributable. No such pair exists today; this
    # refuses the run if one is ever added.
    seen = {}
    for test_file in FEATURE_TEST_FILES:
        for name in existing[test_file]:
            if name in seen:
                errors.append(
                    f"{name} exists in both {seen[name]} and "
                    f"{test_file}; a combined run cannot tell which "
                    "one failed"
                )
            else:
                seen[name] = test_file

    for mutation in MUTATIONS:
        both = sorted(set(mutation.get("also_fails", {})) & set(mutation["expect"]))
        if both:
            errors.append(
                f"{mutation['name']}: {both} are in both expect and "
                "also_fails; a test cannot both prove the behaviour and "
                "merely fall over"
            )
        for name, reason in mutation.get("also_fails", {}).items():
            if not reason.strip():
                errors.append(
                    f"{mutation['name']}: also_fails entry {name} has no "
                    "reason; say why the test fails without proving anything"
                )
            if name not in every_test:
                errors.append(
                    f"{mutation['name']}: also_fails names {name}, which "
                    "does not exist in any feature test file"
                )

    for test_file, excluded in NOT_MUTATED.items():
        for name, reason in excluded.items():
            cited = sorted(n for n in mutation_names if n in reason)
            if cited:
                errors.append(
                    f"{test_file}: the reason for {name} names {cited}; a "
                    "reason may not claim another mutation covers a test"
                )

    for test_file in FEATURE_TEST_FILES:
        collected = existing[test_file]
        excluded = NOT_MUTATED.get(test_file, {})
        if test_file == EXPIRY_TESTS:
            stray = {
                name for name in collected
                if "scroll" in name and name not in EXPIRY_FEATURE_TESTS
            }
            for name in sorted(stray):
                errors.append(
                    f"{test_file}: {name} looks like a continuous-scroll test "
                    "but is not in EXPIRY_FEATURE_TESTS"
                )
            collected = collected & EXPIRY_FEATURE_TESTS

        for name in sorted(collected - covered - set(excluded)):
            errors.append(
                f"{test_file}: {name} is neither broken by a mutation nor "
                "listed in NOT_MUTATED with a reason"
            )
        for name in sorted(set(excluded) - existing[test_file]):
            errors.append(
                f"{test_file}: NOT_MUTATED names {name}, which no longer "
                "exists"
            )
        for name in sorted(set(excluded) & covered):
            errors.append(
                f"{test_file}: {name} is both expected by a mutation and "
                "listed in NOT_MUTATED"
            )
    return errors


def main(argv):
    # A name on the command line runs that mutation alone. The reason this
    # exists is the skill's own rule about not running the gate beside a test
    # suite in the same worktree: a suite that reads only some of these files
    # lets the mutations for the others run now and the rest run after. The
    # scope line at the end says which subset ran, so a partial run cannot be
    # read as a full sweep.
    selected = list(argv[1:])
    if selected:
        known = {m["name"] for m in MUTATIONS}
        unknown = [name for name in selected if name not in known]
        if unknown:
            print(f"ERROR: no such mutation: {unknown}")
            return 1
        mutations = [m for m in MUTATIONS if m["name"] in selected]
    else:
        mutations = list(MUTATIONS)

    targets = {
        target for mutation in mutations for target, _, _ in _edits(mutation)
    }
    originals = {}
    for target in targets:
        raw = target.read_bytes()
        if b"\r\n" in raw:
            # The patterns above are written with LF. A file stored with
            # CRLF makes every multi-line pattern miss in a batch, which
            # reads as a survivor. Refuse rather than rewrite the file's
            # endings. Checked per target: this repository can mix
            # conventions per file, so one clean target proves nothing
            # about the next.
            print(f"ERROR: {target.name} uses CRLF; patterns here are LF")
            return 1
        originals[target] = raw
    sources = {t: raw.decode("utf-8") for t, raw in originals.items()}

    errors, survivors, caught = [], [], []

    # Every expected test name must exist before the first mutation. A name
    # that no longer exists can never appear in the failed set, so a genuine
    # catch would be reported as a survivor.
    # Every feature test file is collected even on a single-mutation run,
    # because the completeness check below is a property of this gate file
    # rather than of the subset being run.
    existing = {}
    for test_file in FEATURE_TEST_FILES:
        collect = subprocess.run(
            [sys.executable, "-m", "pytest", test_file, "--collect-only", "-q",
             "-p", "no:cacheprovider"],
            cwd=SERVICE_DIR, capture_output=True, text=True,
            timeout=RUN_TIMEOUT_S,
        )
        existing[test_file] = {
            _node_key(line.strip())
            for line in collect.stdout.splitlines() if "::" in line
        }
    # A name is checked against every feature test file rather than one of
    # them, because a mutation's failures now reach across files.
    every_test = set().union(*existing.values())
    for mutation in mutations:
        for name in mutation["expect"]:
            if name not in every_test:
                errors.append(f"{mutation['name']}: no such test {name}")
    errors.extend(_coverage_errors(existing))
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    # A suite that is already red reports every mutation as caught for a
    # reason unrelated to the mutation. The baseline covers exactly what
    # each mutation run covers: all five feature test files together.
    baseline = _run_pytest(FEATURE_TEST_FILES)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print(f"baselines green; running {len(mutations)} mutations")

    try:
        for mutation in mutations:
            name = mutation["name"]
            # Each edit is applied to the working copy the previous one
            # produced, so two edits in the same file both land, and the
            # exactly-one-match rule is tested against the text the edit
            # really sees.
            working = dict(sources)
            refused = False
            for target, old, new in _edits(mutation):
                count = working[target].count(old)
                if count != 1:
                    errors.append(
                        f"{name}: pattern matched {count} times, need 1"
                    )
                    print(f"ERROR {name}: pattern matched {count} times")
                    refused = True
                    break
                working[target] = working[target].replace(old, new, 1)
            if refused:
                continue

            changed = {
                target: source for target, source in working.items()
                if source != sources[target]
            }
            for target, mutated in changed.items():
                if target.suffix == ".py":
                    try:
                        compile(mutated, str(target), "exec")
                    except SyntaxError as exc:
                        errors.append(
                            f"{name}: mutant does not compile: {exc}"
                        )
                        print(f"ERROR {name}: mutant does not compile")
                        refused = True
                        break
            if refused:
                continue

            for target, mutated in changed.items():
                target.write_bytes(mutated.encode("utf-8"))
            try:
                result = _run_pytest(FEATURE_TEST_FILES)
            except subprocess.TimeoutExpired:
                errors.append(f"{name}: mutant never terminated")
                print(f"ERROR {name}: mutant never terminated")
                continue
            finally:
                for target in changed:
                    target.write_bytes(originals[target])

            combined = result.stdout + result.stderr
            if "+++ Timeout +++" in combined:
                errors.append(f"{name}: suite-timeout abort, no verdict")
                print(f"ERROR {name}: suite-timeout abort")
                continue

            failed = _failed_names(combined)
            expect = set(mutation["expect"])
            collateral = set(mutation.get("also_fails", {}))
            missing = sorted(expect - failed)
            # The expect list is an EXACT claim, not a lower bound. Before
            # wh-voice-access-parity.2.3.3.2.6 the run only checked that
            # every expected name failed, so a mutation could also break
            # tests nobody had looked at and still print a clean "caught".
            # Those extra failures matter: a test that falls over on a
            # shared precondition has not established its own behaviour
            # under the mutation, and a reader counting failures would
            # think it had. So every failure must be declared, either as
            # proof (expect) or as collateral (also_fails, with the reason
            # it proves nothing).
            undeclared = sorted(failed - expect - collateral)
            stale = sorted(collateral - failed)
            if missing:
                survivors.append(f"{name}: expected failures missing: {missing}")
                print(f"SURVIVED {name}: {missing} did not fail")
            elif undeclared or stale:
                if undeclared:
                    errors.append(
                        f"{name}: these tests failed and are in neither "
                        f"expect nor also_fails: {undeclared}"
                    )
                    print(f"ERROR    {name}: undeclared failures {undeclared}")
                if stale:
                    errors.append(
                        f"{name}: also_fails names {stale}, which did not "
                        "fail; the declaration is stale"
                    )
                    print(f"ERROR    {name}: stale also_fails {stale}")
            else:
                caught.append(name)
                if collateral:
                    print(
                        f"caught   {name}: {sorted(expect)} "
                        f"(also, proving nothing: {sorted(collateral)})"
                    )
                else:
                    print(f"caught   {name}: {sorted(expect)}")
    finally:
        for target, raw in originals.items():
            target.write_bytes(raw)

    if len(mutations) == len(MUTATIONS):
        scope = f"all {len(MUTATIONS)} mutations for this feature, none skipped"
    else:
        ran = {m["name"] for m in mutations}
        skipped = [m["name"] for m in MUTATIONS if m["name"] not in ran]
        scope = (
            f"{len(mutations)} of {len(MUTATIONS)} mutations, named on the "
            f"command line. NOT RUN in this pass: {skipped}"
        )
    print(
        f"\nscope: {scope}."
        f"\ncaught {len(caught)}, survivors {len(survivors)}, errors {len(errors)}"
    )
    for line in survivors + errors:
        print(f"  {line}")
    return 0 if not survivors and not errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
