"""Post-ready provider death, exercised without launching a speech provider."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import main as main_module
from state_manager import StateManager
from stt import remote_stt_launcher as launcher_module


@pytest.fixture
def journey(tmp_path, monkeypatch, mock_config, mock_event_bus, mock_gui_queue):
    threads = []
    processes = []
    spawn_failure = []
    report_failure = []

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -1

    def spawn(*args, **kwargs):
        if spawn_failure:
            raise spawn_failure.pop()
        proc = Process()
        processes.append(proc)
        return proc

    class DeferredThread:
        def __init__(self, *, target, args, daemon, kwargs=None):
            self.target, self.args = target, args
            self.kwargs = kwargs or {}
            self.finished = False

        def start(self):
            if self.target == launcher._provider_stopped and report_failure:
                raise report_failure.pop()
            threads.append(self)

        def is_alive(self):
            return False

        def finish(self):
            self.finished = True
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(launcher_module.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher_module.threading, "Thread", DeferredThread)
    launcher = launcher_module.RemoteSTTLauncher(
        services_dir=tmp_path / "providers", app_data_dir=tmp_path / "appdata",
        ws_port=12345,
    )
    launcher._providers = [
        {"name": name, "display_name": name, "service_dir": tmp_path,
         "launcher": "synthetic.py", "startup_timeout_seconds": 0}
        for name in ("google_stt", "parakeet_tdt")
    ]
    notices = Mock()
    launcher.set_notify_callback(notices)
    ws = SimpleNamespace(send_command_to_stt=AsyncMock())
    launcher.set_websocket_manager(ws)
    loop = asyncio.new_event_loop()
    state = StateManager(mock_config, mock_event_bus, loop, mock_gui_queue, None)
    state.set_remote_stt_launcher(launcher)
    launcher.set_provider_stopped_callback(
        lambda name, *, generation=None, may_wait=False:
        state.set_remote_stt_stopped(name, generation)
    )

    def start(name="google_stt", ready=True):
        assert launcher.start_provider(name)
        state.set_running_remote_stt_provider(name)
        if ready:
            launcher.signal_provider_ready(launcher.launch_generation(name))
            threads[-1].finish()
        return processes[-1]

    def update():
        mock_gui_queue.put_nowait.reset_mock()
        state.send_state_update()
        emitted = bool(mock_gui_queue.put_nowait.call_count)
        assert emitted, "the GUI must receive its state update"
        return mock_gui_queue.put_nowait.call_args.args[0]

    def finish_reports():
        for thread in list(threads):
            if thread.target == launcher._provider_stopped and not thread.finished:
                thread.finish()

    yield SimpleNamespace(
        launcher=launcher, state=state, processes=processes, threads=threads,
        spawn_failure=spawn_failure, notices=notices, start=start, update=update,
        ws=ws, loop=loop, finish_reports=finish_reports,
        report_failure=report_failure,
    )
    loop.close()


def test_post_ready_death_restarts_once_and_publishes_new_generation(journey):
    original = journey.start()
    original_generation = journey.launcher.launch_generation("google_stt")
    original.returncode = 1

    displayed = journey.update()

    assert len(journey.processes) == 2, "post-ready death must spawn one recovery"
    assert displayed["stt_provider"] == "google_stt"
    assert journey.state._running_remote_stt_generation != original_generation
    assert journey.state._running_remote_stt_generation == journey.launcher.launch_generation("google_stt")
    journey.launcher.signal_provider_ready()
    journey.threads[-1].finish()
    journey.update()
    assert len(journey.processes) == 2


def test_failed_retry_spawn_clears_tray_and_notifies(journey):
    journey.start().returncode = 1
    journey.spawn_failure.append(OSError("synthetic spawn refusal"))

    journey.update()
    journey.finish_reports()
    displayed = journey.update()

    assert displayed["stt_provider"] is None, "failed recovery must clear the tray"
    assert journey.notices.called, "failed recovery must explain its failure"
    assert "Failed to start" in journey.notices.call_args.args[1]
    journey.update()
    assert len(journey.processes) == 1


def test_failed_retry_startup_clears_new_generation(journey):
    journey.start().returncode = 1
    journey.update()
    assert len(journey.processes) == 2, "recovery must begin before its failure"
    journey.launcher.signal_provider_startup_failed()
    journey.threads[-1].finish()

    assert journey.update()["stt_provider"] is None
    assert len(journey.processes) == 2


def test_successful_retry_death_does_not_restart_a_second_time(journey):
    journey.start().returncode = 1
    journey.update()
    assert len(journey.processes) == 2, "first recovery must happen"
    journey.launcher.signal_provider_ready()
    journey.threads[-1].finish()
    journey.processes[-1].returncode = 2

    journey.update()
    journey.finish_reports()
    assert journey.update()["stt_provider"] is None
    assert len(journey.processes) == 2, "one recovery is the whole retry budget"
    assert journey.notices.called
    journey.update()
    assert len(journey.processes) == 2


def test_websocket_disconnect_and_reconnect_do_not_restart_live_process(journey):
    journey.start()
    journey.state.unregister_stt_connection()
    assert journey.update()["stt_provider"] == "google_stt"
    journey.state.register_stt_connection(object())
    journey.launcher.signal_provider_ready()
    assert journey.update()["stt_provider"] == "google_stt"
    assert len(journey.processes) == 1
    journey.notices.assert_not_called()


def test_pre_ready_death_remains_a_startup_failure_without_retry(journey):
    journey.start(ready=False).returncode = 1
    journey.threads[-1].finish()
    assert journey.update()["stt_provider"] is None
    assert len(journey.processes) == 1


def test_replaced_provider_death_cannot_restart_or_clear_replacement(journey):
    old = journey.start()
    journey.start("parakeet_tdt")
    old.returncode = 1
    assert journey.update()["stt_provider"] == "parakeet_tdt"
    assert len(journey.processes) == 2
    journey.notices.assert_not_called()


def test_intentional_stop_disables_recovery_before_shutdown_send(journey):
    proc = journey.start()

    async def shutdown(_command):
        proc.returncode = 0
        journey.update()
        assert len(journey.processes) == 1

    journey.ws.send_command_to_stt.side_effect = shutdown
    assert journey.loop.run_until_complete(journey.launcher.stop_provider("google_stt"))
    journey.update()
    assert len(journey.processes) == 1
    journey.notices.assert_not_called()


def test_application_shutdown_does_not_revive_already_dead_provider(journey):
    journey.start().returncode = 1
    journey.loop.run_until_complete(journey.launcher.shutdown_all_providers())
    journey.update()
    assert len(journey.processes) == 1
    journey.notices.assert_not_called()


def test_retry_refused_before_spawn_still_clears_tray_and_notifies(journey):
    journey.start().returncode = 1
    journey.launcher.ws_port = 0
    journey.update()
    journey.finish_reports()
    assert journey.update()["stt_provider"] is None
    assert journey.notices.called
    assert len(journey.processes) == 1


def test_unknown_process_health_preserves_state_delivery_without_retry(journey, monkeypatch):
    proc = journey.start()
    monkeypatch.setattr(proc, "poll", Mock(side_effect=OSError("synthetic poll unavailable")))
    journey.state.config_service.set("FLOATING_BUTTON_SIZE", 80)
    state = journey.update()
    assert state["FLOATING_BUTTON_SIZE"] == 80, "health failure must not suppress GUI updates"
    assert state["stt_provider"] == "google_stt"
    assert len(journey.processes) == 1
    journey.notices.assert_not_called()


def test_live_pid_after_supervisor_exit_is_not_confirmed_provider_death(journey, monkeypatch):
    journey.start().returncode = 1
    pid_file = journey.launcher._get_pid_file_path("google_stt")
    pid_file.write_text("1234567")
    monkeypatch.setattr(launcher_module.psutil, "pid_exists", lambda pid: True)
    assert journey.update()["stt_provider"] == "google_stt"
    assert len(journey.processes) == 1
    assert pid_file.read_text() == "1234567"
    journey.notices.assert_not_called()
    # A surviving child can exit later. Waiting must leave recovery armed.
    monkeypatch.setattr(launcher_module.psutil, "pid_exists", lambda pid: False)
    journey.update()
    assert len(journey.processes) == 2


def test_ready_after_startup_deadline_still_arms_post_ready_watchdog(journey):
    proc = journey.start(ready=False)
    journey.threads[-1].finish()  # Slow-start path delegates its pre-ready watch.
    journey.launcher.signal_provider_ready()
    journey.threads[-1].finish()  # Pre-ready watcher returns on this ready.
    proc.returncode = 1
    journey.update()
    assert len(journey.processes) == 2


def test_stale_same_provider_record_cannot_restart_latest_launch(journey):
    journey.start().returncode = 1
    old_generation = journey.state._running_remote_stt_generation
    journey.start().returncode = 1
    journey.state._running_remote_stt_generation = old_generation
    journey.update()
    assert len(journey.processes) == 2
    assert journey.state._running_remote_stt_generation == old_generation
    journey.notices.assert_not_called()


def test_switch_during_liveness_observation_cannot_restart_old_provider(journey, monkeypatch):
    proc = journey.start()
    proc.returncode = 1
    ordinary_is_running = journey.launcher.is_running

    def switch_during_check(name):
        monkeypatch.setattr(journey.launcher, "is_running", ordinary_is_running)
        journey.start("parakeet_tdt")
        return False

    monkeypatch.setattr(journey.launcher, "is_running", switch_during_check)
    assert journey.update()["stt_provider"] == "parakeet_tdt"
    assert len(journey.processes) == 2
    journey.notices.assert_not_called()


def test_synchronous_retry_failure_after_spawn_cannot_be_overwritten(journey, monkeypatch):
    journey.start().returncode = 1
    ordinary_spawn = launcher_module.subprocess.Popen

    def fail_immediately(*args, **kwargs):
        proc = ordinary_spawn(*args, **kwargs)
        journey.launcher.signal_provider_startup_failed()
        journey.state.set_remote_stt_stopped(
            "google_stt", journey.launcher.launch_generation("google_stt")
        )
        return proc

    monkeypatch.setattr(launcher_module.subprocess, "Popen", fail_immediately)
    assert journey.update()["stt_provider"] is None
    assert len(journey.processes) == 2
    journey.threads[-1].finish()
    assert journey.update()["stt_provider"] is None


def test_dead_launch_without_ready_is_not_retried_before_monitor_decides(journey):
    journey.start(ready=False).returncode = 1
    journey.update()
    assert len(journey.processes) == 1
    journey.notices.assert_not_called()
    journey.threads[-1].finish()
    assert journey.update()["stt_provider"] is None


def test_shutdown_disables_all_recovery_before_first_provider_send(journey):
    journey.start()
    journey.start("parakeet_tdt").returncode = 1

    async def shutdown(_command):
        journey.update()
        assert len(journey.processes) == 2

    journey.ws.send_command_to_stt.side_effect = shutdown
    journey.loop.run_until_complete(journey.launcher.shutdown_all_providers())
    assert len(journey.processes) == 2
    journey.notices.assert_not_called()


@pytest.mark.parametrize("kind", ["ready", "startup_failed"])
def test_retry_websocket_success_or_failure_reaches_real_state_and_speech_queue(journey, kind):
    from integrations.websocket_manager import WebSocketManager

    journey.start().returncode = 1
    journey.update()
    assert len(journey.processes) == 2
    journey.state.speech_notifier._send_notification = journey.notices

    async def deliver():
        manager = WebSocketManager(loop=asyncio.get_running_loop())
        manager.state_manager = journey.state
        manager.remote_stt_launcher = journey.launcher

        async def frames():
            yield json.dumps({"type": "capabilities", "provider": "google_stt", "emits_eos": False})
            yield json.dumps({"type": "notification", "kind": kind, "title": "Synthetic provider",
                              "message": "Speech ready" if kind == "ready" else "Synthetic startup failure"})
            if kind == "ready":
                yield json.dumps({"type": "final", "text": "show grid", "utterance_id": 1})

        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 9999)
        ws.__aiter__ = Mock(return_value=frames())
        await manager.handle_connection(ws)
        journey.threads[-1].finish()
        events = []
        while not manager.word_queue.empty():
            events.append(manager.word_queue.get_nowait())
        return events

    events = journey.loop.run_until_complete(deliver())
    displayed = journey.update()
    assert len(journey.processes) == 2
    if kind == "ready":
        assert displayed["stt_provider"] == "google_stt"
        assert [event.word for event in events if event.word] == ["show", "grid"]
        assert any(event.is_utterance_end_marker for event in events)
    else:
        assert displayed["stt_provider"] is None
        assert events == []
        assert any("Synthetic startup failure" in call.args[1]
                   for call in journey.notices.call_args_list)


@pytest.fixture
def surviving_switch(journey, monkeypatch):
    """Run the ordinary switch producer and the production stopped callback."""
    monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 0)
    callbacks = []

    def stopped(name, *, may_wait=False, generation=None):
        callbacks.append((name, may_wait, generation))
        main_module._reconcile_stopped_remote_provider(
            journey.state, journey.launcher, name,
            may_wait=may_wait, generation=generation,
        )

    journey.launcher.set_provider_stopped_callback(stopped)
    old = journey.start()
    journey.ws.send_command_to_stt.side_effect = OSError("synthetic disconnected shutdown")
    config = SimpleNamespace(set=Mock(), save=AsyncMock(return_value=True))
    controller = SimpleNamespace(
        state_manager=journey.state, config_service=config,
        service_manager=SimpleNamespace(remote_stt_launcher=journey.launcher),
    )
    journey.loop.run_until_complete(
        main_module.LogicController._switch_stt_provider(controller, "parakeet_tdt")
    )
    assert journey.ws.send_command_to_stt.await_count == 1
    assert old.poll() is None, "failed shutdown must leave the original engine alive"
    assert journey.state._running_remote_stt_provider == "parakeet_tdt"
    journey.launcher.signal_provider_ready()
    journey.threads[-1].finish()
    return SimpleNamespace(journey=journey, callbacks=callbacks, old=old)


def _terminal_watchdog_failure(case, ending):
    journey = case.journey
    journey.processes[-1].returncode = 1
    if ending == "retry-death":
        journey.update()
        assert len(journey.processes) == 3
        journey.launcher.signal_provider_ready()
        journey.threads[-1].finish()
        journey.processes[-1].returncode = 2
    elif ending == "early-refusal":
        journey.launcher.ws_port = 0
    elif ending == "pre-stamp-exception":
        journey.launcher.wake_word_config = {"enabled": True, "model_dir": "synthetic"}
        journey.launcher._resolve_wake_word_model_dir = Mock(
            side_effect=OSError("synthetic model path refusal before stamp")
        )
    else:
        journey.spawn_failure.append(OSError("synthetic retry spawn refusal"))
    journey.update()


@pytest.mark.parametrize("ending", ["retry-death", "early-refusal", "spawn-refusal", "pre-stamp-exception"])
def test_watchdog_terminal_failure_reconciles_surviving_switch_off_loop(surviving_switch, ending):
    case = surviving_switch
    journey = case.journey
    _terminal_watchdog_failure(case, ending)
    # DeferredThread does not execute until this explicit off-loop handoff.
    queued_calls = list(case.callbacks)
    journey.finish_reports()
    assert journey.state._running_remote_stt_provider == "google_stt", (
        "terminal watchdog failure must retain the surviving previous engine"
    )
    assert queued_calls == [], "the event loop must not run survivor reconciliation"
    assert len(case.callbacks) == 1 and case.callbacks[0][1] is True
    count = len(journey.processes)
    assert journey.update()["stt_provider"] == "google_stt"
    assert len(journey.processes) == count
    assert journey.notices.called


@pytest.mark.parametrize("ending", ["retry-death", "early-refusal", "spawn-refusal", "pre-stamp-exception"])
def test_watchdog_delayed_reconciliation_preserves_newer_same_provider(surviving_switch, ending):
    case = surviving_switch
    journey = case.journey
    _terminal_watchdog_failure(case, ending)
    journey.launcher.ws_port = 12345
    journey.launcher.wake_word_config = {}
    journey.start("parakeet_tdt")
    new_generation = journey.state._running_remote_stt_generation
    journey.finish_reports()
    assert journey.state._running_remote_stt_provider == "parakeet_tdt"
    assert journey.state._running_remote_stt_generation == new_generation
    assert len(case.callbacks) == 1
    assert case.callbacks[0][2] != new_generation


@pytest.mark.parametrize("ending", ["retry-death", "early-refusal", "spawn-refusal", "pre-stamp-exception"])
def test_watchdog_refused_report_retries_only_reconciliation(surviving_switch, ending):
    case = surviving_switch
    journey = case.journey
    journey.report_failure.append(RuntimeError("synthetic report thread refusal"))
    _terminal_watchdog_failure(case, ending)
    count = len(journey.processes)
    journey.update()
    journey.finish_reports()
    assert journey.state._running_remote_stt_provider == "google_stt", (
        "a refused report handoff must remain eligible for survivor reconciliation"
    )
    assert len(journey.processes) == count, "report retry must not restart the provider"
    assert len(case.callbacks) == 1


@pytest.mark.parametrize("ending", ["retry-death", "early-refusal", "spawn-refusal", "pre-stamp-exception"])
def test_watchdog_terminal_report_clears_record_when_no_survivor(surviving_switch, ending):
    case = surviving_switch
    case.old.returncode = 0
    _terminal_watchdog_failure(case, ending)
    case.journey.finish_reports()
    assert case.journey.state._running_remote_stt_provider is None
    assert len(case.callbacks) == 1 and case.callbacks[0][1] is True


@pytest.mark.parametrize("intent", ["stop", "shutdown"])
def test_watchdog_pending_report_respects_stop_and_shutdown(surviving_switch, intent):
    case = surviving_switch
    journey = case.journey
    journey.report_failure.append(RuntimeError("synthetic report thread refusal"))
    _terminal_watchdog_failure(case, "spawn-refusal")
    case.callbacks.clear()
    if intent == "stop":
        journey.loop.run_until_complete(journey.launcher.stop_provider("parakeet_tdt"))
    else:
        journey.loop.run_until_complete(journey.launcher.shutdown_all_providers())
    count = len(journey.threads)
    journey.update()
    assert len(journey.threads) == count
    assert case.callbacks == []


def test_reconciled_survivor_death_restarts_once_and_records_the_new_launch(
    surviving_switch,
):
    """The engine reconciliation adopted is the engine in use: watch it."""
    case = surviving_switch
    journey = case.journey
    _terminal_watchdog_failure(case, "spawn-refusal")
    journey.finish_reports()
    assert journey.state._running_remote_stt_provider == "google_stt"
    adopted_generation = journey.state._running_remote_stt_generation
    count = len(journey.processes)

    case.old.returncode = 1
    displayed = journey.update()

    assert len(journey.processes) == count + 1, (
        "the adopted survivor's post-ready death must spawn one replacement"
    )
    assert displayed["stt_provider"] == "google_stt"
    assert journey.state._running_remote_stt_generation != adopted_generation
    assert journey.state._running_remote_stt_generation == (
        journey.launcher.launch_generation("google_stt")
    )
    journey.update()
    assert len(journey.processes) == count + 1, "one restart is the whole budget"


def test_reconciled_survivor_refused_restart_reports_and_notifies(surviving_switch):
    """A stale adopted launch earns the same terminal report and notice."""
    case = surviving_switch
    journey = case.journey
    _terminal_watchdog_failure(case, "spawn-refusal")
    journey.finish_reports()
    assert journey.state._running_remote_stt_provider == "google_stt"
    journey.notices.reset_mock()
    journey.launcher.ws_port = 0

    case.old.returncode = 1
    journey.update()
    journey.finish_reports()

    assert journey.update()["stt_provider"] is None, (
        "the tray must stop naming an adopted engine that could not restart"
    )
    assert journey.notices.called, "the user must be told the engine is gone"
    assert "could not restart" in journey.notices.call_args.args[1]


def test_switch_back_to_a_surviving_engine_watches_it_again(journey):
    """A stop that never landed must not blind the watchdog after re-adoption."""
    old = journey.start()
    journey.ws.send_command_to_stt.side_effect = OSError(
        "synthetic disconnected shutdown"
    )
    journey.loop.run_until_complete(journey.launcher.stop_provider("google_stt"))
    assert old.poll() is None, "the shutdown that never landed leaves it alive"
    journey.start("parakeet_tdt")
    journey.loop.run_until_complete(journey.launcher.stop_provider("parakeet_tdt"))
    # The switch back finds google_stt alive, so no new launch is stamped.
    assert journey.launcher.start_provider("google_stt")
    journey.state.set_running_remote_stt_provider("google_stt")
    count = len(journey.processes)
    assert count == 2, "the switch back must not spawn a duplicate"

    old.returncode = 1
    displayed = journey.update()

    assert len(journey.processes) == count + 1, (
        "the re-adopted engine's post-ready death must spawn one replacement"
    )
    assert displayed["stt_provider"] == "google_stt"


def test_switch_back_to_a_pid_file_survivor_watches_it_again(journey, monkeypatch):
    """The port-match adoption exit must re-arm too, not only the tracked one."""
    old = journey.start()
    journey.ws.send_command_to_stt.side_effect = OSError(
        "synthetic disconnected shutdown"
    )
    journey.loop.run_until_complete(journey.launcher.stop_provider("google_stt"))
    # The supervisor is killed without running its PID-file cleanup, so the
    # STT child it spawned outlives it. start_provider then adopts through
    # the port-match exit rather than the tracked-supervisor one.
    old.returncode = 1
    child_alive = [True]
    monkeypatch.setattr(
        launcher_module.psutil, "pid_exists", lambda pid: child_alive[0]
    )
    journey.launcher._get_pid_file_path("google_stt").write_text("1234567")
    (journey.launcher.app_data_dir / "google_stt.port").write_text("12345")

    assert journey.launcher.start_provider("google_stt")
    journey.state.set_running_remote_stt_provider("google_stt")
    count = len(journey.processes)
    assert count == 1, "the port-match adoption must not spawn a duplicate"

    child_alive[0] = False
    displayed = journey.update()

    assert len(journey.processes) == count + 1, (
        "the engine adopted by port match must be watched again"
    )
    assert displayed["stt_provider"] == "google_stt"


def test_a_requested_stop_still_blocks_recovery_when_the_engine_dies(journey):
    """No false positive: nothing re-adopted this engine after the stop."""
    proc = journey.start()
    journey.ws.send_command_to_stt.side_effect = OSError(
        "synthetic disconnected shutdown"
    )
    journey.loop.run_until_complete(journey.launcher.stop_provider("google_stt"))

    proc.returncode = 0
    journey.update()

    assert len(journey.processes) == 1, (
        "an engine the launcher asked to stop must not be restarted"
    )
    journey.notices.assert_not_called()


def test_failed_switch_keeps_watching_the_engine_it_left_running(journey, monkeypatch):
    """The failed switch leaves the old engine recorded, so it is still watched."""
    monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 0)
    old = journey.start()
    journey.ws.send_command_to_stt.side_effect = OSError(
        "synthetic disconnected shutdown"
    )
    journey.spawn_failure.append(OSError("synthetic replacement spawn refusal"))
    config = SimpleNamespace(set=Mock(), save=AsyncMock(return_value=True))
    controller = SimpleNamespace(
        state_manager=journey.state, config_service=config,
        service_manager=SimpleNamespace(remote_stt_launcher=journey.launcher),
    )
    journey.loop.run_until_complete(
        main_module.LogicController._switch_stt_provider(controller, "parakeet_tdt")
    )
    assert journey.state._running_remote_stt_provider == "google_stt", (
        "the switch must leave the engine it could not replace recorded"
    )
    assert old.poll() is None, "the shutdown that never landed leaves it alive"
    count = len(journey.processes)

    old.returncode = 1
    displayed = journey.update()

    assert len(journey.processes) == count + 1, (
        "the engine a failed switch left running must still be watched"
    )
    assert displayed["stt_provider"] == "google_stt"
