"""Tests for LocalAIServerLauncher (wh-ai-runtime-server-launcher).

No test here starts a real model server. The process starter and the health
probe are both injected, so every case runs in milliseconds and the assertions
are about the command line and the state machine rather than about llama.cpp.
"""

import contextlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from ai import server_launcher
from ai.runtime_config import RuntimeConfig
from ai.server_launcher import (
    Liveness,
    LocalAIServerLauncher,
    _ServerRecord,
    _default_owner_alive,
    _default_spawn,
    _default_stale_probe,
    _default_stale_terminate,
    _kill_child_when_we_exit,
    _own_process_name,
)


def _config(**overrides):
    base = dict(
        enabled=True,
        model_path=r"C:\models\gemma-4-E4B.gguf",
        binary_dir=r"C:\llama",
        port=8899,
        context_size=8192,
        gpu_layers=99,
        startup_timeout_seconds=90,
        invalid_key=None,
    )
    base.update(overrides)
    return RuntimeConfig(**base)


class FakeProcess:
    """Stands in for a Popen handle."""

    def __init__(self, exits_with=None, pid=1234):
        self._exits_with = exits_with
        self.pid = pid
        self.terminated = False
        self.killed = False
        self.waited = False

    def die(self, code=1):
        """The server exited on its own, with nobody having asked it to."""
        self._exits_with = code

    def poll(self):
        return self._exits_with

    def terminate(self):
        self.terminated = True
        self._exits_with = 0

    def kill(self):
        self.killed = True
        self._exits_with = -9

    def wait(self, timeout=None):
        self.waited = True
        if self._exits_with is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return self._exits_with


class Spawner:
    """Records the command line it was asked to run."""

    def __init__(self, process=None):
        self.commands = []
        self.process = process or FakeProcess()

    def __call__(self, command):
        self.commands.append(command)
        return self.process

    @property
    def command(self):
        assert len(self.commands) == 1, f"expected one spawn, got {len(self.commands)}"
        return self.commands[0]


def _healthy_after(n):
    """A probe that reports unhealthy n times, then healthy."""
    calls = {"n": 0}

    def probe(port):
        calls["n"] += 1
        return calls["n"] > n

    probe.calls = calls
    return probe


def _never_healthy(port):
    return False


def _sleepless(seconds):
    """Stands in for time.sleep so the timeout cases do not really wait."""
    _sleepless.total += seconds


_sleepless.total = 0.0


class LeftoverProcesses:
    """The processes a previous, crashed session might have left running.

    ``probe`` answers the one question the launcher asks: is this recorded
    number a model server of ours that is still running? A number belonging to
    an unrelated program answers False, which is what stops the launcher from
    terminating something the user needs.

    ``terminate`` reports whether the process is confirmed gone. Numbers put
    in ``unstoppable`` refuse to die, standing in for a server we lack the
    rights to end, or one wedged so badly that neither terminate nor kill
    takes effect.
    """

    def __init__(self, ours=(), unstoppable=()):
        self.ours = set(ours)
        self.unstoppable = set(unstoppable)
        self.terminated = []

    def probe(self, pid):
        return Liveness.ALIVE if pid in self.ours else Liveness.GONE

    def terminate(self, pid):
        self.terminated.append(pid)
        if pid in self.unstoppable:
            return False
        self.ours.discard(pid)
        return True


def _record(server_pid, owner_pid=4242, owner_name="python.exe"):
    """The text the launcher writes to the record file.

    The owner defaults to a real process id and name because every record the
    launcher writes now has both: a structured record missing either is read as
    corrupt, not as a server whose owner has gone. The crashed-session case is
    expressed by the owner check answering GONE, not by an absent owner.
    """
    return _ServerRecord(server_pid, owner_pid, owner_name).format()


def _no_owner(pid, name):
    """Every recorded owner has gone -- the plain crashed-session case."""
    return Liveness.GONE


def _owner_still_running(pid, name):
    """The Wheelhouse that started the recorded server is still running."""
    return Liveness.ALIVE


def _launcher(config=None, spawn=None, probe=None, clock=None,
              pid_file=None, leftovers=None, owner_alive=None,
              start_lock=None):
    leftovers = leftovers or LeftoverProcesses()
    spawn = spawn or Spawner()

    # The default machine: the port answers exactly while a server we started
    # is running, and goes quiet when it exits. One probe answers both
    # questions the launcher asks -- "is the port taken?" before the spawn and
    # "is it ready?" after -- because in the real system it is one probe.
    #
    # It reads the launcher's own two handles rather than remembering what it
    # spawned, because several tests replace _spawn directly to start a second
    # server. A probe that remembered the first one would keep answering for a
    # process that has exited, and the wait for the second server would never
    # end.
    holder = {}

    def default_probe(port):
        launcher = holder.get("launcher")
        if launcher is None:
            return False
        process = launcher._process or launcher._unstopped
        return process is not None and process.poll() is None

    launcher = LocalAIServerLauncher(
        config or _config(),
        spawn=spawn,
        probe=probe or default_probe,
        sleep=lambda s: None,
        monotonic=clock or (lambda: 0.0),
        pid_file=pid_file,
        stale_probe=leftovers.probe,
        stale_terminate=leftovers.terminate,
        # Unless a test says otherwise, whoever recorded the server has gone.
        # That is the crashed-session case the recovery path is written for.
        owner_alive=owner_alive or _no_owner,
        # Yields True: the lock was taken. Tests that want the other answer
        # pass their own. nullcontext yields None, which reads as "no
        # exclusivity" and would stop every record deletion in this file.
        start_lock=start_lock or (lambda port: contextlib.nullcontext(True)),
    )
    holder["launcher"] = launcher
    return launcher


class TestTheCommandLine:

    def test_names_the_binary_in_the_configured_directory(self):
        spawn = Spawner()
        _launcher(spawn=spawn).start()
        assert spawn.command[0] == str(Path(r"C:\llama") / "llama-server.exe")

    def test_carries_every_required_argument(self):
        spawn = Spawner()
        _launcher(spawn=spawn).start()
        cmd = spawn.command
        for flag, value in (
            ("-m", r"C:\models\gemma-4-E4B.gguf"),
            ("--host", "127.0.0.1"),
            ("--port", "8899"),
            ("-c", "8192"),
            ("-ngl", "99"),
        ):
            assert flag in cmd, f"{flag} missing from {cmd}"
            assert cmd[cmd.index(flag) + 1] == value, f"{flag} has the wrong value"
        assert "--no-webui" in cmd

    def test_turns_reasoning_off(self):
        """Both flags are required, not decorative.

        Without them the model spends its whole answer budget on hidden
        reasoning and returns an empty success, which the fix command reports
        as a failure the user cannot act on.
        """
        spawn = Spawner()
        _launcher(spawn=spawn).start()
        cmd = spawn.command
        assert cmd[cmd.index("--reasoning") + 1] == "off"
        assert cmd[cmd.index("--reasoning-budget") + 1] == "0"

    def test_the_port_comes_from_the_configuration(self):
        spawn = Spawner()
        _launcher(_config(port=9001), spawn=spawn).start()
        cmd = spawn.command
        assert cmd[cmd.index("--port") + 1] == "9001"

    def test_processor_only_placement_is_passed_through(self):
        spawn = Spawner()
        _launcher(_config(gpu_layers=0), spawn=spawn).start()
        cmd = spawn.command
        assert cmd[cmd.index("-ngl") + 1] == "0"


