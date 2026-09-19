"""Mutation gate for finding wh-voice-access-parity.1.6.2.1.

Codex reported that seven command entries lacked ``whole_utterance_only``, so
a phrase such as "show numbers in the report" executed the command in the
middle of a sentence and dropped the words after it. The seven are
select-word, select-line, select-paragraph, show-numbers, hide-numbers,
show-grid and hide-grid.

The data fix is already in the file: commit c3a54348 (bead
wh-cmd-prefix-word-loss, 2026-08-26) added ``whole_utterance_only = true`` to
153 patterns by a rule, and all seven fall under that rule. What was missing
is evidence naming these seven, in BOTH directions, which the signed
acceptance for wh-voice-access-parity.1.6 requires. The guard tests are in
``TestVoiceAccessAliasEntriesKeepBothDirections`` in
tests/test_router_command_prefix_word_loss.py, and they were written after the
fix, so this gate is what proves they can still see the defect.

Run it from services/wheelhouse:

    python tests/mutation_gate_voice_access_whole_utterance.py

Nine mutations of speech/config/patterns.toml. Seven revert the fix one entry
at a time, and two are input-level checks that the mutation-gate skill asks
for, aimed at the direction a revert cannot reach:

  drop-flag-<doc_id>   Remove ``whole_utterance_only = true`` from one entry.
                       That IS the defect codex reported, restricted to a
                       single command, so a gate that removed all seven at
                       once could not tell which test was doing the work.
                       Seven of these, one per reported entry.
  require-this-word    Narrow ``^select (?:this )?word$`` to
                       ``^select this word$``. The flag stays, so every
                       mid-sentence test still passes; the alias "select word"
                       simply stops working. Only the whole-utterance half can
                       see it, which is what makes it the input-level check
                       for direction two.
  drop-apply-numbers   Narrow ``^(?:show|apply) numbers$`` to
                       ``^show numbers$``. Same shape as above on a different
                       entry, and it removes exactly the older wording that
                       wh-dismiss-alias-restore put back at David's direction.

All nine must be caught, and "caught" means the expected test failed on its
own assertion. A mutant that makes the test raise an exception instead has
proved nothing: the test never reached the assertion the mutation claims to
break. So this gate reads the reason pytest prints after each FAILED name and
refuses to count an exception as a catch (wh-voice-access-parity.2.3.2.4).

Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern, a mutant that is no longer valid TOML, a per-mutation timeout, a
suite-timeout abort, any pytest return code other than 0 or 1, any ERROR line
in the summary, and an expected test that failed for any reason other than its
own assertion.

The gate detects the target file's own line endings and translates each
pattern to them before matching, so a future CRLF conversion of patterns.toml
cannot turn every multi-line pattern into a silent pattern-not-found batch. It
never rewrites the file's endings.

SCOPE NOTE: the mutations also fail the rule-based sweep tests in
``TestEveryExposedCommandIsWholeUtteranceOnly`` in the same file, which is
expected and harmless -- the gate scores only the named expected tests. The
run covers one test file, not the whole suite, so a mutation's effect on
tests/test_grid_speech_routing.py is outside what this gate measures.
"""

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "speech" / "config" / "patterns.toml"
TEST_FILES = ["tests/test_router_command_prefix_word_loss.py"]

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

_MID = "test_the_phrase_inside_a_longer_sentence_dictates"
_WHOLE = "test_the_phrase_alone_still_executes"

# The seven entries codex named. Each anchor is the doc_id line plus every
# line between it and the flag, which makes the match unique: a doc_id occurs
# once in the file.
_SEVEN = [
    ("select-word", 'doc_id = "select-word"\n'),
    ("select-line", 'doc_id = "select-line"\n'),
    ("select-paragraph", 'doc_id = "select-paragraph"\n'),
    ("show-numbers", 'doc_id = "show-numbers"\nrequires_hotword = false\n'),
    ("hide-numbers", 'doc_id = "hide-numbers"\nrequires_hotword = false\n'),
    ("show-grid", 'doc_id = "show-grid"\n'),
    ("hide-grid", 'doc_id = "hide-grid"\n'),
]

MUTATIONS = [
    {
        "name": f"drop-flag-{doc_id}",
        "old": head + "whole_utterance_only = true\n",
        "new": head,
        "expect": [_MID],
    }
    for doc_id, head in _SEVEN
]

MUTATIONS += [
    {
        # Input-level, direction two. The flag is untouched, so every
        # mid-sentence assertion still holds; the optional "this" simply
        # becomes required and the bare alias stops matching.
        "name": "require-this-word",
        "old": "pattern = '''^select (?:this )?word$'''\n",
        "new": "pattern = '''^select this word$'''\n",
        "expect": [_WHOLE],
    },
    {
        # Input-level, direction two, on a different entry. Dropping "apply"
        # removes the older Wheelhouse wording that wh-dismiss-alias-restore
        # put back at David's direction, so a person who learned it loses it.
        "name": "drop-apply-numbers",
        "old": "pattern = '''^(?:show|apply) numbers$'''\n",
        "new": "pattern = '''^show numbers$'''\n",
        "expect": [_WHOLE],
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
    mutation run should produce are the expected catchers and the rule-based
    sweeps, and both fail on their own asserts.
    """
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


def error_lines(output):
    """Summary lines for tests that ERRORED rather than failed."""
    return [line for line in output.splitlines() if line.startswith("ERROR ")]


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
            tomllib.loads(mutated)
        except tomllib.TOMLDecodeError as exc:
            errors.append(f"{mut['name']}: mutated file is not valid TOML: {exc}")
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
        # would prove nothing (wh-voice-access-parity.2.3.2.4).
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
