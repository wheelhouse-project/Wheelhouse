"""Mutation gate for one wheelhouse.log rotation per start (wh-log-triple-rotation).

Proves tests/test_log_rotation.py and tests/test_launcher.py (plus
tests/test_logging_setup.py for the standalone-rotation mutation) fail for
the right reason when the protected behavior breaks. Run from
services/wheelhouse:

    uv run python tests/mutation_gate_log_rotation.py          # full sweep
    uv run python tests/mutation_gate_log_rotation.py --check  # patterns only

Runner structure copied from tests/mutation_gate_process_priority.py
(journal, recovery, durable restore, compile check, per-mutation timeout,
suite-timeout detection, expected-name validation, --check). Additions for
this gate:

- Each expected catcher names a substring its failure reason must contain.
  A catcher that fails for any other reason (an unrelated crash) is an
  ERROR ("wrong-reason"), never a catch.
- Verdicts come only from pytest's short test summary section, with
  COLUMNS=1000 so the reason suffix is not cut. Exit code 0 means no test
  failed (a survivor); the summary is read only on a nonzero exit.
- A KeyboardInterrupt during the restore is held, the restore is retried
  once, the bytecode caches are cleared, and only then is it re-raised.

Pattern-not-found, ambiguous patterns, non-compiling mutants, timeouts,
suite-timeout aborts, and wrong-reason failures are ERRORS, never verdicts.
None of the tests touch the repo-root wheelhouse.log: they use tmp_path,
and test_launcher.py stubs the launcher's rotation (hermetic_log_rotation).
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
TEST_FILES = ["tests/test_log_rotation.py", "tests/test_launcher.py"]
LOGGING_SETUP_TESTS = "tests/test_logging_setup.py"
ALL_TEST_FILES = TEST_FILES + [LOGGING_SETUP_TESTS]

LOG_ROTATION = SERVICE / "utils" / "log_rotation.py"
LOGGING_SETUP = SERVICE / "utils" / "logging_setup.py"
LAUNCHER = SERVICE / "launcher.py"

# "expect" maps each expected catcher to a substring of its short-summary
# failure reason. "selection" overrides the default TEST_FILES.
MUTATIONS = [
    {
        "name": "empty-log-counts-as-content",
        "file": LOG_ROTATION,
        "old": "return os.path.getsize(log_file_path) > 0",
        "new": "return os.path.getsize(log_file_path) >= 0",
        "expect": {"test_empty_log_is_not_rotated": "assert True is False"},
    },
    {
        # test_keeps_five_backups... fails on its first line, the constant
        # pin `assert LOG_BACKUP_COUNT == 5`, before its shift assertions.
        "name": "backup-count-five-becomes-two",
        "file": LOG_ROTATION,
        "old": "LOG_BACKUP_COUNT = 5",
        "new": "LOG_BACKUP_COUNT = 2",
        "expect": {
            "test_keeps_five_backups_and_shifts_each_one_place": "assert 2 == 5",
            "test_the_launcher_file_handler_keeps_five_backups": "assert 2 == 5",
        },
    },
    {
        # The helper reports no move, and nothing moves.
        "name": "rotation-never-moves-the-file",
        "file": LOG_ROTATION,
        "old": "            return _move_log_to_first_backup(log_file_path)",
        "new": "            return False",
        "expect": {
            "test_one_app_start_rotates_the_log_exactly_once":
                "assert False is True",
            "test_keeps_five_backups_and_shifts_each_one_place":
                "assert False is True",
            # The launcher helper now reports the unmoved non-empty log
            # (wh-log-triple-rotation.1.1) before the backup read fails.
            "test_rotates_the_project_log_once":
                "could not be renamed",
            "test_setup_logging_still_rotates_without_the_launcher":
                "assert [] == ['wheelhouse.log.1']",
        },
    },
    {
        # wh-log-triple-rotation.1.2: without the move back, a held backup
        # leaves the previous run under the temporary .rotate.* name.
        "name": "failed-backup-move-strands-the-log",
        "file": LOG_ROTATION,
        "old": (
            "        _undo_moves(done, error)\n"
            "        raise"
        ),
        "new": "        raise",
        "expect": {
            "test_a_backup_that_cannot_move_leaves_the_log_in_place": "== []",
            "test_a_backup_held_open_by_another_program_leaves_the_log_in_place":
                "== []",
        },
    },
    {
        # Shifting .1 first overwrites .2 before .2 has moved.
        "name": "backups-shift-oldest-last",
        "file": LOG_ROTATION,
        "old": "for i in range(LOG_BACKUP_COUNT - 1, 0, -1):",
        "new": "for i in range(1, LOG_BACKUP_COUNT):",
        "expect": {
            # A move onto an existing backup is refused, so the rotation
            # raises instead of overwriting (wh-log-triple-rotation.1.3).
            "test_keeps_five_backups_and_shifts_each_one_place":
                "FileExistsError",
        },
    },
    {
        # wh-log-triple-rotation.1.3: restoring only the log left each
        # completed backup move in place, so each failed start lost one
        # more old backup.
        "name": "failed-rotation-restores-only-the-log",
        "file": LOG_ROTATION,
        "old": "        _undo_moves(done, error)\n",
        "new": "        os.rename(temp_path, log_file_path)\n",
        "expect": {
            "test_repeated_failed_rotations_keep_every_backup":
                "== ['wheelhouse....lhouse.log.5']",
            "test_a_backup_held_open_across_restarts_keeps_every_backup":
                "== ['wheelhouse....lhouse.log.5']",
        },
    },
    {
        # Undoing the first move first finds its name already taken.
        "name": "failed-rotation-undoes-in-forward-order",
        "file": LOG_ROTATION,
        "old": "reversed(done)",
        "new": "done",
        "expect": {
            "test_repeated_failed_rotations_keep_every_backup":
                "== ['wheelhouse....lhouse.log.5']",
        },
    },
    {
        # wh-log-triple-rotation.1.4: without the per-move try, one failed
        # undo stops the loop before the log's own move is undone.
        "name": "undo-stops-at-the-first-failure",
        "file": LOG_ROTATION,
        "old": (
            "        try:\n"
            "            os.rename(destination, source)\n"
            "        except OSError as undo_error:\n"
            "            error.add_note(f\"could not move {destination} back to {source}: {undo_error}\")\n"
        ),
        "new": "        os.rename(destination, source)\n",
        "expect": {
            "test_the_log_returns_to_its_own_name":
                "assert 'opened by a reader' == 'held open by another program'",
        },
    },
    {
        # A failed undo is not named on the raised error.
        "name": "failed-undo-not-noted",
        "file": LOG_ROTATION,
        "old": "            error.add_note(f\"could not move {destination} back to {source}: {undo_error}\")\n",
        "new": "            pass\n",
        "expect": {
            # The assertion message is the empty notes list.
            "test_the_log_returns_to_its_own_name": "AssertionError: []",
        },
    },
    {
        # The oldest backup is overwritten at once instead of set aside,
        # so a failed rotation cannot bring it back.
        "name": "oldest-backup-overwritten-not-set-aside",
        "file": LOG_ROTATION,
        "old": (
            "        if os.path.exists(oldest_path):\n"
            "            os.rename(oldest_path, dropped_path)\n"
            "            done.append((oldest_path, dropped_path))\n"
            "        for i in range(LOG_BACKUP_COUNT - 1, 0, -1):\n"
            "            backup_path = f\"{log_file_path}.{i}\"\n"
            "            if os.path.exists(backup_path):\n"
            "                os.rename(backup_path, "
        ),
        "new": (
            "        for i in range(LOG_BACKUP_COUNT - 1, 0, -1):\n"
            "            backup_path = f\"{log_file_path}.{i}\"\n"
            "            if os.path.exists(backup_path):\n"
            "                os.replace(backup_path, "
        ),
        "expect": {
            "test_repeated_failed_rotations_keep_every_backup":
                "== ['wheelhouse....lhouse.log.5']",
        },
    },
    {
        # The set-aside oldest backup is never deleted.
        "name": "dropped-backup-left-behind",
        "file": LOG_ROTATION,
        "old": "        os.remove(dropped_path)\n",
        "new": "        pass\n",
        "expect": {
            "test_a_successful_rotation_leaves_no_extra_file": "AssertionError",
        },
    },
    {
        # wh-log-triple-rotation.1.2: a failed standalone rotation must not
        # cost the process its file handler.
        "name": "standalone-rotation-failure-drops-the-file-handler",
        "file": LOGGING_SETUP,
        "old": "                _rotation_error = exc",
        "new": "                raise",
        "expect": {
            "test_a_standalone_start_whose_rotation_fails_still_logs_to_the_file":
                "LogicProcess startup line",
        },
    },
    {
        "name": "standalone-rotation-failure-not-warned",
        "file": LOGGING_SETUP,
        "old": "    if _rotation_error is not None:",
        "new": "    if False:",
        "expect": {
            "test_a_standalone_start_whose_rotation_fails_still_logs_to_the_file":
                "Could not start a new wheelhouse.log for this run",
        },
    },
    {
        "name": "children-ignore-the-launcher-skip-flag",
        "file": LOGGING_SETUP,
        "old": 'if os.environ.get(LAUNCHER_ROTATED_ENV) != "1":',
        "new": 'if os.environ.get(LAUNCHER_ROTATED_ENV) != "yes":',
        "expect": {
            "test_setup_logging_skips_rotation_when_the_launcher_already_rotated":
                "setup_logging rotated although the launcher had already rotated",
            "test_one_app_start_rotates_the_log_exactly_once":
                "== ['wheelhouse.log.1']",
        },
    },
    {
        "name": "standalone-setup-logging-never-rotates",
        "file": LOGGING_SETUP,
        "old": (
            '            try:\n'
            '                rotate_log_for_new_run(log_file_path)'
        ),
        "new": (
            '            try:\n'
            '                pass'
        ),
        "selection": TEST_FILES + [LOGGING_SETUP_TESTS],
        "expect": {
            "test_setup_logging_still_rotates_without_the_launcher":
                "assert [] == ['wheelhouse.log.1']",
            "test_log_rotates_previous_session_on_restart":
                "Previous session should be rotated to .1",
            "test_log_rotates_on_startup_when_existing_log_has_content":
                "Previous log should be rotated to wheelhouse.log.1",
        },
    },
    {
        "name": "launcher-exports-skip-flag-as-zero",
        "file": LAUNCHER,
        "old": 'os.environ[LAUNCHER_ROTATED_ENV] = "1"',
        "new": 'os.environ[LAUNCHER_ROTATED_ENV] = "0"',
        "expect": {
            "test_children_start_with_the_skip_variable_set":
                "('start', 'LogicProcess', '0')",
            "test_a_failed_rotation_leaves_the_log_unrotated_and_still_starts":
                "('start', 'LogicProcess', '0')",
        },
    },
    {
        # A MOVE, not a copy: the first-cycle rotation line leaves its place
        # before logging and lands after _configure_launcher_logging.
        "name": "first-rotation-moves-after-launcher-logging",
        "file": LAUNCHER,
        "old": (
            "    _first_rotation_error = _rotate_log_for_new_cycle(project_root)\n"
            "\n"
            "    # --- Configure logging IMMEDIATELY ---\n"
            "    # Launcher can't use setup_logging() (needs config dict + heavier imports);\n"
            "    # _configure_launcher_logging gives it the same queue/listener split with\n"
            "    # the same format and file (wh-console-write-resilience).\n"
            "    _listener = _configure_launcher_logging(project_root)\n"
        ),
        "new": (
            "\n"
            "    # --- Configure logging IMMEDIATELY ---\n"
            "    # Launcher can't use setup_logging() (needs config dict + heavier imports);\n"
            "    # _configure_launcher_logging gives it the same queue/listener split with\n"
            "    # the same format and file (wh-console-write-resilience).\n"
            "    _listener = _configure_launcher_logging(project_root)\n"
            "    _first_rotation_error = _rotate_log_for_new_cycle(project_root)\n"
        ),
        "expect": {
            "test_the_first_rotation_runs_before_the_launcher_writes_any_line":
                "[('configure',), ('rotate'",
            "test_each_cycle_rotates_once_before_its_children_start":
                "[('configure',), ('rotate'",
        },
    },
    {
        # Mutates the guard's value only; first_cycle still advances, so the
        # loop terminates.
        "name": "restart-cycle-never-rotates",
        "file": LAUNCHER,
        "old": "        if not first_cycle:",
        "new": "        if False:",
        "expect": {
            "test_each_cycle_rotates_once_before_its_children_start":
                "('start', 'GuiProcess', '1'), ('start', 'LogicProcess', '1')",
        },
    },
    {
        "name": "first-cycle-rotates-twice",
        "file": LAUNCHER,
        "old": "        if not first_cycle:",
        "new": "        if True:",
        "expect": {
            "test_each_cycle_rotates_once_before_its_children_start":
                "('configure',), ('rotate', '1'), ('start', 'LogicProcess'",
        },
    },
    {
        "name": "cycle-rotation-raises-instead-of-returning",
        "file": LAUNCHER,
        "old": (
            "        moved = rotate_log_for_new_run(log_file_path)\n"
            "    except Exception as exc:\n"
            "        return exc"
        ),
        "new": (
            "        moved = rotate_log_for_new_run(log_file_path)\n"
            "    except Exception as exc:\n"
            "        raise"
        ),
        "expect": {
            "test_returns_the_error_instead_of_raising":
                "RuntimeError: Cannot acquire lock after 20 attempts",
        },
    },
    {
        # wh-log-triple-rotation.1.1: a rename the library skipped reads as
        # success again, so the run appends with no warning.
        "name": "skipped-move-passes-as-success",
        "file": LAUNCHER,
        "old": "    if had_content and not moved:",
        "new": "    if False:",
        "expect": {
            "test_a_skipped_move_of_a_non_empty_log_is_reported":
                "assert False",
            "test_a_log_held_open_by_another_program_is_reported":
                "assert False",
        },
    },
    {
        # Input-level: the content check always answers yes, so an absent
        # or empty log (nothing to move) is reported as a failure.
        "name": "nothing-to-move-reported-as-failure",
        "file": LAUNCHER,
        "old": "    had_content = log_has_content(log_file_path)",
        "new": "    had_content = True",
        "expect": {
            "test_nothing_to_move_is_not_reported": "could not be renamed",
        },
    },
    {
        "name": "launcher-handler-keeps-two-backups",
        "file": LAUNCHER,
        "old": "maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,",
        "new": "maxBytes=LOG_MAX_BYTES, backupCount=2,",
        "expect": {
            "test_the_launcher_file_handler_keeps_five_backups": "assert 2 == 5",
        },
    },
    {
        # The caplog assertion prints the list of record messages (strings);
        # the start-count assertions before it print tuples, so "['" tells
        # the warning assertion apart from them.
        "name": "first-rotation-failure-not-warned",
        "file": LAUNCHER,
        "old": "        _warn_log_rotation_failed(_first_rotation_error)",
        "new": "        pass",
        "expect": {
            "test_a_failed_rotation_leaves_the_log_unrotated_and_still_starts":
                "AssertionError: ['",
        },
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
    therefore leaves path holding whatever it held before
    (wh-process-priority-durable.1.6).
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

    An abrupt termination while the mutant is on disk bypasses the
    finally-restore; the journal survives the process, so _recover_pending
    on the next invocation can undo the mutant
    (wh-process-priority-durable.1.8). Format: decimal length of the
    original on the first line, then the original bytes, then the mutant.
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

    A target matching the recorded mutant is restored to the recorded
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
    (wh-process-priority-durable.1.5).
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
    # the equality read above and the replace inside _write_atomic is
    # overwritten by the original snapshot. To remove: mutate a disposable
    # copy of the source tree instead of live source. Accepted because the
    # window needs a writer inside this session-owned worktree during a
    # gate run, which the worktree rules forbid.
    try:
        _write_atomic(path, original)
    except OSError as e:
        return f"{name}: restore write failed; the MUTANT REMAINS in {path}: {e}"
    return None


def _restore_holding_interrupt(path, original, mutated, name):
    """Restore, surviving a Ctrl+C that lands inside the restore itself.

    Returns (error string or None, held KeyboardInterrupt or None). One
    interrupt during the restore is held and the restore retried once; a
    second interrupt during the retry is held too and the failure printed.
    The caller clears bytecode caches before re-raising the held interrupt.
    """
    held = None
    try:
        return _restore_source(path, original, mutated, name), None
    except KeyboardInterrupt as e:
        held = e
    try:
        return _restore_source(path, original, mutated, name), held
    except KeyboardInterrupt:
        msg = (f"{name}: restore interrupted twice; the MUTANT MAY REMAIN "
               f"in {path}; the recovery journal is kept")
        print(f"ERROR {msg}")
        return msg, held


def _pytest_env():
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COLUMNS="1000")


def _run_pytest(selection):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *selection,
         "-rf", "-q", "-p", "no:randomly"],
        cwd=SERVICE, env=_pytest_env(), capture_output=True, text=True,
        timeout=180,
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


def _short_summary_failures(out):
    """Map failed test name -> reason from pytest's short summary only.

    Returns None when the section is absent. Lines outside the section
    (captured log records at ERROR level, for example) are never read.
    """
    lines = out.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^=+ short test summary info =+$", line):
            start = i + 1
            break
    if start is None:
        return None
    failed = {}
    for line in lines[start:]:
        if line.startswith("="):
            break
        m = re.match(r"^FAILED (\S+)(?: - (.*))?$", line)
        if not m:
            continue
        name = re.sub(r"\[.*\]$", "", m.group(1).rsplit("::", 1)[-1])
        failed[name] = m.group(2) or ""
    return failed


def check_only() -> int:
    """Validate every pattern and Python mutant without running or writing."""
    stale = ambiguous = broken = 0
    for mutation in MUTATIONS:
        path = mutation["file"]
        data = path.read_bytes()
        old = _translate(mutation["old"], data)
        new = _translate(mutation["new"], data)
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
    caught = 0

    recovery_errors = _recover_pending(m["file"] for m in MUTATIONS)
    if recovery_errors:
        for e in recovery_errors:
            print(f"ERROR {e}")
        return 1

    # Expected-name validation before the first mutation.
    collect = subprocess.run(
        [sys.executable, "-m", "pytest", *ALL_TEST_FILES,
         "--collect-only", "-q", "-p", "no:randomly"],
        cwd=SERVICE, env=_pytest_env(), capture_output=True, text=True,
        timeout=180,
    )
    real_names = {}
    for line in collect.stdout.splitlines():
        if "::" in line:
            test_file = line.split("::", 1)[0].replace("\\", "/")
            name = re.sub(r"\[.*\]$", "", line.rsplit("::", 1)[-1])
            real_names.setdefault(name, set()).add(test_file)
    for m in MUTATIONS:
        selection = m.get("selection", TEST_FILES)
        for name in m["expect"]:
            files = real_names.get(name, set())
            if not files:
                errors.append(f"{m['name']}: expected test {name} does not exist")
            elif not files & set(selection):
                errors.append(
                    f"{m['name']}: expected test {name} is outside the "
                    f"mutation's selection {selection}"
                )
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    # Baseline must be green over every file any mutation selects.
    base = _run_pytest(ALL_TEST_FILES)
    if base.returncode != 0:
        print("ERROR baseline run is not green; refusing to mutate")
        print(base.stdout[-2000:])
        return 1
    print("baseline green")

    for m in MUTATIONS:
        path = m["file"]
        selection = m.get("selection", TEST_FILES)
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
            print(f"ERROR {errors[-1]}")
            continue
        try:
            _write_atomic(path, mutated)
        except OSError as e:
            errors.append(
                f"{m['name']}: mutant write failed; target left unchanged: {e}"
            )
            print(f"ERROR {errors[-1]}")
            _clear_journal(path)
            continue
        timed_out = False
        held = None
        try:
            run = _run_pytest(selection)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            restore_error, held = _restore_holding_interrupt(
                path, data, mutated, m["name"])
            _clear_pycache()
            if held is not None:
                if restore_error:
                    print(f"ERROR {restore_error}")
                raise held
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
        if run.returncode == 0:
            survivors.append(f"{m['name']}: no test failed")
            print(f"SURVIVED {m['name']}: no test failed "
                  f"(expected {sorted(m['expect'])})")
            continue
        if run.returncode != 1:
            errors.append(f"{m['name']}: pytest exit code {run.returncode}")
            print(f"ERROR {m['name']}: pytest exit code {run.returncode}")
            print(out[-2000:])
            continue
        failed = _short_summary_failures(out)
        if failed is None:
            errors.append(f"{m['name']}: exit 1 but no short test summary")
            print(f"ERROR {m['name']}: exit 1 but no short test summary")
            print(out[-2000:])
            continue
        missing = [t for t in m["expect"] if t not in failed]
        if missing:
            survivors.append(
                f"{m['name']}: expected {missing}, failed={sorted(failed)}")
            print(f"SURVIVED {m['name']}: expected {missing} to fail; "
                  f"failed={sorted(failed)}")
            continue
        wrong = {
            t: failed[t] for t, reason in m["expect"].items()
            if reason not in failed[t]
        }
        if wrong:
            errors.append(f"{m['name']}: wrong-reason {wrong}")
            print(f"ERROR {m['name']}: wrong-reason (an expected catcher "
                  f"failed on something other than its named assertion)")
            for t, reason in wrong.items():
                print(f"    {t}: expected {m['expect'][t]!r} in {reason!r}")
            continue
        caught += 1
        print(f"caught {m['name']}")
        for t in m["expect"]:
            print(f"    {t}: {failed[t]}")
        others = sorted(set(failed) - set(m["expect"]))
        if others:
            print(f"    also failed: {others}")

    print(
        f"scope: ran {len(MUTATIONS)} mutations (full set for this gate): "
        f"{caught} caught, {len(survivors)} survivors, {len(errors)} errors"
    )
    return 1 if (survivors or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
