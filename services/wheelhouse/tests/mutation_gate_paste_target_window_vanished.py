"""Mutation gate for the vanished-paste-target retry (wh-paste-target-window-vanished).

WHY THIS GATE EXISTS. Three of the eight handler tests were written
before the source change and were seen to fail, which is the first half
of the rule. The second half is that a test can pass for the wrong
reason, and five of those eight tests pass both before and after the fix
on purpose: they guard behaviour the fix must NOT change. Nothing but a
mutation can show those five are able to fail at all.

The fix lets one lost word be tried once more. Every mutation below
attacks one of the four conditions that make the second attempt safe, or
the probe that decides the window is gone.

WHAT THE BEAD CHANGED, and therefore what this gate defends:

  ui/hwnd_utils.py: hwnd_no_longer_exists, a probe that answers True
  only when the handle is proven to name no window
      invert-the-window-probe       the probe answers gone for a LIVE
                                    window, so a paste that a live
                                    window refused is tried again
                                    against that same window
      a-missing-handle-is-called-gone
                                    a falsy handle answers gone, so a
                                    caller that never had a target
                                    recovers on no evidence
      a-failed-probe-is-called-gone the except arm answers gone, so the
                                    probe fails open instead of closed

  ui/strategies/specific.py: ClipboardOnlyStrategy reports the window
  gone ONLY on the branch that fired no keystroke
      the-strategy-always-reports-the-window-gone
                                    the field starts True, so a failure
                                    AFTER the Ctrl+V reports the window
                                    gone and the handler pastes the same
                                    word a second time. This is the one
                                    mutation whose harm is a double
                                    paste rather than a lost word.
      the-strategy-never-reports-the-window-gone
                                    the probe result is thrown away, so
                                    the incident stays unfixed

  ui/ui_action_handler.py: the retry in _execute_insert_with_ack
      any-strategy-may-retry        the strategy test goes, so a
                                    strategy that sends first and
                                    verifies afterwards can be retried.
                                    ClipboardOnlyStrategy is the only
                                    one whose failure proves no keystroke
                                    fired.
      a-live-window-may-retry       the window-gone test goes, so the
                                    refusal for a live window that lost
                                    the foreground (wh-oe7u.3) starts
                                    recapturing instead of refusing
      the-retry-may-run-twice       the cap reads attempt == 2, so a
                                    word can be tried three times
      the-program-comparison-is-gone
                                    the newly captured target's program
                                    is no longer compared, so a word
                                    spoken into one program can land in
                                    another
      the-empty-program-name-is-accepted
                                    an unreadable program name stops
                                    refusing, so two unknown programs
                                    compare equal
      the-captured-program-name-is-taken-from-the-retry
                                    the captured name is overwritten
                                    from the retry capture before the
                                    comparison, so the comparison always
                                    agrees with itself
      the-program-check-stops-refusing
                                    the guard runs and then falls
                                    through, so the wrong program is
                                    remembered and routed even though
                                    the check said no. This is the
                                    ordering guard: the check has to run
                                    BEFORE remember_target, or a later
                                    retraction can delete text this
                                    program never wrote.

  ui/ui_action_handler.py: which attempt governs the retraction gate
  (finding wh-paste-target-window-vanished.1.2). _used_simple_paste is
  per-utterance and nothing clears it inside an utterance, so only the
  attempt that ENDS the loop may close it.
      the-superseded-attempt-closes-the-gate
                                    the reported state itself: the
                                    failed first attempt closes the
                                    gate, so the word the retry
                                    delivered cannot be retracted by
                                    voice
      the-clipboard-only-branch-stops-closing-the-gate
                                    the gate stops closing for the
                                    attempts that really did paste
                                    without proof, so a retraction
                                    walks back a span nobody measured
      the-gate-follows-the-superseded-attempt
                                    the guard is inverted, which is
                                    both halves of the error at once
      the-superseded-attempt-stops-invalidating
                                    the wrong fix the reviewer warned
                                    against: the whole ClipboardOnly
                                    branch is skipped for the
                                    superseded attempt, so the retry's
                                    TextPerfector composes against the
                                    dead window's mirror
      the-retry-decision-stops-reading-will-retry
                                    the two reads of will_retry
                                    disagree, so the loop ends on the
                                    first attempt while the bookkeeping
                                    still treats it as superseded

WHAT wh-lost-word-neighbour-paths ADDED, and therefore what the last
eight mutations defend. That bead fixed two neighbours of the same
defect: the DEFAULT dictation paths lost the word, and one failed word
blocked scratch-that for a whole utterance.

  ui/strategies/specific.py: ClipboardFallbackStrategy's preflight
  refusal, the one branch whose answer reaches the handler on BOTH
  default paths
      the-default-paths-never-report-the-window-gone
                                    the probe goes, so the merged state
                                    returns: the word is refused before
                                    any keystroke and delivered nowhere
      the-default-paths-always-report-the-window-gone
                                    the probe answers gone for a live
                                    window, so a refusal that is not an
                                    incident starts recapturing

  ui/ui_action_handler.py: the proof term on the retry
      an-unproven-attempt-may-retry the retry stops asking whether the
                                    attempt PROVED it was empty, so an
                                    abort after clear_selection's
                                    Delete earns a retry that repeats
                                    the Delete against a newly captured
                                    target

  ui/strategies/specific.py: how a composite combines the two fields
      unicode-first-passes-the-inner-proof-through
                                    the inner answer is passed through,
                                    so a call whose first attempt may
                                    have typed claims it delivered
                                    nothing
      standard-passes-the-inner-proof-through
                                    the same error one level down
      standard-combines-the-window-answer-instead-of-passing-it
                                    target_window_gone is combined with
                                    "and" like delivered_nothing. The
                                    shadow attempt never sets it, so
                                    the "and" answers False for every
                                    call and no retry ever fires -- the
                                    default paths lose the word again
                                    while still reporting
                                    delivered_nothing True

  ui/ui_action_handler.py: the retraction gate's proof term
      an-empty-word-still-closes-the-gate
                                    the reported state: a word that
                                    reached no window closes the gate
                                    for the whole utterance
      the-rejected-insert-stops-closing-the-gate
                                    the naive reading of the same fix,
                                    and a regression: the
                                    RejectedInsertionStrategy write is
                                    unconditional on purpose, because
                                    the remembered window already moved
                                    to a window this insert never wrote
                                    to

  wh-lost-word-neighbour-paths hard gate, gap G1 (2026-09-20): four
  lines the tests already covered while no mutation attacked them
      the-successful-rejection-may-retry
                                    the fifth retry conjunct goes, so a
                                    result that reports success AND an
                                    empty attempt AND a dead window
                                    earns a retry. Reachable:
                                    _slow_path_preflight builds
                                    stale_rejection with success=True
                                    (strategies/specific.py:423) and
                                    ClipboardFallbackStrategy returns it
                                    at :547 with delivered_nothing=True
                                    and the probe's answer, so the retry
                                    retypes a word the program already
                                    told the user it REJECTED
      the-simple-paste-branch-always-closes-the-gate
                                    the SimplePaste branch of
                                    _execute_insert_with_ack loses its
                                    exemption, so one empty word shuts
                                    the retraction gate for the whole
                                    utterance
      the-raw-simple-paste-branch-always-closes-the-gate
                                    the same loss in raw_insert_text
      the-raw-clipboard-only-branch-always-closes-the-gate
                                    the same loss in raw_insert_text's
                                    ClipboardOnly branch. Both
                                    raw_insert_text entries can only be
                                    caught by
                                    TestRawInsertTextFollowsTheSameRule.
                                    Other tests in the swept file DO
                                    drive raw_insert_text; none can
                                    catch these two. The comment above
                                    _RAW_INSERT names each one and why

HOW TO RUN IT, from services/wheelhouse:

    .venv/Scripts/python.exe tests/mutation_gate_paste_target_window_vanished.py

    --check            verify every pattern matches exactly once and
                       every mutant compiles, without running pytest.
                       This is NOT a sweep and cannot see a masked
                       survivor.
    NAME [NAME ...]    run only the mutations named, as bare
                       positional arguments. There is no --only flag:
                       main() treats every argument that is not
                       --check as a mutation name and refuses an
                       unknown one. The docstring claimed a --only
                       flag until 2026-09-20; passing it produced
                       "ERROR: no such mutation: ['--only']".

Read the whole output. Never read it through a pipe: a pipe replaces the
gate's exit code with its own and hides the lines outside its window.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Line buffering, so a killed run still shows where it stopped: a lost
# buffer would name the wrong mutation as the last one reached.
# ``sys.stdout`` is typed ``TextIO`` in typeshed and ``reconfigure``
# exists only on ``TextIOWrapper``, so the call is correct at runtime and
# unprovable to the checker.
sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

SERVICE_DIR = Path(__file__).resolve().parents[1]

HWND_UTILS = SERVICE_DIR / "ui" / "hwnd_utils.py"
STRATEGY = SERVICE_DIR / "ui" / "strategies" / "specific.py"
HANDLER = SERVICE_DIR / "ui" / "ui_action_handler.py"

TARGETS = {HWND_UTILS, STRATEGY, HANDLER}

RETRY_TESTS = "tests/test_ui/test_paste_target_window_vanished.py"
HWND_TESTS = "tests/test_ui/test_hwnd_utils.py"
# wh-lost-word-neighbour-paths: the two neighbours of the merged fix.
# The gate has to collect this file as well, or every expected name in
# it fails the pre-run name check and no mutation is applied.
NEIGHBOUR_TESTS = "tests/test_ui/test_lost_word_neighbour_paths.py"

FEATURE_TEST_FILES = (RETRY_TESTS, HWND_TESTS, NEIGHBOUR_TESTS)

# Both files run in about a second clean. No mutation here can make a
# test wait on a timer -- every one changes a return, a guard or an
# assignment -- so this limit exists only to turn a hang into a reported
# error rather than a lost run.
RUN_TIMEOUT_S = 600

# pytest-timeout kills the process before the short summary prints, so a
# parser would find no records and report a survivor for a mutation that
# may have been caught.
TIMEOUT_BANNER = "+++ Timeout +++"

SUMMARY_HEADER = "short test summary info"

# A catch has to come from the test's own assertion. These are the
# leading tokens a pytest summary line carries when it does.
ASSERTION_REASONS = ("assert", "AssertionError", "Failed", "DID NOT RAISE")

_SURVIVES = "TestTheWordSurvivesADeadTargetWindow"
_LIVE = "TestTheRefusalForALiveWindowIsUnchanged"
_ONCE = "TestTheRetryHappensAtMostOnce"
_PROGRAM = "TestTheWordStaysInsideItsOwnApplication"
_BRANCH = "TestOnlyTheNoKeystrokeBranchReportsTheWindowGone"
_PROBE = "TestHwndNoLongerExists"
_GATE = "TestTheRetractionGateFollowsTheAttemptThatEndsTheLoop"
# wh-lost-word-neighbour-paths.1.4, in the same file as _GATE.
_REBIND = "TestARetryThatRebindsTheTargetProtectsAnEarlierWord"
# wh-lost-word-neighbour-paths.1.5, in the same file as _REBIND.
_RETURN = "TestARetryThatReturnsToTheCreditedWindowKeepsTheRetraction"

# wh-lost-word-neighbour-paths classes, in
# tests/test_ui/test_lost_word_neighbour_paths.py.
#
# TestTheDefaultPathsSurviveADeadTargetWindow is deliberately absent,
# because no mutation here names a test in it: it hands the handler a
# result it built itself, so every catch it could make is already made
# by a test that drives the real strategy code.
#
# TestRawInsertTextFollowsTheSameRule was absent for the same stated
# reason until the hard gate of 2026-09-20 (gap G1). That reason was
# wrong for this one class. raw_insert_text has its OWN two writes of
# the retraction gate, at ui/ui_action_handler.py:3013 and :3020, and
# it is the only class that can catch a mutation of either write. The
# class is named below and three of the four G1 entries at the end of
# MUTATIONS use it.
#
# The first version of this paragraph said "no test outside this class
# drives that method". Round 7 of the review showed that is false
# (wh-lost-word-neighbour-paths.1.7, closed accepted_declined). Three
# calls of raw_insert_text sit in the swept file: two at :909 and :912
# inside the MODULE-LEVEL helper _raw_insert (defined at :899, column
# 0, so it belongs to no class), and a direct call at :1194. What is
# true is the CONCLUSION, and each outside caller misses for its own
# reason, so check all three before adding the next raw entry:
#   _raw_insert's caller at :919 is inside this same class, so it is
#     not an outside caller at all.
#   Its callers at :1028 and :1042 are inside
#     TestAnEmptyWordAgainstALiveWindowStillClosesTheGate (class at
#     :973). These DO reach both mutated branches, passing
#     clipboard_only_strategy and simple_paste_strategy. They still
#     cannot catch either mutant, and NOT because they miss the
#     branch: their window is alive (window_gone=False), so the
#     exemption never applies, the unmutated code sets the flag True,
#     and the mutant sets the same flag True unconditionally. Both
#     assert the retraction gate is closed, so they pass identically
#     with and without the mutation.
#   The direct call at :1194 is inside
#     TestARejectedInsertStillClosesTheGateUnconditionally (class at
#     :1149). It routes to rejected_strategy, so it enters the third
#     branch of raw_insert_text and its UNCONDITIONAL write at
#     ui/ui_action_handler.py:3035, which no mutation here touches.
# That last line is also the map for the obvious next entry. The write
# at :3035 has no mutation today, and the test at :1194 is its catcher,
# sitting in a file this gate already sweeps.
_UNPROVEN = "TestAnAttemptThatMayHaveTypedIsNeverRetried"
_RAW_INSERT = "TestRawInsertTextFollowsTheSameRule"
# The fifth retry conjunct's own class, added with gap G1.
_SUCCEEDED = "TestASuccessfulRejectionIsNeverRetried"
_DELETE = "TestTheClipboardFallbackDeleteIsNeverRepeated"
_COMPOSITE = "TestACompositeNeverPassesAnInnerAnswerThrough"
_EMPTY_WORD = "TestOneEmptyWordDoesNotCloseTheRetractionGate"
_REJECTED = "TestARejectedInsertStillClosesTheGateUnconditionally"
_REAL = "TestARealDefaultStrategyReachesTheRetry"


def _survives(name):
    return f"{_SURVIVES}::{name}"


def _live(name):
    return f"{_LIVE}::{name}"


def _once(name):
    return f"{_ONCE}::{name}"


def _program(name):
    return f"{_PROGRAM}::{name}"


def _branch(name):
    return f"{_BRANCH}::{name}"


def _probe(name):
    return f"{_PROBE}::{name}"


def _gate(name):
    return f"{_GATE}::{name}"


def _rebind(name):
    return f"{_REBIND}::{name}"


def _returning(name):
    return f"{_RETURN}::{name}"


def _unproven(name):
    return f"{_UNPROVEN}::{name}"


def _raw(name):
    return f"{_RAW_INSERT}::{name}"


def _succeeded(name):
    return f"{_SUCCEEDED}::{name}"


def _delete(name):
    return f"{_DELETE}::{name}"


def _composite(name):
    return f"{_COMPOSITE}::{name}"


def _empty_word(name):
    return f"{_EMPTY_WORD}::{name}"


def _rejected(name):
    return f"{_REJECTED}::{name}"


def _real(name):
    return f"{_REAL}::{name}"


# ---------------------------------------------------------------------
# Source excerpts. Every pattern is written with LF; _translate puts
# them into the target's own ending before matching, and the file is
# never normalised. Each one must match EXACTLY once -- the runner
# refuses otherwise, because source.replace(old, new, 1) would edit some
# other part of the file and the round would report a survivor for code
# nothing touched.
# ---------------------------------------------------------------------

# The probe's one decisive line. ``if not hwnd: / return False / try:``
# above it is byte-identical to the opening of
# hwnd_is_invisible_or_toolwindow, so the IsWindow line is what makes
# each pattern here name one place.
_PROBE_DECISION = "        return not win32gui.IsWindow(hwnd)\n"

_PROBE_HEAD = """    if not hwnd:
        return False
    try:
        return not win32gui.IsWindow(hwnd)
