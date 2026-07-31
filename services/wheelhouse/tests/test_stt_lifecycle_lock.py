"""One lock serializes the STT lifecycle commands (review finding
wh-google-creds-file-picker.1.3).

The GUI listener starts set_google_credentials_file,
switch_stt_provider, restart_stt_service, and hard_restart_stt_service
as independent asyncio tasks. The credentials handler awaits for up to
ten seconds between reading the selected provider and starting the
replacement process, so without serialization a provider switch, a
second credentials selection, or a restart command can interleave with
that window and leave two providers running or none. Every handler
below must acquire the shared ``_stt_lifecycle_lock`` before touching
provider state, so overlapping commands run one at a time in arrival
order.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


def _service_account_file(tmp_path, name="sa.json"):
    from google_key_fixture import write_service_account_key

    return write_service_account_key(tmp_path / name)


def _controller(current_provider: str = "google_stt"):
    from main import LogicController

    controller = MagicMock(spec=LogicController)

    class _Settings:
        def __init__(self, values):
            self.values = dict(values)
            self.save_count = 0

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            self.values[key] = value

        def unset(self, key):
            self.values.pop(key, None)

        async def save(self) -> bool:
            self.save_count += 1
            return True

    controller.config_service = _Settings(
        {"stt.mode": "remote", "stt.last_provider": current_provider}
    )
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    controller.service_manager = MagicMock()
    launcher = MagicMock()
    launcher.stop_provider = AsyncMock(return_value=True)
    launcher.start_provider = MagicMock(return_value=True)
    launcher.is_running = MagicMock(return_value=False)
    launcher.get_provider_by_name = MagicMock(return_value={"name": "any"})
    controller.service_manager.remote_stt_launcher = launcher
    controller.app = MagicMock()
    controller.app.websocket_manager = MagicMock()
    controller.app.websocket_manager.send_command_to_stt = AsyncMock()
    return controller


async def _held_lock(controller):
    """Give the controller a shared lifecycle lock and acquire it."""
    lock = asyncio.Lock()
    controller._stt_lifecycle_lock = lock
    await lock.acquire()
    return lock


@pytest.mark.asyncio
async def test_credentials_handler_waits_for_lifecycle_lock(tmp_path):
    from main import LogicController

    controller = _controller()
    lock = await _held_lock(controller)
    path = _service_account_file(tmp_path)

    task = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path))
    )
    await asyncio.sleep(0.05)
    # While another lifecycle command holds the lock, nothing may be
    # written or restarted.
    assert controller.config_service.save_count == 0
    launcher = controller.service_manager.remote_stt_launcher
    launcher.stop_provider.assert_not_awaited()

    lock.release()
    await asyncio.wait_for(task, timeout=5)
    assert controller.config_service.save_count == 1
    launcher.start_provider.assert_called_once_with("google_stt")


@pytest.mark.asyncio
async def test_switch_provider_waits_for_lifecycle_lock():
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    lock = await _held_lock(controller)

    task = asyncio.ensure_future(
        LogicController._switch_stt_provider(controller, "parakeet_tdt")
    )
    await asyncio.sleep(0.05)
    launcher = controller.service_manager.remote_stt_launcher
    launcher.stop_provider.assert_not_awaited()
    launcher.start_provider.assert_not_called()

    lock.release()
    await asyncio.wait_for(task, timeout=5)
    launcher.stop_provider.assert_awaited_once_with("google_stt")
    launcher.start_provider.assert_called_once_with("parakeet_tdt")


@pytest.mark.asyncio
async def test_restart_service_waits_for_lifecycle_lock():
    from main import LogicController

    controller = _controller()
    lock = await _held_lock(controller)

    task = asyncio.ensure_future(
        LogicController.restart_stt_service(controller)
    )
    await asyncio.sleep(0.05)
    send = controller.app.websocket_manager.send_command_to_stt
    send.assert_not_awaited()

    lock.release()
    await asyncio.wait_for(task, timeout=5)
    send.assert_awaited_once_with("restart_service")


@pytest.mark.asyncio
async def test_hard_restart_service_waits_for_lifecycle_lock():
    from main import LogicController

    controller = _controller()
    lock = await _held_lock(controller)

    task = asyncio.ensure_future(
        LogicController.hard_restart_stt_service(controller)
    )
    await asyncio.sleep(0.05)
    send = controller.app.websocket_manager.send_command_to_stt
    send.assert_not_awaited()

    lock.release()
    await asyncio.wait_for(task, timeout=5)
    send.assert_awaited_once_with("hard_restart_service")


@pytest.mark.asyncio
async def test_lock_is_created_when_absent(tmp_path):
    """A controller without the lock attribute (a spec'd mock in tests,
    or a controller built before __init__ finished) gets one lazily, so
    the serialization never silently disappears."""
    from main import LogicController

    controller = _controller()
    path = _service_account_file(tmp_path)

    await LogicController._set_google_credentials_file(controller, str(path))

    assert isinstance(controller._stt_lifecycle_lock, asyncio.Lock)


@pytest.mark.asyncio
async def test_queued_switch_sees_the_credentials_restart_result(tmp_path):
    """A switch sent while the credentials handler HOLDS the lock runs
    after it, reads the config state the credentials handler wrote, and
    stops the provider the credentials handler started -- never in
    parallel with it."""
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    path = _service_account_file(tmp_path)
    launcher = controller.service_manager.remote_stt_launcher
    # The old process is still alive on the first check, so the
    # credentials handler enters its wait loop and yields control --
    # the exact window the unserialized switch used to run in.
    launcher.is_running = MagicMock(side_effect=[True, False])

    order: list[str] = []
    real_stop = launcher.stop_provider

    async def recording_stop(name):
        order.append(f"stop:{name}")
        return await real_stop(name)

    launcher.stop_provider = AsyncMock(side_effect=recording_stop)
    launcher.start_provider = MagicMock(
        side_effect=lambda name: order.append(f"start:{name}") or True
    )

    creds = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path))
    )
    # Wait until the credentials handler is past validation, holds the
    # lock, and has stopped the old process -- then send the switch.
    async def _first_stop_recorded():
        while not order:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_first_stop_recorded(), timeout=5)
    switch = asyncio.ensure_future(
        LogicController._switch_stt_provider(controller, "parakeet_tdt")
    )
    await asyncio.wait_for(asyncio.gather(creds, switch), timeout=5)

    assert order == [
        "stop:google_stt",
        "start:google_stt",
        "stop:google_stt",
        "start:parakeet_tdt",
    ]


@pytest.mark.asyncio
async def test_switch_during_validation_defers_the_credentials_restart(
    tmp_path,
):
    """The key-file validation runs BEFORE the lock is taken, with a
    bounded wait, so a hung network read cannot monopolize the lifecycle
    lock (wh-google-creds-file-picker.1.9). A switch arriving during
    that validation may therefore win the lock first; the credentials
    apply phase then reads the post-switch config and defers the
    restart instead of restarting a provider the user just left."""
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    path = _service_account_file(tmp_path)
    launcher = controller.service_manager.remote_stt_launcher

    order: list[str] = []
    real_stop = launcher.stop_provider

    async def recording_stop(name):
        order.append(f"stop:{name}")
        return await real_stop(name)

    launcher.stop_provider = AsyncMock(side_effect=recording_stop)
    launcher.start_provider = MagicMock(
        side_effect=lambda name: order.append(f"start:{name}") or True
    )

    creds = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path))
    )
    # One loop iteration: the credentials task is inside its validation
    # thread and has not taken the lock yet.
    await asyncio.sleep(0)
    switch = asyncio.ensure_future(
        LogicController._switch_stt_provider(controller, "parakeet_tdt")
    )
    await asyncio.wait_for(asyncio.gather(creds, switch), timeout=5)

    # Only the switch touched provider state; google_stt was not
    # restarted behind the user's back.
    assert order == ["stop:google_stt", "start:parakeet_tdt"]
    # The credentials were still saved for the next Google start.
    assert (
        controller.config_service.values["stt.google.credentials_file"]
        == str(path)
    )
    messages = [
        call.args[0]["message"]
        for call in controller.state_manager.state_to_gui_queue.put_nowait
        .call_args_list
        if call.args[0].get("action") == "show_notification"
        and call.args[0].get("title") == "Wheelhouse: Google Cloud Credentials"
    ]
    assert any("next time" in message for message in messages)


@pytest.mark.asyncio
async def test_an_older_pick_finishing_late_does_not_overwrite_a_newer_pick(
    tmp_path, monkeypatch
):
    """Validation runs before the lock, so two overlapping credential
    picks queue for the lock in validation-completion order, not in the
    order the user picked them. Without a freshness check, an older pick
    on a slow path (a network drive) finishing after a newer local pick
    would overwrite the newer selection and restart the provider onto
    the wrong key (review finding wh-google-creds-file-picker.1.13)."""
    import threading

    import main as main_module
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    path_a = _service_account_file(tmp_path, "slow_network_key.json")
    path_b = _service_account_file(tmp_path, "fast_local_key.json")
    launcher = controller.service_manager.remote_stt_launcher

    real_validate = main_module._google_key_file_error
    gate = threading.Event()

    def slow_validation_for_a(candidate):
        if candidate == str(path_a):
            gate.wait(timeout=10)
        return real_validate(candidate)

    monkeypatch.setattr(
        main_module, "_google_key_file_error", slow_validation_for_a
    )

    try:
        pick_a = asyncio.ensure_future(
            LogicController._set_google_credentials_file(
                controller, str(path_a)
            )
        )
        await asyncio.sleep(0.05)  # A is inside its validation thread
        pick_b = asyncio.ensure_future(
            LogicController._set_google_credentials_file(
                controller, str(path_b)
            )
        )
        await asyncio.wait_for(pick_b, timeout=5)
        assert (
            controller.config_service.values["stt.google.credentials_file"]
            == str(path_b)
        )
    finally:
        gate.set()
    await asyncio.wait_for(pick_a, timeout=5)

    # The newer pick B stays installed everywhere; the older pick A was
    # discarded, did not save, and did not restart the provider again.
    assert (
        controller.config_service.values["stt.google.credentials_file"]
        == str(path_b)
    )
    assert launcher.google_credentials_file == str(path_b)
    assert controller.config_service.save_count == 1
    launcher.start_provider.assert_called_once_with("google_stt")
    messages = [
        call.args[0]["message"]
        for call in controller.state_manager.state_to_gui_queue.put_nowait
        .call_args_list
        if call.args[0].get("action") == "show_notification"
    ]
    saved_notices = [m for m in messages if "Credentials saved" in m]
    assert len(saved_notices) == 1


def _saved_notices(controller):
    messages = [
        call.args[0]["message"]
        for call in controller.state_manager.state_to_gui_queue.put_nowait
        .call_args_list
        if call.args[0].get("action") == "show_notification"
    ]
    return [m for m in messages if "Credentials saved" in m]


@pytest.mark.asyncio
async def test_a_pick_superseded_during_its_save_does_not_restart(tmp_path):
    """The generation check at lock entry is not enough: a newer pick B
    can arrive while the older pick A already owns the lifecycle lock
    and is suspended inside the settings save. Without a freshness
    fence after the save, A would install its superseded key on the
    launcher and burn a full stop/start cycle before B can apply
    (review finding wh-google-creds-file-picker.1.20)."""
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    path_a = _service_account_file(tmp_path, "older_key.json")
    path_b = _service_account_file(tmp_path, "newer_key.json")
    launcher = controller.service_manager.remote_stt_launcher

    first_save_started = asyncio.Event()
    release_save = asyncio.Event()

    class _GatedSaveSettings:
        def __init__(self, values):
            self.values = dict(values)
            self.save_count = 0

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            self.values[key] = value

        def unset(self, key):
            self.values.pop(key, None)

        async def save(self) -> bool:
            self.save_count += 1
            if self.save_count == 1:
                first_save_started.set()
                await release_save.wait()
            return True

    controller.config_service = _GatedSaveSettings(
        {"stt.mode": "remote", "stt.last_provider": "google_stt"}
    )

    pick_a = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path_a))
    )
    await asyncio.wait_for(first_save_started.wait(), timeout=2)
    pick_b = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path_b))
    )
    await asyncio.sleep(0.05)  # B takes its generation and queues on the lock
    release_save.set()
    await asyncio.wait_for(pick_a, timeout=5)
    await asyncio.wait_for(pick_b, timeout=5)

    # Only the newer pick B may restart the provider; A stops at the
    # post-save freshness fence.
    assert (
        controller.config_service.values["stt.google.credentials_file"]
        == str(path_b)
    )
    assert launcher.google_credentials_file == str(path_b)
    launcher.start_provider.assert_called_once_with("google_stt")
    assert launcher.stop_provider.await_count == 1
    assert len(_saved_notices(controller)) == 1


@pytest.mark.asyncio
async def test_a_pick_superseded_during_its_stop_does_not_start_the_old_key(
    tmp_path
):
    """A newer pick B arriving while the older pick A is suspended in
    stop_provider must keep A from starting the provider on the
    superseded key; B performs the one restart
    (review finding wh-google-creds-file-picker.1.20)."""
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    path_a = _service_account_file(tmp_path, "older_key.json")
    path_b = _service_account_file(tmp_path, "newer_key.json")
    launcher = controller.service_manager.remote_stt_launcher

    stop_started = asyncio.Event()
    release_stop = asyncio.Event()

    async def gated_first_stop(provider):
        if not stop_started.is_set():
            stop_started.set()
            await release_stop.wait()
        return True

    launcher.stop_provider = AsyncMock(side_effect=gated_first_stop)

    pick_a = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path_a))
    )
    await asyncio.wait_for(stop_started.wait(), timeout=2)
    pick_b = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path_b))
    )
    await asyncio.sleep(0.05)  # B takes its generation and queues on the lock
    release_stop.set()
    await asyncio.wait_for(pick_a, timeout=5)
    await asyncio.wait_for(pick_b, timeout=5)

    assert (
        controller.config_service.values["stt.google.credentials_file"]
        == str(path_b)
    )
    assert launcher.google_credentials_file == str(path_b)
    launcher.start_provider.assert_called_once_with("google_stt")
    assert len(_saved_notices(controller)) == 1


@pytest.mark.asyncio
async def test_a_failed_successor_starts_the_engine_the_superseded_pick_stopped(
    tmp_path
):
    """The freshness fences assume the newer pick will perform the
    restart, but the newer pick can fail validation. If the older pick
    already stopped the provider and skipped its start at the post-stop
    fence, the failing newer pick must start the engine instead of
    leaving it stopped with no owner
    (review finding wh-google-creds-file-picker.1.21)."""
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    path_a = _service_account_file(tmp_path, "older_valid_key.json")
    path_b = tmp_path / "newer_invalid_key.json"
    path_b.write_text("this is not json", encoding="utf-8")
    launcher = controller.service_manager.remote_stt_launcher

    stop_started = asyncio.Event()
    release_stop = asyncio.Event()

    async def gated_first_stop(provider):
        if not stop_started.is_set():
            stop_started.set()
            await release_stop.wait()
        return True

    launcher.stop_provider = AsyncMock(side_effect=gated_first_stop)

    pick_a = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path_a))
    )
    await asyncio.wait_for(stop_started.wait(), timeout=2)
    pick_b = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path_b))
    )
    await asyncio.sleep(0.1)  # B takes its generation and fails validation
    release_stop.set()
    await asyncio.wait_for(pick_a, timeout=5)
    await asyncio.wait_for(pick_b, timeout=5)

    # The invalid pick B never saved; A's valid key stays installed and
    # the engine A stopped is running again.
    assert (
        controller.config_service.values["stt.google.credentials_file"]
        == str(path_a)
    )
    assert launcher.google_credentials_file == str(path_a)
    launcher.start_provider.assert_called_once_with("google_stt")


@pytest.mark.asyncio
async def test_a_successor_whose_save_fails_starts_the_engine(tmp_path):
    """A newer pick that validates but cannot save rolls back and
    returns; if it superseded an older pick at the post-stop fence, it
    must start the engine that pick stopped
    (review finding wh-google-creds-file-picker.1.21)."""
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    path_a = _service_account_file(tmp_path, "older_valid_key.json")
    path_b = _service_account_file(tmp_path, "newer_valid_key.json")
    launcher = controller.service_manager.remote_stt_launcher

    class _SecondSaveFailsSettings:
        def __init__(self, values):
            self.values = dict(values)
            self.save_count = 0

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            self.values[key] = value

        def unset(self, key):
            self.values.pop(key, None)

        async def save(self) -> bool:
            self.save_count += 1
            # Call 1 is A's save; call 2 is B's save (fails); call 3 is
            # B's rollback resave.
            return self.save_count != 2

    controller.config_service = _SecondSaveFailsSettings(
        {"stt.mode": "remote", "stt.last_provider": "google_stt"}
    )

    stop_started = asyncio.Event()
    release_stop = asyncio.Event()

    async def gated_first_stop(provider):
        if not stop_started.is_set():
            stop_started.set()
            await release_stop.wait()
        return True

    launcher.stop_provider = AsyncMock(side_effect=gated_first_stop)

    pick_a = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path_a))
    )
    await asyncio.wait_for(stop_started.wait(), timeout=2)
    pick_b = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path_b))
    )
    await asyncio.sleep(0.1)  # B takes its generation and queues on the lock
    release_stop.set()
    await asyncio.wait_for(pick_a, timeout=5)
    await asyncio.wait_for(pick_b, timeout=5)

    # B's failed save rolled back to A's value (the last saved state);
    # the engine A stopped is running again.
    assert (
        controller.config_service.values["stt.google.credentials_file"]
        == str(path_a)
    )
    assert launcher.google_credentials_file == str(path_a)
    launcher.start_provider.assert_called_once_with("google_stt")


@pytest.mark.asyncio
async def test_a_successor_failing_after_the_fence_pays_the_owed_restart(
    tmp_path, monkeypatch
):
    """The other ordering of the failed-successor race: the newer pick's
    validation outlives the older pick's whole stop-and-fence, so the
    fence has already recorded the restart debt by the time the newer
    pick fails. The failing pick must then pay the debt under the lock
    and start the engine (wh-google-creds-file-picker.1.21)."""
    import threading

    import main as main_module
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    path_a = _service_account_file(tmp_path, "older_valid_key.json")
    path_b = tmp_path / "newer_invalid_key.json"
    path_b.write_text("this is not json", encoding="utf-8")
    launcher = controller.service_manager.remote_stt_launcher

    stop_started = asyncio.Event()
    release_stop = asyncio.Event()

    async def gated_first_stop(provider):
        if not stop_started.is_set():
            stop_started.set()
            await release_stop.wait()
        return True

    launcher.stop_provider = AsyncMock(side_effect=gated_first_stop)

    real_validation = main_module._google_key_file_error
    release_b_validation = threading.Event()

    def gated_validation(path):
        if str(path) == str(path_b):
            release_b_validation.wait(timeout=5)
        return real_validation(path)

    monkeypatch.setattr(
        main_module, "_google_key_file_error", gated_validation
    )

    pick_a = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path_a))
    )
    await asyncio.wait_for(stop_started.wait(), timeout=2)
    pick_b = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path_b))
    )
    await asyncio.sleep(0.1)  # B takes its generation, hangs in validation
    release_stop.set()
    # A runs to completion FIRST: its post-stop fence sees a live
    # successor and records the restart debt.
    await asyncio.wait_for(pick_a, timeout=5)
    launcher.start_provider.assert_not_called()
    release_b_validation.set()
    await asyncio.wait_for(pick_b, timeout=5)

    # The invalid pick B never saved; A's valid key stays installed and
    # B paid the restart debt A recorded at its fence.
    assert (
        controller.config_service.values["stt.google.credentials_file"]
        == str(path_a)
    )
    assert launcher.google_credentials_file == str(path_a)
    launcher.start_provider.assert_called_once_with("google_stt")
