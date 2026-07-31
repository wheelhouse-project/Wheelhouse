"""Tests for wiring the local model server into the Logic process lifecycle
(wh-ai-runtime-startup-wiring).

Three things are under test and nothing else:

- Whether a launcher gets built at all, which depends on both [ai] enabled and
  the validated [ai.runtime] section.
- That starting the server does not freeze the event loop. The server can take
  a minute and a half to load a model; done on the loop thread that would
  freeze every other service in the Logic process.
- That shutdown stops the server, including when the start failed partway and
  when stopping the AI client itself raises.

No test here starts a real server. The launcher is a stand-in throughout,
except in the last class, which builds a real one over fake processes --
because the bug it guards against was a stand-in agreeing with the wiring
about something the real launcher does not do (wh-local-ai-runtime.2.22).
"""

import asyncio
import contextlib
import subprocess
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _deps(overrides=None):
    """Mock ServiceManager constructor arguments with a configurable config."""
    values = overrides or {}

    config_service = MagicMock()
    config_service.get.side_effect = lambda key, default=None: values.get(key, default)
    config_service.get_config.return_value = {}

    app = MagicMock()
    app.get_screen_dimensions.return_value = (1920, 1080)

    return {
        "config_service": config_service,
        "event_bus": MagicMock(),
        "loop": asyncio.new_event_loop(),
        "app": app,
        "state_manager": MagicMock(),
    }


def _manager(overrides=None):
    from service_manager import ServiceManager

    return ServiceManager(**_deps(overrides))


def _runtime_config(**overrides):
    """A valid [ai.runtime] table."""
    table = {
        "enabled": True,
        "model_path": r"C:\models\gemma.gguf",
        "binary_dir": r"C:\llama",
    }
    table.update(overrides)
    return table


class FakeLauncher:
    """Stands in for LocalAIServerLauncher.

    start() and restart() are counted apart because they are not the same
    operation and the wiring has to ask for the right one. start() leaves a
    live-but-unresponsive server exactly where it is; only restart() replaces
    it.
    """

    def __init__(self, start_result=True):
        self._start_result = start_result
        self.started = 0
        self.restarted = 0
        self.stopped = 0
        self.shut_down = 0

    def start(self):
        self.started += 1
        return self._start_result

    def restart(self):
        self.restarted += 1
        return self._start_result

    def stop(self):
        self.stopped += 1

    def shutdown(self):
        self.shut_down += 1
        self.stop()


class _FakeProcess:
    """Stands in for a Popen handle that is alive until asked to stop."""

    def __init__(self, pid, exits_with=None):
        self.pid = pid
        self._exits_with = exits_with
        self.terminated = False

    def poll(self):
        return self._exits_with

    def terminate(self):
        self.terminated = True
        self._exits_with = 0

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self._exits_with


class _UnstoppableProcess(_FakeProcess):
    """Stays running whatever is done to it."""

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="llama-server", timeout=timeout or 0)


class _SequenceSpawner:
    """Hands out a different process for each start, in order."""

    def __init__(self, *processes):
        self._processes = list(processes)
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        return self._processes[len(self.commands) - 1]


