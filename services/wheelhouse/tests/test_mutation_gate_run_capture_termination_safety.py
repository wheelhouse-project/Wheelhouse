"""Exercise the gate on temporary source; never mutate production in these tests."""
from pathlib import Path
import json
from types import SimpleNamespace
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

import mutation_gate_owned_guard as owned
import mutation_gate_run_capture_termination as gate


@pytest.fixture
def synthetic_gate(tmp_path, monkeypatch):
    target = tmp_path / "subject.py"
    original = b"value = 1\r\n"
    target.write_bytes(original)
    mutations = [(name, "value = 1", f"value = {value}", "test_probe", "guard failed")
                 for name, value in (("first", 2), ("second", 3))]
    monkeypatch.setattr(gate, "REPO", tmp_path)
    monkeypatch.setattr(gate, "TARGET", target)
    monkeypatch.setattr(gate, "MUTATIONS", mutations)
    monkeypatch.setattr(sys, "argv", ["gate"])
    return target, original


def _install_transport(monkeypatch, target, behavior):
    calls = []

    def run(command, **kwargs):
        calls.append(target.read_bytes())
        report = Path(next(arg.split("=", 1)[1] for arg in command
                           if arg.startswith("--junitxml=")))
        return behavior(command, kwargs, report, calls)

    # The old gate and the corrected gate reach the same subprocess boundary.
    # Neither transport starts a real process in these synthetic regressions.
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(owned, "run_owned", run)
    return calls


def _report(command, report, *, failure=False):
    suite = ET.Element("testsuite")
    case = ET.SubElement(suite, "testcase", name="test_probe")
    if failure:
        ET.SubElement(case, "failure", message="AssertionError: guard failed")
    ET.ElementTree(suite).write(report)
    return SimpleNamespace(returncode=int(failure), stdout="", stderr="",
                           cleanup_confirmed=True)


@pytest.mark.parametrize("uncertain_at", [1, 2])
def test_unconfirmed_cleanup_never_restores_or_continues(
    synthetic_gate, monkeypatch, uncertain_at,
):
    target, original = synthetic_gate
    uncertainty = owned.CleanupUnconfirmedError("synthetic lifetime exit is unconfirmed")

    def behavior(command, kwargs, report, calls):
        if len(calls) == uncertain_at:
            raise uncertainty
        return _report(command, report, failure=len(calls) > 1)

    calls = _install_transport(monkeypatch, target, behavior)
    escaped = None
    try:
        gate.main()
    except owned.CleanupUnconfirmedError as exc:
        escaped = exc
    assert escaped is uncertainty, "cleanup uncertainty must escape, never become a catch/error count"
    assert len(calls) == uncertain_at, "no retry or next mutation may run after uncertainty"
    expected = original if uncertain_at == 1 else b"value = 2\r\n"
    assert target.read_bytes() == expected, "unconfirmed cleanup must not restore over descendants"


def test_normal_sweep_keeps_named_catchers_and_exact_restoration(synthetic_gate, monkeypatch):
    target, original = synthetic_gate
    calls = _install_transport(monkeypatch, target, lambda command, kwargs, report, calls:
                               _report(command, report, failure=len(calls) > 1))
    assert gate.main() == 0
    assert calls == [original, b"value = 2\r\n", b"value = 3\r\n"]
    assert target.read_bytes() == original


@pytest.mark.parametrize("interrupted", [False, True])
def test_partial_publication_preserves_original_before_any_mutant_runs(
    synthetic_gate, monkeypatch, interrupted,
):
    target, original = synthetic_gate
    mutant = b"value = 2\r\n"
    stop = KeyboardInterrupt("synthetic partial write") if interrupted else OSError("synthetic disk full")
    write_bytes = Path.write_bytes
    faults = []

    def partial_write(path, data):
        if data == mutant and not faults:
            write_bytes(path, data[:4])
            faults.append(target.read_bytes())
            raise stop
        return write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", partial_write)
    calls = _install_transport(monkeypatch, target, lambda command, kwargs, report, calls:
                               _report(command, report, failure=len(calls) > 1))
    result = escaped = None
    try:
        result = gate.main()
    except BaseException as exc:
        escaped = exc
    assert target.read_bytes() == original, "publication failure must never strand partial target bytes"
    assert faults == [original], "failed temporary writes must leave the tracked target intact"
    if interrupted:
        assert escaped is stop
        assert calls == [original]
    else:
        assert escaped is None
        assert result == 1
        assert calls == [original, b"value = 3\r\n"]
    assert not list(target.parent.glob("*.mutation-gate.*.tmp"))


