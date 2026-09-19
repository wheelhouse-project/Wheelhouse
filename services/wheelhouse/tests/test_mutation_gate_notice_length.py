"""The notice-length mutation gate must not judge cached bytecode.

wh-notice-length-guard.2.2. Codex round 5 filed it against a comment this
branch had just written, and the comment was wrong in a way that matters:
it claimed the gate's fresh ``write_bytes`` mtime invalidates any .pyc
left from before the run. CPython compares the source mtime TRUNCATED TO
AN INTEGER SECOND, plus the source byte count, against the .pyc header.
Six of this gate's mutations keep the source the same byte length, so a
.pyc written for the clean source stays valid for a mutant written in the
same second, and the pytest child then executes CLEAN code while a mutant
sits on disk.

``PYTHONDONTWRITEBYTECODE=1`` does not close it. That variable stops
Python WRITING a .pyc; it does not stop Python reading one already there.

The failure direction is loud rather than silent: the clean code passes
every test, so the gate reports that mutation as a SURVIVOR. This cache
state cannot turn a real survivor into a reported catch. The cost is a
wasted investigation into a mutation that was never executed.

These tests drive ``_clear_pycache`` DIRECTLY against temporary paths.
They never delete a real __pycache__ and never write to
utils/notice_text.py or any other tracked source. Two of them start an
interpreter -- the two hazard tests, each against a module it created
under tmp_path.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GATE_PATH = Path(__file__).resolve().parent / "mutation_gate_notice_length.py"

_spec = importlib.util.spec_from_file_location(
    "mutation_gate_notice_length", GATE_PATH
)
assert _spec is not None and _spec.loader is not None, GATE_PATH
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


class _Shutil:
    """The one function the cache sweep calls, with a refusal counter.

    Swallowing a removal is exactly what ``ignore_errors=True`` does with
    a directory Windows will not delete. Installed as the gate module's
    ``shutil`` attribute rather than over ``shutil.rmtree`` itself, so the
    substitution reaches the gate's own lookups and nothing else.
    """

    def __init__(self, refuse=0):
        self.refuse = refuse
        self.calls = []

    def rmtree(self, path, ignore_errors=False):
        self.calls.append(Path(path))
        if self.refuse > 0:
            self.refuse -= 1
            return
        shutil.rmtree(path, ignore_errors=ignore_errors)


_CLEAN = "MAX_MESSAGE_WIDE_CHARACTERS = 255\nMARKER = 'unchanged'\n"
# 255 -> 256 is one character for one character, which is what makes the
# hazard reachable: the source byte count the .pyc header records is
# still correct after the mutation.
_MUTANT = "MAX_MESSAGE_WIDE_CHARACTERS = 256\nMARKER = 'unchanged'\n"


def _read_limit_in_a_fresh_interpreter(work):
    """Import the sample module in a child that may not WRITE bytecode.

    This is the gate's own condition: ``_run_pytest`` sets exactly this
    variable on the child that runs the mutant.
    """
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run(
        [sys.executable, "-c",
         "import sample_module as m; print(m.MAX_MESSAGE_WIDE_CHARACTERS)"],
        cwd=work, env=env, capture_output=True, text=True, check=True,
    )
    return int(done.stdout.strip())


class TestTheStalePycHazardItself:
    """wh-notice-length-guard.2.2: the defect the cache clear exists for."""

    def test_a_same_size_mutant_in_the_cached_second_runs_the_clean_code(
        self, tmp_path
    ):
        """Without the clear, the child imports the source the gate replaced.

        The mutant's mtime is set to the clean source's mtime with
        ``os.utime`` rather than raced against the clock, so the test is
        deterministic. Forcing it recreates exactly the state two writes
        in one second produce on their own: that state was reproduced
        without forcing anything, on this machine, and the measurements
        are recorded on wh-notice-length-guard.2.2.
        """
        module = tmp_path / "sample_module.py"
        module.write_bytes(_CLEAN.encode())
        writing = dict(os.environ)
        writing.pop("PYTHONDONTWRITEBYTECODE", None)
        subprocess.run(
            [sys.executable, "-c", "import sample_module"],
            cwd=tmp_path, env=writing, check=True,
        )
        clean_mtime = module.stat().st_mtime
        assert list((tmp_path / "__pycache__").glob("sample_module.*.pyc"))

        module.write_bytes(_MUTANT.encode())
        os.utime(module, (clean_mtime, clean_mtime))
        assert len(_MUTANT.encode()) == len(_CLEAN.encode())

        assert _read_limit_in_a_fresh_interpreter(tmp_path) == 255

    def test_the_clear_makes_the_same_child_read_the_mutant(self, tmp_path):
        """With the clear, the child compiles the source that is on disk.

        This is the half that fails without ``_clear_pycache``, and it is
        the whole reason the function exists.
        """
        module = tmp_path / "sample_module.py"
        module.write_bytes(_CLEAN.encode())
        writing = dict(os.environ)
        writing.pop("PYTHONDONTWRITEBYTECODE", None)
        subprocess.run(
            [sys.executable, "-c", "import sample_module"],
            cwd=tmp_path, env=writing, check=True,
        )
        clean_mtime = module.stat().st_mtime

        module.write_bytes(_MUTANT.encode())
        os.utime(module, (clean_mtime, clean_mtime))

        error, interrupt = gate._clear_pycache(root=tmp_path)

        assert error is None
        assert interrupt is None
        assert _read_limit_in_a_fresh_interpreter(tmp_path) == 256


class TestTheGateRefusesAFailedCacheClear:
    """A cache the gate could not clear stops the run rather than warning."""

    def test_a_directory_that_survives_both_passes_is_reported(
        self, tmp_path, monkeypatch
    ):
        """``ignore_errors=True`` is silent, so the caller has to look."""
        cache = tmp_path / "pkg" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "mod.cpython-313.pyc").write_bytes(b"stale bytecode")
        fake = _Shutil(refuse=99)
        monkeypatch.setattr(gate, "shutil", fake)

        error, interrupt = gate._clear_pycache(root=tmp_path)

        assert interrupt is None
        assert error is not None
        assert str(cache) in error
        assert len(fake.calls) == 2
        assert cache.exists()

    def test_a_directory_removed_on_the_second_pass_is_not_an_error(
        self, tmp_path, monkeypatch
    ):
        """The retry is the point of the two passes; only the last counts.

        A scan holding the directory for a moment is the ordinary Windows
        case, and it is gone by the second attempt.
        """
        cache = tmp_path / "pkg" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "mod.cpython-313.pyc").write_bytes(b"stale bytecode")
        fake = _Shutil(refuse=1)
        monkeypatch.setattr(gate, "shutil", fake)

        error, interrupt = gate._clear_pycache(root=tmp_path)

        assert error is None
        assert interrupt is None
        assert not cache.exists()
        assert len(fake.calls) == 2

    def test_a_completed_pass_is_reported_when_the_retry_is_cut_short(
        self, tmp_path, monkeypatch
    ):
        """The message must describe the passes that actually ran.

        Pass 1 finishes and leaves one directory behind. Pass 2 is
        interrupted before it can retry that directory. Saying every
        attempt was interrupted would be false, and dropping pass 1's
        result would lose the only directory the gate managed to find.
        """

        class _RefuseThenInterrupt:
            """Refuse every removal, then interrupt the second pass."""

            def __init__(self):
                self.calls = []

            def rmtree(self, path, ignore_errors=False):
                self.calls.append(Path(path))
                if len(self.calls) > 1:
                    raise KeyboardInterrupt()

        cache = tmp_path / "pkg" / "__pycache__"
        cache.mkdir(parents=True)
        fake = _RefuseThenInterrupt()
        monkeypatch.setattr(gate, "shutil", fake)

        error, interrupt = gate._clear_pycache(root=tmp_path)

        assert isinstance(interrupt, KeyboardInterrupt)
        assert error is not None
        assert "every attempt" not in error
        assert "could not remove 1 __pycache__ directory" in error
        assert str(cache) in error

    def test_the_sweep_covers_the_whole_service_but_never_the_venv(
        self, tmp_path, monkeypatch
    ):
        """The four targets sit in three directories, so three caches.

        The gate's targets are the service root, utils/ and ui/, so
        narrowing the sweep would mean naming three directories and
        keeping that list right as the targets move. The service sweep
        also reaches the caches the collection subprocess leaves, which
        the gate never enumerates.
        The .venv is excluded from the DELETION only -- rglob still
        walks it -- because it holds no source this gate rewrites.
        An earlier version of this docstring said a mutant module is
        imported through modules carrying cached bytecode of their own.
        Codex round 6 caught that: an importer's .pyc re-executes its
        import, which re-checks the imported module against its own
        .pyc, so a stale importer cache cannot hide a mutant.
        """
        deep = tmp_path / "ui" / "helpers" / "__pycache__"
        deep.mkdir(parents=True)
        inside_venv = tmp_path / ".venv" / "Lib" / "__pycache__"
        inside_venv.mkdir(parents=True)
        fake = _Shutil()
        monkeypatch.setattr(gate, "shutil", fake)

        error, interrupt = gate._clear_pycache(root=tmp_path)

        assert error is None
        assert interrupt is None
        assert fake.calls == [deep]
        assert inside_venv.exists()

    def test_a_failed_clear_is_an_exit_code_not_a_warning(
        self, monkeypatch, capsys
    ):
        """The baseline may not start on bytecode the gate cannot vouch for."""
        monkeypatch.setattr(
            gate, "_clear_pycache",
            lambda root=None: ("a locked __pycache__ survived", None),
        )

        assert gate._clear_pycache_or_abort() == 1
        assert "ERROR a locked __pycache__ survived" in capsys.readouterr().out

    def test_a_clean_clear_lets_the_run_start(self, monkeypatch):
        """None is what main reads as permission to run the baseline."""
        monkeypatch.setattr(gate, "_clear_pycache", lambda root=None: (None, None))

        assert gate._clear_pycache_or_abort() is None

    def test_a_held_interrupt_beats_the_abort(self, monkeypatch, capsys):
        """A Ctrl+C ends the run as an interrupt, not as a quiet exit code.

        The operator who pressed it has to see it; an exit code reads like
        an ordinary refusal, and the cache error is printed either way.
        """
        monkeypatch.setattr(
            gate, "_clear_pycache",
            lambda root=None: (
                "a locked __pycache__ survived", KeyboardInterrupt()
            ),
        )

        with pytest.raises(KeyboardInterrupt):
            gate._clear_pycache_or_abort()

        assert "ERROR a locked __pycache__ survived" in capsys.readouterr().out

    def test_an_interrupt_during_every_pass_says_a_mutant_may_remain(
        self, tmp_path, monkeypatch
    ):
        """A sweep that never completed cannot report a clean cache."""

        class _Interrupting:
            def rmtree(self, path, ignore_errors=False):
                raise KeyboardInterrupt()

        (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
        monkeypatch.setattr(gate, "shutil", _Interrupting())

        error, interrupt = gate._clear_pycache(root=tmp_path)

        assert isinstance(interrupt, KeyboardInterrupt)
        assert error is not None
        assert "MUTANT .pyc MAY REMAIN" in error
