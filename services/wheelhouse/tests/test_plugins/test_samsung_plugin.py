"""Samsung lifecycle, command feedback and coordinator integration."""
import asyncio
import re
from unittest.mock import AsyncMock, Mock

import pytest

from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import (HardwareBrightnessCommand, BrightnessStateChanged,
    BrightnessOverflowEvent, SystemConfigurationErrorEvent, BrightnessAdjustCommand)
from services.wheelhouse.integrations.samsung_control import BrightnessChange
from services.wheelhouse.plugins import samsung_plugin as mod
from services.wheelhouse.plugins.base import PluginState


@pytest.fixture
def setup(monkeypatch):
    control = Mock()
    control.get_brightness = AsyncMock(return_value=10)
    control.change_brightness = AsyncMock(return_value=BrightnessChange(12))
    control.last_error = None
    factory = Mock(return_value=control)
    monkeypatch.setattr(mod, 'SamsungControl', factory)
    config = Mock()
    values = {'plugins.samsung.enabled': True, 'plugins.samsung.ip_address': '192.0.2.10',
              'plugins.samsung.credential_file': 'test.dpapi'}
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    return config, values, control, factory, EventBus()


@pytest.mark.asyncio
async def test_command_cascades_only_remainder_to_software_dimmer(setup):
    from services.wheelhouse.coordinators.brightness_coordinator import BrightnessCoordinator
    config, _, control, _, bus = setup
    config.get_config.return_value = {'brightness_coordinator': {'software_dimmer': 'overlay'}}
    dimmer = Mock()
    dimmer.set_brightness.return_value = True
    coordinator = BrightnessCoordinator(config, bus, software_dimmer=dimmer)
    plugin = mod.SamsungPlugin()
    control.get_brightness.return_value = 2
    control.change_brightness.return_value = BrightnessChange(0, -8)
    coordinator.start()
    await plugin.initialize(config, bus)
    await plugin.start()
    try:
        await bus.publish(BrightnessAdjustCommand(-10))
        control.change_brightness.assert_awaited_once_with(-10)
        assert coordinator.get_status()['software_dimmer_level'] == 92
        dimmer.set_brightness.assert_called_with(92)
    finally:
        await plugin.stop()
        coordinator.stop()


@pytest.mark.asyncio
async def test_existing_thumb_wheel_controls_route_to_samsung(setup, monkeypatch):
    from services.wheelhouse.coordinators.brightness_coordinator import BrightnessCoordinator
    from services.wheelhouse.handlers import mouse_handler as mouse_mod

    config, _, control, _, bus = setup
    config.get_config.return_value = {'brightness_coordinator': {'software_dimmer': 'overlay'}}
    monkeypatch.setattr(mouse_mod, 'HIDListener', Mock())
    dimmer = Mock()
    dimmer.set_brightness.return_value = True
    coordinator = BrightnessCoordinator(config, bus, software_dimmer=dimmer)
    handler = mouse_mod.MouseHandler(asyncio.get_running_loop(), config, Mock(),
                                    Mock(), Mock(), dimmer, bus)
    plugin = mod.SamsungPlugin()
    coordinator.start()
    await plugin.initialize(config, bus)
    await plugin.start()
    try:
        # The original input handler accumulates quarter-steps into a TV step.
        await handler._handle_brightness_zone_event(-4)
        control.change_brightness.assert_not_awaited()
        await handler._handle_brightness_zone_event(-4)
        control.change_brightness.assert_awaited_once_with(2)
        assert coordinator.get_status()['plugin_states']['samsung']['level'] == 12
    finally:
        await plugin.stop()
        coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('level', [0, 10, None])
@pytest.mark.parametrize('reason', ['offline', 'timeout', 'rejected'])
async def test_offline_software_dimming_matches_existing_sony_path(setup, level, reason):
    from services.wheelhouse.coordinators.brightness_coordinator import BrightnessCoordinator

    config, _, control, _, bus = setup
    config.get_config.return_value = {'brightness_coordinator': {'software_dimmer': 'overlay'}}
    control.get_brightness.return_value = level
    control.change_brightness.return_value = BrightnessChange(error=reason)
    dimmer = Mock()
    dimmer.set_brightness.return_value = True
    coordinator = BrightnessCoordinator(config, bus, software_dimmer=dimmer)
    plugin = mod.SamsungPlugin()
    coordinator.start()
    await plugin.initialize(config, bus)
    await plugin.start()
    dimmer.set_brightness.reset_mock()
    try:
        await bus.publish(BrightnessAdjustCommand(-2))
        # An unreachable TV hands a dim step to the software dimmer whatever its
        # last known level (wh-bravia-offline-fallback); a rejection does not.
        if reason in ('offline', 'timeout'):
            dimmer.set_brightness.assert_called_once_with(98)
            # Further dimming stays available through the original coordinator.
            await bus.publish(BrightnessAdjustCommand(-2))
            assert coordinator.get_status()['software_dimmer_level'] == 96
            control.change_brightness.assert_awaited_once_with(-2)
        else:
            dimmer.set_brightness.assert_not_called()
        assert plugin.get_health_status()['details']['tv_connected'] is False
    finally:
        await plugin.stop()
        coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, None])
