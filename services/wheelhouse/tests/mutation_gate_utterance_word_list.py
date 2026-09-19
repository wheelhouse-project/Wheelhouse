"""Mutation gate for the per-utterance word list (Stage 1 of
wh-whole-utterance-command-matching).

Run it from services/wheelhouse:

    python tests/mutation_gate_utterance_word_list.py

Stage 1 adds ``SpeechProcessor._words_this_utterance``: every real word
of the current utterance, appended at WordEvent arrival, reset when a
word opens a new utterance, rebuilt by a retraction replay, and mirrored
in directly on the editor-path retract (whose replay happens inline in
the GUI and never re-enters process_word_event). Nothing reads the list
yet; Stage 3 will. The tests were written red-first (AttributeError
before the implementation), and this gate proves they pin the specific
placement claims the implementation makes.

Six mutations of speech/speech_processor.py:

  hold-blind-append   Skip the append while a replacement prefix is
                      held. This is the placement claim: the held-prefix
                      pass CONSUMES a completing word and returns before
                      the main routing, so an append site placed after
                      that pass would miss the word. The structural
                      equivalent of moving the append later.
  no-reset            Never start a fresh list on start_of_utterance.
                      Utterances accumulate forever; the retraction
                      replay (whose first word opens the utterance)
                      appends instead of rebuilding.
  markers-append      Drop the marker exclusion so the utterance-end
                      marker and the timeout sentinel append their
                      word="" payloads.
  editor-mirror-dropped
                      Remove the editor-path mirror, leaving the list
                      holding pre-revision words after an editor
                      retract.
  mirror-ignores-result
                      Mirror without checking retract_editor_text's
                      return, recording corrected words the GUI never
                      installed (codex finding
                      wh-whole-utterance-command-matching.1.1).
  order-flip          insert(0, ...) instead of append: the list exists
                      and holds the right words, but not in spoken
                      order. Input-level check that the tests compare
                      the ordered list, not membership.

All six must be caught, and "caught" means the expected test failed on
its own assertion. Reported as errors, never as a verdict:
pattern-not-found, an ambiguous pattern, a mutation that does not
compile, a per-mutation timeout, a suite-timeout abort, any pytest
return code other than 0 or 1, any ERROR line in the summary, and an
expected test that failed for any reason other than its own assertion.

The gate detects the target file's own line endings and translates each
pattern to them before matching. It never rewrites the file's endings.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "speech" / "speech_processor.py"
TEST_FILES = ["tests/test_utterance_word_list.py"]

# --tb=line is what carries the failure REASON. This project's pytest prints
# its short summary as a bare "FAILED <nodeid>" with no " - reason" suffix, so
# the summary alone cannot tell an assertion failure from a crash. With
# --tb=line each failure also prints one "<path>:<lineno>: <reason>" line.
PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

# One "<path>:<lineno>: <reason>" line from --tb=line.
_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")

# A reason that begins with an exception class name means the test raised
# rather than failed its assertion. AssertionError is the one exception name
# that IS an assertion failure: pytest prints it when the assert carries a
# message, and prints the assert expression itself when it does not.
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

MUTATIONS = [
    {
        # The placement claim. A completing word is consumed by
        # _resolve_pending_replacement_prefix, which returns before the
        # main routing; appending only when no prefix is held behaves
        # exactly like an append site placed after that pass.
        "name": "hold-blind-append",
        "old": "        if word_event.word and not (\n",
        "new": (
            "        if word_event.word "
            "and self._pending_replacement_prefix is None and not (\n"
        ),
        "expect": ["test_word_completing_held_replacement_prefix_recorded"],
    },
    {
        "name": "no-reset",
        # 09758e55 put a snapshot of the outgoing utterance's list above
        # the reset, so the old three-line anchor stopped matching and
        # this mutation reported pattern-not-found in every run since
        # 2026-08-29. The refreshed pattern removes ONLY the reset and
        # keeps the snapshot, so the mutant is still exactly "the list
        # is never cleared at a new utterance". The snapshot then names
        # the same list object the append keeps growing, which is the
        # unavoidable consequence of not resetting, not a second
        # mutation.
        "old": """                self._previous_utterance_words = self._words_this_utterance
                self._words_this_utterance = []
""",
        "new": """                self._previous_utterance_words = self._words_this_utterance
""",
        "expect": [
            "test_new_utterance_resets_list",
            "test_retraction_replay_rebuilds_list",
        ],
    },
    {
        # With the guard gone, the utterance-end marker and the timeout
        # sentinel append their word="" payloads. The held-prefix test
        # also fails under this mutant (its timeout sentinel appends ""
        # between "question" and "mark"); that extra failure is an
        # assertion failure too, which the reason check below accepts.
        "name": "markers-append",
        "old": """        if word_event.word and not (
            word_event.is_utterance_end_marker
            or word_event.is_retraction_marker
            or word_event.is_timeout_finalize_marker
            or word_event.is_lifecycle_reset_marker
        ):
""",
        "new": """        if True:
