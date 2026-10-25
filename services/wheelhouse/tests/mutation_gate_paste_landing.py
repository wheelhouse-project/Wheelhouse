"""Mutation gate for the Notepad paste check (wh-dictation-paste-loss-load).

WHY THIS GATE EXISTS. tests/test_ui/test_paste_landing.py and
tests/test_ui/test_verified_paste_landing.py guard the check that follows
a Ctrl+V into a RichEditD2DPT control (Windows 11 Notepad): the control's
text length must change; if it does not, verified_paste sends Ctrl+V once
more; a word that still does not arrive gets one WARNING and no retract
credit. A test can pass for the wrong reason, so each protected decision
is broken here at the level of the decision itself.

WHAT IS DEFENDED

  ui/paste_landing.py
      class-scope-check-removed          every class gets a check, so a
                                         browser caption length looks like
                                         a lost paste on every word
      focused-class-check-removed        a focused window of another class
                                         is queried
      selection-not-empty-allowed        a paste over a selection is
                                         checked; the length can stay the
                                         same after a good paste
      selection-query-failure-ignored    "do not know" reads as a caret
      selection-call-timeout-reads-as-caret
                                         a zero return of the EM_GETSEL
                                         call reads as the caret (0, 0)
      selection-error-reads-as-caret     an exception reads as the caret
      selection-query-drops-abortifhung  a hung target can stop the Input
                                         process through EM_GETSEL
      selection-start-end-swapped        the two DWORDs swap places
      selection-reads-the-start-twice    one DWORD receives both positions
      selection-message-id-changed       EM_GETSEL is another message
      selection-compares-the-start-to-zero
                                         a caret past the start of the
                                         text reads as a selection
      selection-start-is-16-bit          a caret past 65535 characters
                                         reads as a selection
      smto-abortifhung-dropped           a hung target can stop the Input
                                         process
      send-error-returns-zero            an error reads as length 0, not
                                         "no answer"
      wait-skips-the-first-read          a wait of 0 reads nothing
      wait-deadline-reversed             the deadline test is inverted
      wait-ignores-a-failed-query        "no answer" reads as a change
      wait-ignores-a-length-change       a changed length is not reported
      check-records-the-focused-control-as-its-window
                                         the second Ctrl+V's focus read
                                         starts from the control, not the
                                         window
  ui/clipboard_operations.py
      prepare-never-called               no check is ever prepared
      prepare-gets-no-window             the check reads no top-level window
      flutter-path-gets-a-paste-check    a Flutter paste gets a length check
      landing-wait-key-renamed           paste_landing_wait_ms is ignored
      first-wait-not-counted-from-ctrl-v the first wait ignores the time
                                         already spent since the Ctrl+V
      changed-length-sends-a-second-ctrl-v
      first-query-failure-sends-a-second-ctrl-v
      clipboard-change-does-not-stop-the-second-paste
      target-change-does-not-stop-the-second-paste
      focus-moved-does-not-stop-the-second-paste
      unreadable-focus-guard-removed
      focus-read-from-the-watched-control
      foreground-guard-removed           no target identity: a foreign
                                         foreground window gets the paste
      foreground-guard-runs-with-an-identity
      foreground-read-failure-reads-as-foreground
      foreground-compares-the-watched-control
      foreground-roots-not-compared      any foreground root gets the paste
      foreground-check-logs-a-second-warning
                                         a lost word logs two WARNING lines
      short-second-ctrl-v-skips-the-keyups
      short-second-ctrl-v-returns-one
      second-unchanged-returns-one
      second-unchanged-warning-silenced
      duplicate-rule-removed             the duplicate rule never returns 2
      duplicate-rule-counts-characters-not-units
                                         one emoji or one line break reads
                                         as two copies
      growth-ignores-line-breaks         a line break counts 1 unit, not 2
      growth-counts-code-points-not-utf16-units
                                         an emoji counts 1 unit, not 2
      credit-loop-ignores-copies-typed
  ui/ui_action_handler.py
      start-utterance-does-not-set-the-id

A NOTE ON ``expect``. It is the set of tests that MUST fail for the
mutation to count as caught. An EMPTY list marks a mutation that no
current test is expected to catch: it is then reported as caught only if
SOME test fails, and as SURVIVED otherwise. Nothing is hidden: every other
failure is printed on an ``also failed`` line.

A NOTE ON READING VERDICTS. Only pytest's short-summary section is parsed,
after a nonzero exit code. Each expected failure's reason must be an
assertion; any other reason (AttributeError, TypeError...) is reported as
an ERROR, not a catch.

crewcut: the mutant is written straight over the target with
``write_bytes`` (helpers shared with
mutation_gate_unverified_target_paste.py), not through an atomic replace.
A hard kill inside that write can leave part of a mutant in a tracked
file; the restore runs in a ``finally`` and the end of the sweep reads
every target and reports a file that does not hold its pre-run bytes.

Run from services/wheelhouse, NEVER while a test suite runs in the same
worktree:

    <interpreter> tests/mutation_gate_paste_landing.py [--check] [name ...]
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse the runner helpers of the unverified-target-paste gate. Importing
# runs no sweep: its entry point sits under ``if __name__ == "__main__"``.
import mutation_gate_unverified_target_paste as base  # noqa: E402

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

SERVICE_DIR = base.SERVICE_DIR

LANDING = SERVICE_DIR / "ui" / "paste_landing.py"
CLIPBOARD = SERVICE_DIR / "ui" / "clipboard_operations.py"
HANDLER = SERVICE_DIR / "ui" / "ui_action_handler.py"
APP = SERVICE_DIR / "app.py"
MAIN = SERVICE_DIR / "main.py"

LANDING_TESTS = "tests/test_ui/test_paste_landing.py"
VERIFIED_TESTS = "tests/test_ui/test_verified_paste_landing.py"
# wh-dictation-paste-loss-load.2: the copy-retry entries at the end of
# MUTATIONS are caught by these two files.
CLIPBOARD_TESTS = "tests/test_ui/test_clipboard_operations.py"
NEIGHBOUR_TESTS = "tests/test_ui/test_lost_word_neighbour_paths.py"
# wh-dictation-paste-loss-load.1.7: the shared budget around both attempts.
VANISHED_TESTS = "tests/test_ui/test_paste_target_window_vanished.py"
# wh-dictation-paste-loss-load.1.9: the replay budget, the delivery
# argument of send_request, and the forwarder's two arguments.
RETRY_HANDLER_TESTS = "tests/test_ui/test_retry_dictation_handler.py"
APP_TESTS = "tests/test_app.py"
FORWARD_TESTS = "tests/test_logic_retry_forwarding.py"
TEST_FILES = (
    LANDING_TESTS, VERIFIED_TESTS, CLIPBOARD_TESTS, NEIGHBOUR_TESTS,
    VANISHED_TESTS, RETRY_HANDLER_TESTS, APP_TESTS, FORWARD_TESTS,
)

# Both files run in a few seconds clean. No mutation here can wait on a
# real timer (the wait tests use a fake clock; the clipboard tests patch
# ``time``), so this exists to turn a hang into a reported error.
RUN_TIMEOUT_S = 300
base.RUN_TIMEOUT_S = RUN_TIMEOUT_S

MUTATIONS = [
    # -----------------------------------------------------------------
    # ui/paste_landing.py
    # -----------------------------------------------------------------
    {
        "name": "class-scope-check-removed",
        "target": LANDING,
        "old": "    if target_class_name not in PASTE_CHECK_CLASSES:\n",
        "new": "    if False:\n",
        "expect": ["test_prepare_skips_every_other_class"],
    },
    {
        "name": "focused-class-check-removed",
        "target": LANDING,
        "old": "    if _class_name(hwnd) not in PASTE_CHECK_CLASSES:\n",
        "new": "    if False:\n",
        "expect": [
            "test_prepare_skips_when_the_focused_window_is_another_class",
        ],
    },
    {
        "name": "selection-not-empty-allowed",
        "target": LANDING,
        "old": "    if not empty:\n",
        "new": "    if False:\n",
        "expect": ["test_prepare_skips_a_non_empty_selection"],
    },
    {
        # With ``empty`` None the next test, ``not empty``, is true, so the
        # mutant reports selection_not_empty: a wrong reason, an assertion.
        "name": "selection-query-failure-ignored",
        "target": LANDING,
        "old": "    if empty is None:\n",
        "new": "    if False:\n",
        "expect": [
            "test_prepare_skips_when_the_selection_query_gets_no_answer",
        ],
    },
    {
        # The two DWORDs keep their initial 0 when the call returns 0, so
        # the mutant reads a timeout as the caret (0, 0).
        "name": "selection-call-timeout-reads-as-caret",
        "target": LANDING,
        "old": "    if not answered:\n        return None\n",
        "new": "    if False:\n        return None\n",
        "expect": ["test_query_selection_returns_none_when_the_call_times_out"],
    },
    {
        "name": "selection-error-reads-as-caret",
        "target": LANDING,
        "old": (
            '        logger.debug("paste check: EM_GETSEL to %s failed: %s", '
            "hwnd, exc)\n"
            "        return None\n"
        ),
        "new": (
            '        logger.debug("paste check: EM_GETSEL to %s failed: %s", '
            "hwnd, exc)\n"
            "        return (0, 0)\n"
        ),
        "expect": ["test_query_selection_returns_none_on_an_error"],
    },
    {
        "name": "selection-query-drops-abortifhung",
        "target": LANDING,
        "old": "            SMTO_ABORTIFHUNG, QUERY_TIMEOUT_MS, ctypes.byref(result),\n",
        "new": "            0, QUERY_TIMEOUT_MS, ctypes.byref(result),\n",
        "expect": [
            "test_query_selection_sends_em_getsel_with_two_dword_pointers",
        ],
    },
    {
        "name": "selection-start-end-swapped",
        "target": LANDING,
        "old": (
            "            wt.HWND(hwnd), EM_GETSEL, ctypes.byref(start), "
            "ctypes.byref(end),\n"
        ),
        "new": (
            "            wt.HWND(hwnd), EM_GETSEL, ctypes.byref(end), "
            "ctypes.byref(start),\n"
        ),
        "expect": ["test_query_selection_returns_the_start_and_the_end"],
    },
    {
        "name": "selection-reads-the-start-twice",
        "target": LANDING,
        "old": (
            "            wt.HWND(hwnd), EM_GETSEL, ctypes.byref(start), "
            "ctypes.byref(end),\n"
        ),
        "new": (
            "            wt.HWND(hwnd), EM_GETSEL, ctypes.byref(start), "
            "ctypes.byref(start),\n"
        ),
        "expect": [
            "test_query_selection_returns_the_start_and_the_end",
            "test_query_selection_sends_em_getsel_with_two_dword_pointers",
        ],
    },
    {
        "name": "selection-message-id-changed",
        "target": LANDING,
        "old": "EM_GETSEL = 0x00B0\n",
        "new": "EM_GETSEL = 0x00B1\n",
        "expect": [
            "test_query_selection_sends_em_getsel_with_two_dword_pointers",
        ],
    },
    {
        "name": "selection-compares-the-start-to-zero",
        "target": LANDING,
        "old": "    return start == end\n",
        "new": "    return start == 0\n",
        "expect": ["test_prepare_checks_a_caret_past_65535_characters"],
    },
    {
        # A WORD keeps 16 bits: 70000 becomes 4464, which differs from the
        # end, so a caret past 65535 characters reads as a selection.
        "name": "selection-start-is-16-bit",
        "target": LANDING,
        "old": "    start = wt.DWORD(0)\n",
        "new": "    start = wt.WORD(0)\n",
        "expect": [
            "test_query_selection_answers_for_a_caret_past_65535_characters",
            "test_query_selection_sends_em_getsel_with_two_dword_pointers",
        ],
    },
    {
        "name": "check-records-the-focused-control-as-its-window",
        "target": LANDING,
        "old": 'root_hwnd=target_hwnd), "ready"\n',
        "new": 'root_hwnd=hwnd), "ready"\n',
        "expect": ["test_prepare_returns_the_focused_control_and_its_length"],
    },
    {
        "name": "growth-ignores-line-breaks",
        "target": LANDING,
        "old": '    return len(text.encode("utf-16-le")) // 2 + text.count("\\n")\n',
        "new": '    return len(text.encode("utf-16-le")) // 2\n',
        "expect": [
            "test_a_line_break_with_the_growth_of_one_copy_is_credited_once",
            "test_max_growth_of_one_paste_counts_utf16_units_and_line_breaks",
        ],
    },
    {
        "name": "growth-counts-code-points-not-utf16-units",
        "target": LANDING,
        "old": '    return len(text.encode("utf-16-le")) // 2 + text.count("\\n")\n',
        "new": '    return len(text) + text.count("\\n")\n',
        "expect": [
            "test_an_emoji_word_with_the_growth_of_one_copy_is_credited_once",
            "test_max_growth_of_one_paste_counts_utf16_units_and_line_breaks",
        ],
    },
    {
        "name": "smto-abortifhung-dropped",
        "target": LANDING,
        "old": "            hwnd, msg, 0, 0, SMTO_ABORTIFHUNG, QUERY_TIMEOUT_MS,\n",
        "new": "            hwnd, msg, 0, 0, 0, QUERY_TIMEOUT_MS,\n",
        "expect": ["test_send_with_timeout_uses_abort_if_hung"],
    },
    {
        "name": "send-error-returns-zero",
        "target": LANDING,
        "old": (
            '        logger.debug("paste check: message %#x to %s failed: %s", '
            "msg, hwnd, exc)\n"
            "        return None\n"
        ),
        "new": (
            '        logger.debug("paste check: message %#x to %s failed: %s", '
            "msg, hwnd, exc)\n"
            "        return 0\n"
        ),
        "expect": ["test_send_with_timeout_returns_none_on_an_error"],
    },
    {
        # The deadline test moves above the read, so a wait of 0 returns
        # "unchanged" without reading. ``length`` is not yet assigned at
        # the new return, so the mutant returns length_before.
        "name": "wait-skips-the-first-read",
        "target": LANDING,
        "old": (
            "        length = _text_length(hwnd)\n"
            "        elapsed = clock() - start\n"
        ),
        "new": (
            "        elapsed = clock() - start\n"
            "        if elapsed >= wait_s:\n"
            '            return LengthWait("unchanged", length_before, elapsed)\n'
            "        length = _text_length(hwnd)\n"
        ),
        "expect": ["test_wait_reads_at_least_once_with_a_zero_wait"],
    },
    {
        "name": "wait-deadline-reversed",
        "target": LANDING,
        "old": "        if elapsed >= wait_s:\n",
        "new": "        if elapsed <= wait_s:\n",
        "expect": [
            "test_wait_returns_changed_with_the_new_length",
            "test_wait_returns_unchanged_at_the_deadline",
        ],
    },
    {
        "name": "wait-ignores-a-failed-query",
        "target": LANDING,
        "old": "        if length is None:\n",
        "new": "        if False:\n",
        "expect": ["test_wait_stops_on_a_failed_query"],
    },
    {
        "name": "wait-ignores-a-length-change",
        "target": LANDING,
        "old": "        if length != length_before:\n",
        "new": "        if False:\n",
        "expect": ["test_wait_returns_changed_with_the_new_length"],
    },

    # -----------------------------------------------------------------
    # ui/clipboard_operations.py
    # -----------------------------------------------------------------
    {
        "name": "prepare-never-called",
        "target": CLIPBOARD,
        "old": (
            "            paste_check, check_reason = prepare_paste_check(\n"
            "                check_root_hwnd, target_class_name,\n"
            "            )\n"
        ),
        "new": '            paste_check, check_reason = None, "mutant"\n',
        "expect": [
            "test_a_landed_paste_sends_one_ctrl_v_and_credits_once",
            "test_an_unchanged_length_sends_ctrl_v_again",
        ],
    },
    {
        "name": "prepare-gets-no-window",
        "target": CLIPBOARD,
        "old": "                check_root_hwnd = hwnd\n",
        "new": "                check_root_hwnd = None\n",
        "expect": ["test_a_landed_paste_sends_one_ctrl_v_and_credits_once"],
    },
    {
        "name": "flutter-path-gets-a-paste-check",
        "target": CLIPBOARD,
        "old": (
            "        if use_flutter:\n"
            '            paste_check, check_reason = None, "flutter"\n'
        ),
        "new": (
            "        if False:\n"
            '            paste_check, check_reason = None, "flutter"\n'
        ),
        "expect": ["test_a_flutter_paste_gets_no_paste_check"],
    },
    {
        "name": "landing-wait-key-renamed",
        "target": CLIPBOARD,
        "old": 'timing_config.get("paste_landing_wait_ms", 500)',
        "new": 'timing_config.get("paste_landing_wait_ms_x", 500)',
        "expect": ["test_the_first_wait_uses_the_configured_landing_wait"],
    },
    {
        "name": "first-wait-not-counted-from-ctrl-v",
        "target": CLIPBOARD,
        "old": (
            "        wait_s = max(0.0, self.paste_landing_wait - "
            "(time.perf_counter() - t_sent))\n"
        ),
        "new": "        wait_s = self.paste_landing_wait\n",
        "expect": ["test_the_first_wait_counts_the_time_since_the_ctrl_v"],
    },
    {
        # ``pass`` replaces the early return so the changed status falls
        # through into the unchanged flow.
        "name": "changed-length-sends-a-second-ctrl-v",
        "target": CLIPBOARD,
        "old": (
            "                check.length_before, first.length, since_send_ms,\n"
            "            )\n"
            "            return 1\n"
        ),
        "new": (
            "                check.length_before, first.length, since_send_ms,\n"
            "            )\n"
            "            pass\n"
        ),
        "expect": ["test_a_landed_paste_sends_one_ctrl_v_and_credits_once"],
    },
    {
        "name": "first-query-failure-sends-a-second-ctrl-v",
        "target": CLIPBOARD,
        "old": '        if first.status == "query_failed":\n',
        "new": "        if False:\n",
        "expect": [
            "test_a_failed_length_query_after_the_paste_means_no_second_paste",
        ],
    },
    {
        "name": "clipboard-change-does-not-stop-the-second-paste",
        "target": CLIPBOARD,
        "old": "        if get_sequence_number() != sequence_before_send:\n",
        "new": "        if False:\n",
        "expect": ["test_a_changed_clipboard_means_no_second_paste"],
    },
    {
        "name": "target-change-does-not-stop-the-second-paste",
        "target": CLIPBOARD,
        "old": (
            "        if not self._captured_identity_is_current(target_identity):\n"
            "            logger.warning(\n"
            '                "paste check: word not typed'
        ),
        "new": (
            "        if False:\n"
            "            logger.warning(\n"
            '                "paste check: word not typed'
        ),
        "expect": ["test_a_changed_target_means_no_second_paste"],
    },
    {
        "name": "focus-moved-does-not-stop-the-second-paste",
        "target": CLIPBOARD,
        "old": "        if focused != check.hwnd:\n",
        "new": "        if False:\n",
        "expect": ["test_a_moved_focus_means_no_second_paste_and_no_credit"],
    },
    {
        # A focus that cannot be read (0) is also not the watched control, so
        # the next guard still stops the paste; the test pins the warning of
        # this guard, which says the focus could not be read.
        "name": "unreadable-focus-guard-removed",
        "target": CLIPBOARD,
        "old": "        if not focused:\n",
        "new": "        if False:\n",
        "expect": [
            "test_an_unreadable_focus_means_no_second_paste_and_no_credit",
        ],
    },
    {
        "name": "focus-read-from-the-watched-control",
        "target": CLIPBOARD,
        "old": "        focused = focused_control_hwnd(check.root_hwnd)\n",
        "new": "        focused = focused_control_hwnd(check.hwnd)\n",
        "expect": [
            "test_the_second_ctrl_v_reads_the_focused_control_of_the_checked_window",
        ],
    },
    {
        "name": "foreground-guard-removed",
        "target": CLIPBOARD,
        "old": (
            "        if target_identity is None and not "
            "self._check_window_is_foreground(check):\n"
        ),
        "new": "        if False:\n",
        "expect": [
            "test_without_a_target_identity_a_foreign_foreground_means_no_second_paste",
        ],
    },
    {
        "name": "foreground-guard-runs-with-an-identity",
        "target": CLIPBOARD,
        "old": (
            "        if target_identity is None and not "
            "self._check_window_is_foreground(check):\n"
        ),
        "new": (
            "        if not self._check_window_is_foreground(check):\n"
        ),
        "expect": [
            "test_with_a_target_identity_the_identity_check_stands_in_for_the_foreground_read",
        ],
    },
    {
        "name": "foreground-read-failure-reads-as-foreground",
        "target": CLIPBOARD,
        "old": (
            '            logger.debug("paste check: GetForegroundWindow failed: '
            '%s", exc)\n'
            "            return False\n"
        ),
        "new": (
            '            logger.debug("paste check: GetForegroundWindow failed: '
            '%s", exc)\n'
            "            return True\n"
        ),
        "expect": [
            "test_without_a_target_identity_an_unreadable_foreground_means_no_second_paste",
        ],
    },
    {
        "name": "foreground-compares-the-watched-control",
        "target": CLIPBOARD,
        "old": (
            "        check_root = normalize_hwnd_for_foreground_compare("
            "check.root_hwnd)\n"
        ),
        "new": (
            "        check_root = normalize_hwnd_for_foreground_compare("
            "check.hwnd)\n"
        ),
        "expect": [
            "test_without_a_target_identity_the_check_window_is_compared_to_the_foreground",
        ],
    },
    {
        # The check window and the foreground window are compared by root,
        # strictly. A mutant that accepts any foreground root lets the
        # second Ctrl+V reach another window.
        "name": "foreground-roots-not-compared",
        "target": CLIPBOARD,
        "old": (
            "        return check_root is not None and check_root == "
            "foreground_root\n"
        ),
        "new": "        return check_root is not None\n",
        "expect": [
            "test_without_a_target_identity_a_foreign_foreground_means_no_second_paste",
            "test_without_a_target_identity_a_foreground_with_no_root_means_no_second_paste",
        ],
    },
    {
        # wh-dictation-paste-loss-load.1.2: the comparison through
        # _foreground_matches_target logged its own WARNING, so a lost word
        # had two WARNING lines.
        "name": "foreground-check-logs-a-second-warning",
        "target": CLIPBOARD,
        "old": (
            "        return check_root is not None and check_root == "
            "foreground_root\n"
        ),
        "new": (
            "        return self._foreground_matches_target(\n"
            "            check.root_hwnd, foreground, phase=\"paste-check\",\n"
            "        )\n"
        ),
        "expect": [
            "test_without_a_target_identity_a_foreign_foreground_means_no_second_paste",
            "test_without_a_target_identity_a_foreground_with_no_root_means_no_second_paste",
        ],
    },
    {
        "name": "short-second-ctrl-v-skips-the-keyups",
        "target": CLIPBOARD,
        "old": (
            "            _send_modifier_keyups(('ctrl',))\n"
            "            logger.warning(\n"
            '                "paste check: second Ctrl+V SendInput'
        ),
        "new": (
            "            pass\n"
            "            logger.warning(\n"
            '                "paste check: second Ctrl+V SendInput'
        ),
        "expect": [
            "test_a_short_second_ctrl_v_releases_ctrl_and_gets_no_credit",
        ],
    },
    {
        # A short second delivery returns 1, so the word is credited and
        # the wait after the second Ctrl+V runs.
        "name": "short-second-ctrl-v-returns-one",
        "target": CLIPBOARD,
        "old": (
            '                "%d/%d); word not typed (utterance=%s, '
            'text_len=%d).",\n'
            "                accepted, expected, utterance, len(text),\n"
            "            )\n"
            "            return 0\n"
        ),
        "new": (
            '                "%d/%d); word not typed (utterance=%s, '
            'text_len=%d).",\n'
            "                accepted, expected, utterance, len(text),\n"
            "            )\n"
            "            return 1\n"
        ),
        "expect": [
            "test_a_short_second_ctrl_v_releases_ctrl_and_gets_no_credit",
        ],
    },
    {
        "name": "second-unchanged-returns-one",
        "target": CLIPBOARD,
        "old": (
            '                "paste check: word not typed after a second '
            'Ctrl+V "\n'
            '                "(utterance=%s, text_len=%d).", utterance, '
            "len(text),\n"
            "            )\n"
            "            return 0\n"
        ),
        "new": (
            '                "paste check: word not typed after a second '
            'Ctrl+V "\n'
            '                "(utterance=%s, text_len=%d).", utterance, '
            "len(text),\n"
            "            )\n"
            "            return 1\n"
        ),
        "expect": [
            "test_a_word_that_never_arrives_logs_one_warning_and_gets_no_credit",
        ],
    },
    {
        "name": "second-unchanged-warning-silenced",
        "target": CLIPBOARD,
        "old": (
            '        if second.status == "unchanged":\n'
            "            logger.warning(\n"
        ),
        "new": (
            '        if second.status == "unchanged":\n'
            "            logger.info(\n"
        ),
        "expect": [
            "test_a_word_that_never_arrives_logs_one_warning_and_gets_no_credit",
        ],
    },
    {
        "name": "duplicate-rule-removed",
        "target": CLIPBOARD,
        "old": (
            "        if second.length - check.length_before >= "
            "2 * max_growth_of_one_paste(text):\n"
        ),
        "new": "        if False:\n",
        "expect": ["test_a_late_first_paste_is_reported_and_credited_twice"],
    },
    {
        # The old rule: a growth of 2 x len(text) code points. One emoji
        # (2 UTF-16 units) and one "\n" (2 units) then read as two copies.
        "name": "duplicate-rule-counts-characters-not-units",
        "target": CLIPBOARD,
        "old": (
            "        if second.length - check.length_before >= "
            "2 * max_growth_of_one_paste(text):\n"
        ),
        "new": (
            "        if second.length - check.length_before >= 2 * len(text):\n"
        ),
        "expect": [
            "test_an_emoji_word_with_the_growth_of_one_copy_is_credited_once",
            "test_a_line_break_with_the_growth_of_one_copy_is_credited_once",
        ],
    },
    {
        "name": "credit-loop-ignores-copies-typed",
        "target": CLIPBOARD,
        "old": "        for _ in range(copies_typed):\n",
        "new": "        for _ in range(1):\n",
        "expect": [
            "test_a_late_first_paste_is_reported_and_credited_twice",
            "test_a_word_that_never_arrives_logs_one_warning_and_gets_no_credit",
        ],
    },

    # -----------------------------------------------------------------
    # ui/ui_action_handler.py
    # -----------------------------------------------------------------
    {
        "name": "start-utterance-does-not-set-the-id",
        "target": HANDLER,
        "old": "        self.clipboard.current_utterance_id = utterance_id\n",
        "new": "        pass\n",
        "expect": ["test_start_utterance_gives_the_clipboard_the_utterance_id"],
    },

    # -----------------------------------------------------------------
    # wh-dictation-paste-loss-load.2: the copy retry
    #
    # No mutant here can hang. The fake clock moves on every sleep, so a
    # loop whose deadline is loosened still ends: the deadline-ignored
    # mutant adds 60 s, which is 1200 pauses of 0.05 s on the fake clock.
    # -----------------------------------------------------------------
    {
        # The first failure ends the copy, as before this bead.
        "name": "copy-never-retried",
        "target": CLIPBOARD,
        "old": "                        not retry\n",
        "new": "                        True\n",
        "expect": [
            "TestSafeCopyRetry::"
            "test_a_copy_that_succeeds_inside_the_window_returns_true",
            "TestSafeCopyRetry::"
            "test_a_copy_that_keeps_failing_gives_up_at_about_three_seconds",
        ],
    },
    {
        "name": "copy-deadline-ignored",
        "target": CLIPBOARD,
        # The deadline moves, not the exit test: with the exit test
        # loosened, ``deadline - now`` goes negative, a negative pause
        # runs the fake clock backwards, and the loop never ends.
        "old": "        deadline = started + remaining\n",
        "new": "        deadline = started + remaining + 60.0\n",
        "expect": [
            "TestSafeCopyRetry::"
            "test_a_copy_that_keeps_failing_gives_up_at_about_three_seconds",
        ],
    },
    {
        "name": "copy-retries-every-exception",
        "target": CLIPBOARD,
        "old": "                        or not _is_open_clipboard_failure(e)\n",
        "new": "                        or False\n",
        "expect": [
            "TestSafeCopyRetry::"
            "test_another_pyperclip_windows_error_is_not_retried",
            "TestSafeCopyRetry::"
            "test_a_failure_that_is_not_open_clipboard_is_not_retried",
        ],
    },
    {
        "name": "give-up-line-drops-the-utterance",
        "target": CLIPBOARD,
        "old": '"clipboard copy outcome: failed (utterance=%s, "\n',
        "new": '"clipboard copy outcome: failed (utt=%s, "\n',
        "expect": ["TestSafeCopyRetry::test_the_give_up_line_names_the_utterance"],
    },
    {
        # Boss ruling 01:35: a copy outside a word (a selection sentinel)
        # retries for the whole window again, so two of them reach about 7 s.
        "name": "copy-outside-a-word-retries",
        "target": CLIPBOARD,
        "old": "        retry = in_budget\n",
        "new": "        pass\n",
        "expect": [
            "TestSafeCopyRetry::test_a_copy_outside_a_word_makes_one_attempt",
        ],
    },
    {
        # Verifier finding D1: a copy that succeeded late spends the
        # window, and the next copy of the word is skipped untried.
        "name": "late-success-skips-the-next-copy",
        "target": CLIPBOARD,
        "old": "            if self._copy_budget_gave_up:\n",
        "new": (
            "            if self._copy_budget_gave_up or "
            "self._copy_budget_spent >= COPY_RETRY_SECONDS:\n"
        ),
        "expect": [
            "TestSafeCopyRetry::"
            "test_a_late_success_leaves_the_next_copy_its_attempt",
            "TestSafeCopyRetry::"
            "test_a_late_success_then_a_held_clipboard_logs_the_give_up_line",
        ],
    },
    {
        # Each later copy of a word that gave up waits again.
        "name": "give-up-never-recorded",
        "target": CLIPBOARD,
        "old": "                                self._copy_budget_gave_up = True\n",
        "new": "                                pass\n",
        "expect": [
            "TestSafeCopyRetry::"
            "test_a_word_scope_shares_one_budget_between_attempts",
        ],
    },
    {
        # The next word inherits the give-up of the word before it.
        "name": "give-up-not-reset-for-the-next-word",
        "target": CLIPBOARD,
        "old": (
            "            self._copy_budget_charge = 0.0\n"
            "            self._copy_budget_gave_up = False\n"
        ),
        "new": "            self._copy_budget_charge = 0.0\n",
        "expect": [
            "TestSafeCopyRetry::test_the_budget_is_fresh_for_the_next_word",
        ],
    },
    {
        "name": "copy-failure-line-drops-the-utterance",
        "target": CLIPBOARD,
        "old": "                                now - started, self.current_utterance_id, e,\n",
        "new": "                                now - started, None, e,\n",
        "expect": [
            "TestSafeCopyRetry::"
            "test_the_copy_failure_line_names_the_utterance_inside_a_word",
        ],
    },
    {
        # After a copy that succeeded late, the next copy of the word gets
        # the whole window again instead of one attempt. (A copy that gave
        # up stops later copies through _copy_budget_gave_up instead.)
        "name": "word-budget-never-spent",
        "target": CLIPBOARD,
        "old": "                self._copy_budget_spent += _copy_monotonic() - started\n",
        "new": "                self._copy_budget_spent += 0.0\n",
        "expect": [
            "TestSafeCopyRetry::"
            "test_a_late_success_then_a_held_clipboard_logs_the_give_up_line",
        ],
    },
    {
        "name": "spent-budget-still-calls-pyperclip",
        "target": CLIPBOARD,
        "old": "            if self._copy_budget_gave_up:\n",
        "new": "            if False:\n",
        "expect": [
            "TestSafeCopyRetry::"
            "test_a_word_scope_shares_one_budget_between_attempts",
        ],
    },
    {
        "name": "word-budget-ignores-the-opening-thread",
        "target": CLIPBOARD,
        "old": "            and self._copy_budget_owner == threading.get_ident()\n",
        "new": "",
        "expect": [
            "TestSafeCopyRetry::"
            "test_another_thread_does_not_draw_on_the_word_budget",
        ],
    },
    {
        "name": "recopy-retries-too",
        "target": CLIPBOARD,
        "old": "                if not self._safe_copy(text, retry=False):\n",
        "new": "                if not self._safe_copy(text):\n",
        "expect": [
            "TestSafeCopyRetry::test_the_verification_recopy_does_not_retry",
        ],
    },
    {
        "name": "skipped-attempt-logs-a-second-error",
        "target": CLIPBOARD,
        "old": "            if self._last_copy_skipped:\n",
        "new": "            if False:\n",
        "expect": [
            "TestSafeCopyRetry::"
            "test_a_second_attempt_in_the_scope_adds_no_second_give_up_error",
        ],
    },
    {
        # The shadow attempt and the fallback each get the whole window,
        # and so does the retry after a closed target window (.1.7).
        "name": "dictated-word-has-no-shared-budget",
        "target": HANDLER,
        "old": (
            "            with self.clipboard.copy_retry_budget():\n"
            "                while True:\n"
        ),
        "new": (
            "            if True:\n"
            "                while True:\n"
        ),
        "expect": [
            "TestTheRetryKeepsTheWordsCopyBudget::"
            "test_both_attempts_spend_one_copy_budget",
            "TestOneWordWaitsAboutThreeSecondsForTheClipboard::"
            "test_the_word_waits_about_three_seconds_and_types_nothing",
            "TestOneWordWaitsAboutThreeSecondsForTheClipboard::"
            "test_the_budget_is_closed_when_the_insert_ends",
        ],
    },
    {
        "name": "raw-insert-has-no-shared-budget",
        "target": HANDLER,
        "old": (
            "            with self.clipboard.copy_retry_budget():\n"
            "                result = strategy.insert(text, context, request_id, options)\n"
        ),
        "new": (
            "            if True:\n"
            "                result = strategy.insert(text, context, request_id, options)\n"
        ),
        "expect": [
            "TestOneWordWaitsAboutThreeSecondsForTheClipboard::"
            "test_a_raw_insert_opens_the_same_budget",
        ],
    },
    # -----------------------------------------------------------------
    # wh-dictation-paste-loss-load.1.9: the Try-it-anyway replay
    # -----------------------------------------------------------------
    {
        "name": "replay-utterance-branch-has-no-budget",
        "target": HANDLER,
        "old": (
            "            if self.utterance_manager.is_in_utterance():\n"
            "                with self.clipboard.copy_retry_budget():\n"
        ),
        "new": (
            "            if self.utterance_manager.is_in_utterance():\n"
            "                if True:\n"
        ),
        "expect": [
            "TestReplayOpensTheCopyBudget::"
            "test_the_active_utterance_branch_inserts_inside_the_budget",
        ],
    },
    {
        "name": "replay-clipboard-context-branch-has-no-budget",
        "target": HANDLER,
        "old": (
            "                with clipboard_context(restore_delay=0.05):\n"
            "                    with self.clipboard.copy_retry_budget():\n"
        ),
        "new": (
            "                with clipboard_context(restore_delay=0.05):\n"
            "                    if True:\n"
        ),
        "expect": [
            "TestReplayOpensTheCopyBudget::"
            "test_the_clipboard_context_branch_inserts_inside_the_budget",
        ],
    },
    {
        # The default must leave every other caller's plain-action
        # deadline equal to its own timeout.
        "name": "delivery-ttl-default-shortens-a-plain-action",
        "target": APP,
        "old": "        delivery_ttl_s: Optional[float] = None,\n",
        "new": "        delivery_ttl_s: Optional[float] = 1.0,\n",
        "expect": [
            "TestDeliveryProtection::"
            "test_send_request_without_delivery_ttl_keeps_the_caller_deadline",
        ],
    },
    {
        # ... and every durable caller's deadline equal to its long rule.
        # (A default of 500.0 stretches every plain action too, and the
        # suite then waits on real expiry until its timeout; the gate has
        # no verdict for that, so the durable rule itself is moved.)
        "name": "durable-default-deadline-extended",
        "target": APP,
        "old": "            max(effective_timeout, _DURABLE_COMMAND_TTL_S)\n",
        "new": "            max(effective_timeout, _DURABLE_COMMAND_TTL_S) + 10.0\n",
        "expect": [
            "TestDeliveryProtection::"
            "test_durable_request_without_delivery_ttl_keeps_the_long_deadline",
        ],
    },
    {
        "name": "delivery-ttl-never-used",
        "target": APP,
        "old": (
            "        if delivery_ttl_s is not None:\n"
            "            if type(action) is str and action in "
            "_DURABLE_COMMAND_ACTIONS:\n"
        ),
        "new": (
            "        if False:\n"
            "            if type(action) is str and action in "
            "_DURABLE_COMMAND_ACTIONS:\n"
        ),
        "expect": [
            "TestDeliveryProtection::"
            "test_delivery_ttl_sets_the_deadline_of_a_plain_action",
        ],
    },
    {
        "name": "delivery-ttl-shortens-a-durable-action",
        "target": APP,
        "old": "                delivery_ttl = max(delivery_ttl, delivery_ttl_s)\n",
        "new": "                delivery_ttl = delivery_ttl_s\n",
        "expect": [
            "TestDeliveryProtection::"
            "test_delivery_ttl_does_not_shorten_a_durable_action",
        ],
    },
    {
        # The awaited window follows the caller's wait. A mutant that
        # stamps the delivery budget there moves the stall limit.
        "name": "delivery-ttl-moves-the-awaited-window",
        "target": APP,
        "old": (
            "        awaited_window = (\n"
            "            effective_timeout\n"
        ),
        "new": (
            "        awaited_window = (\n"
            "            delivery_ttl_s or effective_timeout\n"
        ),
        "expect": [
            "TestDeliveryProtection::"
            "test_delivery_ttl_does_not_move_the_awaited_window",
        ],
    },
    {
        "name": "delivery-ttl-validation-removed",
        "target": APP,
        "old": (
            "        if delivery_ttl_s is not None:\n"
            "            try:\n"
        ),
        "new": (
            "        if False:\n"
            "            try:\n"
        ),
        "expect": [
            "TestDeliveryProtection::"
            "test_invalid_delivery_ttl_is_refused_before_anything_is_queued",
        ],
    },
    {
        "name": "delivery-ttl-zero-allowed",
        "target": APP,
        "old": "                    and delivery_ttl_s > 0\n",
        "new": "                    and delivery_ttl_s > -1\n",
        "expect": [
            "TestDeliveryProtection::"
            "test_invalid_delivery_ttl_is_refused_before_anything_is_queued",
        ],
    },
    {
        "name": "forwarder-timeout-argument-dropped",
        "target": MAIN,
        "old": "                    timeout_s=10.0, delivery_ttl_s=5.0,\n",
        "new": "                    delivery_ttl_s=5.0,\n",
        "expect": [
            "TestRequestWaitAndDeliveryBudgets::test_the_wait_is_ten_seconds",
            "TestRequestWaitAndDeliveryBudgets::"
            "test_no_other_keyword_is_passed",
        ],
    },
    {
        "name": "forwarder-delivery-ttl-argument-dropped",
        "target": MAIN,
        "old": "                    timeout_s=10.0, delivery_ttl_s=5.0,\n",
        "new": "                    timeout_s=10.0,\n",
        "expect": [
            "TestRequestWaitAndDeliveryBudgets::"
            "test_the_delivery_budget_is_five_seconds",
            "TestRequestWaitAndDeliveryBudgets::"
            "test_no_other_keyword_is_passed",
        ],
    },
    {
        "name": "forwarder-delivery-ttl-as-long-as-the-wait",
        "target": MAIN,
        "old": "                    timeout_s=10.0, delivery_ttl_s=5.0,\n",
        "new": "                    timeout_s=10.0, delivery_ttl_s=10.0,\n",
        "expect": [
            "TestRequestWaitAndDeliveryBudgets::"
            "test_the_delivery_budget_is_five_seconds",
        ],
    },
]

TARGETS = sorted({LANDING, CLIPBOARD, HANDLER, APP, MAIN})


def _check(sources, endings):
    """Patterns match once, each mutant compiles, expected names exist."""
    stale, broken = [], []
    for mutation in MUTATIONS:
        _, error = base._apply(mutation, sources, endings)
        if error is None:
            continue
        (broken if "does not compile" in error else stale).append(error)
    for line in stale + broken:
        print(f"ERROR {line}")

    every_test = set()
    collect_problems = []
    for test_file in TEST_FILES:
        found, problem = base._collect(test_file)
        if problem:
            collect_problems.append(problem)
        every_test |= found
    missing_names = [
        f"{m['name']}: no such test {n}"
        for m in MUTATIONS for n in m["expect"] if n not in every_test
    ]
    for line in collect_problems + missing_names:
        print(f"ERROR {line}")
    print(f"checked {len(MUTATIONS)} patterns, {len(stale)} stale, "
          f"{len(broken)} that do not compile, "
          f"{len(missing_names) + len(collect_problems)} expected test "
          f"names missing ({len(every_test)} tests collected)")
    bad = stale or broken or missing_names or collect_problems
    return 1 if bad else 0


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

    originals = {t: t.read_bytes() for t in TARGETS}
    endings = base._endings(originals)
    sources = {t: raw.decode("utf-8") for t, raw in originals.items()}
    for target, ending in endings.items():
        print(f"{target.name}: {'CRLF' if ending == chr(13) + chr(10) else 'LF'}"
              " line endings; patterns translated to match")

    if check_only:
        return _check(sources, endings)

    errors, survivors, caught = [], [], []

    # Every expected test name must exist before the first mutation.
    every_test = set()
    for test_file in TEST_FILES:
        found, problem = base._collect(test_file)
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
          f"({len(every_test)} collected across {len(TEST_FILES)} files)")

    abort = base._clear_pycache_or_abort()
    if abort is not None:
        return abort

    # A suite that is already red reports every mutation as caught.
    baseline = base._run_pytest(TEST_FILES)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print(f"baseline green; running {len(mutations)} mutations")

    held = None
    try:
        for mutation in mutations:
            name = mutation["name"]
            working, error = base._apply(mutation, sources, endings)
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
                write_error = base._write_mutants(changed, written)
                if write_error is not None:
                    errors.append(f"{name}: {write_error}")
                    print(f"ERROR {name}: {write_error}")
                else:
                    result = base._run_pytest(TEST_FILES)
            except subprocess.TimeoutExpired:
                errors.append(f"{name}: mutant never terminated within "
                              f"{RUN_TIMEOUT_S}s")
                print(f"ERROR {name}: mutant never terminated")
                result = None
            finally:
                problems, stop = base._restore_written(
                    name, written, originals,
                )
                for problem in problems:
                    errors.append(problem)
                    print(f"ERROR {problem}")
                if stop is not None and held is None:
                    held = stop
                cache_error, stop = base._clear_pycache()
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
            if base.TIMEOUT_BANNER in combined:
                errors.append(f"{name}: suite-timeout-abort, no verdict")
                print(f"ERROR    {name}: suite-timeout-abort")
                continue

            # Exit code first: 0 means no test failed (a survivor) and
            # prints no short-summary section.
            if result.returncode == 0:
                records = {}
            elif result.returncode == 1:
                summary = base._summary_section(combined)
                if summary is None:
                    errors.append(f"{name}: pytest reported failures but "
                                  "printed no short-summary section")
                    print(f"ERROR    {name}: no short-summary section")
                    continue
                records = base._failure_records(summary)
            else:
                errors.append(f"{name}: pytest exited {result.returncode}, "
                              "which is neither a pass nor a test failure")
                print(f"ERROR    {name}: pytest exited {result.returncode}")
                continue
            failed = set(records)
            expect = set(mutation["expect"])

            if not expect:
                if not failed:
                    survivors.append(f"{name}: no test failed (no catcher "
                                     "is expected; none exists)")
                    print(f"SURVIVED {name}: no test failed")
                    continue
                expect = failed

            missing = sorted(expect - failed)
            if missing:
                survivors.append(
                    f"{name}: expected failures missing: {missing}"
                )
                print(f"SURVIVED {name}: {missing} did not fail")
                if failed:
                    print(f"         {name} did fail: {sorted(failed)}")
                continue

            unrelated = [
                problem for problem in
                (base._unrelated(records, n) for n in sorted(expect))
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
        for problem in base._final_mismatches(originals):
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
