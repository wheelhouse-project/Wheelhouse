"""Tests for BrightnessCoordinator falling back to software dimming when a TV is offline.

A hardware plugin publishes BrightnessOverflowEvent(reason="device_offline")
when it cannot reach its device. The coordinator's at_min/at_max direction
check belongs to reason "at_hardware_limit" only: an unreachable TV is never
recorded at minimum, so applying the check to device_offline dropped every dim
step and the software dimmer never engaged (wh-bravia-offline-fallback).
"""
import logging

import pytest
from unittest.mock import MagicMock, AsyncMock

from services.wheelhouse.events import BrightnessOverflowEvent, BrightnessStateChanged

_MOD = "coordinators.brightness_coordinator"


@pytest.fixture
def mock_config_service():
    config = MagicMock()
    config.get_config.return_value = {
        "brightness_coordinator": {
            "software_dimmer": "gamma_dimmer",
            "unwinding_threshold": 10,
        }
    }
    return config


@pytest.fixture
def mock_event_bus():
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def mock_dimmer():
    dimmer = MagicMock()
    dimmer.set_brightness = MagicMock(return_value=True)
    return dimmer


@pytest.fixture
def coordinator(mock_config_service, mock_event_bus, mock_dimmer):
    from coordinators.brightness_coordinator import BrightnessCoordinator

    coord = BrightnessCoordinator(
        config_service=mock_config_service,
        event_bus=mock_event_bus,
        software_dimmer=mock_dimmer,
    )
    coord.start()
    mock_dimmer.set_brightness.reset_mock()
    return coord


async def _record_mid_range_state(coordinator, plugin="bravia"):
    """The TV was last seen at 40%: neither at minimum nor at maximum."""
    await coordinator._handle_state_change(BrightnessStateChanged(
        level=40, at_min=False, at_max=False, source_plugin=plugin))


class TestOfflineDimFallsBackToSoftware:

    @pytest.mark.asyncio
    async def test_device_offline_dim_engages_software_dimmer(self, coordinator, mock_dimmer):
        from coordinators.brightness_coordinator import CoordinatorState
        await _record_mid_range_state(coordinator)

        await coordinator._handle_overflow(BrightnessOverflowEvent(
            delta=-10, source_plugin="bravia", reason="device_offline"))

        mock_dimmer.set_brightness.assert_called_once_with(90)
        assert coordinator._software_dimmer_level == 90
        assert coordinator._is_software_active is True
        assert coordinator._state == CoordinatorState.CASCADED

    @pytest.mark.asyncio
    async def test_device_offline_dim_with_no_recorded_state_engages_software_dimmer(
            self, coordinator, mock_dimmer):
        """A TV unreachable since start has no recorded state at all."""
        from coordinators.brightness_coordinator import CoordinatorState

        await coordinator._handle_overflow(BrightnessOverflowEvent(
            delta=-10, source_plugin="bravia", reason="device_offline"))

        mock_dimmer.set_brightness.assert_called_once_with(90)
        assert coordinator._state == CoordinatorState.CASCADED

    @pytest.mark.asyncio
    async def test_hardware_limit_dim_not_at_min_is_still_dropped(
            self, coordinator, mock_dimmer, caplog):
        from coordinators.brightness_coordinator import CoordinatorState
        await _record_mid_range_state(coordinator)

        with caplog.at_level(logging.WARNING, logger=_MOD):
            await coordinator._handle_overflow(BrightnessOverflowEvent(
                delta=-10, source_plugin="bravia", reason="at_hardware_limit"))

        mock_dimmer.set_brightness.assert_not_called()
        assert coordinator._is_software_active is False
        assert coordinator._state == CoordinatorState.IDLE
        assert any("Overflow event for dimming but plugin not at minimum" in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]

    @pytest.mark.asyncio
    async def test_hardware_limit_brighten_not_at_max_is_still_dropped(
            self, coordinator, mock_dimmer, caplog):
        coordinator._is_software_active = True
        coordinator._software_dimmer_level = 50
        await _record_mid_range_state(coordinator)

        with caplog.at_level(logging.WARNING, logger=_MOD):
            await coordinator._handle_overflow(BrightnessOverflowEvent(
                delta=10, source_plugin="bravia", reason="at_hardware_limit"))

        mock_dimmer.set_brightness.assert_not_called()
        assert coordinator._software_dimmer_level == 50
        assert any("Overflow event for brightening but plugin not at maximum" in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]


