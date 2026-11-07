"""Mutation gate for criterion 6 of wh-escape-hides-overlay.

The change: "escape", "dismiss" and "press escape" press the Escape key and
then hide the numbers and the mouse grid. The doc_id "escape" row and the
press-keys row in speech/config/patterns.toml each gained a second step,
``escape_hides_numbers_and_grid`` (speech/actions.py). The guard tests are
the boundary journeys at the end of tests/test_overlay_journeys.py.

Run it from services/wheelhouse with the service interpreter:

    .venv/Scripts/python.exe tests/mutation_gate_escape_hides_overlay.py

Seven mutations:

  drop-hide-from-escape-row   Remove the hide step from the doc_id "escape"
                              row. The revert for "escape" and "dismiss".
  drop-hide-from-press-row    Remove the hide step from the press-keys row.
                              The revert for "press escape", "press escape
                              3 times" and "press esc".
  hide-before-the-key         Put the hide step BEFORE the press step in the
                              escape row. Breaks the step-order ruling only;
                              the key and the hide both still happen.
  press-hides-for-other-keys  INPUT-LEVEL. Drop the only-Escape test of the
                              parsed keys, so "press tab" and "press control
                              escape" start hiding.
  press-skips-a-count         INPUT-LEVEL. Refuse to hide when a count was
                              spoken (the scope before the boss's ruling of
                              2026-11-05 17:04), so "press escape 3 times"
                              stops hiding.
  press-skips-other-spellings INPUT-LEVEL. Refuse to hide unless the spoken
                              key words are exactly "escape", so "press esc"
                              stops hiding.
  hide-only-the-numbers       Drop the grid from the hides, so a shown grid
                              stays up and the nothing-shown sequence loses
                              its clear_grid.

"Caught" means every expected test (with its parameter id) failed on its own
assertion. Reported as errors, never as a verdict: pattern-not-found, an
ambiguous pattern, a mutant that does not parse (tomllib for patterns.toml,
compile for actions.py), a per-mutation timeout, a suite-timeout abort, a
pytest return code other than 0 or 1, an ERROR summary line, an expected
name that pytest does not collect, and an expected test that raised instead
of failing its assertion. Each target's own line endings are detected and
the patterns translated to them; restores use write_bytes.
"""

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
PATTERNS = SERVICE / "speech" / "config" / "patterns.toml"
ACTIONS = SERVICE / "speech" / "actions.py"
SELECTION = [
    "tests/test_overlay_journeys.py",
    "-k", "escape_command or other_press_commands",
]
PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

_NUM = "test_escape_command_with_numbers_shown_presses_escape_then_hides_them"
_GRID = "test_escape_command_with_grid_shown_presses_escape_then_hides_it"
_NONE = "test_escape_command_with_nothing_shown_presses_escape_quietly"
_OTHER = "test_other_press_commands_keep_the_numbers_and_the_grid"


def _cases(test, *ids):
    return [f"{test}[{case}]" for case in ids]


def _three_journeys(*ids):
    """The numbers, grid and nothing-shown journeys for each id."""
    return _cases(_NUM, *ids) + _cases(_GRID, *ids) + _cases(_NONE, *ids)


# Every press command whose only key is Escape (the boss's ruling of
# 2026-11-05 17:04), and every phrase under the rule.
_PRESS_ESCAPE = ("press-escape", "press-escape-3-times", "press-esc")
_ALL_ESCAPE = ("escape", "dismiss") + _PRESS_ESCAPE
_ONLY_ESCAPE_CHECK = (
    '            if not keys or any(key != "esc" for key in keys):\n'
)


_ESCAPE_ROW_ACTIONS = (
    'actions = [\n'
    '    { function = "press", params = ["esc"] },\n'
    '    { function = "escape_hides_numbers_and_grid" }\n'
    ']\n'
)
_PRESS_ROW_ACTIONS = (
    'actions = [\n'
    '    { function = "press_keys", params = ["g1", "g2"] },\n'
    '    { function = "escape_hides_numbers_and_grid", params = ["g1", "g2"] }\n'
    ']\n'
)