class TestStarting:

    def test_a_disabled_configuration_starts_nothing(self):
        spawn = Spawner()
        launcher = _launcher(_config(enabled=False), spawn=spawn)
        assert launcher.start() is False
        assert spawn.commands == []
        assert launcher.is_running is False

    def test_reports_success_once_the_server_is_healthy(self):
        launcher = _launcher(probe=_healthy_after(3))
        assert launcher.start() is True
        assert launcher.is_running is True

    def test_waits_for_health_rather_than_assuming_it(self):
        probe = _healthy_after(4)
        assert _launcher(probe=probe).start() is True
        assert probe.calls["n"] == 5

    def test_gives_up_after_the_configured_timeout(self):
        ticks = iter([0.0] + [float(t) for t in range(1, 400)])
        launcher = _launcher(
            _config(startup_timeout_seconds=10),
            probe=_never_healthy,
            clock=lambda: next(ticks),
        )
        assert launcher.start() is False
        assert launcher.is_running is False

    def test_a_timeout_stops_the_process_it_started(self):
        """Otherwise a server that came up slowly is left running unattended."""
        process = FakeProcess()
        spawn = Spawner(process)
        ticks = iter([0.0] + [float(t) for t in range(1, 400)])
        launcher = _launcher(
            _config(startup_timeout_seconds=10),
            spawn=spawn,
            probe=_never_healthy,
            clock=lambda: next(ticks),
        )
        launcher.start()
        assert process.terminated is True

    def test_a_process_that_exits_early_fails_without_waiting(self):
        process = FakeProcess(exits_with=1)
        launcher = _launcher(spawn=Spawner(process), probe=_never_healthy)
        assert launcher.start() is False

    def test_a_spawn_failure_is_reported_not_raised(self):
        def explode(command):
            raise OSError("no such file")

        launcher = _launcher(spawn=explode)
        assert launcher.start() is False
        assert launcher.is_running is False

    def test_starting_twice_does_not_spawn_a_second_server(self):
        spawn = Spawner()
        launcher = _launcher(spawn=spawn)
        assert launcher.start() is True
        assert launcher.start() is True
        assert len(spawn.commands) == 1


class TestStopping:

    def test_stop_terminates_the_process(self):
        process = FakeProcess()
        launcher = _launcher(spawn=Spawner(process))
        launcher.start()
        launcher.stop()
        assert process.terminated is True
        assert launcher.is_running is False

    def test_stop_kills_a_process_that_ignores_terminate(self):
        class Stubborn(FakeProcess):
            def terminate(self):
                self.terminated = True  # but stays alive

        process = Stubborn()
        launcher = _launcher(spawn=Spawner(process))
        launcher.start()
        launcher.stop()
        assert process.killed is True

    def test_stop_without_a_start_is_harmless(self):
        _launcher().stop()

    def test_stop_is_safe_to_call_twice(self):
        process = FakeProcess()
        launcher = _launcher(spawn=Spawner(process))
        launcher.start()
        launcher.stop()
        launcher.stop()
        assert launcher.is_running is False

    def test_stop_after_a_failed_start_leaves_nothing_running(self):
        process = FakeProcess()
        ticks = iter([0.0] + [float(t) for t in range(1, 400)])
        launcher = _launcher(
            _config(startup_timeout_seconds=5),
            spawn=Spawner(process),
            probe=_never_healthy,
            clock=lambda: next(ticks),
        )
        launcher.start()
        launcher.stop()
        assert launcher.is_running is False


class UnstoppableProcess(FakeProcess):
    """A server that stays running whatever is done to it.

    Stands in for one this account lacks the rights to end, and for one wedged
    badly enough that neither terminate nor kill takes effect. ``raises_at``
    names the calls that fail outright; the calls not named appear to succeed
    and the process still does not exit, so the wait that follows times out.
    """

    def __init__(self, raises_at=(), pid=1234):
        super().__init__(exits_with=None, pid=pid)
        self._raises_at = set(raises_at)
        # How many times something has asked this process to stop. What a
        # test asserts on when the point is that a second attempt was made.
        self.attempts = 0

    def terminate(self):
        self.attempts += 1
        self.terminated = True
        if "terminate" in self._raises_at:
            raise PermissionError("access is denied")

    def kill(self):
        self.killed = True
        if "kill" in self._raises_at:
            raise PermissionError("access is denied")


class TestAStopThatCouldNotEndTheServer:
    """The record is the only way anything ever finds that server again.

    stop() deleted it first and tried to end the process afterwards, so every
    failure below left llama-server.exe running with nothing on disk naming
    it: holding the port and several gigabytes of memory for as long as the
    machine stays on, and unreachable by every later start. A user who
    controls this machine by voice cannot open Task Manager and clear it.

    This is the same failure already fixed for a server left behind by an
    earlier session. It survived on the ordinary shutdown path, which is the
    one that runs every time Wheelhouse closes.
    """

    @pytest.mark.parametrize(
        "raises_at",
        [
            pytest.param((), id="neither call takes effect"),
            pytest.param(("terminate",), id="terminate is refused"),
            pytest.param(("kill",), id="kill is refused"),
        ],
    )
    def test_a_server_that_would_not_stop_keeps_its_record(
        self, tmp_path, raises_at
    ):
        pid_file = tmp_path / "llama-server.pid"
        process = UnstoppableProcess(raises_at=raises_at)
        launcher = _launcher(spawn=Spawner(process), pid_file=pid_file)

        assert launcher.start() is True
        assert pid_file.exists()

        launcher.stop()

        assert pid_file.exists(), (
            "the only handle on a server that is still running was deleted"
        )

    def test_a_server_that_did_stop_has_its_record_removed(self, tmp_path):
        """The other half, so that keeping the record does not become the
        answer in every case: a confirmed exit must still clear it, or the
        next start spends its time chasing a server that has gone.
        """
        pid_file = tmp_path / "llama-server.pid"
        launcher = _launcher(spawn=Spawner(FakeProcess()), pid_file=pid_file)

        launcher.start()
        launcher.stop()

        assert not pid_file.exists()

    def test_a_server_that_had_already_exited_has_its_record_removed(
        self, tmp_path
    ):
        pid_file = tmp_path / "llama-server.pid"
        process = FakeProcess()
        launcher = _launcher(spawn=Spawner(process), pid_file=pid_file)

        launcher.start()
        process.die(0)
        launcher.stop()

        assert not pid_file.exists()


class TestNoticingTheServerDied:
    """The server can exit on its own after a healthy start -- it runs out of
    memory, the user ends it in Task Manager, antivirus removes it. Holding the
    old handle and calling it a running server makes every later question about
    the server answer with the state it had minutes ago.
    """

    def test_a_server_that_exits_is_no_longer_running(self):
        process = FakeProcess()
        launcher = _launcher(spawn=Spawner(process))
        launcher.start()
        assert launcher.is_running is True
        process.die(code=3)
        assert launcher.is_running is False

    def test_a_server_that_exited_can_be_started_again(self):
        """The guard at the top of start() has to ask whether the server is
        still running, not whether we once started one. Asking the wrong
        question reports success without starting anything, and the AI
        features stay broken for the rest of the session.
        """
        first, second = FakeProcess(pid=11), FakeProcess(pid=22)
        handles = iter([first, second])
        commands = []

        def spawn(command):
            commands.append(command)
            return next(handles)

        launcher = _launcher(spawn=spawn)
        assert launcher.start() is True
        first.die(code=3)
        assert launcher.start() is True
        assert len(commands) == 2, "the dead handle was treated as a live server"
        assert launcher.is_running is True

    def test_a_live_server_is_still_reported_as_running(self):
        """The obvious way to fail the two tests above is to report False
        always, which would leave stop() with nothing to stop."""
        launcher = _launcher(spawn=Spawner(FakeProcess()))
        launcher.start()
        assert launcher.is_running is True

    def test_the_death_is_logged_once_however_often_it_is_asked(self, caplog):
        """Letting go of the dead handle is what makes the report happen once.

        Every answer is the same either way, so this log line is the only place
        the difference shows. Keeping the handle would write the warning on
        every question instead, and a caller that asks in a loop would fill the
        log with one event repeated.
        """
        process = FakeProcess()
        launcher = _launcher(spawn=Spawner(process))
        launcher.start()
        process.die(code=3)
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                assert launcher.is_running is False
        exits = [r for r in caplog.records if "exited on its own" in r.getMessage()]
        assert len(exits) == 1, f"reported {len(exits)} times, not once"


