"""The keyboard-refusal mutation gate must not damage what it rewrites.

wh-keyboard-refusal-notice.1.3 and .1.4. Two failures in the gate itself,
both reachable from an ordinary full sweep of the fifty mutations:

1. The loop that wrote a mutant into a tracked source file sat OUTSIDE the
   ``try`` whose ``finally`` restores it, and registered the target only
   AFTER ``write_bytes`` returned. A Ctrl+C landing in that window left the
   mutant on disk and the target out of the registry, so nothing put it
   back; the sweep's own final check only printed a warning. Both targets
   are shipped files -- ui/ui_action_handler.py and
   utils/win_input_sender.py -- in a checkout several sessions commit to.
2. ``_clear_pycache`` removed with ``ignore_errors=True`` and never looked
   to see whether the directory had gone, and the pre-baseline call only
   printed its error before running the whole sweep anyway. A stale .pyc
   beside a restored source is executed by the next process, so the gate
   would be judging code it did not write.

These tests drive the extracted steps DIRECTLY against temporary paths.
They never write to ui/ui_action_handler.py or utils/win_input_sender.py,
never delete a real __pycache__, and never start a pytest process: the
three that read ``main`` replace every step that would touch a real file,
including the pattern application, so they hold whichever mutation of a
tracked source is on disk while they run.
"""

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

GATE_PATH = (
    Path(__file__).resolve().parent
    / "mutation_gate_keyboard_refusal_notice.py"
)

_spec = importlib.util.spec_from_file_location(
    "mutation_gate_keyboard_refusal_notice", GATE_PATH
)
assert _spec is not None and _spec.loader is not None, GATE_PATH
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


_ORIGINAL = {"a": b"the first source\n", "b": b"the second source\n"}
_MUTANT = {"a": "the first MUTANT\n", "b": "the second MUTANT\n"}


def _failing_write(monkeypatch, victim, when, failure):
    """Break ``_atomic_write`` once, at a named point, for one target.

    ``when`` names the point in the replacement window the failure lands.
    "before" raises without touching the disk. "temp" writes the neighbour
    file and raises before the replace, which is the OSError or Ctrl+C
    that lands mid-write. "after" completes the replace and raises, which
    is the interrupt arriving between the write returning and the caller's
    next statement.

    The failure is one shot, so the restore that follows writes normally,
    as it would after a single Ctrl+C. Targets are plain Paths: the seam
    is this function, not a method on the target, because the gate no
    longer writes a target directly.
    """
    real = gate._atomic_write
    state = {"armed": True}

    def wrapper(target, raw):
        if Path(target) != Path(victim) or not state["armed"]:
            return real(target, raw)
        state["armed"] = False
        if when == "before":
            raise failure()
        if when == "temp":
            Path(target).with_name(Path(target).name + ".gate-tmp") \
                .write_bytes(raw)
            raise failure()
        real(target, raw)
        raise failure()

    monkeypatch.setattr(gate, "_atomic_write", wrapper)


class _Shutil:
    """The one function the cache sweep calls, with a refusal counter.

    Swallowing a removal is exactly what ``ignore_errors=True`` does with a
    directory Windows will not delete. Installed as the gate module's
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


def _two_targets(tmp_path):
    """Two targets holding their originals, in the sweep's own order."""
    targets = []
    for key in ("a", "b"):
        path = tmp_path / f"{key}.py"
        path.write_bytes(_ORIGINAL[key])
        targets.append(path)
    return targets


def _write_then_restore(changed, originals, name="a-mutation"):
    """The sweep's own sequence: write inside a try, restore in its finally.

    A KeyboardInterrupt is expected to escape the write step -- that is how
    the sweep learns to stop -- so it is caught here the way the sweep's
    ``finally`` catches it, and the restore still runs.
    """
    written = {}
    escaped = None
    error = None
    try:
        error = gate._write_mutants(changed, written)
    except KeyboardInterrupt as stop:
        escaped = stop
    problems, interrupt = gate._restore_written(name, written, originals)
    return SimpleNamespace(
        written=written, error=error, escaped=escaped,
        problems=problems, interrupt=interrupt,
    )


class _Completed:
    """The three fields of a CompletedProcess the gate reads."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Subprocess:
    """Stands in for the module, so only the gate's own lookups change."""

    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, stdout):
        self._stdout = stdout
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        return _Completed(0, self._stdout)


