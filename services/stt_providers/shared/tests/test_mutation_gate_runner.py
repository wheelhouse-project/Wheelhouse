"""The mutation gate runner's restore discipline.

The runner rewrites tracked source files. Its contract with the tree is
that the pre-run bytes come back, whatever happens, and that no mutant
survives a Ctrl+C. These tests pin the two seams where an interrupt can
land outside the restore: the write that applies the mutant, and the
bytecode-cache clear that must follow every restore attempt.

The mutation-gate skill records why the second one matters: a same-size
mutant restored without clearing the cache leaves bytecode compiled from
the mutant, so the next run executes the mutant from cache while the
source on disk reads correct.
"""
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_gate_runner as runner  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cleanup_evidence(tmp_path, monkeypatch):
    """These mutations own tmp_path, including their recovery state.

    When the real gate runs this suite, its parent deliberately has a pending
    process marker. Temporary-source fixture mutations must not share that
    parent's recovery directory or another test's unconfirmed cleanup state.
    """
    monkeypatch.setattr(runner, "EVIDENCE_ROOT", tmp_path / "gate-evidence")
    monkeypatch.setattr(runner, "_cleanup_confirmed", True)


class _FakeCompleted(SimpleNamespace):
    """Enough of subprocess.CompletedProcess for the runner to read."""


def _green():
    # Real pytest always ends with a result line; the runner requires it.
    return _FakeCompleted(returncode=0, stdout="1 passed in 0.01s\n", stderr="")


def _caught(name):
    return _FakeCompleted(
        returncode=1,
        stdout=f"FAILED tests/x.py::{name} - assert 0\n1 failed in 0.01s\n",
        stderr="")


@pytest.fixture
def one_mutation(tmp_path):
    """A single mutation over a real file in a temporary directory."""
    service = tmp_path / "service"
    (service / "tests").mkdir(parents=True)
    target = service / "target.py"
    target.write_bytes(b"VALUE = 1\n")
    return {
        "name": "value-changed",
        "file": target,
        "service": service,
        "test_file": "tests/x.py",
        "old": "VALUE = 1",
        "new": "VALUE = 2",
        "expect": ["test_value"],
    }, target, b"VALUE = 1\n"


@pytest.fixture
def stub_pytest(monkeypatch):
    """Collection finds the expected test; the baseline is green and the
    mutant is caught."""
    monkeypatch.setattr(runner, "_collected_names",
                        lambda service, test_file: {"test_value"})
    calls = {"n": 0}

    def _run(service, test_file):
        calls["n"] += 1
        return _green() if calls["n"] == 1 else _caught("test_value")

    monkeypatch.setattr(runner, "_run_pytest", _run)
    return calls


class TestAnInterruptAtTheWrite:
    """The mutant is applied by one rename over the target. An interrupt
    landing the instant that rename completes must not leave the mutant on
    disk: the restore in the outer finally is what takes it back off."""

    def test_the_mutant_does_not_survive_an_interrupt_at_the_write(
            self, one_mutation, stub_pytest, monkeypatch):
        mutation, target, original = one_mutation
        real_replace = runner.os.replace
        fired = {"n": 0}

        def _replace_then_interrupt(src, dst):
            real_replace(src, dst)
            # Only the apply is interrupted; the restore's own rename has
            # to be able to finish, or the test proves nothing.
            if Path(dst) == target and fired["n"] == 0:
                fired["n"] = 1
                raise KeyboardInterrupt("ctrl-c as the mutant lands")

        monkeypatch.setattr(runner.os, "replace", _replace_then_interrupt)

        with pytest.raises(KeyboardInterrupt):
            runner.run([mutation], argv=[])

        assert fired["n"] == 1, "the mutant never reached the target"
        assert target.read_bytes() == original