""",
        "expect": ["test_markers_do_not_append"],
    },
    {
        "name": "editor-mirror-dropped",
        # wh-spaced-punctuation-names-unresolved.3.1.2 (de2bb449)
        # renamed this arm's replay text to editor_replay, because
        # the retracted arm now prepends the previous utterance's
        # held words to it. The mirror still mirrors what was
        # actually replayed, so only the name here changed.
        "old": "                self._words_this_utterance = editor_replay.split()\n",
        "new": "                pass\n",
        "expect": ["test_editor_retraction_mirrors_final_text"],
    },
    {
        # Codex round-2 finding wh-whole-utterance-command-matching.1.1:
        # the mirror runs only when retract_editor_text confirmed the
        # replay. Ignoring the result mirrors corrected words the GUI
        # never installed.
        "name": "mirror-ignores-result",
        "old": "            if retract_ok:\n",
        "new": "            if True:\n",
        "expect": ["test_editor_retraction_failed_retract_keeps_list"],
    },
    {
        # Input-level: the list exists and holds the right words, but in
        # reverse order. Proves the tests compare the ordered list.
        "name": "order-flip",
        "old": "            self._words_this_utterance.append(word_event.word)\n",
        "new": (
            "            self._words_this_utterance.insert(0, "
            "word_event.word)\n"
        ),
        "expect": ["test_plain_dictation_words_recorded"],
    },
]


def _pytest(*extra):
    return subprocess.run(
        ["uv", "run", "python", "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def collect_names():
    """Real test names in the file, so a renamed test cannot read as a survivor."""
    out = _pytest(*TEST_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def is_assertion_failure(reason):
    """True when pytest's short reason describes a failed assert statement."""
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    return not _EXCEPTION_HEAD.match(reason)


def failed_names(output):
    """Every test name on a FAILED summary line, parameters stripped."""
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def failure_reasons(output):
    """Every failure reason --tb=line printed, one per failing parameter.

    --tb=line does not say which test each reason belongs to, so the caller
    requires that EVERY reason in the run is an assertion failure. That is the
    stricter reading and it is the right one here: the only failures a
    mutation run should produce are the expected catchers.
    """
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


# A pytest -rfE summary error line names a node id ("ERROR tests/x.py" or
# "ERROR tests/x.py::test_y"). Captured LOG lines also start with "ERROR "
# ("ERROR    speech.command_engine:command_engine.py:316 ..."), so a bare
# startswith("ERROR ") check reports a false gate error whenever a failing
# test's captured output holds an error-level log record.
_ERROR_SUMMARY = re.compile(r"^ERROR\s+\S+\.py(::\S+)?(\s|$)")


def error_lines(output):
    """Summary lines for tests that ERRORED rather than failed."""
    return [
        line for line in output.splitlines() if _ERROR_SUMMARY.match(line)
    ]


def main():
    original = SRC.read_bytes()
    # Match the file's own line endings rather than assuming LF. A repository
    # can mix conventions per file, and an LF pattern finds nothing in a CRLF
    # file -- which reads as a batch of pattern-not-found errors.
    newline = "\r\n" if b"\r\n" in original else "\n"
    text = original.decode("utf-8")
    for mut in MUTATIONS:
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)

    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"line endings in {SRC.name}: {'CRLF' if newline == chr(13) + chr(10) else 'LF'}")
    print(f"collected {len(real_names)} test names in {', '.join(TEST_FILES)}")
    for mut in MUTATIONS:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(f"{mut['name']}: expected test {name!r} does not exist")
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    baseline = _pytest(*TEST_FILES, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print("baseline green")

    for mut in MUTATIONS:
        count = text.count(mut["old"])
        if count != 1:
            errors.append(f"{mut['name']}: pattern matched {count} times, expected 1")
            print("ERROR", errors[-1])
            continue
        mutated = text.replace(mut["old"], mut["new"], 1)
        try:
            compile(mutated, str(SRC), "exec")
        except SyntaxError as exc:
            errors.append(f"{mut['name']}: mutated source does not compile: {exc}")
            print("ERROR", errors[-1])
            continue
        SRC.write_bytes(mutated.encode("utf-8"))
        try:
            result = _pytest(*TEST_FILES, *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            SRC.write_bytes(original)
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            SRC.write_bytes(original)
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        if result.returncode not in (0, 1):
            # 0 means every test passed, 1 means some test failed. Anything
            # else is an aborted run -- interrupted, internal error, usage
            # error, nothing collected -- and is not a verdict either way.
            errors.append(
                f"{mut['name']}: pytest returned {result.returncode}; no verdict"
            )
            print("ERROR", errors[-1])
            continue
        stray = error_lines(result.stdout)
        if stray:
            errors.append(f"{mut['name']}: pytest reported errors: {stray[:3]}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        missing = [n for n in mut["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; failed tests were {sorted(got)}")
            continue
        # The expected test failed. Insist every failure in the run was an
        # assertion failure: a mutant that makes a test RAISE never reached
        # the assertion the mutation claims to break, so counting it as caught
        # would prove nothing.
        crashed = [r for r in failure_reasons(result.stdout)
                   if not is_assertion_failure(r)]
        if crashed:
            errors.append(
                f"{mut['name']}: a test raised instead of failing its "
                f"assertion: {sorted(set(crashed))}"
            )
            print("ERROR", errors[-1])
            continue
        caught.append(mut["name"])
        print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    print(f"scope: {len(MUTATIONS)} mutations, none skipped")
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
