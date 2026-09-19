"""Verdict and source-restoration boundaries of the PTT author-time gate."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def gate(tmp_path, monkeypatch):
    path = Path(__file__).with_name("mutation_gate_ptt_pending_state.py")
    spec = importlib.util.spec_from_file_location("ptt_pending_gate_test", path)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", sys.__stdout__)
        spec.loader.exec_module(module)
    module.EVIDENCE = tmp_path / "evidence"
    module.UNSAFE_MARKER = module.EVIDENCE / "cleanup-unconfirmed.json"
    return module


def test_ptt_pending_gate_check_never_spawns_or_writes(gate, monkeypatch, tmp_path):
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
def test_ptt_pending_gate_uses_fresh_attributed_junit(gate, monkeypatch, verdict):
    class CleanupUnconfirmedError(RuntimeError):
        cleanup_confirmed = False

    def run_owned(command, cwd, env, timeout_seconds, evidence_dir):
        assert command[:2] == [sys.executable, str(gate.ROOT / "scripts/run_tests.py")]
        assert cwd == gate.ROOT
        assert timeout_seconds == 180
        assert Path(env["PYTHONPYCACHEPREFIX"]).parent == evidence_dir
        report = Path(command[command.index("--junitxml") + 1])
        assert not report.exists()
        body = {"pass": "", "assertion": '<failure message="assert False"/>',
                "error": '<error message="setup failed"/>',
                "nonassertion": '<failure message="AttributeError: broken"/>',
                "skip": '<skipped/>', "missing": "", "mismatch": '<failure message="assert False"/>'}[verdict]
        if verdict != "missing":
            report.write_text(f'<testsuites><testsuite><testcase name="case" time="0.25">{body}</testcase></testsuite></testsuites>')
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


@pytest.mark.parametrize("form", ["exception", "result"])
def test_ptt_pending_gate_unconfirmed_cleanup_refuses_restore_and_next_run(gate, monkeypatch, tmp_path, form):
    class CleanupUnconfirmedError(RuntimeError):
        cleanup_confirmed = False

    def run_owned(*args):
        if form == "exception":
            raise CleanupUnconfirmedError("owned child still running")
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


def test_ptt_pending_gate_keeps_original_backup_and_restores_caught_mutant(gate, monkeypatch, tmp_path):
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
    entry, = json.loads(manifest.read_text())
    assert Path(entry["backup"]).read_bytes() == original
    assert len(calls) == 2
