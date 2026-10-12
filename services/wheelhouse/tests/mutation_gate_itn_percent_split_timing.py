"""Mutation gate for the split timing of "fifty percent" (wh-itn-percent-digits).

The ITN stage writes "fifty percent" as "50%", but speech arrives in pieces:
"fifty" is stable on the screen before the word "percent" is heard. The
message layer must then correct the stable word "fifty" to "50%", which means
a final that carries digits in place of an already-sent word has to break the
stable prefix. These tests guard that, and the shared service's own
tests cannot see it, because the rule lives in services/stt_providers/shared
and the correction lives in services/wheelhouse. This gate breaks each guarded
behaviour in turn and requires every named test to fail. A mutation nothing
catches is a finding against the test, not against the gate.

* the-word-prefix-break-never-fires-at-the-message-level: the test in
  integrations/websocket_manager.py that decides whether a final still starts
  with the words already sent is replaced by ``if True:``. The delta becomes
  the trailing words only, so the stable word "fifty" is never corrected and
  the screen keeps it. This is the reported defect.
* the-percent-rule-eats-the-word-percent-on-the-screen: the percent rule in
  shared_stt/transcript_rules.py returns the end of its phrase one word too
  far, so the word "percent" is consumed and the "%" is never written. The
  file belongs to another service than the test selection: the end-to-end
  tests load the real module by file path, so the mutant reaches them.
* the-editor-replay-never-applies-the-percent-replacement: the call in
  speech/speech_processor.py that applies the allowed text replacements
  to the corrected final, in the editor arm of ``_handle_retraction``, is
  replaced by an assignment that leaves the text as it was. The editor
  then inserts "50 percent" with its word still a word. The unit tests
  that read the replay text handed to ``retract_editor_text`` must fail.
* the-percent-pass-runs-the-pattern-action: the loop of
  ``_apply_allowed_text_replacements`` runs the matched pattern's action
  through ``TextParser._execute_rule`` before it writes the literal text.
  The text comes back the same, but the app receives an
  ``intelligent_insert_text`` call. The no-action test must fail.
* the-percent-pass-runs-over-the-earlier-utterance-words: the pass is moved
  from before ``_restore_earlier_utterance_words`` to after it, so it runs
  over the joined string. The earlier utterance's spoken word "percent" is
  then rewritten as "%". The test that puts an earlier "percent" in front
  of the replay must fail.
* the-editor-replay-never-applies-the-percent-replacement-at-the-document:
  the same mutation as the entry above, with the test that reads the
  editor document itself. A gate entry runs one test file, and the
  document test lives in another file than the message-level tests. The
  end-to-end harness cannot reach the editor document, because it builds
  no logic_controller, so this test is the only screen-level proof.
* the-percent-pass-strips-whitespace-before-any-replacement: the write in
  ``_apply_allowed_text_replacements`` removes the whitespace in front of
  the match for every replacement, not only for punctuation. A user entry
  can give the percent pattern a word, and that word then loses the
  whitespace the speaker produced. The test that gives the pattern a word
  must fail.
* the-editor-arm-logs-the-restore-twice: the record call of
  ``_restore_earlier_utterance_words`` in the editor arm loses its
  ``log=False``, so one correction writes the restore line twice. A
  reader of a pipeline log would count two restores. The test that counts
  the lines must fail.

A further entry once mutated the same word-prefix break and expected the
end-to-end tests to catch it. It survived, and it is gone. The comment
in the mutation table below says why: those tests never run
integrations/websocket_manager.py.

WHY THIS GATE HAS ITS OWN LAUNCHER
----------------------------------
The shared runner (services/stt_providers/shared/tests/mutation_gate_runner.py)
launches every suite through scripts/run_tests.py, which runs ``uv run pytest``
and creates a config.toml from the template when none exists. Neither is
allowed here. So this gate keeps the runner's pattern checks, byte-exact
restoration and interrupt handling, and replaces only the layer that starts
pytest: ``_run_pytest``, ``_collected_names``, ``_failed_names`` and
``_clear_pycache``. It copies
services/wheelhouse/tests/mutation_gate_audio_suppression_auto.py.

Three false verdicts this launcher refuses to report:
* A pytest ERROR is not a failure. ``run_tests`` raises instead, and the
  runner reports an error.
* A verdict read outside pytest's short summary is not a verdict. Only the
  lines after the short-summary header are read.
* Each failing test's reason is printed, because a mutant that crashes before
  the assertion reads as a catch and proves nothing.

INTERPRETERS
------------
A worktree has no virtual environment of its own, so the wheelhouse
interpreter is taken from this checkout when it has one and from the main
checkout otherwise.

Usage (from the worktree root, any interpreter):
    python services/wheelhouse/tests/mutation_gate_itn_percent_split_timing.py --check
    python services/wheelhouse/tests/mutation_gate_itn_percent_split_timing.py
    python ... mutation_gate_itn_percent_split_timing.py the-word-prefix-break
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[3]
WHEELHOUSE = WORKTREE / "services/wheelhouse"
SHARED = WORKTREE / "services/stt_providers/shared"

spec = importlib.util.spec_from_file_location(
    "itn_percent_split_timing_shared_gate",
    WORKTREE / "services/stt_providers/shared/tests/mutation_gate_runner.py",
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


# ---------------------------------------------------------------------------
# The launch layer
# ---------------------------------------------------------------------------

def _main_checkout() -> Path:
    """The checkout that owns the virtual environments.

    A worktree's .git is a file naming <main>/.git/worktrees/<name>; a normal
    checkout's is a directory, and then this checkout is the main one.
    """
    git = WORKTREE / ".git"
    if git.is_dir():
        return WORKTREE
    gitdir = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
    return gitdir.parents[2]


MAIN = _main_checkout()


def _interpreter(service: Path) -> Path:
    """The python.exe that owns a service's dependencies."""
    local = service / ".venv/Scripts/python.exe"
    if local.exists():
        return local
    return MAIN / service.relative_to(WORKTREE) / ".venv/Scripts/python.exe"


