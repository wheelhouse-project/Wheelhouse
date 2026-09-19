"""Mutation gate for the branch whitespace fix (wh-explainer-branch-space-loss).

Run it from services/wheelhouse:

    python tests/mutation_gate_explainer_branch_space.py

It proves that the tests in TestShownPhrasesMatchTheirOwnPattern really
catch the defect they were written for. Two mutations of the one call that
carries the fix, in speech/pattern_explainer.py:

  keep-edges-off     Strip non-article alternation branches again, so the
                     branch "this " of "(?:this )?" comes back as "this"
                     and "select (?:this )?word" shows "select thisword".
                     Preserve collapsible article branches: stripping those
                     also disables article collapse and makes the unrelated
                     landmark test crash at the variant cap. That crash is
                     an error, not evidence for the whitespace assertion.
  keep-edges-always  Pass keep_edges=True, so the WHOLE pattern also keeps
                     its edges. The whole-pattern strip must survive the
                     fix, and five existing tests in the file guard it --
                     the new class does not, because every shipped pattern
                     is anchored and has no edge whitespace to strip.

Both must be caught. Pattern-not-found, an ambiguous pattern, a mutation
that does not compile, a timeout, and a suite-timeout abort are reported as
errors, never as a verdict. Crashes, pytest setup/collection errors, unexpected
exit codes, and missing failure reasons are also errors. A catch requires
every failing test to have reached an assertion.

One test is deselected: TestShippedPatternCoverage::
test_trigger_fallbacks_match_the_pinned_allowlist. It fails for an
unrelated reason tracked as wh-explainer-allowlist-landmarks, and a red
baseline would make every mutation report "caught" for the wrong reason.
Delete the deselect once that bead closes.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "speech" / "pattern_explainer.py"
TEST_FILE = "tests/test_pattern_explainer.py"
CLASS = "TestShownPhrasesMatchTheirOwnPattern"

KNOWN_FAILURE = (
    "tests/test_pattern_explainer.py::TestShippedPatternCoverage::"
    "test_trigger_fallbacks_match_the_pinned_allowlist"
)
# Like the scroll gate, use --tb=line: this project's FAILED summary lines
# carry names but no reasons. Include ERROR summaries without matching logs.
PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line", "--deselect", KNOWN_FAILURE]
_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_ERROR_SUMMARY = re.compile(r"^ERROR\s+\S+\.py(::\S+)?(\s|$)")
_ASSERTION = re.compile(r"^(?:assert(?:\s|$)|AssertionError(?:\b))")

MUTATIONS = [
    {
        "name": "keep-edges-off",
        "old": "_collapse_whitespace(variant, keep_edges=_branch)",
        "new": "_collapse_whitespace(variant, keep_edges=_branch and variant in _COLLAPSIBLE_ARTICLES)",
        "expect": ["test_every_shown_phrase_matches_the_pattern_it_belongs_to"],
    },
    {
        "name": "keep-edges-always",
        "old": "_collapse_whitespace(variant, keep_edges=_branch)",
        "new": "_collapse_whitespace(variant, keep_edges=True)",
        # Caught by the pinned-English tests elsewhere in the file, not by
        # the new class. Any assertion failure counts, so no name is required.
        "expect": [],
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
    """Real test names in CLASS, so a renamed test cannot read as a survivor."""
    out = _pytest(f"{TEST_FILE}::{CLASS}", "--collect-only", "-q", "-p", "no:randomly")
    if out.returncode != 0 or error_lines(out.stdout + "\n" + out.stderr):
        raise ValueError(f"pytest returned {out.returncode} or reported collection errors")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def failed_names(output):
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def error_lines(output):
    """Pytest ERROR summaries, excluding captured application ERROR logs."""
    return [line for line in output.splitlines() if _ERROR_SUMMARY.match(line)]


def failure_reasons(output):
    """One reason per failing parameter from pytest's --tb=line output."""
    return [match.group("reason").strip() for line in output.splitlines()
            if (match := _TB_LINE.match(line.strip()))]


def is_assertion_failure(reason):
    """Require positive assertion evidence, including message-bearing asserts.

    Inferring assertions from the absence of an Error/Exception suffix would
    accept custom exceptions such as BrokenVariant and builtins like SystemExit.
    """
    return bool(_ASSERTION.match(reason))


def main():
    original = SRC.read_bytes()
    text = original.decode("utf-8")
    errors, caught, survived = [], [], []

    try:
        real_names = collect_names()
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR collection: {exc}")
        return 1
    print(f"collected {len(real_names)} test names in {CLASS}")
    for mut in MUTATIONS:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(f"{mut['name']}: expected test {name!r} does not exist")
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    baseline = _pytest(TEST_FILE, *PYTEST_ARGS)
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
            result = _pytest(TEST_FILE, *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            SRC.write_bytes(original)
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            SRC.write_bytes(original)
        output = result.stdout + "\n" + result.stderr
        if "+++ Timeout +++" in output:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        if result.returncode not in (0, 1):
            errors.append(f"{mut['name']}: pytest returned {result.returncode}; no verdict")
            print("ERROR", errors[-1])
            continue
        stray = error_lines(output)
        if stray:
            errors.append(f"{mut['name']}: pytest reported errors: {stray[:3]}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(output)
        if result.returncode == 1:
            reasons = failure_reasons(output)
            failures = sum(line.startswith("FAILED ") for line in output.splitlines())
            # Missing output is not proof. Check all failures before checking
            # expected names, so an unrelated crash cannot read as a survivor.
            if not got or len(reasons) != failures:
                errors.append(f"{mut['name']}: incomplete failure reasons; no verdict")
                print("ERROR", errors[-1])
                continue
            crashed = [reason for reason in reasons if not is_assertion_failure(reason)]
            if crashed:
                errors.append(f"{mut['name']}: a test raised instead of failing its "
                              f"assertion: {sorted(set(crashed))}")
                print("ERROR", errors[-1])
                continue
        missing = [n for n in mut["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; failed tests were {sorted(got)}")
        else:
            caught.append(mut["name"])
            print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    print(f"scope: {len(MUTATIONS)} mutations, none skipped")
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
