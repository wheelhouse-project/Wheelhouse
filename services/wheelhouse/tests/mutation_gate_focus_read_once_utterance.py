"""Mutation gate for wh-focus-read-once-utterance (one focus read for each utterance).

Usage (from services/wheelhouse):

    .venv/Scripts/python.exe tests/mutation_gate_focus_read_once_utterance.py [--check] [--only name,name]

``--only`` runs the named mutations and refuses an unknown name. ``--check``
verifies that every pattern matches exactly once in the current source and
that every mutant compiles, without running a test.

The gate covers the shipped lines of commit fbd812f4 in
``ui/ui_action_handler.py`` and ``input_proc.py``. Every mutation runs the
one test file ``tests/test_ui/test_focus_read_once_utterance.py``. The
groups, in the order of the table below:

  use-*        the five parts of the condition under which a word uses the
               stored read (_choose_focus_read and _stored_read_is_current)
  store-*      the parts of the store condition (_focus_read_is_storable)
  keep-*       the store-or-discard step (_keep_focus_read)
  d1-* to d7-* the discard events D1 to D7 (note_input_command is D6)
  discard-*    the discard itself (_discard_focus_read)
  c1-*, c4-*   the boss ruling's conditions C1 and C4
  route-*      the reuse of the C4 router answer at the router site
  log-*        the FOCUS_READ line, its level, and its cause values
  ip-*         the note_input_command call in input_process_main

A mutation may carry ``edits``, a list of (old, new) pairs applied together,
in place of one ``old``/``new`` pair. Each old text must match exactly once.

Pattern-not-found, an ambiguous pattern, a mutation that does not compile, a
per-mutation timeout, a suite-timeout abort, a skipped catcher and a missing
expected test name are reported as errors, never as a verdict. A restore that
cannot be proved stops the sweep outright.

EXPECTED SURVIVORS. A mutation with the key "survives" names no catcher and
carries the reason it survives. It passes only when its whole selection
stays green; any failed test prints "CAUGHT EXPECTED SURVIVOR" and the gate
exits 1, because that test shows the reason is no longer true.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
HANDLER_SRC = SERVICE / "ui" / "ui_action_handler.py"
INPUT_SRC = SERVICE / "input_proc.py"

TESTS = "tests/test_ui/test_focus_read_once_utterance.py"
COLLECT_FILES = [TESTS]
SELECTION = [TESTS]
# -v prints one line per test, which is what names a SKIPPED catcher; -rf
# prints the short-summary FAILED lines failed_names() reads.
PYTEST_ARGS = ["-p", "no:randomly", "-v", "-rf"]
PER_MUTATION_TIMEOUT_S = 300

# --- catcher names -------------------------------------------------------
THREE_WORDS = "test_three_words_in_one_window_make_one_focus_read"
NO_LONGER_CURRENT = (
    "test_a_window_that_is_no_longer_current_discards_the_stored_read"
)
ROOT_BESIDE_IS_CURRENT = "test_the_foreground_root_check_stands_beside_is_current"
FAILING_CHECK = "test_a_failing_foreground_check_reads_again"
NEXT_START = "test_a_word_after_the_next_start_utterance_gets_a_new_read"
FINAL_FLUSH = "test_the_final_letter_flush_uses_the_stored_read_then_end_discards"
FLUTTER_NOT_STORED = "test_a_flutter_read_is_not_stored"
RETRY_REPLACES = "test_the_retry_read_replaces_the_stored_read"
FAILED_RETRY = "test_a_failed_retry_read_discards_the_stored_read"
FLUTTER_RETRY = "test_a_flutter_retry_read_discards_the_stored_read"
WRAP_NOT_DICTATION = "test_wrap_or_insert_is_not_a_dictation_command"
NEW_THEN_STORED = "test_a_new_read_then_a_stored_read"
RETRIED_TWO_LINES = "test_a_retried_word_has_two_lines"
_CAUSE = "test_the_cause_of_a_new_read"
CAUSE_WINDOW = f"{_CAUSE}[window-window_changed]"
CAUSE_FAILED = f"{_CAUSE}[failed-failed_read]"
CAUSE_COMMAND = f"{_CAUSE}[command-command]"
CAUSE_USER_INPUT = f"{_CAUSE}[user_input-user_input]"
CAUSE_EMPTY_IDENTITY = f"{_CAUSE}[empty_identity-failed_read]"
CAUSE_NO_CONTROL = f"{_CAUSE}[no_control-failed_read]"
CAUSE_FLUTTER = f"{_CAUSE}[flutter-flutter]"
DIRECT_CALL = "test_a_direct_call_is_not_dictation"
OUTSIDE_UTTERANCE = "test_outside_an_utterance_every_word_reads"
READ_ONCE = "test_a_discard_during_the_check_does_not_break_the_word"
DEAD_STORED = "test_one_new_read_and_a_second_evaluation"
NEW_READ_CLIPBOARD_ONLY = (
    "test_a_new_read_routed_to_clipboard_only_is_not_read_again"
)
OTHER_COMMAND = "test_another_command_makes_the_next_word_read_again"
CLICK_OR_KEY = "test_a_click_or_key_makes_the_next_word_read_again"
NOTED_BEFORE = "test_every_dispatched_command_is_noted_before_it_runs"
FAILING_NOTE = "test_a_failing_note_does_not_end_the_loop"
FAILING_NOTE_WARNING = "test_a_failing_note_is_logged_at_warning_not_error"
LINE_IS_INFO = "test_the_focus_read_line_is_info"
_NOT_DICTATION = "test_an_insert_that_is_not_dictation_stores_nothing"
NOT_DICTATION_ALL = [
    f"{_NOT_DICTATION}[None]",
    f"{_NOT_DICTATION}[wrap_or_insert]",
    f"{_NOT_DICTATION}[verbatim_insert_text]",
]
REJECT_NOT_AGAIN = "test_a_reject_of_the_stored_read_is_not_evaluated_again"

# --- source under mutation, quoted exactly -------------------------------
_USE_FLAG = (
    "        if not self._command_reuses_focus_read:\n"
    '            cause = "not_dictation"\n'
)
_USE_UTTERANCE = (
    "        elif not self.utterance_manager.is_in_utterance():\n"
    '            cause = "no_utterance"\n'
)
_USE_STORED_NONE = (
    "        elif stored is None:\n"
    "            cause = self._focus_read_discard_cause\n"
)
_CHECK_IDENTITY_READ = (
    "        identity = stored.target_identity\n"
    "        try:\n"
    "            return bool(\n"
)
_CHECK_BOTH = (
    "                current_foreground_root() == identity.foreground_root\n"
    "                and identity.is_current()\n"
)
_CHECK_EXCEPT = (
    "                and identity.is_current()\n"
    "            )\n"
    "        except Exception:\n"
    "            return False\n"
)
_WINDOW_CHANGED = (
    '            self._discard_focus_read("window_changed")\n'
    '            cause = "window_changed"\n'
)
_STORE_FAILED = (
    "            not context.focus_read_failed\n"
    "            and identity is not None\n"
)
_STORE_NONE = (
    "            and identity is not None\n"
    "            and identity != TargetIdentity()\n"
)
_STORE_EMPTY = (
    "            and identity != TargetIdentity()\n"
    "            and context.focused_control\n"
)
_STORE_CONTROL = (
    "            and context.focused_control\n"
    "            and not context.is_flutter\n"
)
_STORE_FLUTTER = (
    "            and context.focused_control\n"
    "            and not context.is_flutter\n"
    "        )\n"
)
_KEEP_FLAG = (
    "        if not self._command_reuses_focus_read:\n"
    "            return\n"
)
_KEEP_UTTERANCE = (
    "        if not self.utterance_manager.is_in_utterance():\n"
    "            return\n"
    "        if self._focus_read_is_storable(context):\n"
)
_KEEP_FLUTTER = (
    "        elif context.is_flutter:\n"
    '            self._discard_focus_read("flutter")\n'
)
_KEEP_FAILED = (
    "        else:\n"
    '            self._discard_focus_read("failed_read")\n'
)
_D1 = (
    '        self._discard_focus_read("first_word")\n'
    "\n"
    "        # Reset retraction tracking"
)
_D2 = (
    '        self._discard_focus_read("first_word")\n'
    "\n"
    "        # Skip clipboard restore"
)
_FLUSH_HEAD = (
    "        if not self._flush_letter_buffer():\n"
    "            if self._last_insert_was_rejected:\n"
    "                logger.warning(\n"
    '                    "end_utterance: final letter-buffer flush was "\n'
)
_RETRY_KEEP = (
    '                        read_cause = "retry"\n'
    "                        self._keep_focus_read(context)\n"
)
_FIRST_PASS = (
    "                    if attempt == 0:\n"
    "                        context, read_cause, read_ms = (\n"
)
_NOTE_REUSE = (
    "            self._command_reuses_focus_read = True\n"
    "            return\n"
)
_NOTE_OTHER = (
    "        self._command_reuses_focus_read = False\n"
    '        self._discard_focus_read("command")\n'
)
_REUSE_LIST = (
    '    "intelligent_insert_text",\n'
    '    "end_utterance",\n'
    "})\n"
)
_D7 = '        self._discard_focus_read("user_input")\n'
_DISCARD_BODY = (
    "        self._focus_read_discard_cause = cause\n"
    "        self._utterance_focus_read = None\n"
)
_RETURN_STORED = "            return stored, None, 0\n"
_C4_CHECK = "                        if routed is self.router.clipboard_only:\n"
_C4_ONLY_STORED = "                    if read_cause is None:\n"
_C4_DISCARD = (
    '                            self._discard_focus_read("stored_control_rejected")\n'
)
_C4_NEW_READ = (
    "                            context, read_ms = self._take_focus_read()\n"
    '                            read_cause = "stored_control_rejected"\n'
)
_C4_KEEP = (
    '                            read_cause = "stored_control_rejected"\n'
    "                            self._keep_focus_read(context)\n"
)
_C4_ROUTED_NONE = (
    "                            self._keep_focus_read(context)\n"
    "                            routed = None\n"
)
_ROUTE_REUSE = (
    "                    if routed is not None:\n"
    "                        strategy = routed\n"
)
_LOG_CALL = "                    self._log_focus_read(read_cause, read_ms)\n"
_LOG_STORED = '            logger.info("FOCUS_READ source=stored")\n'
_LOG_NEW = (
    "            logger.info(\n"
    '                "FOCUS_READ source=new cause=%s duration_ms=%d",\n'
)
_IP_NOTE_CALL = "                ui_handler.note_input_command(action)\n"
_IP_NOTE_BLOCK = (
    "            try:\n"
    "                ui_handler.note_input_command(action)\n"
    "            except Exception as e:\n"
    "                logger.warning(\n"
    '                    "note_input_command failed for %s: %s",\n'
    "                    action, _safe_error_text(e),\n"
    "                )\n"
)
_IP_NOTE_WARNING = (
    "                logger.warning(\n"
    '                    "note_input_command failed for %s: %s",\n'
)

# An expected-survivor reason shared by the two discards that sit right
# before a _keep_focus_read call.
_KEEP_FOLLOWS = (
    "Equivalent mutant: _keep_focus_read runs right after the new read on "
    "this path with the reuse flag True and the handler in an utterance "
    "(both are why the stored read was in use), so it either replaces the "
    "stored read or discards it with its own cause. The discard matters "
    "only if capture_context raises, and capture_context catches the "
    "GetFocusedControl failure itself (ui/context.py)"
)

MUTATIONS = [
    # --- use condition ---------------------------------------------------
    {
        "name": "use-flag-ignored",
        "file": HANDLER_SRC,
        "old": _USE_FLAG,
        "new": "        if False:\n" '            cause = "not_dictation"\n',
        "expect": [DIRECT_CALL],
    },
    {
        "name": "use-utterance-ignored",
        "file": HANDLER_SRC,
        "old": _USE_UTTERANCE,
        "new": "        elif False:\n" '            cause = "no_utterance"\n',
        "expect": [OUTSIDE_UTTERANCE],
    },
    {
        # The None test is dropped. The identity read moves inside the try
        # so the mutant answers False for None instead of crashing on an
        # AttributeError, and the word reaches the cause assertion.
        "name": "use-stored-none-check-dropped",
        "file": HANDLER_SRC,
        "edits": [
            (
                _USE_STORED_NONE,
                "        elif False:\n"
                "            cause = self._focus_read_discard_cause\n",
            ),
            (
                _CHECK_IDENTITY_READ,
                "        try:\n"
                "            identity = stored.target_identity\n"
                "            return bool(\n",
            ),
        ],
        "expect": [NEW_THEN_STORED, CAUSE_FAILED, CAUSE_COMMAND, CAUSE_USER_INPUT],
    },
    {
        "name": "use-foreground-root-dropped",
        "file": HANDLER_SRC,
        "old": _CHECK_BOTH,
        "new": "                identity.is_current()\n",
        "expect": [ROOT_BESIDE_IS_CURRENT],
    },
    {
        "name": "use-is-current-dropped",
        "file": HANDLER_SRC,
        "old": _CHECK_BOTH,
        "new": (
            "                current_foreground_root() == identity.foreground_root\n"
        ),
        "expect": [NO_LONGER_CURRENT],
    },
    {
        "name": "use-check-doubt-answers-true",
        "file": HANDLER_SRC,
        "old": _CHECK_EXCEPT,
        "new": _CHECK_EXCEPT.replace("return False", "return True"),
        "expect": [FAILING_CHECK],
    },
    {
        "name": "use-window-changed-discard-dropped",
        "file": HANDLER_SRC,
        "old": _WINDOW_CHANGED,
        "new": "            pass\n" '            cause = "window_changed"\n',
        "expect": [],
        "survives": _KEEP_FOLLOWS,
    },
    {
        "name": "log-window-changed-cause",
        "file": HANDLER_SRC,
        "old": _WINDOW_CHANGED,
        "new": (
            '            self._discard_focus_read("window_changed")\n'
            '            cause = "first_word"\n'
        ),
        "expect": [CAUSE_WINDOW],
    },
    {
        "name": "log-stored-none-cause-fixed",
        "file": HANDLER_SRC,
        "old": _USE_STORED_NONE,
        "new": "        elif stored is None:\n" '            cause = "first_word"\n',
        "expect": [
            CAUSE_FAILED, CAUSE_COMMAND, CAUSE_USER_INPUT,
            CAUSE_EMPTY_IDENTITY, CAUSE_NO_CONTROL, CAUSE_FLUTTER,
        ],
    },
    # --- store condition -------------------------------------------------
    {
        "name": "store-failed-flag-dropped",
        "file": HANDLER_SRC,
        "old": _STORE_FAILED,
        "new": "            identity is not None\n",
        "expect": [],
        "survives": (
            "Equivalent mutant: capture_context sets focused_control to None "
            "in the same except arm that sets focus_read_failed "
            "(ui/context.py), so the focused_control part of the condition "
            "refuses every failed read"
        ),
    },
    {
        "name": "store-none-identity-dropped",
        "file": HANDLER_SRC,
        "old": _STORE_NONE,
        "new": "            and identity != TargetIdentity()\n",
        "expect": [],
        "survives": (
            "Equivalent mutant: capture_context always supplies "
            "target_identity, an empty TargetIdentity on every failure "
            "(UIContext docstring and every branch of capture_context in "
            "ui/context.py), and _choose_focus_read stores only what "
            "capture_context returned"
        ),
    },
    {
        "name": "store-empty-identity-dropped",
        "file": HANDLER_SRC,
        "old": _STORE_EMPTY,
        "new": "            and context.focused_control\n",
        "expect": [CAUSE_EMPTY_IDENTITY],
    },
    {
        "name": "store-no-control-dropped",
        "file": HANDLER_SRC,
        "old": _STORE_CONTROL,
        "new": "            and not context.is_flutter\n",
        "expect": [CAUSE_NO_CONTROL],
    },
    {
        "name": "store-flutter-dropped",
        "file": HANDLER_SRC,
        "old": _STORE_FLUTTER,
        "new": "            and context.focused_control\n" "        )\n",
        "expect": [FLUTTER_NOT_STORED, CAUSE_FLUTTER],
    },
    # --- keep (store or discard) -----------------------------------------
    {
        "name": "keep-flag-check-dropped",
        "file": HANDLER_SRC,
        "old": _KEEP_FLAG,
        "new": "        pass\n",
        "expect": NOT_DICTATION_ALL,
    },
    {
        "name": "keep-utterance-check-dropped",
        "file": HANDLER_SRC,
        "old": _KEEP_UTTERANCE,
        "new": "        if self._focus_read_is_storable(context):\n",
        "expect": [OUTSIDE_UTTERANCE],
    },
    {
        "name": "keep-flutter-discard-dropped",
        "file": HANDLER_SRC,
        "old": _KEEP_FLUTTER,
        "new": "        elif context.is_flutter:\n" "            pass\n",
        "expect": [FLUTTER_RETRY, CAUSE_FLUTTER],
    },
    {
        "name": "log-flutter-cause",
        "file": HANDLER_SRC,
        "old": _KEEP_FLUTTER,
        "new": (
            "        elif context.is_flutter:\n"
            '            self._discard_focus_read("failed_read")\n'
        ),
        "expect": [CAUSE_FLUTTER],
    },
    {
        "name": "keep-failed-discard-dropped",
        "file": HANDLER_SRC,
        "old": _KEEP_FAILED,
        "new": "        else:\n" "            pass\n",
        "expect": [FAILED_RETRY],
    },
    {
        "name": "log-failed-read-cause",
        "file": HANDLER_SRC,
        "old": _KEEP_FAILED,
        "new": "        else:\n" '            self._discard_focus_read("flutter")\n',
        "expect": [CAUSE_FAILED, CAUSE_EMPTY_IDENTITY, CAUSE_NO_CONTROL],
    },
    # --- discard events ----------------------------------------------------
    {
        # The command loop notes start_utterance before it runs, and that
        # note discards the record too (D6), so the record half of D1 is
        # covered twice on the loop's path. What D1 alone decides there is
        # the cause: the first word reads with cause first_word, not
        # command.
        "name": "d1-start-discard-dropped",
        "file": HANDLER_SRC,
        "old": _D1,
        "new": "        pass\n\n        # Reset retraction tracking",
        "expect": [NEW_THEN_STORED],
    },
    {
        "name": "d2-end-discard-dropped",
        "file": HANDLER_SRC,
        "old": _D2,
        "new": "        pass\n\n        # Skip clipboard restore",
        "expect": [FINAL_FLUSH],
    },
    {
        # The D2 discard MOVES ahead of the final letter flush, so the
        # flush makes its own read.
        "name": "d2-end-discards-before-flush",
        "file": HANDLER_SRC,
        "edits": [
            (_D2, "        pass\n\n        # Skip clipboard restore"),
            (
                _FLUSH_HEAD,
                '        self._discard_focus_read("first_word")\n' + _FLUSH_HEAD,
            ),
        ],
        "expect": [FINAL_FLUSH],
    },
    {
        "name": "d4-retry-keep-dropped",
        "file": HANDLER_SRC,
        "old": _RETRY_KEEP,
        "new": (
            '                        read_cause = "retry"\n'
            "                        pass\n"
        ),
        "expect": [RETRY_REPLACES, FAILED_RETRY, FLUTTER_RETRY],
    },
    {
        "name": "log-retry-cause",
        "file": HANDLER_SRC,
        "old": _RETRY_KEEP,
        "new": (
            '                        read_cause = "first_word"\n'
            "                        self._keep_focus_read(context)\n"
        ),
        "expect": [RETRIED_TWO_LINES],
    },
    {
        "name": "d4-retry-uses-stored",
        "file": HANDLER_SRC,
        "old": _FIRST_PASS,
        "new": (
            "                    if True:\n"
            "                        context, read_cause, read_ms = (\n"
        ),
        "expect": [RETRY_REPLACES, RETRIED_TWO_LINES],
    },
    {
        "name": "d6-reuse-command-sets-false",
        "file": HANDLER_SRC,
        "old": _NOTE_REUSE,
        "new": (
            "            self._command_reuses_focus_read = False\n"
            "            return\n"
        ),
        "expect": [THREE_WORDS],
    },
    {
        "name": "d6-other-command-keeps-flag",
        "file": HANDLER_SRC,
        "old": _NOTE_OTHER,
        "new": '        self._discard_focus_read("command")\n',
        "expect": [WRAP_NOT_DICTATION],
    },
    {
        "name": "d6-other-command-no-discard",
        "file": HANDLER_SRC,
        "old": _NOTE_OTHER,
        "new": "        self._command_reuses_focus_read = False\n" "        pass\n",
        "expect": [OTHER_COMMAND, CAUSE_COMMAND, WRAP_NOT_DICTATION],
    },
    {
        "name": "log-command-cause",
        "file": HANDLER_SRC,
        "old": _NOTE_OTHER,
        "new": (
            "        self._command_reuses_focus_read = False\n"
            '        self._discard_focus_read("user_input")\n'
        ),
        "expect": [CAUSE_COMMAND],
    },
    {
        "name": "d6-reuse-list-without-end-utterance",
        "file": HANDLER_SRC,
        "old": _REUSE_LIST,
        "new": '    "intelligent_insert_text",\n' "})\n",
        "expect": [FINAL_FLUSH],
    },
    {
        "name": "d6-reuse-list-without-word",
        "file": HANDLER_SRC,
        "old": _REUSE_LIST,
        "new": '    "end_utterance",\n' "})\n",
        "expect": [THREE_WORDS],
    },
    {
        "name": "d7-invalidate-discard-dropped",
        "file": HANDLER_SRC,
        "old": _D7,
        "new": "        pass\n",
        "expect": [CLICK_OR_KEY, CAUSE_USER_INPUT],
    },
    {
        "name": "log-user-input-cause",
        "file": HANDLER_SRC,
        "old": _D7,
        "new": '        self._discard_focus_read("command")\n',
        "expect": [CAUSE_USER_INPUT],
    },
    {
        "name": "discard-keeps-record",
        "file": HANDLER_SRC,
        "old": _DISCARD_BODY,
        "new": "        self._focus_read_discard_cause = cause\n",
        "expect": [NEXT_START, OTHER_COMMAND, CLICK_OR_KEY],
    },
    {
        "name": "discard-keeps-old-cause",
        "file": HANDLER_SRC,
        "old": _DISCARD_BODY,
        "new": "        self._utterance_focus_read = None\n",
        "expect": [CAUSE_FAILED, CAUSE_COMMAND, CAUSE_USER_INPUT, CAUSE_FLUTTER],
    },
    # --- C1 and C4 -----------------------------------------------------------
    {
        # The shared attribute is read a second time after the check.
        "name": "c1-reads-attribute-again",
        "file": HANDLER_SRC,
        "old": _RETURN_STORED,
        "new": "            return self._utterance_focus_read, None, 0\n",
        "expect": [READ_ONCE],
    },
    {
        "name": "c4-check-dropped",
        "file": HANDLER_SRC,
        "old": _C4_CHECK,
        "new": "                        if False:\n",
        "expect": [DEAD_STORED],
    },
    {
        "name": "c4-acts-on-reject",
        "file": HANDLER_SRC,
        "old": _C4_CHECK,
        "new": (
            "                        if routed is self.router.clipboard_only "
            "or routed is self.router.rejected:\n"
        ),
        "expect": [REJECT_NOT_AGAIN],
    },
    {
        "name": "c4-acts-on-new-read",
        "file": HANDLER_SRC,
        "old": _C4_ONLY_STORED,
        "new": "                    if True:\n",
        "expect": [NEW_READ_CLIPBOARD_ONLY],
    },
    {
        "name": "c4-discard-dropped",
        "file": HANDLER_SRC,
        "old": _C4_DISCARD,
        "new": "                            pass\n",
        "expect": [],
        "survives": _KEEP_FOLLOWS,
    },
    {
        "name": "c4-no-new-read",
        "file": HANDLER_SRC,
        "old": _C4_NEW_READ,
        "new": '                            read_cause = "stored_control_rejected"\n',
        "expect": [DEAD_STORED],
    },
    {
        "name": "c4-new-read-not-kept",
        "file": HANDLER_SRC,
        "old": _C4_KEEP,
        "new": (
            '                            read_cause = "stored_control_rejected"\n'
            "                            pass\n"
        ),
        "expect": [DEAD_STORED],
    },
    {
        # The second router call is skipped: the ClipboardOnlyStrategy
        # answer for the dead stored control is used.
        "name": "c4-second-router-call-skipped",
        "file": HANDLER_SRC,
        "old": _C4_ROUTED_NONE,
        "new": "                            self._keep_focus_read(context)\n",
        "expect": [DEAD_STORED],
    },
    {
        "name": "log-stored-control-rejected-cause",
        "file": HANDLER_SRC,
        "old": _C4_KEEP,
        "new": (
            '                            read_cause = "window_changed"\n'
            "                            self._keep_focus_read(context)\n"
        ),
        "expect": [DEAD_STORED],
    },
    {
        # The router site ignores the C4 answer and routes the stored read
        # a second time.
        "name": "route-c4-answer-ignored",
        "file": HANDLER_SRC,
        "old": _ROUTE_REUSE,
        "new": "                    if False:\n" "                        strategy = routed\n",
        "expect": [REJECT_NOT_AGAIN],
    },
    # --- the log line -----------------------------------------------------
    {
        "name": "log-call-dropped",
        "file": HANDLER_SRC,
        "old": _LOG_CALL,
        "new": "                    pass\n",
        "expect": [NEW_THEN_STORED, LINE_IS_INFO],
    },
    {
        "name": "log-stored-at-error",
        "file": HANDLER_SRC,
        "old": _LOG_STORED,
        "new": '            logger.error("FOCUS_READ source=stored")\n',
        "expect": [LINE_IS_INFO],
    },
    {
        "name": "log-new-at-error",
        "file": HANDLER_SRC,
        "old": _LOG_NEW,
        "new": _LOG_NEW.replace("logger.info(", "logger.error("),
        "expect": [LINE_IS_INFO],
    },
    {
        "name": "log-stored-says-new",
        "file": HANDLER_SRC,
        "old": _LOG_STORED,
        "new": '            logger.info("FOCUS_READ source=new")\n',
        "expect": [NEW_THEN_STORED, RETRIED_TWO_LINES],
    },
    # --- input_proc -------------------------------------------------------
    {
        "name": "ip-note-call-dropped",
        "file": INPUT_SRC,
        "old": _IP_NOTE_CALL,
        "new": "                pass\n",
        "expect": [NOTED_BEFORE, FAILING_NOTE],
    },
    {
        "name": "ip-note-uncontained",
        "file": INPUT_SRC,
        "old": _IP_NOTE_BLOCK,
        "new": "            ui_handler.note_input_command(action)\n",
        "expect": [FAILING_NOTE, FAILING_NOTE_WARNING],
    },
    {
        "name": "ip-note-failure-at-error",
        "file": INPUT_SRC,
        "old": _IP_NOTE_WARNING,
        "new": _IP_NOTE_WARNING.replace("logger.warning(", "logger.error("),
        "expect": [FAILING_NOTE_WARNING],
    },
]


def _clear_bytecode():
    for directory in (
        SERVICE / "__pycache__",
        SERVICE / "ui" / "__pycache__",
        SERVICE / "tests" / "__pycache__",
        SERVICE / "tests" / "test_ui" / "__pycache__",
    ):
        shutil.rmtree(directory, ignore_errors=True)


def _pytest(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=PER_MUTATION_TIMEOUT_S,
        # COLUMNS keeps pytest from cutting the " - <reason>" suffix off a
        # short-summary line wider than 80 columns.
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "COLUMNS": "1000"},
    )


def _node_name(node_part: str) -> str:
    """``name[param-id]`` from the part after the last ``::``.

    The parameter list stays: different cases catch different mutations.
    Every id in the selection is hyphenated, with no space.
    """
    return node_part.split(" - ", 1)[0].strip().split(" ")[0]


def collect_names():
    """Real test names, so a renamed test cannot read as a survivor."""
    out = _pytest(*COLLECT_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(_node_name(line.split("::", 1)[1].split("::")[-1]))
    return names


def _short_summary(output):
    marker = "short test summary info"
    idx = output.find(marker)
    if idx < 0:
        return None
    return output[idx:]


def failed_lines(output):
    """``{name: full FAILED line}`` from the short-summary section only."""
    section = _short_summary(output)
    found = {}
    if section is None:
        return found
    for line in section.splitlines():
        if line.startswith("FAILED ") and "::" in line:
            node = line[len("FAILED "):].split(" - ", 1)[0]
            found[_node_name(node.split("::")[-1])] = line
    return found


def skipped_names(output):
    """Test names on -v per-test lines that read ``...::name SKIPPED (...)``."""
    names = set()
    for line in output.splitlines():
        if "::" in line and " SKIPPED" in line:
            names.add(line.split("::")[-1].strip().split(" ")[0])
    return names


def _newline_of(raw: bytes) -> str:
    return "\r\n" if b"\r\n" in raw else "\n"


def _edits(mut: dict):
    if "edits" in mut:
        return list(mut["edits"])
    return [(mut["old"], mut["new"])]


def _build_mutant(text: str, newline: str, mut: dict):
    """Return (mutant_text, error). Exactly one match per edit, and a
    compiling result. Every old text is counted in the ORIGINAL source, so
    one edit cannot create or remove the match of another."""
    edits = [
        (old.replace("\n", newline), new.replace("\n", newline))
        for old, new in _edits(mut)
    ]
    for old, _new in edits:
        count = text.count(old)
        if count != 1:
            return None, f"{mut['name']}: pattern matched {count} times, expected 1"
    mutated = text
    for old, new in edits:
        if mutated.count(old) != 1:
            return None, f"{mut['name']}: edits overlap"
        mutated = mutated.replace(old, new, 1)
    try:
        compile(mutated, str(mut["file"]), "exec")
    except SyntaxError as exc:
        return None, f"{mut['name']}: mutated source does not compile: {exc}"
    return mutated, None


def _originals():
    out = {}
    for mut in MUTATIONS:
        path = mut["file"]
        if path not in out:
            out[path] = path.read_bytes()
    return out


def _declaration_problems(mutations):
    """Name each mutation whose expected-survivor declaration has no reason."""
    problems = []
    for mut in mutations:
        if "survives" not in mut:
            continue
        reason = mut["survives"]
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"{mut['name']}: an expected survivor needs a reason")
    return problems


def _declared(mutations):
    return sum(1 for mut in mutations if "survives" in mut)


def _duplicate_names():
    seen, dupes = set(), []
    for mut in MUTATIONS:
        if mut["name"] in seen:
            dupes.append(mut["name"])
        seen.add(mut["name"])
    return dupes


def check_only():
    originals = _originals()
    stale, non_compiling = [], []
    invalid = _declaration_problems(MUTATIONS)
    invalid += [f"{name}: duplicate mutation name" for name in _duplicate_names()]
    for problem in invalid:
        print("ERROR", problem)
    for mut in MUTATIONS:
        raw = originals[mut["file"]]
        _mutant, error = _build_mutant(
            raw.decode("utf-8"), _newline_of(raw), mut
        )
        if error is None:
            continue
        if "does not compile" in error:
            non_compiling.append(error)
        else:
            stale.append(error)
        print("ERROR", error)
    line = (
        f"checked {len(MUTATIONS)} patterns, {len(stale)} stale, "
        f"{len(non_compiling)} that do not compile"
    )
    if _declared(MUTATIONS):
        line += f", {_declared(MUTATIONS)} expected survivors"
    print(line)
    return 1 if stale or non_compiling or invalid else 0


def _write_with_retry(path: Path, data: bytes) -> None:
    """Write ``data``; retry a bounded number of times on a held file.

    On Windows the language server, an antivirus scan, or the previous
    pytest process can hold the target for a moment ([WinError 5]).
    """
    for attempt in range(5):
        try:
            path.write_bytes(data)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.5)


def _restore(path: Path, original: bytes) -> bool:
    """Put the source back, and say whether the original bytes are back.

    A False is a hard condition for the caller: the mutant is sitting in a
    live source file. A second Ctrl+C landing inside the restore is held,
    retried once, and re-raised only after the bytecode caches are cleared.
    """
    held = None
    restored = False
    for _ in range(2):
        try:
            _write_with_retry(path, original)
            restored = True
            break
        except KeyboardInterrupt as exc:
            held = exc
        except OSError as exc:
            print(f"ERROR could not restore {path}: {exc}")
            break
    else:
        print(f"ERROR could not restore {path}: interrupted twice")
    if restored:
        try:
            restored = path.read_bytes() == original
        except KeyboardInterrupt as exc:
            held = exc
            restored = False
            print(f"ERROR interrupted while reading {path} back")
        except OSError as exc:
            print(f"ERROR could not read {path} back: {exc}")
            restored = False
        if not restored:
            print(f"ERROR {path} still differs from the original after the restore")
    _clear_bytecode()
    if held is not None:
        raise held
    return restored


def _apply_and_run(path: Path, mutant: bytes, original: bytes, selection):
    """Write the mutant, run the selection, restore. Return (result, error).

    The write is INSIDE the guarded block: an interrupt or an OSError during
    the write must still reach the restore. A failed restore clears
    ``result`` so no verdict is read from a run whose source is still
    mutated.
    """
    result = None
    error = None
    _clear_bytecode()
    try:
        _write_with_retry(path, mutant)
        result = _pytest(*selection, *PYTEST_ARGS)
    except subprocess.TimeoutExpired:
        error = "timed out"
    except OSError as exc:
        error = f"could not write the mutant to {path.name}: {exc}"
    finally:
        if not _restore(path, original):
            error = f"the original bytes of {path.name} were not restored"
            result = None
    return result, error


def _selected(argv):
    """The mutations to run, honouring ``--only a,b``. Unknown name -> None."""
    wanted = None
    for i, arg in enumerate(argv):
        if arg == "--only" and i + 1 < len(argv):
            wanted = [n for n in argv[i + 1].split(",") if n]
        elif arg.startswith("--only="):
            wanted = [n for n in arg.split("=", 1)[1].split(",") if n]
    if wanted is None:
        return list(MUTATIONS)
    known = {mut["name"]: mut for mut in MUTATIONS}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        print(f"ERROR --only names no such mutation: {unknown}")
        print(f"      known names: {sorted(known)}")
        return None
    return [known[name] for name in wanted]


def main():
    if "--check" in sys.argv[1:]:
        return check_only()

    mutations = _selected(sys.argv[1:])
    if mutations is None:
        return 1
    invalid = _declaration_problems(mutations)
    invalid += [f"{name}: duplicate mutation name" for name in _duplicate_names()]
    if invalid:
        for problem in invalid:
            print("ERROR", problem)
        return 1

    originals = _originals()
    errors, caught, survived = [], [], []
    expected, expected_caught = [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test names in {COLLECT_FILES}")
    for mut in mutations:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: expected test {name!r} does not exist"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    _clear_bytecode()
    baseline = _pytest(*COLLECT_FILES, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-3000:])
        return 1
    expected_all = {name for mut in mutations for name in mut["expect"]}
    skipped_catchers = sorted(skipped_names(baseline.stdout) & expected_all)
    if skipped_catchers:
        print(f"ERROR expected catchers skipped in the baseline: {skipped_catchers}")
        return 1
    print("baseline green")

    for mut in mutations:
        path = mut["file"]
        original = originals[path]
        mutated, error = _build_mutant(
            original.decode("utf-8"), _newline_of(original), mut
        )
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        result, error = _apply_and_run(
            path, mutated.encode("utf-8"), original, SELECTION
        )
        if error is not None:
            errors.append(f"{mut['name']}: {error}")
            print("ERROR", errors[-1])
            if "not restored" in error:
                print(f"ERROR stopping the sweep; restore {path.name} by hand")
                break
            continue
        assert result is not None
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        skipped = [n for n in mut["expect"] if n in skipped_names(result.stdout)]
        if skipped:
            errors.append(f"{mut['name']}: expected catcher skipped: {skipped}")
            print("ERROR", errors[-1])
            continue
        if "survives" in mut:
            if result.returncode == 0:
                expected.append(mut["name"])
                print(f"expected survivor {mut['name']}: {mut['survives']}")
                continue
            got = failed_lines(result.stdout)
            if got:
                expected_caught.append(mut["name"])
                print(f"CAUGHT EXPECTED SURVIVOR {mut['name']} by {sorted(got)}")
                for name in sorted(got):
                    print(f"         {got[name]}")
            else:
                errors.append(
                    f"{mut['name']}: exit {result.returncode} with no FAILED "
                    "line in the short summary"
                )
                print("ERROR", errors[-1])
                print(result.stdout[-3000:])
            continue
        # Exit code first: 0 means no test failed, and a green run prints no
        # short-summary section at all.
        if result.returncode == 0:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; no test failed")
            continue
        got = failed_lines(result.stdout)
        if not got:
            errors.append(
                f"{mut['name']}: exit {result.returncode} with no FAILED "
                "line in the short summary"
            )
            print("ERROR", errors[-1])
            print(result.stdout[-3000:])
            continue
        missing = [n for n in mut["expect"] if n not in got]
        if missing:
            survived.append(mut["name"])
            print(
                f"SURVIVED {mut['name']}; missing {missing}; "
                f"failed {sorted(got)}"
            )
            continue
        caught.append(mut["name"])
        print(f"caught   {mut['name']} by {sorted(got)}")
        # Print every FAILED line with its reason, so a reader can confirm
        # the expected assertion fired rather than an unrelated crash.
        for name in sorted(got):
            print(f"         {got[name]}")

    print()
    names = ", ".join(mut["name"] for mut in mutations)
    print(
        f"scope: ran {len(mutations)} of {len(MUTATIONS)} mutations: {names}"
    )
    line = f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}"
    if _declared(mutations):
        line += (
            f", {len(expected)} expected survivors, "
            f"{len(expected_caught)} expected survivors caught"
        )
    print(line)
    return 1 if survived or errors or expected_caught else 0


if __name__ == "__main__":
    sys.exit(main())
