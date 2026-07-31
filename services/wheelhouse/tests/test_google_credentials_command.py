"""The set_google_credentials_file command (wh-google-creds-file-picker).

The GUI sends the path the user picked in the file dialog. The Logic
process checks the file is a Google service-account key, saves the path
under stt.google.credentials_file, and restarts the Google speech
provider process when it is the one currently running so the new key
takes effect immediately.

A path that fails the checks is never saved: saving a bad path would
break the next provider start with no visible cause. A write that fails
must also take the value back out of the settings held in memory, for
the same reason the mode-switch rollback exists (see
test_config_save_failure_stops_restart.py).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


class _Settings:
    """Settings that remember what was written to them.

    save_result may be a bool (every save answers the same) or a list of
    bools consumed one per save call (exhausted -> True). Each save also
    records a snapshot of the values it would have written, so a test
    can check what a given save would have put on disk.
    """

    def __init__(self, values: dict, save_result=True):
        self.values = dict(values)
        self.save_result = save_result
        self.save_count = 0
        self.save_snapshots: list[dict] = []

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def unset(self, key):
        self.values.pop(key, None)

    async def save(self) -> bool:
        self.save_count += 1
        self.save_snapshots.append(dict(self.values))
        if isinstance(self.save_result, list):
            if self.save_result:
                return self.save_result.pop(0)
            return True
        return self.save_result


def _service_account_file(tmp_path, name="sa.json"):
    from google_key_fixture import write_service_account_key

    return write_service_account_key(tmp_path / name)


def _controller(
    *,
    save_result=True,
    current_provider: str = "google_stt",
    mode: str = "remote",
    with_launcher: bool = True,
):
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller.config_service = _Settings(
        {"stt.mode": mode, "stt.last_provider": current_provider},
        save_result,
    )
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    controller.service_manager = MagicMock()
    if with_launcher:
        launcher = MagicMock()
        launcher.stop_provider = AsyncMock(return_value=True)
        launcher.start_provider = MagicMock(return_value=True)
        # After a shutdown the old process is gone by default; tests for
        # the stop/start race override this.
        launcher.is_running = MagicMock(return_value=False)
        controller.service_manager.remote_stt_launcher = launcher
    else:
        controller.service_manager.remote_stt_launcher = None
    return controller


def _notifications(controller):
    return [
        call.args[0]
        for call in controller.state_manager.state_to_gui_queue.put_nowait.call_args_list
        if call.args and call.args[0].get("action") == "show_notification"
    ]


@pytest.mark.asyncio
async def test_valid_key_file_is_saved(tmp_path):
    from main import LogicController

    controller = _controller()
    path = _service_account_file(tmp_path)

    await LogicController._set_google_credentials_file(controller, str(path))

    assert (
        controller.config_service.values["stt.google.credentials_file"]
        == str(path)
    )
    assert controller.config_service.save_count == 1


@pytest.mark.asyncio
async def test_google_provider_is_restarted_when_active(tmp_path):
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    path = _service_account_file(tmp_path)

    await LogicController._set_google_credentials_file(controller, str(path))

    launcher = controller.service_manager.remote_stt_launcher
    launcher.stop_provider.assert_awaited_once_with("google_stt")
    launcher.start_provider.assert_called_once_with("google_stt")


@pytest.mark.asyncio
async def test_launcher_attribute_updated_before_restart(tmp_path):
    """The launcher holds the value it was built with; without this
    update the restarted provider would use the old key file."""
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    path = _service_account_file(tmp_path)

    await LogicController._set_google_credentials_file(controller, str(path))

    launcher = controller.service_manager.remote_stt_launcher
    assert launcher.google_credentials_file == str(path)


@pytest.mark.asyncio
async def test_no_restart_when_other_provider_active(tmp_path):
    from main import LogicController

    controller = _controller(current_provider="parakeet_tdt")
    path = _service_account_file(tmp_path)

    await LogicController._set_google_credentials_file(controller, str(path))

    launcher = controller.service_manager.remote_stt_launcher
    launcher.stop_provider.assert_not_awaited()
    launcher.start_provider.assert_not_called()
    # Saved regardless: the key applies the next time Google starts.
    assert (
        controller.config_service.values["stt.google.credentials_file"]
        == str(path)
    )


@pytest.mark.asyncio
async def test_missing_file_is_not_saved(tmp_path):
    from main import LogicController

    controller = _controller()

    await LogicController._set_google_credentials_file(
        controller, str(tmp_path / "nope.json")
    )

    assert "stt.google.credentials_file" not in controller.config_service.values
    assert controller.config_service.save_count == 0
    assert _notifications(controller)


@pytest.mark.asyncio
async def test_non_json_file_is_not_saved(tmp_path):
    from main import LogicController

    controller = _controller()
    path = tmp_path / "not_json.json"
    path.write_text("this is not json", encoding="utf-8")

    await LogicController._set_google_credentials_file(controller, str(path))

    assert "stt.google.credentials_file" not in controller.config_service.values
    assert controller.config_service.save_count == 0
    assert _notifications(controller)


@pytest.mark.asyncio
async def test_wrong_json_type_is_not_saved(tmp_path):
    """A JSON file that is not a service-account key (e.g. an OAuth client
    file, which Google's console also offers for download) is refused with
    a notification instead of being saved and failing at provider start."""
    from main import LogicController

    controller = _controller()
    path = tmp_path / "oauth.json"
    path.write_text(
        json.dumps({"type": "authorized_user"}), encoding="utf-8"
    )

    await LogicController._set_google_credentials_file(controller, str(path))

    assert "stt.google.credentials_file" not in controller.config_service.values
    assert controller.config_service.save_count == 0
    assert _notifications(controller)


@pytest.mark.asyncio
async def test_key_missing_required_fields_is_not_saved(tmp_path):
    """A JSON object whose only fields are type and project_id passes
    the shallow type check but cannot authenticate (no client_email, no
    private key). It must be refused at pick time instead of breaking
    silently at the first utterance (review finding
    wh-google-creds-file-picker.1.4)."""
    from main import LogicController

    controller = _controller()
    path = tmp_path / "incomplete.json"
    path.write_text(
        json.dumps({"type": "service_account", "project_id": "demo"}),
        encoding="utf-8",
    )

    await LogicController._set_google_credentials_file(controller, str(path))

    assert "stt.google.credentials_file" not in controller.config_service.values
    assert controller.config_service.save_count == 0
    assert _notifications(controller)


@pytest.mark.asyncio
async def test_key_with_invalid_private_key_material_is_not_saved(tmp_path):
    """A key with every required field but garbage where the private key
    belongs is refused: Google's loader cannot parse it, so saving it
    would leave the engine selected, apparently ready, and unable to
    transcribe (review finding wh-google-creds-file-picker.1.4)."""
    from google_key_fixture import write_service_account_key
    from main import LogicController

    controller = _controller()
    path = write_service_account_key(
        tmp_path / "badkey.json",
        private_key=(
            "-----BEGIN PRIVATE KEY-----\nnot a real key\n"
            "-----END PRIVATE KEY-----\n"
        ),
    )

    await LogicController._set_google_credentials_file(controller, str(path))

    assert "stt.google.credentials_file" not in controller.config_service.values
    assert controller.config_service.save_count == 0
    assert _notifications(controller)


@pytest.mark.asyncio
async def test_empty_path_is_ignored(tmp_path):
    from main import LogicController

    controller = _controller()

    await LogicController._set_google_credentials_file(controller, "")

    assert controller.config_service.save_count == 0
    launcher = controller.service_manager.remote_stt_launcher
    launcher.start_provider.assert_not_called()


@pytest.mark.asyncio
async def test_failed_save_rolls_back_and_does_not_restart(tmp_path):
    from main import LogicController

    controller = _controller(save_result=False)
    path = _service_account_file(tmp_path)

    await LogicController._set_google_credentials_file(controller, str(path))

    # The in-memory value must not survive a failed write: a later
    # unrelated save would otherwise write the value the user was told
    # could not be saved.
    assert "stt.google.credentials_file" not in controller.config_service.values
    launcher = controller.service_manager.remote_stt_launcher
    launcher.stop_provider.assert_not_awaited()
    launcher.start_provider.assert_not_called()
    assert _notifications(controller)


@pytest.mark.asyncio
async def test_failed_start_notifies_failure(tmp_path):
    """start_provider returns False on unknown provider, unassigned port,
    or a failed spawn; the user must not be told the engine is
    restarting when it is not (review finding
    wh-google-creds-file-picker.1.1)."""
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    launcher = controller.service_manager.remote_stt_launcher
    launcher.start_provider = MagicMock(return_value=False)
    path = _service_account_file(tmp_path)

    await LogicController._set_google_credentials_file(controller, str(path))

    messages = [n["message"] for n in _notifications(controller)]
    assert any("could not be restarted" in m for m in messages)
    assert not any("Restarting the Google speech engine" in m for m in messages)


@pytest.mark.asyncio
async def test_waits_for_provider_exit_before_start(tmp_path):
    """stop_provider only sends the shutdown command and returns; if
    start_provider runs while the old process is still alive, its
    already-running check returns True without spawning a replacement
    and the engine stays down (review finding
    wh-google-creds-file-picker.1.2)."""
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    launcher = controller.service_manager.remote_stt_launcher
    launcher.is_running = MagicMock(side_effect=[True, True, False])

    path = _service_account_file(tmp_path)
    await LogicController._set_google_credentials_file(controller, str(path))

    # The handler kept polling until the old process was gone, and only
    # then started the replacement.
    assert launcher.is_running.call_count == 3
    launcher.start_provider.assert_called_once_with("google_stt")


@pytest.mark.asyncio
async def test_stuck_provider_skips_start_and_notifies(tmp_path, monkeypatch):
    """If the old process never exits, starting a replacement is
    pointless (the already-running check would ignore the call); the
    user is told the credentials are saved but the engine did not
    restart."""
    import main as main_module
    from main import LogicController

    monkeypatch.setattr(main_module, "_GOOGLE_RESTART_WAIT_S", 0.3)
    controller = _controller(current_provider="google_stt")
    launcher = controller.service_manager.remote_stt_launcher
    launcher.is_running = MagicMock(return_value=True)

    path = _service_account_file(tmp_path)
    await LogicController._set_google_credentials_file(controller, str(path))

    launcher.start_provider.assert_not_called()
    messages = [n["message"] for n in _notifications(controller)]
    assert any("did not restart" in m for m in messages)
    # The credentials themselves were still saved.
    assert (
        controller.config_service.values["stt.google.credentials_file"]
        == str(path)
    )


@pytest.mark.asyncio
async def test_oversized_key_file_is_not_saved(tmp_path):
    """A real key is a few kilobytes. A file too large to be one is
    refused by its size alone, before being read, so the Logic process
    never loads an arbitrarily large file into memory (review finding
    wh-google-creds-file-picker.1.7)."""
    from main import LogicController

    controller = _controller()
    # A key that would pass every other check, padded past the size cap:
    # only the size check can be the reason it is refused.
    from google_key_fixture import service_account_key_dict

    padded = service_account_key_dict(padding="x" * 1_000_001)
    path = tmp_path / "huge.json"
    path.write_text(json.dumps(padded), encoding="utf-8")

    await LogicController._set_google_credentials_file(controller, str(path))

    assert "stt.google.credentials_file" not in controller.config_service.values
    assert controller.config_service.save_count == 0
    assert _notifications(controller)


@pytest.mark.asyncio
async def test_slow_key_read_does_not_block_the_event_loop(
    tmp_path, monkeypatch
):
    """The file checks run in a worker thread. A key file on a slow or
    unreachable network drive must stall only its own command, never the
    Logic event loop that routes every voice command (review finding
    wh-google-creds-file-picker.1.7)."""
    import threading

    import main as main_module
    from main import LogicController

    gate = threading.Event()
    started = threading.Event()

    def slow_check(path):
        started.set()
        gate.wait(5)
        return None

    monkeypatch.setattr(main_module, "_google_key_file_error", slow_check)
    controller = _controller()
    path = _service_account_file(tmp_path)

    task = asyncio.ensure_future(
        LogicController._set_google_credentials_file(controller, str(path))
    )
    # The check is blocked on the gate; the event loop must keep turning.
    await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=5)
    await asyncio.sleep(0.05)
    assert not task.done()

    gate.set()
    await asyncio.wait_for(task, timeout=5)
    assert (
        controller.config_service.values["stt.google.credentials_file"]
        == str(path)
    )


@pytest.mark.asyncio
async def test_failed_save_writes_the_rollback_back_to_disk(tmp_path):
    """A concurrent settings save can capture the new path in its
    snapshot before this handler's save fails; that snapshot then lands
    on disk after the in-memory rollback, persisting the path the user
    was told was not saved. After rolling back, the handler saves again
    so the settings file converges on the rolled-back state (review
    finding wh-google-creds-file-picker.1.8)."""
    from main import LogicController

    controller = _controller(save_result=[False, True])
    path = _service_account_file(tmp_path)

    await LogicController._set_google_credentials_file(controller, str(path))

    settings = controller.config_service
    assert settings.save_count == 2
    # The second save ran after the rollback, so its snapshot must not
    # contain the refused path.
    assert "stt.google.credentials_file" not in settings.save_snapshots[1]
    assert "stt.google.credentials_file" not in settings.values
    launcher = controller.service_manager.remote_stt_launcher
    launcher.start_provider.assert_not_called()


@pytest.mark.asyncio
async def test_command_is_registered(tmp_path):
    """The GUI command name maps to the handler."""
    import inspect

    from main import LogicController

    source = inspect.getsource(LogicController)
    assert "set_google_credentials_file" in source
    assert "_set_google_credentials_file" in source


@pytest.mark.asyncio
async def test_validation_does_not_wait_for_the_lifecycle_lock(tmp_path):
    """Validating the picked file reads it from disk, and a key on an
    unreachable network drive can hang those reads indefinitely. The
    validation must therefore run BEFORE the shared STT lifecycle lock
    is taken, or a hung read would block every later provider switch,
    restart, and credentials selection behind it
    (review finding wh-google-creds-file-picker.1.9)."""
    from main import LogicController

    controller = _controller()
    controller._stt_lifecycle_lock = asyncio.Lock()

    async with controller._stt_lifecycle_lock:
        # Another lifecycle command holds the lock the whole time; a
        # validation failure must still complete and tell the user.
        task = asyncio.get_running_loop().create_task(
            LogicController._set_google_credentials_file(
                controller, str(tmp_path / "missing.json")
            )
        )
        await asyncio.wait_for(task, timeout=1.0)

    assert controller.config_service.save_count == 0
    messages = [n.get("message", "") for n in _notifications(controller)]
    assert any("not found" in m.lower() for m in messages)


@pytest.mark.asyncio
async def test_a_hung_file_read_times_out_and_reports(monkeypatch):
    """A filesystem call against a disconnected network share can stay
    pending for minutes. The handler must give up after a bounded wait
    and tell the user, not hang silently
    (review finding wh-google-creds-file-picker.1.9)."""
    import threading

    import main as main_module
    from main import LogicController

    gate = threading.Event()

    def hung_validation(path):
        gate.wait()
        return None

    monkeypatch.setattr(main_module, "_google_key_file_error", hung_validation)
    monkeypatch.setattr(
        main_module, "_GOOGLE_KEY_VALIDATE_TIMEOUT_S", 0.1, raising=False
    )

    controller = _controller()
    try:
        await asyncio.wait_for(
            LogicController._set_google_credentials_file(
                controller, "Z:/keys/sa.json"
            ),
            timeout=2.0,
        )
    finally:
        gate.set()  # let the stuck worker thread finish

    assert controller.config_service.save_count == 0
    messages = [n.get("message", "") for n in _notifications(controller)]
    assert any("timed out" in m.lower() for m in messages)