# Any mutation will do for the two tests that drive ``main``: they replace
# the pattern application, so nothing about this entry's own text matters.
_A_MUTATION = "press-key-discards-the-outcome"


def _install_main_fakes(monkeypatch):
    """Let ``main`` run a whole sweep without a real write or a real pytest.

    Every step that would touch a tracked file or start a process is
    replaced -- the collection subprocess, the pattern application, the
    write, the restore, the cache clear and the pytest run. What is left is
    main's own accounting, which is what these tests read. Returns the list
    the fake pytest records its selections in.
    """
    names = (
        set(gate.FEATURE_TESTS)
        | set(gate.NOT_MUTATED)
        | {n for m in gate.MUTATIONS for n in m["expect"]}
    )
    collected = "".join(f"tests/collected.py::{n}\n" for n in sorted(names))
    monkeypatch.setattr(gate, "subprocess", _Subprocess(collected))
    monkeypatch.setattr(gate, "_apply", lambda mutation, sources: ({}, None))
    monkeypatch.setattr(gate, "_write_mutants", lambda changed, written: None)
    monkeypatch.setattr(
        gate, "_restore_written", lambda name, written, originals: ([], None)
    )
    monkeypatch.setattr(gate, "_clear_pycache", lambda root=None: (None, None))

    runs = []

    def _fake_run_pytest(selection):
        runs.append(list(selection))
        if len(runs) == 1:
            return _Completed(0, "the baseline is green\n")
        mutation = next(m for m in gate.MUTATIONS if m["name"] == _A_MUTATION)
        failed = list(mutation["expect"]) + list(mutation.get("also_fails", {}))
        return _Completed(1, "".join(
            f"FAILED tests/collected.py::{n} - AssertionError\n"
            for n in failed
        ))

    monkeypatch.setattr(gate, "_run_pytest", _fake_run_pytest)
    return runs


