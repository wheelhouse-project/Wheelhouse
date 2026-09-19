"""The removed-menu-item gate must not leave gui.py mutated and call it a pass.

wh-remove-restart-credentials-items.2.4 (codex round 2): the gate wrote the
mutant OUTSIDE the try whose finally restored the file, and its restore
swallowed OSError and returned. Two failures followed from that pair. A write
that opened gui.py and then failed left the file truncated with no restore
attempted, because the only enclosing handler caught KeyboardInterrupt. A
restore that failed printed a line and let the sweep continue, so the run could
end "0 survivors" with the mutant still in a tracked file -- and every later
mutation judged code nobody meant to run.

These tests drive the two functions that decide it: ``_restore``, which now
reports whether the original bytes are back, and ``_apply_and_run``, which puts
the mutant write and the test run under one restoration finally.
"""

import importlib.util
import subprocess
from pathlib import Path

GATE_PATH = Path(__file__).resolve().parent / "mutation_gate_removed_menu_items.py"

_spec = importlib.util.spec_from_file_location(
    "mutation_gate_removed_menu_items", GATE_PATH
)
assert _spec is not None and _spec.loader is not None, GATE_PATH
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

ORIGINAL = b"the real source\n"


class _FakeTarget:
    """A stand-in for the gate's TARGET path.

    It records every write, and can be told to fail a write or to read
    back something other than what was written -- the two ways a restore
    can leave the wrong bytes on disk.
    """

    def __init__(self, *, fail_writes=False, read_back=None):
        self.fail_writes = fail_writes
        self.read_back = read_back
        self.writes: list[bytes] = []
        self.content = b""

    def write_bytes(self, data: bytes) -> int:
        self.writes.append(data)
        if self.fail_writes:
            raise OSError(13, "permission denied")
        self.content = data
        return len(data)

    def read_bytes(self) -> bytes:
        return self.content if self.read_back is None else self.read_back

    def __str__(self) -> str:
        return "<fake gui.py>"


class TestRestoreReportsWhatHappened:
    def test_a_restore_that_puts_the_original_back_reports_success(self, monkeypatch):
        target = _FakeTarget()
        monkeypatch.setattr(gate, "TARGET", target)
        monkeypatch.setattr(gate, "_clear_pycache", lambda: None)

        assert gate._restore(ORIGINAL) is True
        assert target.content == ORIGINAL

    def test_a_restore_that_cannot_write_reports_failure(self, monkeypatch):
        target = _FakeTarget(fail_writes=True)
        monkeypatch.setattr(gate, "TARGET", target)
        monkeypatch.setattr(gate, "_clear_pycache", lambda: None)

        assert gate._restore(ORIGINAL) is False

    def test_a_restore_whose_bytes_do_not_match_reports_failure(self, monkeypatch):
        target = _FakeTarget(read_back=b"still mutated\n")
        monkeypatch.setattr(gate, "TARGET", target)
        monkeypatch.setattr(gate, "_clear_pycache", lambda: None)

        assert gate._restore(ORIGINAL) is False


class TestTheMutantWriteIsInsideTheRestore:
    def test_a_run_restores_the_source_and_says_so(self, monkeypatch):
        target = _FakeTarget()
        monkeypatch.setattr(gate, "TARGET", target)
        monkeypatch.setattr(gate, "_clear_pycache", lambda: None)
        monkeypatch.setattr(gate, "_run_selection", lambda: (1, "FAILED x::y\n"))

        rc, out, status, restored = gate._apply_and_run("mutant\n", ORIGINAL)

        assert (rc, status, restored) == (1, "ran", True)
        assert "FAILED" in out
        assert target.content == ORIGINAL

    def test_a_failed_mutant_write_still_reaches_the_restore(self, monkeypatch):
        target = _FakeTarget(fail_writes=True)
        monkeypatch.setattr(gate, "TARGET", target)
        monkeypatch.setattr(gate, "_clear_pycache", lambda: None)

        def _must_not_run():
            raise AssertionError("the selection ran after the mutant write failed")

        monkeypatch.setattr(gate, "_run_selection", _must_not_run)

        rc, out, status, restored = gate._apply_and_run("mutant\n", ORIGINAL)

        assert status == "write-error"
        assert rc is None
        assert "permission denied" in out
        # The restore was attempted, twice: the write of the mutant, then
        # the bounded retry of the original.
        assert target.writes == [b"mutant\n", ORIGINAL, ORIGINAL]
        assert restored is False  # the same failure stops the restore too

    def test_a_run_that_never_terminates_still_reaches_the_restore(self, monkeypatch):
        target = _FakeTarget()
        monkeypatch.setattr(gate, "TARGET", target)
        monkeypatch.setattr(gate, "_clear_pycache", lambda: None)

        def _hang():
            raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

        monkeypatch.setattr(gate, "_run_selection", _hang)

        rc, _out, status, restored = gate._apply_and_run("mutant\n", ORIGINAL)

        assert (rc, status, restored) == (None, "timeout", True)
        assert target.content == ORIGINAL
