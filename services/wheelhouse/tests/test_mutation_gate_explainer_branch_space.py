"""Explainer gate verdicts must distinguish real assertions from crashed tests."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def gate(monkeypatch):
    path = Path(__file__).with_name("mutation_gate_explainer_branch_space.py")
    spec = importlib.util.spec_from_file_location("explainer_gate", path)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", SimpleNamespace(reconfigure=lambda **kw: None))
        spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module", params=[
    "assertion", "message", "crash", "custom_crash", "mixed", "setup",
    "teardown", "collection",
])
def pytest_result(tmp_path_factory, request):
    """Exercise the installed pytest renderer, including bare FAILED summaries."""
    work = tmp_path_factory.mktemp("explainer_gate_output")
    config = work / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    runner = Path(__file__).resolve().parents[3] / "scripts" / "run_tests.py"
    sources = {
        "assertion": (
            "import logging\n"
            "def test_catcher():\n"
            "    logging.getLogger('speech.pattern_explainer').error('failed')\n"
            "    assert 1 == 0\n"
        ),
        "message": "def test_catcher():\n    assert False, 'wrong phrase'\n",
        "crash": "def test_catcher():\n    raise TypeError('bad variant')\n",
        "custom_crash": (
            "class BrokenVariant(Exception): pass\n"
            "def test_catcher():\n    raise BrokenVariant('bad variant')\n"
        ),
        "mixed": (
            "def test_catcher():\n    assert False\n"
            "def test_other():\n    raise TypeError('bad variant')\n"
        ),
        "setup": (
            "import pytest\n@pytest.fixture\ndef broken():\n"
            "    raise RuntimeError('setup failed')\n"
            "def test_catcher(broken):\n    pass\n"
        ),
        "teardown": (
            "import pytest\n@pytest.fixture\ndef broken():\n"
            "    yield\n    raise RuntimeError('teardown failed')\n"
            "def test_catcher(broken):\n    assert False\n"
        ),
        "collection": "raise ImportError('collection failed')\n",
    }
    kind = request.param
    test = work / f"test_{kind}.py"
    test.write_text(sources[kind], encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(runner), str(test), "-c", str(config),
         "--confcutdir", str(work), "-p", "no:randomly", "-rfE",
         "--tb=line", "--color=no", "--junitxml", str(work / f"{kind}.xml")],
        cwd=runner.parent.parent, capture_output=True, text=True, timeout=20,
    )
    return kind, result


def run_mutation(gate, monkeypatch, tmp_path, result, expect):
    source = tmp_path / "pattern_explainer.py"
    original = b"variant = 0\r\n"
    source.write_bytes(original)
    monkeypatch.setattr(gate, "SRC", source)
    monkeypatch.setattr(gate, "MUTATIONS", [{
        "name": "variant-change", "old": "variant = 0", "new": "variant = 1",
        "expect": expect,
    }])
    monkeypatch.setattr(gate, "collect_names", lambda: {"test_catcher"})
    results = iter([subprocess.CompletedProcess([], 0, "", ""), result])
    monkeypatch.setattr(gate, "_pytest", lambda *args: next(results))
    verdict = gate.main()
    assert source.read_bytes() == original
    return verdict


@pytest.mark.parametrize("expect", [["test_catcher"], []])
def test_main_verdict_from_real_output(
    gate, pytest_result, tmp_path, monkeypatch, capsys, expect,
):
    kind, result = pytest_result
    assert result.returncode == (2 if kind == "collection" else 1), result.stdout + result.stderr
    caught = kind in ("assertion", "message")
    assert run_mutation(gate, monkeypatch, tmp_path, result, expect) == (0 if caught else 1)
    output = capsys.readouterr().out
    assert ("caught 1, survived 0, errors 0" if caught
            else "caught 0, survived 0, errors 1") in output


@pytest.mark.parametrize("returncode", [2, 3, 4, 5, -9, 139])
def test_unexpected_exit_is_error_even_with_assertion_output(
    gate, monkeypatch, tmp_path, capsys, returncode,
):
    result = subprocess.CompletedProcess([], returncode,
        "test_x.py:10: AssertionError: wrong phrase\nFAILED test_x.py::test_catcher\n", "")
    assert run_mutation(gate, monkeypatch, tmp_path, result, ["test_catcher"]) == 1
    assert "caught 0, survived 0, errors 1" in capsys.readouterr().out


@pytest.mark.parametrize("output", [
    "FAILED test_x.py::test_catcher\n",
    "test_x.py:10: assert False\n",  # no named failure evidence
    "",  # aborted run without a summary
])
def test_incomplete_failure_evidence_is_error(gate, monkeypatch, tmp_path, capsys, output):
    result = subprocess.CompletedProcess([], 1, output, "")
    assert run_mutation(gate, monkeypatch, tmp_path, result, []) == 1
    assert "caught 0, survived 0, errors 1" in capsys.readouterr().out


def test_crash_in_unexpected_test_is_error(gate, monkeypatch, tmp_path, capsys):
    result = subprocess.CompletedProcess([], 1,
        "test_x.py:10: TypeError: bad variant\nFAILED test_x.py::test_other\n", "")
    assert run_mutation(gate, monkeypatch, tmp_path, result, ["test_catcher"]) == 1
    assert "caught 0, survived 0, errors 1" in capsys.readouterr().out


@pytest.mark.parametrize("returncode,output", [
    (0, "1 passed\n"),
    (1, "test_x.py:10: assert False\nFAILED test_x.py::test_other\n"),
])
def test_surviving_mutation_remains_a_survivor(gate, monkeypatch, tmp_path, capsys, returncode, output):
    result = subprocess.CompletedProcess([], returncode, output, "")
    assert run_mutation(gate, monkeypatch, tmp_path, result, ["test_catcher"]) == 1
    assert "caught 0, survived 1, errors 0" in capsys.readouterr().out


@pytest.mark.parametrize("returncode,extra", [
    (2, ""),
    (0, "ERROR tests/test_x.py - ImportError: collection failed\n"),
])
def test_failed_collection_stops_before_baseline(gate, monkeypatch, tmp_path, capsys, returncode, extra):
    source = tmp_path / "pattern_explainer.py"
    source.write_bytes(b"variant = 0\n")
    monkeypatch.setattr(gate, "SRC", source)
    calls = []

    def collect(*args):
        calls.append(args)
        # A failed collection may still have printed every expected name.
        return subprocess.CompletedProcess([], returncode,
            "tests/test_x.py::test_every_shown_phrase_matches_the_pattern_it_belongs_to\n" + extra, "")

    monkeypatch.setattr(gate, "_pytest", collect)
    assert gate.main() == 1
    assert len(calls) == 1
    assert "ERROR collection" in capsys.readouterr().out
