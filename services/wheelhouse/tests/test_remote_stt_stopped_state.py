"""A remote STT provider that failed or died stops claiming to be running.

The runtime record StateManager keeps of the engine that started was only
ever assigned, never invalidated, and _get_current_stt_provider falls back
to stt.last_provider when the record is empty. So after a failed start or
a dead provider the tray kept a check mark on an engine that is not
running, and clearing the record alone would not help -- the config still
names the same provider (wh-remote-stt-robustness, Gap A, from finding
wh-kroko-removal.1.7).

The fix is an explicit stopped state that outranks both, so the next
state update leaves no engine checked. These tests cover the places that
know a provider stopped: the launcher's startup monitor, the autostart
path, an explicit switch whose replacement fails to start, and the
reconciliation of a replacement that failed after it had spawned.

A fifth place was the Google credentials restart. It is gone:
wh-remove-restart-credentials-items deleted
_set_google_credentials_file on David's order (QUESTIONS-2026-09-04.md
item 20), and its cases here went with it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _launcher(tmp_path):
    from stt.remote_stt_launcher import RemoteSTTLauncher

    return RemoteSTTLauncher(
        services_dir=tmp_path / "providers",
        app_data_dir=tmp_path / "appdata",
    )


def _dead_subprocess():
    proc = MagicMock()
    proc.poll.return_value = 1
    return proc


def _live_subprocess():
    proc = MagicMock()
    proc.poll.return_value = None
    return proc


class TestStartupMonitor:
    """The startup monitor is the only place that learns a start failed."""

    def test_a_reported_startup_failure_marks_the_provider_stopped(self, tmp_path):
        launcher = _launcher(tmp_path)
        stopped = MagicMock()
        launcher.set_provider_stopped_callback(stopped)
        launcher._provider_startup_failed = True
        launcher._provider_ready_event.set()

        launcher._monitor_startup("google_stt", "Google", timeout=0.05)

        stopped.assert_called_once_with(
            "google_stt", may_wait=True, generation=None
        )

    def test_a_dead_subprocess_marks_the_provider_stopped(self, tmp_path):
        launcher = _launcher(tmp_path)
        stopped = MagicMock()
        launcher.set_provider_stopped_callback(stopped)
        launcher._provider_startup_failed = False
        launcher._provider_ready_event.clear()
        launcher._subprocesses["google_stt"] = _dead_subprocess()

        launcher._monitor_startup("google_stt", "Google", timeout=0.05)

        stopped.assert_called_once_with(
            "google_stt", may_wait=True, generation=None
        )

    def test_a_slow_but_live_subprocess_is_not_marked_stopped(self, tmp_path):
        # A GPU cold start can outrun the ready-signal timeout while the
        # provider is healthy (wh-v0q). Marking it stopped would blank the
        # tray for an engine that is about to transcribe.
        launcher = _launcher(tmp_path)
        stopped = MagicMock()
        launcher.set_provider_stopped_callback(stopped)
        launcher._provider_startup_failed = False
        launcher._provider_ready_event.clear()
        launcher._subprocesses["google_stt"] = _live_subprocess()

        launcher._monitor_startup("google_stt", "Google", timeout=0.05)

        stopped.assert_not_called()

    def test_a_successful_start_is_not_marked_stopped(self, tmp_path):
        launcher = _launcher(tmp_path)
        stopped = MagicMock()
        launcher.set_provider_stopped_callback(stopped)
        launcher._provider_startup_failed = False
        launcher._provider_ready_event.set()

        launcher._monitor_startup("google_stt", "Google", timeout=0.05)

        stopped.assert_not_called()

    def test_a_failing_callback_does_not_escape_the_monitor_thread(self, tmp_path):
        # The monitor runs in its own thread; an exception there is lost
        # and takes the rest of the monitor with it.
        launcher = _launcher(tmp_path)
        launcher.set_provider_stopped_callback(
            MagicMock(side_effect=RuntimeError("no"))
        )
        launcher._provider_startup_failed = True
        launcher._provider_ready_event.set()

        launcher._monitor_startup("google_stt", "Google", timeout=0.05)


class TestAutostart:
    """ServiceManager.start_remote_stt, when nothing starts at all."""

    def _service_manager(self, state_manager=None):
        from service_manager import ServiceManager

        loop = asyncio.new_event_loop()
        app = MagicMock()
        app.get_screen_dimensions.return_value = (1920, 1080)
        manager = ServiceManager(
            config_service=MagicMock(),
            event_bus=MagicMock(),
            loop=loop,
            app=app,
            state_manager=state_manager or MagicMock(),
        )
        manager.config_service.get.side_effect = (
            lambda key, default=None: {"stt.last_provider": "google_stt"}.get(
                key, default
            )
        )
        launcher = MagicMock()
        launcher.discover_providers.return_value = [{"name": "google_stt"}]
        launcher.start_provider.return_value = False
        manager.remote_stt_launcher = launcher
        return manager, loop

    def test_no_provider_starting_marks_the_engine_stopped(self):
        manager, loop = self._service_manager()
        try:
            assert manager.start_remote_stt() is False
            manager.state_manager.set_remote_stt_stopped.assert_called_once_with()
        finally:
            loop.close()

    def test_no_providers_discovered_marks_the_engine_stopped(self):
        # Discovering nothing is exactly as terminal as starting nothing:
        # both leave the machine with no speech engine. Only the second
        # said so, so the reader kept naming the configured provider
        # (round 1 finding wh-remote-stt-robustness.2.1).
        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "google_stt"}
        )
        manager, loop = self._service_manager(state_manager=state)
        manager.remote_stt_launcher.discover_providers.return_value = []
        try:
            assert manager.start_remote_stt() is False
            assert state._get_current_stt_provider() is None
        finally:
            loop.close()


def _switch_controller(*, start_result: bool, old_still_running: bool = False):
    """Shaped after mock_dependencies in test_ui_provider_switching.py."""
    from main import LogicController

    config_service = MagicMock()
    config_service.get.side_effect = lambda key, default=None: {
        "stt.mode": "remote",
        "stt.last_provider": "google_stt",
    }.get(key, default)
    config_service.save = AsyncMock(return_value=True)

    launcher = MagicMock()
    launcher.stop_provider = AsyncMock(return_value=True)
    launcher.start_provider = MagicMock(return_value=start_result)
    launcher.get_provider_by_name.return_value = {
        "name": "parakeet_tdt",
        "display_name": "Parakeet v3 (GPU)",
    }
    # Answer this explicitly. A bare MagicMock answers every call with a
    # truthy mock, which would tell the switch that the old engine never
    # exited and silently change what these tests assert.
    launcher.is_running = MagicMock(return_value=old_still_running)

    controller = MagicMock(spec=LogicController)
    controller.config_service = config_service
    controller.service_manager = MagicMock()
    controller.service_manager.remote_stt_launcher = launcher
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    controller.state_manager._get_current_stt_provider.return_value = "google_stt"
    controller.shutdown_event = MagicMock()
    controller.shutdown_event.is_set.return_value = False
    return controller


class TestFailedSwitch:
    """The old engine is stopped before the new one is tried."""

    @pytest.mark.asyncio
    async def test_a_replacement_that_fails_to_start_marks_the_engine_stopped(self):
        from main import LogicController

        controller = _switch_controller(start_result=False)

        await LogicController._switch_stt_provider(controller, "parakeet_tdt")

        controller.state_manager.set_remote_stt_stopped.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_a_failed_replacement_pushes_the_correction_to_the_gui(self):
        from main import LogicController

        controller = _switch_controller(start_result=False)

        await LogicController._switch_stt_provider(controller, "parakeet_tdt")

        controller.state_manager.send_state_update.assert_called()

    @pytest.mark.asyncio
    async def test_a_replacement_that_starts_is_not_marked_stopped(self):
        from main import LogicController

        controller = _switch_controller(start_result=True)

        await LogicController._switch_stt_provider(controller, "parakeet_tdt")

        controller.state_manager.set_remote_stt_stopped.assert_not_called()


def _switch_controller_over(state, *, start_result, old_still_running):
    """_switch_controller with a real StateManager, so a test can assert
    the answer the tray reads."""
    controller = _switch_controller(
        start_result=start_result, old_still_running=old_still_running
    )
    controller.state_manager = state
    state.state_to_gui_queue = MagicMock()
    return controller


class TestAFailedSwitchWithASurvivingEngine:
    """stop_provider only sends the shutdown command and returns.

    The old child can still be alive when the replacement fails to start
    -- its websocket can be disconnected, so the command reaches nobody.
    Marking the engine stopped then makes the same false claim from the
    other direction: the tray shows nothing while the old engine keeps
    transcribing, and no path records a provider on reconnect
    (round 1 finding wh-remote-stt-robustness.2.2).
    """

    @pytest.mark.asyncio
    async def test_an_engine_that_outlives_a_failed_switch_is_still_shown(
        self, monkeypatch
    ):
        import main as main_module
        from main import LogicController

        monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 0.3)
        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "google_stt"}
        )
        state.set_running_remote_stt_provider("google_stt")
        controller = _switch_controller_over(
            state, start_result=False, old_still_running=True
        )

        await LogicController._switch_stt_provider(controller, "parakeet_tdt")

        assert state._get_current_stt_provider() == "google_stt"

    @pytest.mark.asyncio
    async def test_a_failed_switch_waits_for_the_old_engine_to_exit(
        self, monkeypatch
    ):
        # The shutdown command was already sent, so the usual answer to
        # the first is_running call is "still alive, for another moment".
        # Deciding on that first answer would leave the record on an
        # engine that is about to exit, which is the defect Gap A fixes.
        import main as main_module
        from main import LogicController

        monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 5.0)
        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "google_stt"}
        )
        state.set_running_remote_stt_provider("google_stt")
        controller = _switch_controller_over(
            state, start_result=False, old_still_running=True
        )
        launcher = controller.service_manager.remote_stt_launcher
        launcher.is_running = MagicMock(
            side_effect=[True, True, False, False, False]
        )

        await LogicController._switch_stt_provider(controller, "parakeet_tdt")

        assert launcher.is_running.call_count >= 3
        assert state._get_current_stt_provider() is None

    @pytest.mark.asyncio
    async def test_an_engine_that_exits_after_a_failed_switch_is_marked_stopped(
        self, monkeypatch
    ):
        import main as main_module
        from main import LogicController

        monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 0.3)
        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "google_stt"}
        )
        state.set_running_remote_stt_provider("google_stt")
        controller = _switch_controller_over(
            state, start_result=False, old_still_running=False
        )

        await LogicController._switch_stt_provider(controller, "parakeet_tdt")

        assert state._get_current_stt_provider() is None


def _real_state_manager(values):
    """A real StateManager, so a test can assert the answer the tray reads
    instead of asserting that a mock was called."""
    from state_manager import StateManager

    config_service = MagicMock()
    config_service.get.side_effect = lambda key, default=None: values.get(key, default)
    state = StateManager(
        config_service=config_service,
        event_bus=MagicMock(),
        loop=MagicMock(),
        state_to_gui_queue=MagicMock(),
        websocket_manager=MagicMock(),
    )
    # Delivery of the correction is covered by the tests above; these ones
    # are about the answer itself.
    state.send_state_update = MagicMock()
    return state


def _reconcile_launcher(names, running):
    """A launcher whose discovered providers are `names` and whose
    is_running answers through `running`."""
    launcher = MagicMock()
    launcher.get_providers.return_value = [{"name": name} for name in names]
    launcher.is_running.side_effect = running
    return launcher


class TestDelayedReplacementFailure:
    """A replacement that spawns and then fails must not blank an engine
    that outlived the switch.

    start_provider returns True as soon as Popen succeeds, so the switch
    records the replacement straight away. If that replacement later
    reports a failed startup, or dies before the ready signal, the
    monitor calls the stopped callback with the replacement's own name.
    That name matches the record, so the guard inside
    set_remote_stt_stopped does not stop the clear. The old engine can
    still be running: stop_provider only broadcasts the shutdown command,
    a provider whose socket is disconnected never receives it, and the
    provider reconnects on its own (wh-remote-stt-robustness.2.5).
    """

    def test_an_engine_that_outlived_a_failed_replacement_is_still_shown(
        self, monkeypatch
    ):
        import main as main_module
        from main import _reconcile_stopped_remote_provider

        # The survivor never exits, so this test would otherwise sit out
        # the whole wait.
        monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 0.4)
        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "parakeet_tdt"}
        )
        state.set_running_remote_stt_provider("parakeet_tdt")
        launcher = _reconcile_launcher(
            ["google_stt", "parakeet_tdt"],
            lambda name: name == "google_stt",
        )

        _reconcile_stopped_remote_provider(
            state, launcher, "parakeet_tdt", may_wait=True
        )

        assert state._get_current_stt_provider() == "google_stt"

    def test_a_failed_provider_with_nothing_else_running_is_marked_stopped(self):
        # The behaviour the fix keeps: on the autostart and single-engine
        # paths there is no survivor, and the tray must show no engine.
        from main import _reconcile_stopped_remote_provider

        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "parakeet_tdt"}
        )
        state.set_running_remote_stt_provider("parakeet_tdt")
        launcher = _reconcile_launcher(
            ["google_stt", "parakeet_tdt"], lambda name: False
        )

        _reconcile_stopped_remote_provider(
            state, launcher, "parakeet_tdt", may_wait=True
        )

        assert state._get_current_stt_provider() is None

    def test_the_failed_provider_itself_is_not_a_survivor(self):
        # is_running answers True while a launch is in flight and while a
        # child is still exiting, so the provider that just failed would
        # otherwise nominate itself.
        from main import _reconcile_stopped_remote_provider

        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "parakeet_tdt"}
        )
        state.set_running_remote_stt_provider("parakeet_tdt")
        launcher = _reconcile_launcher(["parakeet_tdt"], lambda name: True)

        _reconcile_stopped_remote_provider(
            state, launcher, "parakeet_tdt", may_wait=True
        )

        assert state._get_current_stt_provider() is None

    def test_the_reconciliation_waits_for_a_dying_engine_to_exit(
        self, monkeypatch
    ):
        # The old engine is normally still alive for a moment after the
        # shutdown command, so deciding on the first answer would record
        # an engine that is about to exit -- the defect Gap A removes.
        import main as main_module
        from main import _reconcile_stopped_remote_provider

        monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 5.0)
        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "parakeet_tdt"}
        )
        state.set_running_remote_stt_provider("parakeet_tdt")
        launcher = _reconcile_launcher(
            ["google_stt", "parakeet_tdt"],
            [True, True, False, False, False],
        )

        _reconcile_stopped_remote_provider(
            state, launcher, "parakeet_tdt", may_wait=True
        )

        assert launcher.is_running.call_count >= 3
        assert state._get_current_stt_provider() is None

    def test_a_launcher_that_cannot_answer_still_marks_the_engine_stopped(self):
        # The reconciliation runs on the monitor thread, which is the only
        # place that learns a start failed. An exception there must not
        # cost the stopped state itself.
        from main import _reconcile_stopped_remote_provider

        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "parakeet_tdt"}
        )
        state.set_running_remote_stt_provider("parakeet_tdt")
        launcher = MagicMock()
        launcher.get_providers.side_effect = OSError("provider scan failed")

        _reconcile_stopped_remote_provider(
            state, launcher, "parakeet_tdt", may_wait=True
        )

        assert state._get_current_stt_provider() is None


class TestReconciliationOffTheMonitorThread:
    """start_provider's own exception handler reports on whatever thread
    called it, and a switch calls start_provider on the event loop. That
    report must not scan providers and must not wait: stopping the Logic
    event loop for up to ten seconds would stop every GUI command and
    every voice command with it (wh-remote-stt-robustness.2.6).

    Nothing is lost by not waiting there. The record still names the old
    engine on that path, so the guard inside set_remote_stt_stopped
    ignores the report, and _switch_stt_provider's own failure branch
    does the bounded wait and decides.
    """

    def test_a_report_that_may_not_wait_does_not_scan_providers(self):
        from main import _reconcile_stopped_remote_provider

        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "google_stt"}
        )
        state.set_running_remote_stt_provider("google_stt")
        launcher = _reconcile_launcher(
            ["google_stt", "parakeet_tdt"],
            lambda name: name == "google_stt",
        )

        _reconcile_stopped_remote_provider(
            state, launcher, "parakeet_tdt", may_wait=False
        )

        launcher.is_running.assert_not_called()
        assert state._get_current_stt_provider() == "google_stt"

    def test_a_synchronous_launch_failure_does_not_ask_for_a_wait(self, tmp_path):
        # The wiring itself: only the startup-monitor thread may wait.
        launcher = _launcher(tmp_path)
        stopped = MagicMock()
        launcher.set_provider_stopped_callback(stopped)
        # start_provider refuses before its try block while the port is
        # unassigned, so the exception path would never be reached.
        launcher.ws_port = 5555
        launcher.get_provider_by_name = MagicMock(
            return_value={
                "name": "parakeet_tdt",
                "display_name": "Parakeet",
                "service_dir": tmp_path / "nowhere",
                "launcher": "run.py",
            }
        )

        assert launcher.start_provider("parakeet_tdt") is False

        stopped.assert_called_once_with(
            "parakeet_tdt", may_wait=False, generation=1
        )


class TestReconciliationAgainstANewerRecord:
    """A switch can land while the reconciliation is in its bounded wait.

    The scan and the wait run outside the record lock, so the survivor
    the scan chose can be stale by the time it is written -- and
    discover_providers keeps unsorted directory order, so the engine the
    switch replaced can be the first match. Writing it back would leave
    the tray naming an engine that is not the active one, and a ready
    signal for the new engine only returns from its monitor; nothing
    restores its record (wh-remote-stt-robustness.2.7).
    """

    def test_a_newer_engine_recorded_meanwhile_is_not_overwritten(
        self, monkeypatch
    ):
        import main as main_module
        from main import _reconcile_stopped_remote_provider

        # One scan, no waiting: the seam below is what makes this
        # deterministic, not the timing.
        monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 0.0)
        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "parakeet_tdt"}
        )
        state.set_running_remote_stt_provider("parakeet_tdt")
        launcher = MagicMock()
        launcher.get_providers.return_value = [
            {"name": "google_stt"},
            {"name": "parakeet_tdt"},
        ]

        def _running(name):
            # The user's next switch completes while the scan is in
            # progress, so the record already names the newest engine.
            state.set_running_remote_stt_provider("whisper_cpp")
            return name == "google_stt"

        launcher.is_running.side_effect = _running

        _reconcile_stopped_remote_provider(
            state, launcher, "parakeet_tdt", may_wait=True
        )

        assert state._get_current_stt_provider() == "whisper_cpp"

    def test_a_survivor_is_still_recorded_when_the_record_did_not_move(
        self, monkeypatch
    ):
        # The behaviour .2.5 added, kept: this is the case the new guard
        # must not refuse.
        import main as main_module
        from main import _reconcile_stopped_remote_provider

        monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 0.0)
        state = _real_state_manager(
            {"stt.mode": "remote", "stt.last_provider": "parakeet_tdt"}
        )
        state.set_running_remote_stt_provider("parakeet_tdt")
        launcher = _reconcile_launcher(
            ["google_stt", "parakeet_tdt"],
            lambda name: name == "google_stt",
        )

        _reconcile_stopped_remote_provider(
            state, launcher, "parakeet_tdt", may_wait=True
        )

        assert state._get_current_stt_provider() == "google_stt"
