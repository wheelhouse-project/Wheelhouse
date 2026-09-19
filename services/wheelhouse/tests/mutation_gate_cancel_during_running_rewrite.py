"""Mutation gate for the running-rewrite cancel (wh-cancel-fix-running-rewrite).

WHY THIS GATE EXISTS. One of the thirteen tests in
tests/test_ai/test_cancel_during_running_rewrite.py was seen to fail before
the fix existed:

    TestCancelReachesTheActionWhileTheModelRuns
    ::test_cancel_spoken_during_the_call_prevents_the_paste
    Failed: timed out after 5.0s waiting for the cancel acknowledgement
    "Cancelling."

The other twelve were written after the fix, so they owe their red-first
proof to this file. Every protected behaviour is broken here at the level of
the INPUT the code depends on -- which guard decides the lane opens, which
list the deferred events go into, the end the loop takes them from, the name
the cancel action is looked up under, the two checks that stop the paste --
and not only by removing the lane.

WHAT THE BEAD ADDED, and therefore what this gate defends:

  the lane's own lifetime, which is one word-event turn
      the-lane-never-opens        the guard refuses every open, which is
                                  the state the bead found the code in
      the-lane-opens-outside-a-turn
                                  the guard allows an open while the
                                  processing loop is reading word_queue
                                  itself, so two readers take events
      the-turn-flag-is-never-set  the same refusal from the loop's side
      closing-the-lane-drops-the-deferred-events
                                  the events the lane took are thrown
                                  away instead of replayed, and nothing
                                  else would ever process them

  what the lane recognises, and what it does about it
      the-lane-ignores-the-recognizer
                                  every event is deferred and none is
                                  acted on, so the cancel waits for the
                                  paste exactly as it did before
      the-cancel-action-is-not-called
                                  the match is recognised and logged and
                                  nothing runs
      the-cancel-action-name-is-wrong
                                  a name no pattern carries, so no
                                  pattern is collected and no action is
                                  found
      the-recognizer-collects-words-before-the-hotword
                                  "cancel fix" dictated as ordinary text
                                  cancels the call; the shipped pattern
                                  carries requires_hotword = true

  the deferred events, which must keep the order word_queue would give
      the-deferred-events-are-dropped
                                  the lane reads them off the queue and
                                  keeps none
      the-lane-defers-in-reverse  they go to the front of the list
      the-deferred-events-are-replayed-last-first
                                  the loop takes them off the other end
      the-deferred-events-are-not-replayed
                                  the loop never looks at the list

  the two call sites, which is what makes the fix shared
      the-rewrite-does-not-open-the-lane
                                  _run_ai_text_transform runs the model
                                  await with no lane, so all five shipped
                                  rewrite triggers lose the fix at once
      ask-ai-does-not-open-the-lane
                                  the same for the question path

  the checks that decide whether the answer is pasted
      cancel-fix-ignores-a-running-call
                                  the in-flight check answers no, so the
                                  action sets no flag and says nothing
      cancel-fix-acts-when-nothing-is-running
                                  it answers yes for a call that already
                                  finished, which leaves a flag set that
                                  would cancel the NEXT rewrite
      the-cancelled-status-is-ignored
                                  the cancelled result is treated as an
                                  ordinary failure and the user is told
                                  the server is at fault
      the-step-4-cancel-check-is-disabled
                                  the replacement is pasted and "Done."
                                  is spoken despite the cancel
      the-step-4-check-does-not-stop-the-paste
                                  the check runs, speaks, and falls
                                  through to the paste anyway
      the-step-4-check-does-not-clear-the-flag
                                  the flag survives the cancel it caused
                                  and would cancel the next rewrite

WHAT THIS GATE DOES NOT CLAIM. It covers the thirteen tests THIS BEAD added,
in the one test file named below. It makes no claim about any other AI or
speech test in this repository. It does not mutate ai/service.py: the
post-provider cancellation check there is older than this bead, and step 4
of the transform catches the same input, so a mutation of it would be masked
rather than informative.

Run it from services/wheelhouse, and NEVER while a test suite or a reviewer
is running in the same worktree -- this file rewrites the source that suite
imports:

    uv run python tests/mutation_gate_cancel_during_running_rewrite.py

A single mutation by name runs alone:

    uv run python tests/mutation_gate_cancel_during_running_rewrite.py \
        the-lane-never-opens

--check verifies only that every pattern still matches exactly once and that
every mutant parses. It is NOT a sweep and cannot see a test that stopped
catching its mutation.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE_DIR = Path(__file__).resolve().parents[1]

PROCESSOR = SERVICE_DIR / "speech" / "speech_processor.py"
ACTIONS = SERVICE_DIR / "speech" / "actions.py"

FEATURE_TESTS_FILE = "tests/test_ai/test_cancel_during_running_rewrite.py"
FEATURE_TEST_FILES = (FEATURE_TESTS_FILE,)

# The file runs in about nine seconds clean. A mutation that stops the cancel
# reaching the action turns several five-second waits into failures, so a
# mutant run can take a minute. This limit exists to turn a genuine hang into
# a reported error rather than a lost run.
RUN_TIMEOUT_S = 300

_REACHES = "TestCancelReachesTheActionWhileTheModelRuns"
_TRIGGERS = "TestEveryRewriteTriggerSharesTheFix"
_ASK = "TestAskAiSharesTheFix"
_PASTE = "TestCancelAroundThePaste"
_ORDER = "TestEveryOtherEventKeepsItsOrder"
_TURN = "TestTheLaneOnlyOpensInsideAWordEventTurn"
_HOTWORD = "TestTheCancelStillNeedsTheHotword"
_LATE = "TestAnEventQueuedAfterTheLaneClosesRunsLast"
_REPLAY = "TestTheRecognisedCancelReplaysAsACommand"
_LEAK = "TestAFailedCallDoesNotLeakTheCancelFlag"
_LIKE_ROUTER = "TestTheLaneRecognisesWhatTheRouterWouldRecognise"


def _reaches(name):
    return f"{_REACHES}::{name}"


def _trigger(case):
    """One parametrized rewrite trigger, named by its own id.

    The parameter is kept in the key on purpose: the claim is that ALL FIVE
    shipped triggers share the fix, and a key with the parameter cut off
    would let one failing case stand for five.
    """
    return f"{_TRIGGERS}::test_the_cancel_stops_this_trigger[{case}]"


def _ask(name):
    return f"{_ASK}::{name}"


def _paste(name):
    return f"{_PASTE}::{name}"


def _order(name):
    return f"{_ORDER}::{name}"


def _turn(name):
    return f"{_TURN}::{name}"


def _hotword(name):
    return f"{_HOTWORD}::{name}"


def _late(name):
    return f"{_LATE}::{name}"


def _replay(name):
    return f"{_REPLAY}::{name}"


def _leak(name):
    return f"{_LEAK}::{name}"


def _like_router(name):
    return f"{_LIKE_ROUTER}::{name}"


_A1 = _reaches("test_cancel_spoken_during_the_call_prevents_the_paste")
_EVERY_TRIGGER = [
    _trigger("simplify"),
    _trigger("shorten"),
    _trigger("make-formal"),
    _trigger("pirate"),
    _trigger("translate-to"),
]
_ASK_AI = _ask("test_cancel_during_the_question_cancels_it")
_BEFORE_PASTE = _paste("test_a_cancel_after_the_answer_still_stops_the_paste")
_AFTER_PASTE = _paste("test_a_cancel_after_the_paste_behaves_as_before")
_ORDERING = _order(
    "test_words_spoken_during_the_call_are_typed_after_the_paste"
)
_NO_LANE_OUTSIDE = _turn("test_an_ai_call_outside_the_word_loop_opens_no_lane")
_CLOSING_KEEPS = _turn("test_closing_the_lane_keeps_the_events_it_deferred")
_NEEDS_HOTWORD = _hotword("test_the_trigger_words_alone_do_not_cancel")
_LATE_EVENT = _late(
    "test_a_word_queued_at_the_close_is_typed_after_the_deferred_ones"
)
_REPLAYED_CANCEL = _replay(
    "test_the_replayed_command_shows_nothing_and_sets_nothing"
)
_FLAG_LEAK = _leak(
    "test_a_cancel_during_a_failed_call_does_not_cancel_the_next_one"
)
_ESCAPED_TEXT = _like_router(
    "test_an_escaped_cancel_phrase_does_not_cancel"
)
_PUNCTUATED = _like_router(
    "test_a_punctuated_cancel_still_cancels"
)
_CORRECTED_FINAL = _like_router(
    "test_a_corrected_final_transcript_cancels"
)

# Every test the bead added. Each one must be broken by some mutation below,
# or named in NOT_MUTATED with a reason.
FEATURE_TESTS = {
    _A1,
    *_EVERY_TRIGGER,
    _ASK_AI,
    _BEFORE_PASTE,
    _AFTER_PASTE,
    _ORDERING,
    _NO_LANE_OUTSIDE,
    _CLOSING_KEEPS,
    _NEEDS_HOTWORD,
    _LATE_EVENT,
    _REPLAYED_CANCEL,
    _FLAG_LEAK,
    _ESCAPED_TEXT,
    _PUNCTUATED,
    _CORRECTED_FINAL,
}

NOT_MUTATED = {}

# Every test that watches the cancel travel the whole shipped path, from the
# spoken words to the action. A mutation that closes the lane, or stops the
# action being found, breaks all of them together. _PUNCTUATED is in the
# group for that same reason: it speaks the cancel and waits for the
# acknowledgement, and every mutation that uses this group was measured
# failing it on that wait.
_EVERY_CANCEL_PATH = [
    _A1, *_EVERY_TRIGGER, _ASK_AI, _REPLAYED_CANCEL, _PUNCTUATED,
]

# The two tests that watch the deferred events themselves: the list the lane
# fills, and the order the loop replays them in.
_EVERY_DEFERRED_EVENT = [_ASK_AI, _ORDERING]

MUTATIONS = [
    # -----------------------------------------------------------------
    # The lane's lifetime.
    # -----------------------------------------------------------------
    {
        # The plainest form: no lane ever opens, which is exactly the
        # state the bead found the code in. Every test that speaks the
        # cancel during a model call goes back to waiting for the paste.
        "name": "the-lane-never-opens",
        "target": PROCESSOR,
        "old": (
            "        if not self._in_word_event_turn:\n"
            "            return\n"
            "        if self._ai_cancel_lane_task is not None:\n"
        ),
        "new": (
            "        if True:\n"
            "            return\n"
            "        if self._ai_cancel_lane_task is not None:\n"
        ),
        "expect": [
            *_EVERY_CANCEL_PATH,
            _CORRECTED_FINAL,
            _ORDERING,
            _NEEDS_HOTWORD,
            _CLOSING_KEEPS,
            _LATE_EVENT,
        ],
        # Measured: the escaped-text test times out on its own
        # synchronisation -- "waiting for the lane to defer all four
        # words of the escaped utterance" -- before it ever checks
        # whether the escape held. That failure proves nothing about
        # the escape, so it is collateral rather than proof.
        "also_fails": {
            _ESCAPED_TEXT: "the lane defers nothing, so the test times "
                           "out on its own wait for four deferred events",
        },
    },
    {
        # The other direction, and the one a reader is most likely to
        # think harmless: the lane opens whenever an AI action asks,
        # including from outside the word loop, where the loop is itself
        # reading word_queue and a lane is a second reader.
        "name": "the-lane-opens-outside-a-turn",
        "target": PROCESSOR,
        "old": (
            "        if not self._in_word_event_turn:\n"
            "            return\n"
            "        if self._ai_cancel_lane_task is not None:\n"
        ),
        "new": (
            "        if False:\n"
            "            return\n"
            "        if self._ai_cancel_lane_task is not None:\n"
        ),
        "expect": [_NO_LANE_OUTSIDE],
    },
    {
        # The same refusal reached from the loop instead of the guard:
        # the flag the guard reads is never set, so no lane can open.
        "name": "the-turn-flag-is-never-set",
        "target": PROCESSOR,
        "old": "                    self._in_word_event_turn = True\n",
        "new": "                    pass\n",
        # The ask_ai test is NOT here, and measurably so: it drives the
        # action directly on a rig whose processing loop was never
        # started, and sets the flag by hand because the real caller
        # would be inside a turn. Nothing in the loop runs, so nothing
        # in the loop can be broken for it.
        "expect": [
            _A1,
            *_EVERY_TRIGGER,
            _ORDERING,
            _NEEDS_HOTWORD,
            _LATE_EVENT,
            _REPLAYED_CANCEL,
            _PUNCTUATED,
            _CORRECTED_FINAL,
        ],
        # Measured: the two cancel tests fail on their own wait for
        # the acknowledgement, which is proof. The escaped-text test
        # times out on its synchronisation instead -- "waiting for
        # the lane to defer all four words of the escaped utterance"
        # -- so it never reaches the question it exists to ask.
        "also_fails": {
            _ESCAPED_TEXT: "the lane defers nothing, so the test times "
                           "out on its own wait for four deferred events",
        },
    },
    {
        # Closing the lane throws away what it took. The events are off
        # word_queue and out of the list, so nothing will ever process
        # them -- the words are simply lost.
        "name": "closing-the-lane-drops-the-deferred-events",
        "target": PROCESSOR,
        "old": (
            "        task = self._ai_cancel_lane_task\n"
            "        self._ai_cancel_lane_task = None\n"
        ),
        "new": (
            "        self._deferred_word_events.clear()\n"
            "        task = self._ai_cancel_lane_task\n"
            "        self._ai_cancel_lane_task = None\n"
        ),
        # The escaped words are deferred and then thrown away, so the
        # measured failure is the escape's own outcome: "waiting for the
        # escaped words to be typed as text once the rewrite finished".
        "expect": [
            _CLOSING_KEEPS,
            *_EVERY_DEFERRED_EVENT,
            _LATE_EVENT,
            _ESCAPED_TEXT,
        ],
    },

    # -----------------------------------------------------------------
    # What the lane recognises, and what it does about it.
    # -----------------------------------------------------------------
    {
        # The lane runs and defers, and acts on nothing. Every event
        # still reaches the truth table in order, so only the cancel is
        # lost -- which is the reported defect exactly.
        "name": "the-lane-ignores-the-recognizer",
        "target": PROCESSOR,
        "old": (
            "                if recognizer.observe("
            "word_event, hotword=self.hotword):\n"
        ),
        "new": "                if False:\n",
        # The corrected-final test is proof here too: measured, it fails
        # on its own wait for the cancel acknowledgement.
        "expect": [*_EVERY_CANCEL_PATH, _CORRECTED_FINAL],
    },
    {
        # The match is recognised and logged, and nothing runs. A reader
        # watching the pipeline log would see the cancel arrive.
        "name": "the-cancel-action-is-not-called",
        "target": PROCESSOR,
        "old": "                    await self._run_cancel_action()\n",
        "new": "                    pass\n",
        # The corrected-final test is proof here too: measured, it fails
        # on its own wait for the cancel acknowledgement.
        "expect": [*_EVERY_CANCEL_PATH, _CORRECTED_FINAL],
    },
    {
        # The name is the only link between the pattern file and the
        # Python object. A name no pattern carries collects no patterns,
        # so nothing matches and no action is found.
        "name": "the-cancel-action-name-is-wrong",
        "target": PROCESSOR,
        "old": '_CANCEL_ACTION_NAME = "cancel_fix"\n',
        "new": '_CANCEL_ACTION_NAME = "cancel_fixx"\n',
        # The corrected-final test is proof here too: measured, it fails
        # on its own wait for the cancel acknowledgement.
        "expect": [*_EVERY_CANCEL_PATH, _CORRECTED_FINAL],
    },
    {
        # The hotword arms a command wherever it appears instead of only
        # at the start of an utterance, which is the router's own gate
        # (SpeechRouter.decide). The shipped text escapes -- "type X",
        # "dictate X", "literal X" -- then stop escaping for as long as an
        # AI call runs: "type x-ray cancel fix" cancels the rewrite the
        # user asked for. Codex round 2, finding
        # wh-cancel-fix-running-rewrite.1.4.
        "name": "the-hotword-arms-anywhere-in-the-utterance",
        "target": PROCESSOR,
        "old": (
            "            if word_event.start_of_utterance and _word_matches_hotword(\n"
            "                word, hotword\n"
            "            ):\n"
        ),
        "new": (
            "            if _word_matches_hotword(\n"
            "                word, hotword\n"
            "            ):\n"
        ),
        "expect": [_ESCAPED_TEXT],
    },
    {
        # The lane matches the raw joined words again instead of going
        # through the engine's own single-pattern path, so a transcript
        # that carries sentence punctuation -- what Google speech
        # recognition sends when enable_automatic_punctuation is on --
        # stops being recognised as the cancel command. Codex round 2,
        # finding wh-cancel-fix-running-rewrite.1.2.
        "name": "the-lane-matches-without-the-punctuation-retry",
        "target": PROCESSOR,
        "old": (
            "        return any(\n"
            "            self._matcher.match_single_pattern(\n"
            "                text, pattern, authorized_command=True\n"
            "            ) is not None\n"
            "            for pattern in self._patterns\n"
            "        )\n"
        ),
        "new": (
            "        return any(\n"
            '            pattern["compiled_pattern"].match(text) is not None\n'
            "            for pattern in self._patterns\n"
            "        )\n"
        ),
        "expect": [_PUNCTUATED],
    },
    {
        # The retraction marker is discarded like every other marker, so a
        # cancel that only the CORRECTED final transcript got right is
        # invisible to the lane: WebSocketManager._handle_mode3_retract
        # sends the correction as one marker carrying the whole text, and
        # never as word events. Codex round 2, finding
        # wh-cancel-fix-running-rewrite.1.3.
        "name": "the-corrected-final-transcript-is-not-read",
        "target": PROCESSOR,
        "old": (
            "            self.reset()\n"
            "            return self._corrected_final_is_a_cancel("
            "corrected, hotword=hotword)\n"
        ),
        "new": (
            "            self.reset()\n"
            "            return False\n"
        ),
        "expect": [_CORRECTED_FINAL],
    },
    {
        # Words are collected before any hotword, so the two trigger
        # words dictated as ordinary text cancel the running call.
        "name": "the-recognizer-collects-words-before-the-hotword",
        "target": PROCESSOR,
        "old": "        if not self._hotword_seen:\n",
        "new": "        if False:\n",
        # This breaks the whole cancel path as well, and the reason is
        # worth writing down: the block skipped here is the one that
        # CONSUMES the hotword. Without it "x-ray" is collected as an
        # ordinary word, so the text the patterns are matched against is
        # "x-ray cancel fix", which ^cancel fix$ does not match. The
        # hotword is not merely required; it has to be taken out of the
        # words before they are matched.
        # The corrected-final test is deliberately absent, and it passed
        # under this mutation when measured. observe() returns from its
        # retraction branch before this block, and
        # _corrected_final_is_a_cancel checks and removes the hotword
        # itself, so the mutation cannot reach that path.
        "expect": [*_EVERY_CANCEL_PATH, _NEEDS_HOTWORD],
    },

    # -----------------------------------------------------------------
    # The deferred events.
    # -----------------------------------------------------------------
    {
        # The lane takes each event off word_queue and keeps none, so
        # everything spoken during the model call is lost.
        "name": "the-deferred-events-are-dropped",
        "target": PROCESSOR,
        "old": "                self._deferred_word_events.append(word_event)\n",
        "new": "                pass\n",
        "expect": [*_EVERY_DEFERRED_EVENT, _CLOSING_KEEPS, _LATE_EVENT],
        # Nothing is ever appended, so the escaped-text test times out on
        # its own wait for four deferred events and never asks its
        # question. The events being lost is already proved by the three
        # tests above.
        "also_fails": {
            _ESCAPED_TEXT: "the lane defers nothing, so the test times "
                           "out on its own wait for four deferred events",
        },
    },
    {
        # The list is filled from the front, so the events are replayed
        # backwards. Nothing is lost, which is why the order has tests
        # of its own.
        "name": "the-lane-defers-in-reverse",
        "target": PROCESSOR,
        "old": "                self._deferred_word_events.append(word_event)\n",
        "new": (
            "                self._deferred_word_events.insert(0, word_event)\n"
        ),
        # The replayed cancel is here for a reason worth stating: the
        # words replay backwards, so the truth table is matched against
        # "fix cancel" and does not recognise the command at all. The
        # measured failure is the third assertion,
        # "the replayed cancel utterance was typed as dictation instead
        # of running as the command it is", with actions_sent
        # ['capture_selected_text', 'intelligent_insert_text',
        # 'intelligent_insert_text'].
        # The escaped words survive the lane and never reach the
        # typing, so the measured failure is the escape's own
        # outcome: "waiting for the escaped words to be typed as
        # text once the rewrite finished".
        "expect": [
            *_EVERY_DEFERRED_EVENT,
            _REPLAYED_CANCEL,
            _ESCAPED_TEXT,
        ],
    },
    {
        # The list is filled correctly and read from the wrong end.
        "name": "the-deferred-events-are-replayed-last-first",
        "target": PROCESSOR,
        "old": (
            "                    word_event = "
            "self._deferred_word_events.pop(0)\n"
        ),
        "new": (
            "                    word_event = "
            "self._deferred_word_events.pop()\n"
        ),
        # Same measured outcome as the reverse-deferral mutation above,
        # and for the same reason: the cancel words reach the truth
        # table backwards, so they are typed as dictation.
        # The escaped words survive the lane and never reach the
        # typing, so the measured failure is the escape's own
        # outcome: "waiting for the escaped words to be typed as
        # text once the rewrite finished".
        "expect": [_ORDERING, _REPLAYED_CANCEL, _ESCAPED_TEXT],
    },
    {
        # The loop never looks at the list, so the deferred events sit
        # there for the rest of the session.
        "name": "the-deferred-events-are-not-replayed",
        "target": PROCESSOR,
        "old": "                if self._deferred_word_events:\n",
        "new": "                if False:\n",
        # The cancel utterance is never replayed, so the list never
        # empties. The measured failure is the wait that names it:
        # "timed out after 5.0s waiting for the deferred cancel
        # utterance to be replayed".
        # The escaped words survive the lane and never reach the
        # typing, so the measured failure is the escape's own
        # outcome: "waiting for the escaped words to be typed as
        # text once the rewrite finished".
        "expect": [
            _ORDERING,
            _LATE_EVENT,
            _REPLAYED_CANCEL,
            _ESCAPED_TEXT,
        ],
    },

    # -----------------------------------------------------------------
    # The two call sites.
    # -----------------------------------------------------------------
    {
        # The transform runs the model await with no lane. This is the
        # mutation that proves the five shipped rewrite triggers really
        # do share one helper: all five fail together.
        "name": "the-rewrite-does-not-open-the-lane",
        "target": ACTIONS,
        "old": (
            "                async with self._ai_cancel_lane():\n"
            "                    # Step 3: Send to the AI "
            "(returns a ChatResult).\n"
        ),
        "new": (
            "                async with contextlib.AsyncExitStack():\n"
            "                    # Step 3: Send to the AI "
            "(returns a ChatResult).\n"
        ),
        "expect": [
            _A1,
            *_EVERY_TRIGGER,
            _ORDERING,
            _NEEDS_HOTWORD,
            _LATE_EVENT,
            _REPLAYED_CANCEL,
            _PUNCTUATED,
            _CORRECTED_FINAL,
        ],
        # Measured: the two cancel tests fail on their own wait for
        # the acknowledgement, which is proof. The escaped-text test
        # times out on its synchronisation instead -- "waiting for
        # the lane to defer all four words of the escaped utterance"
        # -- so it never reaches the question it exists to ask.
        "also_fails": {
            _ESCAPED_TEXT: "the lane defers nothing, so the test times "
                           "out on its own wait for four deferred events",
        },
    },
    {
        # The same for the question path, which has no shipped pattern
        # and is therefore driven directly by its own test.
        "name": "ask-ai-does-not-open-the-lane",
        "target": ACTIONS,
        "old": (
            "                    async with self._ai_cancel_lane():\n"
            "                        reply = await asyncio.wait_for(\n"
        ),
        "new": (
            "                    async with contextlib.AsyncExitStack():\n"
            "                        reply = await asyncio.wait_for(\n"
        ),
        "expect": [_ASK_AI],
    },

    # -----------------------------------------------------------------
    # The checks that decide whether the answer is pasted.
    # -----------------------------------------------------------------
    {
        # The in-flight check answers no for a call that is running, so
        # the action sets no flag and says nothing -- which is what the
        # bug report described from the user's side.
        "name": "cancel-fix-ignores-a-running-call",
        "target": ACTIONS,
        "old": "        if ai and ai.is_processing():\n",
        "new": "        if False:\n",
        # The corrected-final test is proof here too: measured, it fails
        # on its own wait for the cancel acknowledgement.
        "expect": [*_EVERY_CANCEL_PATH, _CORRECTED_FINAL],
    },
    {
        # It answers yes for a call that already finished. Nothing is
        # cancelled now, but the flag is left set, and the next rewrite
        # reads it and cancels itself.
        "name": "cancel-fix-acts-when-nothing-is-running",
        "target": ACTIONS,
        "old": "        if ai and ai.is_processing():\n",
        "new": "        if ai:\n",
        "expect": [_AFTER_PASTE, _REPLAYED_CANCEL],
    },
    {
        # A cancelled result is treated as an ordinary failure, so the
        # user is told the server is at fault for something they asked
        # for. The paste is still stopped, which is why this needs a
        # test that reads the words rather than the outcome.
        "name": "the-cancelled-status-is-ignored",
        "target": ACTIONS,
        "old": (
            "                    if corrected.status is "
            "ChatStatus.CANCELLED:\n"
        ),
        "new": "                    if False:\n",
        "expect": [_A1, *_EVERY_TRIGGER],
    },
    {
        # The cancel is honoured -- nothing is pasted -- and the user is
        # told the rewrite finished. This is the third mutation A5 names,
        # and it is the only one of the three whose catcher is the
        # "Done." assertion itself: the two mutations either side of it
        # stop on the paste assertion or on the missing "Cancelled." one,
        # both of which come first in their tests.
        "name": "the-cancelled-path-reports-done",
        "target": ACTIONS,
        "old": (
            "                    if corrected.status is "
            "ChatStatus.CANCELLED:\n"
            '                        self._notify_ai_status("Cancelled.")\n'
        ),
        "new": (
            "                    if corrected.status is "
            "ChatStatus.CANCELLED:\n"
            '                        self._notify_ai_status("Done.")\n'
        ),
        "expect": [_A1, *_EVERY_TRIGGER],
    },
    {
        # The last check before the paste is gone, so a cancel that
        # arrives after the answer pastes the replacement and speaks
        # "Done." as though nothing had been asked.
        "name": "the-step-4-cancel-check-is-disabled",
        "target": ACTIONS,
        "old": (
            "                    if ai.cancel_requested:\n"
            "                        self._notify_ai_status(\"Cancelled.\")\n"
        ),
        "new": (
            "                    if False:\n"
            "                        self._notify_ai_status(\"Cancelled.\")\n"
        ),
        "expect": [_BEFORE_PASTE],
    },
    {
        # The check runs and speaks, and then falls through to the paste
        # anyway. A test that only listened for "Cancelled." would
        # survive this, which is why the paste itself is asserted.
        "name": "the-step-4-check-does-not-stop-the-paste",
        "target": ACTIONS,
        "old": (
            "                    if ai.cancel_requested:\n"
            '                        self._notify_ai_status("Cancelled.")\n'
            "                        return None\n"
        ),
        "new": (
            "                    if ai.cancel_requested:\n"
            '                        self._notify_ai_status("Cancelled.")\n'
            "                        pass\n"
        ),
        "expect": [_BEFORE_PASTE],
    },
    {
        # The one clear the call has is gone, so a cancel recognised
        # during a not-ok call survives the call and the user's NEXT
        # rewrite reads it. AIService._transform_text checks the flag
        # before the provider call, so that next rewrite returns
        # CANCELLED with no model call at all: it silently does
        # nothing. Finding wh-cancel-fix-running-rewrite.1.1.
        "name": "the-transform-never-clears-the-flag",
        "target": ACTIONS,
        "old": (
            "                ai.cancel_requested = False\n"
            "                self._send_gui_action({\"action\": \"hide_working\", \"owner\": owner})\n"
            "                # wh-review-pattern-fixes.28: collapse the fallback-armed\n"
        ),
        "new": (
            "                self._send_gui_action({\"action\": \"hide_working\", \"owner\": owner})\n"
            "                # wh-review-pattern-fixes.28: collapse the fallback-armed\n"
        ),
        "expect": [_FLAG_LEAK, _BEFORE_PASTE],
    },
]


def _edits(mutation):
    """Every (target, old, new) this mutation applies, in order."""
    if "edits" in mutation:
        return [(e["target"], e["old"], e["new"]) for e in mutation["edits"]]
    return [(mutation["target"], mutation["old"], mutation["new"])]


def _clear_pycache(root=None):
    """Remove every __pycache__ under the service; return (error, interrupt).

    Python decides whether cached bytecode is current from the source file's
    (mtime, size), and it truncates that mtime to an INTEGER SECOND. Several
    mutations here keep the source the same byte length -- pop(0) to pop(),
    a comparison rewritten as a constant -- so a .pyc written for the clean
    source before the gate started is still valid for a mutant written in
    the same second, and the pytest child then executes clean code while a
    mutant sits on disk.

    ``ignore_errors=True`` is what makes the check after it necessary: a
    directory Windows will not delete -- an antivirus scan, or another
    process holding a file in it -- leaves ``rmtree`` silent and the
    directory in place, so the only way to know the sweep worked is to look.
    Every survivor goes to a second pass, because the hold is usually
    momentary, and only a survivor after the last pass is an error.

    A pass publishes its result only when it completes, so an interrupted
    pass cannot erase what a completed pass found. ``.venv`` is excluded
    from the DELETION, not from the walk: the check protects installed
    packages rather than saving traversal time.

    The interrupt is held rather than allowed out, so the caller can print
    what it knows before the run ends.
    """
    root = SERVICE_DIR if root is None else root
    interrupt = None
    survivors, swept = [], False
    for _pass in (1, 2):
        found = []
        try:
            for d in root.rglob("__pycache__"):
                if ".venv" not in d.parts:
                    shutil.rmtree(d, ignore_errors=True)
                    if d.exists():
                        found.append(d)
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            continue
        survivors, swept = found, True
        if not survivors:
            return None, interrupt
    if not swept:
        return ("interrupted during every attempt to clear __pycache__; a "
                "MUTANT .pyc MAY REMAIN -- delete those directories before "
                "the next run"), interrupt
    listed = ", ".join(str(d) for d in survivors)
    return (f"could not remove {len(survivors)} __pycache__ director"
            f"{'y' if len(survivors) == 1 else 'ies'}; a MUTANT .pyc MAY "
            f"REMAIN and the next process may import it: {listed}"), interrupt


def _clear_pycache_or_abort():
    """Clear the cache before the baseline; return an exit code or None.

    A cache the gate could not clear is a hard abort, not a note: every
    later verdict in the run would come from a process that may have
    imported bytecode for the source the gate replaced. The held interrupt
    is raised FIRST, so a Ctrl+C during the clear ends the run as an
    interrupt rather than as an exit code that reads like an ordinary
    refusal.
    """
    error, interrupt = _clear_pycache()
    if error:
        print(f"ERROR {error}")
    if interrupt is not None:
        raise interrupt
    return 1 if error else None


def _run_pytest(test_files):
    """Run every named test file in ONE pytest process.

    ``PYTHONDONTWRITEBYTECODE`` stops Python WRITING a .pyc. It does NOT
    stop Python reading one that is already there, so ``_clear_pycache``
    runs before the baseline and again after every mutant is restored.
    Both steps are needed; see the note in ``_clear_pycache``.

    ``-p no:randomly`` keeps the order fixed, so a mutation's verdict does
    not depend on which test happened to run first.
    """
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", *test_files, "-q", "-rf",
         "-p", "no:cacheprovider", "-p", "no:randomly"],
        cwd=SERVICE_DIR, env=env, capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S,
    )


def _node_key(node_id):
    """The part of a pytest node id after the file path.

    ``Class::method`` for a class-based test, and the parameter list is
    KEPT: this file's five rewrite triggers are five parameters of one
    method, and the claim is about all five.
    """
    return node_id.split("::", 1)[1].strip()


def _failed_names(output):
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            node_id = line.split(" ", 1)[1].split(" ")[0]
            if "::" in node_id:
                names.add(_node_key(node_id))
    return names


def _coverage_errors(every_test):
    """Check that every test this bead added is mutated or excluded by name.

    Six ways the claim can stop being true, all reported here:

    1. A FEATURE_TESTS entry that no mutation breaks and NOT_MUTATED does
       not name. That is the over-claim itself.
    2. A FEATURE_TESTS entry that no longer exists.
    3. NOT_MUTATED names a test that no longer exists.
    4. A name is in NOT_MUTATED and in some mutation's expect list, so the
       file says both "covered" and "deliberately not covered".
    5. A reason cites another mutation by name. That claim is unverifiable
       where it is written and unchecked where it is read.
    6. A NOT_MUTATED entry whose reason is empty.
    """
    errors = []
    expected = {
        name for mutation in MUTATIONS for name in mutation["expect"]
    }

    for name in sorted(FEATURE_TESTS):
        if name not in every_test:
            errors.append(f"FEATURE_TESTS names {name}, which does not exist")
            continue
        if name not in expected and name not in NOT_MUTATED:
            errors.append(
                f"{name} is broken by no mutation and NOT_MUTATED does not "
                "name it"
            )

    for name, reason in sorted(NOT_MUTATED.items()):
        if name not in every_test:
            errors.append(f"NOT_MUTATED names {name}, which does not exist")
        if not reason.strip():
            errors.append(f"NOT_MUTATED entry {name} carries no reason")
        if name in expected:
            errors.append(
                f"{name} is in NOT_MUTATED and in a mutation's expect list"
            )
        lowered = reason.lower()
        for mutation in MUTATIONS:
            if mutation["name"] in lowered:
                errors.append(
                    f"NOT_MUTATED entry {name} cites mutation "
                    f"{mutation['name']}; a reason must state a property of "
                    "the behaviour, not point at another mutation"
                )
    return errors


def _check_patterns():
    """--check: every pattern matches exactly once and every mutant parses.

    This is NOT a sweep. It cannot see a test that stopped catching its
    mutation; only a full run does that. It exists to find a stale pattern
    in seconds after a fix rewrites the code under it.
    """
    sources = {}
    for mutation in MUTATIONS:
        for target, _, _ in _edits(mutation):
            if target in sources:
                continue
            raw = target.read_bytes()
            if b"\r\n" in raw:
                print(f"ERROR: {target.name} uses CRLF; patterns here are LF")
                return 1
            sources[target] = raw.decode("utf-8")

    stale, broken = 0, 0
    for mutation in MUTATIONS:
        working = dict(sources)
        ok = True
        for target, old, new in _edits(mutation):
            count = working[target].count(old)
            if count != 1:
                print(
                    f"STALE {mutation['name']}: pattern matched {count} "
                    "times, need 1"
                )
                stale += 1
                ok = False
                break
            working[target] = working[target].replace(old, new, 1)
        if not ok:
            continue
        for target, mutated in working.items():
            if mutated == sources[target]:
                continue
            try:
                compile(mutated, str(target), "exec")
            except SyntaxError as exc:
                print(
                    f"BROKEN {mutation['name']}: mutant does not compile: "
                    f"{exc}"
                )
                broken += 1

    print(
        f"checked {len(MUTATIONS)} patterns, {stale} stale, "
        f"{broken} that do not compile"
    )
    return 0 if not stale and not broken else 1


def main(argv):
    argv = list(argv[1:])
    if "--check" in argv:
        return _check_patterns()

    # A name on the command line runs that mutation alone. The scope line at
    # the end says which subset ran, so a partial run cannot be read as a
    # full sweep.
    selected = argv
    if selected:
        known = {m["name"] for m in MUTATIONS}
        unknown = [name for name in selected if name not in known]
        if unknown:
            print(f"ERROR: no such mutation: {unknown}")
            return 1
        mutations = [m for m in MUTATIONS if m["name"] in selected]
    else:
        mutations = list(MUTATIONS)

    duplicate = [
        m["name"] for m in MUTATIONS
        if [x["name"] for x in MUTATIONS].count(m["name"]) > 1
    ]
    if duplicate:
        print(f"ERROR: duplicate mutation names: {sorted(set(duplicate))}")
        return 1

    targets = {
        target for mutation in mutations for target, _, _ in _edits(mutation)
    }
    originals = {}
    for target in targets:
        raw = target.read_bytes()
        if b"\r\n" in raw:
            # The patterns above are written with LF. A file stored with CRLF
            # makes every multi-line pattern miss, which reads as a survivor.
            # Refuse rather than rewrite the file's endings.
            print(f"ERROR: {target.name} uses CRLF; patterns here are LF")
            return 1
        originals[target] = raw
    sources = {t: raw.decode("utf-8") for t, raw in originals.items()}

    errors, survivors, caught = [], [], []

    # Every expected test name must exist before the first mutation. A name
    # that no longer exists can never appear in the failed set, so a genuine
    # catch would be reported as a survivor.
    every_test = set()
    for test_file in FEATURE_TEST_FILES:
        collect = subprocess.run(
            [sys.executable, "-m", "pytest", test_file, "--collect-only", "-q",
             "-p", "no:cacheprovider"],
            cwd=SERVICE_DIR, capture_output=True, text=True,
            timeout=RUN_TIMEOUT_S,
        )
        every_test |= {
            _node_key(line.strip())
            for line in collect.stdout.splitlines() if "::" in line
        }
    for mutation in mutations:
        for name in mutation["expect"]:
            if name not in every_test:
                errors.append(f"{mutation['name']}: no such test {name}")
    errors.extend(_coverage_errors(every_test))
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    abort = _clear_pycache_or_abort()
    if abort is not None:
        return abort

    # A suite that is already red reports every mutation as caught for a
    # reason unrelated to the mutation.
    baseline = _run_pytest(FEATURE_TEST_FILES)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print(f"baseline green; running {len(mutations)} mutations")

    try:
        for mutation in mutations:
            name = mutation["name"]
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
                try:
                    compile(mutated, str(target), "exec")
                except SyntaxError as exc:
                    errors.append(f"{name}: mutant does not compile: {exc}")
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
                cache_error, stop = _clear_pycache()
                if cache_error:
                    errors.append(f"{name}: {cache_error}")
                    print(f"ERROR {name}: {cache_error}")
                if stop is not None:
                    raise stop

            combined = result.stdout + result.stderr
            if "+++ Timeout +++" in combined:
                # pytest-timeout kills the process before the -rf summary
                # prints, so a parser would find no FAILED lines and report a
                # survivor for a mutation that may have been caught.
                errors.append(f"{name}: suite-timeout abort, no verdict")
                print(f"ERROR {name}: suite-timeout abort")
                continue

            failed = _failed_names(combined)
            expect = set(mutation["expect"])
            collateral = set(mutation.get("also_fails", {}))
            missing = sorted(expect - failed)
            # The expect list is an EXACT claim, not a lower bound. A test
            # that falls over on a shared precondition has not established
            # its own behaviour under the mutation, so every failure must be
            # declared, either as proof (expect) or as collateral
            # (also_fails, with the reason it proves nothing).
            undeclared = sorted(failed - expect - collateral)
            stale = sorted(collateral - failed)
            if missing:
                survivors.append(
                    f"{name}: expected failures missing: {missing}"
                )
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
        scope = f"all {len(MUTATIONS)} mutations for this bead, none skipped"
    else:
        ran = {m["name"] for m in mutations}
        skipped = [m["name"] for m in MUTATIONS if m["name"] not in ran]
        scope = (
            f"{len(mutations)} of {len(MUTATIONS)} mutations, named on the "
            f"command line. NOT RUN in this pass: {skipped}"
        )
    print(
        f"\nscope: {scope}."
        f"\ncaught {len(caught)}, survivors {len(survivors)}, "
        f"errors {len(errors)}"
    )
    for line in survivors + errors:
        print(f"  {line}")
    return 0 if not survivors and not errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
