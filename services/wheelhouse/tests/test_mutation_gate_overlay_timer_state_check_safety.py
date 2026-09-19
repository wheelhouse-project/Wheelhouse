"""The overlay timer gate must never print a verdict over an unrestored main.py.

wh-overlay-timer-cancel-race.2.1. The gate rewrites the live Logic source,
``services/wheelhouse/main.py``, in place for every mutation. Its ``_restore``
caught an ``OSError`` from the restoring write, printed a line, and returned
normally; the caller sat in a ``finally`` and read no result from it. So a
restore refused by a file holder -- an editor, a scanner, another process
with the file open -- on the LAST mutation let the sweep print
``caught 6, survived 0, errors 0`` and exit 0 with the mutant still in a
tracked file. Every later test run, and every other session sharing the
checkout, then reads that mutant as real code.

These tests call the gate's own functions with prepared inputs. They rewrite
no tracked file: every restore and write test points the gate at a file under
``tmp_path`` and stubs the pytest subprocess.
"""

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_GATE_PATH = Path(__file__).resolve().parent / (
    "mutation_gate_overlay_timer_state_check.py"
)


def _load_gate():
    """Import the gate script by path.

    The file deliberately has no ``test_`` prefix, so pytest never collects
    it and a plain import statement cannot reach it either.
    """
    spec = importlib.util.spec_from_file_location(
        "_overlay_timer_gate_runner_safety", _GATE_PATH
    )
    assert spec is not None and spec.loader is not None, (
        f"could not build an import spec for {_GATE_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


_ORIGINAL = b"alpha = 1\nomega = 2\n"


class TestTheSweepStopsWhenTheSourceIsNotBack:
    """The whole finding, seen from ``main()``.

    Two mutations, a stubbed green baseline, a stubbed pytest that reports
    the expected catcher as failed, and a restore of the ORIGINAL bytes that
    is refused. The run must fail and must not go on to the second mutation.
    """

    def test_a_failed_restore_stops_the_sweep_and_fails_the_run(
        self, gate, tmp_path, monkeypatch, capsys,
    ):
        src = tmp_path / "main.py"
        src.write_bytes(_ORIGINAL)
        monkeypatch.setattr(gate, "SRC", src)
        mutations = [
            {
                "name": "first",
                "old": "alpha = 1",
                "new": "alpha = 0",
                "expect": ["test_one"],
                "selection": ["tests/x.py"],
            },
            {
                "name": "second",
                "old": "omega = 2",
                "new": "omega = 0",
                "expect": ["test_one"],
                "selection": ["tests/x.py"],
            },
        ]
        monkeypatch.setattr(gate, "_selected", lambda argv: mutations)
        monkeypatch.setattr(gate, "collect_names", lambda: {"test_one"})
        monkeypatch.setattr(gate, "_clear_bytecode", lambda: None)

        runs = []

        def fake_pytest(*args):
            runs.append(args)
            if len(runs) == 1:
                return SimpleNamespace(returncode=0, stdout="")
            return SimpleNamespace(
                returncode=1,
                stdout="FAILED tests/x.py::test_one - AssertionError\n",
            )

        monkeypatch.setattr(gate, "_pytest", fake_pytest)

        real_write = Path.write_bytes

        def refuse_the_original(self, data):
            if self == src and data == _ORIGINAL:
                raise OSError("the file is locked by another process")
            return real_write(self, data)

        monkeypatch.setattr(Path, "write_bytes", refuse_the_original)

        rc = gate.main()

        assert rc != 0, (
            "the sweep exited 0 after a restore it could not perform, so a "
            "clean-looking run left the mutant in main.py"
        )
        assert len(runs) == 2, (
            "the sweep ran another mutation on top of a source file that "
            "still held the previous mutant"
        )
        out = capsys.readouterr().out
        assert "not restored" in out, out


class TestRestoreReportsWhetherItWorked:
    def test_a_successful_restore_reports_success(self, gate, tmp_path):
        target = tmp_path / "target.py"
        target.write_bytes(b"mutant\n")
        assert gate._restore(target, b"original\n") is True
        assert target.read_bytes() == b"original\n"

    def test_a_refused_restore_reports_failure(
        self, gate, tmp_path, monkeypatch,
    ):
        target = tmp_path / "target.py"
        target.write_bytes(b"mutant\n")

        def refuse(self, data):
            raise OSError("the file is locked by another process")

        monkeypatch.setattr(Path, "write_bytes", refuse)
        assert gate._restore(target, b"original\n") is False, (
            "a restore that could not write reported nothing to its caller, "
            "so the sweep went on to score a mutation whose mutant was still "
            "on disk"
        )


class TestTheMutantWriteIsInsideTheGuard:
    def _target(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_bytes(b"original\n")
        return target

    def test_a_write_failure_yields_an_error_and_no_result(
        self, gate, tmp_path, monkeypatch,
    ):
        target = self._target(tmp_path)
        real_write = Path.write_bytes

        def refuse_the_mutant(self, data):
            if data == b"mutant\n":
                raise OSError("no space left on device")
            return real_write(self, data)

        monkeypatch.setattr(Path, "write_bytes", refuse_the_mutant)
        monkeypatch.setattr(
            gate, "_pytest", lambda *a: pytest.fail("the suite must not run"),
        )
        result, error = gate._apply_and_run(
            target, b"mutant\n", b"original\n", ["tests/x.py"],
        )
        assert result is None
        assert error is not None and "could not write" in error, error
        assert target.read_bytes() == b"original\n", (
            "a failed mutant write left the file changed"
        )

    def test_a_failed_restore_suppresses_the_verdict(
        self, gate, tmp_path, monkeypatch,
    ):
        target = self._target(tmp_path)
        monkeypatch.setattr(gate, "_pytest", lambda *a: "a real pytest result")
        monkeypatch.setattr(gate, "_restore", lambda path, original: False)
        result, error = gate._apply_and_run(
            target, b"mutant\n", b"original\n", ["tests/x.py"],
        )
        assert error is not None and "not restored" in error, error
        assert result is None, (
            "the run's result survived a failed restore, so the caller could "
            "still read a verdict out of a run whose source file is mutated"
        )

    def test_a_timeout_is_an_error_and_the_file_comes_back(
        self, gate, tmp_path, monkeypatch,
    ):
        target = self._target(tmp_path)

        def time_out(*args):
            raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

        monkeypatch.setattr(gate, "_pytest", time_out)
        result, error = gate._apply_and_run(
            target, b"mutant\n", b"original\n", ["tests/x.py"],
        )
        assert result is None
        assert error is not None and "timed out" in error, error
        assert target.read_bytes() == b"original\n"

    def test_a_clean_run_returns_its_result_and_restores(
        self, gate, tmp_path, monkeypatch,
    ):
        target = self._target(tmp_path)
        seen = {}

        def record(*args):
            seen["mutant_on_disk"] = target.read_bytes()
            seen["selection"] = args
            return "a real pytest result"

        monkeypatch.setattr(gate, "_pytest", record)
        result, error = gate._apply_and_run(
            target, b"mutant\n", b"original\n", ["tests/x.py"],
        )
        assert error is None
        assert result == "a real pytest result"
        assert seen["mutant_on_disk"] == b"mutant\n", (
            "the suite ran without the mutant in place"
        )
        assert seen["selection"][0] == "tests/x.py", (
            "the mutation's own test selection was not the one run"
        )
        assert target.read_bytes() == b"original\n"
