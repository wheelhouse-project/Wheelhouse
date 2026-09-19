"""The nested-group mutation gate must never trade safety for a verdict.

wh-grid-click-nested-group.2.2 and .2.3. Both failures let the gate report a
result it has not earned, and both are invisible in the output:

* ``_build_mutant`` compiled only Python mutants. A toml mutation whose
  replacement text was not valid TOML came back with no error, so ``--check``
  called it valid and a full sweep wrote it to disk. The catalog then fails to
  load, the named catcher fails for that reason, and the mutation is counted
  as CAUGHT. That is the false catch the mutation-gate rules exist to prevent:
  a malformed mutant is an error, never a verdict.
* The mutant write sat outside the block whose ``finally`` restores the file,
  and ``_restore`` swallowed an ``OSError`` and returned normally. So an
  interrupted write, or a restore refused by a lock, could leave a mutated
  TRACKED source file on disk while the run went on to print a verdict. Every
  later run in that checkout, and every other session sharing it, then reads
  the mutant as real code.

These tests call the gate's own functions with prepared inputs. They rewrite
no tracked file: the restore tests use a temporary file, and the toml test
only builds mutant bytes in memory without writing them.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

_GATE_PATH = Path(__file__).resolve().parent / (
    "mutation_gate_pattern_catalog_nested_group.py"
)

# The shipped grid-click expression, which the gate's own toml mutation
# already targets, so it is known to appear exactly once in patterns.toml.
_TOML_ANCHOR = "pattern = '''^((click|tap)[.!?]?)$'''"


def _load_gate():
    """Import the gate script by path.

    The file deliberately has no ``test_`` prefix, so pytest never collects
    it and a plain import statement cannot reach it either.
    """
    spec = importlib.util.spec_from_file_location(
        "_nested_group_gate_runner_safety", _GATE_PATH
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


class TestTomlMutantValidation:
    def test_an_unparseable_toml_mutant_is_an_error(self, gate):
        """A replacement that breaks the TOML must not build."""
        mutant, _original, error = gate._build_mutant({
            "name": "probe-unparseable-toml",
            "file": gate.TOML,
            "old": _TOML_ANCHOR,
            # One quote short of a closing delimiter.
            "new": "pattern = '''^((click|tap)[.!?]?)$''",
        })
        assert mutant is None, (
            "the gate built a toml mutant that cannot be parsed; writing it "
            "makes the catalog fail to load, and the catcher then fails for "
            "that reason and is scored as a catch"
        )
        assert error is not None and "does not parse" in error, error

    def test_a_valid_toml_mutant_still_builds(self, gate):
        """The validation must not refuse the mutation the gate ships."""
        mutant, _original, error = gate._build_mutant({
            "name": "probe-valid-toml",
            "file": gate.TOML,
            "old": _TOML_ANCHOR,
            "new": "pattern = '''^((?:click|tap)[.!?]?)$'''",
        })
        assert error is None, error
        assert mutant is not None

    def test_the_toml_anchor_is_unique(self, gate):
        """Guard the two tests above against a later rewrite of the line."""
        text = gate.TOML.read_bytes().decode("utf-8")
        assert text.count(_TOML_ANCHOR) == 1, (
            "the anchor these tests mutate no longer appears exactly once in "
            "patterns.toml, so they are testing something else"
        )


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
        result, error = gate._apply_and_run(target, b"mutant\n", b"original\n")
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
        result, error = gate._apply_and_run(target, b"mutant\n", b"original\n")
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
        result, error = gate._apply_and_run(target, b"mutant\n", b"original\n")
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
            return "a real pytest result"

        monkeypatch.setattr(gate, "_pytest", record)
        result, error = gate._apply_and_run(target, b"mutant\n", b"original\n")
        assert error is None
        assert result == "a real pytest result"
        assert seen["mutant_on_disk"] == b"mutant\n", (
            "the suite ran without the mutant in place"
        )
        assert target.read_bytes() == b"original\n"