class TestWhetherALauncherIsBuilt:

    def test_no_runtime_section_means_no_launcher(self):
        manager = _manager({
            "ai.enabled": True,
            "ai.server.base_url": "http://127.0.0.1:8899/v1",
        })
        assert manager._build_local_ai_launcher() is None

    def test_runtime_disabled_means_no_launcher(self):
        """The common case: somebody else runs the server."""
        manager = _manager({
            "ai.enabled": True,
            "ai.server.base_url": "http://127.0.0.1:8899/v1",
            "ai.runtime": _runtime_config(enabled=False),
        })
        assert manager._build_local_ai_launcher() is None

    def test_a_valid_section_builds_a_launcher_on_the_base_url_port(self):
        manager = _manager({
            "ai.enabled": True,
            "ai.server.base_url": "http://127.0.0.1:8899/v1",
            "ai.runtime": _runtime_config(),
        })
        launcher = manager._build_local_ai_launcher()
        assert launcher is not None
        command = launcher.build_command()
        assert command[command.index("--port") + 1] == "8899"
        assert command[command.index("-m") + 1] == r"C:\models\gemma.gguf"

    def test_the_launcher_is_given_somewhere_to_record_the_process_id(self):
        """Crash recovery only works if the launcher knows where to write.

        A launcher built without that path starts a server perfectly well and
        silently loses the ability to find it again after a crash, which is
        exactly the failure nobody notices until it happens on a user's
        machine. The path resolves through get_user_data_dir, the one resolver
        that agrees between a source checkout and a frozen build.
        """
        from ai.server_launcher import PID_FILE_NAME
        from utils.system import get_user_data_dir

        manager = _manager({
            "ai.enabled": True,
            "ai.server.base_url": "http://127.0.0.1:8899/v1",
            "ai.runtime": _runtime_config(),
        })
        launcher = manager._build_local_ai_launcher()
        assert launcher is not None
        assert launcher._pid_file == get_user_data_dir() / PID_FILE_NAME

    def test_a_bad_value_means_no_launcher_not_a_crash(self):
        """A malformed section must not take the Logic process down.

        An external server at base_url is still perfectly usable, so this
        degrades to "do not start a server", not to "no AI".
        """
        manager = _manager({
            "ai.enabled": True,
            "ai.server.base_url": "http://127.0.0.1:8899/v1",
            "ai.runtime": _runtime_config(context_size=-1),
        })
        assert manager._build_local_ai_launcher() is None

    def test_a_non_loopback_server_address_means_no_launcher(self):
        """We cannot start a process on somebody else's machine."""
        manager = _manager({
            "ai.enabled": True,
            "ai.server.base_url": "http://192.168.1.50:8899/v1",
            "ai.runtime": _runtime_config(),
        })
        assert manager._build_local_ai_launcher() is None

    def test_ai_switched_off_entirely_means_no_launcher(self):
        """[ai] enabled is the master switch and outranks [ai.runtime]."""
        manager = _manager({
            "ai.enabled": False,
            "ai.server.base_url": "http://127.0.0.1:8899/v1",
            "ai.runtime": _runtime_config(),
        })
        assert manager._build_local_ai_launcher() is None


class TestStarting:

    @pytest.mark.asyncio
    async def test_the_server_starts_before_the_client(self):
        """The client's first reachability probe should find a live server."""
        order = []
        manager = _manager()
        launcher = FakeLauncher()
        launcher.start = lambda: order.append("server") or True
        manager.local_ai_launcher = launcher
        manager.ai_service = MagicMock()
        manager.ai_service.start = AsyncMock(
            side_effect=lambda: order.append("client")
        )

        await manager._start_ai_service()

        assert order == ["server", "client"]

    @pytest.mark.asyncio
    async def test_the_client_still_starts_when_the_server_fails(self):
        """AI features fail soft. A dead local server is not a reason to skip
        loading the knowledge base or to leave the client unbuilt -- the user
        may still point base_url at something that comes up later."""
        manager = _manager()
        manager.local_ai_launcher = FakeLauncher(start_result=False)
        manager.ai_service = MagicMock()
        manager.ai_service.start = AsyncMock()

        await manager._start_ai_service()

        manager.ai_service.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_launcher_still_starts_the_client(self):
        manager = _manager()
        manager.local_ai_launcher = None
        manager.ai_service = MagicMock()
        manager.ai_service.start = AsyncMock()

        await manager._start_ai_service()

        manager.ai_service.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_starting_the_server_does_not_freeze_the_event_loop(self):
        """Loading a model file takes tens of seconds.

        The launcher's start() blocks for that whole time, so it has to run off
        the loop thread. This checks that directly: the launcher waits for a
        flag that only a coroutine can set. Run on the loop thread, the flag
        never arrives and the launcher spins out its own deadline.
        """
        loop_ran = {"yes": False}

        class WaitsForTheLoop:
            def __init__(self):
                self.saw_the_loop_run = False

            def start(self):
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not loop_ran["yes"]:
                    time.sleep(0.01)
                self.saw_the_loop_run = loop_ran["yes"]
                return True

            def stop(self):
                pass

        async def other_work():
            await asyncio.sleep(0.02)
            loop_ran["yes"] = True

        launcher = WaitsForTheLoop()
        manager = _manager()
        manager.local_ai_launcher = launcher
        manager.ai_service = MagicMock()
        manager.ai_service.start = AsyncMock()

        await asyncio.gather(manager._start_ai_service(), other_work())

        assert launcher.saw_the_loop_run is True

    @pytest.mark.asyncio
    async def test_a_launcher_that_raises_does_not_stop_startup(self):
        class Exploding:
            def start(self):
                raise RuntimeError("no such binary")

            def stop(self):
                pass

        manager = _manager()
        manager.local_ai_launcher = Exploding()
        manager.ai_service = MagicMock()
        manager.ai_service.start = AsyncMock()

        await manager._start_ai_service()

        manager.ai_service.start.assert_awaited_once()


