"""Mutation gate for the keyboard refusal notice (wh-keyboard-refusal-notice).

WHY THIS GATE EXISTS. Every test this bead added was written before its
implementation and was seen to fail. That is the first half of the rule. The
second half is that a test can pass for the wrong reason, so each protected
behaviour is broken here at the level of the INPUT the code depends on -- the
keyword that lowers the record, the level itself, the wording the user reads,
the map the handler consults to name a key -- and not only by deleting the fix.

WHAT THE BEAD CHANGED, and therefore what this gate defends:

  criterion 1: press_key_action, hotkey_action and type_text learn whether
  the send worked
      <caller>-discards-the-outcome     the verified primitive is called and
                                        its answer thrown away, which is the
                                        defect exactly as it stood
      <caller>-reverted-to-the-blind-primitive
                                        the pre-change call restored. Run
                                        against a NARROWED selection; see
                                        "A note on real keystrokes" below
      <caller>-except-arm-writes-no-notice
                                        a raising primitive reports nothing

  wh-keyboard-refusal-notice.1.1: a short send releases what it left held
      <caller>-release-not-sent         the release is deleted, so the key
                                        stays physically held and Windows
                                        auto-repeats it while the notice
                                        says the press did not work
      <caller>-releases-on-unmapped     the accepted > 0 guard is dropped, so
                                        the unmapped-key refusal releases a
                                        key it never pressed

  criterion 2: the notice names the action and the cause
      unknown-key-not-named             the handler stops reading VK_CODE_MAP
      unknown-and-short-send-not-separated
                                        the two causes are told apart by
                                        ``expected == 0``; the test is what
                                        stops that becoming a coin flip
      the-wording-changes               each of the five messages
      <caller>-wrong-source             the notice names another action
      typing-notice-carries-the-text    the user's dictated text on screen
      typing-log-carries-the-text       the same text in the log file

  criterion 3: send_notice, not the generic [ERROR] box
      <caller>-notice-not-said          the notice call is deleted
      <caller>-notice-on-success        the guard is inverted, so a working
                                        key press shows a box
      notice-failure-logs-error         a failing notice pops the very box
                                        this bead removes
      notice-raise-escapes              a failing notice reaches the caller

  boss e7 ruling A: the caller_notifies keyword, default False
      default-flipped-to-warning        the default stops being ERROR, which
                                        would quietly change fifteen other
                                        call sites of the verified variants
      typing-default-flipped-to-warning the same for the typing primitive
      caller-notifies-ignored           the keyword is accepted and ignored
      typing-caller-notifies-ignored    the same for the typing primitive
      <caller>-drops-the-flag           one caller stops passing it, which
                                        gives that caller two boxes again
      refusal-level-not-passed-through  the level stops reaching the shared
                                        event builder
      press-keys-lowered                press_keys, which writes no notice,
                                        stops writing its ERROR
      <record>-back-to-error            each of the five refusal records
                                        individually

  wh-keyboard-refusal-notice.1.2: a malformed value cannot silence the notice
      hotkey-chord-fallback-removed     the except arm reads ``keys`` a
                                        second time and raises again, so
                                        the exception leaves the method
      typing-total-fallback-removed     the same through ``len(text)``

  wh-keyboard-refusal-notice.1.3 and .1.4: the gate's own two failures
      end-of-sweep-mismatch-only-warns  a tracked file still holding a
                                        mutant is printed, and the run
                                        still exits 0
      cache-survivor-check-dropped      ``ignore_errors=True`` is silent,
                                        so a locked cache reads as cleared
      cache-sweep-narrowed-to-the-targets
                                        the importers of a mutant module
                                        keep their own stale bytecode
      pre-baseline-cache-error-prints-and-continues
                                        the whole sweep runs against a
                                        cache the gate could not clear
      pre-baseline-interrupt-raised-after-the-abort
                                        a Ctrl+C becomes an exit code

  Those five mutate THIS file. The sweeping process holds its own compiled
  copy, so the mutant reaches only the pytest child that imports it. The
  write-and-restore path is the one part no mutation touches, because that
  mutant would be on disk for the length of the run and an interruption
  would leave the next run reading it as the recovery tool; NOT_MUTATED
  carries the reason and the tests carry the guarantee.

A NOTE ON REAL KEYSTROKES, which is why three mutations carry a narrowed
selection. Twenty-two tests in test_ui_action_handler.py call these three
handlers while patching only the name the handler actually uses. Restore the
pre-change call and those patches no longer cover anything, so the test runs
the REAL press_keys or type_string and SendInput types into whatever window
has focus. That happened once during this bead's own development. The three
reverted-to-the-blind-primitive mutations therefore run only against tests
that cannot reach SendInput: the two key names are unmapped, so the send is
refused before SendInput, and the typing test replaces user32 outright.

There is no gate entry for NOTICE_TITLE. It is shared with the wheel refusal
notice and mutation_gate_wheel_refusal_notice.py already breaks it; a second
entry here would report the wheel's test failures as this bead's collateral.

Run it from services/wheelhouse. NEVER while a test suite is running in the
same worktree -- this file rewrites the sources that suite is importing:

    uv run python tests/mutation_gate_keyboard_refusal_notice.py

``--check`` answers "do the patterns still match, and does each mutant still
parse" in seconds, without running a single test.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE_DIR = Path(__file__).resolve().parents[1]

HANDLER = SERVICE_DIR / "ui" / "ui_action_handler.py"
SENDER = SERVICE_DIR / "utils" / "win_input_sender.py"
# This file is a target too, for the five mutations under "The gate's own
# two failures" below. The running process holds its own compiled copy, so
# a mutant of THIS file changes only what the pytest child imports.
GATE = Path(__file__).resolve()

HANDLER_TESTS = "tests/test_ui/test_ui_action_handler.py"
SENDER_TESTS = "tests/test_utils/test_win_input_sender.py"
GATE_TESTS = "tests/test_mutation_gate_keyboard_refusal_notice.py"

FEATURE_TEST_FILES = (HANDLER_TESTS, SENDER_TESTS, GATE_TESTS)

# The three files together run in about nine seconds clean. No mutation here
# can make a test wait on a timer -- every one changes a log level, a string,
# a boolean or a call -- so this limit exists only to turn a hang into a
# reported error rather than a lost run.
RUN_TIMEOUT_S = 300

_NOTICE = "TestTheKeyboardActionsWriteTheirOwnNotice"
_SEAM = "TestOneBoxThroughTheRealPrimitive"
_LEVEL = "TestCallerNotifiesLowersTheRefusalRecord"
_WRITE = "TestTheGateNeverLeavesAMutantBehind"
_CACHE = "TestTheGateRefusesAFailedCacheClear"


def _notice(name):
    return f"{_NOTICE}::{name}"


def _seam(name):
    return f"{_SEAM}::{name}"


def _level(name):
    return f"{_LEVEL}::{name}"


def _write_step(name):
    return f"{_WRITE}::{name}"


def _cache(name):
    return f"{_CACHE}::{name}"


# Every test this bead added. A mutation must break each one, or NOT_MUTATED
# must say why not.
FEATURE_TESTS = tuple(
    _notice(n) for n in (
        "test_an_unknown_key_name_shows_one_notice_naming_the_key",
        "test_a_short_key_send_shows_one_notice_naming_the_key",
        "test_a_short_key_press_releases_the_key",
        "test_an_unrecognised_key_name_releases_nothing",
        "test_a_short_hotkey_releases_the_chord",
        "test_an_unrecognised_hotkey_key_releases_nothing",
        "test_a_successful_key_press_shows_no_notice",
        "test_press_key_action_asks_the_primitive_to_stay_below_error",
        "test_an_unknown_key_inside_a_chord_is_named_in_the_notice",
        "test_a_short_hotkey_send_shows_one_notice_naming_the_chord",
        "test_hotkey_action_asks_the_primitive_to_stay_below_error",
        "test_a_successful_hotkey_shows_no_notice",
        "test_stopped_typing_shows_one_notice_saying_where_it_stopped",
        "test_type_text_asks_the_primitive_to_stay_below_error",
        "test_successful_typing_shows_no_notice",
        "test_two_identical_failures_inside_ten_seconds_show_two_notices",
        "test_a_notice_that_itself_fails_never_logs_an_error",
        "test_the_typed_text_never_reaches_the_log",
        "test_the_notices_say_the_exact_words_the_user_hears",
        "test_a_raising_key_press_shows_one_notice_and_no_error",
        "test_a_raising_hotkey_shows_one_notice_and_no_error",
        "test_raising_typing_shows_one_notice_and_no_error",
        "test_each_notice_names_the_action_that_sent_it",
        "test_a_chord_that_is_not_a_list_still_gets_its_notice",
        "test_text_that_has_no_length_still_gets_its_notice",
    )
) + tuple(
    _seam(n) for n in (
        "test_a_refused_key_press_shows_one_box_and_logs_no_error",
        "test_a_refused_hotkey_shows_one_box_and_logs_no_error",
        "test_refused_typing_shows_one_box_and_logs_no_error",
    )
) + tuple(
    _level(n) for n in (
        "test_a_default_short_send_still_logs_error",
        "test_caller_notifies_lowers_a_short_send_to_warning",
        "test_a_default_unknown_key_still_logs_error",
        "test_caller_notifies_lowers_an_unknown_key_to_warning",
        "test_press_keys_keeps_its_error_whatever_the_callers_do",
        "test_a_default_partial_typing_still_logs_error",
        "test_caller_notifies_lowers_partial_typing_to_warning",
        "test_a_default_raising_send_still_logs_error",
        "test_caller_notifies_lowers_a_raising_send_to_warning",
        "test_a_default_raising_typing_still_logs_error",
        "test_caller_notifies_lowers_raising_typing_to_warning",
    )
) + tuple(
    _write_step(n) for n in (
        "test_a_failed_write_leaves_both_targets_holding_the_original",
        "test_a_target_is_registered_before_its_write_is_attempted",
        "test_a_target_never_holds_part_of_a_mutant",
        "test_a_failed_replace_leaves_no_neighbour_file_behind",
        "test_a_successful_write_leaves_no_neighbour_file_behind",
        "test_a_write_refuses_a_neighbour_it_did_not_create",
        "test_a_sweep_reports_the_leftover_and_leaves_the_target_alone",
        "test_a_failed_cleanup_names_the_orphan_and_keeps_the_real_error",
        "test_a_restore_refuses_every_concurrent_save",
        "test_a_restore_puts_back_the_whole_mutant",
        "test_a_restore_of_an_untouched_target_says_nothing_is_wrong",
        "test_a_temporary_file_left_behind_is_caught_at_the_end_of_the_sweep",
        "test_an_unreadable_target_does_not_hide_the_file_beside_it",
        "test_a_mismatch_found_only_at_the_end_of_the_sweep_cannot_exit_zero",
    )
) + tuple(
    _cache(n) for n in (
        "test_a_directory_that_survives_both_passes_is_reported",
        "test_a_directory_removed_on_the_second_pass_is_not_an_error",
        "test_the_sweep_covers_the_whole_service_but_never_the_venv",
        "test_a_failed_pre_baseline_clear_starts_no_pytest",
        "test_a_held_interrupt_beats_the_pre_baseline_abort",
    )
)

# Tests this gate deliberately does not break, each with the reason. A reason
# must state a property of the behaviour itself. It may NOT cite another
# mutation ("already broken by X"): that claim is unverifiable where it is
# written and unchecked where it is read.
#
# Every entry here is a test of the write-and-restore path, and they share
# one reason. Mutating the OTHER parts of this file is safe:
# the running process holds its own compiled copy, so the mutant reaches
# only the pytest child. The write-and-restore path is the exception,
# because the mutant is on DISK for the length of the run. An interruption
# in that window leaves the next run -- the one an operator reaches for to
# put a tracked source file back -- reading the broken version as its
# recovery tool. That is the outcome this whole finding is about, so the
# gate does not manufacture it.
_RESTORE_PATH = (
    "a mutant of the write-and-restore path sits on DISK for the length of "
    "the run, so a Ctrl+C in that window leaves the next run reading the "
    "broken version as the tool an operator uses to put a tracked source "
    "file back; the guarantee is proved by the test itself instead"
)

NOT_MUTATED = {
    _write_step("test_a_failed_write_leaves_both_targets_holding_the_original"):
        _RESTORE_PATH,
    _write_step("test_a_target_is_registered_before_its_write_is_attempted"):
        _RESTORE_PATH,
    _write_step("test_a_target_never_holds_part_of_a_mutant"):
        _RESTORE_PATH,
    _write_step("test_a_failed_replace_leaves_no_neighbour_file_behind"):
        _RESTORE_PATH,
    _write_step("test_a_successful_write_leaves_no_neighbour_file_behind"):
        _RESTORE_PATH,
    _write_step("test_a_write_refuses_a_neighbour_it_did_not_create"):
        _RESTORE_PATH,
    _write_step("test_a_sweep_reports_the_leftover_and_leaves_the_target_alone"):
        _RESTORE_PATH,
    _write_step(
        "test_a_failed_cleanup_names_the_orphan_and_keeps_the_real_error"
    ): _RESTORE_PATH,
    _write_step("test_a_restore_refuses_every_concurrent_save"):
        _RESTORE_PATH,
    _write_step("test_a_restore_puts_back_the_whole_mutant"):
        _RESTORE_PATH,
    _write_step("test_a_restore_of_an_untouched_target_says_nothing_is_wrong"):
        _RESTORE_PATH,
}

# The narrow selections. Each names only tests that cannot reach the real
# SendInput under its mutation.
_UNMAPPED_KEY_ONLY = [
    HANDLER_TESTS, "-k",
    "test_an_unknown_key_name_shows_one_notice_naming_the_key or "
    "test_a_refused_key_press_shows_one_box_and_logs_no_error",
]
_UNMAPPED_CHORD_ONLY = [
    HANDLER_TESTS, "-k",
    "test_an_unknown_key_inside_a_chord_is_named_in_the_notice or "
    "test_a_refused_hotkey_shows_one_box_and_logs_no_error",
]
_PATCHED_USER32_ONLY = [
    HANDLER_TESTS, "-k", "test_refused_typing_shows_one_box_and_logs_no_error",
]

# The three "no notice on success" inversions. Narrowed for a different
# reason: inverting the guard makes every refusal test lose its notice too,
# and declaring twenty collateral failures would bury the one test that
# actually establishes the property.
_KEY_SUCCESS_ONLY = [
    HANDLER_TESTS, "-k", "test_a_successful_key_press_shows_no_notice",
]
_HOTKEY_SUCCESS_ONLY = [
    HANDLER_TESTS, "-k", "test_a_successful_hotkey_shows_no_notice",
]
_TYPING_SUCCESS_ONLY = [
    HANDLER_TESTS, "-k", "test_successful_typing_shows_no_notice",
]

# The two guard-drop selections, narrowed for the real-keystroke reason the
# module docstring gives. Dropping the accepted > 0 guard puts the unmapped
# refusal on the release path, and only these two tests patch
# _send_modifier_keyups. Every other test that reaches that refusal leaves
# the name real, and the chord ["ctrl", "shhift", "s"] has two MAPPED keys
# in it, so the mutant would send real ctrl and s KEYUP events into
# whatever window has focus.
_PRESS_KEY_RELEASE_ONLY = [
    HANDLER_TESTS, "-k", "test_an_unrecognised_key_name_releases_nothing",
]
_HOTKEY_RELEASE_ONLY = [
    HANDLER_TESTS, "-k", "test_an_unrecognised_hotkey_key_releases_nothing",
]

# The press-key refusal block, quoted once because four mutations replace it.
_PRESS_KEY_BLOCK = """                ok, accepted, expected = verified_press_keys(
                    key, caller_notifies=True
                )
                if not ok:
                    refusal = self._key_refusal_message(
                        (key,), expected,
                        PRESS_KEY_UNKNOWN_MESSAGE, PRESS_KEY_REFUSED_MESSAGE,
                    )
                    if accepted:
                        # A short send can leave the key physically held
                        # (down accepted, up dropped), so the notice
                        # would report a failure while Windows
                        # auto-repeats the key into the focused
                        # application. Release it first, the way
                        # press_key_verified below does (wh-eolas.2.5).
                        # An unmapped key name reports (False, 0, 0) and
                        # sent nothing, so it has nothing to release.
                        _send_modifier_keyups((key,))
                    logger.warning(
                        "press_key_action: key=%r refused (sent %d/%d)",
                        key, accepted, expected,
                    )
                    break