class TestRecoveringFromACrashedSession:
    """Wheelhouse can exit without running its shutdown path -- a crash, a power
    loss, antivirus ending the Logic process. The model server it started keeps
    running, holding the port and several gigabytes of memory. The next start
    cannot bind the port, and the user is told only that the AI is unavailable.

    Recording the process id is how the next run finds that server. The design
    spec requires it, and the speech provider launcher already works this way.
    """

    def test_the_process_id_is_recorded_for_the_next_run(self, tmp_path):
        pid_file = tmp_path / "llama-server.pid"
        launcher = _launcher(spawn=Spawner(FakeProcess(pid=4242)),
                             pid_file=pid_file)
        assert launcher.start() is True
        written = _ServerRecord.parse(pid_file.read_text())
        assert written is not None
        assert written.server_pid == 4242

    def test_the_record_says_which_wheelhouse_owns_the_server(self, tmp_path):
        """Which server it is does not say whether it has been abandoned.

        An orphan is a server whose owner has gone. Without the owner in the
        record, a second Wheelhouse reading it sees only "still running", and
        the only safe-looking action -- stop it -- ends a server the first
        Wheelhouse is using.
        """
        pid_file = tmp_path / "llama-server.pid"
        launcher = _launcher(spawn=Spawner(FakeProcess(pid=4242)),
                             pid_file=pid_file)
        launcher.start()

        written = _ServerRecord.parse(pid_file.read_text())
        assert written is not None
        assert written.owner_pid == os.getpid()
        assert written.owner_name == _own_process_name()

    def test_a_server_left_by_the_last_session_is_stopped(self, tmp_path):
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(_record(777))
        leftovers = LeftoverProcesses(ours=[777])
        launcher = _launcher(pid_file=pid_file, leftovers=leftovers)
        assert launcher.start() is True
        assert leftovers.terminated == [777]

    def test_a_record_holding_only_a_number_is_still_understood(self, tmp_path):
        """An earlier build wrote just the number. Upgrading over it must not
        lose the one thing that can find a server it left running.
        """
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text("777")
        leftovers = LeftoverProcesses(ours=[777])
        launcher = _launcher(pid_file=pid_file, leftovers=leftovers)
        assert launcher.start() is True
        assert leftovers.terminated == [777]

    def test_the_old_server_is_stopped_before_the_new_one_starts(self, tmp_path):
        """Order is the whole point. Starting first means the new server tries
        to bind a port the old one still holds, and it exits immediately.
        """
        events = []
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(_record(777))

        class Recording(LeftoverProcesses):
            def terminate(self, pid):
                events.append("stopped the old server")
                return super().terminate(pid)

        def spawn(command):
            events.append("started the new server")
            return FakeProcess()

        launcher = _launcher(spawn=spawn, pid_file=pid_file,
                             leftovers=Recording(ours=[777]))
        launcher.start()
        assert events == ["stopped the old server", "started the new server"]

    def test_a_number_belonging_to_something_else_is_left_alone(self, tmp_path):
        """Windows gives process ids out again after a program ends. The
        recorded number may now belong to a program the user is relying on.
        """
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text("777")
        leftovers = LeftoverProcesses(ours=[])
        launcher = _launcher(pid_file=pid_file, leftovers=leftovers)
        assert launcher.start() is True
        assert leftovers.terminated == []

    def test_the_real_check_refuses_a_running_program_that_is_not_our_server(self):
        """The test above can only be as good as the check behind it. This one
        runs the real check against a process that is certainly alive and
        certainly not a model server: the one running this test.
        """
        assert _default_stale_probe(os.getpid()) is Liveness.GONE

    def test_the_recorded_number_is_replaced_by_the_new_one(self, tmp_path):
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(_record(777))
        launcher = _launcher(spawn=Spawner(FakeProcess(pid=99)),
                             pid_file=pid_file, leftovers=LeftoverProcesses())
        launcher.start()
        written = _ServerRecord.parse(pid_file.read_text())
        assert written is not None
        assert written.server_pid == 99

    def test_a_record_that_is_not_a_number_does_not_stop_a_start_on_a_free_port(
        self, tmp_path
    ):
        """Text with no fields at all, rather than the truncated record
        TestARecordWeCannotFullyRead uses. The port decides either way: this
        one is silent, so there is nothing the record could be protecting.
        """
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text("not a number")
        launcher = _launcher(pid_file=pid_file, probe=_healthy_after(1))
        assert launcher.start() is True

    def test_a_record_that_cannot_be_written_stops_the_server_it_named(
        self, tmp_path
    ):
        """A directory where the temporary record should be written stands in
        for every way the record can fail to be published. Only the write is
        blocked: a directory at the record's own path would fail the read
        first, and the start would then stop before ever spawning a server,
        proving nothing about what happens to one already running.

        The start used to go ahead regardless. That left a server running
        which nothing could ever find again: if Wheelhouse then stopped
        without shutting down, it held the port and its several gigabytes
        with no record on disk for any later start to act on. A server that
        cannot be recorded is not a successful start.
        """
        pid_file = tmp_path / "llama-server.pid"
        (tmp_path / "llama-server.pid.new").mkdir()
        process = FakeProcess()

        started = _launcher(
            spawn=Spawner(process), pid_file=pid_file
        ).start()

        assert started is False
        assert process.terminated or process.killed, (
            "a server that could not be recorded was left running"
        )

    def test_stopping_removes_the_record(self, tmp_path):
        """A record left behind after a clean stop sends the next run looking
        for a server that is not there.
        """
        pid_file = tmp_path / "llama-server.pid"
        launcher = _launcher(spawn=Spawner(FakeProcess(pid=11)),
                             pid_file=pid_file)
        launcher.start()
        launcher.stop()
        assert pid_file.exists() is False

    def test_a_start_that_never_becomes_healthy_leaves_no_record(self, tmp_path):
        pid_file = tmp_path / "llama-server.pid"
        ticks = iter([0.0] + [float(t) for t in range(1, 400)])
        launcher = _launcher(
            _config(startup_timeout_seconds=5),
            spawn=Spawner(FakeProcess()),
            probe=_never_healthy,
            clock=lambda: next(ticks),
            pid_file=pid_file,
        )
        assert launcher.start() is False
        assert pid_file.exists() is False

    def test_a_launcher_built_without_a_record_still_starts(self):
        """Nothing in the start path may depend on the file being configured."""
        assert _launcher().start() is True