# service -> (cwd for pytest, the service whose environment runs it)
SUITES = {
    WHEELHOUSE: (WHEELHOUSE, WHEELHOUSE),
}

RUN_TIMEOUT = 300
_SUMMARY_HEADER = "short test summary info"


def _bare_name(text: str) -> str:
    """A collected or reported test id, without its path and parameter list."""
    return re.sub(r"\[.*\]$", "", text.split("::")[-1].split()[0])


def _summary(output: str) -> str:
    return output.split(_SUMMARY_HEADER, 1)[1] if _SUMMARY_HEADER in output else ""


def failed_names(output: str) -> set[str]:
    """The tests pytest's short summary reports as FAILED, and only those."""
    return {
        _bare_name(line[len("FAILED "):])
        for line in _summary(output).splitlines()
        if line.startswith("FAILED ")
    }


def run_tests(service: Path, test_file, collect: bool = False):
    cwd, owner = SUITES[service]
    # COLUMNS: pytest truncates a short-summary line to the terminal width,
    # and at the 80-column default these test ids leave no room for the
    # message -- the one part of the line that says whether the mutant
    # reached the assertion or crashed before it.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COLUMNS="1000")
    command = [
        str(_interpreter(owner)), "-m", "pytest",
        *runner._targets(test_file),
        "-p", "no:randomly",
    ]
    command += ["--collect-only", "-q"] if collect else ["-rf", "--tb=short"]
    result = subprocess.run(
        command, cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=RUN_TIMEOUT,
    )
    output = result.stdout + result.stderr
    if collect:
        if result.returncode:
            raise RuntimeError("collection failed: " + output[-2000:])
        return result
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"pytest exited {result.returncode}: " + output[-2000:])
    errored = [line for line in _summary(output).splitlines()
               if line.startswith("ERROR ")]
    if errored:
        raise RuntimeError("pytest reported an ERROR, which is no verdict: "
                           + "; ".join(errored[:3]))
    # Print why each test failed. A mutant that crashes upstream of the
    # assertion reads as a catch and proves nothing, and the reason is the
    # only thing in the run that tells the two apart (mutation-gate skill).
    for line in _summary(output).splitlines():
        if line.startswith("FAILED "):
            print("  " + line[len("FAILED "):][:600], flush=True)
    return result