"""

_PROBE_FAILURE = """            "hwnd_no_longer_exists(%s) probe failed: %s", hwnd, e,
        )
        return False
"""

# The strategy's initialisation, with the two lines after it, so the
# pattern cannot match a bare assignment somewhere else in the file.
# wh-lost-word-neighbour-paths inserted ``delivered_nothing = False``
# between the two lines this pattern used to hold, which is what made it
# stale; the ``if paste_returned_true:`` line is still the anchor.
_FIELD_INIT = """        target_window_gone = False
        delivered_nothing = False
        if paste_returned_true:
"""

_FIELD_SET = "            target_window_gone = hwnd_no_longer_exists(target_hwnd)\n"

# The handler's retry condition. Since
# wh-paste-target-window-vanished.1.2 it is one boolean, read twice: by
# the bookkeeping block, which has to know which attempt ends the loop,
# and by the retry decision itself.
#
# wh-lost-word-neighbour-paths made it stale twice over: the condition
# gained ``and result.delivered_nothing``, and the strategy test grew
# from one class into a multi-line isinstance over the three OUTER types
# the router returns. The pattern now stops at the ``and isinstance(``
# line, which is the neighbour that makes it name one place while
# leaving the comment block inside the call out of it -- a comment is
# the part of this expression most likely to be reworded, and a pattern
# that held it would go stale on a purely editorial change. The tuple
# itself is quoted separately below, because one mutation attacks it.
_WILL_RETRY = """                will_retry = (
                    attempt == 0
                    and not result.success
                    and result.delivered_nothing
                    and result.target_window_gone
                    and isinstance(
"""

# The allow-list tuple inside that isinstance call, with its trailing
# comma, which is what makes the four lines name one place.
_WILL_RETRY_TYPES = """                        (
                            UnicodeFirstStrategy,
                            StandardStrategy,
                            ClipboardOnlyStrategy,
                        ),
"""

# ---------------------------------------------------------------------
# wh-lost-word-neighbour-paths excerpts.
# ---------------------------------------------------------------------

# ClipboardFallbackStrategy's preflight refusal: the one branch whose
# answer reaches the handler on BOTH default dictation paths, because
# StandardStrategy returns its fallback attempt's result and
# UnicodeFirstStrategy returns StandardStrategy's.
_FALLBACK_PROBE = """                delivered_nothing=True,
                target_window_gone=hwnd_no_longer_exists(
                    _context_hwnd(context)
                ),
"""

# UnicodeFirstStrategy's combination of the inner answers.
_UNICODE_COMBINE = """            delivered_nothing=(
                result.delivered_nothing and standard_result.delivered_nothing
            ),
"""

# StandardStrategy's combination, quoted twice: the assignment alone for
# the delivered_nothing mutation, and the assignment plus BOTH returns
# for the target_window_gone mutation, which has to reach whichever
# return the call takes.
_STANDARD_COMBINE = """        delivered_nothing = (
            shadow_result.delivered_nothing and fallback_result.delivered_nothing
        )
"""

_STANDARD_RETURNS = _STANDARD_COMBINE + """        if shadow_result.clipboard_dirty and not fallback_result.clipboard_dirty:
            # A later refusal must retain the earlier clipboard write.
            return replace(
                fallback_result,
                clipboard_dirty=True,
                delivered_nothing=delivered_nothing,
            )
        return replace(fallback_result, delivered_nothing=delivered_nothing)
"""

# The RejectedInsertionStrategy write in _execute_insert_with_ack. It is
# UNCONDITIONAL on purpose, and the two comment lines above it are what
# separate it from the identical write in raw_insert_text.
_REJECTED_WRITE = """                    # nothing was written, so the shadow buffer's mirror of
                    # the target is still whatever it was.
                    self._used_simple_paste = True
"""

# The retry decision's own read of that boolean. The logger call below
# it is what makes this pattern name one place; the bookkeeping block
# reads the same name two lines differently indented.
_RETRY_BREAK = """                if not will_retry:
                    break
                logger.info(
"""

# The one write of the per-utterance retraction gate on the
# ClipboardOnly branch, with the guard that keeps it off an attempt a
# retry is about to supersede.
#
# wh-lost-word-neighbour-paths added the second term, which made this
# pattern stale once. Finding wh-lost-word-neighbour-paths.1.2 then
# narrowed that term to read two fields instead of one, which made it
# stale a second time; this is the form after that narrowing. The
# ``will_retry`` name in the condition is what separates it from the
# SimplePasteStrategy branch above and from the two parallel writes in
# raw_insert_text, all three of which now read
# ``if not (result.delivered_nothing and result.target_window_gone):``.
_GATE_WRITE = """                    if not will_retry and not (
                        result.delivered_nothing and result.target_window_gone
                    ):
                        self._used_simple_paste = True
"""

# The invalidate that must keep running for BOTH attempts, with the
# elif after it, which is what makes the pattern name one place -- the
# file holds twenty-one other invalidate calls.
_INVALIDATE = """                    self.buffer_manager.invalidate()
                elif isinstance(strategy, RejectedInsertionStrategy):
"""

# The option-2 guard's three decision lines, quoted separately so each
# mutation names the one part it attacks.
_GUARD_HEAD = "                if attempt == 1 and (\n"

_GUARD_EMPTY = "                    not context.process_name\n"

_GUARD_COMPARE = (
    "                    or context.process_name != captured_process_name\n"
)

# The guard's refusal and the assignment that follows it. Replacing the
# break here is what tests the ORDER: the refusal has to happen before
# remember_target runs.
_GUARD_REFUSAL = """                    break
                captured_process_name = context.process_name
"""

# wh-lost-word-neighbour-paths.1.4: the guard that stops a retraction
# crossing from the window the earlier words went to into the window the
# retry chose. The set site sits inside the retry loop, immediately after
# remember_target; the gate sits in retract() after the SimplePaste gate.
_REBIND_CREDIT = (
    "                        if held_credit and not back_at_credited:\n"
)

_REBIND_FLAG = (
    "                            self._retry_rebound_target = True\n"
)

# wh-lost-word-neighbour-paths.1.5: the record of which window each
# credited delivery went to, and the check that reads it. Without the
# record the guard cannot tell a retry that moved from a retry that
# came back, which is the false positive this finding reports.
_RECORD_SUCCESS = (
    "        if not getattr(result, \"success\", False):\n"
    "            return\n"
)

_RECORD_ADD = (
    "        self._credited_target_hwnds.add(_context_hwnd(context) or 0)\n"
)

_RETURN_SPLIT = (
    "        if len(credited) != 1:\n"
    "            return False\n"
)

_RETURN_MATCH = (
    "        return bool(hwnd) and hwnd in credited\n"
)

# wh-lost-word-neighbour-paths hard gate, gap G1: the three writes of
# the per-utterance retraction gate that had no mutation of their own.
# _GATE_WRITE above covers the fourth, the ClipboardOnly branch of
# _execute_insert_with_ack, and it is the only one whose condition
# reads ``will_retry``. These three read the two result fields alone,
# which is what separates them from it and from each other: the first
# is indented for the retry loop and wraps its condition over three
# lines, the second is the same test written on one line at method
# indent, and the third is distinguished by the comment line above it.
_SIMPLE_ACK_WRITE = """                if isinstance(strategy, SimplePasteStrategy):
                    if not (
                        result.delivered_nothing and result.target_window_gone
                    ):
                        self._used_simple_paste = True
"""

_RAW_SIMPLE_WRITE = """        if isinstance(strategy, SimplePasteStrategy):
            if not (result.delivered_nothing and result.target_window_gone):
                self._used_simple_paste = True
"""

_RAW_CLIPBOARD_WRITE = """            # the target cannot be confirmed.
            if not (result.delivered_nothing and result.target_window_gone):
                self._used_simple_paste = True
            # Review wh-kox5.3: invalidate the shadow buffer; see the
"""

_START_CLEAR = (
    "        self._credited_target_hwnds.clear()\n"
    "        self.clipboard.reset_paste_counter()\n"
    "        # wh-9weum Phase 1: clear the soft-fallback strategy's\n"
)


MUTATIONS = [
    # -----------------------------------------------------------------
    # ui/hwnd_utils.py: the probe that decides the window is gone.
    # -----------------------------------------------------------------
    {
        "name": "invert-the-window-probe",
        "target": HWND_UTILS,
        "old": _PROBE_DECISION,
        "new": "        return bool(win32gui.IsWindow(hwnd))\n",
        "expect": [
            _probe("test_a_destroyed_handle_is_proven_gone"),
            _probe("test_a_live_handle_is_not_gone"),
        ],
    },
    {
        "name": "a-missing-handle-is-called-gone",
        "target": HWND_UTILS,
        "old": _PROBE_HEAD,
        "new": _PROBE_HEAD.replace(
            "    if not hwnd:\n        return False\n",
            "    if not hwnd:\n        return True\n",
        ),
        "expect": [
            _probe("test_a_zero_handle_is_not_proven_gone"),
            _probe("test_a_none_handle_is_not_proven_gone"),
        ],
    },
    {
        "name": "a-failed-probe-is-called-gone",
        "target": HWND_UTILS,
        "old": _PROBE_FAILURE,
        "new": _PROBE_FAILURE.replace("        return False\n",
                                      "        return True\n"),
        "expect": [
            _probe("test_a_failed_probe_is_not_proven_gone"),
        ],
    },
    # -----------------------------------------------------------------
    # ui/strategies/specific.py: which branch may report the window gone.
    # -----------------------------------------------------------------
    {
        # The harm here is the opposite of the bug: the word is pasted
        # twice rather than lost. A failure that fired the Ctrl+V would
        # report the window gone, and the handler would paste again.
        "name": "the-strategy-always-reports-the-window-gone",
        "target": STRATEGY,
        "old": _FIELD_INIT,
        "new": _FIELD_INIT.replace(
            "target_window_gone = False", "target_window_gone = True",
        ),
        "expect": [
            _branch("test_a_failure_after_the_keystroke_never_reports_"
                    "the_window_gone"),
            _branch("test_a_delivered_paste_never_reports_the_window_gone"),
        ],
    },
    {
        "name": "the-strategy-never-reports-the-window-gone",
        "target": STRATEGY,
        "old": _FIELD_SET,
        "new": "            target_window_gone = False\n",
        # The handler tests cannot see this one. Every one of them
        # replaces ClipboardOnlyStrategy.insert with a MagicMock, so a
        # mutation inside that method never runs for them. The strategy
        # test below is the whole catcher set.
        "expect": [
            _branch("test_the_no_keystroke_refusal_asks_about_the_"
                    "captured_window"),
        ],
    },
    # -----------------------------------------------------------------
    # ui/ui_action_handler.py: the four conditions on the retry.
    # -----------------------------------------------------------------
    {
        # wh-lost-word-neighbour-paths: the allow-list is a tuple of the
        # three OUTER types the router returns, so the mutation widens
        # the tuple to ``object`` instead of deleting an isinstance
        # call. ``isinstance(strategy, object)`` is True for every
        # object, which is the "no strategy test at all" state.
        "name": "any-strategy-may-retry",
        "target": HANDLER,
        "old": _WILL_RETRY_TYPES,
        "new": "                        object,\n",
        "expect": [
            _live("test_another_strategy_that_fails_is_never_retried"),
            # A real strategy that is deliberately off the allow-list,
            # and proves it delivered nothing, so the allow-list is the
            # only thing refusing its retry.
            _unproven("test_a_simple_paste_refusal_is_never_retried"),
        ],
    },
    {
        "name": "a-live-window-may-retry",
        "target": HANDLER,
        "old": _WILL_RETRY,
        "new": _WILL_RETRY.replace("and result.target_window_gone", "and True"),
        "expect": [
            _live("test_a_live_window_that_lost_the_foreground_is_still_"
                  "refused"),
            # The same boolean governs the retraction gate, and this
            # test measures the retry first: it asserts one capture
            # before it asserts the gate, so a retry that fires for a
            # live window fails it on that count.
            #
            # This test has been renamed twice and its gate assertion
            # moved twice, and its capture assertion never moved, which
            # is why it stays a valid catcher throughout. It was
            # test_a_first_attempt_that_is_not_superseded_still_closes_
            # the_gate at the base, then wh-lost-word-neighbour-paths
            # renamed it to ..._that_proved_nothing_leaves_the_gate_open
            # and inverted the gate assertion, then finding
            # wh-lost-word-neighbour-paths.1.2 moved that assertion back
            # under the name below. Only the capture assertion catches
            # this mutation, so none of that mattered here.
            _gate("test_a_first_attempt_against_a_live_window_still_"
                  "closes_the_gate"),
            # Deliberately NOT expected: the new file's
            # test_a_clipboard_only_word_that_proved_nothing_leaves_
            # retract_open drives the same live-window shape but hands
            # the handler ONE capture, so the retry this mutation lets
            # through raises StopIteration on the second call, the
            # handler's broad except arm swallows it before any
            # bookkeeping, the gate stays open and the test passes.
        ],
    },
    {
        "name": "the-retry-may-run-twice",
        "target": HANDLER,
        "old": _WILL_RETRY,
        "new": _WILL_RETRY.replace("attempt == 0", "attempt <= 1"),
        "expect": [
            _once("test_a_failed_retry_ends_as_today_and_never_loops"),
        ],
    },
    # -----------------------------------------------------------------
    # ui/ui_action_handler.py: which attempt governs the retraction
    # gate (wh-paste-target-window-vanished.1.2). The gate is
    # per-utterance and nothing clears it inside an utterance, so the
    # attempt that ENDS the loop is the only one entitled to close it.
    # -----------------------------------------------------------------
    {
        # The state the finding reported: the superseded attempt closes
        # the gate, so the word the retry delivered cannot be retracted.
        "name": "the-superseded-attempt-closes-the-gate",
        "target": HANDLER,
        "old": _GATE_WRITE,
        "new": "                    self._used_simple_paste = True\n",
        "expect": [
            _gate("test_a_superseded_attempt_leaves_the_delivered_word_"
                  "retractable"),
        ],
    },
    {
        # The opposite error: the gate stops closing for the attempts
        # that really did paste without proof.
        "name": "the-clipboard-only-branch-stops-closing-the-gate",
        "target": HANDLER,
        "old": _GATE_WRITE,
        "new": "                    self._used_simple_paste = False\n",
        # wh-lost-word-neighbour-paths: one of the two tests whose names
        # moved is a valid catcher again, and the other is still not.
        #
        # test_a_first_attempt_against_a_live_window_still_closes_the_
        # gate measures a closing gate once more. Finding
        # wh-lost-word-neighbour-paths.1.2 moved its assertion back,
        # because its attempt binds a LIVE window and only a window
        # proven gone may leave the gate open. It is listed below.
        #
        # test_a_retry_that_fails_and_proved_nothing_leaves_the_gate_
        # open is still NOT a catcher, and substituting its new name
        # would report this mutation as a survivor. Both its attempts
        # carry window_gone=True, so it asserts the gate stays OPEN,
        # which is exactly what this mutation produces.
        "expect": [
            _gate("test_a_first_attempt_that_delivers_still_closes_the_gate"),
            _gate("test_a_first_attempt_against_a_live_window_still_"
                  "closes_the_gate"),
            _gate("test_a_retry_that_delivers_through_clipboard_only_"
                  "closes_the_gate"),
            # The same measurement on the new file, through both
            # callers.
            _empty_word("test_a_clipboard_only_word_that_delivered_still_"
                        "blocks_retract"),
        ],
    },
    {
        # The guard inverted: only a superseded attempt closes the gate.
        #
        # The two retry-through-ClipboardOnly tests are deliberately NOT
        # expected here, and the first sweep of this gate proved why. In
        # both of them the superseded first attempt sets the flag under
        # this mutation, so the gate is already closed by the time the
        # second attempt ends the loop and retract() still answers
        # simple_paste. Listing them made the runner report this
        # mutation as a survivor when three other tests had caught it.
        # wh-lost-word-neighbour-paths: the second term makes the
        # inverted guard a contradiction. will_retry can only be True
        # when result.delivered_nothing is True, so
        # ``will_retry and not result.delivered_nothing`` is False for
        # every attempt and this branch stops closing the gate at all.
        # The mutation is therefore now equivalent in effect to
        # the-clipboard-only-branch-stops-closing-the-gate, and it keeps
        # the same catchers. It is kept because the two attack
        # different text: one deletes the guard, this one inverts the
        # term the merged fix added, and a future change that separates
        # them again will find this entry already in place.
        "name": "the-gate-follows-the-superseded-attempt",
        "target": HANDLER,
        "old": _GATE_WRITE,
        "new": _GATE_WRITE.replace("if not will_retry ", "if will_retry "),
        # test_a_superseded_attempt_leaves_the_delivered_word_
        # retractable is not a catcher: its superseded attempt carries
        # delivered_nothing=True AND reports its window gone, so the
        # second term already keeps the gate open and inverting the
        # first changes nothing for it.
        #
        # test_a_first_attempt_against_a_live_window_still_closes_the_
        # gate IS a catcher, and only became one when finding
        # wh-lost-word-neighbour-paths.1.2 moved its assertion back. Its
        # attempt runs no retry, so under this mutation the gate write
        # is skipped, the gate stays open, and its not_retracted
        # assertion fails. While it asserted an OPEN gate it passed
        # under this mutation and was correctly left out.
        "expect": [
            _gate("test_a_first_attempt_that_delivers_still_closes_the_gate"),
            _gate("test_a_first_attempt_against_a_live_window_still_"
                  "closes_the_gate"),
            _gate("test_a_retry_that_delivers_through_clipboard_only_"
                  "closes_the_gate"),
            _empty_word("test_a_clipboard_only_word_that_delivered_still_"
                        "blocks_retract"),
        ],
    },
    {
        # The wrong fix the reviewer warned against: skipping the whole
        # ClipboardOnly branch for the superseded attempt instead of
        # only its gate write. Without that invalidate the retry's
        # TextPerfector composes against the dead window's mirror.
        "name": "the-superseded-attempt-stops-invalidating",
        "target": HANDLER,
        "old": _INVALIDATE,
        "new": _INVALIDATE.replace(
            "                    self.buffer_manager.invalidate()\n",
            "                    if not will_retry:\n"
            "                        self.buffer_manager.invalidate()\n",
        ),
        "expect": [
            _gate("test_the_superseded_attempt_keeps_its_clipboard_"
                  "bookkeeping"),
        ],
    },
    {
        # The two reads of will_retry must agree. Freezing the retry
        # decision's own read makes the loop end on the first attempt
        # while the bookkeeping still treats it as superseded, which
        # loses the word AND leaves the gate open.
        "name": "the-retry-decision-stops-reading-will-retry",
        "target": HANDLER,
        "old": _RETRY_BREAK,
        "new": _RETRY_BREAK.replace(
            "if not will_retry:", "if not False:",
        ),
        "expect": [
            _survives("test_a_dead_target_window_costs_one_more_capture_"
                      "not_the_word"),
            _survives("test_the_second_attempt_routes_the_newly_captured_"
                      "target"),
        ],
    },
    {
        "name": "the-program-comparison-is-gone",
        "target": HANDLER,
        "old": _GUARD_COMPARE,
        "new": "                    or False\n",
        "expect": [
            _program("test_a_new_target_in_another_application_does_not_"
                     "get_the_word"),
            _program("test_the_refused_retry_leaves_the_remembered_window_"
                     "alone"),
        ],
    },
    {
        "name": "the-empty-program-name-is-accepted",
        "target": HANDLER,
        "old": _GUARD_EMPTY,
        "new": "                    False\n",
        "expect": [
            _program("test_a_capture_that_names_no_application_refuses_"
                     "the_retry"),
        ],
    },
    {
        "name": "the-captured-program-name-is-taken-from-the-retry",
        "target": HANDLER,
        "old": _GUARD_HEAD,
        "new": (
            "                captured_process_name = context.process_name\n"
            "                if attempt == 1 and (\n"
        ),
        "expect": [
            _program("test_a_new_target_in_another_application_does_not_"
                     "get_the_word"),
            _program("test_the_refused_retry_leaves_the_remembered_window_"
                     "alone"),
        ],
    },
    {
        # The ordering guard. The comparison still runs and still
        # decides; it simply stops refusing, so the wrong program is
        # remembered and routed.
        "name": "the-program-check-stops-refusing",
        "target": HANDLER,
        "old": _GUARD_REFUSAL,
        "new": _GUARD_REFUSAL.replace("                    break\n",
                                      "                    pass\n"),
        "expect": [
            _program("test_a_new_target_in_another_application_does_not_"
                     "get_the_word"),
            _program("test_the_refused_retry_leaves_the_remembered_window_"
                     "alone"),
        ],
    },
    # -----------------------------------------------------------------
    # wh-lost-word-neighbour-paths.
    #
    # ui/strategies/specific.py: the new window probe on
    # ClipboardFallbackStrategy's preflight refusal. Only two tests can
    # see these two mutations, and both are in
    # TestARealDefaultStrategyReachesTheRetry: every other test in the
    # two handler files hands the handler a result it built itself, so
    # the real strategy code never runs for them.
    # -----------------------------------------------------------------
    {
        # The probe goes: the default paths report the window as never
        # gone, which is the merged state this bead had to repair -- the
        # word is refused before any keystroke and delivered nowhere.
        "name": "the-default-paths-never-report-the-window-gone",
        "target": STRATEGY,
        "old": _FALLBACK_PROBE,
        "new": """                delivered_nothing=True,
                target_window_gone=False,
""",
        "expect": [
            _real("test_a_real_standard_strategy_retries_when_the_window_"
                  "is_gone"),
        ],
    },
    {
        # The opposite error, and the reason the sibling test exists: a
        # probe hard-coded True retries a live window that merely
        # refused, so the word is delivered twice or lands where the
        # user did not speak it.
        "name": "the-default-paths-always-report-the-window-gone",
        "target": STRATEGY,
        "old": _FALLBACK_PROBE,
        "new": """                delivered_nothing=True,
                target_window_gone=True,
""",
        "expect": [
            _real("test_a_real_standard_strategy_does_not_retry_when_the_"
                  "window_lives"),
        ],
    },
    # -----------------------------------------------------------------
    # ui/ui_action_handler.py: the proof term on the retry.
    # -----------------------------------------------------------------
    {
        # Without the proof term, "the window is gone" is enough to
        # earn a retry, so a strategy that already put input in the
        # queue and then failed is tried again. The named hazard is
        # ClipboardFallbackStrategy's abort after clear_selection's
        # Delete, which the second catcher drives through real code.
        "name": "an-unproven-attempt-may-retry",
        "target": HANDLER,
        "old": _WILL_RETRY,
        "new": _WILL_RETRY.replace("and result.delivered_nothing", "and True"),
        "expect": [
            _unproven("test_a_refusal_that_proves_nothing_is_never_retried"),
            _delete("test_no_second_delete_reaches_a_newly_captured_target"),
        ],
    },
    # -----------------------------------------------------------------
    # ui/strategies/specific.py: how a composite combines the two
    # fields. They are combined DIFFERENTLY, and that is the subtlest
    # part of this bead. delivered_nothing is an "and" across every
    # inner attempt -- any attempt that may have typed spoils the claim
    # for the whole call. target_window_gone passes THROUGH from the
    # inner result the composite returns, because it describes the
    # window the last attempt aimed at; an "and" there would answer
    # False whenever an earlier attempt saw a live window, and no retry
    # would ever fire.
    # -----------------------------------------------------------------
    {
        "name": "unicode-first-passes-the-inner-proof-through",
        "target": STRATEGY,
        "old": _UNICODE_COMBINE,
        "new": """            delivered_nothing=standard_result.delivered_nothing,
""",
        "expect": [
            _composite("test_unicode_first_reports_false_when_its_first_"
                       "attempt_may_have_typed"),
        ],
    },
    {
        "name": "standard-passes-the-inner-proof-through",
        "target": STRATEGY,
        "old": _STANDARD_COMBINE,
        "new": """        delivered_nothing = fallback_result.delivered_nothing
""",
        "expect": [
            _composite("test_standard_reports_false_when_its_shadow_"
                       "attempt_may_have_typed"),
            # The clipboard_dirty rescue branch builds its own result
            # object, so it is a second place the combination can be
            # lost; both returns read the same local.
            _composite("test_standard_keeps_the_earlier_clipboard_write_"
                       "and_the_combination"),
        ],
    },
    {
        # The mutation that pins the difference: combine
        # target_window_gone the way delivered_nothing is combined. The
        # shadow attempt never sets the field, so the "and" answers
        # False for every call and the default paths lose the word
        # again -- with delivered_nothing still True, which is what
        # makes this one look harmless.
        #
        # Both returns are rewritten, because which one runs depends on
        # the inner results' clipboard_dirty: a mutation on the last
        # return alone would be skipped whenever the rescue branch is
        # taken and would read as a survivor.
        "name": "standard-combines-the-window-answer-instead-of-passing-it",
        "target": STRATEGY,
        "old": _STANDARD_RETURNS,
        "new": """        delivered_nothing = (
            shadow_result.delivered_nothing and fallback_result.delivered_nothing
        )
        target_window_gone = (
            shadow_result.target_window_gone
            and fallback_result.target_window_gone
        )
        if shadow_result.clipboard_dirty and not fallback_result.clipboard_dirty:
            # A later refusal must retain the earlier clipboard write.
            return replace(
                fallback_result,
                clipboard_dirty=True,
                delivered_nothing=delivered_nothing,
                target_window_gone=target_window_gone,
            )
        return replace(
            fallback_result,
            delivered_nothing=delivered_nothing,
            target_window_gone=target_window_gone,
        )
""",
        "expect": [
            _real("test_a_real_standard_strategy_retries_when_the_window_"
                  "is_gone"),
        ],
    },
    # -----------------------------------------------------------------
    # ui/ui_action_handler.py: the retraction gate's proof term
    # (neighbour 2), and the one write that must NOT get it.
    # -----------------------------------------------------------------
    {
        # The reported state: one word that failed with no keystroke
        # closes the gate for the whole utterance, so retract() refuses
        # words a counter-crediting strategy really delivered.
        "name": "an-empty-word-still-closes-the-gate",
        "target": HANDLER,
        "old": _GATE_WRITE,
        "new": """                    if not will_retry:
                        self._used_simple_paste = True
""",
        # This reverts BOTH terms now, not one. The mutation below
        # reverts only the window term, which is the narrower defect.
        #
        # wh-lost-word-neighbour-paths.1.2: test_a_first_attempt_that_
        # proved_nothing_leaves_the_gate_open was listed here and is
        # gone, not renamed. Its assertion moved back to a CLOSING
        # gate, which is what this mutation produces, so it passes
        # under the mutation and listing its new name would report a
        # real catch as a survivor. The two entries left both drive
        # attempts whose window is proven gone, so both still measure
        # an open gate.
        "expect": [
            _empty_word("test_a_clipboard_only_word_that_proved_nothing_"
                        "leaves_retract_open"),
            _gate("test_a_retry_that_fails_and_proved_nothing_leaves_the_"
                  "gate_open"),
        ],
    },
    {
        # The defect finding wh-lost-word-neighbour-paths.1.2 reported:
        # the exemption reads emptiness alone and ignores which window
        # the empty attempt bound. ClipboardOnlyStrategy reaches
        # delivered_nothing=True with target_window_gone=False whenever
        # the captured window is alive and merely lost the foreground,
        # and the handler has already re-bound the remembered window to
        # it, so retract()'s focus check passes and the previous
        # window's characters are sent as backspaces into a window
        # WheelHouse never wrote to.
        "name": "the-empty-word-exemption-ignores-the-window",
        "target": HANDLER,
        "old": _GATE_WRITE,
        "new": """                    if not will_retry and not result.delivered_nothing:
                        self._used_simple_paste = True
""",
        # Only the live-window tests can catch this. Every test whose
        # attempt reports window_gone=True behaves identically under
        # this mutation, because both conditions agree there.
        "expect": [
            _gate("test_a_first_attempt_against_a_live_window_still_"
                  "closes_the_gate"),
        ],
    },
    {
        # The naive reading of the same fix, and a regression: applying
        # the proof term to the RejectedInsertionStrategy write. That
        # write is unconditional on purpose. The handler remembers the
        # newly focused window BEFORE it routes, so the remembered
        # window names a window this insert never wrote to while the
        # previous window's characters are still on the counter, and a
        # retraction there deletes text the program did not write.
        "name": "the-rejected-insert-stops-closing-the-gate",
        "target": HANDLER,
        "old": _REJECTED_WRITE,
        "new": """                    # nothing was written, so the shadow buffer's mirror of
                    # the target is still whatever it was.
                    if not result.delivered_nothing:
                        self._used_simple_paste = True
""",
        "expect": [
            _rejected("test_a_rejected_insert_closes_the_gate_although_it_"
                      "proved_nothing"),
        ],
    },
    # -----------------------------------------------------------------
    # ui/ui_action_handler.py: the guard that keeps a retraction inside
    # one window (wh-lost-word-neighbour-paths.1.4).
    # -----------------------------------------------------------------
    {
        # The guard removed entirely. The retry still moves the
        # remembered target, the earlier word's characters are still on
        # the counter, and the retraction sends all of them to the
        # window the retry chose -- deleting text the user typed there.
        "name": "the-rebinding-guard-never-fires",
        "target": HANDLER,
        "old": _REBIND_FLAG,
        "new": (
            "                            self._retry_rebound_target = False\n"
        ),
        "expect": [
            _rebind("test_an_earlier_credited_word_blocks_retraction_"
                    "after_a_rebinding"),
            _rebind("test_the_clipboard_only_route_is_guarded_the_same_way"),
            _returning("test_a_retry_that_moves_to_a_new_window_still_"
                       "refuses"),
            _returning("test_credits_split_across_two_windows_refuse_"
                       "after_a_retry"),
            _returning("test_a_credit_with_no_recorded_window_refuses_"
                       "by_default"),
        ],
    },
    {
        # The guard made unconditional: it stops reading the counters
        # and refuses after any retry. That is the opposite harm --
        # every retried utterance loses its retraction, including the
        # ones whose characters all went to the window the retry chose.
        # Ruling 9 term 1 is what this mutation protects.
        "name": "the-rebinding-guard-ignores-the-credit",
        "target": HANDLER,
        "old": _REBIND_CREDIT,
        "new": "                        if True:\n",
        "expect": [
            _rebind("test_a_rebinding_with_nothing_credited_yet_still_"
                    "retracts"),
            _returning("test_a_retry_that_returns_to_the_credited_"
                       "window_still_retracts"),
            _gate("test_a_retry_that_fails_and_proved_nothing_leaves_the_"
                  "gate_open"),
            _gate("test_a_superseded_attempt_leaves_the_delivered_word_"
                  "retractable"),
            _empty_word("test_a_clipboard_only_word_that_proved_nothing_"
                        "leaves_retract_open"),
            # Deliberately NOT expected:
            # test_a_retry_that_delivers_through_clipboard_only_closes_
            # the_gate. Its retry DELIVERS through ClipboardOnly, so
            # _used_simple_paste closes the older gate first and
            # retract() answers simple_paste before it ever reads this
            # flag. The mutation changes nothing that test can see.
        ],
    },
    # -----------------------------------------------------------------
    # ui/ui_action_handler.py: the credited-window record that tells a
    # retry which moved from a retry which came back
    # (wh-lost-word-neighbour-paths.1.5).
    # -----------------------------------------------------------------
    {
        # The record made blind: every delivery reports window 0. The
        # set then holds one window that no capture can equal, so the
        # guard refuses every retry with a credit -- the .1.4 behaviour
        # this finding corrected.
        "name": "the-delivery-window-is-never-recorded",
        "target": HANDLER,
        "old": _RECORD_ADD,
        "new": "        self._credited_target_hwnds.add(0)\n",
        "expect": [
            _returning("test_a_retry_that_returns_to_the_credited_"
                       "window_still_retracts"),
            _returning("test_a_credit_with_no_recorded_window_refuses_"
                       "by_default"),
            _returning("test_two_windows_without_a_retry_retract_as_they_"
                       "did_before"),
            _returning("test_both_utterance_boundaries_clear_the_credited_"
                       "windows"),
        ],
    },
    {
        # The success check dropped, so an attempt that delivered
        # NOTHING records its window too. The dead helper window then
        # joins the credited set, the set reads as split, and the retry
        # that came back to the real window is refused anyway.
        "name": "a-failed-delivery-is-recorded-too",
        "target": HANDLER,
        "old": _RECORD_SUCCESS,
        "new": "        if False:\n            return\n",
        "expect": [
            _returning("test_a_retry_that_returns_to_the_credited_"
                       "window_still_retracts"),
            _returning("test_a_credit_with_no_recorded_window_refuses_"
                       "by_default"),
        ],
    },
    {
        # The split-credit refusal dropped. Credits that went to two
        # windows now let a retry returning to EITHER of them retract
        # the whole accumulated total, overrunning the characters that
        # went to the other window. Term 2 of the ruling.
        "name": "split-credits-no-longer-refuse",
        "target": HANDLER,
        "old": _RETURN_SPLIT,
        "new": "        if False:\n            return False\n",
        "expect": [
            _returning("test_credits_split_across_two_windows_refuse_"
                       "after_a_retry"),
        ],
    },
    {
        # The window comparison dropped: a single credited window is
        # accepted whatever window the retry captured. That is the .1.4
        # harm returning -- backspaces sent into a window that holds
        # none of the credited characters.
        "name": "any-retry-counts-as-a-return",
        "target": HANDLER,
        "old": _RETURN_MATCH,
        "new": "        return True\n",
        "expect": [
            _returning("test_a_retry_that_moves_to_a_new_window_still_"
                       "refuses"),
        ],
    },
    {
        # The utterance boundary stops clearing the record. Windows
        # credited in an earlier utterance then decide this one's guard,
        # and the set only ever grows.
        "name": "the-utterance-start-keeps-the-old-credited-windows",
        "target": HANDLER,
        "old": _START_CLEAR,
        "new": (
            "        self.clipboard.reset_paste_counter()\n"
            "        # wh-9weum Phase 1: clear the soft-fallback strategy's\n"
        ),
        "expect": [
            _returning("test_both_utterance_boundaries_clear_the_credited_"
                       "windows"),
        ],
    },
    # -----------------------------------------------------------------
    # wh-lost-word-neighbour-paths hard gate, gap G1 (boss, 2026-09-20).
    # Four lines of shipped code that tests already covered but no
    # mutation attacked, so nothing proved those tests can fail.
    # -----------------------------------------------------------------
    {
        # The fifth conjunct of the retry decision. This is NOT an
        # equivalent mutant, although success and target_window_gone
        # look exclusive: _slow_path_preflight builds stale_rejection
        # with success=True at ui/strategies/specific.py:423, and
        # ClipboardFallbackStrategy returns that same object from its
        # preflight arm at :547 with delivered_nothing=True and the
        # window probe's answer. So a result can carry success=True,
        # delivered_nothing=True and target_window_gone=True together,
        # and only this conjunct refuses the retry. Without it the
        # handler retypes a word the program has already told the user
        # it REJECTED, into whichever window the second capture finds.
        "name": "the-successful-rejection-may-retry",
        "target": HANDLER,
        "old": _WILL_RETRY,
        "new": _WILL_RETRY.replace(
            "and not result.success", "and True",
        ),
        "expect": [
            _succeeded("test_a_stale_rejection_whose_window_died_is_never_"
                       "retried"),
        ],
    },
    {
        # The SimplePaste branch of _execute_insert_with_ack loses its
        # exemption and closes the retraction gate for every attempt,
        # which is the state finding .1.2 reported for the branch
        # beside it.
        "name": "the-simple-paste-branch-always-closes-the-gate",
        "target": HANDLER,
        "old": _SIMPLE_ACK_WRITE,
        "new": (
            "                if isinstance(strategy, SimplePasteStrategy):\n"
            "                    self._used_simple_paste = True\n"
        ),
        "expect": [
            _empty_word("test_a_simple_paste_word_that_proved_nothing_"
                        "leaves_retract_open"),
        ],
    },
    {
        # The same loss in raw_insert_text's SimplePaste branch.
        # Other tests in the swept file DO drive raw_insert_text;
        # none of them can catch this mutant. The comment above
        # _RAW_INSERT names each outside caller and the reason it
        # misses, so TestRawInsertTextFollowsTheSameRule is still
        # the only possible catcher.
        "name": "the-raw-simple-paste-branch-always-closes-the-gate",
        "target": HANDLER,
        "old": _RAW_SIMPLE_WRITE,
        "new": (
            "        if isinstance(strategy, SimplePasteStrategy):\n"
            "            self._used_simple_paste = True\n"
        ),
        "expect": [
            _raw("test_a_simple_paste_raw_insert_that_proved_nothing_"
                 "leaves_retract_open"),
        ],
    },
    {
        # The same loss in raw_insert_text's ClipboardOnly branch. The
        # comment line above the write is what makes this pattern name
        # one place rather than two.
        "name": "the-raw-clipboard-only-branch-always-closes-the-gate",
        "target": HANDLER,
        "old": _RAW_CLIPBOARD_WRITE,
        "new": (
            "            # the target cannot be confirmed.\n"
            "            self._used_simple_paste = True\n"
            "            # Review wh-kox5.3: invalidate the shadow buffer; "
            "see the\n"
        ),
        "expect": [
            _raw("test_a_clipboard_only_raw_insert_that_proved_nothing_"
                 "leaves_retract_open"),
        ],
    },
]

def _edits(mutation):
    """Every (target, old, new) this mutation applies, in order."""
    if "edits" in mutation:
        return [(e["target"], e["old"], e["new"]) for e in mutation["edits"]]
    return [(mutation["target"], mutation["old"], mutation["new"])]


def _selection(mutation):
    return mutation.get("selection", list(FEATURE_TEST_FILES))


def _endings(originals):
    """Each target's own line ending, read from its bytes.

    The patterns in this file are written with LF. A target stored with
    CRLF makes every multi-line pattern miss, which reads as a survivor.
    The answer is to translate the PATTERN, never to rewrite the file:
    normalising a shared tracked source would show up as a diff in every
    other session's checkout.
    """
    return {
        target: ("\r\n" if b"\r\n" in raw else "\n")
        for target, raw in originals.items()
    }


def _translate(text, ending):
    if ending == "\n":
        return text
    return text.replace("\n", ending)


def _clear_pycache(root=None):
    """Remove every __pycache__ under the service; return (error, interrupt).

    Python decides whether cached bytecode is current from the source
    file's (mtime, size). Several mutations here keep the file within a
    few bytes of its original length, so a stale .pyc beside a restored
    source can be executed by the next process.
    ``PYTHONDONTWRITEBYTECODE`` is not a second line of defence: it stops
    Python WRITING a .pyc, not reading one that is already there.

    ``ignore_errors=True`` is what makes the check after it necessary. A
    directory Windows will not delete leaves ``rmtree`` silent and the
    directory in place, so the only way to know the sweep worked is to
    look. A survivor goes to the second pass, because the hold is
    usually momentary.

    The interrupt is held rather than allowed out, for the reason
    ``_restore`` records: escaping here skips the message that tells the
    operator a mutant may still be in a real source file.
    """
    root = SERVICE_DIR if root is None else root
    interrupt = None
    survivors, swept = [], False
    for _pass in (1, 2):
        survivors, swept = [], False
        try:
            for d in root.rglob("__pycache__"):
                if ".venv" not in d.parts:
                    shutil.rmtree(d, ignore_errors=True)
                    if d.exists():
                        survivors.append(d)
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            continue
        swept = True
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
    later verdict would be read from a process that may have imported
    bytecode for a source the gate replaced. The held interrupt is
    raised FIRST, so a Ctrl+C during the clear ends the run as an
    interrupt rather than as an exit code that reads like a refusal.
    """
    error, interrupt = _clear_pycache()
    if error:
        print(f"ERROR {error}")
    if interrupt is not None:
        raise interrupt
    return 1 if error else None


def _restore(target, original, mutated):
    """Put the pre-run bytes back; return (error string or None, interrupt).

    ``write_bytes``, never ``write_text``: on Windows ``write_text``
    translates every newline to CRLF, which would rewrite the whole file
    and break every multi-line pattern here on the next run.

    Never overwrite bytes this run did not write. A save landing from an
    editor while pytest ran would otherwise be replaced by the stale
    pre-run snapshot and lost, so a file holding neither the original
    nor the mutant is reported and left alone.

    A KeyboardInterrupt is held rather than allowed out, and returned
    for the caller to re-raise once the cleanup is done. The first
    Ctrl+C stops pytest and reaches this restore; a second one escaping
    from here would leave a real source file holding the mutant, where
    every later run -- and every other session sharing the checkout --
    reads it as the real code. That is the worst outcome this gate has.
    Bounded at two attempts so a held-down Ctrl+C cannot loop.

    Both seams are inside the try: the read that decides what the file
    holds, and the write that puts the original back.
    """
    interrupt = None
    for _attempt in (1, 2):
        try:
            current = target.read_bytes()
            if current == original:
                return None, interrupt
            if current != mutated:
                return (f"{target} changed while pytest ran; refusing to "
                        "overwrite the concurrent edit with the stale "
                        "pre-run snapshot -- reconcile the file by hand"
                        ), interrupt
            target.write_bytes(original)
            return None, interrupt
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            continue
        except OSError as exc:
            return (f"restore failed; the MUTANT REMAINS in {target}: "
                    f"{exc}"), interrupt
    return (f"interrupted during every restore attempt; the MUTANT MAY "
            f"REMAIN in {target} -- check it before the next run"), interrupt


def _write_mutants(changed, written):
    """Put every mutant on disk; return an error string or None.

    ``written`` is the CALLER's registry and is filled in place BEFORE
    each write, because a KeyboardInterrupt escaping this function has
    to leave the caller holding every target it touched -- including the
    one it was in the middle of writing. The sweep restores from this
    registry and nothing else.
    """
    for target, raw in changed.items():
        written[target] = raw
        try:
            target.write_bytes(raw)
        except OSError as exc:
            return f"cannot write the mutant to {target}: {exc}"
    return None


def _restore_written(name, written, originals):
    """Put back every target the write step registered.

    Returns ``(problems, interrupt)``: error strings already prefixed
    with the mutation name, and the first held KeyboardInterrupt.
    """
    problems, interrupt = [], None
    for target, raw in written.items():
        problem, stop = _restore(target, originals[target], raw)
        if problem:
            problems.append(f"{name}: {problem}")
        if stop is not None and interrupt is None:
            interrupt = stop
    return problems, interrupt


def _final_mismatches(originals):
    """Every target whose bytes are wrong at the end, as error strings.

    This is the last place a tracked file still holding a mutant can be
    noticed, so a run that notices one must not report success. The read
    is inside a ``try`` because this runs in the sweep's own ``finally``
    and an OSError raised there would replace whatever exception was on
    its way out -- including a held Ctrl+C.
    """
    problems = []
    for target, raw in sorted(originals.items()):
        try:
            current = target.read_bytes()
        except OSError as exc:
            problems.append(f"cannot read {target} at the end of the sweep; "
                            f"check it by hand: {exc}")
            continue
        if current != raw:
            problems.append(f"{target} did not match its pre-run bytes at "
                            "the end of the sweep; check it by hand")
    return problems


def _run_pytest(selection):
    """Run the named selection in ONE pytest process.

    ``COLUMNS`` is what makes a verdict readable. pytest appends the
    failure's first line to a short-summary record only when the whole
    line FITS the terminal width (``_get_line_with_reprcrash_message``
    in _pytest/terminal.py), and silently drops it when it does not.
    ``capture_output=True`` gives the child no tty, so the width falls
    back to 80 while the node ids here are already about 115 characters
    -- every record then arrived with no reason on it, and the check
    that a catch came from the test's own assertion could not be made
    for a single mutation. Measured on this suite: without COLUMNS the
    whole sweep reported 30 unconfirmable catches and zero verdicts.
    """
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = "1000"
    return subprocess.run(
        [sys.executable, "-m", "pytest", *selection, "-q", "-rfE",
         "-p", "no:cacheprovider", "-p", "no:randomly"],
        cwd=SERVICE_DIR, env=env, capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S,
    )


def _node_key(node_id):
    """``Class::method`` for a class-based test, ``method`` for a bare one.

    The bare method name is not enough: test_insertion_router.py and
    test_phase1_soft_fallback.py both hold a
    ``test_unknown_soft_reject_routes_to_clipboard_only``, in different
    classes.
    """
    return node_id.split("::", 1)[1].split("[")[0].strip()


def _summary_section(output):
    """The lines after pytest's short-summary header, or None.

    Only this section carries test records. A line starting ``ERROR ``
    or ``FAILED `` in the BODY is something else -- a captured log
    record, a traceback line -- and rejection-logs-error-again
    deliberately puts an ERROR-level log record there, so parsing the
    body would invent a verdict.
    """
    lines = output.splitlines()
    start = None
    for index, line in enumerate(lines):
        if SUMMARY_HEADER in line and line.startswith("="):
            start = index + 1
    if start is None:
        return None
    return lines[start:]


def _failure_records(summary):
    """``{Class::method: [(kind, reason), ...]}`` from the summary lines.

    A parametrized test contributes one record per parameter and they
    collapse onto one key, so the reason list can hold more than one
    entry.
    """
    records = {}
    for line in summary:
        for kind in ("FAILED ", "ERROR "):
            if not line.startswith(kind):
                continue
            rest = line[len(kind):]
            node = rest.split(" ", 1)[0]
            reason = rest.split(" - ", 1)[1].strip() if " - " in rest else ""
            if "::" in node:
                records.setdefault(_node_key(node), []).append(
                    (kind.strip(), reason)
                )
            break
    return records


def _unrelated(records, name):
    """Why this test's failure is not the assertion, or None.

    A mutation that makes a test die on an AttributeError upstream of
    its assertion has established nothing about the behaviour, so the
    reason is read rather than assumed.
    """
    for kind, reason in records.get(name, []):
        if kind != "FAILED":
            return f"{name} was reported as {kind}, not a test failure"
        if not reason:
            return (f"{name} carries no reason on its summary line, so the "
                    "failure cannot be confirmed as its assertion")
        if not reason.startswith(ASSERTION_REASONS):
            return f"{name} failed on {reason!r}, which is not an assertion"
    return None


def _apply(mutation, sources, endings):
    """Return (working sources, error string or None).

    Each pattern must match EXACTLY once. Zero matches and two or more
    are different repairs -- a pattern that has gone stale against a
    changed source, and a pattern that has stopped naming one place --
    so they are reported apart, and neither is a survivor.
    """
    working = dict(sources)
    for target, old, new in _edits(mutation):
        ending = endings[target]
        old = _translate(old, ending)
        new = _translate(new, ending)
        count = working[target].count(old)
        if count == 0:
            return None, (f"{mutation['name']}: pattern not found in "
                          f"{target.name}")
        if count > 1:
            return None, (f"{mutation['name']}: pattern ambiguous in "
                          f"{target.name}, matched {count} times, need 1")
        working[target] = working[target].replace(old, new, 1)
    for target, mutated in working.items():
        if mutated == sources[target]:
            continue
        try:
            compile(mutated, str(target), "exec")
        except SyntaxError as exc:
            return None, f"{mutation['name']}: mutant does not compile: {exc}"
    return working, None


def _check(sources, endings):
    """Answer "are the patterns current, and does each mutant parse".

    The counts stay separate. A stale pattern and a mutant that does not
    parse are different repairs, and summing them would hide which one
    this run found. A mutant that does not compile is the worse of the
    two: it reads as ``caught`` in a full sweep, because the interpreter
    rejected it before a single test ran.
    """
    stale, broken = [], []
    for mutation in MUTATIONS:
        _, error = _apply(mutation, sources, endings)
        if error is None:
            continue
        if "does not compile" in error:
            broken.append(error)
        else:
            stale.append(error)
    for line in stale + broken:
        print(f"ERROR {line}")
    print(f"checked {len(MUTATIONS)} patterns, {len(stale)} stale, "
          f"{len(broken)} that do not compile")
    return 1 if stale or broken else 0


def _collect(test_file):
    """Every ``Class::method`` pytest can collect from one file.

    Returns ``(names, error string or None)``. The name set is always a
    real set, empty on failure rather than None: the caller merges it
    with ``|=``, and ``set() |= None`` raises TypeError, which would end
    the run before a single mutation was applied and turn a collection
    problem into a crash with the wrong cause on it.
    """
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "--collect-only", "-q",
         "-p", "no:cacheprovider", "-p", "no:randomly"],
        cwd=SERVICE_DIR, capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S,
    )
    if collected.returncode != 0:
        return set(), (f"cannot collect {test_file}: "
                       f"{collected.stdout[-800:]}{collected.stderr[-800:]}")
    return {
        _node_key(line.strip())
        for line in collected.stdout.splitlines() if "::" in line
    }, None


def main(argv):
    args = list(argv[1:])
    check_only = "--check" in args
    selected = [a for a in args if a != "--check"]

    names = [m["name"] for m in MUTATIONS]
    duplicate = sorted({n for n in names if names.count(n) > 1})
    if duplicate:
        print(f"ERROR: duplicate mutation names: {duplicate}")
        return 1

    if selected:
        unknown = [n for n in selected if n not in names]
        if unknown:
            print(f"ERROR: no such mutation: {unknown}")
            return 1
        mutations = [m for m in MUTATIONS if m["name"] in selected]
    else:
        mutations = list(MUTATIONS)

    originals = {}
    for target in sorted(TARGETS):
        originals[target] = target.read_bytes()
    endings = _endings(originals)
    sources = {t: raw.decode("utf-8") for t, raw in originals.items()}
    for target, ending in endings.items():
        print(f"{target.name}: {'CRLF' if ending == chr(13) + chr(10) else 'LF'}"
              " line endings; patterns translated to match")

    if check_only:
        return _check(sources, endings)

    errors, survivors, caught = [], [], []

    # Every expected test name must exist before the first mutation. A
    # name that no longer exists can never appear in the failed set, so
    # a genuine catch would be reported as a survivor.
    every_test = set()
    for test_file in FEATURE_TEST_FILES:
        found, problem = _collect(test_file)
        if problem:
            print(f"ERROR {problem}")
            return 1
        every_test |= found
    for mutation in mutations:
        for name in mutation["expect"]:
            if name not in every_test:
                errors.append(f"{mutation['name']}: no such test {name}")
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1
    print(f"name validation: every expected test exists "
          f"({len(every_test)} collected across "
          f"{len(FEATURE_TEST_FILES)} files)")

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

    held = None
    try:
        for mutation in mutations:
            name = mutation["name"]
            working, error = _apply(mutation, sources, endings)
            if working is None:
                errors.append(str(error))
                print(f"ERROR {error}")
                continue

            changed = {
                target: source.encode("utf-8")
                for target, source in working.items()
                if source != sources[target]
            }
            written = {}
            result = None
            try:
                write_error = _write_mutants(changed, written)
                if write_error is not None:
                    errors.append(f"{name}: {write_error}")
                    print(f"ERROR {name}: {write_error}")
                else:
                    result = _run_pytest(_selection(mutation))
            except subprocess.TimeoutExpired:
                errors.append(f"{name}: mutant never terminated within "
                              f"{RUN_TIMEOUT_S}s")
                print(f"ERROR {name}: mutant never terminated")
                result = None
            finally:
                # The restore runs whatever happened above, including
                # the timeout arm: a mutant left on disk is the worst
                # outcome this gate has.
                problems, stop = _restore_written(name, written, originals)
                for problem in problems:
                    errors.append(problem)
                    print(f"ERROR {problem}")
                if stop is not None and held is None:
                    held = stop
                cache_error, stop = _clear_pycache()
                if cache_error:
                    errors.append(f"{name}: {cache_error}")
                    print(f"ERROR {name}: {cache_error}")
                if stop is not None and held is None:
                    held = stop
            if held is not None:
                raise held
            if result is None:
                continue

            combined = result.stdout + result.stderr
            if TIMEOUT_BANNER in combined:
                errors.append(f"{name}: suite-timeout-abort, no verdict")
                print(f"ERROR    {name}: suite-timeout-abort")
                continue

            # The exit code says which reading of a missing summary
            # section is right. pytest prints no short-summary section
            # when nothing failed, and that absence IS the verdict: a
            # survivor. Treating it as an unreadable run instead turned
            # never-clear-the-rejection-flag into an error and hid the
            # survivor it had actually found.
            if result.returncode == 0:
                records = {}
            elif result.returncode == 1:
                summary = _summary_section(combined)
                if summary is None:
                    errors.append(f"{name}: pytest reported failures but "
                                  "printed no short-summary section, so "
                                  "there is no verdict to read")
                    print(f"ERROR    {name}: no short-summary section")
                    continue
                records = _failure_records(summary)
            else:
                # 2 interrupted, 3 internal error, 4 usage error, 5 no
                # tests collected. None of them is a verdict.
                errors.append(f"{name}: pytest exited {result.returncode}, "
                              "which is neither a pass nor a test failure")
                print(f"ERROR    {name}: pytest exited "
                      f"{result.returncode}")
                continue
            failed = set(records)
            expect = set(mutation["expect"])
            missing = sorted(expect - failed)
            if missing:
                survivors.append(
                    f"{name}: expected failures missing: {missing}"
                )
                print(f"SURVIVED {name}: {missing} did not fail")
                continue

            unrelated = [
                problem for problem in
                (_unrelated(records, n) for n in sorted(expect))
                if problem
            ]
            if unrelated:
                for problem in unrelated:
                    errors.append(f"{name}: {problem}")
                    print(f"ERROR    {name}: {problem}")
                continue

            caught.append(name)
            print(f"caught   {name}: {sorted(expect)}")
            collateral = sorted(failed - expect)
            if collateral:
                print(f"         {name} also failed: {collateral}")
    finally:
        # An error, not a warning: a run that noticed a tracked file
        # still holding a mutant must not report success.
        for problem in _final_mismatches(originals):
            errors.append(problem)
            print(f"ERROR {problem}")

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