class TestTheCacheClearAfterARestore:
    """_restore may re-raise a held interrupt. The bytecode caches must be
    cleared before it escapes, or a same-size mutant leaves current-looking
    bytecode behind after the source is back."""

    def test_a_reraising_restore_still_clears_the_caches(
            self, one_mutation, stub_pytest, monkeypatch):
        mutation, target, original = one_mutation
        cleared = []

        def _clear(service):
            cleared.append(("clear", target.read_bytes()))

        def _restore_that_reraises(path, data, mutated, name):
            cleared.append(("restore", None))
            raise KeyboardInterrupt("second ctrl-c inside the restore")

        monkeypatch.setattr(runner, "_clear_pycache", _clear)
        monkeypatch.setattr(runner, "_restore", _restore_that_reraises)

        with pytest.raises(KeyboardInterrupt):
            runner.run([mutation], argv=[])

        kinds = [k for k, _ in cleared]
        assert "restore" in kinds, "the restore was never attempted"
        assert kinds[-1] == "clear", (
            "the bytecode caches were not cleared after the restore "
            f"re-raised; call order was {kinds}")


class TestTheOrdinaryPath:
    """The fixes above must not change a normal run."""

    def test_a_caught_mutation_restores_and_reports_success(
            self, one_mutation, stub_pytest, monkeypatch):
        mutation, target, original = one_mutation
        monkeypatch.setattr(runner, "_clear_pycache", lambda service: None)

        rc = runner.run([mutation], argv=[])

        assert rc == 0
        assert target.read_bytes() == original

    def test_a_timeout_is_an_error_and_still_restores(
            self, one_mutation, monkeypatch):
        mutation, target, original = one_mutation
        monkeypatch.setattr(runner, "_collected_names",
                            lambda service, test_file: {"test_value"})
        monkeypatch.setattr(runner, "_clear_pycache", lambda service: None)
        calls = {"n": 0}

        def _run(service, test_file):
            calls["n"] += 1
            if calls["n"] == 1:
                return _green()
            raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

        monkeypatch.setattr(runner, "_run_pytest", _run)

        rc = runner.run([mutation], argv=[])

        assert rc == 1
        assert target.read_bytes() == original


class TestARunWithNoPytestResult:
    """A launch that dies before pytest reports proves nothing about the
    mutant, so it is an error, never a survivor.

    Observed 2026-09-09 (wh-audit13-preengine-load-review.1): two runs of
    a 31-mutation sweep printed only run_tests.py's own lines under host
    load, and the runner reported both as SURVIVED because the failed
    set was empty. Both mutations were caught when run again.
    """

    def test_no_result_line_is_an_error_not_a_survivor(
            self, one_mutation, monkeypatch, capsys):
        mutation, target, original = one_mutation
        monkeypatch.setattr(runner, "_collected_names",
                            lambda service, test_file: {"test_value"})
        monkeypatch.setattr(runner, "_clear_pycache", lambda service: None)
        calls = {"n": 0}

        def _run(service, test_file):
            calls["n"] += 1
            if calls["n"] == 1:
                return _green()
            return _FakeCompleted(
                returncode=1,
                stdout="Running: pytest tests/x.py\n"
                       "[!] JUnit report missing, unreadable, or unchanged; "
                       "skipping summary (no fresh results).\n",
                stderr="")

        monkeypatch.setattr(runner, "_run_pytest", _run)

        rc = runner.run([mutation], argv=[])
        out = capsys.readouterr().out

        assert rc == 1
        assert "ERROR value-changed: no-pytest-result (exit 1)" in out
        assert "SURVIVED" not in out
        assert target.read_bytes() == original