def test_interrupt_after_atomic_publication_restores_before_abort(synthetic_gate, monkeypatch):
    target, original = synthetic_gate
    mutant = b"value = 2\r\n"
    stop = KeyboardInterrupt("synthetic completed replace")
    replace = gate._cleanup.os.replace
    faults = []

    def interrupted_replace(source, destination):
        replace(source, destination)
        if Path(destination) == target and target.read_bytes() == mutant and not faults:
            faults.append(target.read_bytes())
            raise stop

    monkeypatch.setattr(gate._cleanup.os, "replace", interrupted_replace)
    calls = _install_transport(monkeypatch, target, lambda command, kwargs, report, calls:
                               _report(command, report, failure=len(calls) > 1))
    with pytest.raises(KeyboardInterrupt) as caught:
        gate.main()
    assert caught.value is stop
    assert faults == [mutant]
    assert calls == [original], "an interrupted publication must not start a mutant or another mutation"
    assert target.read_bytes() == original
    assert not list(target.parent.glob("*.mutation-gate.*.tmp"))


def test_uncertainty_retains_original_mutant_and_blocks_a_new_invocation(
    synthetic_gate, monkeypatch,
):
    target, original = synthetic_gate
    uncertainty = owned.CleanupUnconfirmedError("synthetic unknown descendant")

    def behavior(command, kwargs, report, calls):
        if len(calls) > 1:
            raise uncertainty
        return _report(command, report)

    calls = _install_transport(monkeypatch, target, behavior)
    with pytest.raises(owned.CleanupUnconfirmedError):
        gate.main()
    marker = target.parent / ".tmp/run-capture-cleanup-unconfirmed.json"
    state = json.loads(marker.read_text())
    evidence = Path(state["evidence_dir"])
    assert state["cleanup_confirmed"] is False
    assert [p.read_bytes() for p in evidence.glob("*.original")] == [original]
    assert [p.read_bytes() for p in evidence.glob("*.mutant")] == [b"value = 2\r\n"]
    with pytest.raises(owned.CleanupUnconfirmedError, match="Prior cleanup"):
        gate.main()
    assert len(calls) == 2
    assert target.read_bytes() == b"value = 2\r\n"


@pytest.mark.parametrize("interrupted", [False, True])
def test_confirmed_cleanup_precedes_restore_and_interrupt_stops_next(
    synthetic_gate, monkeypatch, interrupted,
):
    target, original = synthetic_gate
    events = []
    stop = KeyboardInterrupt("confirmed interruption") if interrupted else subprocess.TimeoutExpired([], 180)
    stop.cleanup_confirmed = True

    def behavior(command, kwargs, report, calls):
        assert kwargs["timeout_seconds"] == 180
        events.append(("run", target.read_bytes()))
        if len(calls) == 2:
            events.append(("cleanup-confirmed", target.read_bytes()))
            raise stop
        return _report(command, report, failure=len(calls) > 1)

    restore = gate._restore

    def observe_restore(*args):
        events.append(("restore", target.read_bytes()))
        return restore(*args)

    monkeypatch.setattr(gate, "_restore", observe_restore)
    calls = _install_transport(monkeypatch, target, behavior)
    if interrupted:
        with pytest.raises(KeyboardInterrupt) as caught:
            gate.main()
        assert caught.value is stop
        assert len(calls) == 2
    else:
        assert gate.main() == 1
        assert len(calls) == 3
    assert events[2:4] == [("cleanup-confirmed", b"value = 2\r\n"),
                           ("restore", b"value = 2\r\n")]
    assert target.read_bytes() == original


def test_restore_entry_interrupt_is_retried_before_abort(synthetic_gate, monkeypatch):
    target, original = synthetic_gate
    restore_code = gate._restore.__code__
    stop = KeyboardInterrupt("at confirmed restoration entry")
    entries = []

    def trace(frame, event, arg):
        if event == "call" and frame.f_code is restore_code:
            entries.append(frame.f_lineno)
            raise stop
        return trace

    def behavior(command, kwargs, report, calls):
        result = _report(command, report, failure=len(calls) > 1)
        if len(calls) == 2:
            sys.settrace(trace)
        return result

    calls = _install_transport(monkeypatch, target, behavior)
    previous = sys.gettrace()
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            gate.main()
    finally:
        sys.settrace(previous)
    assert caught.value is stop
    assert entries == [restore_code.co_firstlineno]
    assert calls == [original, b"value = 2\r\n"]
    assert target.read_bytes() == original, "entry interruption must not strand mutant bytes"


