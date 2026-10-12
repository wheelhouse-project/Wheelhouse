"""The Interim Results menu toggle saves its new value to config.toml.

wh-voice-settings-window stage A (boss ruling R1b). Before this change the
toggle flipped the value in memory only, so the setting returned to on at
the next start. The toggle now saves INTERIM_RESULTS through
StateManager.set_config_value, the same path the other menu toggles use.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from state_manager import StateManager


def _controller(state_manager, ws_manager):
    from main import LogicController

    controller = object.__new__(LogicController)
    controller.state_manager = state_manager
    controller.app = Mock()
    controller.app.websocket_manager = ws_manager
    return controller


@pytest.fixture
def ws_manager():
    ws = Mock()
    ws.interim_results_enabled = True
    ws.send_command_to_stt = AsyncMock()
    return ws


class TestToggleInterimResultsSaves:

    @pytest.mark.asyncio
    async def test_the_new_value_is_saved_with_the_toggle(self, ws_manager):
        sm = Mock()
        sm.interim_results_enabled = True
        sm.set_config_value = AsyncMock(return_value=True)
        await _controller(sm, ws_manager).toggle_interim_results()

        sm.set_config_value.assert_awaited_once_with("INTERIM_RESULTS", False)
        assert sm.interim_results_enabled is False
        assert ws_manager.interim_results_enabled is False
        ws_manager.send_command_to_stt.assert_awaited_once_with(
            "set_interim_results", enabled=False)

    @pytest.mark.asyncio
    async def test_toggling_back_on_saves_true(self, ws_manager):
        sm = Mock()
        sm.interim_results_enabled = False
        sm.set_config_value = AsyncMock(return_value=True)
        await _controller(sm, ws_manager).toggle_interim_results()

        sm.set_config_value.assert_awaited_once_with("INTERIM_RESULTS", True)

    @pytest.mark.asyncio
    async def test_a_real_state_manager_stores_the_value_and_saves_to_disk(
        self, mock_config, mock_event_bus, mock_gui_queue, ws_manager
    ):
        loop = asyncio.get_running_loop()
        sm = StateManager(
            config_service=mock_config, event_bus=mock_event_bus, loop=loop,
            state_to_gui_queue=mock_gui_queue, websocket_manager=None)
        assert sm.interim_results_enabled is True

        await _controller(sm, ws_manager).toggle_interim_results()

        assert mock_config.get("INTERIM_RESULTS") is False
        mock_config.save.assert_awaited()
        # The GUI is told the new value; the last state update carries it.
        states = [
            c.args[0] for c in mock_gui_queue.put_nowait.call_args_list
            if c.args[0].get("action") == "state_update"
        ]
        assert states, "no state update reached the GUI queue"
        assert states[-1]["interim_results_enabled"] is False
