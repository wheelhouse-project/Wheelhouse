"""Mutation gate for the spoken scroll count fixes in speech/actions.py.

Two fixes, both found by codex review of wh-voice-access-parity.2.3:
the explicit-zero rule (wh-voice-access-parity.2.3.2.1) and the
oversized-digit guard in words_to_int (wh-voice-access-parity.2.3.2.6).

crewcut: the file name still says "scroll_zero_count", which now names only
the first of the two fixes. Renaming a committed gate mid-review costs more
churn than it saves; rename it to mutation_gate_scroll_counts.py the next
time this file is touched for another reason.

Run it from services/wheelhouse:

    python tests/mutation_gate_scroll_zero_count.py

Codex filed the defect: "scroll down 0" and "scroll down zero" each sent one
notch, because ``ActionFunctions.scroll`` only overwrote ``clicks`` when the
parsed count was greater than zero, leaving the one-notch default in place. The
guard tests were written after the fix, so this gate is what proves they can
still see the defect.

Five mutations of speech/actions.py -- three of the count block in
ActionFunctions.scroll, two of the digit conversion in words_to_int:

  revert-the-fix   Restore the exact pre-fix condition ``parsed is not None and
                   parsed > 0``. This IS the defect: an explicit zero falls
                   through to the one-notch default.
  boundary-shifts  Change ``parsed <= 0`` to ``parsed < 0``. The fix is still
                   structurally present, so a revert-only gate would pass; this
                   is the input-level check the mutation-gate skill requires.
                   Zero then reaches ``min(0, MAX_SCROLL_CLICKS)`` and the
                   payload carries ``clicks: 0`` instead of being withheld.
  zero-eats-junk   Conflate "unreadable count" with "explicit zero", so a word
                   that is not a number returns None as well. This pins the
                   OTHER half of the rule: "banana" must still fall back to one
                   notch, because that is a recognition failure rather than an
                   instruction.
  oversize-revert  Re-raise instead of returning None, so the digit-count
                   ValueError escapes words_to_int again exactly as it did
                   before the fix. The guard tests catch that ValueError and
                   assert on it rather than letting it propagate, so this
                   mutation still fails an assertion rather than crashing.
  oversize-returns-one
                   Keep the guard, still catch the ValueError, but return 1
                   instead of None. The fix is structurally present, so a
                   revert-only gate would pass; this is the input-level check
                   the mutation-gate skill requires for the second fix. Only
                   the words_to_int test can see it: scroll clamps a returned
                   1 to one notch, which is what its own test expects anyway.

All five must be caught, and "caught" means the expected test failed on its
own assertion. A mutant that makes the test raise an exception instead has
proved nothing: the test never reached the assertion the mutation claims to
break. So this gate reads the reason pytest prints after each FAILED name and
refuses to count an exception as a catch.

Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern, a mutation that does not compile, a per-mutation timeout, a
suite-timeout abort, any pytest return code other than 0 or 1, any ERROR line
in the summary, and an expected test that failed for any reason other than its
own assertion.

The gate detects the target file's own line endings and translates each pattern
to them before matching, so a future CRLF conversion of speech/actions.py cannot
turn every multi-line pattern into a silent pattern-not-found batch. It never
rewrites the file's endings.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "speech" / "actions.py"
# Two files: the scroll payload rules live in the first, and the
# words_to_int conversion contract in the second.
TEST_FILES = ["tests/test_scroll_action.py", "tests/test_actions.py"]

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
        "name": "revert-the-fix",
        "old": """            if parsed is not None:
                if parsed <= 0:
                    logger.debug(
                        "scroll: count %r means no notches; sending nothing",
                        count,
                    )
                    return None
                clicks = min(parsed, MAX_SCROLL_CLICKS)
""",
        "new": """            if parsed is not None and parsed > 0:
                clicks = min(parsed, MAX_SCROLL_CLICKS)
""",
        "expect": ["test_an_explicit_zero_sends_nothing"],
    },
    {
        "name": "boundary-shifts",
        "old": "                if parsed <= 0:\n",
        "new": "                if parsed < 0:\n",
        "expect": ["test_an_explicit_zero_sends_nothing"],
    },
    {
        "name": "zero-eats-junk",
        "old": """            if parsed is not None:
                if parsed <= 0:
                    logger.debug(
                        "scroll: count %r means no notches; sending nothing",
                        count,
                    )
                    return None
                clicks = min(parsed, MAX_SCROLL_CLICKS)
""",
        "new": """            if parsed is None or parsed <= 0:
                logger.debug(
                    "scroll: count %r means no notches; sending nothing",
                    count,
                )
                return None
            clicks = min(parsed, MAX_SCROLL_CLICKS)
""",
        "expect": ["test_a_count_that_is_not_a_number_falls_back_to_one_notch"],
    },
    {
        # Re-raise instead of reporting None. That is exactly the pre-fix
        # behaviour: the digit-count ValueError Python raises above
        # sys.get_int_max_str_digits() escapes words_to_int again. Anchored
        # on the line that follows so the pattern cannot also match the
        # function's final "return None".
        "name": "oversize-revert",
        "old": """            return None
    return parse_number_word(text, aliases=True, zero=True)
""",
        "new": """            raise
    return parse_number_word(text, aliases=True, zero=True)
""",
        "expect": [
            "test_a_digit_string_too_long_to_convert_returns_none",
            "test_a_count_too_long_to_convert_falls_back_to_one_notch",
        ],
    },
    {
        # Input-level: the guard is structurally present and still catches
        # the ValueError, but reports the wrong answer. A revert-only gate
        # would pass this, which is why the mutation-gate skill asks for it.
        # Only the words_to_int test can see it: scroll clamps a returned 1
        # to one notch, which is what its own test already expects.
        "name": "oversize-returns-one",
        "old": """                len(text),
            )
            return None
""",
        "new": """                len(text),
            )
            return 1
""",
        "expect": ["test_a_digit_string_too_long_to_convert_returns_none"],
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


# Captured application logs use logger:file.py:line, not pytest node IDs.
_ERROR_SUMMARY = re.compile(r"^ERROR\s+\S+\.py(::\S+)?(\s|$)")


def error_lines(output):
    """Summary lines for tests that ERRORED rather than failed."""
    return [line for line in output.splitlines() if _ERROR_SUMMARY.match(line)]


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