class TestTheGateNeverLeavesAMutantBehind:
    """wh-keyboard-refusal-notice.1.3: an interrupted write must be undone."""

    @pytest.mark.parametrize("failure", [KeyboardInterrupt, OSError])
    @pytest.mark.parametrize("when", ["before", "temp", "after"])
    @pytest.mark.parametrize("victim", [0, 1])
    def test_a_failed_write_leaves_both_targets_holding_the_original(
        self, tmp_path, monkeypatch, victim, when, failure
    ):
        """Every point in the replacement window, for both targets.

        ``victim=1`` is the worse case the finding names: the first target
        is written and registered, the second one's write fails, and the
        old code restored only the first.
        """
        targets = _two_targets(tmp_path)
        originals = {t: _ORIGINAL[k] for k, t in zip("ab", targets)}
        changed = {t: _MUTANT[k] for k, t in zip("ab", targets)}
        _failing_write(monkeypatch, targets[victim], when, failure)

        outcome = _write_then_restore(changed, originals)

        for key, target in zip("ab", targets):
            assert target.read_bytes() == _ORIGINAL[key]
        assert outcome.problems == []
        if failure is KeyboardInterrupt:
            assert outcome.escaped is not None
            assert outcome.error is None
        else:
            assert outcome.escaped is None
            assert outcome.error is not None
            assert str(targets[victim]) in outcome.error

    @pytest.mark.parametrize("when", ["before", "temp", "after"])
    def test_a_target_is_registered_before_its_write_is_attempted(
        self, tmp_path, monkeypatch, when
    ):
        """The registry is the only thing the sweep's finally restores from.

        A target registered after its write is missing from it for exactly
        the window in which the file may already hold the mutant.
        """
        targets = _two_targets(tmp_path)
        changed = {t: _MUTANT[k] for k, t in zip("ab", targets)}
        _failing_write(monkeypatch, targets[1], when, KeyboardInterrupt)
        written = {}

        with pytest.raises(KeyboardInterrupt):
            gate._write_mutants(changed, written)

        assert targets[1] in written
        assert written[targets[1]] == _MUTANT["b"].encode("utf-8")

    def test_a_target_never_holds_part_of_a_mutant(
        self, tmp_path, monkeypatch
    ):
        """wh-keyboard-refusal-notice.1.6: no half-written state exists.

        The two earlier attempts both tried to tell a half-written file
        apart from a concurrent save, and both were wrong about which byte
        strings only the gate can produce. ``_atomic_write`` removes the
        state instead, and this reads the moment that makes it true: at
        the replace, the whole mutant is in the neighbour file and the
        target still holds every byte of the original. A write straight to
        the target reaches no replace at all, so ``seen`` stays empty.
        """
        target = tmp_path / "a.py"
        target.write_bytes(_ORIGINAL["a"])
        mutant = _MUTANT["a"].encode("utf-8")
        seen = {}
        real_replace = gate.os.replace

        def _watch(src, dst):
            seen["target"] = Path(dst).read_bytes()
            seen["neighbour"] = Path(src).read_bytes()
            return real_replace(src, dst)

        monkeypatch.setattr(gate.os, "replace", _watch)

        gate._atomic_write(target, mutant)

        assert seen.get("target") == _ORIGINAL["a"]
        assert seen.get("neighbour") == mutant
        assert target.read_bytes() == mutant

    @pytest.mark.parametrize("failure", [KeyboardInterrupt, OSError])
    def test_a_failed_replace_leaves_no_neighbour_file_behind(
        self, tmp_path, monkeypatch, failure
    ):
        """An untracked file in the checkout aborts the review loop.

        The neighbour has to go on every path out, including the interrupt
        that lands between writing it and replacing the target. This drives
        the real ``_atomic_write`` and breaks the replace under it, which
        is the one window in which the neighbour exists.
        """
        target = tmp_path / "a.py"
        target.write_bytes(_ORIGINAL["a"])

        def _refuse(src, dst):
            raise failure()

        monkeypatch.setattr(gate.os, "replace", _refuse)

        with pytest.raises(failure):
            gate._atomic_write(target, _MUTANT["a"].encode("utf-8"))

        assert list(tmp_path.glob("*.gate-tmp")) == []
        assert target.read_bytes() == _ORIGINAL["a"]

    def test_a_successful_write_leaves_no_neighbour_file_behind(
        self, tmp_path
    ):
        """The ordinary path has to clean up too, not only the failing one."""
        target = tmp_path / "a.py"
        target.write_bytes(_ORIGINAL["a"])

        gate._atomic_write(target, _MUTANT["a"].encode("utf-8"))

        assert list(tmp_path.glob("*.gate-tmp")) == []
        assert target.read_bytes() == _MUTANT["a"].encode("utf-8")

    def test_a_write_refuses_a_neighbour_it_did_not_create(self, tmp_path):
        """wh-keyboard-refusal-notice.1.7: never truncate what is there.

        The neighbour name is derived from the target, so it is the same
        on every run. Two things can already hold it, and neither is a
        file to overwrite: a sweep killed before its cleanup ran, whose
        leftover is the evidence that the target may hold that run's
        mutant, and a second sweep against this same checkout.
        """
        target = tmp_path / "a.py"
        target.write_bytes(_ORIGINAL["a"])
        leftover = tmp_path / "a.py.gate-tmp"
        leftover.write_bytes(b"a killed sweep left this\n")

        with pytest.raises(OSError) as refused:
            gate._atomic_write(target, _MUTANT["a"].encode("utf-8"))

        assert str(leftover) in str(refused.value)
        assert leftover.read_bytes() == b"a killed sweep left this\n"
        assert target.read_bytes() == _ORIGINAL["a"]

    def test_a_sweep_reports_the_leftover_and_leaves_the_target_alone(
        self, tmp_path
    ):
        """The refusal has to reach the operator, not just the helper.

        Driven through the write step the sweep really calls, so the
        message the run prints is the one that names the file.
        """
        targets = _two_targets(tmp_path)
        originals = {t: _ORIGINAL[k] for k, t in zip("ab", targets)}
        changed = {t: _MUTANT[k] for k, t in zip("ab", targets)}
        leftover = tmp_path / "a.py.gate-tmp"
        leftover.write_bytes(b"a killed sweep left this\n")

        outcome = _write_then_restore(changed, originals)

        assert outcome.error is not None
        assert str(leftover) in outcome.error
        assert outcome.problems == []
        for key, target in zip("ab", targets):
            assert target.read_bytes() == _ORIGINAL[key]
        assert leftover.read_bytes() == b"a killed sweep left this\n"

    def test_a_failed_cleanup_names_the_orphan_and_keeps_the_real_error(
        self, tmp_path, monkeypatch, capsys
    ):
        """wh-keyboard-refusal-notice.1.8: say what was left behind.

        A Windows scanner can hold the neighbour open after a failed
        replace. The removal then fails too, and the file stays untracked
        in the checkout, which aborts the review loop that reads it. The
        run has to name the path. It must not RAISE it: an exception
        raised in a ``finally`` replaces the one already on its way out,
        so the failed replace -- or a held Ctrl+C -- would disappear.
        """
        target = tmp_path / "a.py"
        target.write_bytes(_ORIGINAL["a"])
        tmp = tmp_path / "a.py.gate-tmp"

        def _refuse_replace(src, dst):
            raise OSError("the target is locked")

        def _refuse_unlink(self, missing_ok=False):
            raise OSError("a scanner is holding it")

        monkeypatch.setattr(gate.os, "replace", _refuse_replace)
        monkeypatch.setattr(Path, "unlink", _refuse_unlink)

        with pytest.raises(OSError) as failed:
            gate._atomic_write(target, _MUTANT["a"].encode("utf-8"))

        assert "the target is locked" in str(failed.value)
        said = capsys.readouterr().out
        assert str(tmp) in said
        assert "a scanner is holding it" in said
        assert target.read_bytes() == _ORIGINAL["a"]

    # Each case is named, and the names carry no space: the gate reads the
    # collected id and keeps only the text up to the first space, so a
    # generated id would leave every expected name matching nothing.
    @pytest.mark.parametrize("saved,why", [
        pytest.param(b"an editor saved this while pytest ran\n",
                     "an unrelated save", id="unrelated-save"),
        pytest.param(b"", "a file cleared and saved", id="empty-save"),
        pytest.param(b"the first ",
                     "a save truncated before the mutation site",
                     id="prefix-save"),
    ])
    def test_a_restore_refuses_every_concurrent_save(self, tmp_path, saved, why):
        """wh-keyboard-refusal-notice.1.6: one guard, and no exceptions.

        The empty save and the shortened save are the two the previous
        prefix rule let through: both are prefixes of the mutant, and
        neither was written by this gate. With the write made atomic the
        only byte string the gate can leave is the whole mutant, so every
        one of these is somebody else's work and is left alone.
        """
        target = tmp_path / "a.py"
        target.write_bytes(saved)

        problem, interrupt = gate._restore(
            target, _ORIGINAL["a"], _MUTANT["a"].encode("utf-8"),
        )

        assert interrupt is None, why
        assert problem is not None, why
        assert "changed while pytest ran" in problem
        assert target.read_bytes() == saved

    def test_a_restore_puts_back_the_whole_mutant(self, tmp_path):
        """The one state the gate does produce has to go back.

        Whether the interrupt landed before or after the caller recorded
        the write no longer matters: the file holds the whole mutant
        either way, and that is what the restore reads.
        """
        target = tmp_path / "a.py"
        mutant = _MUTANT["a"].encode("utf-8")
        target.write_bytes(mutant)

        problem, interrupt = gate._restore(target, _ORIGINAL["a"], mutant)

        assert problem is None
        assert interrupt is None
        assert target.read_bytes() == _ORIGINAL["a"]

    def test_a_restore_of_an_untouched_target_says_nothing_is_wrong(
        self, tmp_path
    ):
        """A write that never landed leaves the original, and that is fine.

        This is the case the concurrent-edit guard must not fire on: the
        file already holds what the restore would write.
        """
        target = tmp_path / "a.py"
        target.write_bytes(_ORIGINAL["a"])

        problem, interrupt = gate._restore(
            target, _ORIGINAL["a"], _MUTANT["a"].encode("utf-8"),
        )

        assert problem is None
        assert interrupt is None
        assert target.read_bytes() == _ORIGINAL["a"]

    def test_a_temporary_file_left_behind_is_caught_at_the_end_of_the_sweep(
        self, tmp_path
    ):
        """wh-keyboard-refusal-notice.1.9: printing it is not enough.

        A cleanup failure prints, and the printed line was justified by a
        claim that turned out to be false: that a surviving neighbour
        always accompanies a failure that ends the run non-zero. A
        SUCCESSFUL replace frees the name, so a file at that path
        afterwards belongs to nobody this run can account for, and the run
        would otherwise report success beside two ERROR lines. The last
        check of the sweep is where any survivor has to be caught, whoever
        wrote it.
        """
        target = tmp_path / "a.py"
        target.write_bytes(_ORIGINAL["a"])
        leftover = tmp_path / "a.py.gate-tmp"
        leftover.write_bytes(b"nobody removed this\n")

        problems = gate._final_mismatches({target: _ORIGINAL["a"]})

        assert len(problems) == 1
        assert str(leftover) in problems[0]

        leftover.unlink()

        assert gate._final_mismatches({target: _ORIGINAL["a"]}) == []

    def test_an_unreadable_target_does_not_hide_the_file_beside_it(
        self, tmp_path, monkeypatch
    ):
        """wh-keyboard-refusal-notice.1.10: two checks, not one.

        A tracked file can be unreadable for a moment on Windows while a
        neighbour from a killed earlier sweep sits beside it. The two
        conditions are independent, so a failed read of the target must
        not decide whether the neighbour is looked for: that neighbour is
        what stops the next run against the target, and the read failure
        clears on its own.
        """
        target = tmp_path / "a.py"
        target.write_bytes(_ORIGINAL["a"])
        leftover = tmp_path / "a.py.gate-tmp"
        leftover.write_bytes(b"a killed sweep left this\n")
        real_read = Path.read_bytes

        def _refuse_read(self):
            if Path(self) == target:
                raise OSError("the file is locked")
            return real_read(self)

        monkeypatch.setattr(Path, "read_bytes", _refuse_read)

        problems = gate._final_mismatches({target: _ORIGINAL["a"]})

        assert len(problems) == 2
        assert any(str(target) in p and "the file is locked" in p
                   for p in problems)
        assert any(str(leftover) in p for p in problems)

    def test_a_mismatch_found_only_at_the_end_of_the_sweep_cannot_exit_zero(
        self, monkeypatch, capsys
    ):
        """The sweep's last check is the one that catches what the rest missed.

        A tracked file still holding a mutant there is the worst outcome
        this gate has, so it cannot be a line of output beside a zero exit.
        """
        runs = _install_main_fakes(monkeypatch)
        monkeypatch.setattr(
            gate, "_final_mismatches",
            lambda originals: ["ui_action_handler.py did not match"],
        )

        code = gate.main(["gate", _A_MUTATION])

        out = capsys.readouterr().out
        assert f"caught   {_A_MUTATION}" in out
        assert "ERROR ui_action_handler.py did not match" in out
        assert code == 1
        assert len(runs) == 2


