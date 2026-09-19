"""Scroll gate verdicts against real pytest failure and error output."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def gate(monkeypatch):
    path = Path(__file__).with_name("mutation_gate_scroll_zero_count.py")
    spec = importlib.util.spec_from_file_location("scroll_error_gate", path)
    module = importlib.util.module_from_spec(spec)
    # The CLI configures stdout at import; pytest's capture stream need not.
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", SimpleNamespace(reconfigure=lambda **kw: None))
        spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pytest_results(tmp_path_factory):
    work = tmp_path_factory.mktemp("scroll_gate_output")
    config = work / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    runner = Path(__file__).resolve().parents[3] / "scripts" / "run_tests.py"
    sources = {
        "failure": (
            "import logging\n"
            "def test_catcher():\n"
            "    logging.getLogger('speech.actions').error('scroll failed')\n"
            "    assert 1 == 0\n"
        ),
        "setup": (
            "import pytest\n"
            "@pytest.fixture\n"
            "def broken():\n"
            "    raise RuntimeError('setup failed')\n"
            "def test_catcher(broken):\n"
            "    pass\n"
        ),
        "collection": "raise ImportError('collection failed')\n",
    }
    results = {}
    for kind, source in sources.items():
        test = work / f"test_{kind}.py"
        test.write_text(source, encoding="utf-8")
        results[kind] = subprocess.run(
            [sys.executable, str(runner), str(test), "-c", str(config),
             "--confcutdir", str(work), "-p", "no:randomly", "-rfE",
             "--tb=short", "--color=no", "--junitxml", str(work / f"{kind}.xml")],
            cwd=runner.parent.parent, capture_output=True, text=True, timeout=60,
        )
    return results


@pytest.mark.parametrize("kind,returncode", [("failure", 1), ("setup", 1), ("collection", 2)])
def test_real_pytest_output_classification(gate, pytest_results, kind, returncode):
    result = pytest_results[kind]
    assert result.returncode == returncode, result.stdout + result.stderr
    if kind == "failure":
        assert "ERROR    speech.actions:" in result.stdout
        assert gate.failed_names(result.stdout) == {"test_catcher"}
        assert gate.error_lines(result.stdout) == []
    else:
        assert gate.error_lines(result.stdout)


@pytest.mark.parametrize("kind,expected", [
    ("failure", "caught 1, survived 0, errors 0"),
    ("setup", "caught 0, survived 0, errors 1"),
    ("collection", "caught 0, survived 0, errors 1"),
])
def test_main_verdict_from_real_output(gate, pytest_results, tmp_path, monkeypatch, capsys, kind, expected):
    source = tmp_path / "actions.py"
    original = b"count = 0\n"
    source.write_bytes(original)
    monkeypatch.setattr(gate, "SRC", source)
    monkeypatch.setattr(gate, "MUTATIONS", [{
        "name": "count-change", "old": "count = 0", "new": "count = 1",
        "expect": ["test_catcher"],
    }])
    monkeypatch.setattr(gate, "collect_names", lambda: {"test_catcher"})
    results = iter([subprocess.CompletedProcess([], 0, "", ""), pytest_results[kind]])
    monkeypatch.setattr(gate, "_pytest", lambda *args: next(results))
    assert gate.main() == (0 if kind == "failure" else 1)
    assert expected in capsys.readouterr().out
    assert source.read_bytes() == original


@pytest.mark.parametrize("line", [
    "ERROR tests/test_x.py::test_y - RuntimeError: setup failed",
    "ERROR tests/test_x.py::TestX::test_y[param]",
    "ERROR tests/test_x.py - ImportError: collection failed",
    "ERROR tests/test_x.py",
    r"ERROR C:\tests\test_x.py::test_y - RuntimeError",
])
def test_summary_forms_remain_errors(gate, line):
    assert gate.error_lines(line) == [line]