"""

# The release alone, quoted once because two mutations replace it: one
# deletes it, one drops the guard that keeps it off the unmapped path.
_PRESS_KEY_RELEASE = """                    if accepted:
                        # A short send can leave the key physically held
                        # (down accepted, up dropped), so the notice
                        # would report a failure while Windows
                        # auto-repeats the key into the focused
                        # application. Release it first, the way
                        # press_key_verified below does (wh-eolas.2.5).
                        # An unmapped key name reports (False, 0, 0) and
                        # sent nothing, so it has nothing to release.
                        _send_modifier_keyups((key,))
"""

_HOTKEY_BLOCK = """                    ok, accepted, expected = verified_press_keys(
                        *keys, caller_notifies=True
                    )
                    if not ok:
                        refusal = self._key_refusal_message(
                            tuple(keys), expected,
                            HOTKEY_UNKNOWN_MESSAGE, HOTKEY_REFUSED_MESSAGE,
                        )
                        if accepted:
                            # A short chord can leave a key physically
                            # held (down accepted, up dropped), so a held
                            # ctrl would run every later keystroke as a
                            # shortcut. Release it first, the way
                            # press_key_verified does (wh-eolas.2.5). An
                            # unmapped key name reports (False, 0, 0) and
                            # sent nothing, so it has nothing to release.
                            _send_modifier_keyups(tuple(keys))
                        logger.warning(
                            "hotkey_action: keys=%r refused (sent %d/%d)",
                            keys, accepted, expected,
                        )
                        break
