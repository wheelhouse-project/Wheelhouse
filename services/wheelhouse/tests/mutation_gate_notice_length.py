"""Mutation gate for the notice-length guard (wh-notice-length-guard).

WHY THIS GATE EXISTS. The tests in tests/test_utils/test_notice_text.py
were written before utils/notice_text.py existed and were seen to fail
with ModuleNotFoundError. That is the first half of the rule. The second
half is that a test can fail for the wrong reason, so every protected
behaviour is broken here at the level of the INPUT the code depends on --
the comparison itself, the two limit constants, the characters the
shortened text ends with, the arguments the log line carries -- and not
only by deleting the guard.

WHAT THE BEAD ADDED, and therefore what this gate defends:

  the guard itself
      guard-removed             the length check always passes, so
                                nothing is measured and nothing is cut
      comparison-flipped        the check keeps its operands and reverses
                                its direction, so long text goes through
                                and short text is cut
      ellipsis-removed          the text is cut to the right length with
                                no sign to the reader that it was cut
      non-string-passthrough-removed
                                a value that is not text reaches len()
                                and raises, which drops the notice this
                                module exists to keep

  the two limits
      message-limit-raised-to-the-field-size
                                255 becomes 256, the array's own size,
                                which leaves no room for the terminator
      title-limit-raised-to-the-field-size
                                63 becomes 64, for the same reason
      message-limit-is-the-title-limit
                                the message is measured against 63

  the log line, which is where the removed words survive
      log-line-dropped          the overflow is cut silently
      log-drops-the-whole-text  the line reports only what was kept
      log-drops-the-real-length the line reports the limit rather than
                                the length that was measured
      log-level-lowered         the line drops to DEBUG, which the
                                default configuration does not record

  the unit the fields are measured in, which is the wide character
      width-measured-in-code-points
                                the count returns Python characters, so
                                two emoji inside a 255-character message
                                reach a field that holds 256 wide ones
      the-kept-prefix-counts-every-character-as-one
                                the same defect in the shortening
                                branch: a supplementary character is
                                budgeted at one slot and takes two
      the-prefix-may-run-one-wide-character-over
                                the smallest form, one slot over
      the-prefix-cuts-in-the-middle-of-a-character
                                the encoded bytes are sliced instead,
                                which can keep half a character and
                                produce a lone surrogate that cannot be
                                encoded at all
      the-prefix-keeps-the-whole-text
                                nothing is cut and the ellipsis is
                                appended to the whole text
      the-ellipsis-costs-nothing
                                the three the ellipsis needs are not
                                subtracted, so every shortened field is
                                three wide characters over
      the-log-reports-code-points-not-wide-characters
                                the line reports a count the field does
                                not use, which tells a reader the text
                                fit

  the two fields, which must both be measured and must not swap
      title-not-measured        only the message is guarded
      message-not-measured      only the title is guarded
      limits-swapped            each field is measured against the
                                other's limit

  the one place that reaches plyer, and the one scan that proves it
      the-guard-stops-calling-plyer / the-guard-stops-importing-plyer
                                every notice is lost, which is a
                                worse form of the reported failure
      the-sender-skips-the-fitting
                                the text reaches plyer unmeasured
      app-name-is-sent-even-when-it-was-not-given /
      a-timeout-of-zero-is-dropped
                                the caller acquires an argument it
                                never passed, or loses one it did
      the-sender-swallows-a-delivery-failure
                                a real failure returns True, which
                                makes every caller's handler dead
      a-call-site-calls-plyer-directly-again /
      a-call-site-reaches-plyer-under-an-alias
                                a second route to plyer opens and
                                the scan has to name it

  saying WHY there is no backend, which is the smaller silence
      the-sender-claims-success-with-no-backend /
      the-sender-claims-success-when-plyer-is-missing
                                False becomes True, so the six
                                sites that read the value are told
                                the notice went out
      the-sender-does-not-check-for-a-backend
                                the call goes ahead against a
                                notify that is not callable
      a-failed-plyer-import-is-not-recorded /
      a-notify-that-is-not-callable-is-not-recorded
                                the return value is right and
                                nothing is written, which is the
                                silence again at the five sites that
                                ignore the value
      the-failed-import-line-does-not-name-the-exception
                                the line is written but cannot tell
                                a reader whether plyer is absent or
                                installed and raising
      the-call-site-ignores-the-report
                                ui/ui_action_handler.py stops
                                reading what it is told, and the
                                loss goes unrecorded there again

WHAT THIS GATE DOES NOT CLAIM. It covers the tests THIS BEAD added, in
the two test files and the one test class named below. It makes no
claim about any other notice test in this repository, and none about
the other tests in that class, which the bead did not add.

Run it from services/wheelhouse, and NEVER while a test suite is running
in the same worktree -- this file rewrites the source that suite imports:

    uv run python tests/mutation_gate_notice_length.py

A single mutation by name runs alone:

    uv run python tests/mutation_gate_notice_length.py comparison-flipped
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE_DIR = Path(__file__).resolve().parents[1]

GUARD = SERVICE_DIR / "utils" / "notice_text.py"

# Two of the eleven call sites, one at the top level and one under
# utils/. The scan test judges the whole tree, so a mutation that puts a
# direct plyer call back has to be caught wherever it is put.
GUI = SERVICE_DIR / "gui.py"
SPEECH_NOTIFIER = SERVICE_DIR / "utils" / "speech_notifier.py"

# The one call site that reads what send_notice reports and writes
# its own line when there is no backend.
UI_ACTION_HANDLER = SERVICE_DIR / "ui" / "ui_action_handler.py"

GUARD_TESTS = "tests/test_utils/test_notice_text.py"
SCAN_TESTS = (
    "tests/test_utils/test_notice_sender_is_the_only_plyer_caller.py"
)

# The call site's tests are selected by node id, not by file. That
# file holds hundreds of tests about other subjects, and every
# mutation here runs the whole selection.
CALL_SITE_TESTS = (
    "tests/test_ui/test_ui_action_handler.py::TestShowNotification"
)

FEATURE_TEST_FILES = (GUARD_TESTS, SCAN_TESTS, CALL_SITE_TESTS)

# The file runs in under two seconds clean. No mutation here can make a
# test wait -- every one changes a comparison, a number or a string --
# so this limit exists only to turn a hang into a reported error rather
# than a lost run.
RUN_TIMEOUT_S = 300

_MESSAGE = "TestMessageLength"
_TITLE = "TestTitleLength"
_REPORTED = "TestTheOverflowIsReported"
_FITS = "TestTheOutputFitsTheRealStructure"
_NOT_TEXT = "TestInputThatIsNotText"
_LIMITS = "TestTheLimitsMatchPlyersOwnStructure"
_SENDS = "TestTheSenderFitsWhatItSends"
_SAYS = "TestTheSenderSaysWhetherItSent"
_ARGUMENTS = "TestTheSenderPassesOnlyTheArgumentsItWasGiven"
_RAISES = "TestADeliveryFailureReachesTheCaller"
_SCAN_SEES = "TestTheScanCanSeeTheCodeItJudges"
_ONLY = "TestOnlyTheGuardCallsPlyer"
_SCAN_REPORTS = "TestTheScanWouldReportARealOffender"
_WHY = "TestTheSenderSaysWhyThereIsNoBackend"
_SHOW = "TestShowNotification"
_UNITS = "TestTheOutputFitsInUtf16Units"


def _message(name):
    return f"{_MESSAGE}::{name}"


def _title(name):
    return f"{_TITLE}::{name}"


def _reported(name):
    return f"{_REPORTED}::{name}"


def _fits(name):
    return f"{_FITS}::{name}"


def _not_text(name):
    return f"{_NOT_TEXT}::{name}"


def _limits(name):
    return f"{_LIMITS}::{name}"


def _sends(name):
    return f"{_SENDS}::{name}"


def _says(name):
    return f"{_SAYS}::{name}"


def _arguments(name):
    return f"{_ARGUMENTS}::{name}"


def _raises(name):
    return f"{_RAISES}::{name}"


def _scan_sees(name):
    return f"{_SCAN_SEES}::{name}"


def _only(name):
    return f"{_ONLY}::{name}"


def _scan_reports(name):
    return f"{_SCAN_REPORTS}::{name}"


def _why(name):
    return f"{_WHY}::{name}"


def _show(name):
    return f"{_SHOW}::{name}"


def _units(name):
    return f"{_UNITS}::{name}"


# Every test that reads what plyer was handed. A sender that stops
# calling plyer, or calls it with the wrong text, breaks all of them.
_EVERY_DELIVERY = [
    _sends("test_a_message_over_the_limit_reaches_plyer_shortened"),
    _sends("test_a_title_over_the_limit_reaches_plyer_shortened"),
    _sends("test_text_that_already_fits_reaches_plyer_unchanged"),
    _sends("test_what_plyer_receives_fits_the_real_structure"),
    _arguments("test_app_name_and_timeout_are_left_out_when_not_given"),
    _arguments("test_app_name_and_timeout_are_passed_when_given"),
    _arguments("test_a_timeout_of_zero_is_still_passed"),
    _raises("test_an_overlong_notice_still_reaches_plyer"),
]


# Every test that proves an over-long message is cut. Removing the check
# or reversing it breaks all of them at once.
_EVERY_MESSAGE_SHORTENING = [
    _message("test_a_message_over_the_limit_is_shortened_to_fit"),
    _message("test_the_shortened_message_ends_with_a_visible_ellipsis"),
    _message("test_a_message_one_over_the_limit_is_shortened"),
    _fits("test_the_message_the_guard_returns_fits_szinfo"),
    # send_notice calls fit_notice_text, so the same break is visible
    # one level up, at what plyer was actually handed.
    _sends("test_a_message_over_the_limit_reaches_plyer_shortened"),
    _sends("test_what_plyer_receives_fits_the_real_structure"),
]

_EVERY_TITLE_SHORTENING = [
    _title("test_a_title_over_the_limit_is_shortened_to_fit"),
    _fits("test_the_title_the_guard_returns_fits_szinfotitle"),
    _sends("test_a_title_over_the_limit_reaches_plyer_shortened"),
    _sends("test_what_plyer_receives_fits_the_real_structure"),
]

# Every test that proves the guard measures WIDE characters and not
# Python characters. wh-notice-length-guard.2.1: one character above
# U+FFFF is one code point and two wide characters, so a count taken in
# code points passes text the field cannot hold.
_EVERY_WIDE_CHARACTER_PROOF = [
    _units("test_a_message_whose_code_points_fit_but_units_do_not_is_cut"),
    _units("test_that_message_fits_the_real_szinfo_array"),
    _units("test_a_title_whose_code_points_fit_but_units_do_not_is_cut"),
    _units("test_that_title_fits_the_real_szinfotitle_array"),
    _units("test_a_message_that_is_all_non_bmp_text_fits"),
    _units("test_an_overflow_measured_only_in_units_is_reported"),
]

# The wide-character tests each field breaks, measured from the sweep
# rather than predicted. Every mutation that stops measuring a field, or
# measures it against the other field's limit, breaks exactly these.
_EVERY_MESSAGE_WIDTH_PROOF = [
    _units("test_a_message_that_is_all_non_bmp_text_fits"),
    _units("test_a_message_whose_code_points_fit_but_units_do_not_is_cut"),
    _units("test_an_overflow_measured_only_in_units_is_reported"),
    _units("test_text_the_guard_shortens_is_measured_in_units_too"),
    _units("test_that_message_fits_the_real_szinfo_array"),
]

_EVERY_TITLE_WIDTH_PROOF = [
    _units("test_a_shortened_title_is_measured_in_units_too"),
    _units("test_a_title_whose_code_points_fit_but_units_do_not_is_cut"),
    _units("test_that_title_fits_the_real_szinfotitle_array"),
]

_EVERY_SHORTENED_PREFIX_PROOF = [
    _units("test_text_the_guard_shortens_is_measured_in_units_too"),
    _units("test_a_shortened_title_is_measured_in_units_too"),
    _units("test_a_message_that_is_all_non_bmp_text_fits"),
]

_EVERY_OVERFLOW_REPORT = [
    _reported("test_an_overflowing_message_is_logged_at_warning"),
    _reported("test_the_warning_names_the_field_and_the_measured_length"),
    _reported("test_the_warning_carries_the_whole_text_that_was_shortened"),
    _reported("test_an_overflowing_title_is_logged_at_warning"),
]

MUTATIONS = [
    # -----------------------------------------------------------------
    # The guard itself.
    # -----------------------------------------------------------------
    {
        # The plainest form: the check answers yes for everything, so
        # every value is returned as it arrived. This is the state the
        # bead found the code in.
        "name": "guard-removed",
        "target": GUARD,
        "old": "    if width <= limit:\n",
        "new": "    if True:\n",
        "expect": (
            _EVERY_MESSAGE_SHORTENING
            + _EVERY_TITLE_SHORTENING
            + _EVERY_OVERFLOW_REPORT
            + _EVERY_MESSAGE_WIDTH_PROOF
            + _EVERY_TITLE_WIDTH_PROOF
        ),
    },
    {
        # The direction of the comparison, with both operands left
        # alone. A test that only watched for "something was cut" would
        # survive this: text still gets cut, just the wrong text.
        "name": "comparison-flipped",
        "target": GUARD,
        "old": "    if width <= limit:\n",
        "new": "    if width >= limit:\n",
        "expect": (
            _EVERY_MESSAGE_SHORTENING
            + _EVERY_TITLE_SHORTENING
            + _EVERY_OVERFLOW_REPORT
            + _EVERY_MESSAGE_WIDTH_PROOF
            + _EVERY_TITLE_WIDTH_PROOF
            + [
                _reported("test_a_notice_that_fits_logs_nothing"),
                _title("test_a_long_title_does_not_disturb_the_message"),
                _message(
                    "test_a_real_notice_of_245_characters_passes_unchanged"
                ),
                # Everything short is cut instead, so the text plyer
                # receives stops matching the text it was given.
                _sends("test_text_that_already_fits_reaches_plyer_unchanged"),
                _arguments(
                    "test_app_name_and_timeout_are_left_out_when_not_given"
                ),
                _arguments("test_app_name_and_timeout_are_passed_when_given"),
                # The call site reads the exact text plyer was
                # handed, and an ordinary notice now warns.
                _show("test_valid_notification"),
                _why("test_a_working_backend_writes_no_warning"),
            ]
        ),
    },
    {
        # The text is cut to a length that fits and the reader is given
        # no sign that anything was removed. Length-only tests survive
        # this, which is why the ellipsis has a test of its own.
        "name": "ellipsis-removed",
        "target": GUARD,
        "old": (
            "    kept = _longest_prefix_within(\n"
            "        text, limit - _wide_characters(ELLIPSIS)\n"
            "    ) + ELLIPSIS\n"
        ),
        "new": "    kept = _longest_prefix_within(text, limit)\n",
        "expect": [
            _message("test_the_shortened_message_ends_with_a_visible_ellipsis"),
        ],
    },
    {
        # The pass-through for values that are not text. Without it
        # len(None) raises TypeError inside the guard, so the notice is
        # lost -- the exact outcome this module exists to prevent. The
        # three tests below fail on that TypeError rather than on an
        # assertion, and that is the right reason: the behaviour they
        # pin is "the guard does not raise".
        "name": "non-string-passthrough-removed",
        "target": GUARD,
        "old": "    if not isinstance(text, str):\n        return text\n",
        "new": "    if not isinstance(text, str):\n        pass\n",
        "expect": [
            _not_text("test_a_message_that_is_not_text_passes_through"),
            _not_text("test_a_title_that_is_not_text_passes_through"),
            _not_text("test_a_value_that_is_not_text_logs_nothing"),
        ],
    },
    # -----------------------------------------------------------------
    # The unit the guard measures in (wh-notice-length-guard.2.1). Every
    # mutation here restores a form of the defect codex round 1 found:
    # the field counts wide characters, and counting anything else lets
    # text through that ctypes rejects on plyer's own thread.
    # -----------------------------------------------------------------
    {
        # The defect exactly as it was committed: measure code points.
        # Text of 255 code points holding two emoji is 257 wide
        # characters, and the guard passes it untouched.
        "name": "width-measured-in-code-points",
        "target": GUARD,
        "old": '    return len(text.encode("utf-16-le")) // 2\n',
        "new": "    return len(text)\n",
        "expect": _EVERY_WIDE_CHARACTER_PROOF,
    },
    {
        # The half of the defect that lives in the shortening branch. A
        # supplementary character costs one instead of two, so the kept
        # prefix is measured short and the result still overflows.
        "name": "the-kept-prefix-counts-every-character-as-one",
        "target": GUARD,
        "old": "        width = 2 if ord(character) > 0xFFFF else 1\n",
        "new": "        width = 1\n",
        "expect": _EVERY_SHORTENED_PREFIX_PROOF,
    },
    {
        # One wide character over. The smallest form of the same
        # failure, and the one a length-only test survives.
        "name": "the-prefix-may-run-one-wide-character-over",
        "target": GUARD,
        "old": "        if used + width > budget:\n",
        "new": "        if used + width > budget + 1:\n",
        # Not the all-emoji test: every character there costs two, so
        # the extra slot is never usable and the kept text is the same.
        "expect": [
            _units("test_text_the_guard_shortens_is_measured_in_units_too"),
            _units("test_a_shortened_title_is_measured_in_units_too"),
            _units("test_a_message_whose_code_points_fit_but_units_do_not_is_cut"),
            _units("test_a_title_whose_code_points_fit_but_units_do_not_is_cut"),
            _message("test_a_message_over_the_limit_is_shortened_to_fit"),
            _message("test_a_message_one_over_the_limit_is_shortened"),
            _title("test_a_title_over_the_limit_is_shortened_to_fit"),
            _sends("test_a_message_over_the_limit_reaches_plyer_shortened"),
            _sends("test_a_title_over_the_limit_reaches_plyer_shortened"),
        ],
    },
    {
        # The naive implementation: slice the encoded bytes at twice the
        # budget. It lands between the two halves of a supplementary
        # character whenever an odd number of wide characters comes
        # before one, and the lone surrogate that produces cannot be
        # encoded at all -- a worse loss than the overflow this module
        # guards against.
        "name": "the-prefix-cuts-in-the-middle-of-a-character",
        "target": GUARD,
        "old": (
            "    used = 0\n"
            "    for index, character in enumerate(text):\n"
            "        width = 2 if ord(character) > 0xFFFF else 1\n"
            "        if used + width > budget:\n"
            "            return text[:index]\n"
            "        used += width\n"
            "    return text\n"
        ),
        "new": (
            "    encoded = text.encode(\"utf-16-le\")[: budget * 2]\n"
            "    return encoded.decode(\"utf-16-le\", \"surrogatepass\")\n"
        ),
        "expect": [
            _units("test_the_guard_never_splits_a_supplementary_character"),
        ],
    },
    {
        # Nothing is cut at all: the whole text comes back and the
        # ellipsis is appended to it.
        "name": "the-prefix-keeps-the-whole-text",
        "target": GUARD,
        "old": "            return text[:index]\n",
        "new": "            return text\n",
        # The ellipsis test is left out on purpose: the ellipsis is
        # still appended to the whole text, so it still ends with one.
        "expect": (
            [
                m for m in _EVERY_MESSAGE_SHORTENING
                if not m.endswith("ends_with_a_visible_ellipsis")
            ]
            + _EVERY_TITLE_SHORTENING
            + _EVERY_SHORTENED_PREFIX_PROOF
            + [
                _units(
                    "test_a_message_whose_code_points_fit_but_units_do_not"
                    "_is_cut"
                ),
                _units(
                    "test_a_title_whose_code_points_fit_but_units_do_not"
                    "_is_cut"
                ),
                _units("test_that_message_fits_the_real_szinfo_array"),
                _units("test_that_title_fits_the_real_szinfotitle_array"),
            ]
        ),
    },
    {
        # The ellipsis is still appended but no budget is left for it,
        # so every shortened field is three wide characters over.
        "name": "the-ellipsis-costs-nothing",
        "target": GUARD,
        "old": "        text, limit - _wide_characters(ELLIPSIS)\n",
        "new": "        text, limit\n",
        # Same reason: the ellipsis is appended, it is simply appended
        # to text that already fills the field.
        "expect": (
            [
                m for m in _EVERY_MESSAGE_SHORTENING
                if not m.endswith("ends_with_a_visible_ellipsis")
            ]
            + _EVERY_TITLE_SHORTENING
            # Not the report test: the log line still names the
            # real width. This mutation is about what survives
            # the cut, not about what the line says.
            + [
                m for m in _EVERY_MESSAGE_WIDTH_PROOF
                if not m.endswith("measured_only_in_units_is_reported")
            ]
            + _EVERY_TITLE_WIDTH_PROOF
        ),
    },
    {
        # The two helpers exist so both branches share one measurement.
        # Measuring the kept text in code points makes the log line
        # report a length the field does not use.
        "name": "the-log-reports-code-points-not-wide-characters",
        "target": GUARD,
        "old": (
            "        field_name, width, limit, _wide_characters(kept), text,\n"
        ),
        "new": "        field_name, len(text), limit, len(kept), text,\n",
        # The ASCII overflow tests cannot see this: for text of one wide
        # character per code point the two counts are the same number.
        # Only text that is over by wide characters alone tells them
        # apart.
        "expect": [
            _units("test_an_overflow_measured_only_in_units_is_reported"),
        ],
    },
    # -----------------------------------------------------------------
    # The two limits. Every test that reads a constant moves with the
    # constant, so these are caught by the two tests that compute the
    # expected value from plyer's own field table instead.
    # -----------------------------------------------------------------
    {
        "name": "message-limit-raised-to-the-field-size",
        "target": GUARD,
        "old": "MAX_MESSAGE_WIDE_CHARACTERS = 255\n",
        "new": "MAX_MESSAGE_WIDE_CHARACTERS = 256\n",
        "expect": [
            _limits("test_the_message_limit_is_one_below_szinfo"),
            _message("test_a_message_one_over_the_limit_is_shortened"),
        ],
    },
    {
        "name": "title-limit-raised-to-the-field-size",
        "target": GUARD,
        "old": "MAX_TITLE_WIDE_CHARACTERS = 63\n",
        "new": "MAX_TITLE_WIDE_CHARACTERS = 64\n",
        "expect": [
            _limits("test_the_title_limit_is_one_below_szinfotitle"),
        ],
    },
    {
        # A value that is safe for both fields, so nothing raises and
        # nothing overflows -- the message is simply cut 192 wide
        # characters early, which is the 255 above minus this 63.
        # Two tests catch it: the plyer-derived limit test, and the
        # plain 245-character notice test, which never reads plyer's
        # structure.
        "name": "message-limit-is-the-title-limit",
        "target": GUARD,
        "old": "MAX_MESSAGE_WIDE_CHARACTERS = 255\n",
        "new": "MAX_MESSAGE_WIDE_CHARACTERS = 63\n",
        "expect": [
            _limits("test_the_message_limit_is_one_below_szinfo"),
            _message("test_a_real_notice_of_245_characters_passes_unchanged"),
        ],
    },
    # -----------------------------------------------------------------
    # The log line.
    # -----------------------------------------------------------------
    {
        "name": "log-line-dropped",
        "target": GUARD,
        "old": (
            "    logger.warning(\n"
            '        "Notice %s field was %d wide characters, over the %d the Windows "\n'
            '        "notification structure holds; it was shortened to %d and the "\n'
            '        "whole text follows. Original: %s",\n'
            "        field_name, width, limit, _wide_characters(kept), text,\n"
            "    )\n"
        ),
        "new": "    pass\n",
        "expect": _EVERY_OVERFLOW_REPORT + [
            _units("test_an_overflow_measured_only_in_units_is_reported"),
        ],
    },
    {
        # The line stays and reports only what survived, so the removed
        # words are gone from the log as well as from the notice.
        "name": "log-drops-the-whole-text",
        "target": GUARD,
        "old": (
            "        field_name, width, limit, _wide_characters(kept), text,\n"
        ),
        "new": (
            "        field_name, width, limit, _wide_characters(kept), kept,\n"
        ),
        "expect": [
            _reported(
                "test_the_warning_carries_the_whole_text_that_was_shortened"
            ),
            _units("test_an_overflow_measured_only_in_units_is_reported"),
        ],
    },
    {
        # The line reports the limit where it should report what was
        # measured, so every overflow looks the same size.
        "name": "log-drops-the-real-length",
        "target": GUARD,
        "old": (
            "        field_name, width, limit, _wide_characters(kept), text,\n"
        ),
        "new": (
            "        field_name, limit, limit, _wide_characters(kept), text,\n"
        ),
        "expect": [
            _reported("test_the_warning_names_the_field_and_the_measured_length"),
            _units("test_an_overflow_measured_only_in_units_is_reported"),
        ],
    },
    {
        # DEBUG is below the level the shipped configuration records, so
        # the overflow becomes invisible again without the line moving.
        "name": "log-level-lowered",
        "target": GUARD,
        # Anchored on the line above the call. Four leading spaces
        # alone is not unique any more: send_notice writes two
        # warnings of its own at eight, and each contains this text.
        "old": (
            "    ) + ELLIPSIS\n"
            "    logger.warning(\n"
        ),
        "new": (
            "    ) + ELLIPSIS\n"
            "    logger.debug(\n"
        ),
        "expect": _EVERY_OVERFLOW_REPORT + [
            _units("test_an_overflow_measured_only_in_units_is_reported"),
        ],
    },
    # -----------------------------------------------------------------
    # The two fields, in fit_notice_text.
    # -----------------------------------------------------------------
    {
        # Every wide-character title test fails too: an unmeasured
        # title is exactly what this bead's second finding was about.
        "name": "title-not-measured",
        "target": GUARD,
        "old": '        _fit_one_field(title, MAX_TITLE_WIDE_CHARACTERS, "title"),\n',
        "new": "        title,\n",
        "expect": _EVERY_TITLE_SHORTENING + _EVERY_TITLE_WIDTH_PROOF + [
            _reported("test_an_overflowing_title_is_logged_at_warning"),
        ],
    },
    {
        "name": "message-not-measured",
        "target": GUARD,
        "old": (
            "        _fit_one_field(message, MAX_MESSAGE_WIDE_CHARACTERS, "
            '"message"),\n'
        ),
        "new": "        message,\n",
        "expect": _EVERY_MESSAGE_SHORTENING + _EVERY_MESSAGE_WIDTH_PROOF + [
            _reported("test_an_overflowing_message_is_logged_at_warning"),
            _reported("test_the_warning_names_the_field_and_the_measured_length"),
            _reported(
                "test_the_warning_carries_the_whole_text_that_was_shortened"
            ),
        ],
    },
    {
        # Both fields are still measured, against each other's limit.
        # The message keeps far too little and the title keeps far too
        # much, which is the failure the bead's A4 reading exists to
        # prevent.
        "name": "limits-swapped",
        "target": GUARD,
        "old": (
            '        _fit_one_field(title, MAX_TITLE_WIDE_CHARACTERS, "title"),\n'
            "        _fit_one_field(message, MAX_MESSAGE_WIDE_CHARACTERS, "
            '"message"),\n'
        ),
        "new": (
            '        _fit_one_field(title, MAX_MESSAGE_WIDE_CHARACTERS, "title"),\n'
            "        _fit_one_field(message, MAX_TITLE_WIDE_CHARACTERS, "
            '"message"),\n'
        ),
        "expect": _EVERY_TITLE_SHORTENING + _EVERY_TITLE_WIDTH_PROOF + [
            _message("test_a_message_at_the_limit_passes_unchanged"),
            _message("test_a_real_notice_of_245_characters_passes_unchanged"),
            _reported("test_an_overflowing_title_is_logged_at_warning"),
            _sends("test_a_message_over_the_limit_reaches_plyer_shortened"),
        ],
    },
    # -----------------------------------------------------------------
    # The sender, and the claim that it is the only route to plyer.
    # -----------------------------------------------------------------
    {
        # One of the eleven notices sent the old way again, at the
        # top level of the tree. This REPLACES the send_notice call
        # at that site with a direct plyer call rather than adding a
        # twelfth notice, so the total stays eleven. Nothing about
        # the text changes, so only the scan sees it.
        "name": "a-call-site-calls-plyer-directly-again",
        "target": GUI,
        "old": (
            '                elif action == "show_notification":\n'
            "                    if not send_notice(\n"
        ),
        "new": (
            '                elif action == "show_notification":\n'
            "                    from plyer import notification\n"
            "                    if not notification.notify(\n"
        ),
        "expect": [
            _only("test_no_other_module_calls_plyer_notify"),
            _only("test_no_other_module_imports_plyer"),
        ],
    },
    {
        # The same evasion under an alias, in a different directory.
        # The text "notification.notify(" never appears, so the call
        # scan cannot see it and only the import scan can. This is why
        # the second, stronger scan exists.
        "name": "a-call-site-reaches-plyer-under-an-alias",
        "target": SPEECH_NOTIFIER,
        "old": (
            "            if send_notice(title, message, "
            "app_name='Wheelhouse', timeout=3):\n"
        ),
        "new": (
            "            from plyer import notification as _n\n"
            "            if _n.notify(title=title, message=message):\n"
        ),
        "expect": [_only("test_no_other_module_imports_plyer")],
    },
    {
        # The guard stops reaching plyer at all. Every notice is lost,
        # which is a worse version of the failure the bead opened on.
        "name": "the-guard-stops-calling-plyer",
        "target": GUARD,
        "old": (
            "    notification.notify(**arguments)  # type: ignore[misc]\n"
            "    return True\n"
        ),
        "new": "    return True\n",
        "expect": _EVERY_DELIVERY + [
            _only("test_the_guard_itself_holds_the_call"),
            _raises("test_an_exception_from_plyer_is_not_swallowed"),
            _show("test_valid_notification"),
        ],
    },
    {
        # The guard stops importing plyer, so it can never send.
        "name": "the-guard-stops-importing-plyer",
        "target": GUARD,
        "old": (
            "    try:\n"
            "        from plyer import notification\n"
            "    except Exception as exc:\n"
            "        # The exception type is what separates \"plyer is not installed\"\n"
            "        # from \"plyer is installed and raises on import\". Both produce\n"
            "        # False, and a reader who only sees False cannot tell them\n"
            "        # apart or know where to look.\n"
            "        logger.warning(\n"
            "            \"Notice not sent: importing plyer raised %s: %s. Title: %s\",\n"
            "            type(exc).__name__, exc, title,\n"
            "        )\n"
            "        return False\n"
        ),
        "new": "    notification = None\n",
        "expect": _EVERY_DELIVERY + [
            # notification is None, so the not-callable branch
            # fires on every call: an ordinary notice warns and is
            # never delivered, and a missing plyer is reported as a
            # backend that will not answer rather than as an import
            # that failed.
            _show("test_valid_notification"),
            _why("test_a_working_backend_writes_no_warning"),
            _why("test_the_log_line_names_the_reason_the_import_failed"),
            _only("test_the_guard_itself_imports_plyer"),
            _raises("test_an_exception_from_plyer_is_not_swallowed"),
            _says("test_it_reports_true_when_plyer_took_the_notice"),
        ],
    },
    {
        # The sender hands plyer the text it was given. This is the
        # whole bead undone while every fit_notice_text test stays
        # green, because nothing would call it any more.
        "name": "the-sender-skips-the-fitting",
        "target": GUARD,
        "old": (
            "    fitted_title, fitted_message = fit_notice_text("
            "title, message)\n"
        ),
        "new": "    fitted_title, fitted_message = title, message\n",
        "expect": [
            _sends("test_a_message_over_the_limit_reaches_plyer_shortened"),
            _sends("test_a_title_over_the_limit_reaches_plyer_shortened"),
            _sends("test_what_plyer_receives_fits_the_real_structure"),
        ],
    },
    {
        # "I sent it" when there was nothing to send it with. Five of the
        # eleven call sites ignore this value entirely; of the six that
        # read it, five write their own unavailable line, and all five
        # go quiet under this mutation.
        "name": "the-sender-claims-success-with-no-backend",
        "target": GUARD,
        "old": (
            '            getattr(notification, "notify", None), title,\n'
            "        )\n"
            "        return False\n"
        ),
        "new": (
            '            getattr(notification, "notify", None), title,\n'
            "        )\n"
            "        return True\n"
        ),
        "expect": [_says("test_it_reports_false_when_notify_is_not_callable")],
        "also_fails": {
            _why("test_a_notify_that_is_not_callable_is_logged_too"): (
                "It reads the reported value before it reads the log, "
                "so it falls over on the wrong report. The line it "
                "exists to pin is still written under this mutation, "
                "so its own subject is untouched."
            ),
        },
    },
    {
        # The check is gone rather than wrong, so the call goes ahead
        # against a notify that is not callable. The catchers fail on
        # the TypeError that check exists to prevent -- which is the
        # production failure, not an unrelated crash.
        "name": "the-sender-does-not-check-for-a-backend",
        "target": GUARD,
        "old": (
            '            getattr(notification, "notify", None), title,\n'
            "        )\n"
            "        return False\n"
        ),
        "new": (
            '            getattr(notification, "notify", None), title,\n'
            "        )\n"
            "        pass\n"
        ),
        "expect": [
            _says("test_it_reports_false_when_notify_is_not_callable"),
            _says("test_nothing_reaches_plyer_when_notify_is_not_callable"),
        ],
        "also_fails": {
            _why("test_a_notify_that_is_not_callable_is_logged_too"): (
                "The warning it pins is still written. It fails on "
                "the TypeError raised further down, after its own "
                "subject has already been satisfied."
            ),
        },
    },
    {
        # plyer is not installed at all, and the sender says it sent.
        "name": "the-sender-claims-success-when-plyer-is-missing",
        "target": GUARD,
        "old": (
            "            type(exc).__name__, exc, title,\n"
            "        )\n"
            "        return False\n"
        ),
        "new": (
            "            type(exc).__name__, exc, title,\n"
            "        )\n"
            "        return True\n"
        ),
        "expect": [
            _says("test_it_reports_false_when_plyer_cannot_be_imported"),
            # The one call site that reads the report writes nothing
            # when it is told the notice went out.
            _show("test_a_missing_backend_is_still_logged_here"),
        ],
        "also_fails": {
            _why("test_a_failed_plyer_import_is_logged"): (
                "It reads the reported value before it reads the log. "
                "The WARNING it exists to pin is still written."
            ),
        },
    },
    {
        # The import failed and nothing recorded it. This is the silence
        # this bead exists to remove, in its smallest form: five of the
        # eleven call sites ignore the return value, so at those five a
        # broken plyer would leave no notice and no line anywhere.
        "name": "a-failed-plyer-import-is-not-recorded",
        "target": GUARD,
        "old": (
            "        logger.warning(\n"
            '            "Notice not sent: importing plyer raised '
            '%s: %s. Title: %s",\n'
            "            type(exc).__name__, exc, title,\n"
            "        )\n"
            "        return False\n"
        ),
        "new": "        return False\n",
        "expect": [
            _why("test_a_failed_plyer_import_is_logged"),
            _why("test_the_log_line_names_the_reason_the_import_failed"),
        ],
    },
    {
        # The line is written but says nothing useful. The exception
        # class is what separates "plyer is not installed" from "plyer
        # is installed and raises on import", and a reader who has only
        # "no backend" cannot tell which one to go and fix.
        "name": "the-failed-import-line-does-not-name-the-exception",
        "target": GUARD,
        "old": "            type(exc).__name__, exc, title,\n",
        "new": '            "a problem", "no detail", title,\n',
        "expect": [
            _why("test_the_log_line_names_the_reason_the_import_failed"),
        ],
    },
    {
        # plyer imported but its notify is not callable, and nothing
        # records it. Same silence as the failed import, reached the
        # other way.
        "name": "a-notify-that-is-not-callable-is-not-recorded",
        "target": GUARD,
        "old": (
            "        logger.warning(\n"
            '            "Notice not sent: plyer imported but its notify '
            'attribute is "\n'
            '            "%r, which is not callable. Title: %s",\n'
            '            getattr(notification, "notify", None), title,\n'
            "        )\n"
            "        return False\n"
        ),
        "new": "        return False\n",
        "expect": [
            _why("test_a_notify_that_is_not_callable_is_logged_too"),
        ],
    },
    {
        # The one call site that reads the report stops reading it. The
        # site logs nothing on its own otherwise, and the caller gets
        # None either way, so the loss goes back to being invisible
        # exactly where this branch removed the last silence.
        "name": "the-call-site-ignores-the-report",
        "target": UI_ACTION_HANDLER,
        "old": "                if not send_measured_notice(\n",
        "new": "                if send_measured_notice(\n",
        "expect": [
            _show("test_a_missing_backend_is_still_logged_here"),
        ],
    },
    {
        # app_name=None reaches plyer as an argument. Six of the
        # eleven sites never pass one, and would acquire a None
        # app_name they never asked for.
        "name": "app-name-is-sent-even-when-it-was-not-given",
        "target": GUARD,
        "old": "    if app_name is not None:\n",
        "new": "    if True:\n",
        "expect": [
            _arguments("test_app_name_and_timeout_are_left_out_when_not_given"),
        ],
    },
    {
        # Truthiness instead of an identity check, so timeout=0 is
        # dropped and plyer applies its own default instead.
        "name": "a-timeout-of-zero-is-dropped",
        "target": GUARD,
        "old": "    if timeout is not None:\n",
        "new": "    if timeout:\n",
        "expect": [_arguments("test_a_timeout_of_zero_is_still_passed")],
    },
    {
        # A delivery failure is swallowed here, which makes the
        # error-logging branch of every caller unreachable and turns a
        # lost notice back into silence.
        "name": "the-sender-swallows-a-delivery-failure",
        "target": GUARD,
        "old": (
            "    notification.notify(**arguments)  # type: ignore[misc]\n"
            "    return True\n"
        ),
        "new": (
            "    try:\n"
            "        notification.notify(**arguments)  # type: ignore[misc]\n"
            "    except Exception:\n"
            "        pass\n"
            "    return True\n"
        ),
        "expect": [
            _raises("test_an_exception_from_plyer_is_not_swallowed"),
        ],
    },
]

# Every test this bead added. _coverage_errors below refuses to run if
# one of these is broken by no mutation and is not excused by name.
FEATURE_TESTS = {
    _message("test_a_message_over_the_limit_is_shortened_to_fit"),
    _message("test_the_shortened_message_ends_with_a_visible_ellipsis"),
    _message("test_a_message_at_the_limit_passes_unchanged"),
    _message("test_a_message_one_over_the_limit_is_shortened"),
    _message("test_a_real_notice_of_245_characters_passes_unchanged"),
    _title("test_a_title_over_the_limit_is_shortened_to_fit"),
    _title("test_a_title_at_the_limit_passes_unchanged"),
    _title("test_a_long_title_does_not_disturb_the_message"),
    _reported("test_an_overflowing_message_is_logged_at_warning"),
    _reported("test_the_warning_names_the_field_and_the_measured_length"),
    _reported("test_the_warning_carries_the_whole_text_that_was_shortened"),
    _reported("test_an_overflowing_title_is_logged_at_warning"),
    _reported("test_a_notice_that_fits_logs_nothing"),
    _fits("test_the_message_the_guard_returns_fits_szinfo"),
    _fits("test_the_title_the_guard_returns_fits_szinfotitle"),
    _not_text("test_a_message_that_is_not_text_passes_through"),
    _not_text("test_a_title_that_is_not_text_passes_through"),
    _not_text("test_a_value_that_is_not_text_logs_nothing"),
    _limits("test_the_message_limit_is_one_below_szinfo"),
    _limits("test_the_title_limit_is_one_below_szinfotitle"),
    _limits("test_app_name_needs_no_guard_because_sztip_is_far_larger"),
    _sends("test_a_message_over_the_limit_reaches_plyer_shortened"),
    _sends("test_a_title_over_the_limit_reaches_plyer_shortened"),
    _sends("test_text_that_already_fits_reaches_plyer_unchanged"),
    _sends("test_what_plyer_receives_fits_the_real_structure"),
    _says("test_it_reports_true_when_plyer_took_the_notice"),
    _says("test_it_reports_false_when_notify_is_not_callable"),
    _says("test_it_reports_false_when_plyer_cannot_be_imported"),
    _says("test_nothing_reaches_plyer_when_notify_is_not_callable"),
    _arguments("test_app_name_and_timeout_are_left_out_when_not_given"),
    _arguments("test_app_name_and_timeout_are_passed_when_given"),
    _arguments("test_a_timeout_of_zero_is_still_passed"),
    _raises("test_an_exception_from_plyer_is_not_swallowed"),
    _raises("test_an_overlong_notice_still_reaches_plyer"),
    _scan_sees("test_the_scan_covers_a_large_part_of_the_service"),
    _scan_sees("test_the_guard_itself_is_inside_the_scanned_tree"),
    _scan_sees("test_the_guard_is_excluded_from_the_offender_list_by_name"),
    _only("test_no_other_module_calls_plyer_notify"),
    _only("test_the_guard_itself_holds_the_call"),
    _only("test_no_other_module_imports_plyer"),
    _only("test_the_guard_itself_imports_plyer"),
    _scan_reports("test_a_planted_direct_call_is_named"),
    _scan_reports("test_a_planted_import_is_named"),
    _scan_reports("test_a_test_file_that_quotes_the_call_is_not_named"),
    _why("test_a_failed_plyer_import_is_logged"),
    _why("test_the_log_line_names_the_reason_the_import_failed"),
    _why("test_a_notify_that_is_not_callable_is_logged_too"),
    _why("test_a_working_backend_writes_no_warning"),
    _show("test_a_missing_backend_is_still_logged_here"),
    _units("test_a_message_whose_code_points_fit_but_units_do_not_is_cut"),
    _units("test_that_message_fits_the_real_szinfo_array"),
    _units("test_a_title_whose_code_points_fit_but_units_do_not_is_cut"),
    _units("test_that_title_fits_the_real_szinfotitle_array"),
    _units("test_text_the_guard_shortens_is_measured_in_units_too"),
    _units("test_a_shortened_title_is_measured_in_units_too"),
    _units("test_a_message_that_is_all_non_bmp_text_fits"),
    _units("test_the_guard_never_splits_a_supplementary_character"),
    _units("test_an_overflow_measured_only_in_units_is_reported"),
}

NOT_MUTATED = {
    _title("test_a_title_at_the_limit_passes_unchanged"): (
        "A title of exactly 63 characters is inside every limit this "
        "guard could plausibly carry, so no single-value change to the "
        "source makes it fail. It states the boundary the other title "
        "tests are measured against and is kept for the reader."
    ),
    _limits("test_app_name_needs_no_guard_because_sztip_is_far_larger"): (
        "It asserts a property of plyer's structure and of a literal "
        "written in another module, not of this guard. Nothing in "
        "utils/notice_text.py can change its answer."
    ),
    _scan_sees("test_the_scan_covers_a_large_part_of_the_service"): (
        "It measures the scan's own reach, not the code under it. Any "
        "source change that made it fail would have deleted most of "
        "services/wheelhouse."
    ),
    _scan_sees("test_the_guard_itself_is_inside_the_scanned_tree"): (
        "It asserts that utils/notice_text.py exists at the path the "
        "scan excuses. Deleting the guard is not a mutation; it is the "
        "removal of the whole feature, which every other test reports."
    ),
    _scan_sees("test_the_guard_is_excluded_from_the_offender_list_by_name"): (
        "The same property from the other side: the excused path is "
        "one the walk really produces. No edit to a source file under "
        "test changes it."
    ),
    _scan_reports("test_a_planted_direct_call_is_named"): (
        "It runs the matcher over a throwaway tree built inside the "
        "test. Nothing in services/wheelhouse is read, so no mutation "
        "of the code can reach it."
    ),
    _scan_reports("test_a_planted_import_is_named"): (
        "Same throwaway tree, same reason: it proves the matcher can "
        "report, not that the tree is clean."
    ),
    _scan_reports("test_a_test_file_that_quotes_the_call_is_not_named"): (
        "It pins the one exclusion the scan has to make -- this file "
        "and the gates beside it quote the forbidden text -- against a "
        "planted tree, not against the real one."
    ),
}


def _edits(mutation):
    """Every (target, old, new) this mutation applies, in order."""
    if "edits" in mutation:
        return [(e["target"], e["old"], e["new"]) for e in mutation["edits"]]
    return [(mutation["target"], mutation["old"], mutation["new"])]


def _clear_pycache(root=None):
    """Remove every __pycache__ under the service; return (error, interrupt).

    Python decides whether cached bytecode is current from the source
    file's (mtime, size), and it truncates that mtime to an INTEGER
    SECOND. Six mutations here keep the source the same byte length, so a
    .pyc written for the clean source before the gate started is still
    valid for a mutant written in the same second, and the pytest child
    then executes clean code while a mutant sits on disk.

    ``PYTHONDONTWRITEBYTECODE`` is not a second line of defence. It stops
    Python WRITING a .pyc; it does not stop Python reading one that is
    already there. The collection subprocess above runs without that
    variable, so it can leave one.

    ``ignore_errors=True`` is what makes the check after it necessary: a
    directory Windows will not delete -- an antivirus scan or another
    process holding a file in it -- leaves ``rmtree`` silent and the
    directory in place, so the only way to know the sweep worked is to
    look. Every surviving directory goes to the next pass, because the
    hold is usually momentary, and only a survivor after the last pass is
    an error.

    A PASS PUBLISHES ITS RESULT ONLY WHEN IT COMPLETES, which is why
    each one collects into ``found`` rather than into ``survivors``.
    An earlier version reset both ``survivors`` and ``swept`` at the top
    of every pass, so a run whose pass 1 finished with a survivor and
    whose pass 2 was interrupted reported that EVERY attempt had been
    interrupted, and had already thrown away the directory pass 1 found.
    ``swept`` therefore means "at least one pass completed", and
    ``survivors`` holds the last completed pass's list.

    ``root`` is the directory swept, and only the tests pass it. It stays
    the whole service because the four mutation targets sit in THREE
    directories -- the service root, utils/ and ui/ -- so their cached
    bytecode sits in three separate __pycache__ directories, and the
    collection subprocess leaves caches this gate never enumerates.
    Sweeping the service covers all of them without keeping a list
    correct as the targets move.
    AN EARLIER VERSION GAVE A REASON PYTHON DOES NOT USE, and codex
    round 6 caught it: it said a mutant module is imported through
    modules carrying cached bytecode of their own. An importer's .pyc
    still executes its import at run time, and that import re-checks the
    imported module's own source against its own .pyc, so a stale
    importer cache cannot hide a mutant.
    ``.venv`` is excluded from the DELETION, not from the walk. ``rglob``
    still descends into it; the check protects installed packages rather
    than saving traversal time.

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


