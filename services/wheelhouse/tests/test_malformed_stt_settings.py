"""A scalar where the [stt] section belongs must not close the program.

The STT settings commands write through ConfigService.set. That method
walks a dotted key and assigns into whatever each level holds, so a
settings file that carries a plain value in place of the section raises
TypeError instead of writing. The commands run as tasks whose done
callback turns any exception into a shutdown, so the unguarded write
closed the whole program (wh-remote-stt-robustness, Gap B).

These tests drive the real ConfigService rather than a fake. A fake that
keys a flat dict by the whole dotted string cannot reproduce the
traversal, and so cannot raise this error at all.

The Google credentials picker was a third command covered here. It is
gone: wh-remove-restart-credentials-items deleted
_set_google_credentials_file on David's order (QUESTIONS-2026-09-04.md
item 20), and its cases here went with it.

A TestModeChange class covered the same failure on the pair of writes the
STT mode change made. wh-in-process-capture-removal deleted that branch of
_switch_stt_provider, so there is no second pair of writes left to guard:
a provider no launcher knows now takes the remote path like any other name.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# A settings file that puts a value where the section belongs. Valid
# TOML, so the program loads it and runs.
MALFORMED_SETTINGS = 'stt = "whisper"\n'


def _malformed_config_service(tmp_path):
    """A real ConfigService loaded from a settings file whose stt key
    holds a string instead of a table.

    Only save() is replaced, with an answer that always succeeds: these
    tests are about the write that happens before any save, and a real
    save would make a failure to store look like a failure to write to
    disk. The machine's own config.toml is never read -- the path is
    always under tmp_path.
    """
    from config_service import ConfigService

    path = tmp_path / "config.toml"
    path.write_text(MALFORMED_SETTINGS, encoding="utf-8")
    service = ConfigService(str(path))
    service.save = AsyncMock(return_value=True)
    return service


def _notifications(controller):
    return [
        call.args[0]
        for call in (
            controller.state_manager.state_to_gui_queue.put_nowait.call_args_list
        )
        if call.args and call.args[0].get("action") == "show_notification"
    ]


def _switch_controller(tmp_path, *, start_result=True):
    """A controller for _switch_stt_provider whose settings are malformed.

    Shaped after the mock_dependencies fixture in
    test_ui_provider_switching.py: a spec'd mock controller, a launcher
    that reports the target as a known remote provider, and the runtime
    record as the source of the currently running provider.
    """
    from main import LogicController

    launcher = MagicMock()
    launcher.stop_provider = AsyncMock(return_value=True)
    launcher.start_provider = MagicMock(return_value=start_result)
    launcher.get_provider_by_name.return_value = {
        "name": "parakeet_tdt",
        "display_name": "Parakeet v3 (GPU)",
    }

    controller = MagicMock(spec=LogicController)
    controller.config_service = _malformed_config_service(tmp_path)
    controller.service_manager = MagicMock()
    controller.service_manager.remote_stt_launcher = launcher
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    controller.state_manager._get_current_stt_provider.return_value = "google_stt"
    controller.shutdown_event = MagicMock()
    controller.shutdown_event.is_set.return_value = False
    return controller


class TestProviderSelection:
    """Steps to reproduce, and what each one produced before the fix.

    1. Put a plain value where the STT section belongs in
       services/wheelhouse/config.toml -- replace the whole [stt] table
       with the single line: stt = "whisper". This is valid TOML, and
       ConfigService.get reads through it as a missing key, so Wheelhouse
       starts and transcribes normally.
    2. Start Wheelhouse and say "switch to parakeet" (or pick another
       engine from the tray's STT Provider menu).
    3. LogicController._switch_stt_provider reaches
       config_service.set("stt.last_provider", "parakeet_tdt").
       ConfigService.set walks the key, finds the string "whisper" where
       the table should be, and raises
       TypeError: 'str' object does not support item assignment.
    4. The command runs as a task created by
       create_task_with_error_handling, so _handle_task_completion catches
       the TypeError and calls request_shutdown. Wheelhouse closes.

    """

    @pytest.mark.asyncio
    async def test_a_scalar_stt_setting_does_not_close_the_program(self, tmp_path):
        from main import LogicController

        controller = _switch_controller(tmp_path)

        await LogicController._switch_stt_provider(controller, "parakeet_tdt")

    @pytest.mark.asyncio
    async def test_a_scalar_stt_setting_is_reported_to_the_user(self, tmp_path):
        from main import LogicController

        controller = _switch_controller(tmp_path)

        await LogicController._switch_stt_provider(controller, "parakeet_tdt")

        messages = [n["message"] for n in _notifications(controller)]
        assert messages, "the refused write told the user nothing"
        assert any("settings file" in m for m in messages), messages

    @pytest.mark.asyncio
    async def test_a_scalar_stt_setting_leaves_the_settings_alone(self, tmp_path):
        from main import LogicController

        controller = _switch_controller(tmp_path)

        await LogicController._switch_stt_provider(controller, "parakeet_tdt")

        assert controller.config_service.get_config()["stt"] == "whisper"

    @pytest.mark.asyncio
    async def test_a_refused_write_is_never_saved(self, tmp_path):
        from main import LogicController

        controller = _switch_controller(tmp_path)

        await LogicController._switch_stt_provider(controller, "parakeet_tdt")

        controller.config_service.save.assert_not_called()

