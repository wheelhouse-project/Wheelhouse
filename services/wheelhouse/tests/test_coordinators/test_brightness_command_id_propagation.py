"""A brightness command's id reaches every answer a hardware plugin publishes.

wh-brightness-double-dim-two-plugins: one BrightnessAdjustCommand becomes one
HardwareBrightnessCommand that every hardware plugin answers. BrightnessCoordinator
can apply at most one software step per command only when each answer names the
command it answers. So BrightnessAdjustCommand gets a unique id, the coordinator
copies it into HardwareBrightnessCommand, and each plugin copies it into every
BrightnessStateChanged and BrightnessOverflowEvent it publishes for that command.
"""
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from services.wheelhouse.events import (
    BrightnessAdjustCommand,
    BrightnessOverflowEvent,
    BrightnessStateChanged,
    HardwareBrightnessCommand,
)
from services.wheelhouse.integrations.samsung_control import BrightnessChange
from services.wheelhouse.plugins import samsung_plugin as samsung_mod
from services.wheelhouse.plugins.base import PluginState
from services.wheelhouse.plugins.bravia_plugin import BraviaPlugin
from services.wheelhouse.plugins.internal_panel_plugin import InternalPanelPlugin

COMMAND_ID = 4242
STATE = ("BrightnessStateChanged", None)
OFFLINE = ("BrightnessOverflowEvent", "device_offline")
AT_LIMIT = ("BrightnessOverflowEvent", "at_hardware_limit")


def _mock_bus():
    bus = Mock()
    bus.subscribe = Mock()
    bus.publish = AsyncMock()
    return bus


def _answers(bus):
    """The state and overflow events published on a mock bus, in order."""
    return [c.args[0] for c in bus.publish.call_args_list
            if isinstance(c.args[0], (BrightnessStateChanged, BrightnessOverflowEvent))]


def _shape(events):
    return [(type(e).__name__, getattr(e, "reason", None)) for e in events]


def test_each_brightness_adjust_command_gets_its_own_integer_id():
    first = BrightnessAdjustCommand(delta=-10)
    second = BrightnessAdjustCommand(delta=-10)

    first_id = getattr(first, "command_id", None)
    second_id = getattr(second, "command_id", None)

    assert isinstance(first_id, int) and isinstance(second_id, int), (first_id, second_id)
    assert first_id != second_id


@pytest.mark.asyncio
@pytest.mark.parametrize("software_level, delta, expected_delta", [
    (None, -10, -10),
    (90, 15, 5),
], ids=["idle_route", "unwinding_remainder"])
async def test_coordinator_copies_the_command_id_into_the_hardware_command(
        software_level, delta, expected_delta):
    from services.wheelhouse.coordinators.brightness_coordinator import (
        BrightnessCoordinator, CoordinatorState)
    config = MagicMock()
    config.get_config.return_value = {"brightness_coordinator": {"software_dimmer": "gamma_dimmer"}}
    bus = _mock_bus()
    dimmer = MagicMock()
    dimmer.set_brightness = MagicMock(return_value=True)
    coordinator = BrightnessCoordinator(config_service=config, event_bus=bus, software_dimmer=dimmer)
    coordinator.start()
    if software_level is not None:
        coordinator._is_software_active = True
        coordinator._software_dimmer_level = software_level
        coordinator._state = CoordinatorState.CASCADED
    command = BrightnessAdjustCommand(delta=delta)

    await coordinator._handle_brightness_command(command)

    hardware = [c.args[0] for c in bus.publish.call_args_list
                if isinstance(c.args[0], HardwareBrightnessCommand)]
    assert [(h.delta, h.command_id) for h in hardware] == [(expected_delta, command.command_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_level, delta, expected", [
    (10, -30, [STATE, AT_LIMIT]),
    (90, 20, [STATE, AT_LIMIT]),
    (50, -10, [STATE]),
    (None, -10, [OFFLINE]),
], ids=["at_minimum", "at_maximum", "mid_range_success", "startup_read_none"])
async def test_internal_panel_tags_every_answer_with_the_command_id(cached_level, delta, expected):
    bus = _mock_bus()
    control = AsyncMock()
    control.get_brightness = AsyncMock(return_value=None)
    control.set_brightness = AsyncMock(return_value=True)
    plugin = InternalPanelPlugin()
    plugin._event_bus = bus
    plugin._display_control = control
    plugin._is_hardware_available = True
    plugin._current_brightness = cached_level
    plugin._state = PluginState.RUNNING

    await plugin._handle_brightness_command(
        HardwareBrightnessCommand(delta=delta, command_id=COMMAND_ID))

    answers = _answers(bus)
    assert _shape(answers) == expected
    assert [e.command_id for e in answers] == [COMMAND_ID] * len(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("readings, adjust_result, expected", [
    ([None], True, [OFFLINE]),
    ([50], None, [OFFLINE]),
    ([0], True, [AT_LIMIT, STATE]),
    ([50, 40], True, [STATE]),
], ids=["offline_on_read", "offline_on_adjust", "at_limit", "success"])
async def test_bravia_tags_every_answer_with_the_command_id(readings, adjust_result, expected):
    bus = _mock_bus()
    control = AsyncMock()
    control.get_brightness = AsyncMock(side_effect=readings)
    control.adjust_brightness = AsyncMock(return_value=adjust_result)
    plugin = BraviaPlugin()
    plugin._event_bus = bus
    plugin._bravia_control = control
    plugin._state = PluginState.RUNNING

    await plugin._handle_brightness_command(
        HardwareBrightnessCommand(delta=-10, command_id=COMMAND_ID))

    answers = _answers(bus)
    assert _shape(answers) == expected
    assert [e.command_id for e in answers] == [COMMAND_ID] * len(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("change, expected", [
    (BrightnessChange(error="offline"), [OFFLINE]),
    (BrightnessChange(error="timeout"), [OFFLINE]),
    (BrightnessChange(12), [STATE]),
    (BrightnessChange(0, -8), [STATE, AT_LIMIT]),
], ids=["offline", "timeout", "success", "at_hardware_limit"])
async def test_samsung_tags_every_answer_with_the_command_id(monkeypatch, change, expected):
    bus = _mock_bus()
    control = Mock()
    control.get_brightness = AsyncMock(return_value=10)
    control.change_brightness = AsyncMock(return_value=change)
    control.last_error = None
    monkeypatch.setattr(samsung_mod, "SamsungControl", Mock(return_value=control))
    values = {"plugins.samsung.enabled": True, "plugins.samsung.ip_address": "192.0.2.10",
              "plugins.samsung.credential_file": "test.dpapi"}
    config = Mock()
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    plugin = samsung_mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    bus.publish.reset_mock()

    await plugin._handle_brightness_command(
        HardwareBrightnessCommand(delta=-10, command_id=COMMAND_ID))

    answers = _answers(bus)
    assert _shape(answers) == expected
    assert [e.command_id for e in answers] == [COMMAND_ID] * len(expected)