class TestConnectingTheRestartToTheReadinessChecks:
    """A server that dies after a healthy start has to be replaced.

    AIService already notices -- it re-probes every 60 seconds and after every
    failed request -- but noticing only set a flag. The one call that starts a
    server ran at startup and never again, so the AI features stayed off until
    the whole of Wheelhouse was restarted. These tests are about the
    connection between the noticing and the starting; without it, every test
    of the restart itself passes while nothing in the running program calls
    it.
    """

    def _initialised(self, overrides):
        # The rest of the service graph needs configuration of its own, and
        # none of it is what these tests are about.
        settings = {
            "plugins.bravia.device_name": "TV",
            "SPATIAL_SOUND_EXEC": "",
        }
        settings.update(overrides)
        manager = _manager(settings)
        manager.initialize_services()
        return manager

    def test_the_ai_service_is_told_how_to_start_our_server_again(self):
        manager = self._initialised({
            "ai.enabled": True,
            "ai.server.base_url": "http://127.0.0.1:8899/v1",
            "ai.runtime": _runtime_config(),
        })

        assert manager.ai_service is not None
        assert manager.ai_service._restart_local_server is not None

    def test_an_external_server_is_not_ours_to_start(self):
        """No [ai.runtime] section, so the user runs the server themselves.
        Starting one on their behalf is not something we may do.
        """
        manager = self._initialised({
            "ai.enabled": True,
            "ai.server.base_url": "http://127.0.0.1:8899/v1",
        })

        assert manager.ai_service is not None
        assert manager.ai_service._restart_local_server is None

    @pytest.mark.asyncio
    async def test_the_restart_replaces_the_server(self):
        """It asks for a replacement, not for a server to exist.

        AIService calls this only after the server stopped answering, and a
        server can stop answering while its process is still alive. start()
        reports success on sight of a live process, so asking for a start
        would leave the wedged server running and report that it had been
        dealt with.
        """
        manager = _manager()
        launcher = FakeLauncher(start_result=True)
        manager.local_ai_launcher = launcher

        assert await manager._restart_local_ai_server() is True
        assert launcher.restarted == 1
        assert launcher.started == 0, (
            "asked for a server to exist rather than for a replacement"
        )

    @pytest.mark.asyncio
    async def test_a_restart_that_fails_says_so(self):
        manager = _manager()
        manager.local_ai_launcher = FakeLauncher(start_result=False)

        assert await manager._restart_local_ai_server() is False

    @pytest.mark.asyncio
    async def test_the_restart_does_not_freeze_the_event_loop(self):
        """Starting a server loads a multi-gigabyte model file. On the event
        loop that stops speech recognition, the tray icon, and every other
        service in the Logic process for as long as it takes.

        Asked the obvious way -- gather the restart with other work and check
        the other work ran -- this passes either way, because gather waits for
        both and the other work simply runs after the block finishes. What
        has to be checked is that the loop ran WHILE the launcher was still
        inside start(), so the launcher itself is what reports it.
        """
        loop_ran = {"yes": False}

        class WaitsForTheLoop:
            def __init__(self):
                self.saw_the_loop_run = False

            def restart(self):
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not loop_ran["yes"]:
                    time.sleep(0.01)
                self.saw_the_loop_run = loop_ran["yes"]
                return True

        launcher = WaitsForTheLoop()
        manager = _manager()
        manager.local_ai_launcher = launcher

        async def something_else():
            await asyncio.sleep(0.02)
            loop_ran["yes"] = True

        await asyncio.gather(
            manager._restart_local_ai_server(), something_else()
        )

        assert launcher.saw_the_loop_run is True

    @pytest.mark.asyncio
    async def test_a_restart_with_no_launcher_is_harmless(self):
        manager = _manager()
        manager.local_ai_launcher = None

        assert await manager._restart_local_ai_server() is False


