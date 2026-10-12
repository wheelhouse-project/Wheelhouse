r"""Mutation gate for the "numeric" command (wh-pattern-variants-numeric.1).

Run it from services/wheelhouse. The first form runs the full sweep, the
second checks the patterns only, and the third runs a subset:

    .venv/Scripts/python.exe tests/mutation_gate_numeric_command.py
    .venv/Scripts/python.exe tests/mutation_gate_numeric_command.py --check
    .venv/Scripts/python.exe tests/mutation_gate_numeric_command.py --only=a,b

pytest is started directly with ``sys.executable``, never through ``uv run``,
so the gate cannot build a .venv inside a worktree.

Saying "numeric twenty three" types "23". The guard tests are in
tests/test_numeric_command.py and were written red-first; this gate proves
that each of them fails when the number conversion, or the pattern entry
that reaches it, is broken. Mutations, by target file:

speech/actions.py (the number conversion in number_digits):
  words-to-int-call-removed     The conversion is gone and the raw words are
                                typed: "numeric twenty three" types the words.
  period-added-like-number-point
                                The digits get a trailing period, as
                                number_point types them.
  number-off-by-one             The converted number is typed plus one.
  unreadable-arm-adds-a-period  The words typed when the conversion fails
                                gain the period number_point adds.
  unreadable-arm-types-nothing  The words typed when the conversion fails
                                are replaced by an empty insertion.
  zero-counts-as-unreadable     A converted zero (falsy) is read as a failed
                                conversion, so "numeric zero" types "zero".
  action-not-registered         number_digits is not in the function
                                registry, so the pattern entry names a
                                function that does not exist.

speech/config/patterns.toml (the pattern entry):
  trigger-word-changed          The entry matches another word.
  whole-utterance-flag-off      The entry no longer owns the whole
                                utterance, so a two-word number is cut at
                                the first word.
  capture-cannot-widen          The capture is written as [0-9]+, which the
                                loader does not widen, so number words match
                                nothing.
  entry-calls-number-point      The entry calls number_point, which adds a
                                period.

Each mutation runs tests/test_numeric_command.py (the pipeline tests run the
whole spoken path, so one selection holds every catcher). The runner runs the
unmutated source first and refuses to start unless it is green. It requires
each pattern to match exactly once, compiles every Python mutant before
writing it, clears the target modules' bytecode caches around every run,
sets PYTHONDONTWRITEBYTECODE=1 and COLUMNS=1000, gives each pytest run its
own timeout, writes each mutant inside the try whose finally restores the
source with write_bytes, which survives a second Ctrl+C, and reports as
ERRORS (never verdicts): pattern-not-found,
pattern-ambiguous, does-not-compile, timeout, suite-timeout abort, any
pytest return code other than 0 or 1, any ERROR line in the summary, an
expected test name that does not exist, and an expected test that failed for
any reason other than its own assertion.

The gate detects each target file's own line endings and translates the
patterns to them before matching. It never rewrites a file's endings.

Never run this gate and a test suite at the same time in this worktree: the
gate rewrites the target sources underneath any other run.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
ACT = SERVICE / "speech" / "actions.py"
PATTERNS = SERVICE / "speech" / "config" / "patterns.toml"
TARGETS = (ACT, PATTERNS)

SELECTION = ["tests/test_numeric_command.py"]

PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]
PER_RUN_TIMEOUT_S = 300

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

CONVERSION_BLOCK = """\
        num = words_to_int(number_word)
        if num is None:
            return self.insert_text(number_word)
        return self.insert_text(str(num))
"""

NO_CONVERSION = """\
        return self.insert_text(number_word)
"""

PERIOD_ADDED = """\
        num = words_to_int(number_word)
        if num is None:
            return self.insert_text(number_word)
        return self.insert_text(f"{num}.")
"""

OFF_BY_ONE = """\
        num = words_to_int(number_word)
        if num is None:
            return self.insert_text(number_word)
        return self.insert_text(str(num + 1))
"""

UNREADABLE_PERIOD = """\
        num = words_to_int(number_word)
        if num is None:
            return self.insert_text(f"{number_word}.")
        return self.insert_text(str(num))
"""

UNREADABLE_NOTHING = """\
        num = words_to_int(number_word)
        if num is None:
            return self.insert_text("")
        return self.insert_text(str(num))
"""

ZERO_UNREADABLE = """\
        num = words_to_int(number_word)
        if not num:
            return self.insert_text(number_word)
        return self.insert_text(str(num))
"""

REGISTRY_LINE = """\
        self._functions["number_digits"] = self.number_digits
"""

REGISTRY_GONE = """\
        pass
