"""Mutation-runner timeouts must not strand children or mutated source."""
import importlib.util
from pathlib import Path
import subprocess
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def gate(monkeypatch):
    path = Path(__file__).with_name("mutation_gate_pattern_expression_length.py")
    spec = importlib.util.spec_from_file_location("expression_gate_safety", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(module.os, "killpg", MagicMock(), raising=False)
    import signal
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    # Never execute a real termination command, including before the fix.
    monkeypatch.setattr(module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
    return module


@pytest.mark.parametrize("windows", [False, True])
def test_timeout_terminates_tree_and_bounds_pipe_cleanup(gate, monkeypatch, tmp_path, windows):
    monkeypatch.setattr(gate, "IS_WINDOWS", windows)
    process = MagicMock(pid=12345)
    process.communicate.side_effect = [subprocess.TimeoutExpired("tests", 240), ("", "")]
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(gate.subprocess, "Popen", spawn)
    with pytest.raises(subprocess.TimeoutExpired):
        gate.run_tests(tmp_path, 1)
    assert spawn.call_args.kwargs.get("start_new_session") is (not windows)
    cleanup_timeout = process.communicate.call_args.kwargs.get("timeout")
    assert cleanup_timeout is not None and 0 < cleanup_timeout <= 30
    if windows:
        command = gate.subprocess.run.call_args.args[0]
        assert command == ["taskkill", "/PID", "12345", "/T", "/F"]
        assert 0 < gate.subprocess.run.call_args.kwargs.get("timeout", 0) <= 30
    else:
        gate.os.killpg.assert_called_once_with(12345, 9)
        process.kill.assert_not_called()


@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_cleanup_aborts_sweep_and_restores_source(gate, monkeypatch, tmp_path, interrupted):
    target = tmp_path / "subject.py"
    original = b"value = 1\n"
    target.write_bytes(original)
    monkeypatch.setattr(gate, "MUTATIONS", [
        ("first", target, "value = 1", "value = 2"),
        ("second", target, "value = 1", "value = 3"),
    ])
    monkeypatch.setattr(gate.sys, "argv", ["gate"])
    failure = KeyboardInterrupt() if interrupted else subprocess.TimeoutExpired("tests", 240)
    process = MagicMock(pid=12345)
    process.communicate.side_effect = [failure, subprocess.TimeoutExpired("cleanup", 10)]
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(gate.subprocess, "Popen", spawn)
    run_tests = gate.run_tests
    monkeypatch.setattr(gate, "run_tests", lambda directory, index:
                        ({"baseline"}, set()) if index == 0 else run_tests(directory, index))
    with pytest.raises(KeyboardInterrupt if interrupted else RuntimeError):
        gate.main()
    assert target.read_bytes() == original
    assert spawn.call_count == 1  # no next mutation while children might remain
