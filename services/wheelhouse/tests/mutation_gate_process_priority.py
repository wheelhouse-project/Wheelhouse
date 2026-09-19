"""Mutation gate for the process-priority elevation guard tests.

Proves tests/test_process_priority.py fails for the right reason when the
protected behavior breaks (wh-process-priority-durable). Run from
services/wheelhouse:

    uv run python tests/mutation_gate_process_priority.py

Each mutation: read bytes, require exactly one pattern match (translated to
the file's own line endings), compile the mutant, write it atomically through a same-directory
temp file, clear
__pycache__, run the test file with PYTHONDONTWRITEBYTECODE=1 and a timeout,
restore bytes in a finally block. Pattern-not-found, ambiguous patterns,
non-compiling mutants, timeouts, and suite-timeout aborts are ERRORS, never
verdicts.
"""
# --check validates patterns and Python syntax without collecting tests,
# recovering pending mutations, or writing target files.
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/test_process_priority.py"

MUTATIONS = [
    {
        "name": "high-class-becomes-below-normal",
        "file": SERVICE / "utils" / "process_priority.py",
        "old": "psutil.Process().nice(psutil.HIGH_PRIORITY_CLASS)",
        "new": "psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)",
        "expect": ["test_round_trip_sets_high_priority_class"],
    },
    {
        "name": "failure-reports-success",
        "file": SERVICE / "utils" / "process_priority.py",
        "old": (
            '        logger.warning(\n'
            '            "[priority] process priority elevation failed: %s", e\n'
            '        )\n'
            '        return False'
        ),
        "new": (
            '        logger.warning(\n'
            '            "[priority] process priority elevation failed: %s", e\n'
            '        )\n'
            '        return True'
        ),
        "expect": [
            "test_os_refusal_returns_false",
            "test_unexpected_error_returns_false",
        ],
    },
    {
        "name": "launcher-drops-elevation",
        "file": SERVICE / "launcher.py",
        "old": "    elevate_process_priority()",
        "new": "    pass",
        "expect": ["test_launcher_supervisor_elevates"],
    },
    {
        "name": "logic-drops-elevation",
        "file": SERVICE / "main.py",
        "old": "    elevate_process_priority()",
        "new": "    pass",
        "expect": ["test_logic_entry_elevates"],
    },
    {
        # 8-space patterns: the input call sits inside the entry try block
        # after the .1.4 reorder, one level deeper than the other entries.
        "name": "input-drops-elevation",
        "file": SERVICE / "input_proc.py",
        "old": "        elevate_process_priority()",
        "new": "        pass",
        "expect": ["test_input_entry_elevates"],
    },
    {
        "name": "gui-drops-elevation",
        "file": SERVICE / "gui.py",
        "old": "    elevate_process_priority()",
        "new": "    pass",
        "expect": ["test_gui_entry_elevates"],
    },
    # Relocation into a dead branch keeps the call lexically present inside
    # the entry function but off the execution path; the guard test must
    # still fail (wh-process-priority-durable.1.2).
    {
        "name": "launcher-relocates-elevation-to-dead-branch",
        "file": SERVICE / "launcher.py",
        "old": "    elevate_process_priority()",
        "new": "    if False:\n        elevate_process_priority()",
        "expect": ["test_launcher_supervisor_elevates"],
    },
    {
        "name": "logic-relocates-elevation-to-dead-branch",
        "file": SERVICE / "main.py",
        "old": "    elevate_process_priority()",
        "new": "    if False:\n        elevate_process_priority()",
        "expect": ["test_logic_entry_elevates"],
    },
    {
        "name": "input-relocates-elevation-to-dead-branch",
        "file": SERVICE / "input_proc.py",
        "old": "        elevate_process_priority()",
        "new": "        if False:\n            elevate_process_priority()",
        "expect": ["test_input_entry_elevates"],
    },
    {
        "name": "gui-relocates-elevation-to-dead-branch",
        "file": SERVICE / "gui.py",
        "old": "    elevate_process_priority()",
        "new": "    if False:\n        elevate_process_priority()",
        "expect": ["test_gui_entry_elevates"],
    },
    # An elevation call inserted before setup_logging would log a refusal
    # to bare stderr only; the order guard must fail
    # (wh-process-priority-durable.1.4). The inserted call is unbound at
    # that point in main.py, which does not matter: the guard tests parse
    # the source, they never run the entry functions.
    {
        "name": "logic-elevates-before-logging-setup",
        "file": SERVICE / "main.py",
        "old": "    setup_logging(config_service)",
        "new": "    elevate_process_priority()\n    setup_logging(config_service)",
        "expect": ["test_logic_elevation_follows_logging_setup"],
    },
    {
        "name": "input-elevates-before-logging-setup",
        "file": SERVICE / "input_proc.py",
        "old": "        setup_logging(config)",
        "new": "        elevate_process_priority()\n        setup_logging(config)",
        "expect": ["test_input_elevation_follows_logging_setup"],
    },
    {
        "name": "gui-elevates-before-logging-setup",
        "file": SERVICE / "gui.py",
        "old": "    setup_logging(config)",
        "new": "    elevate_process_priority()\n    setup_logging(config)",
        "expect": ["test_gui_elevation_follows_logging_setup"],
    },
]


