"""A settings change that never reached the disk must not restart the program.

Changing the speech-to-text mode writes the new mode to the settings file and
then restarts, because the mode is chosen at startup. If that write failed and
the restart happened anyway, the program would come back on the OLD mode having
already told the user it switched, and the reason would be invisible: nothing
crashed, the file is simply unchanged.

The write reports whether it succeeded (see ConfigService.save), so the restart
can wait for a write that actually happened. Stopping is only half the job: the
new mode was already put into the settings held in memory before the write was
attempted, so stopping without taking it back out leaves the program holding a
mode it never saved. The next attempt then reads the mode it thinks it is
already on and does nothing, and any later unrelated write saves the change the
user was told had not been made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class _Settings:
    """Settings that remember what was written to them.

    A plain mock with a fixed answer for every read cannot show this defect:
    it keeps reporting the old mode however many times the code overwrote it,
    which is exactly the corruption under test.
    """

    def __init__(self, values: dict, save_result):
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


def _controller(save_result, *, zipformer: bool = False):
    """A LogicController stand-in whose settings write succeeds or fails."""
    from main import LogicController

    service_manager = MagicMock()
    if zipformer:
        # The provider is one a launcher knows, so it runs remotely. Starting
        # in the in-process mode makes the switch a mode change.
        settings = _Settings(
            {"stt.mode": "in_process", "stt.provider": "google"}, save_result
        )
        service_manager.remote_stt_launcher.get_provider_by_name.return_value = {
            "name": "zipformer"
        }
    else:
        # No launcher knows this provider, so it is an in-process one. The
        # current mode is remote, which makes this a mode change.
        settings = _Settings(
            {"stt.mode": "remote", "stt.last_provider": "parakeet_tdt"}, save_result
        )
        service_manager.remote_stt_launcher = None

    controller = MagicMock(spec=LogicController)
    controller.config_service = settings
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    controller.service_manager = service_manager
    controller.restart_program = AsyncMock()
    controller._update_zipformer_gpu_config = AsyncMock()
    return controller


@pytest.mark.asyncio
async def test_a_failed_write_does_not_restart_the_program():
    from main import LogicController

    controller = _controller(save_result=False)

    await LogicController._switch_stt_provider(controller, "google_stt")

    controller.restart_program.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_successful_write_still_restarts_the_program():
    from main import LogicController

    controller = _controller(save_result=True)

    await LogicController._switch_stt_provider(controller, "google_stt")

    controller.restart_program.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_write_saves_the_rollback_back_to_disk():
    """A concurrent settings save can capture the never-saved mode in
    its snapshot before this write fails; that snapshot then lands on
    disk after the in-memory rollback. After rolling back, the switch
    saves again so the settings file converges on the rolled-back state
    (review finding wh-google-creds-file-picker.1.8)."""
    from main import LogicController

    controller = _controller(save_result=[False, True])

    await LogicController._switch_stt_provider(controller, "google_stt")

    settings = controller.config_service
    assert settings.save_count == 2
    # The second save ran after the rollback: its snapshot must hold the
    # old mode, not the mode the user was told did not take effect.
    assert settings.save_snapshots[1]["stt.mode"] == "remote"
    controller.restart_program.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_user_is_told_the_change_could_not_be_saved():
    """Silence would leave the user believing the switch took effect."""
    from main import LogicController

    controller = _controller(save_result=False)

    await LogicController._switch_stt_provider(controller, "google_stt")

    messages = [
        call.args[0]
        for call in controller.state_manager.state_to_gui_queue.put_nowait.call_args_list
    ]
    assert any(m.get("action") == "show_notification" for m in messages)
    assert any(
        "not" in m.get("message", "").lower() or "fail" in m.get("message", "").lower()
        for m in messages
    )


@pytest.mark.asyncio
async def test_a_failed_write_leaves_the_settings_as_they_were():
    """Telling the user nothing changed has to be true of memory as well."""
    from main import LogicController

    controller = _controller(save_result=False)

    await LogicController._switch_stt_provider(controller, "google_stt")

    assert controller.config_service.get("stt.mode") == "remote"
    # Absent, not present-and-empty. A setting put back as None reads as
    # missing but cannot be written, so it would break the next save.
    assert "stt.provider" not in controller.config_service.values


@pytest.mark.asyncio
async def test_the_same_switch_can_be_tried_again_after_a_failure():
    """A failed attempt must not make the next one look already done."""
    from main import LogicController

    controller = _controller(save_result=False)
    await LogicController._switch_stt_provider(controller, "google_stt")

    controller.config_service.save_result = True
    await LogicController._switch_stt_provider(controller, "google_stt")

    controller.restart_program.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_write_does_not_leave_the_provider_file_changed():
    """The provider's own settings file is a second, separate write.

    It is not covered by taking the mode back out of memory, so it must not
    happen at all until the write it belongs to has succeeded.
    """
    from main import LogicController

    controller = _controller(save_result=False, zipformer=True)

    await LogicController._switch_stt_provider(controller, "zipformer_gpu")

    controller._update_zipformer_gpu_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_successful_write_does_change_the_provider_file():
    from main import LogicController

    controller = _controller(save_result=True, zipformer=True)

    await LogicController._switch_stt_provider(controller, "zipformer_gpu")

    controller._update_zipformer_gpu_config.assert_awaited_once_with(True)
