"""Mutation gate for the unverified-target paste routing (wh-paste-when-unverified.2).

WHY THIS GATE EXISTS. The tests this bead added were written before the
source change and were seen to fail. That is the first half of the rule.
The second half is that a test can pass for the wrong reason, so each
protected behaviour is broken here at the level of the DECISION the code
makes -- which strategy the reject branch returns, which guard decides
it, whether a verdict is parked, which level the refusal logs at, and
whether a rejection counts as delivery -- and not only by deleting the
change.

WHAT THE BEAD CHANGED, and therefore what this gate defends:

  the reject branch of ui/router.py routes to ClipboardOnlyStrategy
      revert-the-routing            the paste branch returns the refusal
                                    strategy again, so the words are
                                    dropped rather than pasted
      invert-the-none-guard         the ``is not None`` test is flipped,
                                    so a wired router refuses and an
                                    unwired one returns None
      reject-falls-through-to-keystrokes
                                    both early returns are removed, so a
                                    reject reaches the default
                                    length-based branch. That branch
                                    sends KEYSTROKES for 50 characters
                                    or fewer, and keystrokes into a
                                    browser page body are the one
                                    recorded harm in this area
                                    (wh-fc1x.1, one page scroll per word
                                    in Brave). This mutation is the only
                                    one that guards that harm.
      park-a-verdict-on-the-paste-path
                                    set_pending_verdict is called before
                                    the paste return, so a verdict is
                                    left on a strategy that was not
                                    returned and a later insert that
                                    really does route to the refusal
                                    consumes it
      remove-the-empty-identity-guard
                                    the whole guard goes, so a reject
                                    whose captured identity is the
                                    all-zero TargetIdentity() is pasted
                                    again. That identity fails
                                    is_current() on its first line, so
                                    verified_paste refuses the send
                                    every time and logs the refusal at
                                    ERROR -- one Windows notification on
                                    the direct path, a second one on the
                                    letter-buffer path, and a
                                    PasteFailedError out of
                                    raw_insert_text
      weaken-the-elevated-refusal   the elevated-window branch returns
                                    the paste strategy. Windows discards
                                    input sent to a higher-integrity
                                    window either way, so a paste there
                                    reports a false success. This is
                                    epic acceptance criterion 4, and its
                                    catchers are all in
                                    TestElevationRouting.

  the refused final letter-buffer flush in ui/ui_action_handler.py
      rejection-logs-error-again    the rejection arm logs ERROR again.
                                    An ERROR record IS the Windows
                                    notification -- ErrorNotificationHandler
                                    (utils/error_notifier.py) is a
                                    logging handler at ERROR level that
                                    utils/logging_setup.py attaches to
                                    the root logger. This is acceptance
                                    criterion 5.
      never-record-the-rejection    the flag is a constant False where
                                    the handler reads the insertion
                                    result, so no refusal is ever
                                    recognised
      never-clear-the-rejection-flag
                                    the per-attempt reset is removed, so
                                    a refusal can leak into a later
                                    attempt
      deliver-a-rejection-as-success
                                    the delivery test stops subtracting
                                    the rejection, so a pre-send refusal
                                    reads as delivered text

  the retraction gate for a rejected insert in ui/ui_action_handler.py
      a-rejected-insert-stays-retractable
                                    the gate goes in BOTH get_strategy
                                    callers at once, so a later
                                    retraction is free to run after an
                                    insert that delivered nothing. Both
                                    callers remember the newly focused
                                    window BEFORE they route, so the
                                    retraction's focus check passes
                                    against a window this program never
                                    wrote to, while the characters
                                    credited in the PREVIOUS window are
                                    still on the counter -- the
                                    backspaces then delete the user's
                                    own text in the new window
                                    (wh-paste-when-unverified.3.4)

  the retryable clipboard read failure in ui/clipboard_operations.py
      recovered-read-failure-logs-error-again
                                    ``_safe_paste`` logs ERROR again for
                                    a clipboard read it could not
                                    complete. That read failure is
                                    retryable by construction: grep for
                                    ``_safe_paste`` under services/ finds
                                    the definition at
                                    ui/clipboard_operations.py:258 and
                                    exactly one caller, the verification
                                    loop at :745, which counts the None
                                    as a lock failure and polls again. So
                                    a paste that recovers on the next
                                    poll -- or that proceeds
                                    optimistically and lands -- would
                                    still raise the Windows notification,
                                    for the same reason as
                                    rejection-logs-error-again above. The
                                    give-up arm of verified_paste keeps
                                    its own logger.error, so a genuine
                                    failure still notifies.

A NOTE ON THE EXPECT LISTS. ``expect`` is the set of tests that MUST
fail for the mutation to count as caught. It is a lower bound, not an
exact claim: a routing mutation reaches many router tests at once, and
listing every one of them would bury the tests that actually establish
the behaviour. Every other failure is printed on an ``also failed`` line
for the same mutation, so nothing is hidden, and a reader can see the
blast radius beside the proof.

A NOTE ON READING VERDICTS. Only pytest's short-summary section is
parsed. A line starting ``ERROR `` or ``FAILED `` in the BODY of the
output is not a test record: rejection-logs-error-again deliberately
makes a captured log record at ERROR level appear there, and reading the
body would turn that record into a phantom verdict.

A NOTE ON UNRELATED CRASHES. A mutation that makes an expected test die
on an AttributeError upstream of its assertion has proved nothing. Each
expected failure's reason is read from the summary line and must be an
assertion (``assert ...``, ``AssertionError: ...``, ``Failed: ...``).
Anything else is reported as an ERROR, not a catch.

crewcut: the mutant is written straight over the target with
``write_bytes`` rather than through a neighbour file and ``os.replace``.
A hard kill inside that one write can therefore leave a tracked source
holding part of a mutant, where the atomic form used by
mutation_gate_keyboard_refusal_notice.py cannot. Two things bound the
exposure: the restore runs in a ``finally`` with one retry, and the end
of the sweep reads every target and reports any file that does not hold
its pre-run bytes. To remove the limit, lift ``_atomic_write`` out of
that gate into a shared helper and call it from both.

Run it from services/wheelhouse. NEVER while a test suite is running in
the same worktree -- this file rewrites the sources that suite imports:

    <interpreter> tests/mutation_gate_unverified_target_paste.py

``--check`` answers "do the patterns still match, and does each mutant
still parse" in seconds, without running a single test. Naming
mutations on the command line runs only those, and the scope line says
so.
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

ROUTER = SERVICE_DIR / "ui" / "router.py"
HANDLER = SERVICE_DIR / "ui" / "ui_action_handler.py"
CLIPBOARD = SERVICE_DIR / "ui" / "clipboard_operations.py"

ROUTER_TESTS = "tests/test_ui/test_insertion_router.py"
HANDLER_TESTS = "tests/test_ui/test_ui_action_handler.py"
FALLBACK_TESTS = "tests/test_ui/test_phase1_soft_fallback.py"
CLIPBOARD_TESTS = "tests/test_ui/test_clipboard_operations.py"

FEATURE_TEST_FILES = (
    ROUTER_TESTS, FALLBACK_TESTS, HANDLER_TESTS, CLIPBOARD_TESTS,
)

# The four files run in well under a minute clean. No mutation here can
# make a test wait on a timer -- every one changes a return, a guard, a
# log level or an assignment -- so this limit exists only to turn a hang
# into a reported error rather than a lost run.
RUN_TIMEOUT_S = 600

# pytest-timeout kills the process before the short summary prints, so a
# parser would find no records and report a survivor for a mutation that
# may have been caught.
TIMEOUT_BANNER = "+++ Timeout +++"

SUMMARY_HEADER = "short test summary info"

# A catch has to come from the test's own assertion. These are the
# leading tokens a pytest summary line carries when it does.
ASSERTION_REASONS = ("assert", "AssertionError", "Failed", "DID NOT RAISE")

_PASTE = "TestUnverifiedTargetPaste"
_EMPTY_ROUTER = "TestEmptyIdentityRejectStaysSilent"
_EMPTY_HANDLER = "TestEmptyIdentityRejectIsSilent"
_PRED = "TestPredicateRouting"
_ELEV = "TestElevationRouting"
_MAPPING = "TestRouterSoftRejectMapping"
_LEVEL = "TestLetterBufferRejectionLogLevel"
_RETURN = "TestIntelligentInsertTextReturn"
_WRAP = "TestWrapOrInsert"
_CLIP = "TestVerifiedPaste"
_UNRETRACTABLE = "TestRejectedInsertLeavesTheUtteranceUnretractable"


def _paste(name):
    return f"{_PASTE}::{name}"


def _empty_router(name):
    return f"{_EMPTY_ROUTER}::{name}"


def _empty_handler(name):
    return f"{_EMPTY_HANDLER}::{name}"


def _pred(name):
    return f"{_PRED}::{name}"


def _elev(name):
    return f"{_ELEV}::{name}"


def _mapping(name):
    return f"{_MAPPING}::{name}"


def _level(name):
    return f"{_LEVEL}::{name}"


def _clip(name):
    return f"{_CLIP}::{name}"


def _unretractable(name):
    return f"{_UNRETRACTABLE}::{name}"


# ---------------------------------------------------------------------
# Source excerpts, quoted once where more than one mutation uses them.
# Every pattern is written with LF; _translate puts them into the
# target's own ending before matching, and the file is never normalised.
# ---------------------------------------------------------------------

# The paste return, with the two lines above it. ``return
# self.clipboard_only`` alone appears on the soft-allow accept path as
# well (at a shallower indent), so the debug call's last argument and
# its closing bracket are what make this the reject path and nothing
# else.
_PASTE_RETURN = """                        verdict.process_name or "?",
                    )
                    return self.clipboard_only