@pytest.mark.parametrize("after_replace", [False, True])
def test_restore_body_interrupt_restores_then_stops_next_mutation(
    synthetic_gate, monkeypatch, after_replace,
):
    target, original = synthetic_gate
    mutant = b"value = 2\r\n"
    stop = KeyboardInterrupt("inside confirmed restoration body")
    atomic_write = gate._cleanup._atomic_write_bytes
    interrupted = []

    def interrupt_restoration(path, data):
        if path == target and data == original and not interrupted:
            assert target.read_bytes() == mutant
            interrupted.append(target.read_bytes())
            if after_replace:
                atomic_write(path, data)
            raise stop
        return atomic_write(path, data)

    monkeypatch.setattr(gate._cleanup, "_atomic_write_bytes", interrupt_restoration)
    calls = _install_transport(monkeypatch, target, lambda command, kwargs, report, calls:
                               _report(command, report, failure=len(calls) > 1))
    escaped = None
    try:
        gate.main()
    except KeyboardInterrupt as exc:
        escaped = exc
    assert interrupted == [mutant], "the fault must occur inside the restore body"
    assert target.read_bytes() == original, "confirmed restoration must finish before abort"
    assert calls == [original, mutant], "Ctrl+C during restoration must not run the next mutation"
    assert escaped is stop, "the original cancellation must escape after restoration"


def test_check_does_not_create_evidence_or_spawn(synthetic_gate, monkeypatch):
    target, original = synthetic_gate
    monkeypatch.setattr(sys, "argv", ["gate", "--check"])

    def forbidden(*args, **kwargs):
        pytest.fail("check must not create ownership state or launch processes")

    monkeypatch.setattr(gate, "MutationRun", forbidden)
    monkeypatch.setattr(owned, "run_owned", forbidden)
    assert gate.main() == 0
    assert target.read_bytes() == original
    assert not (target.parent / ".tmp").exists()


def test_red_baseline_refuses_all_mutations(synthetic_gate, monkeypatch):
    target, original = synthetic_gate
    calls = _install_transport(monkeypatch, target, lambda command, kwargs, report, calls:
                               _report(command, report, failure=True))
    with pytest.raises(RuntimeError, match="baseline is not green"):
        gate.main()
    assert calls == [original]
    assert target.read_bytes() == original


def test_reports_and_bytecode_roots_are_fresh_per_owned_invocation(synthetic_gate, monkeypatch):
    target, original = synthetic_gate
    paths = []

    def behavior(command, kwargs, report, calls):
        assert kwargs["cwd"] == target.parent
        assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
        cache = Path(kwargs["env"]["PYTHONPYCACHEPREFIX"])
        assert cache.is_dir()
        assert not report.exists(), "a prior report must never provide a fresh verdict"
        assert (report, cache) not in paths
        paths.append((report, cache))
        return _report(command, report, failure=len(calls) > 1)

    _install_transport(monkeypatch, target, behavior)
    assert gate.main() == 0
    assert len(paths) == 3
    assert all(report.exists() for report, cache in paths)


def test_marker_write_failure_does_not_clear_ownership_interlock(synthetic_gate, monkeypatch):
    target, original = synthetic_gate
    uncertainty = owned.CleanupUnconfirmedError("synthetic unknown descendant")
    write_text = Path.write_text

    def write(path, *args, **kwargs):
        if path.name == "run-capture-cleanup-unconfirmed.json":
            raise OSError("synthetic evidence write refusal")
        return write_text(path, *args, **kwargs)

    def behavior(command, kwargs, report, calls):
        if len(calls) > 1:
            raise uncertainty
        return _report(command, report)

    monkeypatch.setattr(Path, "write_text", write)
    calls = _install_transport(monkeypatch, target, behavior)
    with pytest.raises(owned.CleanupUnconfirmedError) as caught:
        gate.main()
    assert caught.value is uncertainty
    assert len(calls) == 2
    assert target.read_bytes() == b"value = 2\r\n"