class TestARecordWeCannotFullyRead:
    """Every "cannot tell" answer used to be read as "gone", and "gone" is the
    one reading that licenses stopping a process and deleting a record
    (wh-local-ai-runtime.2.9, wh-local-ai-runtime.2.10).

    A process can refuse inspection while running perfectly well: it may be
    running as another user, or protected by security software. The record file
    can be unreadable for the same reasons, or half-written by a build that did
    not replace it atomically.

    The port itself settles what inspection could not. A server answering
    /health is a server in use, whoever owns it, so this start stands aside and
    keeps the record. A silent port has nothing on it worth protecting, so the
    start goes ahead rather than refusing for good.
    """

    def _with_record(self, tmp_path, text, probe=None, leftovers=None,
                     owner_alive=None, process=None):
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(text)
        launcher = _launcher(
            spawn=Spawner(process or FakeProcess(pid=4242)),
            probe=probe,
            pid_file=pid_file,
            leftovers=leftovers,
            owner_alive=owner_alive,
        )
        return launcher, pid_file

    def test_a_record_that_cannot_be_read_stops_the_start(self, tmp_path,
                                                          monkeypatch):
        """Unreadable is not absent. The file may name a server that is
        running, and starting a second one on the same port fails to bind --
        after which the first is still there and still unfindable.

        Only the read is made to fail. Putting a directory where the record
        belongs would break the write as well, and the start would then stop
        for the wrong reason and this test would pass without proving
        anything.
        """
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(_record(777))
        readable = Path.read_text

        def refuse_that_one(self, *args, **kwargs):
            if self == pid_file:
                raise PermissionError(13, "denied")
            return readable(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", refuse_that_one)

        assert _launcher(pid_file=pid_file).start() is False
        assert readable(pid_file) == _record(777)

    def test_a_missing_record_lets_the_start_go_ahead(self, tmp_path):
        """The ordinary first run, and the case the reading above must not be
        confused with.
        """
        pid_file = tmp_path / "llama-server.pid"
        assert _launcher(pid_file=pid_file).start() is True

    def test_a_corrupt_record_beside_a_live_port_stops_the_start(self, tmp_path):
        launcher, pid_file = self._with_record(
            tmp_path, "server_pid=\n", probe=lambda port: True,
        )

        assert launcher.start() is False
        assert pid_file.read_text() == "server_pid=\n"

    def test_a_corrupt_record_beside_a_silent_port_is_replaced(self, tmp_path):
        launcher, pid_file = self._with_record(
            tmp_path, "server_pid=\n", probe=_healthy_after(1),
        )

        assert launcher.start() is True
        written = _ServerRecord.parse(pid_file.read_text())
        assert written is not None and written.server_pid == 4242

    def test_a_server_we_cannot_inspect_beside_a_live_port_stops_the_start(
        self, tmp_path
    ):
        """The recorded number may well be the server answering the port. We
        cannot show that it is not, so it is left alone.
        """
        launcher, pid_file = self._with_record(
            tmp_path, _record(777),
            probe=lambda port: True,
            leftovers=_UninspectableProcesses(),
        )

        assert launcher.start() is False
        assert _ServerRecord.parse(pid_file.read_text()).server_pid == 777

    def test_a_server_we_cannot_inspect_beside_a_silent_port_does_not_block(
        self, tmp_path
    ):
        """Refusing here would be permanent: the same inspection fails on every
        later start, and the AI features would never come back.
        """
        launcher, _pid_file = self._with_record(
            tmp_path, _record(777),
            probe=_healthy_after(1),
            leftovers=_UninspectableProcesses(),
        )

        assert launcher.start() is True

    def test_an_owner_we_cannot_inspect_stops_the_start(self, tmp_path):
        """The server is confirmed one of ours; only its owner is in doubt. If
        that owner is alive this is a Wheelhouse using the server right now,
        and terminating it takes the AI away from a live session.
        """
        launcher, pid_file = self._with_record(
            tmp_path, _record(777),
            leftovers=LeftoverProcesses(ours=[777]),
            owner_alive=lambda pid, name: Liveness.UNKNOWN,
        )

        assert launcher.start() is False
        assert _ServerRecord.parse(pid_file.read_text()).server_pid == 777

    def test_a_record_missing_its_owner_name_is_corrupt(self):
        """Not "a server with no owner": that reading belongs to the legacy
        bare-number record alone. A structured record is written whole, so a
        structured record missing a field was interrupted, and reading its
        owner as gone would license stopping a live server.
        """
        assert _ServerRecord.parse("server_pid=777\nowner_pid=42\n") is None

    def test_a_record_missing_its_owner_id_is_corrupt(self):
        assert _ServerRecord.parse(
            "server_pid=777\nowner_name=python.exe\n"
        ) is None

    def test_a_record_whose_owner_name_is_blank_is_corrupt(self):
        """Distinct from the field being absent: this is what a write cut off
        part-way through the last line leaves behind. Read as a name, the
        blank is the empty owner the legacy path treats as abandoned.
        """
        assert _ServerRecord.parse(
            "server_pid=777\nowner_pid=42\nowner_name=\n"
        ) is None

    def test_a_record_holding_only_a_number_is_still_read(self):
        """The one record that legitimately names no owner."""
        record = _ServerRecord.parse("777")
        assert record is not None
        assert record.server_pid == 777
        assert record.owner_name == ""

    def test_the_real_server_check_does_not_call_a_protected_process_gone(
        self, monkeypatch
    ):
        def refuse(pid):
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr(psutil, "Process", refuse)

        assert _default_stale_probe(os.getpid()) is Liveness.UNKNOWN

    def test_the_real_owner_check_does_not_call_a_protected_process_gone(
        self, monkeypatch
    ):
        def refuse(pid):
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr(psutil, "Process", refuse)

        assert _default_owner_alive(os.getpid(), "python.exe") is Liveness.UNKNOWN


class _UninspectableProcesses(LeftoverProcesses):
    """A recorded server that refuses to say what it is."""

    def probe(self, pid):
        return Liveness.UNKNOWN


class TestAnOldServerThatWillNotStop:
    """Terminating can fail: the process may be protected, or wedged so that
    neither terminate nor kill takes effect.

    The record is the only thing that can find that server. Deleting it and
    starting a replacement makes the failure permanent -- the old server keeps
    the port and its several gigabytes of memory for as long as the machine
    stays on, and no later start can see it, because the number was thrown
    away. So a termination that is not confirmed keeps the record and stops
    the start.
    """

    def _blocked(self, tmp_path, spawner=None):
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(_record(777))
        leftovers = LeftoverProcesses(ours=[777], unstoppable=[777])
        launcher = _launcher(spawn=spawner or Spawner(), pid_file=pid_file,
                             leftovers=leftovers)
        return launcher, pid_file, leftovers

    def test_a_termination_that_failed_keeps_the_record(self, tmp_path):
        launcher, pid_file, _ = self._blocked(tmp_path)
        launcher.start()
        written = _ServerRecord.parse(pid_file.read_text())
        assert written is not None
        assert written.server_pid == 777

    def test_no_replacement_is_started_over_a_server_that_would_not_stop(
        self, tmp_path
    ):
        """It still holds the port, so a replacement could not bind it. The
        one it would achieve is overwriting the record with its own number.
        """
        spawner = Spawner()
        launcher, _, _ = self._blocked(tmp_path, spawner)
        assert launcher.start() is False
        assert spawner.commands == []

    def test_the_termination_was_at_least_attempted(self, tmp_path):
        launcher, _, leftovers = self._blocked(tmp_path)
        launcher.start()
        assert leftovers.terminated == [777]

    def test_a_later_start_can_try_the_same_server_again(self, tmp_path):
        """The point of keeping the record. The obstacle may be temporary --
        antivirus holding the process, a transient rights problem.
        """
        launcher, pid_file, leftovers = self._blocked(tmp_path)
        assert launcher.start() is False

        leftovers.unstoppable.clear()
        second = _launcher(spawn=Spawner(FakeProcess(pid=55)),
                           pid_file=pid_file, leftovers=leftovers)
        assert second.start() is True
        assert leftovers.terminated == [777, 777]
        written = _ServerRecord.parse(pid_file.read_text())
        assert written is not None
        assert written.server_pid == 55

    def test_the_real_termination_reports_a_process_that_is_already_gone(self):
        """A server that ended between the check and the terminate is the
        outcome we wanted, so it counts as success, not as a failure that
        would block the start forever.
        """
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=30)

        assert _default_stale_terminate(process.pid) is True

    def test_the_real_termination_confirms_a_process_it_stopped(self):
        """Run against a real process, so the True is earned rather than
        assumed. A long sleep stands in for a wedged model server.
        """
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            assert _default_stale_terminate(process.pid) is True
            assert process.poll() is not None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    def test_the_real_termination_reports_one_it_could_not_stop(self, monkeypatch):
        """A process we are not allowed to end, or one antivirus is holding.

        This is the branch the whole finding is about, and it is the one the
        tests above cannot reach: both of them stop their process. Saying
        True here would delete the record, and the record is the only thing
        that can find that server again -- it would hold the port and its
        several gigabytes for as long as the machine stays on.
        """
        asked = []

        class Protected:
            def __init__(self, pid):
                asked.append(pid)

            def terminate(self):
                raise psutil.AccessDenied(asked[-1])

        monkeypatch.setattr(psutil, "Process", Protected)

        assert _default_stale_terminate(4321) is False
        # The pid it was given, not one it made up: a function that looked at
        # some other process would satisfy the False above for free.
        assert asked == [4321]


