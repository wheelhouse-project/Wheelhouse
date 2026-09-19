"""Mutation gate for the hotword half of finding wh-voice-access-parity.1.6.2.2.

Commit 9fd44173 gave click-element requires_hotword = true, and the Pattern
Manager help reference kept publishing a bare "click submit button" beside the
retired trigger ^click\\s+(.+)$. The finding asked for a documentation
regression check so a requires_hotword change cannot leave a bare invocation
published. That check is TestPublishedExamplesHonourTheHotword in
tests/test_action_catalog.py, and this gate is what proves it can see the
defect.

One of its four tests already has natural red-first evidence: written as a
sweep rather than a hand list, it failed on its first run against a SECOND
live instance nobody had reported -- select_phrase published a bare "select
brown fox" while ^select (.+)$ also requires the hotword. The message was:

    these published examples show a hotword command without its hotword:
    ["select_phrase: tells the reader to say 'select brown fox', but
    '^select (.+)$' requires the 'x-ray' hotword"]

The other three tests were written after their fix, so they need this gate.

Run it from services/wheelhouse:

    python tests/mutation_gate_hotword_examples.py

Four mutations of speech/action_catalog.py:

  click-trigger-revert  Put back the retired trigger ^click\\s+(.+)$. That IS
                        the defect codex reported. It also takes the entry out
                        of the sweeps' scope, since they judge only examples
                        whose trigger the shipped file carries -- which is the
                        exact blindness the named test exists to cover.
  click-drops-hotword   Publish "click submit button" again with the trigger
                        left correct. The revert above cannot prove this half,
                        because it removes the entry from the sweep entirely.
  select-drops-hotword  The same on the second entry the guard found, so the
                        sweep is shown to judge every entry rather than one.
  click-body-misspelt   Keep the hotword and break the rest: "x-ray klick
                        submit button". The hotword rule still passes, so only
                        the fires-once-the-hotword-is-removed test can see it.
                        This is the input-level check the mutation-gate skill
                        asks for.

All four must be caught, and "caught" means the expected test failed on its
own assertion. A mutant that makes the test raise an exception instead has
proved nothing (wh-voice-access-parity.2.3.2.4), so this gate reads the reason
pytest prints after each FAILED name and refuses to count an exception as a
catch.

Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern, a mutant that does not compile, a per-mutation timeout, a
suite-timeout abort, any pytest return code other than 0 or 1, any ERROR line
in the summary, and an expected test that failed for any reason other than its
own assertion.

The gate detects the target file's own line endings and translates each
pattern to them before matching, and it restores the file with write_bytes so
a restore cannot rewrite the whole file's endings.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "speech" / "action_catalog.py"
TEST_FILES = ["tests/test_action_catalog.py"]

# --tb=line is what carries the failure REASON. This project's pytest prints
# its short summary as a bare "FAILED <nodeid>" with no " - reason" suffix, so
# the summary alone cannot tell an assertion failure from a crash.
PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

_QUOTES_SHIPPED = "test_the_click_by_name_example_quotes_the_shipped_trigger"
_SHOWS_HOTWORD = "test_a_hotword_command_is_never_shown_without_its_hotword"
_STILL_FIRES = "test_the_hotword_example_still_fires_once_the_hotword_is_removed"
_IN_SCOPE = "test_the_rule_covers_something"

MUTATIONS = [
    {
        "name": "click-trigger-revert",
        "old": """            'Trigger "^(?:click|tap)\\\\s+(.+)$" with params ["g1"]: saying '
""",
        "new": """            'Trigger "^click\\\\s+(.+)$" with params ["g1"]: saying '
""",
        # The entry leaves the sweeps' scope as well as failing the named
        # test, so the in-scope guard fires too. Both are expected.
        "expect": [_QUOTES_SHIPPED, _IN_SCOPE],
    },
    {
        "name": "click-drops-hotword",
        "old": """            '"x-ray click submit button" clicks the button labeled Submit. '
""",
        "new": """            '"click submit button" clicks the button labeled Submit. '
""",
        "expect": [_SHOWS_HOTWORD],
    },
    {
        "name": "select-drops-hotword",
        "old": """            '"x-ray select brown fox" selects the first "brown fox" in the '
""",
        "new": """            '"select brown fox" selects the first "brown fox" in the '
""",
        "expect": [_SHOWS_HOTWORD],
    },
    {
        # Input-level: the hotword is still there, so the hotword rule passes.
        # Only the test that strips the hotword and matches the remainder
        # against the shipped pattern can see this one.
        "name": "click-body-misspelt",
        "old": """            '"x-ray click submit button" clicks the button labeled Submit. '
""",
        "new": """            '"x-ray klick submit button" clicks the button labeled Submit. '
""",
        "expect": [_STILL_FIRES],
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