class TestTheGateRefusesAFailedCacheClear:
    """wh-keyboard-refusal-notice.1.4: a cache that survives stops the run."""

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
        """The retry is the point of the two passes; only the last one counts.

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

    def test_the_sweep_covers_the_whole_service_but_never_the_venv(
        self, tmp_path, monkeypatch
    ):
        """A mutant module is imported through modules that cache too.

        Narrowing the sweep to the two mutation targets would leave cached
        bytecode for every importer of them, which is the stale .pyc this
        step exists to remove. The .venv stays out: it holds no source this
        gate rewrites, and it is large enough to make the sweep slow.
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

    def test_a_failed_pre_baseline_clear_starts_no_pytest(
        self, monkeypatch, capsys
    ):
        """PYTHONDONTWRITEBYTECODE stops a write, never a read.

        A cache the gate could not clear means the next process may import
        bytecode for the source the gate is about to replace, so neither
        the baseline nor a single mutation may start.
        """
        runs = _install_main_fakes(monkeypatch)
        monkeypatch.setattr(
            gate, "_clear_pycache",
            lambda root=None: ("a locked __pycache__ survived", None),
        )

        code = gate.main(["gate", _A_MUTATION])

        out = capsys.readouterr().out
        assert runs == []
        assert code == 1
        assert "ERROR a locked __pycache__ survived" in out

    def test_a_held_interrupt_beats_the_pre_baseline_abort(
        self, monkeypatch, capsys
    ):
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
