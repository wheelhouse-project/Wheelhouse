"""Startup makes the sound-pause decision before any service starts.

wh-audio-suppression-auto. The audio monitor publishes its first answer as
soon as the services run. If the decision arrived after that, the first
report would be judged by the default instead of by what Windows said, and a
machine with an echo canceller could pause listening once at every start.

These tests drive LogicController.main through the same harness the STT-mode
tests use: a MagicMock controller whose start_services raises _StopStartup,
so startup stops at the first statement after the decision.
"""

from __future__ import annotations

import asyncio

from unittest.mock import AsyncMock, MagicMock

from services.wheelhouse.config_service import ConfigService

from tests.test_stt_mode_normalization import (
    _config_service,
    _run_startup,
    _startup_controller,
)


MODE_LINE = 'mode = "remote"\n'


class TestTheDecisionReachesTheStateManager:
    """LogicController.main applies the decision before start_services."""

    def test_startup_applies_the_decision_before_services_start(self, tmp_path):
        controller = _startup_controller(_config_service(tmp_path, MODE_LINE))
        order = MagicMock()
        order.attach_mock(
            controller.state_manager.apply_audio_suppression_decision, "apply"
        )
        order.attach_mock(controller.service_manager.start_services, "start_services")

        asyncio.run(_run_startup(controller, AsyncMock(return_value=False)))

        assert [name for name, _, _ in order.mock_calls] == [
            "apply",
            "start_services",
        ]
        controller.state_manager.apply_audio_suppression_decision.assert_called_once_with(
            False
        )

    def test_startup_passes_the_setting_with_the_auto_default(self, tmp_path):
        """A settings file with no key gives the decision the string "auto"."""
        controller = _startup_controller(_config_service(tmp_path, MODE_LINE))
        decide = AsyncMock(return_value=True)

        asyncio.run(_run_startup(controller, decide))

        decide.assert_awaited_once_with("auto")

    def test_a_false_setting_reaches_the_decision_unchanged(self, tmp_path):
        path = tmp_path / "config.toml"
        # The key must precede [stt], or TOML makes it a member of that table.
        path.write_text(
            "ENABLE_AUDIO_SUPPRESSION = false\n"
            "[stt]\n"
            'mode = "remote"\n'
            'last_provider = "parakeet_tdt"\n'
            'provider = "google"\n',
            encoding="utf-8",
        )
        controller = _startup_controller(ConfigService(str(path)))
        decide = AsyncMock(return_value=True)

        asyncio.run(_run_startup(controller, decide))

        decide.assert_awaited_once_with(False)
