"""The software dimmer takes the wheel steps when no hardware brightness plugin controls a display.

wh-gamma-dimmer-no-hardware: on a desktop with one external monitor (mavencore),
no plugin listens for HardwareBrightnessCommand. The internal panel plugin has
no WMI brightness, and the Bravia and Samsung plugins are off. The coordinator
engaged the software dimmer only on a BrightnessOverflowEvent, which only a
plugin sends, so a wheel turn at the left edge changed nothing.

The rule (boss ruling 19:33): the coordinator sends the step to the software
dimmer when no handler is subscribed to HardwareBrightnessCommand. A plugin
subscribes only when it is set up for a display, and no plugin unsubscribes, so
a plugin that is set up and drops a command without an answer never starts
software dimming.

Most tests use a real EventBus and a real coordinator. The mock-bus tests state
what EventBus.has_subscribers returns.
"""
import logging
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from services.wheelhouse.coordinators.brightness_coordinator import (
    BrightnessCoordinator,
    CoordinatorState,
)
from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import (
    BrightnessAdjustCommand,
    HardwareBrightnessCommand,
)
from services.wheelhouse.integrations.samsung_control import BrightnessChange
from services.wheelhouse.plugins import internal_panel_plugin as panel_mod
from services.wheelhouse.plugins import samsung_plugin as samsung_mod

_LOGGER = "services.wheelhouse.coordinators.brightness_coordinator"


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


async def _start_panel_without_hardware(config, bus, monkeypatch):
    """The internal panel plugin on a desktop: WMI has no brightness control."""
    control = AsyncMock()
    control.initialize = AsyncMock(return_value=False)
    monkeypatch.setattr(panel_mod, "InternalPanelControl", Mock(return_value=control))
    plugin = panel_mod.InternalPanelPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    return plugin


async def _start_panel(config, bus, monkeypatch, level):
    """A laptop panel plugin whose WMI control reports *level*."""
    control = AsyncMock()
    control.initialize = AsyncMock(return_value=True)
    control.get_brightness = AsyncMock(return_value=level)
    control.set_brightness = AsyncMock(return_value=True)
    monkeypatch.setattr(panel_mod, "InternalPanelControl", Mock(return_value=control))
    plugin = panel_mod.InternalPanelPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    return control


async def _start_offline_samsung(config, bus, monkeypatch):
    """A Samsung plugin whose TV is unreachable from start: it reports no state."""
    control = Mock()
    control.get_brightness = AsyncMock(return_value=None)
    control.change_brightness = AsyncMock(return_value=BrightnessChange(error="offline"))
    control.last_error = "offline"
    monkeypatch.setattr(samsung_mod, "SamsungControl", Mock(return_value=control))
    plugin = samsung_mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    return control


def _subscribe_silent_plugin(bus):
    """Subscribe a handler that records each HardwareBrightnessCommand and answers nothing."""
    commands = []

    async def record(event):
        commands.append(event)

    bus.subscribe(HardwareBrightnessCommand, record)
    return commands


async def _turn(bus, *deltas):
    for delta in deltas:
        await bus.publish(BrightnessAdjustCommand(delta=delta))


def _mock_bus(has_subscribers):
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.publish = AsyncMock()
    bus.has_subscribers = MagicMock(return_value=has_subscribers)
    return bus


class TestNoPluginControlsADisplay:
    """Acceptance item 1."""

    @pytest.mark.asyncio
    async def test_dim_step_goes_to_the_software_dimmer(
            self, config, bus, coordinator, dimmer, monkeypatch):
        await _start_panel_without_hardware(config, bus, monkeypatch)

        await _turn(bus, -10)

        dimmer.set_brightness.assert_called_once_with(90)
        assert coordinator._software_dimmer_level == 90
        assert coordinator._is_software_active is True
        assert coordinator._state == CoordinatorState.CASCADED

    @pytest.mark.asyncio
    async def test_each_step_dims_by_the_command_delta(self, bus, coordinator, dimmer):
        # The overflow path applies the overflow's delta, which each plugin sets
        # to the command's delta, so the step size is the same.
        await _turn(bus, -10, -15)

        assert [c.args[0] for c in dimmer.set_brightness.call_args_list] == [90, 75]
        assert coordinator._software_dimmer_level == 75

    @pytest.mark.asyncio
    async def test_first_step_dims_by_its_own_delta(self, bus, coordinator, dimmer):
        # Only the first step goes through the no-hardware path; later steps go
        # through the CASCADED unwinding path. A first step other than -10 shows
        # that the no-hardware path passes the command's delta on.
        await _turn(bus, -15)

        dimmer.set_brightness.assert_called_once_with(85)
        assert coordinator._software_dimmer_level == 85

    @pytest.mark.asyncio
    async def test_zero_step_changes_nothing(self, bus, coordinator, dimmer):
        await _turn(bus, 0)

        assert coordinator._state == CoordinatorState.IDLE
        assert coordinator._is_software_active is False
        dimmer.set_brightness.assert_not_called()

    @pytest.mark.asyncio
    async def test_brighten_returns_to_no_dimming(self, bus, coordinator, dimmer):
        await _turn(bus, -10, -10, 10)
        assert coordinator._software_dimmer_level == 90
        assert coordinator._state == CoordinatorState.CASCADED

        await _turn(bus, 10)

        assert dimmer.set_brightness.call_args_list[-1].args == (100,)
        assert coordinator._software_dimmer_level == 100
        assert coordinator._is_software_active is False
        assert coordinator._state == CoordinatorState.IDLE

    @pytest.mark.asyncio
    async def test_brighten_at_no_dimming_changes_nothing_and_logs_no_error(
            self, bus, coordinator, dimmer, caplog):
        caplog.set_level(logging.DEBUG, logger=_LOGGER)

        await _turn(bus, 10)

        dimmer.set_brightness.assert_not_called()
        assert coordinator._software_dimmer_level == 100
        assert coordinator._is_software_active is False
        assert coordinator._state == CoordinatorState.IDLE
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []

    @pytest.mark.asyncio
    async def test_without_a_software_dimmer_nothing_changes(self, config, bus):
        coordinator = BrightnessCoordinator(config_service=config, event_bus=bus, software_dimmer=None)
        coordinator.start()

        await _turn(bus, -10)

        assert coordinator._is_software_active is False
        assert coordinator._state == CoordinatorState.IDLE

    @pytest.mark.asyncio
    async def test_mock_bus_with_no_subscriber_dims_in_software_and_publishes_nothing(
            self, config, dimmer):
        bus = _mock_bus(has_subscribers=False)
        coordinator = BrightnessCoordinator(config_service=config, event_bus=bus, software_dimmer=dimmer)
        coordinator.start()
        dimmer.set_brightness.reset_mock()

        await coordinator._handle_brightness_command(BrightnessAdjustCommand(delta=-10))

        bus.has_subscribers.assert_called_with(HardwareBrightnessCommand)
        bus.publish.assert_not_awaited()
        dimmer.set_brightness.assert_called_once_with(90)


