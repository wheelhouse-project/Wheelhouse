"""Mutation gate for criterion 5 of wh-voice-access-parity.1.6.

Two things need proving here, and they need different mutations.

THE ALIAS ITSELF. "dismiss" was the last item of the bead's SCOPE list still
unshipped. Its red-first evidence is natural rather than manufactured: the
completeness table in tests/test_voice_access_alias_completeness.py was
written with ("dismiss", "escape", False) already in it, and its first run
failed three parametrized cases -- and only those three, out of 117 -- with

    AssertionError: 'dismiss' is listed as needing no hotword to reach
    doc_id 'escape', but the matcher refuses it without one

The trigger was then widened to ^(?:escape|dismiss)$.

THE COMPLETENESS CHECK. The other 37 forms in that table already worked, so
every assertion about them was written green. This gate is what shows those
assertions can see a defect at all -- in particular the ordering assertion,
which is the part of the bead's DONE WHEN ("first-match-wins ordering is
checked for each new alias") that no other test in the repository covers.

Run it from services/wheelhouse:

    python tests/mutation_gate_voice_access_dismiss_alias.py

Five mutations of speech/config/patterns.toml:

  revert-dismiss            Put ^escape$ back. The plain revert of the fix.
  drop-whole-utterance      Take whole_utterance_only off the escape row and
                            leave the alias in place. INPUT-LEVEL: "dismiss"
                            alone still fires, so every direction-two
                            assertion stays green and only the router test
                            that dictates "dismiss the meeting invite" sees
                            it. That test is the entire reason the alias is
                            safe to ship, since "dismiss" is an ordinary
                            English word.
  gate-the-escape-row       Add requires_hotword = true to the escape row.
                            Criterion 8 forbids adding it to any entry, and
                            nothing else in the suite would notice.
  narrow-select-word        Make ^select (?:this )?word$ require "this". The
                            bare form then falls through to ^select (.+)$
                            (select-phrase), which is a DIFFERENT command and
                            needs the hotword. This is the ordering mutation:
                            the table says select-word owns "select word",
                            and the assertion that the first owning row is
                            the named one is what catches the fall-through.
  shadow-dismiss-numbers    Drop "dismiss" from ^(?:hide|dismiss) numbers$.
                            Widening the escape row must not cost the older
                            two-word wording wh-dismiss-alias-restore put
                            back at David's direction on 2026-08-20.

All five must be caught, and "caught" means the expected test failed on its
own assertion. A mutant that makes a test raise has proved nothing
(wh-voice-access-parity.2.3.2.4), so this gate reads the reason pytest prints
after each FAILED name and refuses to count an exception as a catch.

Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern, a mutant that is not valid TOML, a per-mutation timeout, a
suite-timeout abort, any pytest return code other than 0 or 1, any ERROR line
in the summary, and an expected test that failed for any reason other than
its own assertion. The target is a data file, so the compile check is
``tomllib.loads`` rather than ``compile`` -- a broken TOML file would make
every test error at collection and read as a false catch.

The gate detects the target file's own line endings and translates each
pattern to them before matching, and restores the file with write_bytes so a
restore cannot rewrite the whole file's endings.

A mutation may also fail tests this gate does not name -- dropping
whole_utterance_only, for instance, is visible to the sweep in
tests/test_router_command_prefix_word_loss.py. That file is not in this
gate's selection, and the gate scores only the named expected tests.
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
TEST_FILES = ["tests/test_voice_access_alias_completeness.py"]

# --tb=line is what carries the failure REASON. This project's pytest prints
# its short summary as a bare "FAILED <nodeid>" with no " - reason" suffix, so
# the summary alone cannot tell an assertion failure from a crash.
PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

_OWNS = "test_the_named_row_owns_the_form"
_FIRES = "test_the_live_matcher_fires_and_consumes_the_whole_form"
_NO_HOTWORD = "test_a_hotword_free_alias_does_not_need_the_hotword"
_ALONE = "test_the_word_alone_still_presses_escape"
_MID = "test_the_word_inside_a_longer_sentence_dictates"
_TOGGLES = "test_the_two_dismiss_toggles_are_untouched"

_ESCAPE_ROW = 'doc_id = "escape"\nwhole_utterance_only = true\n'

MUTATIONS = [
    {
        "name": "revert-dismiss",
        "old": "pattern = '''^(?:escape|dismiss)$'''\n",
        "new": "pattern = '''^escape$'''\n",
        "expect": [_OWNS, _FIRES, _NO_HOTWORD, _ALONE],
    },
    {
        # Input-level. The alias still fires alone, so only the router's
        # mid-sentence direction can see this one.
        "name": "drop-whole-utterance",
        "old": _ESCAPE_ROW,
        "new": 'doc_id = "escape"\n',
        "expect": [_MID],
    },
    {
        "name": "gate-the-escape-row",
        "old": _ESCAPE_ROW,
        "new": 'doc_id = "escape"\nrequires_hotword = true\nwhole_utterance_only = true\n',
        "expect": [_OWNS, _FIRES, _NO_HOTWORD],
    },
    {
        # The ordering mutation: "select word" falls through to the broader
        # ^select (.+)$ row, which is a different command behind the hotword.
        "name": "narrow-select-word",
        "old": "pattern = '''^select (?:this )?word$'''\n",
        "new": "pattern = '''^select this word$'''\n",
        "expect": [_OWNS, _FIRES, _NO_HOTWORD],
    },
    {
        "name": "shadow-dismiss-numbers",
        "old": "pattern = '''^(?:hide|dismiss) numbers$'''\n",
        "new": "pattern = '''^hide numbers$'''\n",
        "expect": [_OWNS, _FIRES, _NO_HOTWORD, _TOGGLES],
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
    """Every failure reason --tb=line printed, one per failing parameter."""
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
