"""Mutation gate for the clipboard save retry (wh-clipboard-save-lost-restore).

WHY THIS GATE EXISTS. tests/test_ui/test_clipboard_save_retry.py guards
the save of the clipboard text at utterance start while another program
holds the clipboard: the save retries "Error calling OpenClipboard" for
COPY_RETRY_SECONDS, a save that still fails logs one WARNING, and the save
time comes off the first word's copy budget (boss ruling 2026-10-20 20:36,
option 1). A test can pass for the wrong reason, so each protected
decision is broken here at the level of the decision itself.

WHAT IS DEFENDED

  ui/utterance_clipboard_manager.py
      save-retry-removed                 the first OpenClipboard failure
                                         ends the save, as before the fix
      save-retries-every-error           a GetClipboardData failure or a
                                         non-pyperclip error is retried
      save-window-doubled                the save retries for 6 s
      save-window-one-second             the save gives up before a 2.6 s
                                         hold ends
      save-pause-half-a-second           the pause between attempts is not
                                         the copy retry pause
      save-sleeps-before-every-attempt   a save with no hold waits
      failure-warning-wording-lost       the WARNING does not say that the
                                         clipboard text was not saved
      failure-warning-at-debug           the failure line is not a WARNING
      save-seconds-not-measured          last_save_seconds stays 0.0
      chained-start-keeps-old-seconds    a chained start reports the
                                         previous save's seconds

  ui/clipboard_operations.py
      charge-ignored                     a new budget starts at 0.0 spent
      charge-not-cleared                 the charge applies to every
                                         later budget
      charge-setter-drops-value          charge_next_copy_budget stores 0.0

  ui/ui_action_handler.py
      handler-charge-dropped             start_utterance charges nothing
      handler-charges-before-save        the charge reads the save time
                                         before the manager measures it
      handler-end-keeps-charge           end_utterance leaves the charge, so
                                         a later budget starts with the
                                         save time spent

The runner is the one in tests/mutation_gate_paste_landing.py, which
reuses the helpers of tests/mutation_gate_unverified_target_paste.py: a
pattern must match exactly once, every mutant must compile, every
expected test name must exist, the baseline must be green, and verdicts
come from pytest's short-summary section only.

Run from services/wheelhouse of the worktree:

    <interpreter> tests/mutation_gate_clipboard_save_retry.py [--check] [name ...]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse the paste-landing gate's runner. Importing runs no sweep: its entry
# point sits under ``if __name__ == "__main__"``. Its main() reads the
# module globals MUTATIONS, TARGETS and TEST_FILES at call time, so this
# gate replaces those three before it calls main().
import mutation_gate_paste_landing as runner  # noqa: E402

SERVICE_DIR = runner.SERVICE_DIR

MANAGER = SERVICE_DIR / "ui" / "utterance_clipboard_manager.py"
CLIPBOARD = SERVICE_DIR / "ui" / "clipboard_operations.py"
HANDLER = SERVICE_DIR / "ui" / "ui_action_handler.py"

SAVE_TESTS = "tests/test_ui/test_clipboard_save_retry.py"
MANAGER_TESTS = "tests/test_ui/test_utterance_clipboard_manager.py"
CLIPBOARD_TESTS = "tests/test_ui/test_clipboard_operations.py"
TEST_FILES = (SAVE_TESTS, MANAGER_TESTS, CLIPBOARD_TESTS)

MUTATIONS = [
    # -----------------------------------------------------------------
    # ui/utterance_clipboard_manager.py
    # -----------------------------------------------------------------
    {
        "name": "save-retry-removed",
        "target": MANAGER,
        "old": "if _is_open_clipboard_failure(e) and now < deadline:",
        "new": "if False:",
        "expect": [
            "TestSaveRetry::test_a_2_6_second_hold_still_saves_and_restores_the_user_text",
            "TestSaveRetry::test_a_hold_that_never_ends_stops_retrying_at_three_seconds",
            "TestSaveRetry::test_the_pause_between_attempts_is_the_copy_retry_pause",
            "TestSaveFailureWarning::test_a_save_that_succeeds_after_a_hold_logs_no_warning",
        ],
    },
    {
        "name": "save-retries-every-error",
        "target": MANAGER,
        "old": "if _is_open_clipboard_failure(e) and now < deadline:",
        "new": "if now < deadline:",
        "expect": [
            "TestSaveRetry::test_another_error_is_not_retried",
            "TestSaveRetry::test_a_non_windows_exception_is_not_retried",
        ],
    },
    {
        "name": "save-window-doubled",
        "target": MANAGER,
        "old": "deadline = started + COPY_RETRY_SECONDS",
        "new": "deadline = started + 2 * COPY_RETRY_SECONDS",
        "expect": [
            "TestSaveRetry::test_a_hold_that_never_ends_stops_retrying_at_three_seconds",
        ],
    },
    {
        "name": "save-window-one-second",
        "target": MANAGER,
        "old": "deadline = started + COPY_RETRY_SECONDS",
        "new": "deadline = started + 1.0",
        "expect": [
            "TestSaveRetry::test_a_2_6_second_hold_still_saves_and_restores_the_user_text",
        ],
    },
    {
        "name": "save-pause-half-a-second",
        "target": MANAGER,
        "old": "_save_sleep(min(_COPY_RETRY_PAUSE, deadline - now))",
        "new": "_save_sleep(0.5)",
        "expect": [
            "TestSaveRetry::test_the_pause_between_attempts_is_the_copy_retry_pause",
        ],
    },
    {
        "name": "save-sleeps-before-every-attempt",
        "target": MANAGER,
        "old": "text = pyperclip.paste()",
        "new": "_save_sleep(_COPY_RETRY_PAUSE); text = pyperclip.paste()",
        "expect": [
            "TestNoHold::test_no_hold_makes_one_paste_call_and_never_sleeps",
            "TestSaveRetry::test_another_error_is_not_retried",
        ],
    },
    {
        "name": "failure-warning-wording-lost",
        "target": MANAGER,
        "old": 'f"({e}); the clipboard text was not saved and will "',
        "new": 'f"({e}); the save failed and will "',
        "expect": [
            "TestSaveFailureWarning::test_a_save_that_fails_after_its_retries_logs_one_warning",
        ],
    },
    {
        "name": "failure-warning-at-debug",
        "target": MANAGER,
        "old": (
            "                    logger.warning(\n"
            '                        f"[UTT-{utterance_id}] Could not read '
            'the clipboard "\n'
        ),
        "new": (
            "                    logger.debug(\n"
            '                        f"[UTT-{utterance_id}] Could not read '
            'the clipboard "\n'
        ),
        "expect": [
            "TestSaveFailureWarning::test_a_save_that_fails_after_its_retries_logs_one_warning",
        ],
    },
    {
        "name": "save-seconds-not-measured",
        "target": MANAGER,
        "old": "self.last_save_seconds = _save_clock() - started",
        "new": "self.last_save_seconds = 0.0",
        "expect": [
            "TestSaveRetry::test_a_2_6_second_hold_still_saves_and_restores_the_user_text",
            "TestNoHold::test_a_chained_start_does_not_save_and_reports_zero_seconds",
        ],
    },
    {
        "name": "chained-start-keeps-old-seconds",
        "target": MANAGER,
        "old": (
            "                self._saved_text = chained_text\n"
            "                self.last_save_seconds = 0.0\n"
        ),
        "new": "                self._saved_text = chained_text\n",
        "expect": [
            "TestNoHold::test_a_chained_start_does_not_save_and_reports_zero_seconds",
        ],
    },
    # -----------------------------------------------------------------
    # ui/clipboard_operations.py
    # -----------------------------------------------------------------
    {
        "name": "charge-ignored",
        "target": CLIPBOARD,
        "old": "self._copy_budget_spent = self._copy_budget_charge",
        "new": "self._copy_budget_spent = 0.0",
        "expect": [
            "TestChargedCopyBudget::test_a_fully_charged_budget_still_makes_one_attempt",
            "TestChargedCopyBudget::test_a_budget_charged_one_second_retries_for_about_two_more",
            "TestChargedCopyBudget::test_the_charge_applies_to_one_budget_only",
        ],
    },
    {
        "name": "charge-not-cleared",
        "target": CLIPBOARD,
        "old": (
            "            self._copy_budget_charge = 0.0\n"
            "            self._copy_budget_gave_up = False\n"
        ),
        "new": "            self._copy_budget_gave_up = False\n",
        "expect": ["TestChargedCopyBudget::test_the_charge_applies_to_one_budget_only"],
    },
    {
        "name": "charge-setter-drops-value",
        "target": CLIPBOARD,
        "old": "self._copy_budget_charge = max(0.0, seconds)",
        "new": "self._copy_budget_charge = 0.0",
        "expect": [
            "TestChargedCopyBudget::test_a_fully_charged_budget_still_makes_one_attempt",
            "TestChargedCopyBudget::test_a_budget_charged_one_second_retries_for_about_two_more",
            "TestChargedCopyBudget::test_the_charge_applies_to_one_budget_only",
        ],
    },
    # -----------------------------------------------------------------
    # ui/ui_action_handler.py
    # -----------------------------------------------------------------
    {
        "name": "handler-charge-dropped",
        "target": HANDLER,
        "old": (
            "        self.clipboard.charge_next_copy_budget(\n"
            "            self.utterance_manager.last_save_seconds\n"
            "        )\n"
        ),
        "new": "        pass\n",
        "expect": [
            "TestHandlerWiring::test_start_utterance_charges_the_copy_budget_with_the_save_time",
        ],
    },
    {
        # A move, not a copy: the charge runs before the manager call.
        "name": "handler-charges-before-save",
        "target": HANDLER,
        "old": (
            "        self.utterance_manager.start_utterance(utterance_id)\n"
            "        # wh-clipboard-save-lost-restore: the save and the first "
            "word's\n"
            "        # copy share one retry window (boss ruling 2026-10-20 "
            "20:36).\n"
            "        self.clipboard.charge_next_copy_budget(\n"
            "            self.utterance_manager.last_save_seconds\n"
            "        )\n"
        ),
        "new": (
            "        self.clipboard.charge_next_copy_budget(\n"
            "            self.utterance_manager.last_save_seconds\n"
            "        )\n"
            "        self.utterance_manager.start_utterance(utterance_id)\n"
        ),
        "expect": [
            "TestHandlerWiring::test_start_utterance_charges_the_copy_budget_with_the_save_time",
        ],
    },
    {
        "name": "handler-end-keeps-charge",
        "target": HANDLER,
        "old": "        self.clipboard.charge_next_copy_budget(0.0)\n",
        "new": "        pass\n",
        "expect": [
            "TestHandlerWiring::test_end_utterance_clears_a_charge_that_no_budget_consumed",
        ],
    },
]

runner.MUTATIONS = MUTATIONS
runner.TARGETS = sorted({MANAGER, CLIPBOARD, HANDLER})
runner.TEST_FILES = TEST_FILES


if __name__ == "__main__":
    sys.exit(runner.main(sys.argv))
