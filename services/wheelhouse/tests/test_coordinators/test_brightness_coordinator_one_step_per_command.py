"""One wheel step adds at most one software dimming step, however many plugins answer it.

wh-brightness-double-dim-two-plugins: BrightnessCoordinator publishes one
HardwareBrightnessCommand per BrightnessAdjustCommand, and every hardware plugin
answers it (EventBus.publish awaits all subscribers together). Applying each
overflow on its own dimmed one step twice: a laptop panel at 0 plus an offline TV,
two offline TVs, or a panel that dimmed in hardware plus an offline TV.

Each test drives the real fan-out: a real EventBus, a real coordinator, and real
plugin classes whose device controls are mocks. BraviaPlugin is started with its
control set directly, as in test_bravia_plugin.py, because its initialize() runs
EDID and SSDP discovery.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, call

import pytest

from services.wheelhouse.coordinators.brightness_coordinator import BrightnessCoordinator
from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import (
    BrightnessAdjustCommand,
    BrightnessOverflowEvent,
    BrightnessStateChanged,
)
from services.wheelhouse.integrations.samsung_control import BrightnessChange
from services.wheelhouse.plugins import internal_panel_plugin as panel_mod
from services.wheelhouse.plugins import samsung_plugin as samsung_mod
from services.wheelhouse.plugins.base import PluginState
from services.wheelhouse.plugins.bravia_plugin import BraviaPlugin


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def config():
    values = {
        "plugins.internal_panel.enabled": True,
        "plugins.samsung.enabled": True,
        "plugins.samsung.ip_address": "192.0.2.10",
        "plugins.samsung.credential_file": "test.dpapi",
    }
    config = Mock()
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.get_config.return_value = {
        "brightness_coordinator": {"software_dimmer": "gamma_dimmer", "unwinding_threshold": 10}
    }
    return config


@pytest.fixture
def dimmer():
    dimmer = MagicMock()
    dimmer.set_brightness = MagicMock(return_value=True)
    return dimmer


@pytest.fixture
def coordinator(config, bus, dimmer):
    coordinator = BrightnessCoordinator(config_service=config, event_bus=bus, software_dimmer=dimmer)
    coordinator.start()
    dimmer.set_brightness.reset_mock()
    yield coordinator
    coordinator.stop()


async def _start_panel(config, bus, monkeypatch, level, set_brightness=None):
    """A laptop panel plugin whose WMI control reports *level*."""
    control = AsyncMock()
    control.initialize = AsyncMock(return_value=True)
    control.get_brightness = AsyncMock(return_value=level)
    control.set_brightness = set_brightness or AsyncMock(return_value=True)
    monkeypatch.setattr(panel_mod, "InternalPanelControl", Mock(return_value=control))
    plugin = panel_mod.InternalPanelPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    return control


async def _start_offline_samsung(config, bus, monkeypatch):
    """A Samsung plugin whose TV is unreachable from start."""
    control = Mock()
    control.get_brightness = AsyncMock(return_value=None)
    control.change_brightness = AsyncMock(return_value=BrightnessChange(error="offline"))
    control.last_error = "offline"
    monkeypatch.setattr(samsung_mod, "SamsungControl", Mock(return_value=control))
    plugin = samsung_mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    return control


async def _start_offline_bravia(bus):
    """A Bravia plugin whose TV is unreachable from start."""
    control = AsyncMock()
    control.get_brightness = AsyncMock(return_value=None)
    control.adjust_brightness = AsyncMock(return_value=None)
    plugin = BraviaPlugin()
    plugin._config = Mock()
    plugin._event_bus = bus
    plugin._bravia_control = control
    plugin._state = PluginState.INITIALIZED
    await plugin.start()
    return control


async def _start_bravia(bus, readings):
    """A Bravia plugin whose TV answers its brightness reads with *readings*, in order."""
    control = AsyncMock()
    control.get_brightness = AsyncMock(side_effect=readings)
    control.adjust_brightness = AsyncMock(return_value=True)
    plugin = BraviaPlugin()
    plugin._config = Mock()
    plugin._event_bus = bus
    plugin._bravia_control = control
    plugin._state = PluginState.INITIALIZED
    await plugin.start()
    return control


async def _start_samsung(config, bus, monkeypatch, level, change):
    """A Samsung plugin whose TV reads *level* at start and answers a change with *change*."""
    control = Mock()
    control.get_brightness = AsyncMock(return_value=level)
    control.change_brightness = AsyncMock(return_value=change)
    control.last_error = None
    monkeypatch.setattr(samsung_mod, "SamsungControl", Mock(return_value=control))
    plugin = samsung_mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    return control


def _record_overflows(bus):
    overflows = []

    async def record(event):
        overflows.append((event.source_plugin, event.reason, event.command_id))

    bus.subscribe(BrightnessOverflowEvent, record)
    return overflows


@pytest.mark.asyncio
async def test_panel_at_minimum_and_offline_samsung_add_one_software_step(
        coordinator, config, bus, dimmer, monkeypatch):
    await _start_panel(config, bus, monkeypatch, level=0)
    await _start_offline_samsung(config, bus, monkeypatch)
    overflows = _record_overflows(bus)
    command = BrightnessAdjustCommand(delta=-10)

    await coordinator._handle_brightness_command(command)

    assert sorted(overflows) == [
        ("internal_panel", "at_hardware_limit", command.command_id),
        ("samsung", "device_offline", command.command_id),
    ]
    assert dimmer.set_brightness.call_args_list == [call(90)]


@pytest.mark.asyncio
async def test_two_offline_tvs_add_one_software_step(
        coordinator, config, bus, dimmer, monkeypatch):
    await _start_offline_bravia(bus)
    await _start_offline_samsung(config, bus, monkeypatch)
    overflows = _record_overflows(bus)
    command = BrightnessAdjustCommand(delta=-10)

    await coordinator._handle_brightness_command(command)

    assert sorted(overflows) == [
        ("bravia", "device_offline", command.command_id),
        ("samsung", "device_offline", command.command_id),
    ]
    assert dimmer.set_brightness.call_args_list == [call(90)]


@pytest.mark.asyncio
async def test_panel_dimmed_in_hardware_and_offline_tv_add_no_software_step(
        coordinator, config, bus, dimmer, monkeypatch):
    panel_control = await _start_panel(config, bus, monkeypatch, level=50)
    await _start_offline_bravia(bus)
    overflows = _record_overflows(bus)
    command = BrightnessAdjustCommand(delta=-10)

    await coordinator._handle_brightness_command(command)

    panel_control.set_brightness.assert_awaited_once_with(40)
    assert overflows == [("bravia", "device_offline", command.command_id)]
    assert dimmer.set_brightness.call_args_list == []


@pytest.mark.asyncio
async def test_offline_tv_answer_before_panel_state_still_adds_no_software_step(
        coordinator, config, bus, dimmer, monkeypatch):
    """The panel's hardware set waits until the TV's offline overflow has been
    published, so the overflow reaches the coordinator before the panel's state."""
    tv_answered = asyncio.Event()

    async def set_brightness_after_tv_answer(level):
        await asyncio.wait_for(tv_answered.wait(), timeout=2)
        return True

    panel_control = await _start_panel(
        config, bus, monkeypatch, level=50,
        set_brightness=AsyncMock(side_effect=set_brightness_after_tv_answer))
    await _start_offline_bravia(bus)
    # Subscribed after the coordinator: for each event the bus starts the
    # coordinator's handler first, so this list shows the order in which the
    # coordinator received the events.
    arrivals = []

    async def record_overflow(event):
        arrivals.append(("overflow", event.source_plugin, event.command_id))
        tv_answered.set()

    async def record_state(event):
        arrivals.append(("state", event.source_plugin, event.command_id))

    bus.subscribe(BrightnessOverflowEvent, record_overflow)
    bus.subscribe(BrightnessStateChanged, record_state)
    command = BrightnessAdjustCommand(delta=-10)

    await coordinator._handle_brightness_command(command)

    panel_control.set_brightness.assert_awaited_once_with(40)
    assert arrivals == [
        ("overflow", "bravia", command.command_id),
        ("state", "internal_panel", command.command_id),
    ]
    assert dimmer.set_brightness.call_args_list == []


