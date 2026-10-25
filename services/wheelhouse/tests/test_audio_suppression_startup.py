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


class TestTheSettingGatesTheExclusiveSpeakerPause:
    """LogicController.main hands the exclusive-speaker pause its setting gate.

    wh-kodi-playback-echo-stutter: the pause applies unless
    ENABLE_AUDIO_SUPPRESSION is false, whatever Windows said about the
    microphone, so the gate comes from the setting and not from the decision.
    """

    def _gate_after_startup(self, tmp_path, setting_line, decision):
        path = tmp_path / "config.toml"
        path.write_text(
            setting_line
            + "[stt]\n"
            'mode = "remote"\n'
            'last_provider = "parakeet_tdt"\n'
            'provider = "google"\n',
            encoding="utf-8",
        )
        controller = _startup_controller(ConfigService(str(path)))
        asyncio.run(_run_startup(controller, AsyncMock(return_value=decision)))
        return controller.state_manager.apply_exclusive_speaker_pause_gate

    def test_no_setting_gives_an_open_gate_even_when_the_decision_is_off(self, tmp_path):
        gate = self._gate_after_startup(tmp_path, "", False)
        gate.assert_called_once_with(True)

    def test_auto_gives_an_open_gate(self, tmp_path):
        gate = self._gate_after_startup(
            tmp_path, 'ENABLE_AUDIO_SUPPRESSION = "auto"\n', False
        )
        gate.assert_called_once_with(True)

    def test_true_gives_an_open_gate(self, tmp_path):
        gate = self._gate_after_startup(
            tmp_path, "ENABLE_AUDIO_SUPPRESSION = true\n", True
        )
        gate.assert_called_once_with(True)

    def test_false_gives_a_closed_gate(self, tmp_path):
        gate = self._gate_after_startup(
            tmp_path, "ENABLE_AUDIO_SUPPRESSION = false\n", False
        )
        gate.assert_called_once_with(False)

    def test_the_gate_is_set_before_services_start(self, tmp_path):
        controller = _startup_controller(_config_service(tmp_path, MODE_LINE))
        order = MagicMock()
        order.attach_mock(
            controller.state_manager.apply_exclusive_speaker_pause_gate, "gate"
        )
        order.attach_mock(controller.service_manager.start_services, "start_services")

        asyncio.run(_run_startup(controller, AsyncMock(return_value=True)))

        assert [name for name, _, _ in order.mock_calls] == ["gate", "start_services"]


class TestTheDetectorAnswersBeforeTheProviderStarts:
    """LogicController.main starts the exclusive-speaker detector first.

    wh-kodi-playback-echo-stutter.1.1: a provider that connects while the
    detector has no answer could have its speech acted on while an app holds
    the speaker. So main applies the gate, starts the detector and (gate on)
    waits for its first answer before it starts the provider.
    """

    @staticmethod
    def _config(tmp_path, setting_line):
        path = tmp_path / "config.toml"
        path.write_text(
            setting_line
            + "[stt]\n"
            'mode = "remote"\n'
            'last_provider = "parakeet_tdt"\n'
            'provider = "google"\n',
            encoding="utf-8",
        )
        return ConfigService(str(path))

    def test_an_app_holding_the_speaker_is_known_when_the_provider_starts(
        self, tmp_path, mock_gui_queue, mock_websocket_manager
    ):
        from services.wheelhouse.event_bus import EventBus
        from services.wheelhouse.handlers import exclusive_speaker_monitor as esm
        from state_manager import StateManager

        from tests.test_exclusive_speaker_monitor import IN_USE, FakeBackend

        seen = {}

        async def scenario():
            loop = asyncio.get_running_loop()
            bus = EventBus()
            config = self._config(tmp_path, "")
            state = StateManager(
                config_service=config,
                event_bus=bus,
                loop=loop,
                state_to_gui_queue=mock_gui_queue,
                websocket_manager=mock_websocket_manager,
            )
            state._speech_enabled = True
            state.speech_notifier._send_notification = MagicMock()
            fake = FakeBackend()
            fake.a_result = IN_USE
            monitor = esm.ExclusiveSpeakerMonitor(
                loop, bus, backend_factory=lambda: fake, poll_interval=0.05
            )
            controller = _startup_controller(config)
            controller.state_manager = state
            # main() hands this to the state manager; broadcast must be awaitable.
            controller.app.websocket_manager.broadcast = AsyncMock()
            controller.service_manager.exclusive_speaker_monitor = monitor

            def provider_starts():
                seen["paused"] = state._exclusive_speaker_pause_active()
                seen["speech_enabled"] = state.speech_enabled

            controller.service_manager.start_remote_stt.side_effect = provider_starts

            async def delayed_auto_query(setting):
                await asyncio.sleep(0.3)  # a slow Windows query
                return True

            try:
                await _run_startup(controller, delayed_auto_query)
            finally:
                monitor.stop()

        asyncio.run(scenario())

        assert seen == {"paused": True, "speech_enabled": False}

    def test_a_closed_gate_starts_the_detector_and_does_not_wait(self, tmp_path):
        controller = _startup_controller(
            self._config(tmp_path, "ENABLE_AUDIO_SUPPRESSION = false\n")
        )
        monitor = controller.service_manager.exclusive_speaker_monitor
        order = MagicMock()
        order.attach_mock(monitor.start, "detector_start")
        order.attach_mock(monitor.wait_first_answer, "detector_wait")
        order.attach_mock(controller.service_manager.start_remote_stt, "provider")

        asyncio.run(_run_startup(controller, AsyncMock(return_value=True)))

        assert [name for name, _, _ in order.mock_calls] == [
            "detector_start",
            "provider",
        ]
        monitor.wait_first_answer.assert_not_awaited()

    def test_an_open_gate_orders_gate_start_wait_provider_decision_services(
        self, tmp_path
    ):
        controller = _startup_controller(self._config(tmp_path, ""))
        monitor = controller.service_manager.exclusive_speaker_monitor
        decide = AsyncMock(return_value=True)
        order = MagicMock()
        order.attach_mock(
            controller.state_manager.apply_exclusive_speaker_pause_gate, "gate"
        )
        order.attach_mock(monitor.start, "detector_start")
        order.attach_mock(monitor.wait_first_answer, "detector_wait")
        order.attach_mock(controller.service_manager.start_remote_stt, "provider")
        order.attach_mock(decide, "decision")
        order.attach_mock(controller.service_manager.start_services, "services")

        asyncio.run(_run_startup(controller, decide))

        assert [name for name, _, _ in order.mock_calls] == [
            "gate",
            "detector_start",
            "detector_wait",
            "provider",
            "decision",
            "services",
        ]
        monitor.wait_first_answer.assert_awaited_once_with()

    def test_the_detector_still_starts_when_there_is_no_provider_launcher(
        self, tmp_path
    ):
        controller = _startup_controller(self._config(tmp_path, ""))
        controller.service_manager.remote_stt_launcher = None
        monitor = controller.service_manager.exclusive_speaker_monitor

        asyncio.run(_run_startup(controller, AsyncMock(return_value=True)))

        monitor.start.assert_called_once_with()
        monitor.wait_first_answer.assert_awaited_once_with()
        controller.service_manager.start_remote_stt.assert_not_called()
