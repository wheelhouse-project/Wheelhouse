"""Tests for utils.process_priority -- process priority class elevation.

The elevation exists because a Task Scheduler launch runs the whole process
tree at Below Normal priority (task XML Priority default 7), and a Below
Normal process class starves the speech pipeline whenever the machine is
saturated by bulk compute (wh-process-priority-durable, 2026-08-28).
Thread-level elevation (shared_audio.thread_priority) cannot compensate for
the process class, so every latency-sensitive WheelHouse process raises its
own class to High at startup. A process may raise its own priority class up
to High without administrator rights.
"""
import ast
import sys
from pathlib import Path

import psutil
import pytest

from services.wheelhouse.utils.process_priority import elevate_process_priority

_windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows process priority classes only"
)

_SERVICE_DIR = Path(__file__).resolve().parents[1]


@_windows_only
class TestElevateProcessPriority:
    def test_round_trip_sets_high_priority_class(self):
        """The real Windows API round-trip: elevate, read back High, restore."""
        proc = psutil.Process()
        original = proc.nice()
        try:
            ok = elevate_process_priority()
            assert ok is True
            assert proc.nice() == psutil.HIGH_PRIORITY_CLASS
        finally:
            proc.nice(original)


class TestFailurePaths:
    """The never-crash guarantee: OS failures return False, never raise."""

    def test_os_refusal_returns_false(self, monkeypatch):
        import services.wheelhouse.utils.process_priority as pp

        class _RefusingProcess:
            def nice(self, value=None):
                raise psutil.AccessDenied(pid=0)

        monkeypatch.setattr(pp.psutil, "Process", _RefusingProcess)
        assert elevate_process_priority() is False

    def test_unexpected_error_returns_false(self, monkeypatch):
        import services.wheelhouse.utils.process_priority as pp

        class _BrokenProcess:
            def __init__(self):
                raise RuntimeError("no such process")

        monkeypatch.setattr(pp.psutil, "Process", _BrokenProcess)
        assert elevate_process_priority() is False

    def test_failure_is_logged(self, monkeypatch, caplog):
        import services.wheelhouse.utils.process_priority as pp

        class _RefusingProcess:
            def nice(self, value=None):
                raise psutil.AccessDenied(pid=0)

        monkeypatch.setattr(pp.psutil, "Process", _RefusingProcess)
        with caplog.at_level("WARNING"):
            elevate_process_priority()
        assert any("priority" in r.message.lower() for r in caplog.records)