@pytest.mark.asyncio
async def test_panel_and_samsung_both_at_minimum_add_one_software_step(
        coordinator, config, bus, dimmer, monkeypatch):
    """Both plugins publish their level of 0 before their at_hardware_limit overflow,
    so both overflows pass the at_min check; only the first one adds a step."""
    await _start_panel(config, bus, monkeypatch, level=0)
    await _start_samsung(config, bus, monkeypatch, level=0, change=BrightnessChange(0, -10))
    overflows = _record_overflows(bus)
    command = BrightnessAdjustCommand(delta=-10)

    await coordinator._handle_brightness_command(command)

    assert sorted(overflows) == [
        ("internal_panel", "at_hardware_limit", command.command_id),
        ("samsung", "at_hardware_limit", command.command_id),
    ]
    assert dimmer.set_brightness.call_args_list == [call(90)]


@pytest.mark.asyncio
async def test_tv_that_sent_an_overflow_and_a_state_did_not_apply_the_command_in_hardware(
        coordinator, config, bus, dimmer, monkeypatch):
    """Bravia publishes its at_hardware_limit overflow before its state. The coordinator
    still holds the TV's start-up level of 50, so the at_min check rejects that
    overflow, and the TV's tagged state arrives after it. A plugin that sent an
    overflow for the command did not apply the command in hardware, so the offline
    Samsung's held step still applies once.

    The step of 1 shows which overflow applied: Bravia raises a step below 2 to 2,
    so its overflow would set the dimmer to 98; the Samsung overflow sets it to 99."""
    await _start_bravia(bus, readings=[50, 0])
    await _start_offline_samsung(config, bus, monkeypatch)
    answers = []

    async def record(event):
        answers.append((event.source_plugin, getattr(event, "reason", "state"), event.command_id))

    bus.subscribe(BrightnessOverflowEvent, record)
    bus.subscribe(BrightnessStateChanged, record)
    command = BrightnessAdjustCommand(delta=-1)

    await coordinator._handle_brightness_command(command)

    assert sorted(answers) == [
        ("bravia", "at_hardware_limit", command.command_id),
        ("bravia", "state", command.command_id),
        ("samsung", "device_offline", command.command_id),
    ]
    assert dimmer.set_brightness.call_args_list == [call(99)]


@pytest.mark.asyncio
async def test_offline_overflow_for_a_finished_command_applies_on_arrival(coordinator, bus, dimmer):
    """A command's record closes when its publish returns. A device_offline overflow
    tagged with that command that arrives afterwards has no record to wait in, so it
    applies on arrival, as an untagged overflow does. No plugin is running, so the
    command itself gets no answer and adds no step."""
    command = BrightnessAdjustCommand(delta=-10)
    await coordinator._handle_brightness_command(command)
    assert dimmer.set_brightness.call_args_list == []

    await bus.publish(BrightnessOverflowEvent(
        delta=-10, source_plugin="bravia", reason="device_offline",
        command_id=command.command_id))

    assert dimmer.set_brightness.call_args_list == [call(90)]