MUTATIONS = [
    {
        "name": "drop-hide-from-escape-row",
        "file": PATTERNS,
        "old": _ESCAPE_ROW_ACTIONS,
        "new": 'actions = [\n    { function = "press", params = ["esc"] }\n]\n',
        "expect": _three_journeys("escape", "dismiss"),
    },
    {
        "name": "drop-hide-from-press-row",
        "file": PATTERNS,
        "old": _PRESS_ROW_ACTIONS,
        "new": (
            'actions = [\n'
            '    { function = "press_keys", params = ["g1", "g2"] }\n'
            ']\n'
        ),
        "expect": _three_journeys(*_PRESS_ESCAPE),
    },
    {
        # A move, not a copy: the press step is still there exactly once.
        "name": "hide-before-the-key",
        "file": PATTERNS,
        "old": _ESCAPE_ROW_ACTIONS,
        "new": (
            'actions = [\n'
            '    { function = "escape_hides_numbers_and_grid" },\n'
            '    { function = "press", params = ["esc"] }\n'
            ']\n'
        ),
        "expect": _three_journeys("escape", "dismiss"),
    },
    {
        "name": "press-hides-for-other-keys",
        "file": ACTIONS,
        "old": _ONLY_ESCAPE_CHECK,
        "new": "            if not keys:\n",
        "expect": _cases(_OTHER, "press-tab", "press-control-escape"),
    },
    {
        "name": "press-skips-a-count",
        "file": ACTIONS,
        "old": _ONLY_ESCAPE_CHECK,
        "new": (
            '            if (not keys or any(key != "esc" for key in keys)\n'
            "                    or repeat_str is not None):\n"
        ),
        "expect": _three_journeys("press-escape-3-times"),
    },
    {
        "name": "press-skips-other-spellings",
        "file": ACTIONS,
        "old": _ONLY_ESCAPE_CHECK,
        "new": (
            '            if (not keys or any(key != "esc" for key in keys)\n'
            '                    or key_sequence.strip().lower() != "escape"):\n'
        ),
        "expect": _three_journeys("press-esc"),
    },
    {
        "name": "hide-only-the-numbers",
        "file": ACTIONS,
        "old": (
            '            ("numbers", self.hide_overlay_command),\n'
            '            ("grid", self.grid_dismiss_command),\n'
        ),
        "new": '            ("numbers", self.hide_overlay_command),\n',
        "expect": _cases(_GRID, *_ALL_ESCAPE) + _cases(_NONE, *_ALL_ESCAPE),
    },
]


def _env():
    return {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "COLUMNS": "1000",
    }


def _pytest(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=300,
        env=_env(),
    )


def _clear_pycache():
    for cache in (SERVICE / "speech").rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def collect_names():
    """Collected test ids (name plus parameter id) in the selection."""
    out = _pytest(*SELECTION, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split(" ")[0])
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
            names.add(line.split("::")[-1].strip().split(" ")[0])
    return names


def failure_reasons(output):
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


def error_lines(output):
    """ERROR lines in the short summary only (after its header)."""
    lines = output.splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines)
            if "short test summary info" in line
        )
    except StopIteration:
        return []
    return [line for line in lines[start:] if line.startswith("ERROR ")]


def _parses(path, text):
    if path.suffix == ".toml":
        tomllib.loads(text)
    else:
        compile(text, str(path), "exec")


def main():
    originals = {p: p.read_bytes() for p in {m["file"] for m in MUTATIONS}}
    for mut in MUTATIONS:
        raw = originals[mut["file"]]
        newline = "\r\n" if b"\r\n" in raw else "\n"
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)

    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test ids in the selection")
    for mut in MUTATIONS:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: expected test {name!r} is not collected"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    _clear_pycache()
    baseline = _pytest(*SELECTION, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print("baseline green")

    for mut in MUTATIONS:
        path = mut["file"]
        original = originals[path]
        text = original.decode("utf-8")
        count = text.count(mut["old"])
        if count != 1:
            errors.append(
                f"{mut['name']}: pattern matched {count} times in "
                f"{path.name}, expected 1"
            )
            print("ERROR", errors[-1])
            continue
        mutated = text.replace(mut["old"], mut["new"], 1)
        try:
            _parses(path, mutated)
        except (tomllib.TOMLDecodeError, SyntaxError) as exc:
            errors.append(f"{mut['name']}: mutant does not parse: {exc}")
            print("ERROR", errors[-1])
            continue
        path.write_bytes(mutated.encode("utf-8"))
        _clear_pycache()
        try:
            result = _pytest(*SELECTION, *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            path.write_bytes(original)
            _clear_pycache()
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
                f"SURVIVED {mut['name']}; missing {missing}; failed {sorted(got)}"
            )
            continue
        crashed = [
            r for r in failure_reasons(result.stdout)
            if not is_assertion_failure(r)
        ]
        if crashed:
            errors.append(
                f"{mut['name']}: a test raised instead of failing its "
                f"assertion: {sorted(set(crashed))}"
            )
            print("ERROR", errors[-1])
            continue
        caught.append(mut["name"])
        print(f"caught   {mut['name']} by {len(got)} tests: {sorted(got)}")

    for path, original in originals.items():
        if path.read_bytes() != original:
            errors.append(f"{path.name} differs from its original after the run")
            print("ERROR", errors[-1])

    print()
    print(f"scope: {len(MUTATIONS)} mutations, none skipped")
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
