"""Drive the provider gate itself through uncertain and confirmed cleanup."""
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
import mutation_gate_owned_guard as owned
import mutation_gate_provider_env_check as gate


@pytest.mark.parametrize("uncertain", [True, False], ids=["unconfirmed", "confirmed-interrupt"])
def test_provider_caller_preserves_source_until_cleanup_is_confirmed(tmp_path, monkeypatch, uncertain):
    source = tmp_path / "subject.py"
    original = b"value = 1\n"
    mutant = b"value = 2\n"
    source.write_bytes(original)
    mutations = [gate.mutation(name, "value = 1", "value = 2", "test_probe", "assert", file=source)
                 for name in ("first", "must-not-run")]
    monkeypatch.setattr(gate, "MUTATIONS", mutations)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["gate"])
    calls, clears = [], []
    monkeypatch.setattr(gate, "clear_bytecode", lambda: clears.append(len(calls)))
    error = owned.CleanupUnconfirmedError("owned child exit unknown") if uncertain else KeyboardInterrupt()
    if not uncertain:
        error.cleanup_confirmed = True
    def process(command, **kwargs):
        calls.append(source.read_bytes())
        if len(calls) == 1:
            root = ET.Element("testsuite")
            ET.SubElement(root, "testcase", name="test_probe")
            ET.ElementTree(root).write(Path(command[command.index("--junitxml") + 1]))
            return subprocess.CompletedProcess(command, 0, "", "")
        assert source.read_bytes() == mutant
        raise error
    monkeypatch.setattr(gate.subprocess, "run", process)
    monkeypatch.setattr(owned, "run_owned", process)
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