"""

_HOTKEY_RELEASE = """                        if accepted:
                            # A short chord can leave a key physically
                            # held (down accepted, up dropped), so a held
                            # ctrl would run every later keystroke as a
                            # shortcut. Release it first, the way
                            # press_key_verified does (wh-eolas.2.5). An
                            # unmapped key name reports (False, 0, 0) and
                            # sent nothing, so it has nothing to release.
                            _send_modifier_keyups(tuple(keys))
"""

_TYPING_BLOCK = """            ok, sent, error = type_string_verified(text, caller_notifies=True)
            if not ok:
                refusal = TYPING_REFUSED_MESSAGE.format(
                    sent=sent, total=len(text)
                )
                # The typed text is never logged here, only the counts and
                # the primitive's own short reason.
                logger.warning(
                    "type_text: typing stopped after %d of %d characters (%s)",
                    sent, len(text), error,
                )
"""

# Every test that reports a refused key press. Deleting the notice, or
# throwing the outcome away, breaks all of them at once.
_EVERY_KEY_NOTICE = [
    _notice("test_an_unknown_key_name_shows_one_notice_naming_the_key"),
    _notice("test_a_short_key_send_shows_one_notice_naming_the_key"),
    _notice("test_a_notice_that_itself_fails_never_logs_an_error"),
    _notice("test_each_notice_names_the_action_that_sent_it"),
    _seam("test_a_refused_key_press_shows_one_box_and_logs_no_error"),
]

_EVERY_HOTKEY_NOTICE = [
    _notice("test_an_unknown_key_inside_a_chord_is_named_in_the_notice"),
    _notice("test_a_short_hotkey_send_shows_one_notice_naming_the_chord"),
    _notice("test_two_identical_failures_inside_ten_seconds_show_two_notices"),
    _notice("test_each_notice_names_the_action_that_sent_it"),
    _seam("test_a_refused_hotkey_shows_one_box_and_logs_no_error"),
]

_EVERY_TYPING_NOTICE = [
    _notice("test_stopped_typing_shows_one_notice_saying_where_it_stopped"),
    _notice("test_each_notice_names_the_action_that_sent_it"),
    _seam("test_refused_typing_shows_one_box_and_logs_no_error"),
]

MUTATIONS = [
    # -----------------------------------------------------------------
    # Criterion 1: the callers learn whether the send worked.
    # -----------------------------------------------------------------
    {
        # The defect exactly as it stood, with the primitive left alone so
        # the mutant is the behaviour and not a signature change: the
        # answer is thrown away, so the caller has nothing to report.
        "name": "press-key-discards-the-outcome",
        "target": HANDLER,
        "old": _PRESS_KEY_BLOCK,
        "new": "                verified_press_keys(key, caller_notifies=True)\n",
        # The release goes with the block, so the wh-keyboard-refusal-
        # notice.1.1 test fails here too. Declared, not narrowed away:
        # a caller that throws the outcome away cannot release either.
        "expect": _EVERY_KEY_NOTICE + [
            _notice("test_a_short_key_press_releases_the_key"),
        ],
    },
    {
        "name": "hotkey-discards-the-outcome",
        "target": HANDLER,
        "old": _HOTKEY_BLOCK,
        "new": (
            "                    verified_press_keys(\n"
            "                        *keys, caller_notifies=True\n"
            "                    )\n"
        ),
        # The release goes with the block; see press-key-discards-the-
        # outcome for why that failure is declared rather than narrowed.
        "expect": _EVERY_HOTKEY_NOTICE + [
            _notice("test_a_short_hotkey_releases_the_chord"),
        ],
    },
    {
        "name": "type-text-discards-the-outcome",
        "target": HANDLER,
        "old": _TYPING_BLOCK,
        "new": (
            "            type_string_verified(text, caller_notifies=True)\n"
        ),
        # NOT test_the_typed_text_never_reaches_the_log: discarding the
        # outcome deletes the log line that would have carried the text, so
        # that test stays green and establishes nothing here. The line is
        # pinned by typing-log-carries-the-text instead.
        "expect": _EVERY_TYPING_NOTICE,
    },
    {
        # The literal pre-change call. Narrowed: see the module docstring.
        "name": "press-key-reverted-to-the-blind-primitive",
        "target": HANDLER,
        "old": _PRESS_KEY_BLOCK,
        "new": "                press_keys(key)\n",
        "selection": _UNMAPPED_KEY_ONLY,
        "expect": [
            _notice("test_an_unknown_key_name_shows_one_notice_naming_the_key"),
            _seam("test_a_refused_key_press_shows_one_box_and_logs_no_error"),
        ],
    },
    {
        "name": "hotkey-reverted-to-the-blind-primitive",
        "target": HANDLER,
        "old": _HOTKEY_BLOCK,
        "new": "                    press_keys(*keys)\n",
        "selection": _UNMAPPED_CHORD_ONLY,
        "expect": [
            _notice("test_an_unknown_key_inside_a_chord_is_named_in_the_notice"),
            _seam("test_a_refused_hotkey_shows_one_box_and_logs_no_error"),
        ],
    },
    {
        "name": "type-text-reverted-to-the-blind-primitive",
        "target": HANDLER,
        "old": _TYPING_BLOCK,
        "new": "            type_string(text)\n",
        "selection": _PATCHED_USER32_ONLY,
        "expect": [
            _seam("test_refused_typing_shows_one_box_and_logs_no_error"),
        ],
    },

    # -----------------------------------------------------------------
    # wh-keyboard-refusal-notice.1.1: a short send releases the key it
    # left physically held, and only when it actually sent something.
    # -----------------------------------------------------------------
    {
        # The defect the finding reported: the notice lands while the key
        # is still down, so Windows auto-repeats it into the target.
        "name": "press-key-release-not-sent",
        "target": HANDLER,
        "old": _PRESS_KEY_RELEASE,
        "new": "",
        "expect": [
            _notice("test_a_short_key_press_releases_the_key"),
        ],
    },
    {
        "name": "hotkey-release-not-sent",
        "target": HANDLER,
        "old": _HOTKEY_RELEASE,
        "new": "",
        "expect": [
            _notice("test_a_short_hotkey_releases_the_chord"),
        ],
    },
    {
        # Without the guard the unmapped-key refusal, which sent nothing,
        # releases a key it never pressed and drags the primitive's own
        # "unmapped key; skipping" WARNING in with it. Narrowed: see
        # _PRESS_KEY_RELEASE_ONLY.
        "name": "press-key-releases-on-unmapped",
        "target": HANDLER,
        "old": _PRESS_KEY_RELEASE,
        "new": "                    _send_modifier_keyups((key,))\n",
        "selection": _PRESS_KEY_RELEASE_ONLY,
        "expect": [
            _notice("test_an_unrecognised_key_name_releases_nothing"),
        ],
    },
    {
        "name": "hotkey-releases-on-unmapped",
        "target": HANDLER,
        "old": _HOTKEY_RELEASE,
        "new": "                        _send_modifier_keyups(tuple(keys))\n",
        "selection": _HOTKEY_RELEASE_ONLY,
        "expect": [
            _notice("test_an_unrecognised_hotkey_key_releases_nothing"),
        ],
    },

    {
        "name": "press-key-except-arm-writes-no-notice",
        "target": HANDLER,
        "old": '            refusal = PRESS_KEY_REFUSED_MESSAGE.format(keys=key)\n',
        "new": "            refusal = None\n",
        "expect": [
            _notice("test_a_raising_key_press_shows_one_notice_and_no_error"),
        ],
    },
    {
        "name": "hotkey-except-arm-writes-no-notice",
        "target": HANDLER,
        "old": (
            "            refusal = HOTKEY_REFUSED_MESSAGE.format(chord=chord)\n"
        ),
        "new": "            refusal = None\n",
        "expect": [
            _notice("test_a_raising_hotkey_shows_one_notice_and_no_error"),
            _notice("test_a_chord_that_is_not_a_list_still_gets_its_notice"),
        ],
    },
    {
        "name": "type-text-except-arm-writes-no-notice",
        "target": HANDLER,
        "old": (
            "            refusal = TYPING_REFUSED_MESSAGE.format("
            "sent=0, total=total)\n"
        ),
        "new": "            refusal = None\n",
        "expect": [
            _notice("test_raising_typing_shows_one_notice_and_no_error"),
            _notice("test_text_that_has_no_length_still_gets_its_notice"),
        ],
    },
    {
        # An ERROR in the except arm brings back the generic box beside the
        # notice, which is the duplicate this bead removes.
        "name": "press-key-except-arm-logs-error",
        "target": HANDLER,
        "old": (
            "            logger.warning(f\"Error pressing key '{key}': {e}\", "
            "exc_info=True)\n"
        ),
        "new": (
            "            logger.error(f\"Error pressing key '{key}': {e}\", "
            "exc_info=True)\n"
        ),
        "expect": [
            _notice("test_a_raising_key_press_shows_one_notice_and_no_error"),
        ],
    },
    {
        "name": "hotkey-except-arm-logs-error",
        "target": HANDLER,
        "old": (
            "            logger.warning(\n"
            "                f\"Error executing hotkey '{keys}' "
            "(repeat={repeat}): {e}\",\n"
        ),
        "new": (
            "            logger.error(\n"
            "                f\"Error executing hotkey '{keys}' "
            "(repeat={repeat}): {e}\",\n"
        ),
        "expect": [
            _notice("test_a_raising_hotkey_shows_one_notice_and_no_error"),
            _notice("test_a_chord_that_is_not_a_list_still_gets_its_notice"),
        ],
    },
    {
        "name": "type-text-except-arm-logs-error",
        "target": HANDLER,
        "old": (
            '            logger.warning(f"Error in type_text: {e}", '
            "exc_info=True)\n"
        ),
        "new": (
            '            logger.error(f"Error in type_text: {e}", '
            "exc_info=True)\n"
        ),
        "expect": [
            _notice("test_raising_typing_shows_one_notice_and_no_error"),
            _notice("test_text_that_has_no_length_still_gets_its_notice"),
        ],
    },

    # -----------------------------------------------------------------
    # Criterion 2: the notice names the action and the cause.
    # -----------------------------------------------------------------
    {
        # The handler stops reading VK_CODE_MAP, so an unmapped key gets the
        # short-send wording and the user is never told which name was wrong.
        "name": "unknown-key-not-named",
        "target": HANDLER,
        "old": (
            "        if expected == 0:\n"
            "            unrecognized = [\n"
            "                str(k) for k in keys "
            "if str(k).lower() not in VK_CODE_MAP\n"
            "            ]\n"
            "            if unrecognized:\n"
            "                return unknown.format(\n"
            '                    chord=chord, keys=", ".join(unrecognized)\n'
            "                )\n"
        ),
        "new": "",
        "expect": [
            _notice("test_an_unknown_key_name_shows_one_notice_naming_the_key"),
            _notice("test_an_unknown_key_inside_a_chord_is_named_in_the_notice"),
        ],
    },
    {
        # ``expected == 0`` is the whole of the separation between an
        # unrecognized name and a short send. Inverted, an unmapped key
        # falls through to the short-send wording.
        "name": "unknown-and-short-send-not-separated",
        "target": HANDLER,
        "old": "        if expected == 0:\n",
        "new": "        if expected != 0:\n",
        "expect": [
            _notice("test_an_unknown_key_name_shows_one_notice_naming_the_key"),
            _notice("test_an_unknown_key_inside_a_chord_is_named_in_the_notice"),
        ],
    },
    {
        "name": "the-press-key-unknown-wording-changes",
        "target": HANDLER,
        "old": (
            "PRESS_KEY_UNKNOWN_MESSAGE = (\n"
            '    "Cannot press {keys}: Wheelhouse does not know that key name"\n'
            ")\n"
        ),
        "new": 'PRESS_KEY_UNKNOWN_MESSAGE = "Key press failed"\n',
        "expect": [
            _notice("test_the_notices_say_the_exact_words_the_user_hears"),
        ],
    },
    {
        "name": "the-press-key-refused-wording-changes",
        "target": HANDLER,
        "old": 'PRESS_KEY_REFUSED_MESSAGE = "Pressing {keys} did not work"\n',
        "new": 'PRESS_KEY_REFUSED_MESSAGE = "Key press failed"\n',
        "expect": [
            _notice("test_the_notices_say_the_exact_words_the_user_hears"),
        ],
    },
    {
        "name": "the-hotkey-unknown-wording-changes",
        "target": HANDLER,
        "old": (
            "HOTKEY_UNKNOWN_MESSAGE = (\n"
            '    "Cannot run the {chord} shortcut: '
            'Wheelhouse does not know {keys}"\n'
            ")\n"
        ),
        "new": 'HOTKEY_UNKNOWN_MESSAGE = "Shortcut failed"\n',
        "expect": [
            _notice("test_the_notices_say_the_exact_words_the_user_hears"),
        ],
    },
    {
        "name": "the-hotkey-refused-wording-changes",
        "target": HANDLER,
        "old": 'HOTKEY_REFUSED_MESSAGE = "The {chord} shortcut did not work"\n',
        "new": 'HOTKEY_REFUSED_MESSAGE = "Shortcut failed"\n',
        "expect": [
            _notice("test_the_notices_say_the_exact_words_the_user_hears"),
        ],
    },
    {
        "name": "the-typing-wording-changes",
        "target": HANDLER,
        "old": (
            'TYPING_REFUSED_MESSAGE = '
            '"Typing stopped after {sent} of {total} characters"\n'
        ),
        "new": (
            'TYPING_REFUSED_MESSAGE = '
            '"Typing failed ({sent}/{total})"\n'
        ),
        "expect": [
            _notice("test_the_notices_say_the_exact_words_the_user_hears"),
        ],
    },
    {
        # The user's own dictated words, on a notice anyone in the room can
        # read.
        "name": "typing-notice-carries-the-text",
        "target": HANDLER,
        "old": (
            "                refusal = TYPING_REFUSED_MESSAGE.format(\n"
            "                    sent=sent, total=len(text)\n"
            "                )\n"
        ),
        "new": (
            "                refusal = TYPING_REFUSED_MESSAGE.format(\n"
            "                    sent=sent, total=len(text)\n"
            '                ) + f": {text}"\n'
        ),
        "expect": [
            _notice("test_stopped_typing_shows_one_notice_saying_where_it_stopped"),
        ],
    },
    {
        # The same words in the log file, which outlives the notice.
        "name": "typing-log-carries-the-text",
        "target": HANDLER,
        "old": (
            "                logger.warning(\n"
            '                    "type_text: typing stopped after '
            '%d of %d characters (%s)",\n'
            "                    sent, len(text), error,\n"
            "                )\n"
        ),
        "new": (
            "                logger.warning(\n"
            '                    "type_text: typing stopped after '
            '%d of %d characters (%s) %s",\n'
            "                    sent, len(text), error, text,\n"
            "                )\n"
        ),
        "expect": [
            _notice("test_the_typed_text_never_reaches_the_log"),
        ],
    },
    {
        "name": "press-key-wrong-source",
        "target": HANDLER,
        "old": (
            "            self._say_the_keyboard_refused("
            'refusal, source="press key")\n'
        ),
        "new": (
            "            self._say_the_keyboard_refused("
            'refusal, source="hotkey")\n'
        ),
        "expect": [
            _notice("test_each_notice_names_the_action_that_sent_it"),
        ],
    },
    {
        "name": "hotkey-wrong-source",
        "target": HANDLER,
        "old": (
            "            self._say_the_keyboard_refused("
            'refusal, source="hotkey")\n'
        ),
        "new": (
            "            self._say_the_keyboard_refused("
            'refusal, source="press key")\n'
        ),
        "expect": [
            _notice("test_each_notice_names_the_action_that_sent_it"),
        ],
    },
    {
        "name": "type-text-wrong-source",
        "target": HANDLER,
        "old": (
            "            self._say_the_keyboard_refused("
            'refusal, source="type text")\n'
        ),
        "new": (
            "            self._say_the_keyboard_refused("
            'refusal, source="hotkey")\n'
        ),
        "expect": [
            _notice("test_each_notice_names_the_action_that_sent_it"),
        ],
    },

    # -----------------------------------------------------------------
    # Criterion 3: send_notice, not the generic [ERROR] box.
    # -----------------------------------------------------------------
    {
        "name": "press-key-notice-not-said",
        "target": HANDLER,
        "old": (
            "        if refusal is not None:\n"
            "            self._say_the_keyboard_refused("
            'refusal, source="press key")\n'
        ),
        "new": "        if refusal is not None:\n            pass\n",
        "expect": _EVERY_KEY_NOTICE + [
            _notice("test_a_raising_key_press_shows_one_notice_and_no_error"),
        ],
    },
    {
        "name": "hotkey-notice-not-said",
        "target": HANDLER,
        "old": (
            "        if refusal is not None:\n"
            "            self._say_the_keyboard_refused("
            'refusal, source="hotkey")\n'
        ),
        "new": "        if refusal is not None:\n            pass\n",
        "expect": _EVERY_HOTKEY_NOTICE + [
            _notice("test_a_raising_hotkey_shows_one_notice_and_no_error"),
            _notice("test_a_chord_that_is_not_a_list_still_gets_its_notice"),
        ],
    },
    {
        "name": "type-text-notice-not-said",
        "target": HANDLER,
        "old": (
            "        if refusal is not None:\n"
            "            self._say_the_keyboard_refused("
            'refusal, source="type text")\n'
        ),
        "new": "        if refusal is not None:\n            pass\n",
        "expect": _EVERY_TYPING_NOTICE + [
            _notice("test_raising_typing_shows_one_notice_and_no_error"),
            _notice("test_text_that_has_no_length_still_gets_its_notice"),
        ],
    },
    {
        # A working key press must be silent. Narrowed: see above.
        "name": "press-key-notice-on-success",
        "target": HANDLER,
        "old": (
            "        if refusal is not None:\n"
            "            self._say_the_keyboard_refused("
            'refusal, source="press key")\n'
        ),
        "new": (
            "        if refusal is None:\n"
            "            self._say_the_keyboard_refused("
            'refusal, source="press key")\n'
        ),
        "selection": _KEY_SUCCESS_ONLY,
        "expect": [_notice("test_a_successful_key_press_shows_no_notice")],
    },
    {
        "name": "hotkey-notice-on-success",
        "target": HANDLER,
        "old": (
            "        if refusal is not None:\n"
            "            self._say_the_keyboard_refused("
            'refusal, source="hotkey")\n'
        ),
        "new": (
            "        if refusal is None:\n"
            "            self._say_the_keyboard_refused("
            'refusal, source="hotkey")\n'
        ),
        "selection": _HOTKEY_SUCCESS_ONLY,
        "expect": [_notice("test_a_successful_hotkey_shows_no_notice")],
    },
    {
        "name": "type-text-notice-on-success",
        "target": HANDLER,
        "old": (
            "        if refusal is not None:\n"
            "            self._say_the_keyboard_refused("
            'refusal, source="type text")\n'
        ),
        "new": (
            "        if refusal is None:\n"
            "            self._say_the_keyboard_refused("
            'refusal, source="type text")\n'
        ),
        "selection": _TYPING_SUCCESS_ONLY,
        "expect": [_notice("test_successful_typing_shows_no_notice")],
    },
    {
        "name": "notice-failure-logs-error",
        "target": HANDLER,
        "old": (
            "            logger.warning(\n"
            '                "%s: the refusal notice failed: %s",\n'
            "                source, type(exc).__name__,\n"
            "            )\n"
        ),
        "new": (
            "            logger.error(\n"
            '                "%s: the refusal notice failed: %s",\n'
            "                source, type(exc).__name__,\n"
            "            )\n"
        ),
        "expect": [
            _notice("test_a_notice_that_itself_fails_never_logs_an_error"),
        ],
    },
    {
        # The catching test fails by the RuntimeError escaping
        # press_key_action, not by its own assertion. That IS the property:
        # a report the Input loop cannot survive is worse than no report.
        # Verified by reading the failing output, not assumed.
        "name": "notice-raise-escapes",
        "target": HANDLER,
        "old": (
            "        try:\n"
            "            send_notice(NOTICE_TITLE, message, source=source)\n"
            "        except Exception as exc:  "
            "# noqa: BLE001 -- a report must never raise\n"
            "            # WARNING, not ERROR, because an ERROR here pops "
            "its own\n"
            "            # notification -- the duplicate this bead exists "
            "to remove.\n"
            "            logger.warning(\n"
            '                "%s: the refusal notice failed: %s",\n'
            "                source, type(exc).__name__,\n"
            "            )\n"
        ),
        "new": (
            "        send_notice(NOTICE_TITLE, message, source=source)\n"
        ),
        "expect": [
            _notice("test_a_notice_that_itself_fails_never_logs_an_error"),
        ],
    },

    # -----------------------------------------------------------------
    # Boss e7 ruling A: the caller_notifies keyword, default False.
    # -----------------------------------------------------------------
    {
        # The default is what fifteen other call sites of the verified
        # variants, and every caller that shows no notice of its own, rely
        # on. Flipping it silences their box.
        "name": "default-flipped-to-warning",
        "target": SENDER,
        "old": (
            "    refusal_level = logging.WARNING if caller_notifies "
            "else logging.ERROR\n"
            "    if not keys:\n"
        ),
        "new": "    refusal_level = logging.WARNING\n    if not keys:\n",
        "expect": [
            _level("test_a_default_short_send_still_logs_error"),
            _level("test_a_default_unknown_key_still_logs_error"),
            _level("test_a_default_raising_send_still_logs_error"),
        ],
    },
    {
        "name": "typing-default-flipped-to-warning",
        "target": SENDER,
        "old": (
            "    refusal_level = logging.WARNING if caller_notifies "
            "else logging.ERROR\n"
            "    if not text:\n"
        ),
        "new": "    refusal_level = logging.WARNING\n    if not text:\n",
        "expect": [
            _level("test_a_default_partial_typing_still_logs_error"),
            _level("test_a_default_raising_typing_still_logs_error"),
        ],
    },
    {
        # The keyword accepted and ignored: the caller's notice is joined by
        # the generic box again, which is the duplicate this bead removes.
        "name": "caller-notifies-ignored",
        "target": SENDER,
        "old": (
            "    refusal_level = logging.WARNING if caller_notifies "
            "else logging.ERROR\n"
            "    if not keys:\n"
        ),
        "new": "    refusal_level = logging.ERROR\n    if not keys:\n",
        "expect": [
            _level("test_caller_notifies_lowers_a_short_send_to_warning"),
            _level("test_caller_notifies_lowers_an_unknown_key_to_warning"),
            _level("test_caller_notifies_lowers_a_raising_send_to_warning"),
            _seam("test_a_refused_key_press_shows_one_box_and_logs_no_error"),
            _seam("test_a_refused_hotkey_shows_one_box_and_logs_no_error"),
        ],
    },
    {
        "name": "typing-caller-notifies-ignored",
        "target": SENDER,
        "old": (
            "    refusal_level = logging.WARNING if caller_notifies "
            "else logging.ERROR\n"
            "    if not text:\n"
        ),
        "new": "    refusal_level = logging.ERROR\n    if not text:\n",
        "expect": [
            _level("test_caller_notifies_lowers_partial_typing_to_warning"),
            _level("test_caller_notifies_lowers_raising_typing_to_warning"),
            _seam("test_refused_typing_shows_one_box_and_logs_no_error"),
        ],
    },
    {
        "name": "press-key-drops-the-flag",
        "target": HANDLER,
        "old": (
            "                ok, accepted, expected = verified_press_keys(\n"
            "                    key, caller_notifies=True\n"
            "                )\n"
        ),
        "new": (
            "                ok, accepted, expected = "
            "verified_press_keys(key)\n"
        ),
        "expect": [
            _notice("test_press_key_action_asks_the_primitive_to_stay_below_error"),
            _seam("test_a_refused_key_press_shows_one_box_and_logs_no_error"),
            "TestPressKeyAction::test_press_key_calls_the_verified_primitive",
        ],
    },
    {
        "name": "hotkey-drops-the-flag",
        "target": HANDLER,
        "old": (
            "                    ok, accepted, expected = "
            "verified_press_keys(\n"
            "                        *keys, caller_notifies=True\n"
            "                    )\n"
        ),
        "new": (
            "                    ok, accepted, expected = "
            "verified_press_keys(*keys)\n"
        ),
        "expect": [
            _notice("test_hotkey_action_asks_the_primitive_to_stay_below_error"),
            _seam("test_a_refused_hotkey_shows_one_box_and_logs_no_error"),
            "TestHotkeyAction::test_hotkey_standard_app",
        ],
    },
    {
        "name": "type-text-drops-the-flag",
        "target": HANDLER,
        "old": (
            "            ok, sent, error = type_string_verified("
            "text, caller_notifies=True)\n"
        ),
        "new": "            ok, sent, error = type_string_verified(text)\n",
        "expect": [
            _notice("test_type_text_asks_the_primitive_to_stay_below_error"),
            _seam("test_refused_typing_shows_one_box_and_logs_no_error"),
            "TestTypeText::test_type_text_accepts_extra_kwargs",
            "TestTypeText::test_type_text_calls_the_verified_primitive",
            "TestTypeText::test_type_text_empty_string",
            "TestTypeText::test_type_text_special_characters",
        ],
    },
    {
        # The level stops reaching the shared event builder, so an
        # unrecognized key name keeps its ERROR whatever the caller asked.
        "name": "refusal-level-not-passed-through",
        "target": SENDER,
        "old": (
            "        events, num_events = _build_press_keys_events(\n"
            "            keys, refusal_level=refusal_level\n"
            "        )\n"
        ),
        "new": (
            "        events, num_events = _build_press_keys_events(keys)\n"
        ),
        "expect": [
            _level("test_caller_notifies_lowers_an_unknown_key_to_warning"),
            _seam("test_a_refused_key_press_shows_one_box_and_logs_no_error"),
            _seam("test_a_refused_hotkey_shows_one_box_and_logs_no_error"),
        ],
    },
    {
        # press_keys writes no notice of its own, so its ERROR is the only
        # thing that reaches the user. It must not follow the callers down.
        "name": "press-keys-lowered",
        "target": SENDER,
        "old": (
            "    try:\n"
            "        events, num_events = _build_press_keys_events(keys)\n"
            "    except _InvalidKeyError:\n"
            "        return\n"
        ),
        "new": (
            "    try:\n"
            "        events, num_events = _build_press_keys_events(\n"
            "            keys, refusal_level=logging.WARNING\n"
            "        )\n"
            "    except _InvalidKeyError:\n"
            "        return\n"
        ),
        "expect": [
            _level("test_press_keys_keeps_its_error_whatever_the_callers_do"),
        ],
    },

    # -----------------------------------------------------------------
    # Each refusal record, one at a time. A single record left at ERROR is
    # a single duplicated box, and nothing else in the gate would see it.
    # -----------------------------------------------------------------
    {
        "name": "unmapped-key-record-back-to-error",
        "target": SENDER,
        "old": (
            "        logger.log(\n"
            "            refusal_level,\n"
            '            f"One or more keys in {keys} are not valid. '
            'Aborting.",\n'
            "        )\n"
        ),
        "new": (
            '        logger.error(f"One or more keys in {keys} are not '
            'valid. Aborting.")\n'
        ),
        "expect": [
            _level("test_caller_notifies_lowers_an_unknown_key_to_warning"),
            _seam("test_a_refused_key_press_shows_one_box_and_logs_no_error"),
            _seam("test_a_refused_hotkey_shows_one_box_and_logs_no_error"),
        ],
    },
    {
        "name": "short-send-record-back-to-error",
        "target": SENDER,
        "old": (
            "        logger.log(\n"
            "            refusal_level,\n"
            '            "verified_press_keys: short SendInput for keys=%s: "\n'
        ),
        "new": (
            "        logger.error(\n"
            '            "verified_press_keys: short SendInput for keys=%s: "\n'
        ),
        "expect": [
            _level("test_caller_notifies_lowers_a_short_send_to_warning"),
        ],
    },
    {
        "name": "raising-send-record-back-to-error",
        "target": SENDER,
        "old": (
            "        logger.log(\n"
            "            refusal_level,\n"
            '            "verified_press_keys: SendInput raised for '
            'keys=%s: %s",\n'
        ),
        "new": (
            "        logger.error(\n"
            '            "verified_press_keys: SendInput raised for '
            'keys=%s: %s",\n'
        ),
        "expect": [
            _level("test_caller_notifies_lowers_a_raising_send_to_warning"),
        ],
    },
    {
        "name": "partial-typing-record-back-to-error",
        "target": SENDER,
        "old": (
            "            logger.log(\n"
            "                refusal_level,\n"
            '                "type_string_verified: %s; chars_sent=%d/%d",\n'
        ),
        "new": (
            "            logger.error(\n"
            '                "type_string_verified: %s; chars_sent=%d/%d",\n'
        ),
        "expect": [
            _level("test_caller_notifies_lowers_partial_typing_to_warning"),
            _seam("test_refused_typing_shows_one_box_and_logs_no_error"),
        ],
    },
    {
        "name": "raising-typing-record-back-to-error",
        "target": SENDER,
        "old": (
            "            logger.log(\n"
            "                refusal_level,\n"
            '                "type_string_verified: %s; chars_sent=%d/%d '
            'send_us=%.1f",\n'
        ),
        "new": (
            "            logger.error(\n"
            '                "type_string_verified: %s; chars_sent=%d/%d '
            'send_us=%.1f",\n'
        ),
        "expect": [
            _level("test_caller_notifies_lowers_raising_typing_to_warning"),
        ],
    },

    # -----------------------------------------------------------------
    # wh-keyboard-refusal-notice.1.2: the except arm must not raise.
    # Each arm reads the SAME value the try could not read, so the
    # commonest way to reach the arm is the value that makes the second
    # read raise as well.
    # -----------------------------------------------------------------
    {
        "name": "hotkey-chord-fallback-removed",
        "target": HANDLER,
        "old": (
            "            try:\n"
            '                chord = " + ".join(str(k) for k in keys)\n'
            "            except TypeError:\n"
            "                chord = str(keys)\n"
        ),
        "new": '            chord = " + ".join(str(k) for k in keys)\n',
        "expect": [
            _notice("test_a_chord_that_is_not_a_list_still_gets_its_notice"),
        ],
    },
    {
        "name": "typing-total-fallback-removed",
        "target": HANDLER,
        "old": (
            "            try:\n"
            "                total = len(text)\n"
            "            except TypeError:\n"
            "                total = 0\n"
        ),
        "new": "            total = len(text)\n",
        "expect": [
            _notice("test_text_that_has_no_length_still_gets_its_notice"),
        ],
    },

    # -----------------------------------------------------------------
    # The gate's own two failures, wh-keyboard-refusal-notice.1.3 and
    # .1.4. These five mutate THIS file. That is safe for the run that
    # applies them: the sweeping process holds its own compiled copy, so
    # the mutant reaches only the pytest child that imports it. The
    # write-and-restore path is the one part of this file no mutation
    # touches; NOT_MUTATED records why.
    # -----------------------------------------------------------------
    {
        # The sweep's last check reads a tracked file that is still
        # holding a mutant. Printing that and exiting 0 is the silence
        # the finding is about.
        "name": "end-of-sweep-mismatch-only-warns",
        "target": GATE,
        "selection": [GATE_TESTS],
        "old": (
            "        for problem in _final_mismatches(originals):\n"
            "            errors.append(problem)\n"
            '            print(f"ERROR {problem}")\n'
        ),
        "new": (
            "        for problem in _final_mismatches(originals):\n"
            '            print(f"WARNING {problem}")\n'
        ),
        "expect": [
            _write_step(
                "test_a_mismatch_found_only_at_the_end_of_the_sweep_"
                "cannot_exit_zero"
            ),
        ],
    },
    {
        # An unreadable target decides whether the file beside it is
        # looked for. That is the defect the else: replaced, and the
        # neighbour is what stops the next run against that target.
        "name": "end-of-sweep-read-failure-skips-the-neighbour",
        "target": GATE,
        "selection": [GATE_TESTS],
        "old": (
            '            problems.append(f"cannot read {target} at the end '
            'of the sweep; "\n'
            '                            f"check it by hand: {exc}")\n'
            "        else:\n"
        ),
        "new": (
            '            problems.append(f"cannot read {target} at the end '
            'of the sweep; "\n'
            '                            f"check it by hand: {exc}")\n'
            "            continue\n"
            "        if True:\n"
        ),
        "expect": [
            _write_step(
                "test_an_unreadable_target_does_not_hide_the_file_beside_it"
            ),
        ],
    },
    {
        # The end-of-sweep check stops looking for a neighbour file. A
        # survivor is untracked, stops the next run against that target,
        # and can outlive a run that otherwise succeeded.
        "name": "end-of-sweep-leftover-not-looked-for",
        "target": GATE,
        "selection": [GATE_TESTS],
        "old": (
            "        if left:\n"
            '            problems.append(f"{neighbour} is still here at '
            'the end of the "\n'
        ),
        "new": (
            "        if False:\n"
            '            problems.append(f"{neighbour} is still here at '
            'the end of the "\n'
        ),
        "expect": [
            _write_step(
                "test_a_temporary_file_left_behind_is_caught_at_the_end_"
                "of_the_sweep"
            ),
            # A second real catcher, not collateral. That test reads the
            # neighbour report as one of the TWO problems it requires, so
            # dropping the report fails its own named assertion
            # (len(problems) == 2). The full sweep is what exposed the
            # missing entry: the two tests were added in different
            # commits, and the earlier one's mutation was only ever run
            # alone.
            _write_step(
                "test_an_unreadable_target_does_not_hide_the_file_beside_it"
            ),
        ],
    },
    {
        "name": "cache-survivor-check-dropped",
        "target": GATE,
        "selection": [GATE_TESTS],
        "old": (
            "                    shutil.rmtree(d, ignore_errors=True)\n"
            "                    if d.exists():\n"
            "                        survivors.append(d)\n"
        ),
        "new": "                    shutil.rmtree(d, ignore_errors=True)\n",
        "expect": [
            _cache("test_a_directory_that_survives_both_passes_is_reported"),
            _cache(
                "test_a_directory_removed_on_the_second_pass_is_not_an_error"
            ),
        ],
    },
    {
        # A mutant module is imported through modules that cache their own
        # bytecode, so the sweep has to stay wide.
        "name": "cache-sweep-narrowed-to-the-targets",
        "target": GATE,
        "selection": [GATE_TESTS],
        "old": '            for d in root.rglob("__pycache__"):\n',
        "new": (
            '            for d in (HANDLER.parent / "__pycache__",\n'
            '                      SENDER.parent / "__pycache__"):\n'
        ),
        "expect": [
            _cache("test_the_sweep_covers_the_whole_service_but_never_the_venv"),
        ],
        "also_fails": {
            _cache("test_a_directory_that_survives_both_passes_is_reported"):
                "the narrowed sweep never looks inside the temporary root "
                "this test builds, so it falls over on the breadth of the "
                "search rather than on the survivor report it is about",
            _cache(
                "test_a_directory_removed_on_the_second_pass_is_not_an_error"
            ):
                "the same: the directory it watches for is never visited, "
                "so the test says nothing about the second pass",
        },
    },
    {
        "name": "pre-baseline-cache-error-prints-and-continues",
        "target": GATE,
        "selection": [GATE_TESTS],
        "old": "    return 1 if error else None\n",
        "new": "    return None\n",
        "expect": [
            _cache("test_a_failed_pre_baseline_clear_starts_no_pytest"),
        ],
    },
    {
        # An exit code in place of the Ctrl+C reads like an ordinary
        # refusal, and the operator who pressed it never learns it landed.
        "name": "pre-baseline-interrupt-raised-after-the-abort",
        "target": GATE,
        "selection": [GATE_TESTS],
        "old": (
            "    if interrupt is not None:\n"
            "        raise interrupt\n"
            "    return 1 if error else None\n"
        ),
        "new": (
            "    if error:\n"
            "        return 1\n"
            "    if interrupt is not None:\n"
            "        raise interrupt\n"
            "    return None\n"
        ),
        "expect": [
            _cache("test_a_held_interrupt_beats_the_pre_baseline_abort"),
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


def _clear_pycache(root=None):
    """Remove every __pycache__ under the service; return (error, interrupt).

    Python decides whether cached bytecode is current from the source file's
    (mtime, size). Several mutations here keep the file within a byte of its
    original length, so a stale .pyc beside a restored source can be executed
    by the next process. The interrupt is held rather than allowed out, for
    the reason ``_restore`` records: escaping here skips the message that
    tells the operator a mutant may still be in a real source file.

    ``ignore_errors=True`` is what makes the check after it necessary: a
    directory Windows will not delete -- an antivirus scan or another
    process holding a file in it -- leaves ``rmtree`` silent and the
    directory in place, so the only way to know the sweep worked is to
    look. Every surviving directory goes to the next pass, because the
    hold is usually momentary, and only a survivor after the last pass is
    an error.

    ``root`` is the directory swept, and only the tests pass it. It stays
    the whole service: a mutant module is imported through modules that
    carry cached bytecode of their own, so narrowing the sweep to the two
    mutation targets would leave the stale .pyc this exists to remove.

    ``PYTHONDONTWRITEBYTECODE`` is not a second line of defence here. It
    stops Python WRITING a .pyc; it does not stop Python reading one that
    is already there.
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
    later verdict in the run would be read from a process that may have
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


