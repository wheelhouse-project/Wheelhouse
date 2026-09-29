"""Mutation gate for the first-paste clipboard selection guard tests.

Proves tests/test_utils/test_clipboard_manager.py::TestSelectWindowsClipboard
and tests/test_first_paste_start.py fail for the right reason when the
protected behavior breaks (wh-input-first-paste-stall): each WheelHouse
process selects pyperclip's Windows clipboard at start, so the first copy()
or paste() runs no OS detection and starts no child process. Run from
services/wheelhouse:

    .venv/Scripts/python.exe tests/mutation_gate_first_paste.py
    .venv/Scripts/python.exe tests/mutation_gate_first_paste.py --check

--check validates patterns (exactly one match each) and compiles every
mutant without collecting tests or writing target files.

Each mutation: read bytes, require exactly one match for every edit
(translated to the file's own line endings), compile the mutant, write it
atomically, clear __pycache__, run the two test files with
PYTHONDONTWRITEBYTECODE=1, COLUMNS=1000 and a per-mutation timeout, restore
bytes in a finally block. The verdict comes from pytest's exit code first
(0 means survived), then from the short test summary only: every expected
test must be listed as FAILED and its reason must match the expected
assertion, so an unrelated crash is never read as a catch. Pattern-not-found,
ambiguous patterns, non-compiling mutants, timeouts, suite-timeout aborts,
unreadable summaries and wrong-reason failures are ERRORS, never verdicts.
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parents[1]
PYTHON = SERVICE / ".venv" / "Scripts" / "python.exe"
TEST_FILES = [
    "tests/test_utils/test_clipboard_manager.py",
    "tests/test_first_paste_start.py",
]
PER_MUTATION_TIMEOUT_S = 180

# Reason texts from pytest's short summary. The paste test's own forbidden
# stand-in raises this message, so a catch proves pyperclip's OS detection
# ran (platform.system() was called), not that something upstream crashed.
_DETECTION_RAN = r"AssertionError: OS detection ran after the start step"
_CALL_MISSING = r"assert 'select_windows_clipboard' in \["
_ORDER_WRONG = r"assert \d+ < \d+"

_INPUT_SELECT_BLOCK = (
    "        # Before the command loop, so the first paste starts no child\n"
    "        # process for pyperclip's OS detection (wh-input-first-paste-stall).\n"
    "        select_windows_clipboard()\n"
    "\n"
)
_INPUT_UI_HANDLER = (
    "        ui_handler = UIActionHandler(\n"
    "            response_queue, config, shutdown_event=shutdown_event,\n"
    "        )\n"
)

MUTATIONS = [
    {
        "name": "helper-skips-set-clipboard",
        "file": SERVICE / "utils" / "clipboard_manager.py",
        "edits": [(
            '        pyperclip.set_clipboard("windows")\n',
            "        pass\n",
        )],
        "expect": {
            "test_paste_after_start_step_runs_no_os_detection": _DETECTION_RAN,
        },
    },
    {
        # The helper's except arm swallows a failure before the call, so the
        # lazy stubs stay in place: the start step "runs" and selects nothing.
        "name": "helper-swallows-before-call",
        "file": SERVICE / "utils" / "clipboard_manager.py",
        "edits": [(
            '    try:\n'
            '        pyperclip.set_clipboard("windows")\n',
            '    try:\n'
            '        raise RuntimeError("mutation gate")\n'
            '        pyperclip.set_clipboard("windows")\n',
        )],
        "expect": {
            "test_paste_after_start_step_runs_no_os_detection": _DETECTION_RAN,
        },
    },
    {
        "name": "input-drops-selection",
        "file": SERVICE / "input_proc.py",
        "edits": [(
            "        select_windows_clipboard()\n",
            "        pass\n",
        )],
        # The order test's next() then finds no selection: StopIteration,
        # which is a crash, not the order assertion. Only the presence test
        # is the expected catcher here.
        "expect": {
            "test_input_entry_selects_windows_clipboard": _CALL_MISSING,
        },
    },
    {
        "name": "logic-drops-selection",
        "file": SERVICE / "main.py",
        "edits": [(
            "    select_windows_clipboard()\n",
            "    pass\n",
        )],
        "expect": {
            "test_logic_entry_selects_windows_clipboard": _CALL_MISSING,
        },
    },
    {
        "name": "gui-drops-selection",
        "file": SERVICE / "gui.py",
        "edits": [(
            "    select_windows_clipboard()\n",
            "    pass\n",
        )],
        "expect": {
            "test_gui_entry_selects_windows_clipboard": _CALL_MISSING,
        },
    },
    {
        # A MOVE, never a copy: the selection leaves its place before the
        # UIActionHandler and lands right after it.
        "name": "input-selection-moves-after-ui-handler",
        "file": SERVICE / "input_proc.py",
        "edits": [
            (_INPUT_SELECT_BLOCK, ""),
            (
                _INPUT_UI_HANDLER,
                _INPUT_UI_HANDLER + "        select_windows_clipboard()\n",
            ),
        ],
        "expect": {
            "test_input_selection_precedes_ui_handler": _ORDER_WRONG,
        },
    },
]


def _newline(data: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in data else b"\n"


def _build_mutant(mutation, data):
    """Apply every edit in order. Returns (mutant, None) or (None, error)."""
    newline = _newline(data)
    mutated = data
    for index, (old_text, new_text) in enumerate(mutation["edits"], 1):
        old = old_text.encode("utf-8").replace(b"\n", newline)
        new = new_text.encode("utf-8").replace(b"\n", newline)
        count = mutated.count(old)
        if count == 0:
            return None, f"edit {index}: pattern not found"
        if count != 1:
            return None, f"edit {index}: pattern ambiguous ({count} matches)"
        mutated = mutated.replace(old, new, 1)
    return mutated, None


def _compile_error(mutated: bytes, path: Path):
    try:
        compile(mutated.decode("utf-8"), str(path), "exec")
    except SyntaxError as e:
        return f"mutant does not compile: {e}"
    return None


def _publish(tmp: Path, path: Path):
    """Replace path with tmp, retrying a brief sharing refusal (WinError 5)."""
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.3)


def _write_atomic(path: Path, data: bytes):
    tmp = path.with_name(path.name + ".mutation-gate.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _publish(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _journal_path(path: Path) -> Path:
    return path.with_name(path.name + ".gate-journal")


def _write_journal(path, original, mutated):
    """Persist original and mutant so a killed run can be undone next time."""
    payload = str(len(original)).encode("ascii") + b"\n" + original + mutated
    _write_atomic(_journal_path(path), payload)


def _clear_journal(path):
    try:
        _journal_path(path).unlink()
    except FileNotFoundError:
        pass


def _recover_pending(paths):
    """Undo any mutant a killed prior run left behind. Returns errors."""
    errors = []
    for path in sorted(set(paths)):
        journal = _journal_path(path)
        if not journal.exists():
            continue
        try:
            header, rest = journal.read_bytes().split(b"\n", 1)
            length = int(header)
            original, mutated = rest[:length], rest[length:]
            current = path.read_bytes()
        except (OSError, ValueError) as e:
            errors.append(f"{path}: unreadable recovery journal: {e}")
            continue
        if current == mutated and current != original:
            _write_atomic(path, original)
            _clear_journal(path)
            print(f"recovered {path} from an interrupted prior run")
        elif current == original:
            _clear_journal(path)
            print(f"dropped stale journal for {path} (target already original)")
        else:
            errors.append(
                f"{path}: journal exists but the target matches neither the "
                "recorded original nor the mutant; reconcile by hand"
            )
    return errors


def _clear_pycache():
    for d in SERVICE.rglob("__pycache__"):
        if ".venv" not in d.parts:
            shutil.rmtree(d, ignore_errors=True)


def _restore_once(path, original, mutated, name):
    current = path.read_bytes()
    if current == original:
        return None
    if current != mutated:
        return (f"{name}: {path} changed while pytest ran; refusing to "
                "overwrite the concurrent edit -- reconcile by hand")
    _write_atomic(path, original)
    return None


def _restore_source(path, original, mutated, name):
    """Put the pre-run bytes back. Returns (error or None, held interrupt).

    A Ctrl+C landing inside the restore is held, the restore is retried
    once, and the caller re-raises the interrupt only after clearing the
    bytecode caches, so a mutant never stays in source or in __pycache__.
    """
    held = None
    for attempt in range(2):
        try:
            return _restore_once(path, original, mutated, name), held
        except KeyboardInterrupt as e:
            held = e
        except OSError as e:
            if attempt == 1:
                return (f"{name}: restore failed; the MUTANT REMAINS in "
                        f"{path}: {e}"), held
    return (f"{name}: restore interrupted twice; the MUTANT MAY REMAIN in "
            f"{path}"), held


def _pytest_env():
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COLUMNS="1000")


def _run_pytest():
    return subprocess.run(
        [str(PYTHON), "-m", "pytest", *TEST_FILES, "-rfE", "-q",
         "-p", "no:randomly"],
        cwd=SERVICE, env=_pytest_env(), capture_output=True, text=True,
        timeout=PER_MUTATION_TIMEOUT_S,
    )


def _short_summary(out: str):
    """Return the short test summary lines, or None when absent."""
    lines = out.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^=+ short test summary info =+$", line):
            start = i + 1
            break
    if start is None:
        return None
    body = []
    for line in lines[start:]:
        if line.startswith("="):
            break
        body.append(line)
    return body


def _failed_reasons(summary):
    """Map test name to reason for FAILED lines; list ERROR lines apart."""
    failed, errored = {}, []
    for line in summary:
        m = re.match(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$", line)
        if not m:
            continue
        if m.group(1) == "ERROR":
            errored.append(line)
            continue
        test = re.sub(r"\[.*\]$", "", m.group(2).rsplit("::", 1)[-1])
        failed[test] = m.group(3) or ""
    return failed, errored


def check_only() -> int:
    stale = ambiguous = broken = 0
    for m in MUTATIONS:
        data = m["file"].read_bytes()
        mutated, err = _build_mutant(m, data)
        if err:
            if "not found" in err:
                stale += 1
                print(f"STALE {m['name']}: {err} in {m['file']}")
            else:
                ambiguous += 1
                print(f"AMBIGUOUS {m['name']}: {err} in {m['file']}")
            continue
        cerr = _compile_error(mutated, m["file"])
        if cerr:
            broken += 1
            print(f"BROKEN {m['name']}: {cerr}")
    print(f"checked {len(MUTATIONS)} patterns, {stale} stale, "
          f"{ambiguous} ambiguous, {broken} that do not compile")
    return 1 if stale or ambiguous or broken else 0


def main() -> int:
    if "--check" in sys.argv[1:]:
        return check_only()

    recovery_errors = _recover_pending(m["file"] for m in MUTATIONS)
    if recovery_errors:
        for e in recovery_errors:
            print(f"ERROR {e}")
        return 1

    # Every expected catcher must exist before the first mutation.
    collect = subprocess.run(
        [str(PYTHON), "-m", "pytest", *TEST_FILES, "--collect-only", "-q",
         "-p", "no:randomly"],
        cwd=SERVICE, env=_pytest_env(), capture_output=True, text=True,
        timeout=PER_MUTATION_TIMEOUT_S,
    )
    real_names = {
        re.sub(r"\[.*\]$", "", line.split(" ")[0].rsplit("::", 1)[-1])
        for line in collect.stdout.splitlines() if "::" in line
    }
    errors = [
        f"{m['name']}: expected test {name} does not exist"
        for m in MUTATIONS for name in m["expect"] if name not in real_names
    ]
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    base = _run_pytest()
    if base.returncode != 0:
        print("ERROR baseline run is not green; refusing to mutate")
        print((base.stdout + base.stderr)[-3000:])
        return 1
    print("baseline green")

    survivors = []
    caught = 0
    for m in MUTATIONS:
        name, path = m["name"], m["file"]
        data = path.read_bytes()
        mutated, err = _build_mutant(m, data)
        if err is None:
            err = _compile_error(mutated, path)
        if err:
            errors.append(f"{name}: {err}")
            print(f"ERROR {name}: {err}")
            continue
        _clear_pycache()
        _write_journal(path, data, mutated)
        _write_atomic(path, mutated)
        timed_out = False
        run = None
        held = None
        try:
            run = _run_pytest()
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            restore_error, held = _restore_source(path, data, mutated, name)
            _clear_pycache()
            if restore_error:
                print(f"ERROR {restore_error}")
            if held is not None:
                raise held
        if restore_error:
            errors.append(restore_error)
            print("aborting remaining mutations; the recovery journal is kept")
            break
        _clear_journal(path)

        if timed_out:
            errors.append(f"{name}: pytest timed out")
            print(f"ERROR {name}: pytest timed out after "
                  f"{PER_MUTATION_TIMEOUT_S}s")
            continue
        out = run.stdout + run.stderr
        if "+++ Timeout +++" in out:
            errors.append(f"{name}: suite-timeout-abort")
            print(f"ERROR {name}: suite-timeout-abort")
            continue
        if run.returncode == 0:
            survivors.append(name)
            print(f"SURVIVED {name}: no test failed "
                  f"(expected {sorted(m['expect'])})")
            continue
        summary = _short_summary(out)
        if summary is None:
            errors.append(f"{name}: exit {run.returncode} with no short summary")
            print(f"ERROR {name}: exit {run.returncode} with no short summary")
            print(out[-2000:])
            continue
        failed, errored = _failed_reasons(summary)
        if errored:
            errors.append(f"{name}: pytest reported errors: {errored}")
            print(f"ERROR {name}: pytest reported errors: {errored}")
            continue
        missing = [t for t in m["expect"] if t not in failed]
        if missing:
            survivors.append(name)
            print(f"SURVIVED {name}: expected {missing} to fail; "
                  f"failed={sorted(failed)}")
            continue
        wrong = {
            t: failed[t] for t, pattern in m["expect"].items()
            if not re.search(pattern, failed[t])
        }
        if wrong:
            errors.append(f"{name}: wrong failure reason {wrong}")
            print(f"ERROR {name}: failed for the wrong reason: {wrong}")
            continue
        caught += 1
        for t in m["expect"]:
            print(f"caught {name} by {t}: {failed[t][:160]}")
        others = sorted(set(failed) - set(m["expect"]))
        if others:
            print(f"    also failed: {others}")

    print(f"scope: ran {len(MUTATIONS)} of {len(MUTATIONS)} mutations "
          f"(full set for this gate)")
    print(f"summary: {caught}/{len(MUTATIONS)} caught, {len(survivors)} "
          f"survivors, {len(errors)} errors")
    return 1 if (survivors or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