def _run_pytest(test_files):
    """Run every named test file in ONE pytest process."""
    env = dict(os.environ)
    # Python decides whether cached bytecode is current from the source
    # file's (mtime, size), and SIX mutations here leave that size
    # identical: comparison-flipped,
    # message-limit-raised-to-the-field-size,
    # title-limit-raised-to-the-field-size, log-drops-the-whole-text,
    # log-drops-the-real-length and limits-swapped. That six is
    # measured, by comparing each mutation's "old" against its
    # "new" byte length, rather than counted by eye.
    #
    # TWO STEPS PROTECT THE RUN, and the variable below is only one
    # of them. PYTHONDONTWRITEBYTECODE stops Python WRITING a .pyc.
    # It does NOT stop Python reading one that is already there, so
    # _clear_pycache removes every __pycache__ under the service
    # before the baseline and again after every mutant is restored.
    #
    # A fresh mtime is NOT what protects this, and an earlier version
    # of this comment said it was. CPython truncates the source mtime
    # to an INTEGER SECOND before comparing it, so a .pyc written for
    # the clean source stays valid for a same-size mutant written in
    # the same second. Measured on this machine at wh-notice-length-
    # guard.2.2: a child read 255 from cached bytecode while 256 was
    # on disk. tests/test_mutation_gate_notice_length.py holds that
    # reproduction and fails if the clear is removed.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", *test_files, "-q", "-rf",
         "-p", "no:cacheprovider", "-p", "no:randomly"],
        cwd=SERVICE_DIR, env=env, capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S,
    )


