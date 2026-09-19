"""The gate must not restore files beneath possibly running descendants."""
from pathlib import Path
import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import mutation_gate_captured_target_identity as gate


class Unconfirmed(OSError):
    cleanup_confirmed = False


@pytest.fixture
def harness(tmp_path, monkeypatch):
    service = tmp_path / "services/wheelhouse"
    service.mkdir(parents=True)
    source = service / "source.py"
    source.write_bytes(b"value = 1\n")
    state = SimpleNamespace(source=source, launches=[], options=[], uncertain=None, invalid=None)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "SERVICE", service)
    monkeypatch.setattr(gate.runner, "ROOT", tmp_path)
    monkeypatch.setattr(gate.runner, "EVIDENCE_ROOT", tmp_path / "runner-evidence")
    monkeypatch.setattr(gate.runner, "_cleanup_confirmed", True)
    monkeypatch.setenv("QT_QPA_PLATFORM", "synthetic-original")
    mutations = [dict(name=str(value), service=service, test_file="tests/test_value.py",
                      file=source, old="value = 1", new=f"value = {value}",
                      expect=["test_value"]) for value in (2, 3)]
    monkeypatch.setattr(gate, "MUTATIONS", mutations)

    def launched(command, *args, **kwargs):
        state.launches.append(source.read_bytes())
        env = args[1] if len(args) > 1 else kwargs["env"]
        timeout = args[2] if len(args) > 2 else kwargs["timeout_seconds"]
        state.options.append((env["QT_QPA_PLATFORM"], timeout, command[1]))
        if state.uncertain == len(state.launches):
            raise Unconfirmed("synthetic exit uncertainty")
        report = Path(next(arg.split("=", 1)[1] for arg in command
                           if arg.startswith("--junitxml=")))
        report.parent.mkdir(parents=True, exist_ok=True)
        child = f"<{state.invalid}/>" if state.invalid else ""
        if source.read_bytes() != b"value = 1\n":
            child = "<failure/>" + child
        report.write_text('<testsuites><testsuite><testcase name="test_value">'
                          + child + '</testcase></testsuite></testsuites>')
        collect = "--collect-only" in command
        failed = source.read_bytes() != b"value = 1\n"
        # The shared runner (0618a95b, 2026-09-09) counts a run with no
        # pytest result line as an error, not a verdict, so the fake child
        # prints the summary line real pytest prints.
        output = ("tests/test_value.py::test_value\n" if collect else
                  "FAILED tests/test_value.py::test_value\n1 failed in 0.01s\n" if failed else
                  "1 passed in 0.01s\n")
        result = subprocess.CompletedProcess(command, int(failed and not collect), output, "")
        result.cleanup_confirmed = True
        return result

    monkeypatch.setattr(gate.runner, "_owned", lambda: SimpleNamespace(
        run_owned=launched, CleanupUnconfirmedError=Unconfirmed))
    state.root, state.mutations = tmp_path, mutations
    return state


def test_uncertain_call_blocks_fresh_gate_before_any_launch(harness, monkeypatch):
    harness.uncertain = 1
    first = gate.OwnedGate(harness.root / "first")
    with pytest.raises(OSError):
        first.call(gate.SERVICE, "tests/test_value.py", collect=True)
    assert len(harness.launches) == 1
    monkeypatch.setattr(gate.runner, "_cleanup_confirmed", True)
    second = gate.OwnedGate(harness.root / "second")
    try:
        status = second.run(harness.mutations, [])
    except RuntimeError as exc:
        status = str(exc)
    assert len(harness.launches) == 1, "fresh gate launched despite unresolved owned exit"
    assert status == 1
    assert list(gate.runner.EVIDENCE_ROOT.rglob("cleanup-pending.json"))
    assert harness.source.read_bytes() == b"value = 1\n"


def test_uncertain_mutation_returns_failure_without_restore_or_next_mutant(harness):
    harness.uncertain = 3
    owner = gate.OwnedGate(harness.root / "attempt")
    try:
        status = owner.run(harness.mutations, [])
    except RuntimeError as exc:
        status = str(exc)
    assert harness.launches == [b"value = 1\n", b"value = 1\n", b"value = 2\n"]
    assert harness.source.read_bytes() == b"value = 2\n"
    assert (owner.evidence_dir / "original-0.bin").read_bytes() == b"value = 1\n"
    assert (owner.evidence_dir / "current-0.bin").read_bytes() == b"value = 2\n"
    assert status == 1, "the shared runner's failure verdict must reach the script caller"
    assert list(gate.runner.EVIDENCE_ROOT.rglob("cleanup-pending.json"))