class TestAPluginControlsADisplay:
    """Acceptance item 2."""

    @pytest.mark.asyncio
    async def test_a_plugin_that_drops_the_command_does_not_start_software_dimming(
            self, bus, coordinator, dimmer):
        # A plugin set up for a display that answers nothing (for example a
        # Bravia TV that refuses the change).
        commands = _subscribe_silent_plugin(bus)

        await _turn(bus, -10)

        assert [c.delta for c in commands] == [-10]
        dimmer.set_brightness.assert_not_called()
        assert coordinator._state == CoordinatorState.IDLE

    @pytest.mark.asyncio
    async def test_a_panel_with_hardware_takes_the_step(
            self, config, bus, coordinator, dimmer, monkeypatch):
        control = await _start_panel(config, bus, monkeypatch, level=50)

        await _turn(bus, -10)

        control.set_brightness.assert_awaited()
        dimmer.set_brightness.assert_not_called()
        assert coordinator._state == CoordinatorState.IDLE

    @pytest.mark.asyncio
    async def test_offline_device_gives_one_step_through_the_existing_fallback(
            self, config, bus, coordinator, dimmer, monkeypatch):
        await _start_offline_samsung(config, bus, monkeypatch)

        await _turn(bus, -10)

        dimmer.set_brightness.assert_called_once_with(90)
        assert coordinator._software_dimmer_level == 90

    @pytest.mark.asyncio
    async def test_mock_bus_with_a_subscriber_publishes_to_the_hardware(self, config, dimmer):
        bus = _mock_bus(has_subscribers=True)
        coordinator = BrightnessCoordinator(config_service=config, event_bus=bus, software_dimmer=dimmer)
        coordinator.start()
        dimmer.set_brightness.reset_mock()

        await coordinator._handle_brightness_command(BrightnessAdjustCommand(delta=-10))

        published = [c.args[0] for c in bus.publish.await_args_list]
        assert [type(e) for e in published] == [HardwareBrightnessCommand]
        assert published[0].delta == -10
        dimmer.set_brightness.assert_not_called()


class TestPluginSubscribesAfterTheSoftwareDimmerEngaged:
    """Boss ruling 19:26 case 3, as read in ruling 19:33: no fallback from then on."""

    @pytest.mark.asyncio
    async def test_unwinding_returns_to_hardware_and_the_fallback_stays_off(
            self, bus, coordinator, dimmer):
        await _turn(bus, -10, -10)
        assert coordinator._software_dimmer_level == 80

        commands = _subscribe_silent_plugin(bus)

        await _turn(bus, 10)
        assert coordinator._software_dimmer_level == 90
        assert coordinator._state == CoordinatorState.CASCADED
        assert commands == []

        # 90 + 15: software takes 10 and the hardware gets the remaining 5.
        await _turn(bus, 15)
        assert coordinator._software_dimmer_level == 100
        assert coordinator._is_software_active is False
        assert coordinator._state == CoordinatorState.IDLE
        assert [c.delta for c in commands] == [5]

        dimmer.set_brightness.reset_mock()
        await _turn(bus, -10)

        assert [c.delta for c in commands] == [5, -10]
        dimmer.set_brightness.assert_not_called()
        assert coordinator._state == CoordinatorState.IDLE


class TestStopRemovesTheFallbackDimming:
    """Acceptance item 4: tray Exit runs ServiceManager shutdown, which calls stop()."""

    @pytest.mark.asyncio
    async def test_stop_restores_no_dimming(self, bus, coordinator, dimmer):
        await _turn(bus, -10, -10)
        dimmer.set_brightness.reset_mock()

        coordinator.stop()

        dimmer.set_brightness.assert_called_once_with(100)
        assert coordinator._is_software_active is False
        assert coordinator._software_dimmer_level == 100
