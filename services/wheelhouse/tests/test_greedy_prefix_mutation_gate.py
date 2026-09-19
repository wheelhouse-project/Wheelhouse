"""Verdict and cleanup boundaries of the class-separator author-time gate."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def gate(tmp_path, monkeypatch):
    path = Path(__file__).with_name("mutation_gate_greedy_prefix_class_separator.py")
    spec = importlib.util.spec_from_file_location("greedy_class_gate_test", path)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", sys.__stdout__)
        spec.loader.exec_module(module)
    module.EVIDENCE = tmp_path / "evidence"
    module.UNSAFE_MARKER = module.EVIDENCE / "cleanup-unconfirmed.json"
    return module


def test_greedy_class_check_never_spawns_or_writes(gate, monkeypatch, tmp_path):
    target = tmp_path / "target.py"
    target.write_bytes(b"enabled = True\n")
    gate.MUTATIONS = [dict(name="one", service=tmp_path, test_file=("case",),
                          file=target, old="enabled = True\n", new="enabled = False\n", expect=["case"])]

    def forbidden(*args, **kwargs):
        pytest.fail("check-only entered execution or source modification")

    monkeypatch.setattr(gate, "_invoke", forbidden)
    monkeypatch.setattr(gate.runner, "_atomic_write_bytes", forbidden)
    monkeypatch.setattr(gate, "_shared_restore", forbidden)
    assert gate.main(["--check"]) == 0
    assert target.read_bytes() == b"enabled = True\n"
    assert not gate.EVIDENCE.exists()


@pytest.mark.parametrize("verdict", ["pass", "assertion", "error", "nonassertion", "skip", "missing", "mismatch"])
def test_greedy_class_uses_fresh_attributed_junit(gate, monkeypatch, verdict):
    class CleanupUnconfirmedError(RuntimeError):
        cleanup_confirmed = False

    def run_owned(command, cwd, env, timeout_seconds, evidence_dir):
        assert command[:2] == [sys.executable, str(gate.ROOT / "scripts/run_tests.py")]
        assert cwd == gate.ROOT
        assert timeout_seconds == 180
        assert command[command.index("-k") + 1] == gate.SELECTION
        assert Path(env["PYTHONPYCACHEPREFIX"]).parent == evidence_dir
        assert gate.UNSAFE_MARKER.exists()
        assert not gate._cleanup_confirmed
        report = Path(command[command.index("--junitxml") + 1])
        assert not report.exists()
        body = {"pass": "", "assertion": '<failure message="assert False"/>',
                "error": '<error message="setup failed"/>',
                "nonassertion": '<failure message="AttributeError: broken"/>',
                "skip": '<skipped/>', "missing": "", "mismatch": '<failure message="assert False"/>'}[verdict]
        if verdict != "missing":
            report.write_text(f'<testsuites><testsuite><testcase name="case[value]" time="0.25">{body}</testcase></testsuite></testsuites>')
        result = subprocess.CompletedProcess(command, 1 if verdict in ("assertion", "error", "nonassertion") else 0, "", "")
        result.cleanup_confirmed = True
        return result

    owned = SimpleNamespace(run_owned=run_owned, CleanupUnconfirmedError=CleanupUnconfirmedError)
    monkeypatch.setattr(gate, "_load", lambda *args: owned)
    if verdict in ("pass", "assertion"):
        result = gate._invoke(gate.SERVICE, gate.TESTS)
        # The gate carries its own counts, and the duration is the one
        # in the report, not a constant (wh-codex-merge-audit.13.1.3).
        assert result.stdout == ("FAILED case\n1 failed in 0.25s"
                                 if verdict == "assertion"
                                 else "1 passed in 0.25s")
    else:
        with pytest.raises(RuntimeError):
            gate._invoke(gate.SERVICE, gate.TESTS)
    assert gate._cleanup_confirmed
    assert not gate.UNSAFE_MARKER.exists()


@pytest.mark.parametrize("form", ["exception", "result", "unexpected"])
def test_greedy_class_unknown_cleanup_blocks_restore_and_next_run(gate, monkeypatch, tmp_path, form):
    class CleanupUnconfirmedError(RuntimeError):
        cleanup_confirmed = False

    def run_owned(*args):
        if form == "exception":
            raise CleanupUnconfirmedError("owned child still running")
        if form == "unexpected":
            raise RuntimeError("no cleanup proof")
        return SimpleNamespace(cleanup_confirmed=False)

    monkeypatch.setattr(gate, "_load", lambda *args: SimpleNamespace(
        run_owned=run_owned, CleanupUnconfirmedError=CleanupUnconfirmedError))
    target = tmp_path / "target.py"
    original, mutated = b"original\n", b"mutated\n"
    target.write_bytes(mutated)
    with pytest.raises(RuntimeError):
        gate._invoke(gate.SERVICE, gate.TESTS)
    assert "restoration is unsafe" in gate._restore(target, original, mutated, "one")
    assert target.read_bytes() == mutated
    assert gate.UNSAFE_MARKER.exists()
    assert gate.main([]) == 1


def test_greedy_class_confirmed_timeout_allows_exact_restore(gate, monkeypatch, tmp_path):
    class CleanupUnconfirmedError(RuntimeError):
        cleanup_confirmed = False

    def run_owned(command, *args):
        error = subprocess.TimeoutExpired(command, 180)
        error.cleanup_confirmed = True
        raise error

    monkeypatch.setattr(gate, "_load", lambda *args: SimpleNamespace(
        run_owned=run_owned, CleanupUnconfirmedError=CleanupUnconfirmedError))
    target = tmp_path / "target.py"
    original, mutated = b"original\r\n", b"mutated\r\n"
    target.write_bytes(mutated)
    with pytest.raises(subprocess.TimeoutExpired):
        gate._invoke(gate.SERVICE, gate.TESTS)
    assert gate._restore(target, original, mutated, "one") is None
    assert target.read_bytes() == original
    assert not gate.UNSAFE_MARKER.exists()


def test_greedy_class_keeps_both_backups_and_restores_caught_mutant(gate, monkeypatch, tmp_path):
    target = tmp_path / "target.py"
    original = b"enabled = True\r\n"
    target.write_bytes(original)
    gate.MUTATIONS = [dict(name="one", service=tmp_path, test_file=("case",), file=target,
                          old="enabled = True\n", new="enabled = False\n", expect=["case"])]
    calls = []

    def invoke(service, tests, collect=False):
        if collect:
            return {"case"}
        calls.append(target.read_bytes())
        if len(calls) == 1:
            assert target.read_bytes() == original
            return subprocess.CompletedProcess([], 0, "", "")
        assert target.read_bytes() == b"enabled = False\r\n"
        # The real _invoke appends the result line the shared runner
        # requires; a fake without it would prove nothing about the gate.
        return subprocess.CompletedProcess(
            [], 1, "FAILED case\n1 failed in 0.02s", "")

    monkeypatch.setattr(gate, "_invoke", invoke)
    assert gate.main([]) == 0
    assert target.read_bytes() == original
    manifest, = (gate.EVIDENCE / "source-backups").glob("*/manifest.json")
    entries = json.loads(manifest.read_text())
    assert len(entries) == 2
    assert Path(entries[0]["backup"]).read_bytes() == original
    assert Path(entries[1]["backup"]).read_bytes() == b"enabled = False\r\n"
    assert len(calls) == 2


def test_greedy_class_restore_entry_interrupt_restores_before_stopping(gate, monkeypatch, tmp_path):
    target = tmp_path / "subject.py"
    original, mutant = b"value = 1\r\n", b"value = 2\r\n"
    target.write_bytes(original)
    gate.MUTATIONS = [dict(name=name, service=tmp_path, test_file=("case",),
                          file=target, old="value = 1", new="value = 2", expect=["case"])
                      for name in ("first", "must-not-run")]
    calls, entries = [], []
    cleanup_code = gate._shared_restore.__code__
    interrupt = KeyboardInterrupt("at the underlying restore entry")

    def trace(frame, event, arg):
        if event == "call" and frame.f_code is cleanup_code:
            entries.append(frame.f_lineno)
            if len(entries) == 1:
                raise interrupt
        return trace

    def invoke(service, tests, collect=False):
        if collect:
            return {"case"}
        calls.append(target.read_bytes())
        if len(calls) == 1:
            return subprocess.CompletedProcess([], 0, "", "")
        assert target.read_bytes() == mutant
        # Arm only once a real temporary-file mutant has been published.
        sys.settrace(trace)
        return subprocess.CompletedProcess([], 1, "FAILED case", "")

    monkeypatch.setattr(gate, "_invoke", invoke)
    previous = sys.gettrace()
    escaped = None
    try:
        gate.main([])
    except KeyboardInterrupt as exc:
        escaped = exc
    finally:
        sys.settrace(previous)
    assert entries == [cleanup_code.co_firstlineno]
    assert target.read_bytes() == original, "confirmed cleanup must restore after an entry interrupt"
    assert escaped is interrupt, "defer the interrupt until restoration completes"
    assert calls == [original, mutant], "an interrupted sweep must not reach another mutation"


def test_greedy_class_unknown_cleanup_stops_real_loop_without_restore(gate, monkeypatch, tmp_path):
    target = tmp_path / "subject.py"
    original, mutant = b"value = 1\r\n", b"value = 2\r\n"
    target.write_bytes(original)
    gate.MUTATIONS = [dict(name=name, service=tmp_path, test_file=("case",),
                          file=target, old="value = 1", new="value = 2", expect=["case"])
                      for name in ("first", "must-not-run")]
    calls = []
    uncertainty = RuntimeError("synthetic cleanup uncertainty")

    def invoke(service, tests, collect=False):
        if collect:
            return {"case"}
        calls.append(target.read_bytes())
        if len(calls) == 1:
            return subprocess.CompletedProcess([], 0, "", "")
        gate._mark_uncertain(gate.EVIDENCE, uncertainty)
        raise uncertainty

    monkeypatch.setattr(gate, "_invoke", invoke)
    # Since 79b52ef3 the shared runner records an unexecuted mutant as an
    # error, never as caught, and returns 1; the cause stays in the marker.
    assert gate.main([]) == 1, "an unexecuted mutant must fail the gate"
    assert json.loads(gate.UNSAFE_MARKER.read_text(encoding="utf-8"))["error"] == str(uncertainty)
    assert target.read_bytes() == mutant, "uncertain cleanup must never restore source"
    assert calls == [original, mutant]
    assert gate.UNSAFE_MARKER.exists()
    assert gate.main([]) == 1
    assert calls == [original, mutant], "uncertainty must block later sweeps"


def test_greedy_class_entered_cleanup_is_not_blindly_retried(gate, monkeypatch, tmp_path):
    target = tmp_path / "subject.py"
    original, mutant = b"value = 1\n", b"value = 2\n"
    target.write_bytes(original)
    gate.MUTATIONS = [dict(name="one", service=tmp_path, test_file=("case",),
                          file=target, old="value = 1", new="value = 2", expect=["case"])]
    entered, calls = [], []
    interrupt = KeyboardInterrupt("body already entered")

    def restore(*args):
        entered.append("entered")
        raise interrupt

    def invoke(service, tests, collect=False):
        if collect:
            return {"case"}
        calls.append(target.read_bytes())
        return subprocess.CompletedProcess([], 0 if len(calls) == 1 else 1,
                                           "" if len(calls) == 1 else "FAILED case", "")

    monkeypatch.setattr(gate, "_invoke", invoke)
    monkeypatch.setattr(gate, "_shared_restore", restore)
    with pytest.raises(KeyboardInterrupt) as stopped:
        gate.main([])
    assert stopped.value is interrupt
    assert entered == ["entered"]
    assert calls == [original, mutant]