def _publish_durable(tmp, path):
    """Replace path with tmp so the rename itself is on disk on return.

    os.replace maps to MoveFileExW with MOVEFILE_REPLACE_EXISTING and no
    write-through, so a power loss after it returns can lose the rename
    even though the temp file's bytes were fsynced; a journal lost that
    way makes the next invocation's recovery silently skip a still-mutated
    target (wh-process-priority-durable.1.9). MOVEFILE_WRITE_THROUGH makes
    the call wait until the move is on disk. Non-Windows falls back to
    os.replace; these gates only run on Windows, where the targets live.
    """
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.MoveFileExW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
        ]
        kernel32.MoveFileExW.restype = ctypes.c_int
        MOVEFILE_REPLACE_EXISTING = 0x1
        MOVEFILE_WRITE_THROUGH = 0x8
        if not kernel32.MoveFileExW(
            str(tmp), str(path),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        ):
            raise ctypes.WinError()
    else:
        os.replace(tmp, path)


def _write_atomic(path, data):
    """Write data to path so that a partial write can never land in path.

    A same-directory temporary file takes the write; a durable rename swaps
    it in only after a complete, flushed write. A storage failure mid-write
    (a volume filling, a sharing refusal) therefore leaves path holding
    whatever it held before (wh-process-priority-durable.1.6). Every gate
    publish -- journal, mutant, restore -- goes through _publish_durable so
    no ordering between a restore and a journal deletion can invert across
    a power loss (wh-process-priority-durable.1.9).
    """
    tmp = path.with_name(path.name + ".gate-tmp")
    try:
        with open(tmp, "wb") as f:
            written = f.write(data)
            if written != len(data):
                raise OSError(
                    f"short write: {written} of {len(data)} bytes"
                )
            f.flush()
            os.fsync(f.fileno())
        _publish_durable(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _journal_path(path):
    return path.with_name(path.name + ".gate-journal")


def _write_journal(path, original, mutated):
    """Persist the original and mutant bytes before the mutant lands.

    An abrupt termination (a reboot, an End task) while the mutant is on
    disk bypasses the finally-restore, and the original bytes die with the
    process; a rerun's baseline then fails against the mutant and exits
    without repair (wh-process-priority-durable.1.8). The journal survives
    the process, so _recover_pending on the next invocation can undo the
    mutant. Format: decimal length of the original on the first line, then
    the original bytes, then the mutant bytes.
    """
    payload = str(len(original)).encode("ascii") + b"\n" + original + mutated
    _write_atomic(_journal_path(path), payload)


def _clear_journal(path):
    journal = _journal_path(path)
    try:
        journal.unlink()
    except FileNotFoundError:
        pass


def _recover_pending(paths):
    """Undo any mutant a dead run left behind. Returns error strings.

    Runs before the baseline so a crashed prior run self-heals instead of
    presenting as a red baseline (wh-process-priority-durable.1.8). A
    target matching the recorded mutant is restored to the recorded
    original; a target already holding the original means the crash landed
    before the mutant write, so the stale journal is dropped; anything
    else is reported for hand reconciliation and left untouched.
    """
    errors = []
    for path in sorted(set(paths)):
        journal = _journal_path(path)
        if not journal.exists():
            continue
        try:
            payload = journal.read_bytes()
            header, rest = payload.split(b"\n", 1)
            length = int(header)
            original, mutated = rest[:length], rest[length:]
            current = path.read_bytes()
        except (OSError, ValueError) as e:
            errors.append(f"{path}: unreadable recovery journal {journal}: {e}")
            continue
        if current == mutated and current != original:
            try:
                _write_atomic(path, original)
            except OSError as e:
                errors.append(
                    f"{path}: recovery restore failed; the MUTANT REMAINS: {e}"
                )
                continue
            _clear_journal(path)
            print(f"recovered {path} from an interrupted prior run")
        elif current == original:
            _clear_journal(path)
            print(f"dropped stale journal for {path} (target already original)")
        else:
            errors.append(
                f"{path}: recovery journal exists but the target matches "
                "neither the recorded original nor the recorded mutant; "
                "reconcile the file by hand"
            )
    return errors


def _restore_source(path, original, mutated, name):
    """Put the pre-run bytes back. Returns an error string, or None.

    Never overwrite bytes this run did not write: a save landing from
    another session or an IDE while pytest ran would otherwise be replaced
    by the stale pre-run snapshot and lost
    (wh-process-priority-durable.1.5). A failed or refused restore is
    reported explicitly so a mutant never remains in source silently. The
    window between the snapshot read and the mutant write stays unguarded;
    it is microseconds wide, against seconds for a pytest run.
    """
    try:
        current = path.read_bytes()
    except OSError as e:
        return (f"{name}: cannot read {path} before restore; "
                f"the MUTANT MAY REMAIN in source: {e}")
    if current == original:
        return None
    if current != mutated:
        return (f"{name}: {path} changed while pytest ran (bytes differ "
                "from the written mutant); refusing to overwrite the "
                "concurrent edit with the stale pre-run snapshot -- "
                "reconcile the file by hand")
    # crewcut: a check-to-replace window remains -- a save landing between
    # the equality read above and os.replace inside _write_atomic is
    # overwritten by the original snapshot (wh-process-priority-durable.1.7).
    # A handle-based write lock cannot close it: os.replace retires the
    # locked file object at the mutant write, so a held handle guards the
    # superseded object while writers reopen the new one, and in-place
    # locked writes would reintroduce the .1.6 truncation cell. To remove:
    # redesign the gate to mutate a disposable copy of the source tree
    # instead of live source. Accepted because the window needs a writer
    # inside this session-owned worktree during a gate run, a state the
    # worktree rules and the round prompts already forbid.
    try:
        _write_atomic(path, original)
    except OSError as e:
        return f"{name}: restore write failed; the MUTANT REMAINS in {path}: {e}"
    return None


def _run_pytest():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        ["uv", "run", "pytest", TEST_FILE, "-rf", "-q", "-p", "no:randomly"],
        cwd=SERVICE, env=env, capture_output=True, text=True, timeout=180,
    )