class TestTheRestartAgainstARealLauncher:
    """The wiring and a real launcher, composed (wh-local-ai-runtime.2.22).

    Every other test in this file gives the wiring a stand-in whose answer is
    decided in advance, and that is what let this through: a stand-in agreed
    that asking for a start deals with a server that stopped answering, while
    the real launcher returns True on sight of a live process without asking
    it anything. The two halves each passed and the pair did not work.

    So these build a real LocalAIServerLauncher over fake processes, and put
    it in the state the supervisor actually meets: the process is up, and it
    has stopped answering.
    """

    def _launcher(self, spawn, pid_file):
        from ai.runtime_config import RuntimeConfig
        from ai.server_launcher import Liveness, LocalAIServerLauncher

        def probe(port):
            # The port answers exactly while a server of ours is running, and
            # goes quiet when it exits -- one probe for the two questions the
            # launcher asks, as in the real system. It reads the launcher's
            # own handles rather than remembering what it spawned, so a
            # replacement is what it answers about.
            held = launcher._process or launcher._unstopped
            return held is not None and held.poll() is None

        launcher = LocalAIServerLauncher(
            RuntimeConfig(
                enabled=True,
                model_path=r"C:\models\gemma.gguf",
                binary_dir=r"C:\llama",
                port=8899,
                context_size=8192,
                gpu_layers=99,
                startup_timeout_seconds=90,
            ),
            spawn=spawn,
            probe=probe,
            sleep=lambda s: None,
            monotonic=lambda: 0.0,
            pid_file=pid_file,
            stale_probe=lambda pid: Liveness.GONE,
            stale_terminate=lambda pid: True,
            owner_alive=lambda pid, name: Liveness.GONE,
            start_lock=lambda port: contextlib.nullcontext(True),
        )
        return launcher

    @pytest.mark.asyncio
    async def test_a_wedged_server_is_actually_replaced(self, tmp_path):
        """The process is alive, so start() would report success and change
        nothing. What the supervisor needs is the old one ended and a new one
        in its place.
        """
        first, second = _FakeProcess(pid=4242), _FakeProcess(pid=4343)
        spawn = _SequenceSpawner(first, second)
        launcher = self._launcher(spawn, tmp_path / "llama-server.pid")
        assert launcher.start() is True

        manager = _manager()
        manager.local_ai_launcher = launcher

        assert await manager._restart_local_ai_server() is True
        assert first.terminated, "the wedged server was left running"
        assert len(spawn.commands) == 2, (
            f"asked for {len(spawn.commands)} servers, not a replacement"
        )

    @pytest.mark.asyncio
    async def test_a_server_that_will_not_stop_is_not_joined_by_a_second(
        self, tmp_path
    ):
        """Supervision must not turn one server it cannot end into two."""
        first, second = _UnstoppableProcess(pid=4242), _FakeProcess(pid=4343)
        spawn = _SequenceSpawner(first, second)
        launcher = self._launcher(spawn, tmp_path / "llama-server.pid")
        assert launcher.start() is True

        manager = _manager()
        manager.local_ai_launcher = launcher

        assert await manager._restart_local_ai_server() is False
        assert len(spawn.commands) == 1, "a second server was started anyway"


class TestStopping:

    @pytest.mark.asyncio
    async def test_shutdown_stops_the_server(self):
        manager = _manager()
        launcher = FakeLauncher()
        manager.local_ai_launcher = launcher

        await manager.shutdown_services()

        assert launcher.stopped == 1

    @pytest.mark.asyncio
    async def test_shutdown_tells_the_launcher_it_is_final(self):
        """A plain stop() leaves the launcher able to start again, which is
        right when a start gave up waiting and wrong at shutdown.

        Stopping the AI client cancels the readiness loop, and that loop may
        be awaiting a restart running on a worker thread. Cancelling that
        await returns at once while the thread carries on, so a stop() that
        finds nothing recorded returns, and the thread then spawns a server
        nothing will stop. Only shutdown() tells that thread to give up.
        """
        manager = _manager()
        launcher = FakeLauncher()
        manager.local_ai_launcher = launcher

        await manager.shutdown_services()

        assert launcher.shut_down == 1

    @pytest.mark.asyncio
    async def test_shutdown_stops_the_server_even_when_the_client_stop_raises(self):
        """Otherwise a client-side error orphans a model server holding the
        graphics card until the user finds it in Task Manager."""
        manager = _manager()
        launcher = FakeLauncher()
        manager.local_ai_launcher = launcher
        manager.ai_service = MagicMock()
        manager.ai_service.stop = AsyncMock(side_effect=RuntimeError("boom"))

        await manager.shutdown_services()

        assert launcher.stopped == 1

    @pytest.mark.asyncio
    async def test_shutdown_without_a_launcher_is_harmless(self):
        manager = _manager()
        manager.local_ai_launcher = None

        await manager.shutdown_services()

    @pytest.mark.asyncio
    async def test_a_launcher_stop_that_raises_does_not_stop_shutdown(self):
        class Exploding:
            def stop(self):
                raise RuntimeError("boom")

        manager = _manager()
        manager.local_ai_launcher = Exploding()
        manager.mouse_handler = None
        manager.software_dimmer = None

        await manager.shutdown_services()