"""

ENTRY_TRIGGER = """\
pattern = '''^numeric (\\d+)$'''
"""

ENTRY_OTHER_TRIGGER = """\
pattern = '''^numerical (\\d+)$'''
"""

ENTRY_FLAG = """\
doc_id = "numeric-digits"
whole_utterance_only = true
"""

ENTRY_FLAG_OFF = """\
doc_id = "numeric-digits"
whole_utterance_only = false
"""

ENTRY_NO_WIDEN = """\
pattern = '''^numeric ([0-9]+)$'''
"""

ENTRY_ACTION = """\
    { function = "number_digits", params = ["g1"] }
"""

ENTRY_ACTION_POINT = """\
    { function = "number_point", params = ["g1"] }
"""

ALL_PIPELINE = [
    "test_numeric_twenty_three_types_23",
    "test_numeric_five_types_5",
]

MUTATIONS = [
    {
        "name": "words-to-int-call-removed",
        "src": ACT,
        "old": CONVERSION_BLOCK,
        "new": NO_CONVERSION,
        "expect": ALL_PIPELINE + [
            "test_word_number_becomes_digits",
            "test_no_period_is_added",
        ],
    },
    {
        "name": "period-added-like-number-point",
        "src": ACT,
        "old": CONVERSION_BLOCK,
        "new": PERIOD_ADDED,
        "expect": ALL_PIPELINE + [
            "test_numeric_digits_types_the_same_digits",
            "test_digit_string_stays_digits",
            "test_no_period_is_added",
        ],
    },
    {
        "name": "number-off-by-one",
        "src": ACT,
        "old": CONVERSION_BLOCK,
        "new": OFF_BY_ONE,
        "expect": ALL_PIPELINE + ["test_word_number_becomes_digits"],
    },
    {
        "name": "unreadable-arm-adds-a-period",
        "src": ACT,
        "old": CONVERSION_BLOCK,
        "new": UNREADABLE_PERIOD,
        "expect": ["test_unreadable_words_are_typed_as_spoken"],
    },
    {
        "name": "unreadable-arm-types-nothing",
        "src": ACT,
        "old": CONVERSION_BLOCK,
        "new": UNREADABLE_NOTHING,
        "expect": ["test_unreadable_words_are_typed_as_spoken"],
    },
    {
        "name": "zero-counts-as-unreadable",
        "src": ACT,
        "old": CONVERSION_BLOCK,
        "new": ZERO_UNREADABLE,
        "expect": ["test_the_edges_of_the_range"],
    },
    {
        "name": "action-not-registered",
        "src": ACT,
        "old": REGISTRY_LINE,
        "new": REGISTRY_GONE,
        "expect": ["test_action_is_registered"] + ALL_PIPELINE,
    },
    {
        "name": "trigger-word-changed",
        "src": PATTERNS,
        "old": ENTRY_TRIGGER,
        "new": ENTRY_OTHER_TRIGGER,
        "expect": ALL_PIPELINE + [
            "test_exactly_one_entry_with_the_agreed_fields",
        ],
    },
    {
        "name": "whole-utterance-flag-off",
        "src": PATTERNS,
        "old": ENTRY_FLAG,
        "new": ENTRY_FLAG_OFF,
        # The pipeline harness ends each utterance with an end marker, so
        # "numeric twenty three" still arrives whole without the flag and
        # the two-word test does not see it; the entry test and the
        # edges test (which speaks the hundreds) do.
        "expect": [
            "test_the_edges_of_the_range",
            "test_exactly_one_entry_with_the_agreed_fields",
        ],
    },
    {
        "name": "capture-cannot-widen",
        "src": PATTERNS,
        "old": ENTRY_TRIGGER,
        "new": ENTRY_NO_WIDEN,
        "expect": ALL_PIPELINE + [
            "test_exactly_one_entry_with_the_agreed_fields",
        ],
    },
    {
        "name": "entry-calls-number-point",
        "src": PATTERNS,
        "old": ENTRY_ACTION,
        "new": ENTRY_ACTION_POINT,
        "expect": ALL_PIPELINE + [
            "test_exactly_one_entry_with_the_agreed_fields",
        ],
    },
]
for _mut in MUTATIONS:
    _mut["selection"] = SELECTION


def _clear_pycache():
    """Drop the target modules' bytecode so a same-size mutant cannot be
    served from a cache compiled from the other version."""
    for target in TARGETS:
        cache = target.parent / "__pycache__"
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)


def _pytest(*extra):
    _clear_pycache()
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=PER_RUN_TIMEOUT_S,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "COLUMNS": "1000",
        },
    )


def _restore(src, original):
    """Put the original bytes back, surviving a second Ctrl+C.

    A first interrupt reaches the caller's finally; a second one landing
    inside this write would otherwise escape with the mutant still in a
    tracked file. Hold it, retry once, print any failure (an unreported
    mutant is the whole harm), clear bytecode, then re-raise the held
    interrupt.
    """
    held = None
    for _attempt in (1, 2):
        try:
            src.write_bytes(original)
            if src.read_bytes() == original:
                break
        except KeyboardInterrupt as exc:
            held = exc
        except OSError as exc:
            print(f"ERROR restore of {src.name} failed: {exc}")
    else:
        print(f"ERROR {src.name} may still hold a mutant; restore it by hand")
    _clear_pycache()
    if held is not None:
        raise held


def collect_names(selection):
    """Real test names in a selection, so a renamed test cannot read as a survivor."""
    out = _pytest(*selection, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def is_acceptable_failure(reason):
    """True for an assertion failure, false for a crash upstream of it."""
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
    """Every failure reason --tb=line printed, one per failing test."""
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


_ERROR_SUMMARY = re.compile(r"^ERROR\s+\S+\.py(::\S+)?(\s|$)")


def error_lines(output):
    """Summary lines for tests that ERRORED rather than failed."""
    return [line for line in output.splitlines() if _ERROR_SUMMARY.match(line)]


def _load_texts():
    originals = {path: path.read_bytes() for path in TARGETS}
    texts = {}
    for path, raw in originals.items():
        newline = "\r\n" if b"\r\n" in raw else "\n"
        texts[path] = (raw.decode("utf-8"), newline)
        print(
            f"line endings in {path.name}: "
            f"{'CRLF' if newline == chr(13) + chr(10) else 'LF'}"
        )
    for mut in MUTATIONS:
        newline = texts[mut["src"]][1]
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)
    return originals, texts


def _build_mutant(mut, texts):
    """Return (mutated_text, error) -- error names a stale, ambiguous, or
    non-compiling pattern, and is reported, never counted as a verdict."""
    src = mut["src"]
    text = texts[src][0]
    count = text.count(mut["old"])
    if count != 1:
        return None, f"{mut['name']}: pattern matched {count} times, expected 1"
    mutated = text.replace(mut["old"], mut["new"], 1)
    if src.suffix == ".py":
        try:
            compile(mutated, str(src), "exec")
        except SyntaxError as exc:
            return None, f"{mut['name']}: mutated source does not compile: {exc}"
    return mutated, None


def check_only():
    _originals, texts = _load_texts()
    stale = 0
    bad = 0
    for mut in MUTATIONS:
        _mutated, err = _build_mutant(mut, texts)
        if err is None:
            continue
        print("ERROR", err)
        if "does not compile" in err:
            bad += 1
        else:
            stale += 1
    print(
        f"checked {len(MUTATIONS)} patterns, {stale} stale, "
        f"{bad} that do not compile"
    )
    return 1 if stale or bad else 0


def main(argv):
    if "--check" in argv:
        return check_only()
    only = None
    for arg in argv:
        if arg.startswith("--only="):
            only = set(arg[len("--only="):].split(","))
    chosen = [m for m in MUTATIONS if only is None or m["name"] in only]
    if only is not None:
        unknown = only - {m["name"] for m in MUTATIONS}
        if unknown:
            print(f"ERROR unknown mutation names in --only: {sorted(unknown)}")
            return 1

    originals, texts = _load_texts()
    errors, caught, survived = [], [], []

    real_names = collect_names(SELECTION)
    print(f"collected {len(real_names)} test names in {', '.join(SELECTION)}")
    for mut in chosen:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: expected test {name!r} does not exist "
                    f"in its selection"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    baseline = _pytest(*SELECTION, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print(
            f"ERROR baseline is not green for {', '.join(SELECTION)}; "
            f"refusing to start"
        )
        print(baseline.stdout[-2000:])
        return 1
    print(f"baseline green: {', '.join(SELECTION)}")

    for mut in chosen:
        src = mut["src"]
        mutated, err = _build_mutant(mut, texts)
        if err is not None or mutated is None:
            errors.append(err or f"{mut['name']}: no mutant built")
            print("ERROR", errors[-1])
            continue
        try:
            # The write belongs inside this try, not before it. A Ctrl+C
            # that arrives during the write then reaches the finally
            # below, so no interrupt can leave the mutant in a tracked
            # file.
            src.write_bytes(mutated.encode("utf-8"))
            result = _pytest(*mut["selection"], *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            _restore(src, originals[src])
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
            print(
                f"SURVIVED {mut['name']}; missing {missing}; "
                f"failed tests were {sorted(got)}"
            )
            continue
        crashed = [r for r in failure_reasons(result.stdout)
                   if not is_acceptable_failure(r)]
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
    skipped = len(MUTATIONS) - len(chosen)
    print(
        f"scope: ran {len(chosen)} of {len(MUTATIONS)} mutations"
        + (f", skipped {skipped} by --only" if skipped else ", none skipped")
    )
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