def _clear_pycache():
    for d in SERVICE.rglob("__pycache__"):
        if ".venv" not in d.parts:
            shutil.rmtree(d, ignore_errors=True)


def _translate(pattern: str, data: bytes) -> bytes:
    raw = pattern.encode("utf-8")
    if b"\r\n" in data:
        raw = raw.replace(b"\n", b"\r\n")
    return raw


def check_only() -> int:
    """Validate every pattern and Python mutant without running or writing."""
    stale = ambiguous = broken = 0
    for mutation in MUTATIONS:
        path = mutation["file"]
        data = path.read_bytes()
        newline = b"\r\n" if b"\r\n" in data else b"\n"
        old = mutation["old"].encode("utf-8").replace(b"\n", newline)
        new = mutation["new"].encode("utf-8").replace(b"\n", newline)
        count = data.count(old)
        if count == 0:
            stale += 1
            print(f"STALE {mutation['name']}: pattern not found in {path}")
            continue
        if count != 1:
            ambiguous += 1
            print(f"AMBIGUOUS {mutation['name']}: {count} matches in {path}")
            continue
        if path.suffix == ".py":
            try:
                compile(data.replace(old, new, 1), str(path), "exec")
            except SyntaxError as exc:
                broken += 1
                print(f"BROKEN {mutation['name']}: mutant does not compile: {exc}")
    print(
        f"checked {len(MUTATIONS)} patterns, {stale} stale, "
        f"{ambiguous} ambiguous, {broken} that do not compile"
    )
    return 1 if stale or ambiguous or broken else 0