def collected_names(service: Path, test_file) -> set[str]:
    """Every test the selection collects."""
    result = run_tests(service, test_file, collect=True)
    return {_bare_name(line) for line in result.stdout.splitlines()
            if "::" in line}


def clear_pycache(service: Path) -> None:
    """The runner's cache clearing, without walking a whole checkout."""
    skip = {".venv", ".git", "node_modules", ".pytest_cache", "vendor"}
    for parent, directories, _ in os.walk(service):
        directories[:] = [d for d in directories if d not in skip]
        if "__pycache__" in directories:
            directories.remove("__pycache__")
            shutil.rmtree(Path(parent) / "__pycache__", ignore_errors=True)


runner._run_pytest = run_tests
runner._collected_names = collected_names
runner._failed_names = failed_names
runner._clear_pycache = clear_pycache


# ---------------------------------------------------------------------------
# The selections
# ---------------------------------------------------------------------------

MESSAGE_TESTS = (WHEELHOUSE, "tests/test_websocket_manager.py")
EDITOR_REPLAY_TESTS = (
    WHEELHOUSE, "tests/test_speech_processor_editor_multiword_routing.py",
)
SCREEN_TESTS = (
    WHEELHOUSE, "tests/e2e/test_e2e_held_prefix_restore_scope.py",
)
PASS_TESTS = (WHEELHOUSE, "tests/test_replacement_remainder.py")

WEBSOCKET = WHEELHOUSE / "integrations/websocket_manager.py"
SPEECH_PROCESSOR = WHEELHOUSE / "speech/speech_processor.py"
TRANSCRIPT_RULES = SHARED / "shared_stt/transcript_rules.py"

SCREEN_EXPECT = [
    "test_fifty_then_percent_leaves_digits_and_no_leftover_word",
    "test_percent_correction_leaves_an_earlier_utterance_alone",
]

EDITOR_REPLAY_EXPECT = [
    "test_a_corrected_percent_phrase_replays_as_the_symbol",
    "test_the_percent_pass_leaves_the_earlier_utterance_words_alone",
]

PASS_EXPECT = ["test_the_pass_runs_no_pattern_action"]

DOCUMENT_TESTS = (
    WHEELHOUSE, "tests/test_speech_processor_editor_restore_unconditional.py",
)

DOCUMENT_EXPECT = [
    "test_a_corrected_percent_phrase_ends_as_digits_with_no_word_percent",
]

WHITESPACE_EXPECT = [
    "test_a_word_replacement_keeps_the_whitespace_in_front_of_it",
]

RESTORE_LOG_EXPECT = [
    "test_one_editor_correction_writes_one_restore_log_line",
]

# The tests of the text-only pass that had no entry of their own. At
# 6d907456 each of them failed on an AttributeError, because the pass and
# its constants exist on the branch only; each list below names tests that
# must fail on their own assertion under the entry's mutant.
LIST_EXPECT = [
    "test_the_list_holds_exactly_the_percent_pattern",
    "test_the_pass_leaves_a_replacement_outside_the_list_alone",
]
SYMBOL_EXPECT = [
    "test_the_pass_writes_the_percent_symbol_against_its_number",
]
RESCAN_EXPECT = ["test_the_scan_continues_after_the_text_it_wrote"]
TRIGGER_GATE_EXPECT = [
    "test_text_with_no_listed_trigger_never_scans_the_catalog",
]
COMMAND_WORD_EXPECT = ["test_the_pass_leaves_a_command_word_alone"]
BOUND_QUIET_EXPECT = ["test_a_run_of_exactly_the_bound_logs_nothing"]
BOUND_LINE_EXPECT = [
    "test_the_iteration_bound_stops_the_pass_and_logs_one_line",
]

TRIGGER_GATE = (
    "        if not any(\n"
    "            trigger.search(text)\n"
    "            for trigger in self._replay_text_triggers()\n"
    "        ):\n"
)
BOUND_WARNING = (
    "            if self._find_earliest_replacement(rest) is not None:\n"
)