def _atomic_write(target, raw):
    """Put ``raw`` in ``target`` in one step; leave no partial file.

    ``write_bytes`` truncates and then writes, so an interrupt or an
    OSError inside it leaves a tracked production file holding part of a
    mutant -- and nothing on disk tells that apart from a save somebody
    else made in the same window (wh-keyboard-refusal-notice.1.6). Writing
    a neighbour file and replacing the target means the target only ever
    holds the whole of one version or the whole of the other, so
    ``_restore`` needs one guard rather than a rule for each outcome.

    The neighbour sits in the target's own directory because ``os.replace``
    is atomic only within one volume. Its name is derived from the target,
    so it is the same on every run, and it is therefore created
    EXCLUSIVELY: a name already taken belongs to somebody else and this
    call stops rather than truncating it (wh-keyboard-refusal-notice.1.7).
    Two things put a file there. A sweep killed before its cleanup ran
    leaves one, and that leftover is the evidence that the target may hold
    the killed run's mutant, so destroying it destroys the only warning. A
    second sweep against this same checkout is the other, and there the
    shared name lets each run replace the other's mutant and then restore
    bytes it never wrote. ``O_BINARY`` is required on Windows, where an
    ``os.open`` without it translates every newline on the way to disk.

    crewcut: exclusive creation is NOT a lock over the run. It refuses
    only while both runs hold the name at once, and a successful replace
    frees the name for the whole pytest interval that follows, so two
    sweeps can still interleave and one can record a failure the other's
    mutant caused (wh-keyboard-refusal-notice.1.9). Two sweeps against one
    checkout corrupt each other through the TARGETS in any case, because
    each takes its own pre-run snapshot and one can snapshot the other's
    mutant; that is why the standing rule is never to run two, and why
    this refusal is a second line of defence rather than the first. A lock
    held over the whole run, taken before the baseline and released after
    the last check, would remove the need for the rule; it is a design
    decision for the project owner, not for this bead.

    The neighbour is removed on every path out, including an interrupt
    between the write and the replace, so the sweep cannot leave an
    untracked file in the checkout. A removal that itself fails is PRINTED
    rather than raised (wh-keyboard-refusal-notice.1.8). Raising from a
    ``finally`` replaces the exception already on its way out, which here
    is the failed replace or the held Ctrl+C the gate re-raises, and
    losing either is worse than losing the cleanup message. Printing is
    not what fails the run, and an earlier version of this comment was
    wrong to say it did not need to be: ``_final_mismatches`` looks for a
    surviving neighbour beside every target at the end of the sweep and
    reports it as an error there, so the exit status does not depend on
    which failure left the file.
    """
    tmp = target.with_name(target.name + ".gate-tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(tmp, flags)
    except FileExistsError:
        raise OSError(
            f"{tmp} exists already, so this run did not create it and will "
            f"not overwrite it. A killed sweep leaves one behind, and then "
            f"{target} may hold that run's mutant: check it against git "
            "before deleting the leftover. Two sweeps in one checkout do "
            "the same thing; never run them together."
        ) from None
    try:
        with open(handle, "wb") as stream:
            stream.write(raw)
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            print(f"ERROR cannot remove the temporary file {tmp}: {exc}")
            print(f"ERROR {tmp} is untracked and still in the checkout; "
                  "delete it before the next run")


def _restore(target, original, mutated):
    """Put the pre-run bytes back; return (error string or None, interrupt).

    Never overwrite bytes this run did not write: a save landing from an
    editor while pytest ran would otherwise be replaced by the stale
    snapshot and lost.

    There is ONE guard, not one per write outcome, because ``_atomic_write``
    leaves the target holding the whole of one version or the whole of the
    other and never a part of either. An interrupted write is therefore
    indistinguishable from no write at all, which the equal-to-original
    case above already returns on, and every other byte string was written
    by somebody else. Two earlier attempts to tell a half-written file
    apart from a real save both failed -- an unguarded restore for an
    unfinished write (wh-keyboard-refusal-notice.1.5), then a
    prefix-of-the-mutant rule that also accepts an empty save and a save
    truncated before the mutation site (wh-keyboard-refusal-notice.1.6).
    The state was ambiguous, so the fix was to stop producing it.

    A KeyboardInterrupt is held rather than allowed out, and returned for
    the caller to re-raise once the cleanup is done. The first Ctrl+C stops
    pytest and reaches this restore; a second one escaping from here would
    leave a real source file holding the mutant, where every later run --
    and every other session sharing the checkout -- reads it as the real
    code. That is the worst outcome this gate has. Bounded at two attempts
    so a held-down Ctrl+C cannot put the gate in a loop.

    ``write_bytes``, never ``write_text``: on Windows ``write_text``
    translates every newline to CRLF, which would break every multi-line
    pattern in this file on the next run.
    """
    interrupt = None
    for _attempt in (1, 2):
        try:
            current = target.read_bytes()
        except OSError as exc:
            return (f"cannot read {target} before restore; the MUTANT MAY "
                    f"REMAIN in source: {exc}"), interrupt
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            continue
        if current == original:
            return None, interrupt
        if current != mutated:
            return (f"{target} changed while pytest ran; refusing to "
                    "overwrite the concurrent edit with the stale pre-run "
                    "snapshot -- reconcile the file by hand"), interrupt
        try:
            _atomic_write(target, original)
        except OSError as exc:
            return (f"restore failed; the MUTANT REMAINS in {target}: "
                    f"{exc}"), interrupt
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            continue
        return None, interrupt
    return (f"interrupted during every restore attempt; the MUTANT MAY "
            f"REMAIN in {target} -- check it before the next run"), interrupt


def _write_mutants(changed, written):
    """Put every mutant on disk; return an error string or None.

    ``written`` is the CALLER's registry and is filled in place, because a
    KeyboardInterrupt escaping this function has to leave the caller
    holding every target it touched -- including the one it was in the
    middle of writing. Registering a target only after its write returned
    left exactly that target unregistered for the window in which the file
    may already hold the whole mutant, and the sweep restores from this
    registry and nothing else.

    The value is the mutant's bytes. The registry does not record whether
    the write finished, because ``_atomic_write`` makes that unobservable
    and unnecessary: the target holds the whole original or the whole
    mutant, and ``_restore`` reads which.

    An OSError is reported rather than raised so the caller can skip
    pytest for this mutation and still restore what was written.
    """
    for target, mutated in changed.items():
        raw = mutated.encode("utf-8")
        written[target] = raw
        try:
            _atomic_write(target, raw)
        except OSError as exc:
            return f"cannot write the mutant to {target}: {exc}"
    return None


def _restore_written(name, written, originals):
    """Put back every target the write step registered.

    Returns ``(problems, interrupt)``: the error strings already prefixed
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
    """Every target whose file state is wrong at the end, as error strings.

    Two things are wrong: a target that does not hold its pre-run bytes,
    and a neighbour file still sitting beside one. Nothing this run can
    account for writes that name after a successful replace consumes it,
    so a file there belongs to a killed earlier sweep, a cleanup this run
    could not finish, or a second sweep against this checkout. Each of
    those has to end the run non-zero (wh-keyboard-refusal-notice.1.9);
    the printed cleanup message rested on the claim that a survivor always
    accompanies a failure that already fails the run, and that claim was
    wrong.

    Read inside a ``try``: this runs in the sweep's own ``finally``, and an
    OSError raised there would replace whatever exception was on its way
    out -- including the held Ctrl+C the gate re-raises.
    """
    problems = []
    for target, raw in sorted(originals.items()):
        # The two checks are independent and neither may decide the other.
        # A target can be unreadable for a moment on Windows while a
        # neighbour from a killed earlier sweep sits beside it, and that
        # neighbour is what stops the NEXT run against the target, long
        # after the read failure has cleared. Skipping the second check on
        # a failed read hid exactly that (wh-keyboard-refusal-notice.1.10).
        try:
            current = target.read_bytes()
        except OSError as exc:
            problems.append(f"cannot read {target} at the end of the sweep; "
                            f"check it by hand: {exc}")
        else:
            if current != raw:
                problems.append(f"{target} did not match its pre-run bytes "
                                "at the end of the sweep; check it by hand")
        neighbour = target.with_name(target.name + ".gate-tmp")
        try:
            left = neighbour.exists()
        except OSError as exc:
            problems.append(f"cannot check for {neighbour} at the end of "
                            f"the sweep; look for it by hand: {exc}")
            continue
        if left:
            problems.append(f"{neighbour} is still here at the end of the "
                            "sweep; it is untracked, this run cannot "
                            "account for it, and it stops the next run "
                            "against that target -- read it, then delete it")
    return problems


def _run_pytest(selection):
    """Run the named selection in ONE pytest process."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", *selection, "-q", "-rf",
         "-p", "no:cacheprovider", "-p", "no:randomly"],
        cwd=SERVICE_DIR, env=env, capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S,
    )


