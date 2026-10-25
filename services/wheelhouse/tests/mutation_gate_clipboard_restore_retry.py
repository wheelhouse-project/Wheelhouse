"""Mutation gate for the clipboard restore retry (wh-clipboard-restore-fail-lost).

WHY THIS GATE EXISTS. tests/test_ui/test_clipboard_restore_retry.py guards
the deferred restore at utterance end while another program holds the
clipboard: the restore tries "Error calling OpenClipboard" again for
COPY_RETRY_SECONDS, checks the pending and the sequence number again
before each attempt, never makes a new utterance wait for the retry, and
logs one WARNING when every attempt fails. A test can pass for the wrong
reason, so each protected decision is broken here at the level of the
decision itself.

WHAT IS DEFENDED

  ui/utterance_clipboard_manager.py
      restore-retry-removed               the first OpenClipboard failure
                                          ends the restore, as before the
                                          fix
      restore-retries-every-error         a SetClipboardData failure is
                                          retried
      restore-window-doubled              the restore retries for 6 s
      restore-window-one-second           the restore gives up before a
                                          2 s hold ends
      restore-pause-half-a-second         the pause is not the copy retry
                                          pause
      restore-sleeps-under-the-lock       the pause holds the manager lock,
                                          so a new utterance waits
      retry-skips-the-sequence-check      an attempt after the first does
                                          not compare the sequence number
      retry-ignores-a-sequence-read-failure
                                          a failed sequence read between
                                          attempts does not stop the
                                          restore
      held-attempt-stays-cancelled        a held attempt cancels the
                                          pending, so no retry follows
      held-attempt-not-counted            restore_attempts stays 0
      retrying-pending-not-chained        a new utterance after the chain
                                          gap runs its own restore attempt
                                          instead of taking the saved text
      expired-held-not-chained            an expired restore that finds the
                                          clipboard held lets the new
                                          utterance save the dictated text
      chained-restore-not-owed            a chained utterance that never
                                          writes the clipboard schedules
                                          no restore (finding .1.1)
      clean-chain-seq-not-kept            the chain branch does not keep
                                          the checked sequence number
      expired-held-seq-not-kept           the expired-held branch does not
                                          keep the checked sequence number
      owed-restore-uses-the-end-seq       the owed restore takes the
                                          sequence number at the end, so a
                                          copy during the chained utterance
                                          is written over
      chained-seq-replaces-the-dirty-fallback
                                          a dirty utterance with no tracked
                                          write compares with the chained
                                          sequence number and drops the
                                          restore
      failure-warning-miscounts           the WARNING names one attempt too
                                          many
      failure-warning-drops-the-time      the WARNING names 0.0 s
      failure-warning-at-debug            the failure line is not a WARNING

The runner is the one in tests/mutation_gate_paste_landing.py, which
reuses the helpers of tests/mutation_gate_unverified_target_paste.py: a
pattern must match exactly once, every mutant must compile, every
expected test name must exist, the baseline must be green, and verdicts
come from pytest's short-summary section only.

Two mutations are left out on purpose. Setting ``restore_started`` on
every attempt makes the window never end, and a fake clock then never
stops the loop, so that mutant hangs instead of failing. The two stale
checks in _do_restore (the pending is still current) and in
_execute_restore_decision_locked (the pending is not cancelled) guard
the same event: start_utterance sets both in one locked step, so
removing either one alone changes nothing that a test can observe.
The two resets of ``_chained_seq`` (in start_utterance and in
_reset_utterance_state_locked) are also left out: each one alone is
redundant, because start_utterance always sets the value and only an
utterance in progress reads it.

Run from services/wheelhouse of the worktree:

    <interpreter> tests/mutation_gate_clipboard_restore_retry.py [--check] [name ...]
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

RESTORE_TESTS = "tests/test_ui/test_clipboard_restore_retry.py"
SAVE_TESTS = "tests/test_ui/test_clipboard_save_retry.py"
MANAGER_TESTS = "tests/test_ui/test_utterance_clipboard_manager.py"
TEST_FILES = (RESTORE_TESTS, SAVE_TESTS, MANAGER_TESTS)

_HOLD_2S = "TestRestoreRetry::test_a_2_second_hold_at_restore_still_restores_the_user_text"
_NEVER_ENDS = "TestRestoreRetry::test_a_hold_that_never_ends_stops_retrying_at_three_seconds"
_PAUSE = "TestRestoreRetry::test_the_pause_between_attempts_is_the_copy_retry_pause"
_OTHER_ERROR = "TestRestoreRetry::test_another_error_is_not_retried"
_EXTERNAL_WRITE = "TestRetrySequenceCheck::test_an_external_write_between_two_attempts_stops_the_restore"
_SEQ_READ_FAILS = "TestRetrySequenceCheck::test_a_sequence_read_failure_between_attempts_stops_the_restore"
_WARNING = "TestRetryFailureLog::test_the_warning_names_the_utterance_the_attempts_and_the_time"
_NEXT_INSIDE_GAP = "TestNewUtteranceDuringRetry::test_the_next_utterance_inside_the_chain_gap_does_not_wait_and_keeps_the_user_text"
_NEXT_AFTER_GAP = "TestNewUtteranceDuringRetry::test_the_next_utterance_after_the_chain_gap_does_not_wait_and_keeps_the_user_text"
_NEXT_FRESH = "TestNewUtteranceDuringRetry::test_an_external_write_during_the_retry_gives_the_next_utterance_a_fresh_save"
_EXPIRED_HELD = "TestExpiredRestoreHeld::test_the_next_utterance_keeps_the_user_text_as_its_baseline"
_NEXT_NO_WRITE = "TestNewUtteranceDuringRetry::test_a_next_utterance_that_never_writes_the_clipboard_still_gets_the_user_text_back"
_EXPIRED_NO_WRITE = "TestExpiredRestoreHeld::test_a_next_utterance_that_never_writes_the_clipboard_still_gets_the_user_text_back"
_GAP_NO_WRITE = "TestChainedUtteranceWithoutClipboardWrite::test_a_chained_utterance_inside_the_chain_gap_still_gets_the_user_text_back"
_CHAIN_EXTERNAL = "TestChainedUtteranceWithoutClipboardWrite::test_an_external_write_during_the_chained_utterance_stops_the_owed_restore"
_CHAIN_FALLBACK = "TestChainedUtteranceWithoutClipboardWrite::test_a_chained_utterance_that_writes_without_a_sequence_number_keeps_the_fallback"

MUTATIONS = [
    {
        "name": "restore-retry-removed",
        "target": MANAGER,
        "old": "            if _is_open_clipboard_failure(e):\n",
        "new": "            if False:\n",
        "expect": [
            _HOLD_2S, _NEVER_ENDS, _PAUSE,
            _NEXT_INSIDE_GAP, _NEXT_AFTER_GAP, _NEXT_FRESH,
            _EXPIRED_HELD,
        ],
    },
    {
        "name": "restore-retries-every-error",
        "target": MANAGER,
        "old": "            if _is_open_clipboard_failure(e):\n",
        "new": "            if True:\n",
        "expect": [_OTHER_ERROR],
    },
    {
        "name": "restore-window-doubled",
        "target": MANAGER,
        "old": "deadline = expected_pending.restore_started + COPY_RETRY_SECONDS",
        "new": "deadline = expected_pending.restore_started + 2 * COPY_RETRY_SECONDS",
        "expect": [_NEVER_ENDS],
    },
    {
        "name": "restore-window-one-second",
        "target": MANAGER,
        "old": "deadline = expected_pending.restore_started + COPY_RETRY_SECONDS",
        "new": "deadline = expected_pending.restore_started + 1.0",
        "expect": [_HOLD_2S],
    },
    {
        "name": "restore-pause-half-a-second",
        "target": MANAGER,
        "old": "pause = min(_COPY_RETRY_PAUSE, deadline - now)",
        "new": "pause = min(0.5, deadline - now)",
        "expect": [_PAUSE],
    },
    {
        "name": "restore-sleeps-under-the-lock",
        "target": MANAGER,
        "old": (
            "                pause = min(_COPY_RETRY_PAUSE, deadline - now)\n"
            "            _save_sleep(pause)\n"
        ),
        "new": (
            "                pause = min(_COPY_RETRY_PAUSE, deadline - now)\n"
            "                _save_sleep(pause)\n"
        ),
        "expect": [_NEXT_INSIDE_GAP, _NEXT_AFTER_GAP, _NEXT_FRESH],
    },
    {
        "name": "retry-skips-the-sequence-check",
        "target": MANAGER,
        "old": "        if current_seq != pending.clipboard_seq_at_paste:\n            logger.info(\n                f\"[UTT-{pending.utterance_id}] External clipboard write detected \"",
        "new": "        if current_seq != pending.clipboard_seq_at_paste and pending.restore_attempts == 0:\n            logger.info(\n                f\"[UTT-{pending.utterance_id}] External clipboard write detected \"",
        "expect": [_EXTERNAL_WRITE],
    },
    {
        "name": "retry-ignores-a-sequence-read-failure",
        "target": MANAGER,
        "old": (
            "                f\"at restore decision: {e}; refusing to restore (fail safe)\"\n"
            "            )\n"
            "            return None\n"
        ),
        "new": (
            "                f\"at restore decision: {e}; refusing to restore (fail safe)\"\n"
            "            )\n"
            "            current_seq = pending.clipboard_seq_at_paste\n"
        ),
        "expect": [_SEQ_READ_FAILS],
    },
    {
        "name": "held-attempt-stays-cancelled",
        "target": MANAGER,
        "old": "                pending.cancelled = False\n",
        "new": "                pass\n",
        # One pause still happens before the next attempt stops on the
        # cancelled flag, so the pause test cannot catch this mutation.
        "expect": [_HOLD_2S, _NEVER_ENDS],
    },
    {
        "name": "held-attempt-not-counted",
        "target": MANAGER,
        "old": "pending.restore_attempts += 1",
        "new": "pending.restore_attempts += 0",
        "expect": [_NEXT_AFTER_GAP, _WARNING],
    },
    {
        "name": "retrying-pending-not-chained",
        "target": MANAGER,
        "old": "or pending.restore_attempts > 0):",
        "new": "or False):",
        "expect": [_NEXT_AFTER_GAP],
    },
    {
        "name": "expired-held-not-chained",
        "target": MANAGER,
        "old": "                    if held is not None:\n",
        "new": "                    if False:\n",
        "expect": [_EXPIRED_HELD],
    },
    # wh-clipboard-restore-fail-lost.1.1: a chained utterance that never
    # writes the clipboard still owes the restore.
    {
        "name": "chained-restore-not-owed",
        "target": MANAGER,
        "old": "restore_owed = self._clipboard_dirty or self._chained_seq is not None",
        "new": "restore_owed = self._clipboard_dirty",
        "expect": [_NEXT_NO_WRITE, _EXPIRED_NO_WRITE, _GAP_NO_WRITE],
    },
    {
        "name": "clean-chain-seq-not-kept",
        "target": MANAGER,
        "old": (
            "                        chained_seq = pending.clipboard_seq_at_paste\n"
            "                        logger.debug(\n"
        ),
        "new": (
            "                        chained_seq = None\n"
            "                        logger.debug(\n"
        ),
        "expect": [_NEXT_NO_WRITE, _GAP_NO_WRITE],
    },
    {
        "name": "expired-held-seq-not-kept",
        "target": MANAGER,
        "old": (
            "                        chained_seq = pending.clipboard_seq_at_paste\n"
            "                        logger.info(\n"
        ),
        "new": (
            "                        chained_seq = None\n"
            "                        logger.info(\n"
        ),
        "expect": [_EXPIRED_NO_WRITE],
    },
    {
        "name": "owed-restore-uses-the-end-seq",
        "target": MANAGER,
        "old": "            expected_seq = self._chained_seq\n",
        "new": "            expected_seq = None\n",
        "expect": [_CHAIN_EXTERNAL],
    },
    {
        "name": "chained-seq-replaces-the-dirty-fallback",
        "target": MANAGER,
        "old": "if expected_seq is None and not self._clipboard_dirty:",
        "new": "if expected_seq is None:",
        "expect": [_CHAIN_FALLBACK],
    },
    {
        "name": "failure-warning-miscounts",
        "target": MANAGER,
        "old": "f\"{expected_pending.restore_attempts} attempts in \"",
        "new": "f\"{expected_pending.restore_attempts + 1} attempts in \"",
        "expect": [_WARNING],
    },
    {
        "name": "failure-warning-drops-the-time",
        "target": MANAGER,
        "old": "f\"{spent:.1f} s ({held}); another program held the \"",
        "new": "f\"{spent * 0:.1f} s ({held}); another program held the \"",
        "expect": [_WARNING],
    },
    {
        "name": "failure-warning-at-debug",
        "target": MANAGER,
        "old": (
            "                    logger.warning(\n"
            "                        f\"[UTT-{expected_pending.utterance_id}] Could not \"\n"
        ),
        "new": (
            "                    logger.debug(\n"
            "                        f\"[UTT-{expected_pending.utterance_id}] Could not \"\n"
        ),
        "expect": [_WARNING],
    },
]

runner.MUTATIONS = MUTATIONS
runner.TARGETS = [MANAGER]
runner.TEST_FILES = TEST_FILES


if __name__ == "__main__":
    sys.exit(runner.main(sys.argv))
