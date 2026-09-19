"""Check mode validates mutants in memory and never enters a sweep."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(params=["config_save_merge", "process_priority", "read_dictation_gate"])
def gate(request, monkeypatch):
    path = Path(__file__).with_name(f"mutation_gate_{request.param}.py")
    spec = importlib.util.spec_from_file_location(request.param, path)
    module = importlib.util.module_from_spec(spec)
    # Scripts configure their terminal stream when imported.
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", sys.__stdout__)
        spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", [str(path), "--check"])
    return module


def prepare(gate, tmp_path, monkeypatch, source, entries):
    target = tmp_path / "target.py"
    target.write_bytes(source)
    for attr in ("SRC", "SP", "MAIN", "CE", "ACT", "APP"):
        if hasattr(gate, attr):
            monkeypatch.setattr(gate, attr, target)
    mutations = [dict(name=name, old=old, new=new, expect=["test_probe"],
                      file=target, src=target) for name, old, new in entries]
    monkeypatch.setattr(gate, "MUTATIONS", mutations)
    return target


def forbid_sweep(gate, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("--check entered test collection, execution, recovery, or a write")
    monkeypatch.setattr(gate.subprocess, "run", forbidden)
    if hasattr(gate, "run_owned"):
        monkeypatch.setattr(gate, "run_owned", forbidden)
    for name in ("_recover_pending", "_clear_pycache"):
        if hasattr(gate, name):
            monkeypatch.setattr(gate, name, forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_check_is_repeatable_and_read_only(gate, tmp_path, monkeypatch, capsys, newline):
    original = b"value = 1" + newline
    target = prepare(gate, tmp_path, monkeypatch, original,
                     [("valid", "value = 1\n", "value = 2\n")])
    forbid_sweep(gate, monkeypatch)
    for _ in range(2):
        assert gate.main() == 0
        output = capsys.readouterr().out
        assert "checked 1 patterns, 0 stale, 0 ambiguous, 0 that do not compile" in output
        assert target.read_bytes() == original


@pytest.mark.parametrize("entries,summary", [
    ([("missing", "absent", "!")], "1 stale, 0 ambiguous, 0 that do not compile"),
    ([("repeated", "value", "!")], "0 stale, 1 ambiguous, 0 that do not compile"),
    ([("broken", "value = 1", "if :")], "0 stale, 0 ambiguous, 1 that do not compile"),
    ([("missing", "absent", "!"), ("repeated", "value", "!"),
      ("broken", "value = 1", "if :"), ("valid", "value = 2", "value = 3")],
     "1 stale, 1 ambiguous, 1 that do not compile"),
], ids=["stale", "ambiguous", "noncompiling", "mixed"])
def test_check_reports_independent_failure_counts(gate, tmp_path, monkeypatch, capsys,
                                                 entries, summary):
    original = b"value = 1\nvalue = 2\n"
    target = prepare(gate, tmp_path, monkeypatch, original, entries)
    forbid_sweep(gate, monkeypatch)
    assert gate.main() == 1
    output = capsys.readouterr().out
    assert f"checked {len(entries)} patterns, {summary}" in output
    for name, _, _ in entries:
        if name != "valid":
            assert name in output
    assert target.read_bytes() == original


def test_without_check_still_collects_runs_mutant_and_restores(
    gate, tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    service = repo / "services" / "wheelhouse"
    service.mkdir(parents=True)
    original = b"value = 1\n"
    target = prepare(gate, service, monkeypatch, original,
                     [("valid", "value = 1", "value = 2")])
    monkeypatch.setattr(sys, "argv", [gate.__file__])
    monkeypatch.setattr(gate, "SERVICE", service)
    seen = []

    def run(args, **kwargs):
        if gate.__name__ == "config_save_merge":
            assert kwargs["cwd"] == repo
            assert args[:2] == [sys.executable, str(repo / "scripts/run_tests.py")]
        else:
            assert kwargs["cwd"] == service
        seen.append(target.read_bytes())
        if "--collect-only" in args:
            assert len(seen) == 1
            return SimpleNamespace(cleanup_confirmed=True, returncode=0, stdout="tests/probe.py::test_probe\n", stderr="")
        if target.read_bytes() == original:
            return SimpleNamespace(cleanup_confirmed=True, returncode=0, stdout="1 passed\n", stderr="")
        assert target.read_bytes() == b"value = 2\n"
        return SimpleNamespace(cleanup_confirmed=True, returncode=1,
                               stdout="FAILED tests/probe.py::test_probe - AssertionError\n",
                               stderr="")

    monkeypatch.setattr(gate.subprocess, "run", run)
    if hasattr(gate, "run_owned"):
        monkeypatch.setattr(gate, "run_owned", run)
    assert gate.main() == 0
    assert seen == [original, original, b"value = 2\n"]
    assert target.read_bytes() == original
    assert "caught" in capsys.readouterr().out