def _node_key(node_id):
    """``Class::method`` for a class-based test, ``method`` for a bare one.

    The bare method name is not enough here: two classes in the same file
    hold same-shaped names.
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


def _apply(mutation, sources):
    """Return (working sources, error string or None)."""
    working = dict(sources)
    for target, old, new in _edits(mutation):
        count = working[target].count(old)
        if count != 1:
            return None, f"{mutation['name']}: pattern matched {count} " \
                         "times, need 1"
        working[target] = working[target].replace(old, new, 1)
    for target, mutated in working.items():
        if mutated == sources[target]:
            continue
        try:
            compile(mutated, str(target), "exec")
        except SyntaxError as exc:
            return None, f"{mutation['name']}: mutant does not compile: {exc}"
    return working, None


def _check(sources):
    """Answer "are the patterns current, and does each mutant parse".

    The two counts stay separate. A stale pattern and a mutant that does not
    parse are different repairs, and summing them would hide which one this
    run found. A mutant that does not compile is the worse of the two: it
    reads as ``caught`` in a full sweep, because the interpreter rejected it
    before a single test ran.
    """
    stale, broken = [], []
    for mutation in MUTATIONS:
        _, error = _apply(mutation, sources)
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


def _coverage_errors(every_test):
    """Check that every test this bead added is mutated or excluded by name.

    Five ways the claim can stop being true, all reported here:

    1. A FEATURE_TESTS entry that no mutation breaks and NOT_MUTATED does not
       name. That is the over-claim itself.
    2. A FEATURE_TESTS entry that no longer exists.
    3. NOT_MUTATED names a test that no longer exists.
    4. A name is in NOT_MUTATED and in some mutation's expect list.
    5. A reason cites another mutation by name. That claim is unverifiable
       where it is written and unchecked where it is read.
    """
    errors = []
    expected = {name for m in MUTATIONS for name in m["expect"]}

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

    targets = {HANDLER, SENDER, GATE}
    originals = {}
    for target in sorted(targets):
        raw = target.read_bytes()
        if b"\r\n" in raw:
            # The patterns here are written with LF. A file stored with CRLF
            # makes every multi-line pattern miss in a batch, which reads as
            # a survivor. Refuse rather than rewrite the file's endings, and
            # check per target: this repository mixes conventions per file.
            print(f"ERROR: {target.name} uses CRLF; patterns here are LF")
            return 1
        originals[target] = raw
    sources = {t: raw.decode("utf-8") for t, raw in originals.items()}

    if check_only:
        return _check(sources)

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

    held = None
    try:
        for mutation in mutations:
            name = mutation["name"]
            working, error = _apply(mutation, sources)
            if working is None:
                errors.append(str(error))
                print(f"ERROR {error}")
                continue

            changed = {
                target: source for target, source in working.items()
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
                errors.append(f"{name}: mutant never terminated")
                print(f"ERROR {name}: mutant never terminated")
                result = None
            finally:
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
                print(f"caught   {name}: {sorted(expect)}")
    finally:
        # An error, not a warning: this is the last place a tracked file
        # still holding a mutant can be noticed, and a run that noticed it
        # must not report success.
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