class TestAPartialWriteAtTheApply:
    """Path.write_bytes truncates before it writes. An interrupt landing
    between those two steps leaves bytes that are neither the pre-run
    snapshot nor the whole mutant, and a source file in that state does not
    even compile (wh-stt-load-metrics.3.2.2)."""

    def test_a_partial_mutant_write_is_not_left_on_disk(
            self, one_mutation, stub_pytest, monkeypatch):
        mutation, target, original = one_mutation
        real_write = Path.write_bytes
        fired = {"n": 0}

        def _half_then_interrupt(self, data):
            """Truncate and write half the bytes, then take the interrupt."""
            if data != original and fired["n"] == 0:
                fired["n"] = 1
                real_write(self, data[:len(data) // 2])
                raise KeyboardInterrupt("ctrl-c mid-write")
            return real_write(self, data)

        monkeypatch.setattr(Path, "write_bytes", _half_then_interrupt)

        with pytest.raises(KeyboardInterrupt):
            runner.run([mutation], argv=[])

        assert fired["n"] == 1, "the partial write never happened"
        assert target.read_bytes() == original


class TestAPartialWriteAtTheRestore:
    """The restore write has the same shape as the apply write, so an
    interrupt can leave the target half written there too. The retry must
    finish the job rather than read its own damage as an editor's save."""

    def test_a_partial_restore_write_is_retried_to_completion(
            self, one_mutation, stub_pytest, monkeypatch):
        mutation, target, original = one_mutation
        cleared = []
        monkeypatch.setattr(runner, "_clear_pycache",
                            lambda service: cleared.append(target.read_bytes()))
        real_write = Path.write_bytes
        fired = {"n": 0}
        stop = KeyboardInterrupt("ctrl-c mid-restore")

        def _half_then_interrupt(self, data):
            # Only the restore write carries the pre-run bytes, and only its
            # first attempt is interrupted: the retry must be able to finish.
            if data == original and fired["n"] == 0:
                fired["n"] = 1
                real_write(self, data[:len(data) // 2])
                raise stop
            return real_write(self, data)

        monkeypatch.setattr(Path, "write_bytes", _half_then_interrupt)

        with pytest.raises(KeyboardInterrupt) as caught:
            runner.run([mutation], argv=[])

        assert fired["n"] == 1, "the partial restore write never happened"
        assert target.read_bytes() == original
        assert caught.value is stop, "successful restoration must retain cancellation"
        assert cleared[-1] == original, "cache clearing must follow the completed restore"
        assert stub_pytest["n"] == 2


class TestARealConcurrentEdit:
    """The protection the partial-write fix must not weaken: bytes this run
    did not write are somebody's save, and the stale snapshot must not
    replace them."""

    def test_an_editor_save_is_refused_not_overwritten(
            self, one_mutation, monkeypatch, capsys):
        mutation, target, original = one_mutation
        edited = b"VALUE = 99  # saved from an editor while pytest ran\n"
        monkeypatch.setattr(runner, "_collected_names",
                            lambda service, test_file: {"test_value"})
        monkeypatch.setattr(runner, "_clear_pycache", lambda service: None)
        calls = {"n": 0}

        def _run(service, test_file):
            calls["n"] += 1
            if calls["n"] == 1:
                return _green()
            target.write_bytes(edited)
            return _caught("test_value")

        monkeypatch.setattr(runner, "_run_pytest", _run)

        rc = runner.run([mutation], argv=[])

        assert rc == 1
        assert target.read_bytes() == edited
        assert "refusing to overwrite the concurrent edit" in capsys.readouterr().out


def _never_entered():
    """A body nothing calls.

    Its code object is the decoy a stand-in below wears as ``__code__``, so
    that no traceback frame can ever belong to it. That is what the caller
    sees when an interrupt reaches a callee before the callee has a frame
    at all, which is the event these tests are about.
    """


class _AtTheDoor:
    """A callee the interrupt reaches at its door, once, on a chosen call.

    A stand-in that raised from its own body could not reproduce the event:
    its frame would already have moved to one of its statements, which is
    the shape of a callee that RAN. Wearing ``_never_entered``'s code
    object gives the guard the other shape -- no frame of the callee's in
    the traceback at all -- and unlike an injected trace function it can be
    used on a chosen call rather than only the first.
    """

    def __init__(self, real, on=1, message="at the door"):
        self._real = real
        self._on = on
        self._message = message
        self.calls = 0
        self.doors = 0
        self.__code__ = _never_entered.__code__

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls == self._on and self.doors == 0:
            self.doors = 1
            raise KeyboardInterrupt(self._message)
        return self._real(*args, **kwargs)


class TestTwoGateRunsInOneCheckout:
    """Two gate processes can select mutations over the same source file.

    The write-then-rename protects a writer from its own partial write. It
    says nothing about a second writer that shares the temporary name: each
    can move the other's bytes over the target, and each can delete the
    file the other is about to rename (wh-stt-load-metrics.3.2.4).
    """

    def test_two_runs_do_not_share_one_temporary_file(
            self, tmp_path, monkeypatch):
        target = tmp_path / "victim.py"
        target.write_bytes(b"ORIGINAL\n")
        used = []
        real_replace = runner.os.replace

        def _record(src, dst):
            used.append(Path(src))
            real_replace(src, dst)

        monkeypatch.setattr(runner.os, "replace", _record)
        monkeypatch.setattr(runner.os, "getpid", lambda: 1111)
        runner._atomic_write_bytes(target, b"FIRST\n")
        monkeypatch.setattr(runner.os, "getpid", lambda: 2222)
        runner._atomic_write_bytes(target, b"SECOND\n")

        assert used[0] != used[1], (
            f"both runs wrote {used[0].name}; either can move the other's "
            "bytes over the target, or delete the file the other is about "
            "to rename")

    def test_a_concurrent_run_cannot_capture_this_runs_rename(
            self, tmp_path, monkeypatch):
        """The interleaving the finding names. The other run completes its
        whole write while this one sits between its temporary write and its
        rename. This run must still publish its own bytes, and must still
        find its own temporary file where it left it.
        """
        target = tmp_path / "victim.py"
        target.write_bytes(b"ORIGINAL\n")
        real_replace = runner.os.replace
        other = {}

        def _the_other_run_writes_between(src, dst):
            if not other:
                other["done"] = True
                monkeypatch.setattr(runner.os, "getpid", lambda: 2222)
                runner._atomic_write_bytes(target, b"OTHER\n")
                monkeypatch.setattr(runner.os, "getpid", lambda: 1111)
            real_replace(src, dst)

        monkeypatch.setattr(runner.os, "replace",
                            _the_other_run_writes_between)
        monkeypatch.setattr(runner.os, "getpid", lambda: 1111)
        runner._atomic_write_bytes(target, b"MINE\n")

        assert other == {"done": True}, "the other run never wrote"
        assert target.read_bytes() == b"MINE\n"


class TestAnInterruptAtACleanupCallsDoor:
    """A guard written inside a helper cannot cover that helper's own call
    boundary. The interpreter delivers a pending signal at the callee's
    first instruction, so a Ctrl+C there skips the whole call -- and at
    that moment the mutant is already published in the tracked file
    (wh-stt-load-metrics.3.2.5).
    """

    def test_the_restore_runs_when_the_interrupt_lands_at_its_door(
            self, one_mutation, stub_pytest, monkeypatch):
        mutation, target, original = one_mutation
        door = _AtTheDoor(runner._restore)
        monkeypatch.setattr(runner, "_restore", door)

        with pytest.raises(KeyboardInterrupt):
            runner.run([mutation], argv=[])

        assert target.read_bytes() == original
        assert door.calls == 2, "the restore was never made again"

    def test_the_cache_clear_runs_when_the_interrupt_lands_at_its_door(
            self, one_mutation, stub_pytest, monkeypatch):
        """The same door on the call that stops a mutant .pyc being read
        beside a source file already put back. The door is chosen by what
        has happened rather than by a call count: a count would go on
        passing while pointing at some other clear if the run gained one.
        """
        real_clear = runner._clear_pycache
        real_restore = runner._restore
        mutation, target, original = one_mutation
        state = {"restored": False, "after": 0, "doors": 0}

        def _watch_restore(*args):
            outcome = real_restore(*args)
            state["restored"] = True
            return outcome

        class _Clear:
            # The decoy code object again: the guard reads "the body never
            # ran" off the traceback, and a frame of _never_entered's can
            # never appear in one.
            __code__ = _never_entered.__code__

            def __call__(self, service):
                if state["restored"]:
                    state["after"] += 1
                    if state["doors"] == 0:
                        state["doors"] = 1
                        raise KeyboardInterrupt("at the cache clear's door")
                return real_clear(service)

        monkeypatch.setattr(runner, "_restore", _watch_restore)
        monkeypatch.setattr(runner, "_clear_pycache", _Clear())

        with pytest.raises(KeyboardInterrupt):
            runner.run([mutation], argv=[])

        assert state["after"] == 2, (
            "the cache clear after the restore was skipped; a same-size "
            "mutant leaves bytecode compiled from the mutant beside a "
            "source file that reads correct")
        assert target.read_bytes() == original


class TestTheCheckMode:
    """--check answers "would every mutation still apply?" without
    running a single test.

    A gate's patterns go stale as the code they quote is rewritten, and a
    stale pattern reads as a survivor hours into a sweep. The check is
    what finds that in seconds. The mutation-gate skill records why it
    must also compile each mutant: a pattern can match exactly once and
    still produce text that does not parse, and a check that only asks
    "does it match" then reports clean while the mutation can never run.
    """

    def test_a_gate_whose_patterns_all_match_passes(
            self, one_mutation, stub_pytest):
        mutation, _target, _original = one_mutation
        assert runner.run([mutation], argv=["--check"]) == 0

    def test_the_check_runs_no_tests(self, one_mutation, stub_pytest):
        """The whole point is the seconds-long answer. A check that falls
        through into the run costs the hours it was meant to save."""
        mutation, _target, _original = one_mutation
        assert runner.run([mutation], argv=["--check"]) == 0
        assert stub_pytest["n"] == 0

    def test_the_check_leaves_the_target_untouched(
            self, one_mutation, stub_pytest):
        mutation, target, original = one_mutation
        assert runner.run([mutation], argv=["--check"]) == 0
        assert target.read_bytes() == original

    def test_a_stale_pattern_fails_the_check(
            self, one_mutation, stub_pytest, capsys):
        mutation, _target, _original = one_mutation
        mutation["old"] = "VALUE = 99"
        mutation["new"] = "VALUE = 98"
        assert runner.run([mutation], argv=["--check"]) == 1
        assert "value-changed" in capsys.readouterr().out

    def test_an_ambiguous_pattern_fails_the_check(
            self, one_mutation, stub_pytest, capsys):
        """Two matches is not a survivor and not a pass: replace() edits
        the first, so the mutation lands somewhere its name does not
        claim."""
        mutation, target, _original = one_mutation
        target.write_bytes(b"VALUE = 1\nVALUE = 1\n")
        assert runner.run([mutation], argv=["--check"]) == 1
        assert "ambiguous" in capsys.readouterr().out

    def test_a_match_inside_a_deeper_indent_fails_the_check(
            self, one_mutation, stub_pytest, capsys):
        """One match, at a place the pattern did not name.

        An indented line contains every shallower indentation of itself
        as a substring, so a pattern written for four spaces still finds
        a line written with eight. The count is one, so neither the
        not-found nor the ambiguous rule fires, and the edit lands four
        characters into an indent nobody chose. Observed on the
        provider-ready-handshake gate: a "with" statement added four
        spaces and six patterns went on reporting ok.
        """
        mutation, target, _original = one_mutation
        target.write_bytes(b"if x:\n    if y:\n        VALUE = 1\n")
        mutation["old"] = "    VALUE = 1"
        mutation["new"] = "    VALUE = 2"
        assert runner.run([mutation], argv=["--check"]) == 1
        assert "not at a line start" in capsys.readouterr().out

    def test_a_pattern_that_quotes_mid_line_is_still_legal(
            self, one_mutation, stub_pytest, capsys):
        """The rule reads the characters before the match, not the
        column. Real text before it means the author meant it."""
        mutation, target, _original = one_mutation
        target.write_bytes(b"x = VALUE = 1\n")
        assert runner.run([mutation], argv=["--check"]) == 0
        assert "not at a line start" not in capsys.readouterr().out

    def test_a_mutant_that_does_not_compile_fails_the_check(
            self, one_mutation, stub_pytest, capsys):
        mutation, _target, _original = one_mutation
        mutation["new"] = "VALUE = ("
        assert runner.run([mutation], argv=["--check"]) == 1
        assert "does not compile" in capsys.readouterr().out

    def test_stale_and_non_compiling_are_counted_apart(
            self, one_mutation, stub_pytest, capsys):
        """They are different repairs: a stale pattern is re-quoted
        against the current source, a non-compiling mutant is rewritten.
        A summary that adds them together tells the reader neither."""
        mutation, target, _original = one_mutation
        target.write_bytes(b"VALUE = 1\nOTHER = 1\n")
        stale = dict(mutation, name="stale-one", old="VALUE = 99",
                     new="VALUE = 98")
        broken = dict(mutation, name="broken-one", old="OTHER = 1",
                      new="OTHER = (")
        assert runner.run([stale, broken], argv=["--check"]) == 1
        out = capsys.readouterr().out
        assert "1 stale" in out
        assert "1 that do not compile" in out

    def test_a_clean_check_says_zero_of_each(
            self, one_mutation, stub_pytest, capsys):
        mutation, _target, _original = one_mutation
        runner.run([mutation], argv=["--check"])
        out = capsys.readouterr().out
        assert "0 stale" in out
        assert "0 that do not compile" in out

    def test_the_check_translates_line_endings(
            self, one_mutation, stub_pytest):
        """A CRLF file must not report every multi-line pattern stale."""
        mutation, target, _original = one_mutation
        target.write_bytes(b"VALUE = 1\r\nOTHER = 2\r\n")
        mutation["old"] = "VALUE = 1\nOTHER = 2"
        mutation["new"] = "VALUE = 2\nOTHER = 3"
        assert runner.run([mutation], argv=["--check"]) == 0

    def test_a_non_python_target_is_not_compiled(
            self, one_mutation, tmp_path, stub_pytest):
        """A document has no syntax. Compiling it would report every
        document mutation as broken."""
        mutation, _target, _original = one_mutation
        doc = tmp_path / "service" / "notes.md"
        doc.write_bytes(b"a claim\n")
        mutation["file"] = doc
        mutation["old"] = "a claim"
        mutation["new"] = "a different claim"
        assert runner.run([mutation], argv=["--check"]) == 0

    def test_a_powershell_mutant_that_does_not_parse_fails_the_check(
            self, one_mutation, tmp_path, stub_pytest, capsys):
        """The PowerShell half of the same rule (wh-parakeet-hotword-vocab.3).

        The installer is a .ps1 file and its test harness begins by parsing
        the whole script, so a mutant that does not parse fails every test
        in the selection at once -- including the ones the mutation names --
        and reads as caught while proving nothing."""
        if shutil.which("powershell") is None and shutil.which("pwsh") is None:
            pytest.skip("no powershell on PATH to ask")
        script = tmp_path / "service" / "thing.ps1"
        script.write_bytes(b"function Foo {\n    $x = 1\n}\n")
        mutation, _target, _original = one_mutation
        mutation["file"] = script
        mutation["old"] = "    $x = 1\n"
        mutation["new"] = "    if ($true) {\n"
        assert runner.run([mutation], argv=["--check"]) == 1
        out = capsys.readouterr().out
        assert "does not parse" in out
        assert "Missing closing" in out, (
            "the message must name the reason, not only the location: " + out
        )
        assert script.read_bytes() == b"function Foo {\n    $x = 1\n}\n", (
            "the check must leave the target exactly as it found it"
        )

    def test_a_powershell_mutant_that_parses_passes_the_check(
            self, one_mutation, tmp_path, stub_pytest):
        """The other half: a valid PowerShell mutant must not be refused,
        or every .ps1 mutation would report as broken."""
        if shutil.which("powershell") is None and shutil.which("pwsh") is None:
            pytest.skip("no powershell on PATH to ask")
        script = tmp_path / "service" / "thing.ps1"
        script.write_bytes(b"function Foo {\n    $x = 1\n}\n")
        mutation, _target, _original = one_mutation
        mutation["file"] = script
        mutation["old"] = "    $x = 1\n"
        mutation["new"] = "    $x = 2\n"
        assert runner.run([mutation], argv=["--check"]) == 0

    def test_the_check_still_honours_a_name_filter(
            self, one_mutation, stub_pytest):
        """--check sits beside the filter arguments, it does not replace
        them; checking one mutation of a large gate must stay possible."""
        mutation, target, _original = one_mutation
        target.write_bytes(b"VALUE = 1\nOTHER = 1\n")
        stale = dict(mutation, name="stale-one", old="VALUE = 99",
                     new="VALUE = 98")
        good = dict(mutation, name="good-one", old="OTHER = 1",
                    new="OTHER = 2")
        assert runner.run([stale, good], argv=["--check", "good-one"]) == 0
        assert stub_pytest["n"] == 0
