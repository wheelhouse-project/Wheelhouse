"""Mutation gate for wh-safety-word-free-commands.

The change: click, activate, switch to, show, minimize, maximize and cancel
fix run without the safety word, and a window or click command that cannot do
its job is typed as dictation (no safety word) or explained by a notice
(safety word spoken). This gate breaks each new failure branch in turn and
names the tests that must fail for it.

Run it from services/wheelhouse with that service's own interpreter, never
through ``uv run`` (that can build a .venv inside a worktree):

    .venv/Scripts/python.exe tests/mutation_gate_safety_word_free_commands.py

    --check          verify that every pattern still matches exactly once and
                     every mutant compiles; run no test
    --only=<name>    run one mutation (repeatable); the scope line says so
    --discover       run the mutations WITHOUT expected-test validation and
                     print each failed set and failure reason; used to choose
                     the "expect" lists, never as a verdict

Where the mutations are, by target file:

  input_proc.py       the whole-word window-title match and its two uses, the
                      single reply an awaited activate owes the Logic
                      process (refusal, stopped outcome, exactly one reply),
                      the launch limit, and the foreground verification
  app.py              InputRefused for a refusal reply, a refusal logged
                      below ERROR, quiet_timeout logged at WARNING
  command_engine.py   ACTIVATE_TIMEOUT_S for the activate step only, a
                      "stopped" reply stops the rule with no later key
                      (acceptance item 5), StepFailed recorded as
                      last_step_failure and reset on every parse
  speech_processor.py the one place the safety word decides between a
                      notice and typing, the lane-cancel silence, and the
                      cancel recognizer's bare (no safety word) path
  speech/actions.py   click_element and cancel_fix raising StepFailed /
                      NothingToCancel
  main.py             forward_click_element's defer_failure_notice path
  patterns.toml       requires_hotword put back on each freed row, and taken
                      off go-to-app

Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern, a mutation that does not compile, a per-mutation timeout, a
suite-timeout abort, any pytest return code other than 0 or 1, any ERROR
line in the summary, an expected test that does not exist, and a failing test
that raised an exception instead of failing its assertion (unless the
mutation lists a reason prefix in "raises_ok": a test that waits for a reply the
mutant never sends fails with a timeout, and that timeout IS the missing
behavior).

Each target file's own line endings are detected and every pattern is
translated to them before matching; a file is restored with write_bytes and
its bytecode cache entries are deleted after every mutation.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
INPUT_PROC = SERVICE / "input_proc.py"
APP = SERVICE / "app.py"
MAIN = SERVICE / "main.py"
ENGINE = SERVICE / "speech" / "command_engine.py"
PROCESSOR = SERVICE / "speech" / "speech_processor.py"
ACTIONS = SERVICE / "speech" / "actions.py"
PATTERNS = SERVICE / "speech" / "config" / "patterns.toml"

F_TITLE = "tests/test_input_proc_window_title_match.py"
F_REPLY = "tests/test_input_proc_activate_reply.py"
F_ACTIVATE = "tests/test_input_proc_activate_window.py"
F_APP = "tests/test_app_input_refusal.py"
F_WIN = "tests/test_safety_word_free_window_commands.py"
F_CLICK = "tests/test_safety_word_free_click.py"
F_CANCEL = "tests/test_safety_word_free_cancel_fix.py"
F_CANCEL_AI = "tests/test_ai/test_cancel_during_running_rewrite.py"
F_CLICK_FORM = "tests/test_click_element_clicks_form.py"

ALL_TEST_FILES = [
    F_TITLE, F_REPLY, F_ACTIVATE, F_APP, F_WIN, F_CLICK, F_CANCEL,
    F_CANCEL_AI, F_CLICK_FORM,
]

# --tb=line carries the failure REASON; this project's pytest prints a bare
# "FAILED <nodeid>" summary with no reason suffix. COLUMNS keeps pytest from
# cutting a long node id.
PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

MUTATION_TIMEOUT_S = 300


def mut(name, file, old, new, tests, expect, **extra):
    entry = {
        "name": name, "file": file, "old": old, "new": new,
        "tests": tests, "expect": expect,
    }
    entry.update(extra)
    return entry


MUTATIONS = [
    # =====================================================================
    # input_proc.py: the window-title match
    # =====================================================================
    mut(
        "title-phrase-regex-meaning",
        INPUT_PROC,
        "{re.escape(phrase)}",
        "{phrase}",
        [F_TITLE],
        ['test_the_phrase_has_no_regular_expression_meaning'],
    ),
    mut(
        "title-ending-punctuation-kept",
        INPUT_PROC,
        '.strip().rstrip(".,!?;:").strip()',
        ".strip()",
        [F_TITLE],
        ['test_ending_punctuation_is_removed_from_the_phrase'],
    ),
    mut(
        "title-no-boundary-before",
        INPUT_PROC,
        'rf"(?<!\\w){re.escape(phrase)}(?!\\w)"',
        'rf"{re.escape(phrase)}(?!\\w)"',
        [F_TITLE],
        ['test_a_partial_word_is_not_the_foreground_window', 'test_a_symbol_phrase_still_needs_a_boundary_before_its_first_letter', 'test_me_does_not_find_welcome'],
    ),
    mut(
        "title-no-boundary-after",
        INPUT_PROC,
        'rf"(?<!\\w){re.escape(phrase)}(?!\\w)"',
        'rf"(?<!\\w){re.escape(phrase)}"',
        [F_TITLE],
        ['test_a_phrase_inside_a_longer_word_does_not_match'],
    ),
    mut(
        "title-backslash-b-boundary",
        INPUT_PROC,
        'rf"(?<!\\w){re.escape(phrase)}(?!\\w)"',
        'rf"\\b{re.escape(phrase)}\\b"',
        [F_TITLE],
        ['test_a_phrase_ending_in_a_symbol_matches_that_title', 'test_notepad_plus_plus_finds_its_window'],
    ),
    mut(
        "title-case-sensitive",
        INPUT_PROC,
        'title or "", re.IGNORECASE)',
        'title or "", 0)',
        [F_TITLE],
        ['test_a_phrase_ending_in_a_symbol_matches_that_title', 'test_art_does_not_find_start_menu_but_finds_art_class', 'test_notepad_plus_plus_finds_its_window'],
    ),
    mut(
        "title-empty-phrase-matches-everything",
        INPUT_PROC,
        "    if not phrase:\n        return False\n",
        "    if not phrase:\n        return True\n",
        [F_TITLE],
        ['test_a_phrase_that_is_only_punctuation_matches_nothing'],
    ),
    mut(
        "find-window-matches-by-regular-expression",
        INPUT_PROC,
        "if _window_title_has_phrase(title, target):",
        "if re.search(target, title, re.IGNORECASE):",
        [F_TITLE],
        ['test_art_does_not_find_start_menu_but_finds_art_class', 'test_me_does_not_find_welcome'],
    ),
    mut(
        "foreground-matches-by-regular-expression",
        INPUT_PROC,
        "return _window_title_has_phrase(title, target)",
        "return bool(re.search(target, title, re.IGNORECASE))",
        [F_TITLE],
        ['test_a_partial_word_is_not_the_foreground_window'],
    ),
    mut(
        "foreground-exe-target-read-as-a-title",
        INPUT_PROC,
        '        if target.lower().endswith(".exe"):\n            try:\n',
        "        if False:\n            try:\n",
        [F_TITLE],
        ['test_an_exe_target_compares_the_process_name'],
    ),
    mut(
        "foreground-process-lookup-failure-counts-as-in-front",
        INPUT_PROC,
        '                logger.debug(f"Verification process lookup failed: {e}")\n'
        "                return False\n",
        '                logger.debug(f"Verification process lookup failed: {e}")\n'
        "                return True\n",
        [F_TITLE, F_REPLY],
        ['test_a_failed_process_lookup_is_not_the_foreground_window', 'test_a_failed_process_name_read_is_not_the_foreground_window'],
    ),
    mut(
        "foreground-read-failure-counts-as-in-front",
        INPUT_PROC,
        '        logger.debug(f"Verification loop iteration failed: {e}")\n'
        "        return False\n",
        '        logger.debug(f"Verification loop iteration failed: {e}")\n'
        "        return True\n",
        [F_TITLE, F_REPLY],
        ['test_a_failed_foreground_read_is_not_the_foreground_window', 'test_a_failed_title_read_is_not_the_foreground_window'],
    ),

    # =====================================================================
    # input_proc.py: the one reply an awaited activate owes
    # =====================================================================
    mut(
        "reply-stop-carries-no-stopped-outcome",
        INPUT_PROC,
        "        return self.refuse(message, outcome='stopped')\n",
        "        return self.refuse(message)\n",
        [F_REPLY],
        ['test_a_spoken_program_whose_window_is_not_in_front_stops_the_rule', 'test_an_exe_whose_window_is_not_in_front_stops_the_rule', 'test_the_reply_does_not_wait_for_the_store_window_wait'],
        raises_ok=['KeyError:'],
    ),
    mut(
        "reply-refusal-not-marked",
        INPUT_PROC,
        "'error': True, 'refusal': True,\n",
        "'error': True, 'refusal': False,\n",
        [F_REPLY],
        ['test_a_running_store_program_that_is_refused_answers', 'test_no_program_answers_with_the_notice_text_and_shows_none', 'test_the_refusal_is_marked_and_shows_no_notice'],
    ),
    mut(
        "reply-a-second-answer-is-sent",
        INPUT_PROC,
        "            if self._answered:\n                self._logger.debug(\n",
        "            if False:\n                self._logger.debug(\n",
        [F_REPLY],
        ['test_a_second_answer_is_dropped', 'test_an_error_after_the_answer_is_dropped'],
    ),
    mut(
        "reply-ensure-answered-answers-nothing",
        INPUT_PROC,
        '        if owed:\n            self.refuse(f"Could not activate {self.target}.")\n',
        "        if owed:\n            pass\n",
        [F_REPLY],
        ['test_a_lookup_that_raises_still_answers_once'],
    ),
    mut(
        "reply-ensure-answered-ignores-a-running-verification",
        INPUT_PROC,
        "            owed = not self._answered and not self._verifying\n",
        "            owed = not self._answered\n",
        [F_REPLY],
        ['test_a_spoken_program_whose_window_is_not_in_front_stops_the_rule', 'test_an_exe_whose_window_is_not_in_front_stops_the_rule'],
        raises_ok=['KeyError:'],
    ),
    mut(
        "reply-started-never-marks-the-verification",
        INPUT_PROC,
        "            self._verifying = True\n",
        "            self._verifying = False\n",
        [F_REPLY],
        ['test_a_spoken_program_whose_window_is_not_in_front_stops_the_rule', 'test_an_exe_whose_window_is_not_in_front_stops_the_rule'],
        raises_ok=['KeyError:'],
    ),
    mut(
        "started-window-not-in-front-answers-ok",
        INPUT_PROC,
        '            self.stop(f"{self.target} did not come to the front")\n'
        "        except Exception as e:\n",
        "            self.put({'request_id': self.request_id, 'status': 'ok',\n"
        "                      'path': 'heuristic_done', 'action': self.action})\n"
        "        except Exception as e:\n",
        [F_REPLY],
        ['test_a_spoken_program_whose_window_is_not_in_front_stops_the_rule', 'test_an_exe_whose_window_is_not_in_front_stops_the_rule', 'test_the_reply_does_not_wait_for_the_store_window_wait'],
        raises_ok=['KeyError:'],
    ),
    mut(
        "started-window-check-always-passes",
        INPUT_PROC,
        "                if _target_is_foreground(self.target, self._logger):\n",
        "                if True:\n",
        [F_REPLY],
        ['test_a_spoken_program_whose_window_is_not_in_front_stops_the_rule', 'test_an_exe_whose_window_is_not_in_front_stops_the_rule', 'test_the_reply_does_not_wait_for_the_store_window_wait'],
        raises_ok=['KeyError:'],
    ),
    mut(
        "started-window-verification-crash-answers-nothing",
        INPUT_PROC,
        "                exc_info=True,\n"
        "            )\n"
        '            self.stop(f"{self.target} did not come to the front")\n',
        "                exc_info=True,\n"
        "            )\n"
        "            pass\n",
        [F_REPLY],
        ['test_a_verification_that_raises_stops_the_rule'],
    ),
    mut(
        "no-match-answers-nothing",
        INPUT_PROC,
        "        if reply is not None:\n            reply.refuse(message)\n",
        "        if reply is not None:\n            pass\n",
        [F_REPLY],
        ['test_a_spoken_program_that_cannot_start_answers_and_shows_none', 'test_no_program_answers_with_the_notice_text_and_shows_none', 'test_several_programs_answer_with_the_list_and_show_none'],
    ),
    mut(
        "no-match-also-shows-the-notice",
        INPUT_PROC,
        "            reply.refuse(message)\n        else:\n            _notice(message)\n",
        "            reply.refuse(message)\n        _notice(message)\n",
        [F_REPLY],
        ['test_a_spoken_program_that_cannot_start_answers_and_shows_none', 'test_no_program_answers_with_the_notice_text_and_shows_none', 'test_several_programs_answer_with_the_list_and_show_none'],
    ),
    mut(
        "exe-start-failure-answers-nothing",
        INPUT_PROC,
        '            reply.refuse(f"Could not start {target}.")\n',
        "            pass\n",
        [F_REPLY],
        ['test_an_exe_that_cannot_start_answers_and_shows_none'],
    ),
    mut(
        "exe-start-failure-also-shows-the-notice",
        INPUT_PROC,
        '            reply.refuse(f"Could not start {target}.")\n'
        "        else:\n"
        '            notice(f"Could not start {target}.")\n',
        '            reply.refuse(f"Could not start {target}.")\n'
        '        notice(f"Could not start {target}.")\n',
        [F_REPLY],
        ['test_an_exe_that_cannot_start_answers_and_shows_none'],
    ),
    mut(
        "exe-start-never-waits-for-the-window",
        INPUT_PROC,
        "    else:\n        if reply is not None:\n            reply.started()\n    finally:\n",
        "    else:\n        if reply is not None:\n            pass\n    finally:\n",
        [F_REPLY],
        ['test_an_exe_whose_window_comes_to_the_front_answers_ok', 'test_an_exe_whose_window_is_not_in_front_stops_the_rule'],
        raises_ok=['KeyError:'],
    ),
    mut(
        "spoken-start-never-waits-for-the-window",
        INPUT_PROC,
        "            if reply is not None:\n                reply.started()\n",
        "            if reply is not None:\n                pass\n",
        [F_REPLY],
        ['test_a_spoken_program_whose_window_comes_to_the_front_answers_ok', 'test_a_spoken_program_whose_window_is_not_in_front_stops_the_rule', 'test_the_reply_does_not_wait_for_the_store_window_wait'],
        raises_ok=['KeyError:'],
    ),
    mut(
        "running-store-window-never-verified",
        INPUT_PROC,
        "                    if reply is not None:\n                        reply.verify_running()\n",
        "                    if reply is not None:\n                        pass\n",
        [F_REPLY],
        ['test_a_running_store_program_that_is_brought_forward_answers_ok'],
        raises_ok=['KeyError:'],
    ),
    mut(
        "running-store-window-refusal-answers-nothing",
        INPUT_PROC,
        "                        reply.refuse(\n"
        '                            f"Windows refused to bring {program.name} "\n'
        '                            f"to the front"\n'
        "                        )\n",
        "                        pass\n",
        [F_REPLY],
        ['test_a_running_store_program_that_is_refused_answers'],
    ),
    mut(
        "found-window-refusal-answers-nothing",
        INPUT_PROC,
        "                    reply.refuse(\n"
        '                        f"Windows refused to bring {target} to the front"\n'
        "                    )\n",
        "                    pass\n",
        [F_REPLY],
        ['test_the_refusal_is_marked_and_shows_no_notice'],
    ),
    mut(
        "found-window-never-verified",
        INPUT_PROC,
        "        if reply is not None and verify_found_window:\n",
        "        if False:\n",
        [F_REPLY, F_ACTIVATE],
        ['test_an_awaited_success_is_still_verified'],
    ),
    mut(
        "launch-limit-shows-a-notice-for-an-awaited-activate",
        INPUT_PROC,
        "        if on_refused is not None:\n            on_refused(_LAUNCH_BUSY_NOTICE)\n",
        "        if False:\n            on_refused(_LAUNCH_BUSY_NOTICE)\n",
        [F_REPLY],
        ['test_the_launch_limit_answers_and_shows_no_notice'],
    ),
    mut(
        "exe-launch-limit-refusal-not-passed-on",
        INPUT_PROC,
        "                    else buffer_manager.invalidate\n"
        "                ),\n"
        "                on_refused=None if reply is None else reply.refuse,\n",
        "                    else buffer_manager.invalidate\n"
        "                ),\n"
        "                on_refused=None,\n",
        [F_REPLY],
        ['test_the_launch_limit_answers_and_shows_no_notice'],
    ),
    mut(
        "spoken-launch-limit-refusal-not-passed-on",
        INPUT_PROC,
        "                _notice,\n"
        "                on_refused=None if reply is None else reply.refuse,\n",
        "                _notice,\n"
        "                on_refused=None,\n",
        [F_REPLY],
        ['test_the_launch_limit_answers_and_shows_no_notice'],
    ),
    mut(
        "a-launch-that-raises-is-not-answered",
        INPUT_PROC,
        "    if reply is None:\n        return work\n",
        "    if True:\n        return work\n",
        [F_REPLY],
        ['test_a_lookup_that_raises_still_answers_once'],
    ),
    mut(
        "handler-error-sent-past-the-single-reply",
        INPUT_PROC,
        "                reply.put(error_reply)\n",
        "                response_queue.put(error_reply)\n",
        [F_REPLY, F_ACTIVATE],
        ['test_an_error_after_the_answer_is_dropped'],
    ),

    # =====================================================================
    # app.py: a refusal is not an error
    # =====================================================================
    mut(
        "app-refusal-reply-is-a-plain-runtime-error",
        APP,
        "                                if response.get('refusal'):\n",
        "                                if False:\n",
        [F_APP],
        ['test_a_refusal_reply_resolves_as_input_refused', 'test_send_request_logs_a_refusal_below_error'],
        raises_ok=['RuntimeError:'],
    ),
    mut(
        "app-every-error-reply-is-a-refusal",
        APP,
        "                                if response.get('refusal'):\n",
        "                                if True:\n",
        [F_APP],
        ['test_a_plain_error_reply_stays_a_plain_runtime_error', 'test_send_request_still_logs_a_plain_error_at_error'],
    ),
    mut(
        "app-refusal-logged-at-error",
        APP,
        "            logger.info(\n"
        '                "Request \'%s\' (request_id=%s) refused by the Input process: %s",\n',
        "            logger.error(\n"
        '                "Request \'%s\' (request_id=%s) refused by the Input process: %s",\n',
        [F_APP],
        ['test_send_request_logs_a_refusal_below_error'],
    ),
    mut(
        "app-refusal-falls-to-the-general-handler",
        APP,
        "        except InputRefused as refusal:\n",
        "        except ZeroDivisionError as refusal:\n",
        [F_APP],
        ['test_send_request_logs_a_refusal_below_error'],
    ),
    mut(
        "app-refusal-is-swallowed",
        APP,
        '                queued["action"], request_id, refusal,\n'
        "            )\n"
        "            raise\n",
        '                queued["action"], request_id, refusal,\n'
        "            )\n"
        "            pass\n",
        [F_APP],
        ['test_send_request_logs_a_refusal_below_error'],
    ),
    mut(
        "app-quiet-timeout-ignored",
        APP,
        "                logging.WARNING if quiet_timeout else logging.ERROR,\n",
        "                logging.ERROR,\n",
        [F_APP],
        ['test_quiet_timeout_logs_below_error_and_still_raises'],
    ),
    mut(
        "app-every-timeout-is-quiet",
        APP,
        "                logging.WARNING if quiet_timeout else logging.ERROR,\n",
        "                logging.WARNING,\n",
        [F_APP],
        ['test_a_timeout_logs_at_error_by_default'],
    ),

    # =====================================================================
    # command_engine.py: the rule's reaction to the Input process
    # =====================================================================
    mut(
        "engine-activate-wait-is-the-default",
        ENGINE,
        '                            if func_name == "activate" else {}\n',
        "                            if False else {}\n",
        [F_WIN],
        ['test_a_slow_activate_stops_quietly_with_no_error_record', 'test_the_activate_step_waits_longer_than_the_default_timeout'],
        raises_ok=['KeyError:'],
    ),
    mut(
        "engine-every-step-gets-the-activate-wait",
        ENGINE,
        '                            if func_name == "activate" else {}\n',
        "                            if True else {}\n",
        [F_WIN],
        ['test_the_activate_step_waits_longer_than_the_default_timeout'],
    ),
    mut(
        "engine-activate-wait-is-too-short",
        ENGINE,
        "ACTIVATE_TIMEOUT_S = 15.0\n",
        "ACTIVATE_TIMEOUT_S = 1.0\n",
        [F_WIN],
        ['test_the_activate_step_waits_longer_than_the_default_timeout'],
    ),
    mut(
        "engine-activate-timeout-logged-at-error",
        ENGINE,
        '                                "quiet_timeout": True,\n',
        '                                "quiet_timeout": False,\n',
        [F_WIN],
        ['test_a_slow_activate_stops_quietly_with_no_error_record', 'test_the_activate_step_waits_longer_than_the_default_timeout'],
    ),
    mut(
        "engine-a-stopped-reply-is-an-ordinary-failure",
        ENGINE,
        "                                if reply.get('outcome') == 'stopped':\n",
        "                                if False:\n",
        [F_WIN],
        ['test_a_stopped_command_shows_no_notice_and_types_nothing', 'test_a_stopped_freed_command_types_nothing_and_says_nothing', 'test_a_stopped_rule_is_handled_and_sends_no_key'],
    ),
    mut(
        "engine-a-stopped-reply-abandons-the-rule",
        ENGINE,
        '                                        "process: %s", exc,\n'
        "                                    )\n"
        "                                    return True\n",
        '                                        "process: %s", exc,\n'
        "                                    )\n"
        "                                    return False\n",
        [F_WIN],
        ['test_a_stopped_command_shows_no_notice_and_types_nothing', 'test_a_stopped_freed_command_types_nothing_and_says_nothing', 'test_a_stopped_rule_is_handled_and_sends_no_key'],
    ),
    mut(
        "engine-a-refusal-is-re-raised-unchanged",
        ENGINE,
        "                                raise StepFailed(str(exc)) from exc\n",
        "                                raise\n",
        [F_WIN],
        ['test_a_failed_close_shows_a_notice_and_sends_no_key', 'test_a_refused_bring_forward_logs_no_error_and_shows_a_notice', 'test_the_failure_record_is_reset_by_the_next_call'],
        raises_ok=['TimeoutError'],
    ),
    mut(
        "engine-an-unanswered-activate-lets-the-rule-go-on",
        ENGINE,
        '                                func_name == "activate"\n'
        "                                and isinstance(exc, asyncio.TimeoutError)\n",
        '                                func_name == "activate"\n'
        "                                and False\n",
        [F_WIN],
        ['test_a_freed_command_that_never_got_an_answer_types_nothing', 'test_a_slow_activate_stops_quietly_with_no_error_record', 'test_an_activate_that_never_answers_stops_the_rule'],
        raises_ok=['TimeoutError'],
    ),
    mut(
        "engine-step-failure-not-recorded",
        ENGINE,
        "            if isinstance(e, StepFailed):\n"
        "                self.last_step_failure = e\n",
        "            if isinstance(e, StepFailed):\n"
        "                pass\n",
        [F_WIN, F_CLICK, F_CANCEL],
        ['test_a_click_with_no_match_after_the_safety_word_shows_a_notice', 'test_a_reused_id_after_the_replay_is_an_ordinary_failure', 'test_with_the_safety_word_a_notice_is_shown_and_nothing_typed'],
        raises_ok=['TimeoutError', 'ValueError:'],
    ),
    mut(
        "engine-step-failure-kept-for-the-next-parse",
        ENGINE,
        "        self.dictation_fallback_this_parse = False\n"
        "        self.last_step_failure = None\n",
        "        self.dictation_fallback_this_parse = False\n",
        [F_WIN],
        ['test_the_failure_record_is_reset_by_the_next_call'],
    ),

    # =====================================================================
    # speech/actions.py
    # =====================================================================
    mut(
        "actions-an-unparseable-click-target-is-ignored",
        ACTIONS,
        '            raise StepFailed("Say the name or number of what to click.")\n',
        "            return None\n",
        [F_CLICK],
        ['test_with_the_safety_word_a_notice_is_shown_and_nothing_typed', 'test_without_the_safety_word_the_words_are_typed'],
    ),
    mut(
        "actions-a-deferred-click-failure-is-dropped",
        ACTIONS,
        "        if isinstance(deferred, dict):\n"
        "            raise self._click_step_failed(lc, deferred)\n",
        "        if isinstance(deferred, dict):\n"
        "            return None\n",
        [F_CLICK],
        ['test_a_click_with_no_match_after_the_safety_word_shows_a_notice', 'test_a_replacement_word_is_converted_when_the_words_are_typed', 'test_without_the_safety_word_the_words_are_typed_with_no_notice'],
        raises_ok=['ValueError:'],
    ),
    mut(
        "actions-click-asks-for-the-notice-at-once",
        ACTIONS,
        "            query, trace_id, defer_failure_notice=True,\n",
        "            query, trace_id, defer_failure_notice=False,\n",
        [F_CLICK],
        ['test_a_click_with_no_match_is_typed_whole', 'test_a_replacement_word_is_converted_when_the_words_are_typed', 'test_without_the_safety_word_the_words_are_typed_with_no_notice'],
    ),
    mut(
        "actions-click-failure-carries-no-toast",
        ACTIONS,
        "        return StepFailed(text, show=show)\n",
        "        return StepFailed(text)\n",
        [F_CLICK],
        ['test_a_click_with_no_match_after_the_safety_word_shows_a_notice', 'test_a_typed_attempt_does_not_use_up_the_one_notice', 'test_with_the_safety_word_a_notice_is_shown_and_nothing_typed'],
        raises_ok=['ValueError:'],
    ),
    mut(
        "actions-disabled-notice-flag-never-set",
        ACTIONS,
        "                lc._click_disabled_notice_shown = True\n"
        "            lc._forward_click_notice(**kwargs)\n",
        "                pass\n"
        "            lc._forward_click_notice(**kwargs)\n",
        [F_CLICK],
        ['test_with_the_safety_word_a_notice_is_shown_and_nothing_typed'],
    ),
    # The typo test is not a catcher here: since cancel-fix is
    # whole_utterance_only (68ac69f0), "cancel fix the typo" never reaches
    # this code, so the mutation cannot change that test's outcome.
    mut(
        "actions-cancel-with-no-job-is-silent",
        ACTIONS,
        '            raise NothingToCancel("No AI job is running.")\n',
        "            return None\n",
        [F_CANCEL, F_CANCEL_AI],
        ['test_a_cancel_after_the_paste_says_there_is_nothing_to_cancel', 'test_without_the_safety_word_the_words_are_typed_with_no_notice'],
    ),
    mut(
        "actions-cancel-with-no-ai-service-is-silent",
        ACTIONS,
        "        if not (ai and ai.is_processing()):\n"
        '            raise NothingToCancel("No AI job is running.")\n',
        "        if ai and not ai.is_processing():\n"
        '            raise NothingToCancel("No AI job is running.")\n'
        "        if not ai:\n"
        "            return None\n",
        [F_CANCEL],
        ['test_with_the_safety_word_a_notice_is_shown_and_nothing_is_typed', 'test_without_the_safety_word_the_words_are_typed_with_no_notice'],
    ),
    mut(
        "actions-nothing-to-cancel-is-no-step-failure",
        ACTIONS,
        "class NothingToCancel(StepFailed):\n",
        "class NothingToCancel(ActionFailed):\n",
        [F_CANCEL, F_CANCEL_AI],
        ['test_a_cancel_after_the_paste_says_there_is_nothing_to_cancel', 'test_the_job_is_cancelled_and_nothing_else_happens', 'test_with_the_safety_word_a_notice_is_shown_and_nothing_is_typed'],
    ),
    mut(
        "actions-step-failure-is-no-action-failure",
        ACTIONS,
        "class StepFailed(ActionFailed):\n",
        "class StepFailed(Exception):\n",
        [F_WIN, F_CLICK, F_CANCEL],
        ['test_a_cancel_with_no_job_logs_no_error', 'test_a_reused_id_after_the_replay_is_an_ordinary_failure', 'test_with_the_safety_word_a_notice_is_shown_and_nothing_typed'],
        raises_ok=['TimeoutError', 'ValueError:'],
    ),

    # =====================================================================
    # main.py: forward_click_element's deferred failures
    # =====================================================================
    mut(
        "main-deferral-never-happens",
        MAIN,
        "        if defer_failure_notice and (\n",
        "        if False and (\n",
        [F_CLICK],
        ['test_a_click_with_no_match_is_typed_whole', 'test_not_found_returns_the_notice_arguments_and_shows_nothing', 'test_without_the_safety_word_the_words_are_typed_with_no_notice'],
        raises_ok=['TypeError:'],
    ),
    mut(
        "main-every-outcome-is-deferred",
        MAIN,
        '            response.outcome == "not_found"\n'
        "            or (\n",
        "            True\n"
        "            or (\n",
        [F_CLICK],
        ['test_ambiguous_shows_its_notice_and_types_nothing', 'test_an_execution_failure_with_a_match_shows_its_notice_only', 'test_every_other_path_returns_none_and_shows_as_before'],
        raises_ok=['ValueError:'],
    ),
    mut(
        "main-a-walk-deadline-is-shown-at-once",
        MAIN,
        '                and response.reason == "walk_deadline_exceeded"\n',
        "                and False\n",
        [F_CLICK],
        ['test_walk_deadline_exceeded_returns_the_arguments', 'test_without_the_safety_word_the_words_are_typed_with_no_notice'],
        raises_ok=['TypeError:'],
    ),
    mut(
        "main-disabled-click-shows-at-once",
        MAIN,
        "            if defer_failure_notice:\n"
        "                # The caller decides whether the notice is shown (the\n",
        "            if False:\n"
        "                # The caller decides whether the notice is shown (the\n",
        [F_CLICK],
        ['test_disabled_by_config_returns_the_arguments_without_the_flag', 'test_without_the_safety_word_the_words_are_typed_with_no_notice'],
        raises_ok=['TypeError:'],
    ),
    mut(
        "main-notice-snapshot-id-ignores-the-response",
        MAIN,
        "        snapshot_id=response.snapshot_id or fallback_snapshot_id,\n",
        "        snapshot_id=fallback_snapshot_id,\n",
        [F_CLICK],
        ['test_not_found_returns_the_notice_arguments_and_shows_nothing'],
    ),
    mut(
        "main-notice-trace-id-ignores-the-response",
        MAIN,
        "        trace_id=response.trace_id or trace_id,\n"
        "        late_correction=late_correction,\n",
        "        trace_id=trace_id,\n"
        "        late_correction=late_correction,\n",
        [F_CLICK],
        ['test_not_found_returns_the_notice_arguments_and_shows_nothing'],
    ),

    # =====================================================================
    # speech_processor.py: where the safety word decides
    # =====================================================================
    mut(
        "processor-safety-word-failure-shows-no-notice",
        PROCESSOR,
        "            if hotword_authorized and isinstance(notice, str) and notice:\n",
        "            if False and isinstance(notice, str) and notice:\n",
        [F_WIN, F_CLICK, F_CANCEL],
        ['test_a_click_with_no_match_after_the_safety_word_shows_a_notice', 'test_a_refused_bring_forward_logs_no_error_and_shows_a_notice', 'test_with_the_safety_word_a_notice_is_shown_and_nothing_typed'],
        raises_ok=['TimeoutError', 'ValueError:'],
    ),
    mut(
        "processor-failure-without-safety-word-types-nothing",
        PROCESSOR,
        "                await self._process_remainder(command_text)\n"
        "                return\n",
        "                return\n",
        [F_WIN, F_CLICK, F_CANCEL],
        ['test_a_click_with_no_match_is_typed_whole', 'test_a_reused_id_after_the_replay_is_an_ordinary_failure', 'test_without_the_safety_word_the_words_are_typed_with_no_notice'],
    ),
    mut(
        "processor-failure-typed-without-replacements",
        PROCESSOR,
        "                await self._process_remainder(command_text)\n"
        "                return\n",
        "                await self._send_to_dictation(command_text)\n"
        "                return\n",
        [F_WIN, F_CLICK],
        ['test_a_replacement_word_is_converted_when_the_words_are_typed'],
    ),
    mut(
        "processor-failure-toast-never-shown",
        PROCESSOR,
        "                    try:\n"
        "                        show()\n",
        "                    try:\n"
        "                        pass\n",
        [F_CLICK],
        ['test_a_click_with_no_match_after_the_safety_word_shows_a_notice', 'test_a_typed_attempt_does_not_use_up_the_one_notice', 'test_with_the_safety_word_a_notice_is_shown_and_nothing_typed'],
        raises_ok=['ValueError:'],
    ),
    mut(
        "processor-failure-notice-never-shown",
        PROCESSOR,
        "                else:\n"
        "                    self._show_failure_notice(notice)\n",
        "                else:\n"
        "                    pass\n",
        [F_WIN, F_CANCEL],
        ['test_a_failed_close_shows_a_notice_and_sends_no_key', 'test_a_failed_freed_command_with_the_safety_word_shows_a_notice', 'test_with_the_safety_word_a_notice_is_shown_and_nothing_is_typed'],
    ),
    mut(
        "processor-failure-notice-goes-nowhere",
        PROCESSOR,
        "            self.text_parser.action_functions.show_notice(notice)\n",
        "            pass\n",
        [F_WIN, F_CANCEL],
        ['test_a_failed_close_shows_a_notice_and_sends_no_key', 'test_a_failed_freed_command_with_the_safety_word_shows_a_notice', 'test_with_the_safety_word_a_notice_is_shown_and_nothing_is_typed'],
    ),
    mut(
        "processor-failure-keeps-the-notice-and-types-too",
        PROCESSOR,
        "                    self._show_failure_notice(notice)\n"
        "                return\n",
        "                    self._show_failure_notice(notice)\n"
        "                await self._process_remainder(command_text)\n"
        "                return\n",
        [F_WIN, F_CANCEL],
        ['test_a_failed_close_shows_a_notice_and_sends_no_key', 'test_a_failed_freed_command_with_the_safety_word_shows_a_notice', 'test_with_the_safety_word_a_notice_is_shown_and_nothing_is_typed'],
        raises_ok=['TimeoutError'],
    ),
    # The typo test is not a catcher here: since cancel-fix is
    # whole_utterance_only (68ac69f0), "cancel fix the typo" never reaches
    # this code, so the mutation cannot change that test's outcome.
    mut(
        "processor-lane-cancel-silence-covers-any-utterance",
        PROCESSOR,
        "                and self._current_utterance_id in self._lane_cancelled_utterances\n",
        "                and True\n",
        [F_CANCEL],
        ['test_a_reused_id_after_the_replay_is_an_ordinary_failure', 'test_without_the_safety_word_the_words_are_typed_with_no_notice'],
    ),
    mut(
        "processor-lane-cancel-silence-is-kept-after-the-replay",
        PROCESSOR,
        "                self._lane_cancelled_utterances.remove(\n"
        "                    self._current_utterance_id\n"
        "                )\n",
        "                pass\n",
        [F_CANCEL],
        ['test_a_reused_id_after_the_replay_is_an_ordinary_failure'],
    ),
    mut(
        "processor-lane-cancel-ids-never-forgotten",
        PROCESSOR,
        "                self._forget_lane_cancels_except(word_event.utterance_id)\n",
        "                pass\n",
        [F_CANCEL],
        ['test_an_id_left_by_an_earlier_utterance_silences_nothing_later'],
    ),
    mut(
        "processor-lane-cancel-forget-keeps-others",
        PROCESSOR,
        "            if recorded == utterance_id\n",
        "            if recorded != utterance_id\n",
        [F_CANCEL],
        ['test_a_reused_id_after_the_replay_is_an_ordinary_failure', 'test_the_job_is_cancelled_and_nothing_else_happens', 'test_the_silence_belongs_to_that_utterance_only'],
    ),
    mut(
        "processor-lane-cancel-with-no-job-logs-an-error",
        PROCESSOR,
        "        except ActionFailed as exc:\n",
        "        except ZeroDivisionError as exc:\n",
        [F_CANCEL],
        ['test_a_cancel_with_no_job_logs_no_error'],
    ),
    mut(
        "processor-lane-cancel-with-no-job-counts-as-a-cancel",
        PROCESSOR,
        '            logger.info("Cancel-only lane: nothing to cancel (%s)", exc)\n'
        "            return False\n",
        '            logger.info("Cancel-only lane: nothing to cancel (%s)", exc)\n'
        "            return True\n",
        [F_CANCEL],
        ['test_a_cancel_with_no_job_logs_no_error'],
    ),
    mut(
        "recognizer-bare-utterance-judged-word-by-word",
        PROCESSOR,
        "            self._words.append(word)\n"
        "            if not word_event.end_of_utterance:\n"
        "                return False\n"
        "            matched = self._is_a_cancel(self._words, authorized=False)\n"
        "            self.reset()\n"
        "            return matched\n"
        "\n"
        "        if not self._hotword_seen:\n",
        "            self._words.append(word)\n"
        "            if False:\n"
        "                return False\n"
        "            matched = self._is_a_cancel(self._words, authorized=False)\n"
        "            self.reset()\n"
        "            return matched\n"
        "\n"
        "        if not self._hotword_seen:\n",
        [F_CANCEL],
        ['test_cancel_fix_the_typo_does_not_cancel_a_running_job', 'test_the_end_marker_fires_when_the_last_word_carried_no_end_flag', 'test_without_the_safety_word_a_longer_utterance_never_fires'],
    ),
    mut(
        "recognizer-bare-first-word-judged-without-its-end",
        PROCESSOR,
        "            self._words.append(word)\n"
        "            if not word_event.end_of_utterance:\n"
        "                return False\n"
        "            matched = self._is_a_cancel(self._words, authorized=False)\n"
        "            self.reset()\n"
        "            return matched\n"
        "\n"
        "        self._words.append(word)\n"
        "        matched = self._is_a_cancel(self._words)\n",
        "            self._words.append(word)\n"
        "            if False:\n"
        "                return False\n"
        "            matched = self._is_a_cancel(self._words, authorized=False)\n"
        "            self.reset()\n"
        "            return matched\n"
        "\n"
        "        self._words.append(word)\n"
        "        matched = self._is_a_cancel(self._words)\n",
        [F_CANCEL],
        ['test_a_reused_id_after_the_replay_is_an_ordinary_failure', 'test_the_job_is_cancelled_and_nothing_else_happens', 'test_without_the_safety_word_it_fires_on_the_last_word_only'],
    ),
    mut(
        "recognizer-end-marker-never-judges-a-bare-utterance",
        PROCESSOR,
        "            matched = self._bare_started and self._is_a_cancel(\n"
        "                self._words, authorized=False\n"
        "            )\n",
        "            matched = False\n",
        [F_CANCEL],
        ['test_the_end_marker_fires_when_the_last_word_carried_no_end_flag'],
    ),
    mut(
        "recognizer-retraction-never-judges-a-bare-text",
        PROCESSOR,
        "            return self._is_a_cancel(\n"
        "                [word.strip().lower() for word in words], authorized=False\n"
        "            )\n",
        "            return False\n",
        [F_CANCEL],
        ['test_a_retraction_without_the_safety_word_fires_only_on_the_whole_command'],
    ),
    mut(
        "recognizer-bare-start-never-recorded",
        PROCESSOR,
        "            self._bare_started = True\n",
        "            self._bare_started = False\n",
        [F_CANCEL],
        ['test_a_reused_id_after_the_replay_is_an_ordinary_failure', 'test_the_job_is_cancelled_and_nothing_else_happens', 'test_without_the_safety_word_it_fires_on_the_last_word_only'],
    ),
    mut(
        "recognizer-bare-state-kept-across-utterances",
        PROCESSOR,
        "        self._hotword_seen = False\n"
        "        self._bare_started = False\n"
        "\n"
        "    def _is_a_cancel(",
        "        self._hotword_seen = False\n"
        "\n"
        "    def _is_a_cancel(",
        [F_CANCEL],
        ['test_the_hotword_of_a_later_utterance_arms_after_a_bare_utterance'],
    ),
    mut(
        "recognizer-end-marker-authorizes-a-gated-pattern",
        PROCESSOR,
        "            matched = self._bare_started and self._is_a_cancel(\n"
        "                self._words, authorized=False\n"
        "            )\n",
        "            matched = self._bare_started and self._is_a_cancel(\n"
        "                self._words, authorized=True\n"
        "            )\n",
        [F_CANCEL],
        ['test_a_hotword_gated_cancel_pattern_never_fires_without_the_hotword'],
    ),
    mut(
        "recognizer-retraction-authorizes-a-gated-pattern",
        PROCESSOR,
        "            return self._is_a_cancel(\n"
        "                [word.strip().lower() for word in words], authorized=False\n"
        "            )\n",
        "            return self._is_a_cancel(\n"
        "                [word.strip().lower() for word in words], authorized=True\n"
        "            )\n",
        [F_CANCEL],
        ['test_a_hotword_gated_cancel_pattern_never_fires_without_the_hotword'],
    ),
    mut(
        "recognizer-bare-words-authorize-a-gated-pattern",
        PROCESSOR,
        "            self._words.append(word)\n"
        "            if not word_event.end_of_utterance:\n"
        "                return False\n"
        "            matched = self._is_a_cancel(self._words, authorized=False)\n"
        "            self.reset()\n"
        "            return matched\n"
        "\n"
        "        if not self._hotword_seen:\n",
        "            self._words.append(word)\n"
        "            if not word_event.end_of_utterance:\n"
        "                return False\n"
        "            matched = self._is_a_cancel(self._words, authorized=True)\n"
        "            self.reset()\n"
        "            return matched\n"
        "\n"
        "        if not self._hotword_seen:\n",
        [F_CANCEL],
        ['test_a_hotword_gated_cancel_pattern_never_fires_without_the_hotword'],
    ),
    mut(
        "recognizer-bare-first-word-authorizes-a-gated-pattern",
        PROCESSOR,
        "            self._words.append(word)\n"
        "            if not word_event.end_of_utterance:\n"
        "                return False\n"
        "            matched = self._is_a_cancel(self._words, authorized=False)\n"
        "            self.reset()\n"
        "            return matched\n"
        "\n"
        "        self._words.append(word)\n"
        "        matched = self._is_a_cancel(self._words)\n",
        "            self._words.append(word)\n"
        "            if not word_event.end_of_utterance:\n"
        "                return False\n"
        "            matched = self._is_a_cancel(self._words, authorized=True)\n"
        "            self.reset()\n"
        "            return matched\n"
        "\n"
        "        self._words.append(word)\n"
        "        matched = self._is_a_cancel(self._words)\n",
        [F_CANCEL],
        ['test_a_hotword_gated_cancel_pattern_never_fires_without_the_hotword'],
    ),
    mut(
        "recognizer-hotword-path-refuses-a-gated-pattern",
        PROCESSOR,
        "    def _is_a_cancel(self, words: list[str], *, authorized: bool = True) -> bool:\n",
        "    def _is_a_cancel(self, words: list[str], *, authorized: bool = False) -> bool:\n",
        [F_CANCEL],
        ['test_a_hotword_gated_cancel_pattern_still_fires_after_the_hotword'],
    ),

    # =====================================================================
    # patterns.toml: which rows need the safety word
    # =====================================================================
    mut(
        "row-activate-app-needs-the-safety-word",
        PATTERNS,
        'doc_id = "activate-app"\n',
        'doc_id = "activate-app"\nrequires_hotword = true\n',
        [F_WIN],
        ['test_a_freed_command_runs_with_no_safety_word', 'test_a_replacement_word_is_converted_when_the_words_are_typed', 'test_the_freed_rows_do_not_require_the_safety_word'],
    ),
    mut(
        "row-switch-to-app-needs-the-safety-word",
        PATTERNS,
        'doc_id = "switch-to-app"\n',
        'doc_id = "switch-to-app"\nrequires_hotword = true\n',
        [F_WIN],
        ['test_a_freed_command_runs_with_no_safety_word', 'test_a_stopped_freed_command_types_nothing_and_says_nothing', 'test_the_freed_rows_do_not_require_the_safety_word'],
    ),
    mut(
        "row-show-app-needs-the-safety-word",
        PATTERNS,
        'doc_id = "show-app"\n',
        'doc_id = "show-app"\nrequires_hotword = true\n',
        [F_WIN],
        ['test_a_freed_command_runs_with_no_safety_word', 'test_a_stopped_freed_command_types_nothing_and_says_nothing', 'test_the_freed_rows_do_not_require_the_safety_word'],
    ),
    mut(
        "row-minimize-app-needs-the-safety-word",
        PATTERNS,
        'doc_id = "minimize-app"\n',
        'doc_id = "minimize-app"\nrequires_hotword = true\n',
        [F_WIN],
        ['test_a_freed_command_runs_with_no_safety_word', 'test_a_slow_activate_stops_quietly_with_no_error_record', 'test_the_freed_rows_do_not_require_the_safety_word'],
        raises_ok=['KeyError:', 'TimeoutError'],
    ),
    mut(
        "row-maximize-app-needs-the-safety-word",
        PATTERNS,
        'doc_id = "maximize-app"\n',
        'doc_id = "maximize-app"\nrequires_hotword = true\n',
        [F_WIN],
        ['test_a_freed_command_runs_with_no_safety_word', 'test_a_stopped_freed_command_types_nothing_and_says_nothing', 'test_the_freed_rows_do_not_require_the_safety_word'],
    ),
    mut(
        "row-click-element-needs-the-safety-word",
        PATTERNS,
        'doc_id = "click-element"\n',
        'doc_id = "click-element"\nrequires_hotword = true\n',
        [F_CLICK, F_CLICK_FORM],
        ['test_a_click_runs_with_no_safety_word', 'test_an_execution_failure_with_a_match_shows_its_notice_only', 'test_without_the_safety_word_the_words_are_typed_with_no_notice'],
        raises_ok=['KeyError:', 'ValueError:'],
    ),
    mut(
        "row-cancel-fix-needs-the-safety-word",
        PATTERNS,
        'doc_id = "cancel-fix"\n',
        'doc_id = "cancel-fix"\nrequires_hotword = true\n',
        [F_CANCEL, F_CANCEL_AI],
        ['test_a_retraction_without_the_safety_word_fires_only_on_the_whole_command', 'test_the_job_is_cancelled_and_nothing_else_happens', 'test_without_the_safety_word_it_fires_on_the_last_word_only'],
    ),
    mut(
        "row-go-to-app-runs-without-the-safety-word",
        PATTERNS,
        'doc_id = "go-to-app"\nrequires_hotword = true\n',
        'doc_id = "go-to-app"\n',
        [F_WIN],
        ['test_go_to_app_does_not_activate_without_the_safety_word', 'test_go_to_app_is_its_own_row_and_keeps_the_safety_word', 'test_go_to_app_without_the_safety_word_activates_nothing'],
        raises_ok=['KeyError:'],
    ),
]


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _pytest(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=MUTATION_TIMEOUT_S,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "COLUMNS": "1000",
        },
    )


def _clear_bytecode(path):
    """Delete the cached bytecode of ``path`` so a mutant is never skipped."""
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for entry in cache.glob(f"{path.stem}.*.pyc"):
            try:
                entry.unlink()
            except OSError:
                pass


def _write_restoring(path, data):
    """write_bytes with a bounded retry for a transient Windows lock."""
    for attempt in range(10):
        try:
            path.write_bytes(data)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.5)


def collect_names():
    """Real test names, so a renamed test cannot read as a survivor."""
    out = _pytest(*ALL_TEST_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def is_assertion_failure(reason):
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    return not _EXCEPTION_HEAD.match(reason)


def failed_names(output):
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def failure_reasons(output):
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


_SUMMARY_ERROR = re.compile(r"^ERROR \S+::")


def error_lines(output):
    """Short-summary ERROR lines only (a node id follows the word).

    A captured log record at ERROR level also starts with "ERROR" but is
    followed by spaces and a logger name, and a genuine catch whose test
    logged an error must not read as a gate error.
    """
    return [
        line for line in output.splitlines() if _SUMMARY_ERROR.match(line)
    ]


def _sources():
    sources = {}
    for path in {m["file"] for m in MUTATIONS}:
        original = path.read_bytes()
        sources[path] = {
            "original": original,
            "text": original.decode("utf-8"),
            "newline": "\r\n" if b"\r\n" in original else "\n",
        }
    return sources


def _translate(sources):
    for m in MUTATIONS:
        newline = sources[m["file"]]["newline"]
        m["old"] = m["old"].replace("\n", newline)
        m["new"] = m["new"].replace("\n", newline)


def _prepare(m, sources):
    """Return (mutated_text, error). Exactly one match, and it must compile."""
    text = sources[m["file"]]["text"]
    count = text.count(m["old"])
    if count != 1:
        return None, f"{m['name']}: pattern matched {count} times, expected 1"
    mutated = text.replace(m["old"], m["new"], 1)
    if m["file"].suffix == ".py":
        try:
            compile(mutated, str(m["file"]), "exec")
        except SyntaxError as exc:
            return None, f"{m['name']}: mutated source does not compile: {exc}"
    return mutated, None


def check_only(sources):
    stale, broken = [], []
    for m in MUTATIONS:
        _mutated, error = _prepare(m, sources)
        if error is None:
            continue
        print("ERROR", error)
        (broken if "does not compile" in error else stale).append(m["name"])
    print()
    print(
        f"checked {len(MUTATIONS)} patterns, "
        f"{len(stale)} stale, {len(broken)} that do not compile"
    )
    return 1 if stale or broken else 0


def main():
    sources = _sources()
    _translate(sources)
    for path, source in sources.items():
        ending = "CRLF" if source["newline"] == "\r\n" else "LF"
        print(f"line endings in {path.name}: {ending}")

    names = [m["name"] for m in MUTATIONS]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        print("ERROR duplicate mutation names:", duplicated)
        return 1

    if "--check" in sys.argv:
        return check_only(sources)

    discover = "--discover" in sys.argv
    only = [a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")]
    unknown = [n for n in only if n not in set(names)]
    if unknown:
        print("ERROR --only names no mutation:", unknown)
        return 1
    selected = [m for m in MUTATIONS if not only or m["name"] in only]

    errors, caught, survived = [], [], []

    if not discover:
        real_names = collect_names()
        print(f"collected {len(real_names)} test names")
        for m in selected:
            if not m["expect"]:
                errors.append(f"{m['name']}: no expected tests named")
            for name in m["expect"]:
                if name not in real_names:
                    errors.append(
                        f"{m['name']}: expected test {name!r} does not exist"
                    )
        if errors:
            for line in errors:
                print("ERROR", line)
            return 1

    baselines = {}
    for m in selected:
        key = tuple(m["tests"])
        if key in baselines:
            continue
        result = _pytest(*key, *PYTEST_ARGS)
        if result.returncode != 0:
            print(f"ERROR baseline is not green for {list(key)}; refusing")
            print(result.stdout[-2000:])
            return 1
        baselines[key] = True
    print(f"baseline green for {len(baselines)} test selections")

    for m in selected:
        path = m["file"]
        original = sources[path]["original"]
        mutated, error = _prepare(m, sources)
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        _write_restoring(path, mutated.encode("utf-8"))
        _clear_bytecode(path)
        try:
            result = _pytest(*m["tests"], *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{m['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            try:
                _write_restoring(path, original)
            finally:
                _clear_bytecode(path)
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{m['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        if result.returncode not in (0, 1):
            errors.append(
                f"{m['name']}: pytest returned {result.returncode}; no verdict"
            )
            print("ERROR", errors[-1])
            continue
        stray = error_lines(result.stdout)
        if stray:
            errors.append(f"{m['name']}: pytest reported errors: {stray[:3]}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        if discover:
            reasons = sorted({r[:110] for r in failure_reasons(result.stdout)})
            print(f"DISCOVER {m['name']} rc={result.returncode}")
            print(f"    failed: {sorted(got)}")
            print(f"    reasons: {reasons}")
            continue
        missing = [n for n in m["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(m["name"])
            print(
                f"SURVIVED {m['name']}; missing {missing}; "
                f"failed tests were {sorted(got)}"
            )
            continue
        allowed = tuple(m.get("raises_ok") or ())
        crashed = [
            r for r in failure_reasons(result.stdout)
            if not is_assertion_failure(r) and not r.startswith(allowed)
        ]
        if crashed:
            errors.append(
                f"{m['name']}: a test raised instead of failing its "
                f"assertion: {sorted(set(crashed))[:3]}"
            )
            print("ERROR", errors[-1])
            continue
        caught.append(m["name"])
        print(f"caught   {m['name']} by {sorted(got)}")

    print()
    skipped = len(MUTATIONS) - len(selected)
    print(
        f"scope: {len(selected)} of {len(MUTATIONS)} mutations, "
        + (f"{skipped} skipped by --only" if skipped else "none skipped")
    )
    if discover:
        print("discover mode: no verdict")
        return 0
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