class TestTwoWheelhousesAtOnce:
    """Wheelhouse does not enforce a single instance: launcher.py reads its own
    process-id file and does nothing with the number. Two of them starting at
    once is therefore a real sequence of events.

    The record on its own cannot tell an abandoned server from one another
    Wheelhouse started a moment ago -- both are "running" -- so it records the
    owner too.
    """

    def test_a_server_a_running_wheelhouse_owns_is_not_stopped(self, tmp_path):
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(_record(777, owner_pid=4242, owner_name="python.exe"))
        leftovers = LeftoverProcesses(ours=[777])

        launcher = _launcher(pid_file=pid_file, leftovers=leftovers,
                             owner_alive=_owner_still_running)
        launcher.start()

        assert leftovers.terminated == []

    def test_no_second_server_is_started_beside_the_first(self, tmp_path):
        """One port, one server. Starting a second one that cannot bind
        achieves nothing and overwrites the first one's record.
        """
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(_record(777, owner_pid=4242, owner_name="python.exe"))
        spawner = Spawner()

        launcher = _launcher(spawn=spawner, pid_file=pid_file,
                             leftovers=LeftoverProcesses(ours=[777]),
                             owner_alive=_owner_still_running)

        assert launcher.start() is False
        assert spawner.commands == []

    def test_the_running_wheelhouses_record_is_left_intact(self, tmp_path):
        """The one that gives up must not take the record with it. Without it
        the other Wheelhouse's server cannot be found again, and it becomes
        the next orphan.
        """
        pid_file = tmp_path / "llama-server.pid"
        theirs = _record(777, owner_pid=4242, owner_name="python.exe")
        pid_file.write_text(theirs)

        launcher = _launcher(pid_file=pid_file,
                             leftovers=LeftoverProcesses(ours=[777]),
                             owner_alive=_owner_still_running)
        launcher.start()

        assert pid_file.read_text() == theirs

    def test_stopping_does_not_remove_a_record_we_did_not_write(self, tmp_path):
        """A launcher that never started anything owns no record. Deleting
        whatever it finds would erase another Wheelhouse's server.
        """
        pid_file = tmp_path / "llama-server.pid"
        theirs = _record(777, owner_pid=4242, owner_name="python.exe")
        pid_file.write_text(theirs)

        _launcher(pid_file=pid_file).stop()

        assert pid_file.read_text() == theirs

    def test_a_failed_start_does_not_remove_the_record_written_after_it(
        self, tmp_path
    ):
        """The interleaving that loses a healthy server.

        This launcher records its server, then its own start fails and it
        cleans up. In between, another Wheelhouse has replaced the record
        with one for the server it just started. Removing the file by path
        rather than by contents would erase that.
        """
        pid_file = tmp_path / "llama-server.pid"
        theirs = _record(999, owner_pid=4242, owner_name="python.exe")
        ticks = iter([0.0] + [float(t) for t in range(1, 400)])

        def probe_then_they_write(port):
            pid_file.write_text(theirs)
            return False

        launcher = _launcher(
            _config(startup_timeout_seconds=5),
            spawn=Spawner(FakeProcess(pid=11)),
            probe=probe_then_they_write,
            clock=lambda: next(ticks),
            pid_file=pid_file,
        )

        assert launcher.start() is False
        assert pid_file.read_text() == theirs

    def test_the_real_owner_check_refuses_a_number_that_is_not_that_program(self):
        """Windows issues process ids again after a process ends. A recorded
        number may now belong to something else, and reading that as "the
        owner is alive" would make an orphan look owned forever.
        """
        assert (
            _default_owner_alive(os.getpid(), "definitely-not-us.exe")
            is Liveness.GONE
        )

    def test_the_real_owner_check_accepts_this_running_process(self):
        assert (
            _default_owner_alive(os.getpid(), _own_process_name())
            is Liveness.ALIVE
        )

    def test_a_record_with_no_owner_is_treated_as_abandoned(self):
        """What an upgrade from an earlier build leaves behind. Treating it as
        owned would leave its server running with nothing able to stop it.
        """
        assert _default_owner_alive(0, "") is Liveness.GONE

    def test_our_own_name_falls_back_to_the_program_we_are_running(
        self, monkeypatch
    ):
        """psutil can refuse to describe even this process, and the name it
        would have given is what the next start compares against. Returning
        nothing writes a record with no owner, which the previous test shows
        is read as abandoned -- so the fallback protects a live server.
        """
        def refuse(pid):
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr(psutil, "Process", refuse)

        assert _own_process_name() == os.path.basename(sys.executable)

    def test_a_server_we_cannot_name_ourselves_the_owner_of_is_stopped(
        self, tmp_path, monkeypatch
    ):
        """The fallback above cannot fail, so this is the belt-and-braces case.
        Recording a server with no owner tells the next start it is abandoned
        and invites it to stop a server in active use; writing no record at
        all leaves five gigabytes on the port that nothing can find again.
        Neither is acceptable, so the server is stopped and the start fails.
        """
        pid_file = tmp_path / "llama-server.pid"
        process = FakeProcess(pid=4242)
        monkeypatch.setattr(server_launcher, "_own_process_name", lambda: "")

        launcher = _launcher(spawn=Spawner(process), pid_file=pid_file)

        assert launcher.start() is False
        assert process.terminated or process.killed
        assert not pid_file.exists()


class TestOnlyOneStartAtATime:
    """Clearing an old server, starting the new one and recording it are one
    step. Two starts interleaved inside it can both find no record and both
    start a server; whichever records itself second erases the other's record,
    and that server becomes the next orphan.
    """

    def _lock_watching(self, events):
        @contextlib.contextmanager
        def lock(port):
            events.append(f"took the lock for port {port}")
            yield True
            events.append("released the lock")
        return lock

    def test_the_lock_is_held_while_the_server_is_started_and_recorded(
        self, tmp_path
    ):
        events = []
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(_record(777))

        class Recording(LeftoverProcesses):
            def terminate(self, pid):
                events.append("stopped the old server")
                return super().terminate(pid)

        def spawn(command):
            events.append("started the new server")
            return FakeProcess(pid=11)

        launcher = _launcher(spawn=spawn, pid_file=pid_file,
                             leftovers=Recording(ours=[777]),
                             start_lock=self._lock_watching(events))
        launcher.start()

        assert events == [
            "took the lock for port 8899",
            "stopped the old server",
            "started the new server",
            "released the lock",
        ]

    def test_the_lock_is_released_before_the_wait_for_the_model_to_load(
        self, tmp_path
    ):
        """That wait is as long as the model takes to load. Holding the lock
        across it would make a second Wheelhouse wait the same time for a step
        that reads nothing the first one is writing.
        """
        events = []
        asks = {"n": 0}

        def probe(port):
            asks["n"] += 1
            if asks["n"] == 1:
                # The first ask is the check for something else already on the
                # port, which does belong inside the lock. Only the wait for
                # the model to load is what this test times.
                return False
            events.append("asked whether the server is ready")
            return True

        launcher = _launcher(probe=probe, pid_file=tmp_path / "x.pid",
                             start_lock=self._lock_watching(events))
        launcher.start()

        assert events.index("released the lock") < events.index(
            "asked whether the server is ready"
        )

    def test_the_lock_is_released_when_the_start_fails(self, tmp_path):
        """Otherwise the first failure stops every later start from running."""
        events = []

        def spawn(command):
            raise OSError("no such file")

        launcher = _launcher(spawn=spawn, start_lock=self._lock_watching(events))

        assert launcher.start() is False
        assert events[-1] == "released the lock"

    def test_the_lock_is_released_when_the_start_gives_way_to_another(
        self, tmp_path
    ):
        """The path that returns early because another Wheelhouse owns the
        server leaves the block through a different exit than the others.
        """
        events = []
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(_record(777, owner_pid=4242, owner_name="python.exe"))

        launcher = _launcher(pid_file=pid_file,
                             leftovers=LeftoverProcesses(ours=[777]),
                             owner_alive=_owner_still_running,
                             start_lock=self._lock_watching(events))

        assert launcher.start() is False
        assert events[-1] == "released the lock"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
    def test_the_real_lock_keeps_a_second_holder_out(self):
        """Run against the real Windows mutex rather than a stand-in, so the
        name, the wait and the release are all exercised.
        """
        from ai.server_launcher import _default_start_lock

        with _default_start_lock(65123):
            import threading
            got_in = threading.Event()

            def second_holder():
                with _default_start_lock(65123):
                    got_in.set()

            thread = threading.Thread(target=second_holder, daemon=True)
            thread.start()
            # A mutex is owned by a thread, not a process, so a second thread
            # in this process is a faithful stand-in for a second Wheelhouse.
            assert got_in.wait(timeout=1.0) is False

        thread.join(timeout=10)
        assert got_in.is_set() is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
    def test_the_real_lock_does_not_make_two_ports_wait_for_each_other(self):
        from ai.server_launcher import _default_start_lock

        with _default_start_lock(65123):
            with _default_start_lock(65124):
                pass