"""

# The legacy no-ClipboardOnly refusal. ``return self.rejected`` at this
# indent appears twice in the file -- here and in the elevated-window
# branch, whose set_pending block is identical line for line -- so the
# comment above it is what picks this one out.
_LEGACY_REFUSAL = """                # (legacy test fixtures).
                set_pending = getattr(
                    self.rejected, "set_pending_verdict", None,
                )
                if callable(set_pending):
                    set_pending(verdict)
                return self.rejected
"""

# The empty-identity guard, whole. Its own two decision lines plus the
# block they control, so removing it takes the guard out entirely rather
# than leaving a condition that could still be read as intact. The
# ``captured_identity`` name appears nowhere else in the file, so this
# pattern names one place.
_EMPTY_IDENTITY_GUARD = """                captured_identity = getattr(context, "target_identity", None)
                if captured_identity == TargetIdentity():
                    logger.debug(
                        "Router: rejected text target with an EMPTY "
                        "captured identity -> RejectedInsertionStrategy "
                        "(silent drop; a paste would be refused "
                        "pre-send and logged at ERROR) reason=%s "
                        "class=%s process=%s",
                        verdict.reason,
                        verdict.class_name or "?",
                        verdict.process_name or "?",
                    )
                    return self.rejected
"""

# The elevated-window refusal, picked out by the section comment that
# follows it.
_ELEVATED_REFUSAL = """                if callable(set_pending):
                    set_pending(verdict)
                return self.rejected

        # 2. Flutter? -> Flutter Strategy. Runs before the text-target
