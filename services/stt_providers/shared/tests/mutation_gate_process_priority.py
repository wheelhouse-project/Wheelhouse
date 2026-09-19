"""Mutation gate for the STT-side process-priority elevation guard tests.

Proves the process-class additions to tests/test_thread_priority.py fail for
the right reason when the protected behavior breaks
(wh-process-priority-durable). Run from services/stt_providers/shared:

    uv run python tests/mutation_gate_process_priority.py

Same discipline as the wheelhouse-side gate: byte IO, one-match patterns
translated to the file's own line endings, compile check, atomic mutant
write through a same-directory temp file, timeout, atomic restore in
finally; pattern/compile/timeout problems are ERRORS, never verdicts.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parents[1]
PROVIDERS_DIR = SERVICE.parent
TEST_FILE = "tests/test_thread_priority.py"

_PROVIDER_NAMES = [
    "distil_medium_en",
    "google_stt_server",
    "sherpa_offline_parakeet_stt_server",
]

_PROVIDER_MUTATIONS = [
    {
        "name": f"{provider}-drops-elevation",
        "file": PROVIDERS_DIR / provider / "main.py",
        "old": "    elevate_current_process()",
        "new": "    pass",
        "expect": ["test_provider_main_elevates_process"],
    }
    for provider in _PROVIDER_NAMES
] + [
    # Relocation into a dead branch keeps the call lexically present but off
    # the __main__ execution path; the guard test must still fail
    # (wh-process-priority-durable.1.2).
    {
        "name": f"{provider}-relocates-elevation-to-dead-branch",
        "file": PROVIDERS_DIR / provider / "main.py",
        "old": "    elevate_current_process()",
        "new": "    if False:\n        elevate_current_process()",
        "expect": ["test_provider_main_elevates_process"],
    }
    for provider in _PROVIDER_NAMES
]

MUTATIONS = [
    {
        "name": "high-class-becomes-below-normal",
        "file": SERVICE / "shared_audio" / "thread_priority.py",
        "old": "HIGH_PRIORITY_CLASS = 0x00000080",
        "new": "HIGH_PRIORITY_CLASS = 0x00004000",
        "expect": ["test_round_trip_sets_high_priority_class"],
    },
    {
        "name": "process-failure-reports-success",
        "file": SERVICE / "shared_audio" / "thread_priority.py",
        "old": (
            '        logger.warning("[priority] process priority elevation '
            'unavailable: %s", e)\n'
            "        return False"
        ),
        "new": (
            '        logger.warning("[priority] process priority elevation '
            'unavailable: %s", e)\n'
            "        return True"
        ),
        "expect": ["test_exception_returns_false"],
    },
] + _PROVIDER_MUTATIONS


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


def main() -> int:
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