def main() -> int:
    if "--check" in sys.argv[1:]:
        return check_only()

    errors = []
    survivors = []

    # Undo anything an abruptly terminated prior run left behind, before
    # the baseline can fail against its mutant
    # (wh-process-priority-durable.1.8).
    recovery_errors = _recover_pending(m["file"] for m in MUTATIONS)
    if recovery_errors:
        for e in recovery_errors:
            print(f"ERROR {e}")
        return 1

    # Expected-name validation before the first mutation.
    collect = subprocess.run(
        ["uv", "run", "pytest", TEST_FILE, "--collect-only", "-q"],
        cwd=SERVICE, capture_output=True, text=True, timeout=180,
    )
    real_names = set()
    for line in collect.stdout.splitlines():
        if "::" in line:
            real_names.add(re.sub(r"\[.*\]$", "", line.rsplit("::", 1)[-1]))
    for m in MUTATIONS:
        for name in m["expect"]:
            if name not in real_names:
                errors.append(f"{m['name']}: expected test {name} does not exist")
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    # Baseline must be green.
    base = _run_pytest()
    if base.returncode != 0:
        print("ERROR baseline run is not green; refusing to mutate")
        print(base.stdout[-2000:])
        return 1
    print("baseline green")

    for m in MUTATIONS:
        path = m["file"]
        data = path.read_bytes()
        old = _translate(m["old"], data)
        new = _translate(m["new"], data)
        count = data.count(old)
        if count == 0:
            errors.append(f"{m['name']}: pattern not found")
            print(f"ERROR {m['name']}: pattern not found")
            continue
        if count > 1:
            errors.append(f"{m['name']}: pattern ambiguous ({count} matches)")
            print(f"ERROR {m['name']}: pattern ambiguous ({count} matches)")
            continue
        mutated = data.replace(old, new, 1)
        try:
            compile(mutated.decode("utf-8"), str(path), "exec")
        except SyntaxError as e:
            errors.append(f"{m['name']}: mutant does not compile: {e}")
            print(f"ERROR {m['name']}: mutant does not compile: {e}")
            continue
        _clear_pycache()
        try:
            _write_journal(path, data, mutated)
        except OSError as e:
            errors.append(
                f"{m['name']}: journal write failed; target left unchanged: {e}"
            )
            print(
                f"ERROR {m['name']}: journal write failed; "
                f"target left unchanged: {e}"
            )
            continue
        try:
            _write_atomic(path, mutated)
        except OSError as e:
            errors.append(
                f"{m['name']}: mutant write failed; target left unchanged: {e}"
            )
            print(
                f"ERROR {m['name']}: mutant write failed; "
                f"target left unchanged: {e}"
            )
            _clear_journal(path)
            continue
        timed_out = False
        try:
            run = _run_pytest()
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            restore_error = _restore_source(path, data, mutated, m["name"])
            _clear_pycache()
        if restore_error:
            errors.append(restore_error)
            print(f"ERROR {restore_error}")
            print("aborting remaining mutations: source no longer matches "
                  "what this run snapshotted; the recovery journal is kept "
                  "for the next invocation")
            break
        _clear_journal(path)
        if timed_out:
            errors.append(f"{m['name']}: pytest timed out")
            print(f"ERROR {m['name']}: pytest timed out")
            continue
        out = run.stdout + run.stderr
        if "+++ Timeout +++" in out:
            errors.append(f"{m['name']}: suite-timeout-abort")
            print(f"ERROR {m['name']}: suite-timeout-abort")
            continue
        failed = {
            re.sub(r"\[.*\]$", "", line.split("::")[-1].split()[0])
            for line in out.splitlines()
            if line.startswith("FAILED")
        }
        missing = [t for t in m["expect"] if t not in failed]
        if missing:
            survivors.append(f"{m['name']}: expected {missing}, failed={sorted(failed)}")
            print(f"SURVIVED {m['name']}: expected {missing} to fail; failed={sorted(failed)}")
        else:
            print(f"caught {m['name']} by {sorted(failed & set(m['expect']))}")

    print(
        f"scope: ran {len(MUTATIONS)} mutations (full set for this gate), "
        f"{len(survivors)} survivors, {len(errors)} errors"
    )
    return 1 if (survivors or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