async def test_disabled_or_absent_never_touches_hardware(setup, enabled):
    config, values, control, factory, bus = setup
    if enabled is None:
        values.clear()
    else:
        values['plugins.samsung.enabled'] = enabled
    plugin = mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    await bus.publish(HardwareBrightnessCommand(2))
    factory.assert_not_called()
    control.get_brightness.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_stop_restart_has_exactly_one_subscription(setup):
    config, _, control, _, bus = setup
    plugin = mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    await plugin.start()
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 1
    await plugin.stop()
    assert plugin.state == PluginState.STOPPED
    await plugin.stop()
    assert plugin.state == PluginState.STOPPED
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 1
    await plugin.start()
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 2
    await plugin.stop()
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 2


@pytest.mark.asyncio
async def test_offline_at_start_remains_running_and_recovers(setup):
    config, _, control, _, bus = setup
    control.get_brightness.return_value = None
    control.last_error = 'timeout'
    plugin = mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    assert plugin.state == PluginState.RUNNING
    assert plugin.get_health_status()['status'] == 'degraded'
    control.last_error = None
    await bus.publish(HardwareBrightnessCommand(2))
    assert plugin.get_health_status()['status'] == 'healthy'


@pytest.mark.asyncio
async def test_partial_overflow_publishes_minimum_before_overflow(setup):
    config, _, control, _, bus = setup
    control.change_brightness.return_value = BrightnessChange(0, -8)
    events = []
    async def capture(event):
        events.append(event)
    bus.subscribe(BrightnessStateChanged, capture)
    bus.subscribe(BrightnessOverflowEvent, capture)
    plugin = mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    events.clear()
    await bus.publish(HardwareBrightnessCommand(-10))
    assert len(events) == 2
    assert isinstance(events[0], BrightnessStateChanged) and events[0].at_min
    assert isinstance(events[1], BrightnessOverflowEvent) and events[1].delta == -8


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['pairing_required', 'rejected', 'unsupported',
                                  'timeout', 'offline', 'readback_mismatch'])
async def test_failures_never_report_success_or_fake_limit(setup, reason):
    config, _, control, _, bus = setup
    plugin = mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    received = []
    async def capture(event):
        received.append(event)
    for event_type in (BrightnessStateChanged, BrightnessOverflowEvent, SystemConfigurationErrorEvent):
        bus.subscribe(event_type, capture)
    control.change_brightness.return_value = BrightnessChange(error=reason)
    await bus.publish(HardwareBrightnessCommand(2))
    offline_handoff = reason in ('timeout', 'offline')
    assert len(received) == (2 if offline_handoff else 1)
    assert isinstance(received[0], SystemConfigurationErrorEvent)
    if offline_handoff:
        assert isinstance(received[1], BrightnessOverflowEvent)
        assert received[1].reason == 'device_offline'
    assert plugin.get_health_status()['details']['error'] == reason
    await bus.publish(HardwareBrightnessCommand(2))
    assert sum(isinstance(event, SystemConfigurationErrorEvent) for event in received) == 1
    assert not any(isinstance(event, BrightnessStateChanged) for event in received)


@pytest.mark.asyncio
async def test_zero_delta_is_a_noop(setup):
    config, _, control, _, bus = setup
    plugin = mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    await bus.publish(HardwareBrightnessCommand(0))
    control.change_brightness.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_failed_attempt_logs_a_warning_even_when_the_notice_is_deduplicated(setup, caplog):
    config, _, control, _, bus = setup
    plugin = mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    notices = []
    async def capture(event):
        notices.append(event)
    bus.subscribe(SystemConfigurationErrorEvent, capture)
    control.change_brightness.return_value = BrightnessChange(error='timeout')
    caplog.set_level('WARNING', logger=mod.__name__)
    await bus.publish(HardwareBrightnessCommand(-2))
    await bus.publish(HardwareBrightnessCommand(-2))
    lines = [record.getMessage() for record in caplog.records
             if record.name == mod.__name__ and record.levelname == 'WARNING']
    assert len(lines) == 2
    for line in lines:
        assert 'reason=timeout' in line
        assert re.search(r'elapsed=\d+\.\d+s', line)
    assert len(notices) == 1
