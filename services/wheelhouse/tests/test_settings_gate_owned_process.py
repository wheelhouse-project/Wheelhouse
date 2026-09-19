"""Mutation adapters preserve a live mutant when child cleanup is uncertain."""
import importlib.util
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace
import pytest

HELPERS = Path(__file__).resolve().parents[3] / 'scripts' / 'codex'
sys.path.insert(0, str(HELPERS))
from owned_process import CleanupUnconfirmedError

@pytest.mark.parametrize('outcome', ['uncertain', 'returned-uncertain', 'timeout'])
@pytest.mark.parametrize('name', ['settings_ack', 'config_save_merge'])
def test_uncertain_cleanup_preserves_mutant_backup_and_bytecode(name, outcome, tmp_path, monkeypatch):
    script = Path(__file__).with_name('mutation_gate_' + name + '.py')
    spec = importlib.util.spec_from_file_location('owned_gate_' + name, script)
    gate = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as scoped:
        scoped.setattr(sys, 'stdout', sys.__stdout__)
        spec.loader.exec_module(gate)
    service = tmp_path / 'repo' / 'services' / 'wheelhouse'
    service.mkdir(parents=True)
    source = service / 'probe.py'
    original, mutant = b'value = 1\n', b'value = 2\n'
    source.write_bytes(original)
    monkeypatch.setattr(gate, 'SERVICE', service)
    monkeypatch.setattr(sys, 'argv', [str(script)])
    if name == 'settings_ack':
        monkeypatch.setattr(gate, 'ROOT', service.parents[1])
        monkeypatch.setattr(gate, 'MUTATIONS', [
            (label, 'probe.py', [('value = 1', 'value = 2')], 'test_probe')
            for label in ['first', 'must-not-run']])
    else:
        monkeypatch.setattr(gate, 'SRC', source)
        monkeypatch.setattr(gate, 'MUTATIONS', [
            dict(name=label, old='value = 1', new='value = 2', expect=['test_probe'])
            for label in ['first', 'must-not-run']])
    calls, markers = [], []
    def run(command, **kwargs):
        assert command[:2] == [sys.executable, str(service.parents[1] / 'scripts/run_tests.py')]
        assert kwargs['cwd'] == service.parents[1]
        assert kwargs['timeout_seconds'] in (120, 300)
        calls.append(source.read_bytes())
        if source.read_bytes() == mutant:
            marker = Path(kwargs['env']['PYTHONPYCACHEPREFIX']) / 'still-owned.pyc'
            marker.parent.mkdir(parents=True)
            marker.write_bytes(b'keep')
            markers.append(marker)
            if outcome == 'timeout':
                error = subprocess.TimeoutExpired(command, kwargs['timeout_seconds'])
                error.cleanup_confirmed = True
                raise error
            if outcome == 'returned-uncertain':
                return SimpleNamespace(cleanup_confirmed=False)
            raise CleanupUnconfirmedError('test: descendant exit unknown')
        report = Path(command[command.index('--junitxml') + 1])
        report.write_text('<testsuites><testsuite><testcase name="test_probe"/></testsuite></testsuites>')
        return SimpleNamespace(returncode=0, stdout='tests/probe.py::test_probe\n', stderr='', cleanup_confirmed=True)
    monkeypatch.setattr(gate, 'run_owned', run, raising=False)
    monkeypatch.setattr(gate.subprocess, 'run', run)
    if outcome == 'timeout':
        assert gate.main() == 1
        assert source.read_bytes() == original
        assert calls.count(mutant) == 2
        assert markers and all(not marker.exists() for marker in markers)
        return
    with pytest.raises(CleanupUnconfirmedError):
        gate.main()
    assert source.read_bytes() == mutant, 'restored source while child exit was unknown'
    assert calls.count(mutant) == 1, 'ran another mutant after uncertain cleanup'
    assert markers and markers[0].read_bytes() == b'keep', 'deleted owned bytecode'
    backups = list(service.rglob('*.original'))
    assert backups and any(path.read_bytes() == original for path in backups)