class TestTheServerDiesWithUs:
    """Starting the server in its own process group keeps Ctrl+C away from it.
    It does NOT make Windows stop the server when Wheelhouse stops. A job
    object set to end its processes when it closes does, and Windows closes it
    for us however the Logic process exits, including a crash.
    """

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
    def test_closing_the_job_stops_the_child(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            job = _kill_child_when_we_exit(process)
            assert job is not None, "the child was never put in a job object"
            # Closing the last handle is exactly what Windows does when this
            # process exits, however it exits.
            job.close()
            assert process.wait(timeout=10) is not None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
    def test_the_job_handle_outlives_the_call_that_made_it(self):
        """A handle nothing keeps is collected, which closes the job, which
        ends the server we just started. Holding it is what makes the whole
        mechanism work rather than being an immediate self-inflicted failure.
        """
        process = _default_spawn(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        try:
            time.sleep(0.5)
            assert process.poll() is None, (
                "the server ended as soon as it started; the job handle was "
                "not kept alive"
            )
        finally:
            process.kill()
            process.wait(timeout=10)


class TestShuttingDownWhileAStartIsRunning:
    """Shutdown can land in the middle of a start, and cancelling the wait for
    that start does not stop it.

    A model server can now be started again mid-session, when a readiness
    check finds the old one has died. That start runs on a worker thread,
    because loading a multi-gigabyte model on the event loop would freeze
    speech recognition and every other service in the Logic process.
    Shutdown cancels the readiness loop that is awaiting it -- and a cancelled
    await on a worker thread returns at once while the thread carries on
    (verified: the await raises CancelledError immediately and the thread
    finishes afterwards).

    So `stop()` can run, find nothing recorded, and return, after which the
    thread spawns a server that nothing will ever stop. The job object is the
    backstop for that, but it is best-effort by design and logs a warning when
    it cannot be created, so a start must also know that shutdown has begun.
    """

    def _launcher_and_spawn(self, tmp_path, on_spawn=None):
        pid_file = tmp_path / "llama-server.pid"
        process = FakeProcess(pid=4242)

        class WatchfulSpawner(Spawner):
            def __call__(self, command):
                if on_spawn is not None:
                    on_spawn()
                return super().__call__(command)

        spawn = WatchfulSpawner(process)
        launcher = _launcher(spawn=spawn, pid_file=pid_file)
        return launcher, spawn, process, pid_file

    def test_a_start_that_lands_after_shutdown_stops_what_it_started(self, tmp_path):
        """The window the whole class is about: shutdown ran while this start
        was between deciding to spawn and having something to stop.
        """
        holder = {}

        def shut_down_mid_start():
            holder["launcher"].shutdown()

        launcher, spawn, process, pid_file = self._launcher_and_spawn(
            tmp_path, on_spawn=shut_down_mid_start
        )
        holder["launcher"] = launcher

        assert launcher.start() is False
        assert process.terminated is True

    def test_no_record_is_left_naming_a_server_we_stopped(self, tmp_path):
        """A record outliving its server sends the next start after a process
        id that by then belongs to something else.
        """
        holder = {}

        def shut_down_mid_start():
            holder["launcher"].shutdown()

        launcher, _spawn, _process, pid_file = self._launcher_and_spawn(
            tmp_path, on_spawn=shut_down_mid_start
        )
        holder["launcher"] = launcher

        launcher.start()

        assert not pid_file.exists()

    def test_a_shutdown_while_the_record_is_being_written_leaves_none(
        self, tmp_path, monkeypatch
    ):
        """The narrower window inside the write itself (wh-local-ai-runtime.2.8).

        A shutdown landing before the write reads self._process finds nothing
        to record, so no record is made. A shutdown landing after the record is
        on disk is the ordinary path and removes it. Between those two is the
        moment the record is being moved into place: the stop has already run
        and found no record text to remove, and the move then publishes one
        naming a server that has just been stopped.
        """
        pid_file = tmp_path / "llama-server.pid"
        process = FakeProcess(pid=4242)
        launcher = _launcher(spawn=Spawner(process), pid_file=pid_file)

        real_replace = os.replace

        def shut_down_then_replace(source, destination):
            launcher.shutdown()
            return real_replace(source, destination)

        monkeypatch.setattr(os, "replace", shut_down_then_replace)

        assert launcher.start() is False
        assert process.terminated is True
        assert not pid_file.exists(), "a record was left naming a stopped server"

    def test_the_flag_is_set_before_anything_is_stopped(self, tmp_path):
        """The flag is how a start on another thread learns shutdown began.

        Set after the stop, there is a window where the stop has already
        looked for a server and found none, and the flag is not yet there for
        the start to read -- which is precisely the interleaving this whole
        mechanism exists to close.
        """
        launcher, _spawn, _process, _pid_file = self._launcher_and_spawn(tmp_path)
        seen = []
        launcher.stop = lambda: seen.append(launcher._shutdown)

        launcher.shutdown()

        assert seen == [True]

    def test_a_start_asked_for_after_shutdown_does_nothing(self, tmp_path):
        """The readiness loop is cancelled at shutdown, but a restart already
        queued behind it must not spawn a server on the way out.
        """
        launcher, spawn, _process, _pid_file = self._launcher_and_spawn(tmp_path)
        launcher.shutdown()

        assert launcher.start() is False
        assert spawn.commands == []

    def test_the_wait_for_health_gives_up_when_shutdown_begins(self, tmp_path):
        """Otherwise the thread spends the whole startup timeout -- ninety
        seconds by default -- asking a server that has been stopped whether it
        is ready yet.
        """
        probes = {"n": 0}
        holder = {}

        def probe(port):
            probes["n"] += 1
            if probes["n"] == 1:
                # The check for something else already on the port, before the
                # spawn. Answering False lets the start proceed to the wait
                # this test is about.
                return False
            if probes["n"] == 2:
                holder["launcher"].shutdown()
            return False

        pid_file = tmp_path / "llama-server.pid"
        launcher = _launcher(spawn=Spawner(FakeProcess(pid=4242)), probe=probe,
                             pid_file=pid_file)
        holder["launcher"] = launcher

        assert launcher.start() is False
        # One readiness probe, not the hundreds the timeout would allow.
        assert probes["n"] == 2

    def test_a_start_that_never_became_healthy_can_still_be_retried(self, tmp_path):
        """start() stops the server it gave up waiting for, and that stop must
        not be read as shutdown: the next readiness check is entitled to try
        again, and a launcher poisoned by its own timeout would never start a
        server for the rest of the session.
        """
        pid_file = tmp_path / "llama-server.pid"
        ticks = iter([0.0] + [float(t) for t in range(1, 400)])
        launcher = _launcher(_config(startup_timeout_seconds=10),
                             spawn=Spawner(FakeProcess(pid=4242)),
                             probe=_never_healthy, clock=lambda: next(ticks),
                             pid_file=pid_file)

        assert launcher.start() is False

        second = Spawner(FakeProcess(pid=4343))
        launcher._spawn = second
        # The first server was stopped, so the port is free again until the
        # second one takes it. Answering True straight away would instead say
        # something else is there, and the retry would rightly refuse.
        launcher._probe = _healthy_after(1)

        assert launcher.start() is True
        assert len(second.commands) == 1


class TestAPortSomethingElseIsAlreadyOn:
    """The record can only ever describe a server Wheelhouse started.

    A llama-server the user started by hand, a server whose record was lost,
    or any other program on the configured port answers the health probe just
    the same -- and that probe is what decides whether our own start
    succeeded. So a start that skips the port check spawns a child that cannot
    bind, watches the incumbent answer for it, reports success, and publishes
    a record naming a process that is about to exit (wh-local-ai-runtime.2.13).

    The corrupt-record and cannot-inspect cases already consult the port. The
    two that did not are the ones below: no record at all, and a record whose
    server is confirmed gone.
    """

    def test_no_record_and_a_server_already_answering_stops_the_start(
        self, tmp_path
    ):
        pid_file = tmp_path / "llama-server.pid"
        spawn = Spawner(FakeProcess(pid=4242))
        launcher = _launcher(spawn=spawn, probe=lambda port: True,
                             pid_file=pid_file)

        assert launcher.start() is False
        assert spawn.commands == [], "a second server was started on a taken port"
        assert not pid_file.exists(), (
            "a record was published naming a server that cannot bind"
        )

    def test_no_record_and_a_silent_port_starts_normally(self, tmp_path):
        """The other half. The ordinary first start must not be refused by
        the check that catches the occupied port.
        """
        pid_file = tmp_path / "llama-server.pid"
        spawn = Spawner(FakeProcess(pid=4242))
        launcher = _launcher(spawn=spawn, probe=_healthy_after(1),
                             pid_file=pid_file)

        assert launcher.start() is True
        assert len(spawn.commands) == 1
        assert pid_file.exists()

    @pytest.mark.parametrize(
        "record_text",
        [
            pytest.param(_record(4242), id="structured record"),
            pytest.param("4242", id="legacy bare number"),
        ],
    )
    def test_a_record_whose_server_is_gone_beside_a_live_port_stops_the_start(
        self, tmp_path, record_text
    ):
        """The recorded number really is dead, so the record is worthless --
        but something else is on the port, and that is a separate question the
        old code never asked.
        """
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(record_text, encoding="utf-8")
        spawn = Spawner(FakeProcess(pid=5555))
        launcher = _launcher(spawn=spawn, probe=lambda port: True,
                             pid_file=pid_file, leftovers=LeftoverProcesses())

        assert launcher.start() is False
        assert spawn.commands == [], "a second server was started on a taken port"

    def test_a_record_whose_server_is_gone_beside_a_silent_port_starts(
        self, tmp_path
    ):
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(_record(4242), encoding="utf-8")
        spawn = Spawner(FakeProcess(pid=5555))
        launcher = _launcher(spawn=spawn, probe=_healthy_after(1),
                             pid_file=pid_file, leftovers=LeftoverProcesses())

        assert launcher.start() is True
        assert len(spawn.commands) == 1


class StubbornProcess(UnstoppableProcess):
    """Refuses the first termination and accepts the second."""

    def terminate(self):
        super().terminate()
        if self.attempts >= 2:
            self._exits_with = 0


class TestStoppingAServerTwice:
    """A stop that could not end the server must leave a second try possible.

    stop() dropped its process handle before it knew whether the server had
    exited, so one failed terminate made the failure permanent: every later
    stop found nothing to stop, shutdown ended with the server still running,
    and a mid-session start read the retained record and declined because the
    owner it named -- this very process -- was still alive
    (wh-local-ai-runtime.2.15).
    """

    def test_a_second_stop_tries_again(self, tmp_path):
        pid_file = tmp_path / "llama-server.pid"
        process = StubbornProcess(pid=4242)
        launcher = _launcher(spawn=Spawner(process), pid_file=pid_file)

        assert launcher.start() is True
        launcher.stop()
        assert process.attempts == 1
        assert pid_file.exists(), "the first stop lost the record too"

        launcher.stop()

        assert process.attempts == 2, "the second stop did not try again"
        assert not pid_file.exists(), (
            "the server stopped but its record was left behind"
        )

    def test_shutdown_after_a_failed_stop_tries_again(self, tmp_path):
        """The case that matters most: the last chance to end the server."""
        pid_file = tmp_path / "llama-server.pid"
        process = StubbornProcess(pid=4242)
        launcher = _launcher(spawn=Spawner(process), pid_file=pid_file)

        launcher.start()
        launcher.stop()
        launcher.shutdown()

        assert process.attempts == 2
        assert process.poll() is not None, "Wheelhouse closed with the server up"

    def test_a_server_we_failed_to_stop_is_not_reported_as_running(
        self, tmp_path
    ):
        """Retaining the handle must not turn into is_running saying yes.

        The readiness recovery stops the old server and starts a new one; a
        launcher that answered "already running" there would silently do
        nothing and report success.
        """
        pid_file = tmp_path / "llama-server.pid"
        launcher = _launcher(spawn=Spawner(UnstoppableProcess(pid=4242)),
                             pid_file=pid_file)

        launcher.start()
        launcher.stop()

        assert launcher.is_running is False

    def test_our_own_record_does_not_read_as_another_wheelhouse(self, tmp_path):
        """The second half of the same defect.

        After a failed stop the record is still ours, naming this process as
        the owner. The owner check finds that process alive -- it is us -- and
        the start declined, so one failed stop refused every start for the
        rest of the session.
        """
        pid_file = tmp_path / "llama-server.pid"
        leftovers = LeftoverProcesses(ours=(4242,))
        launcher = _launcher(
            # Stubborn, not unstoppable: a server this launcher still holds
            # and cannot end refuses the start on its own account, which is a
            # different rule and has its own tests. This one is about the
            # record that the failed stop leaves behind.
            spawn=Spawner(StubbornProcess(pid=4242)),
            pid_file=pid_file,
            leftovers=leftovers,
            owner_alive=_owner_still_running,
        )
        launcher.start()
        launcher.stop()

        second = Spawner(FakeProcess(pid=5555))
        launcher._spawn = second

        assert launcher.start() is True, (
            "this session's own record refused this session a start"
        )
        assert leftovers.terminated == [4242], (
            "the old server was left running beside the new one"
        )


class TestASpawnThatCannotBeRecorded:
    """A server that cannot be recorded and cannot be stopped is the exact
    orphan this whole file exists to prevent (wh-local-ai-runtime.2.16).

    Publication happens after the spawn, so a directory that cannot be created
    is discovered with a live child already running. start() then stops it --
    and when that stop cannot confirm the exit there is no record text to
    keep, so nothing on disk names the child at all.
    """

    def test_a_record_location_that_cannot_be_made_stops_before_spawning(
        self, tmp_path
    ):
        """The cheapest fix for most of the class: find out before spawning."""
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("", encoding="utf-8")
        spawn = Spawner(FakeProcess(pid=4242))
        launcher = _launcher(spawn=spawn,
                             pid_file=blocked / "llama-server.pid")

        assert launcher.start() is False
        assert spawn.commands == [], (
            "a server was started that could never have been recorded"
        )

    def test_a_publication_failure_beside_an_unstoppable_server_records_it(
        self, tmp_path, monkeypatch
    ):
        """The residue: the move into place fails after the spawn, and the
        child will not stop. Something must still name it, or the port and
        several gigabytes are held until the machine restarts.
        """
        pid_file = tmp_path / "llama-server.pid"
        process = UnstoppableProcess(pid=4242)
        launcher = _launcher(spawn=Spawner(process), pid_file=pid_file)

        calls = {"n": 0}
        real_replace = os.replace

        def fail_the_first_replace(source, destination):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("the record could not be moved into place")
            return real_replace(source, destination)

        monkeypatch.setattr(os, "replace", fail_the_first_replace)

        assert launcher.start() is False

        assert pid_file.exists(), (
            "a live server was left that nothing on disk names"
        )
        record = _ServerRecord.parse(pid_file.read_text(encoding="utf-8"))
        assert record is not None and record.server_pid == 4242


class TestRemovingARecordWhileAnotherStartPublishesOne:
    """Compare-and-delete is a read and an unlink, not one operation
    (wh-local-ai-runtime.2.14).

    Another Wheelhouse can replace the file between the two, after which this
    one deletes a record naming a server that is running. That is the
    unfindable orphan the comparison was added to prevent, so the deletion
    has to be exclusive with publication, not merely careful.
    """

    def test_the_deletion_is_held_under_the_start_lock(self, tmp_path):
        """Exclusion is what makes the comparison mean anything. The lock the
        starts already take is the one that has to cover the deletion.
        """
        pid_file = tmp_path / "llama-server.pid"
        held = []

        @contextlib.contextmanager
        def recording_lock(port):
            held.append("in")
            try:
                yield True
            finally:
                held.append("out")

        launcher = _launcher(spawn=Spawner(FakeProcess(pid=4242)),
                             pid_file=pid_file, start_lock=recording_lock)
        launcher.start()
        held.clear()

        launcher.stop()

        assert held == ["in", "out"], (
            "the record was deleted outside the lock that serializes starts"
        )
        assert not pid_file.exists()

    def test_a_lock_that_could_not_be_taken_leaves_the_record_alone(
        self, tmp_path
    ):
        """Without exclusion the comparison proves nothing, and deleting is
        the irreversible direction. Keeping a record that names a stopped
        server costs the next start one inspection; deleting one that names a
        running server costs the user the port until they restart.
        """
        pid_file = tmp_path / "llama-server.pid"

        @contextlib.contextmanager
        def lock_that_fails(port):
            yield False

        launcher = _launcher(spawn=Spawner(FakeProcess(pid=4242)),
                             pid_file=pid_file, start_lock=lock_that_fails)
        launcher.start()

        launcher.stop()

        assert pid_file.exists(), (
            "the record was deleted without holding the lock"
        )

    def test_a_start_that_cannot_take_the_lock_still_clears_a_dead_record(
        self, tmp_path
    ):
        """Failing closed on deletion must not reach the start path.

        A start that refused to clear a record naming a confirmed-dead server
        whenever the lock was unavailable would leave the AI features off for
        a reason that has nothing to do with any server.
        """
        pid_file = tmp_path / "llama-server.pid"
        pid_file.write_text(_record(4242), encoding="utf-8")

        @contextlib.contextmanager
        def lock_that_fails(port):
            yield False

        launcher = _launcher(spawn=Spawner(FakeProcess(pid=5555)),
                             probe=_healthy_after(1), pid_file=pid_file,
                             leftovers=LeftoverProcesses(),
                             start_lock=lock_that_fails)

        assert launcher.start() is True
        record = _ServerRecord.parse(pid_file.read_text(encoding="utf-8"))
        assert record is not None and record.server_pid == 5555


class TestAStartPastAServerWeCouldNotStop:
    """A handle we still hold outranks anything the record file says
    (wh-local-ai-runtime.2.18).

    The record is hearsay: it describes a process by number and can be absent,
    stale or wrong. A retained handle is this launcher's own knowledge that it
    started a server and could not confirm the server stopped. A start that
    consulted only the record could spawn a second server past the first, and
    the next stop -- which reads _process before _unstopped -- would then throw
    the first handle away, leaving a live server with neither a handle nor a
    record naming it.
    """

    def test_a_start_stops_the_retained_server_before_starting_another(
        self, tmp_path
    ):
        """The ordinary recovery. The retry works, so the start goes ahead."""
        pid_file = tmp_path / "llama-server.pid"
        first = StubbornProcess(pid=4242)
        launcher = _launcher(spawn=Spawner(first), pid_file=pid_file)

        launcher.start()
        launcher.stop()
        assert first.attempts == 1

        second = Spawner(FakeProcess(pid=5555))
        launcher._spawn = second

        assert launcher.start() is True
        assert first.attempts == 2, (
            "the server we could not stop was never tried again"
        )
        assert len(second.commands) == 1

    def test_a_server_that_still_will_not_stop_refuses_the_start(
        self, tmp_path, monkeypatch
    ):
        """The case that can leave an orphan nothing names.

        Both attempts to publish a record fail, so nothing on disk describes
        the first server at all. A start that asked only the record would find
        none, see a silent port, and spawn a second one.
        """
        pid_file = tmp_path / "llama-server.pid"
        first = UnstoppableProcess(pid=4242)
        launcher = _launcher(spawn=Spawner(first), pid_file=pid_file,
                             probe=lambda port: False)

        def always_fail(source, destination):
            raise OSError("the record could not be moved into place")

        monkeypatch.setattr(os, "replace", always_fail)

        assert launcher.start() is False
        assert not pid_file.exists(), "the fixture did not produce its premise"
        assert first.attempts == 1

        second = Spawner(FakeProcess(pid=5555))
        launcher._spawn = second

        assert launcher.start() is False
        assert second.commands == [], (
            "a second server was started beside one we could not stop"
        )
        assert first.attempts == 2

        launcher.shutdown()
        assert first.attempts == 3, (
            "the last chance to end the first server was spent on the second"
        )

    def test_a_retained_handle_outranks_a_record_the_table_would_pass(
        self, tmp_path
    ):
        """A record naming a process that has gone, beside a silent port, is
        the plainest state the record table lets a start proceed on. The
        handle has to be resolved first even so, because the table is
        answering about a different process from the one we are holding.
        """
        pid_file = tmp_path / "llama-server.pid"
        first = UnstoppableProcess(pid=4242)
        answers = iter([False, True])

        def probe(port):
            # Free, then ready for the first start; silent from then on, as a
            # server that never bound would leave it.
            return next(answers, False)

        # An advancing clock, so that a start which wrongly went ahead gives
        # up on the readiness wait instead of asking a silent port forever.
        ticks = iter([0.0] + [float(t) for t in range(1, 400)])
        launcher = _launcher(_config(startup_timeout_seconds=10),
                             spawn=Spawner(first), pid_file=pid_file,
                             probe=probe, clock=lambda: next(ticks))
        assert launcher.start() is True
        launcher.stop()

        # 7777 is not one of ours, so the recorded server reads as gone.
        pid_file.write_text(_record(7777), encoding="utf-8")
        second = Spawner(FakeProcess(pid=5555))
        launcher._spawn = second

        assert launcher.start() is False
        assert second.commands == [], (
            "the record table was allowed to answer for a server we hold"
        )

    def test_the_retry_is_not_announced_again_once_it_has_worked(
        self, tmp_path, caplog
    ):
        """Letting go of the handle is what stops the warning repeating.

        Every later start behaves the same either way: the process is gone, so
        asking it to stop again does nothing and the start carries on to its
        other checks. This warning is the only place the difference shows, and
        it is worth showing. Someone reading the log during a real incident
        would otherwise find every start still announcing a server that could
        not be stopped, long after one was.
        """
        first = StubbornProcess(pid=4242)
        answers = iter([False, True])

        def probe(port):
            # Free, then ready, for the first start. Occupied from then on, so
            # the starts below refuse at the port check, before they reach the
            # spawn. That matters: a start that spawns and then fails its
            # readiness wait calls stop(), and stop() clears the handle -- so
            # the difference this test looks for would be undone by the very
            # start meant to show it. A record location is passed for the same
            # reason, since the port is not asked about without one.
            return next(answers, True)

        launcher = _launcher(spawn=Spawner(first), probe=probe,
                             pid_file=tmp_path / "llama-server.pid")
        assert launcher.start() is True
        launcher.stop()

        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                assert launcher.start() is False

        retries = [r for r in caplog.records
                   if "could not be stopped" in r.getMessage()]
        assert len(retries) == 1, (
            f"announced on {len(retries)} starts, not on the one that retried"
        )


class SequenceSpawner:
    """Hands out a different process for each start, in order."""

    def __init__(self, *processes):
        self._processes = list(processes)
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        return self._processes[len(self.commands) - 1]


class TestReplacingAServerThatStoppedAnswering:
    """A server can stop answering while its process is still alive
    (wh-local-ai-runtime.2.22).

    start() reports success the moment it sees the process it started has not
    exited, without asking whether that process answers. For the caller that
    only wants a server to exist, that is right and cheap. For the readiness
    supervisor it is useless: it asks for this only after watching the server
    fail to answer, so a reply of True leaves the same wedged server in place,
    resets the wait to a minute, and produces the same failed probe for as
    long as Wheelhouse runs. A llama server that deadlocks, or whose health
    thread dies while the process stays up, is never replaced.
    """

    def test_the_live_process_is_ended_and_another_started(self, tmp_path):
        first = FakeProcess(pid=4242)
        second = FakeProcess(pid=4343)
        spawn = SequenceSpawner(first, second)
        launcher = _launcher(spawn=spawn, pid_file=tmp_path / "llama-server.pid")

        assert launcher.start() is True

        assert launcher.restart() is True
        assert first.terminated, "the wedged server was left running"
        assert len(spawn.commands) == 2, (
            f"asked for {len(spawn.commands)} servers, not a replacement"
        )
        assert launcher._process is second

    def test_a_server_that_will_not_stop_is_not_joined_by_a_second(
        self, tmp_path
    ):
        """The retained-handle refusal has to survive the replacement.

        Spawning a second server while the first is still holding the port is
        the failure the retained handle exists to prevent: two of ours running
        with one record between them, and the one the record does not name
        cannot be found again by anything.
        """
        first = UnstoppableProcess(pid=4242)
        second = FakeProcess(pid=4343)
        spawn = SequenceSpawner(first, second)
        launcher = _launcher(spawn=spawn, pid_file=tmp_path / "llama-server.pid")

        assert launcher.start() is True

        assert launcher.restart() is False
        assert len(spawn.commands) == 1, "a second server was started anyway"

    def test_it_starts_one_when_none_is_running(self, tmp_path):
        """The supervisor uses this for both cases, because it cannot tell
        them apart: a process that exited and one that is alive but wedged
        look the same from a failed probe.
        """
        launcher = _launcher(pid_file=tmp_path / "llama-server.pid")

        assert launcher.restart() is True

    def test_it_does_nothing_once_wheelhouse_is_shutting_down(self, tmp_path):
        """Stopping the server and then failing to start it is what shutdown
        wants anyway, but a replacement is not a thing to begin during one.
        """
        first = FakeProcess(pid=4242)
        second = FakeProcess(pid=4343)
        spawn = SequenceSpawner(first, second)
        launcher = _launcher(spawn=spawn, pid_file=tmp_path / "llama-server.pid")
        assert launcher.start() is True
        launcher.shutdown()

        assert launcher.restart() is False
        assert len(spawn.commands) == 1