PREFIX_TEST = (
    "        if len(new_words) >= len(sent_words) "
    "and new_words[:len(sent_words)] == sent_words:\n"
)


def mutation(name, selection, file, old, new, expect):
    service, test_file = selection
    return dict(name=name, service=service, test_file=test_file, file=file,
                old=old, new=new, expect=list(expect))


MUTATIONS = [
    mutation(
        "the-word-prefix-break-never-fires-at-the-message-level",
        MESSAGE_TESTS, WEBSOCKET,
        PREFIX_TEST,
        "        if True:\n",
        ["test_a_final_with_digits_breaks_the_stable_prefix"]),
    # There is deliberately NO entry that mutates the word prefix
    # break and expects the end-to-end tests to catch it. Those
    # tests never run integrations/websocket_manager.py:
    # tests/e2e/e2e_harness.py imports it nowhere, and the
    # docstring of its send_retraction_marker says it reproduces
    # the production shape of _handle_mode3_retract. The harness
    # starts after the websocket manager, inside the Logic
    # process, so such an entry can never be caught. The entry
    # above proves that line through tests/test_websocket_manager.py
    # instead. Measured 2026-10-06: the entry ran and survived.
    mutation(
        "the-percent-rule-eats-the-word-percent-on-the-screen",
        SCREEN_TESTS, TRANSCRIPT_RULES,
        "    return _write_number(run, value), anchor\n",
        "    return _write_number(run, value), anchor + 1\n",
        SCREEN_EXPECT),
    mutation(
        "the-editor-replay-never-applies-the-percent-replacement",
        EDITOR_REPLAY_TESTS, SPEECH_PROCESSOR,
        "            final_text = self._apply_allowed_text_replacements("
        "final_text)\n",
        "            final_text = final_text\n",
        EDITOR_REPLAY_EXPECT),
    mutation(
        "the-editor-replay-never-applies-the-percent-replacement"
        "-at-the-document",
        DOCUMENT_TESTS, SPEECH_PROCESSOR,
        "            final_text = self._apply_allowed_text_replacements("
        "final_text)\n",
        "            final_text = final_text\n",
        DOCUMENT_EXPECT),
    # The pass is a synchronous method, so the mutant runs the action with
    # asyncio.run; the no-action test is a plain test with no running loop.
    mutation(
        "the-percent-pass-runs-the-pattern-action",
        PASS_TESTS, SPEECH_PROCESSOR,
        "            literal = self._allowed_replacement_text(pattern_data)\n",
        "            asyncio.run(self.text_parser._execute_rule(\n"
        "                match, pattern_data['actions'],\n"
        "                pattern_data.get('validation_group')))\n"
        "            literal = self._allowed_replacement_text(pattern_data)\n",
        PASS_EXPECT),
    # Not a bare swap of the two lines: the pass would then run over the
    # unrewritten text and its result would be thrown away. This puts the
    # pass over the joined string, which is the defect to guard.
    mutation(
        "the-percent-pass-runs-over-the-earlier-utterance-words",
        EDITOR_REPLAY_TESTS, SPEECH_PROCESSOR,
        "            final_text = self._apply_allowed_text_replacements("
        "final_text)\n"
        "            editor_replay = self._restore_earlier_utterance_words("
        "final_text)\n",
        "            editor_replay = self._restore_earlier_utterance_words("
        "final_text)\n"
        "            editor_replay = self._apply_allowed_text_replacements("
        "editor_replay)\n",
        ["test_the_percent_pass_leaves_the_earlier_utterance_words_alone"]),
    # The write branch keeps the condition's first half, so the mutant
    # still refuses an empty replacement and fails on the whitespace
    # assertion only.
    mutation(
        "the-percent-pass-strips-whitespace-before-any-replacement",
        PASS_TESTS, SPEECH_PROCESSOR,
        "            stripped = literal.strip()\n"
        "            if stripped and all(\n"
        "                character in string.punctuation\n"
        "                for character in stripped\n"
        "            ):\n",
        "            stripped = literal.strip()\n"
        "            if stripped:\n",
        WHITESPACE_EXPECT),
    # The call still returns the same text, so only the log line count
    # changes and the test fails on its own count assertion.
    mutation(
        "the-editor-arm-logs-the-restore-twice",
        EDITOR_REPLAY_TESTS, SPEECH_PROCESSOR,
        "            spoken_replay = self._restore_earlier_utterance_words(\n"
        "                final_text, log=False)\n",
        "            spoken_replay = self._restore_earlier_utterance_words(\n"
        "                final_text)\n",
        RESTORE_LOG_EXPECT),
    # wh-itn-percent-digits.2: the next eight entries cover the tests of
    # TestTheTextOnlyReplacementPass that failed on an AttributeError at
    # 6d907456 and had no entry. "hello period world" holds no percent
    # word, so the trigger gate returns it at once; only a list that
    # names the period pattern lets the pass rewrite it.
    mutation(
        "the-text-pass-list-names-the-period-pattern",
        PASS_TESTS, SPEECH_PROCESSOR,
        '_EDITOR_REPLAY_TEXT_DOC_IDS = ["punct-percent"]\n',
        '_EDITOR_REPLAY_TEXT_DOC_IDS = ["punct-percent", "punct-period"]\n',
        LIST_EXPECT),
    # Without the rstrip the separator stays in front of the symbol, so
    # "50 percent" is written "50 %" and the test fails on its comparison.
    mutation(
        "the-text-pass-keeps-the-space-before-the-symbol",
        PASS_TESTS, SPEECH_PROCESSOR,
        "                preceding = before.rstrip()\n",
        "                preceding = before\n",
        SYMBOL_EXPECT),
    # The text written is put back in front of the scan, so the trigger
    # inside "x percent y" is found again. The scan then runs to the step
    # bound and the result differs from the expected string.
    mutation(
        "the-text-pass-rescans-the-text-it-wrote",
        PASS_TESTS, SPEECH_PROCESSOR,
        "            rest = rest[end:]\n"
        "        else:\n",
        "            rest = written + rest[end:]\n"
        "            written = ''\n"
        "        else:\n",
        RESCAN_EXPECT),
    # The condition is replaced, not the block: the early return keeps
    # its body, and the mutant fails on the scan count alone.
    mutation(
        "the-text-pass-scans-the-catalog-without-a-trigger",
        PASS_TESTS, SPEECH_PROCESSOR,
        TRIGGER_GATE,
        "        if False:\n",
        TRIGGER_GATE_EXPECT),
    # "backspace" holds no percent word, so the trigger gate returns it
    # before the scan: only a mutant that runs the text through the
    # command parser first can send the key press. asyncio.run, because
    # the test is a plain test with no running loop.
    mutation(
        "the-text-pass-runs-a-command-through-the-parser",
        PASS_TESTS, SPEECH_PROCESSOR,
        TRIGGER_GATE,
        "        asyncio.run(self.text_parser.parse_and_execute(text))\n"
        + TRIGGER_GATE,
        COMMAND_WORD_EXPECT),
    # The same line as the next entry, mutated the other way. A bound
    # reached exactly as the last match is used up is a complete pass.
    mutation(
        "the-step-bound-warns-when-no-match-is-left",
        PASS_TESTS, SPEECH_PROCESSOR,
        BOUND_WARNING,
        "            if True:\n",
        BOUND_QUIET_EXPECT),
    mutation(
        "the-step-bound-never-warns",
        PASS_TESTS, SPEECH_PROCESSOR,
        BOUND_WARNING,
        "            if False:\n",
        BOUND_LINE_EXPECT),
    # The bound is multiplied, so a pass that has matches left after
    # three steps finishes them and the result is no longer cut short.
    mutation(
        "the-step-bound-is-never-reached",
        PASS_TESTS, SPEECH_PROCESSOR,
        "        for _ in range(_TEXT_REPLACEMENT_PASS_MAX_STEPS):\n",
        "        for _ in range(_TEXT_REPLACEMENT_PASS_MAX_STEPS * 100):\n",
        BOUND_LINE_EXPECT),
]


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