def _entry_path_statements(source_path: Path, function_name: str) -> list:
    """Statements that run unconditionally when the named function is entered.

    The function's direct body, with each top-level ``try`` block's body
    inlined in place: statements in a try body run in order on the entry
    path until an exception, unlike nested defs, dead branches, and other
    compound statements, which must not count
    (wh-process-priority-durable.1.2).
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == function_name
        ):
            flat = []
            for stmt in node.body:
                if isinstance(stmt, ast.Try):
                    flat.extend(stmt.body)
                else:
                    flat.append(stmt)
            return flat
    return []


def _call_name(stmt: ast.stmt) -> str | None:
    """The called name when stmt is a bare call statement, else None."""
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        fn = stmt.value.func
        return getattr(fn, "id", None) or getattr(fn, "attr", None)
    return None


def _function_calls_elevation(source_path: Path, function_name: str) -> bool:
    """True when the named top-level function calls elevate_process_priority
    as a statement on its unconditional entry path.
    """
    return any(
        _call_name(stmt) == "elevate_process_priority"
        for stmt in _entry_path_statements(source_path, function_name)
    )


def _elevation_follows_setup_logging(
    source_path: Path, function_name: str
) -> bool:
    """True when every elevate_process_priority call on the entry path comes
    after the setup_logging call.

    Order matters: elevate_process_priority logs a warning when the OS
    refuses, and a record emitted before setup_logging goes only to
    Python's lastResort stderr handler, never to the configured process log
    (utils/logging_setup.py; wh-process-priority-durable.1.4).
    """
    names = [
        _call_name(stmt)
        for stmt in _entry_path_statements(source_path, function_name)
    ]
    if "setup_logging" not in names or "elevate_process_priority" not in names:
        return False
    setup_index = names.index("setup_logging")
    return all(
        index > setup_index
        for index, name in enumerate(names)
        if name == "elevate_process_priority"
    )


class TestEntryPointsElevate:
    """Every latency-sensitive process entry point elevates its own class.

    These are source-level guard tests: calling the real entry functions
    would start the whole application. Deleting the elevation call from an
    entry point makes the matching test fail.
    """

    def test_launcher_supervisor_elevates(self):
        # The supervision path lives in _run_supervisor, which main() calls
        # after the one-shot CLI shortcuts (--clear-screen-reader-flag,
        # --reset-first-use-hints) that exit before any spawn -- those
        # shortcuts do not need elevation.
        assert _function_calls_elevation(
            _SERVICE_DIR / "launcher.py", "_run_supervisor"
        )

    def test_logic_entry_elevates(self):
        assert _function_calls_elevation(
            _SERVICE_DIR / "main.py", "start_logic_process"
        )

    def test_input_entry_elevates(self):
        assert _function_calls_elevation(
            _SERVICE_DIR / "input_proc.py", "input_process_main"
        )

    def test_gui_entry_elevates(self):
        assert _function_calls_elevation(
            _SERVICE_DIR / "gui.py", "gui_process_target"
        )


class TestElevationFollowsLoggingSetup:
    """The elevation call comes after setup_logging in each spawned child.

    An elevation refusal logs a warning; emitted before setup_logging it
    reaches only bare stderr and never the process log
    (wh-process-priority-durable.1.4). The launcher is exempt: it has no
    setup_logging call -- it configures its logging QueueListener before
    the elevation call in its own startup sequence.
    """

    def test_logic_elevation_follows_logging_setup(self):
        assert _elevation_follows_setup_logging(
            _SERVICE_DIR / "main.py", "start_logic_process"
        )

    def test_input_elevation_follows_logging_setup(self):
        assert _elevation_follows_setup_logging(
            _SERVICE_DIR / "input_proc.py", "input_process_main"
        )

    def test_gui_elevation_follows_logging_setup(self):
        assert _elevation_follows_setup_logging(
            _SERVICE_DIR / "gui.py", "gui_process_target"
        )


class TestMutationGateRecovery:
    """The gate journal restores a mutant left by an abrupt termination.

    A reboot or kill while a mutant is on disk bypasses the finally-restore
    and the original bytes die with the process; the journal persists them
    so the next invocation can undo the mutant before its baseline runs
    (wh-process-priority-durable.1.8). Temp files only -- never live source.
    """

    def _load_gate(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "mutation_gate_process_priority",
            str(_SERVICE_DIR / "tests" / "mutation_gate_process_priority.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_publish_durable_replaces_target_on_disk(self, tmp_path):
        gate = self._load_gate()
        target = tmp_path / "target.py"
        tmp = tmp_path / "target.py.gate-tmp"
        target.write_bytes(b"old")
        tmp.write_bytes(b"new")
        gate._publish_durable(tmp, target)
        assert target.read_bytes() == b"new"
        assert not tmp.exists()

    def test_write_atomic_publishes_through_durable_rename(
        self, tmp_path, monkeypatch
    ):
        # os.replace alone is not power-loss durable on Windows (MoveFileExW
        # without MOVEFILE_WRITE_THROUGH), so every gate publish must go
        # through _publish_durable (wh-process-priority-durable.1.9).
        gate = self._load_gate()
        target = tmp_path / "target.py"
        calls = []

        def recorder(tmp, path):
            calls.append((tmp, path))
            import os

            os.replace(tmp, path)

        monkeypatch.setattr(gate, "_publish_durable", recorder)
        gate._write_atomic(target, b"payload")
        assert target.read_bytes() == b"payload"
        assert len(calls) == 1 and calls[0][1] == target

    def test_interrupted_run_is_recovered(self, tmp_path):
        gate = self._load_gate()
        target = tmp_path / "target.py"
        original, mutant = b"x = 1\n", b"x = 2\n"
        target.write_bytes(original)
        gate._write_journal(target, original, mutant)
        gate._write_atomic(target, mutant)
        # Abrupt termination: no restore ran, the journal is still there.
        errors = gate._recover_pending([target])
        assert errors == []
        assert target.read_bytes() == original
        assert not gate._journal_path(target).exists()

    def test_stale_journal_with_restored_target_is_removed(self, tmp_path):
        gate = self._load_gate()
        target = tmp_path / "target.py"
        original, mutant = b"x = 1\n", b"x = 2\n"
        target.write_bytes(original)
        gate._write_journal(target, original, mutant)
        # Crash landed between the journal write and the mutant write.
        errors = gate._recover_pending([target])
        assert errors == []
        assert target.read_bytes() == original
        assert not gate._journal_path(target).exists()

    def test_conflicting_target_is_reported_not_overwritten(self, tmp_path):
        gate = self._load_gate()
        target = tmp_path / "target.py"
        original, mutant = b"x = 1\n", b"x = 2\n"
        edit = b"x = 3  # post-crash edit\n"
        target.write_bytes(original)
        gate._write_journal(target, original, mutant)
        gate._write_atomic(target, mutant)
        target.write_bytes(edit)
        errors = gate._recover_pending([target])
        assert len(errors) == 1 and "reconcile" in errors[0]
        assert target.read_bytes() == edit
        assert gate._journal_path(target).exists()