def test_confirmed_mutations_restore_bytes_and_keep_recovery_evidence(harness):
    owner = gate.OwnedGate(harness.root / "attempt")
    assert owner.run(harness.mutations, []) == 0
    assert harness.launches == [b"value = 1\n", b"value = 1\n", b"value = 2\n", b"value = 3\n"]
    assert harness.source.read_bytes() == b"value = 1\n"
    assert harness.options == [("offscreen", 300, str(harness.root / "scripts/run_tests.py"))] * 4
    assert os.environ["QT_QPA_PLATFORM"] == "synthetic-original"
    assert not list(gate.runner.EVIDENCE_ROOT.rglob("cleanup-pending.json"))
    originals = list(gate.runner.EVIDENCE_ROOT.rglob("source.original.bin"))
    assert len(originals) == 2
    assert all(path.read_bytes() == b"value = 1\n" for path in originals)
    mutants = list(gate.runner.EVIDENCE_ROOT.rglob("source.mutant.bin"))
    assert {path.read_bytes() for path in mutants} == {b"value = 2\n", b"value = 3\n"}
    report = json.loads((owner.evidence_dir / "mutations.json").read_text())
    assert report["cleanup_confirmed"] is True
    assert all(report["restored"].values())


@pytest.mark.parametrize("invalid", ["error", "skipped"])
def test_invalid_junit_cannot_count_as_a_catch(harness, invalid):
    harness.invalid = invalid
    owner = gate.OwnedGate(harness.root / "attempt")
    with pytest.raises(RuntimeError, match="Invalid mutation verdict"):
        owner.call(gate.SERVICE, "tests/test_value.py")
    assert gate.runner._cleanup_confirmed is True
    assert not list(gate.runner.EVIDENCE_ROOT.rglob("cleanup-pending.json"))
    assert harness.source.read_bytes() == b"value = 1\n"


def test_uncertain_call_has_no_scored_verdict(harness):
    harness.uncertain = 1
    owner = gate.OwnedGate(harness.root / "attempt")
    with pytest.raises(OSError):
        owner.call(gate.SERVICE, "tests/test_value.py")
    assert owner.reports[0]["cleanup_confirmed"] is False
    assert "failed" not in owner.reports[0]


def test_confirmed_body_interruption_finishes_restore_before_propagating(harness, monkeypatch):
    original_write = gate.runner._atomic_write_bytes
    original_clear = gate.runner._clear_pycache
    interrupted = False
    cache_after_interrupt = []

    def write(path, data):
        nonlocal interrupted
        original_write(path, data)
        if data == b"value = 1\n" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("synthetic completed restoration")

    def clear(service):
        if interrupted:
            cache_after_interrupt.append(harness.source.read_bytes())
        return original_clear(service)

    monkeypatch.setattr(gate.runner, "_atomic_write_bytes", write)
    monkeypatch.setattr(gate.runner, "_clear_pycache", clear)
    owner = gate.OwnedGate(harness.root / "attempt")
    with pytest.raises(KeyboardInterrupt):
        owner.run(harness.mutations, [])
    assert harness.source.read_bytes() == b"value = 1\n"
    assert len(harness.launches) == 3
    assert cache_after_interrupt == [b"value = 1\n"]
    assert not list(gate.runner.EVIDENCE_ROOT.rglob("cleanup-pending.json"))


def test_check_never_loads_owned_helper_or_creates_evidence(harness, monkeypatch):
    load = Mock(side_effect=AssertionError("--check must not load owned execution"))
    monkeypatch.setattr(gate.runner, "_owned", load)
    assert gate.main(["--check"]) == 0
    load.assert_not_called()
    assert not harness.launches
    assert not gate.runner.EVIDENCE_ROOT.exists()
    assert not (harness.root / ".tmp/captured-target-mutations").exists()