def _node_key(node_id):
    """The part of a pytest node id after the file path, without parameters.

    ``Class::method`` for a class-based test. The bare method name is not
    enough: this file puts same-shaped names in different classes.
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


def _coverage_errors(every_test):
    """Check that every test this bead added is mutated or excluded by name.

    Six ways the claim can stop being true, all reported here:

    1. A FEATURE_TESTS entry that no mutation breaks and NOT_MUTATED does
       not name. That is the over-claim itself.
    2. A FEATURE_TESTS entry that no longer exists.
    3. NOT_MUTATED names a test that no longer exists.
    4. A name is in NOT_MUTATED and in some mutation's expect list, so
       the file says both "covered" and "deliberately not covered".
    5. A reason cites another mutation by name. That claim is
       unverifiable where it is written and unchecked where it is read.
    6. A NOT_MUTATED entry whose reason is empty. An entry with no
       reason excludes a test from the claim while saying nothing
       about why, which is the over-claim wearing a name.
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
    mutation; only a full run does that. It exists to find a stale
    pattern in seconds after a fix rewrites the guard.
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

    # A name on the command line runs that mutation alone. The scope line
    # at the end says which subset ran, so a partial run cannot be read
    # as a full sweep.
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
            # The patterns above are written with LF. A file stored with
            # CRLF makes every multi-line pattern miss, which reads as a
            # survivor. Refuse rather than rewrite the file's endings.
            print(f"ERROR: {target.name} uses CRLF; patterns here are LF")
            return 1
        originals[target] = raw
    sources = {t: raw.decode("utf-8") for t, raw in originals.items()}

    errors, survivors, caught = [], [], []

    # Every expected test name must exist before the first mutation. A
    # name that no longer exists can never appear in the failed set, so a
    # genuine catch would be reported as a survivor.
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
                # prints, so a parser would find no FAILED lines and
                # report a survivor for a mutation that may have been
                # caught.
                errors.append(f"{name}: suite-timeout abort, no verdict")
                print(f"ERROR {name}: suite-timeout abort")
                continue

            failed = _failed_names(combined)
            expect = set(mutation["expect"])
            collateral = set(mutation.get("also_fails", {}))
            missing = sorted(expect - failed)
            # The expect list is an EXACT claim, not a lower bound. A
            # test that falls over on a shared precondition has not
            # established its own behaviour under the mutation, so every
            # failure must be declared, either as proof (expect) or as
            # collateral (also_fails, with the reason it proves nothing).
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