"""

MUTATIONS = [
    # -----------------------------------------------------------------
    # ui/router.py: the reject branch pastes instead of dropping.
    # -----------------------------------------------------------------
    {
        # The behaviour exactly as it stood before the bead: a reject
        # selects the refusal strategy and the dictated words are
        # dropped.
        "name": "revert-the-routing",
        "target": ROUTER,
        "old": _PASTE_RETURN,
        "new": _PASTE_RETURN.replace(
            "return self.clipboard_only", "return self.rejected",
        ),
        "expect": [
            _paste("test_every_reject_reason_routes_to_clipboard_only"),
            _paste("test_reject_never_reaches_the_length_based_branch"),
            _paste("test_reject_keeps_the_debug_telemetry_line"),
            _pred("test_unknown_soft_reject_routes_to_clipboard_only"),
            _mapping("test_unknown_soft_reject_routes_to_clipboard_only"),
            _mapping("test_default_reject_routes_to_clipboard_only"),
            _mapping("test_denylist_reject_routes_to_clipboard_only"),
        ],
    },
    {
        # The guard decides which of the two routes a reject takes. With
        # the test flipped, a production router (ClipboardOnly wired)
        # refuses, and a legacy fixture returns the None it was given.
        "name": "invert-the-none-guard",
        "target": ROUTER,
        "old": "                if self.clipboard_only is not None:\n",
        "new": "                if self.clipboard_only is None:\n",
        "expect": [
            _paste("test_every_reject_reason_routes_to_clipboard_only"),
            _paste("test_reject_never_reaches_the_length_based_branch"),
            _paste("test_reject_keeps_the_debug_telemetry_line"),
            _paste("test_reject_does_not_set_a_pending_verdict_when_pasting"),
            _paste("test_reject_without_clipboard_only_still_returns_rejected"),
            _pred("test_unknown_soft_reject_routes_to_clipboard_only"),
            _mapping("test_unknown_soft_reject_routes_to_clipboard_only"),
        ],
    },
    {
        # The one recorded harm in this area. Without both returns a
        # reject falls through to the default length-based branch, which
        # sends keystrokes for 50 characters or fewer -- and keystrokes
        # into a browser page body scrolled the page once per word in
        # Brave (wh-fc1x.1). Neither return is the only statement in its
        # block, and ``pass`` goes in each place regardless so the
        # mutant cannot fail to compile for a reason unrelated to the
        # behaviour.
        "name": "reject-falls-through-to-keystrokes",
        "target": ROUTER,
        "edits": [
            {
                "target": ROUTER,
                "old": "                    return self.clipboard_only\n",
                "new": "                    pass\n",
            },
            {
                "target": ROUTER,
                "old": _LEGACY_REFUSAL,
                "new": _LEGACY_REFUSAL.replace(
                    "                return self.rejected\n",
                    "                pass\n",
                ),
            },
        ],
        "expect": [
            _paste("test_every_reject_reason_routes_to_clipboard_only"),
            _paste("test_reject_never_reaches_the_length_based_branch"),
            _paste("test_reject_keeps_the_debug_telemetry_line"),
            _paste("test_reject_without_clipboard_only_still_returns_rejected"),
            _pred("test_unknown_soft_reject_routes_to_clipboard_only"),
            _mapping("test_unknown_soft_reject_routes_to_clipboard_only"),
            _mapping("test_default_reject_routes_to_clipboard_only"),
            _mapping("test_denylist_reject_routes_to_clipboard_only"),
        ],
    },
    {
        # A verdict parked on a strategy that was not returned is
        # consumed by the next insert that really does route there --
        # the elevated refusal -- which then reports the wrong reason.
        "name": "park-a-verdict-on-the-paste-path",
        "target": ROUTER,
        "old": _PASTE_RETURN,
        "new": (
            '                        verdict.process_name or "?",\n'
            "                    )\n"
            "                    set_pending = getattr(\n"
            '                        self.rejected, "set_pending_verdict", '
            "None,\n"
            "                    )\n"
            "                    if callable(set_pending):\n"
            "                        set_pending(verdict)\n"
            "                    return self.clipboard_only\n"
        ),
        "expect": [
            _paste("test_reject_does_not_set_a_pending_verdict_when_pasting"),
            _pred("test_unknown_soft_reject_routes_to_clipboard_only"),
        ],
    },
    {
        # The guard removed entirely, which is the behaviour the boss
        # hard gate found: every reject pasted, including the one whose
        # captured identity is the all-zero TargetIdentity(). That
        # record fails is_current() on its FIRST line, with no Windows
        # call, so verified_paste refuses the send on every such
        # dictation and logs the refusal at ERROR -- and an ERROR record
        # IS the Windows notification. The three catchers cover the
        # three consequences separately: the direct path reaches
        # verified_paste at all, the letter-buffer path loses its
        # WARNING arm and takes the ERROR arm instead (a second
        # notification for one dictation), and raw_insert_text raises
        # PasteFailedError on the success=False that comes back.
        "name": "remove-the-empty-identity-guard",
        "target": ROUTER,
        "old": _EMPTY_IDENTITY_GUARD,
        "new": "",
        "expect": [
            _empty_router("test_empty_identity_reject_returns_rejected"),
            _empty_handler(
                "test_direct_insert_never_reaches_verified_paste",
            ),
            _empty_handler("test_letter_buffer_flush_takes_the_warning_arm"),
            _empty_handler("test_raw_insert_text_does_not_raise_on_this_reject"),
        ],
    },
    {
        # Epic acceptance criterion 4. Windows discards input sent to a
        # higher-integrity window, so pasting there would report a
        # success the user never got. The catchers are all in
        # TestElevationRouting, which is where the criterion lives.
        "name": "weaken-the-elevated-refusal",
        "target": ROUTER,
        "old": _ELEVATED_REFUSAL,
        "new": _ELEVATED_REFUSAL.replace(
            "                return self.rejected\n",
            "                return self.clipboard_only\n",
        ),
        "expect": [
            _elev("test_elevated_routes_to_rejected"),
            _elev("test_elevated_beats_soft_allow_silent_paste"),
            _elev("test_elevated_beats_flutter_early_return"),
        ],
    },

    # -----------------------------------------------------------------
    # ui/ui_action_handler.py: a refusal is not a fault.
    # -----------------------------------------------------------------
    {
        # Acceptance criterion 5. ErrorNotificationHandler
        # (utils/error_notifier.py) is a logging handler at ERROR level
        # that utils/logging_setup.py attaches to the root logger, so an
        # ERROR record here IS the Windows notification the bead
        # removes.
        "name": "rejection-logs-error-again",
        "target": HANDLER,
        "old": (
            "            if self._last_insert_was_rejected:\n"
            "                logger.warning(\n"
        ),
        "new": (
            "            if self._last_insert_was_rejected:\n"
            "                logger.error(\n"
        ),
        "expect": [
            _level("test_rejected_final_flush_logs_warning_not_error"),
            _level("test_rejection_does_not_leak_into_a_later_plain_failure"),
        ],
    },
    {
        # The flag is read from the insertion result at the one place
        # the handler reads that result. A constant False means no
        # refusal is ever recognised and every refused flush is reported
        # as a fault.
        "name": "never-record-the-rejection",
        "target": HANDLER,
        "old": (
            "            self._last_insert_was_rejected = "
            "result.was_rejected\n"
        ),
        "new": "            self._last_insert_was_rejected = False\n",
        "expect": [
            _level("test_rejected_final_flush_logs_warning_not_error"),
            _level("test_rejection_does_not_leak_into_a_later_plain_failure"),
        ],
    },
    {
        # The per-attempt reset. The assignment is not the only
        # statement in its block -- the focus-drift reset sits above it
        # and the ``try`` below -- so the line is removed outright and
        # the comment above it is kept as the anchor.
        #
        # test_rejection_does_not_leak_into_a_later_plain_failure is NOT
        # a catcher, although this entry named it until the sweep of
        # 2026-09-17 reported the survivor. The reset runs at
        # ui_action_handler.py:1726 and the recording line reassigns the
        # flag at :1842, so on every path that reaches :1842 the reset
        # decides nothing. That test drives its second flush with
        # InsertionResult(success=False, ...), whose was_rejected is
        # False, so :1842 clears the flag whether or not the reset ran.
        # Only a path that returns BEFORE :1842 can catch this. There
        # are two: the focus-drift refusal returns at :1761, which the
        # letter-buffer flush cannot reach (it passes no options, so the
        # captured-target guard at :1741 is never entered), and the
        # exception arm returns at :1894 without touching the flag. The
        # catcher below drives that second path.
        "name": "never-clear-the-rejection-flag",
        "target": HANDLER,
        "old": (
            "        # exception handler (neither of which reads a result).\n"
            "        self._last_insert_was_rejected = False\n"
        ),
        "new": (
            "        # exception handler (neither of which reads a result).\n"
        ),
        "expect": [
            _level(
                "test_rejection_does_not_leak_into_a_later_attempt_that_raises"
            ),
        ],
    },
    {
        # The delivery test. A pre-send rejection returns success=True
        # so the caller's Future resolves cleanly, and subtracting the
        # rejection is the whole of what tells the caller no text
        # landed. Three fail-closed protections read that False.
        #
        # The fail-closed tests at test_ui_action_handler.py:1351, :1713
        # and :1859 are NOT catchers, although the brief for this gate
        # named them: each drives the branch with
        # ``InsertionResult(success=False, clipboard_dirty=False)``,
        # whose was_rejected is already False, so this mutation leaves
        # their outcome identical. The catchers below are the three
        # tests that drive the same protections with a REJECTED result
        # (success=True plus rejected_reason).
        "name": "deliver-a-rejection-as-success",
        "target": HANDLER,
        "old": (
            "            return result.success and not result.was_rejected\n"
        ),
        "new": "            return result.success\n",
        "expect": [
            f"{_RETURN}::test_pre_send_rejection_returns_false",
            f"{_WRAP}::test_empty_delimiters_rejected_insert_no_caret_move",
            _level("test_rejected_final_flush_logs_warning_not_error"),
        ],
    },
    {
        # wh-paste-when-unverified.3.4. The retraction gate for a
        # rejected insert, removed in BOTH get_strategy callers at once
        # so neither caller can be read as still protected by the other.
        #
        # Both branches carry comment lines above the assignment, so
        # deleting the assignment outright would leave a block whose
        # only contents are comments -- a SyntaxError, which the gate
        # would report as a false catch. ``pass`` keeps the mutant
        # compiling AND removes the behaviour, because the branch is
        # last in its isinstance chain and does nothing else.
        #
        # ``self._used_simple_paste = True`` appears six times in the
        # file (three per caller), so each pattern carries the comment
        # line immediately above it; those two comment lines appear
        # nowhere else.
        "name": "a-rejected-insert-stays-retractable",
        "target": HANDLER,
        "edits": [
            {
                "target": HANDLER,
                "old": (
                    "                # the target is still whatever it "
                    "was.\n"
                    "                self._used_simple_paste = True\n"
                ),
                "new": (
                    "                # the target is still whatever it "
                    "was.\n"
                    "                pass\n"
                ),
            },
            {
                "target": HANDLER,
                "old": (
                    "            # nothing, so there is nothing to "
                    "invalidate.\n"
                    "            self._used_simple_paste = True\n"
                ),
                "new": (
                    "            # nothing, so there is nothing to "
                    "invalidate.\n"
                    "            pass\n"
                ),
            },
        ],
        "expect": [
            _unretractable("test_stale_com_reject_blocks_the_retraction"),
            _unretractable(
                "test_stale_com_reject_blocks_the_retraction_from_raw_insert"
            ),
        ],
    },

    # -----------------------------------------------------------------
    # ui/clipboard_operations.py: a retryable clipboard read is not a
    # fault either.
    # -----------------------------------------------------------------
    {
        # The same notification mechanism as rejection-logs-error-again,
        # one layer down. ``_safe_paste`` cannot tell a fatal read from a
        # momentary lock, and its one caller (the verification loop at
        # ui/clipboard_operations.py:745) polls again on every None, so
        # ERROR here notifies the user of a paste that then succeeds.
        # The ``except Exception as e:`` line above it is part of the
        # pattern: it is what makes this the read wrapper rather than any
        # other warning in the file.
        "name": "recovered-read-failure-logs-error-again",
        "target": CLIPBOARD,
        "old": (
            "        except Exception as e:\n"
            '            logger.warning(f"Clipboard paste/read failed: {e}")\n'
        ),
        "new": (
            "        except Exception as e:\n"
            '            logger.error(f"Clipboard paste/read failed: {e}")\n'
        ),
        "expect": [
            _clip("test_recovered_clipboard_read_failure_logs_no_error"),
            _clip("test_optimistic_paste_after_lock_failures_logs_no_error"),
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
    for target in sorted({ROUTER, HANDLER, CLIPBOARD}):
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
