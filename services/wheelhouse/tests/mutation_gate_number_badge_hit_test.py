"""Mutation gate for the click hit test's private user32 binding
(wh-number-badge-problems).

Run it from services/wheelhouse with the service's own interpreter:

    .venv/Scripts/python.exe tests/mutation_gate_number_badge_hit_test.py
    .venv/Scripts/python.exe tests/mutation_gate_number_badge_hit_test.py --check

It proves that TestRootWindowAtPointLeavesTheSharedUser32Alone in
tests/test_utils/test_win_input_sender.py catches the defect it was written
for: ``root_window_at_point`` in utils/win_input_sender.py leaving a ctypes
signature on the process-shared ``ctypes.windll.user32`` function objects,
which made uiautomation's ``GetTopLevelControl`` raise on every later call
and refused every text insertion until restart.

  shared-binding                   The private binding IS the shared object,
                                   so the import-time signatures land on
                                   ``ctypes.windll.user32``. The object-level
                                   form of the defect.
  shared-get-ancestor-signature    The function puts the GetAncestor
                                   signature back on the shared object, as the
                                   original code did.
  shared-window-from-point-sig     The function puts a private POINT
                                   signature back on the shared
                                   WindowFromPoint, as the original code did.
  root-dropped                     The function reports 0 instead of the root,
                                   which the pywin32-oracle test must catch.

All four must be caught. Pattern-not-found, an ambiguous pattern, a mutation
that does not compile, a per-mutation timeout, a suite-timeout abort, and an
expected catcher that SKIPPED (the pywin32-oracle test skips on a host with
no coverable point) are reported as errors, never as a verdict. ``--check``
verifies every pattern matches exactly once in the current source and that
every mutant compiles, without running any test.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "utils" / "win_input_sender.py"
TEST_FILE = "tests/test_utils/test_win_input_sender.py"
CLASS = "TestRootWindowAtPointLeavesTheSharedUser32Alone"

# -v prints one line per test, which is what names a SKIPPED catcher; -rf
# keeps the FAILED summary lines failed_names() reads.
PYTEST_ARGS = ["-p", "no:randomly", "-v", "-rf"]
PER_MUTATION_TIMEOUT_S = 180

_ORIGINAL_ROOT_CALL = "    root = _HIT_TEST_USER32.GetAncestor(hwnd, _GA_ROOT)\n"
_ORIGINAL_POINT_CALL = (
    "    hwnd = _HIT_TEST_USER32.WindowFromPoint(wintypes.POINT(x, y))\n"
)

MUTATIONS = [
    {
        "name": "shared-binding",
        "old": '_HIT_TEST_USER32 = ctypes.WinDLL("user32", use_last_error=True)\n',
        "new": "_HIT_TEST_USER32 = ctypes.windll.user32\n",
        # The WindowFromPoint call-form test stays green here on purpose:
        # this mutant puts the fix's own wintypes.POINT signature on the
        # shared object, and input_proc.py passes a wintypes.POINT, so that
        # form is compatible. The two tests below still catch the mutant.
        "expect": [
            "test_the_uiautomation_get_ancestor_call_form_still_works_after_a_hit_test",
            "test_the_shared_function_objects_carry_no_signature_after_a_hit_test",
        ],
    },
    {
        "name": "shared-get-ancestor-signature",
        "old": _ORIGINAL_ROOT_CALL,
        "new": (
            "    user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]\n"
            "    user32.GetAncestor.restype = ctypes.c_void_p\n"
            + _ORIGINAL_ROOT_CALL
        ),
        "expect": [
            "test_the_uiautomation_get_ancestor_call_form_still_works_after_a_hit_test",
            "test_the_shared_function_objects_carry_no_signature_after_a_hit_test",
        ],
    },
    {
        "name": "shared-window-from-point-sig",
        "old": _ORIGINAL_POINT_CALL,
        "new": (
            "    class _POINT(ctypes.Structure):\n"
            '        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]\n'
            "    user32.WindowFromPoint.argtypes = [_POINT]\n"
            "    user32.WindowFromPoint.restype = ctypes.c_void_p\n"
            + _ORIGINAL_POINT_CALL
        ),
        "expect": [
            "test_the_input_proc_window_from_point_call_form_still_works_after_a_hit_test",
            "test_the_shared_function_objects_carry_no_signature_after_a_hit_test",
        ],
    },
    {
        "name": "root-dropped",
        "old": "    return int(root or hwnd or 0)\n",
        "new": "    return 0\n",
        "expect": ["test_the_hit_test_still_reports_the_root_window_at_the_point"],
    },
]


def _clear_bytecode():
    for cache in SERVICE.glob("utils/__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _pytest(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=PER_MUTATION_TIMEOUT_S,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def collect_names():
    """Real test names in CLASS, so a renamed test cannot read as a survivor."""
    out = _pytest(f"{TEST_FILE}::{CLASS}", "--collect-only", "-q", "-p", "no:randomly")
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


def skipped_names(output):
    """Test names on -v per-test lines that read ``...::name SKIPPED (...)``."""
    names = set()
    for line in output.splitlines():
        if "::" in line and " SKIPPED" in line:
            names.add(line.split("::")[-1].strip().split(" ")[0].split("[")[0])
    return names


def _newline_of(raw: bytes) -> str:
    return "\r\n" if b"\r\n" in raw else "\n"


def _build_mutant(text: str, newline: str, mut: dict):
    """Return (mutant_text, error). Exactly one match and a compiling result."""
    old = mut["old"].replace("\n", newline)
    new = mut["new"].replace("\n", newline)
    count = text.count(old)
    if count != 1:
        return None, f"{mut['name']}: pattern matched {count} times, expected 1"
    mutated = text.replace(old, new, 1)
    try:
        compile(mutated, str(SRC), "exec")
    except SyntaxError as exc:
        return None, f"{mut['name']}: mutated source does not compile: {exc}"
    return mutated, None


def check_only():
    raw = SRC.read_bytes()
    text = raw.decode("utf-8")
    newline = _newline_of(raw)
    stale, non_compiling = [], []
    for mut in MUTATIONS:
        mutant, error = _build_mutant(text, newline, mut)
        if error is None:
            continue
        if "does not compile" in error:
            non_compiling.append(error)
        else:
            stale.append(error)
        print("ERROR", error)
    print(
        f"checked {len(MUTATIONS)} patterns, {len(stale)} stale, "
        f"{len(non_compiling)} that do not compile"
    )
    return 1 if stale or non_compiling else 0


def main():
    if "--check" in sys.argv[1:]:
        return check_only()

    original = SRC.read_bytes()
    text = original.decode("utf-8")
    newline = _newline_of(original)
    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test names in {CLASS}")
    for mut in MUTATIONS:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(f"{mut['name']}: expected test {name!r} does not exist")
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    _clear_bytecode()
    baseline = _pytest(TEST_FILE, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    expected_all = {name for mut in MUTATIONS for name in mut["expect"]}
    skipped_catchers = sorted(skipped_names(baseline.stdout) & expected_all)
    if skipped_catchers:
        print(f"ERROR expected catchers skipped in the baseline: {skipped_catchers}")
        print("      a skipped catcher can never fail, so its mutation cannot be proven")
        return 1
    print("baseline green")

    for mut in MUTATIONS:
        mutated, error = _build_mutant(text, newline, mut)
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        _clear_bytecode()
        SRC.write_bytes(mutated.encode("utf-8"))
        try:
            result = _pytest(TEST_FILE, *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            SRC.write_bytes(original)
            _clear_bytecode()
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        skipped = [n for n in mut["expect"] if n in skipped_names(result.stdout)]
        if skipped:
            errors.append(f"{mut['name']}: expected catcher skipped: {skipped}")
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
