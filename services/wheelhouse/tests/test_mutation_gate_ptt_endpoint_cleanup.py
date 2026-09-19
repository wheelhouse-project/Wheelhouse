"""Exercise the endpoint caller's real shared mutation loop cleanup path."""
import subprocess
import sys

import pytest
import mutation_gate_owned_guard as owned
import mutation_gate_ptt_endpoint_identity as gate
import mutation_gate_ptt_mode_consistency as adapter


@pytest.mark.parametrize("uncertain", [True, False], ids=["unconfirmed", "confirmed-interrupt"])
def test_endpoint_caller_preserves_source_until_cleanup_is_confirmed(tmp_path, monkeypatch, uncertain):
    source = tmp_path / "subject.py"
    original, mutant = b"value = 1\n", b"value = 2\n"
    source.write_bytes(original)
    mutations = [dict(name=name, file=source, old="value = 1", new="value = 2",
                      service=tmp_path, test_file="test_probe.py", expect=["test_probe"])
                 for name in ("first", "must-not-run")]
    monkeypatch.setattr(gate, "MUTATIONS", mutations)
    monkeypatch.setattr(adapter, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["gate"])
    monkeypatch.setattr(gate.runner, "_collected_names", lambda *a: {"test_probe"})
    calls, clears = [], []
    monkeypatch.setattr(gate.runner, "_clear_pycache", lambda *a: clears.append(len(calls)))
    error = owned.CleanupUnconfirmedError("owned child exit unknown") if uncertain else KeyboardInterrupt()
    if not uncertain:
        error.cleanup_confirmed = True
    def process(command, **kwargs):
        calls.append(source.read_bytes())
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 0, "", "")
        assert source.read_bytes() == mutant
        raise error
    monkeypatch.setattr(adapter.subprocess, "run", process)
    monkeypatch.setattr(owned, "run_owned", process)
    def run_tests(*args):
        return adapter.subprocess.run(["synthetic"], cwd=tmp_path, env={}, timeout=1,
                                      capture_output=True, text=True)
    monkeypatch.setattr(gate.runner, "_run_pytest", run_tests)
    with pytest.raises(type(error)):
        gate.main()
    assert len(calls) == 2, "uncertain cleanup or interruption must abort the next mutant"
    if uncertain:
        assert source.read_bytes() == mutant, "a possibly live descendant must retain its mutant source"
        assert 2 not in clears, "bytecode must not be cleared after unconfirmed cleanup"
        backups = list(tmp_path.glob(".tmp/mutation-owned/*/*.original"))
        assert len(backups) == 1 and backups[0].read_bytes() == original
        assert backups[0].with_suffix(".mutant").read_bytes() == mutant
    else:
        assert source.read_bytes() == original, "confirmed interruption must restore original bytes"
        assert 2 in clears