class TestOfflineBrightenRaisesSoftwareLevel:

    @pytest.mark.asyncio
    async def test_brighten_command_while_cascaded_raises_software_level_without_hardware(
            self, coordinator, mock_dimmer, mock_event_bus):
        """Once the offline dim step engaged software dimming, a brighten step
        goes to the software dimmer before any hardware command."""
        from services.wheelhouse.events import BrightnessAdjustCommand, HardwareBrightnessCommand
        await coordinator._handle_overflow(BrightnessOverflowEvent(
            delta=-30, source_plugin="bravia", reason="device_offline"))
        mock_dimmer.set_brightness.reset_mock()
        mock_event_bus.publish.reset_mock()

        await coordinator._handle_brightness_command(BrightnessAdjustCommand(delta=10))

        mock_dimmer.set_brightness.assert_called_once_with(80)
        assert coordinator._software_dimmer_level == 80
        assert not any(isinstance(c.args[0], HardwareBrightnessCommand)
                       for c in mock_event_bus.publish.call_args_list)

    @pytest.mark.asyncio
    async def test_device_offline_brighten_overflow_while_software_active_raises_level(
            self, coordinator, mock_dimmer):
        """A brighten step sent to hardware before software engaged can come
        back as a device_offline overflow after software dimming is active."""
        from coordinators.brightness_coordinator import CoordinatorState
        await _record_mid_range_state(coordinator)
        await coordinator._handle_overflow(BrightnessOverflowEvent(
            delta=-30, source_plugin="bravia", reason="device_offline"))
        mock_dimmer.set_brightness.reset_mock()

        await coordinator._handle_overflow(BrightnessOverflowEvent(
            delta=10, source_plugin="bravia", reason="device_offline"))

        mock_dimmer.set_brightness.assert_called_once_with(80)
        assert coordinator._software_dimmer_level == 80
        assert coordinator._state == CoordinatorState.CASCADED

    @pytest.mark.asyncio
    async def test_device_offline_brighten_overflow_with_software_inactive_does_nothing(
            self, coordinator, mock_dimmer):
        from coordinators.brightness_coordinator import CoordinatorState

        await coordinator._handle_overflow(BrightnessOverflowEvent(
            delta=10, source_plugin="bravia", reason="device_offline"))

        mock_dimmer.set_brightness.assert_not_called()
        assert coordinator._is_software_active is False
        assert coordinator._state == CoordinatorState.IDLE


class TestUnwindingSendsOnlyTheRemainderToHardware:
    """wh-bravia-offline-fallback.1.2: when a brighten step restores the software
    dimmer to 100, only the part of the step that software did not absorb goes to
    hardware. Sending the whole step made each offline dim/brighten cycle leave
    the TV one step brighter than before."""

    async def _cascade_to(self, coordinator, mock_dimmer, mock_event_bus, level):
        await coordinator._handle_overflow(BrightnessOverflowEvent(
            delta=level - 100, source_plugin="bravia", reason="device_offline"))
        mock_dimmer.set_brightness.reset_mock()
        mock_event_bus.publish.reset_mock()

    @staticmethod
    def _hardware_deltas(mock_event_bus):
        from services.wheelhouse.events import HardwareBrightnessCommand
        return [c.args[0].delta for c in mock_event_bus.publish.call_args_list
                if isinstance(c.args[0], HardwareBrightnessCommand)]

    @pytest.mark.asyncio
    async def test_step_fully_absorbed_by_software_sends_nothing_to_hardware(
            self, coordinator, mock_dimmer, mock_event_bus):
        from coordinators.brightness_coordinator import CoordinatorState
        from services.wheelhouse.events import BrightnessAdjustCommand
        await self._cascade_to(coordinator, mock_dimmer, mock_event_bus, 90)

        await coordinator._handle_brightness_command(BrightnessAdjustCommand(delta=10))

        mock_dimmer.set_brightness.assert_called_once_with(100)
        assert coordinator._state == CoordinatorState.IDLE
        assert self._hardware_deltas(mock_event_bus) == []

    @pytest.mark.asyncio
    async def test_step_larger_than_software_dimming_sends_the_remainder(
            self, coordinator, mock_dimmer, mock_event_bus):
        from services.wheelhouse.events import BrightnessAdjustCommand
        await self._cascade_to(coordinator, mock_dimmer, mock_event_bus, 90)

        await coordinator._handle_brightness_command(BrightnessAdjustCommand(delta=15))

        mock_dimmer.set_brightness.assert_called_once_with(100)
        assert self._hardware_deltas(mock_event_bus) == [5]
