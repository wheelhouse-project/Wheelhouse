"""Tests for BrightnessCoordinator handling software dimmer failures.

Validates that the coordinator does not update internal brightness state
when the software dimmer reports a failure (returns False from set_brightness).

Root cause: GammaDimmer.set_brightness() silently fails when SetDeviceGammaRamp
returns FALSE, but the coordinator unconditionally updates _software_dimmer_level,
causing state drift where the coordinator thinks dimming is active but the display
hasn't changed.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock

_MOD = "coordinators.brightness_coordinator"


@pytest.fixture
def mock_config_service():
    """Minimal ConfigService mock for coordinator."""
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
    """Minimal EventBus mock for coordinator."""
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def mock_dimmer():
    """Mock software dimmer with controllable set_brightness return value."""
    dimmer = MagicMock()
    dimmer.set_brightness = MagicMock(return_value=True)
    dimmer.current_brightness_percent = 100
    return dimmer


@pytest.fixture
def coordinator(mock_config_service, mock_event_bus, mock_dimmer):
    """BrightnessCoordinator with mocked dependencies."""
    from coordinators.brightness_coordinator import BrightnessCoordinator

    coord = BrightnessCoordinator(
        config_service=mock_config_service,
        event_bus=mock_event_bus,
        software_dimmer=mock_dimmer,
    )
    coord.start()
    return coord


# ===========================================================================
# Coordinator state management on dimmer failure
# ===========================================================================

class TestCoordinatorDimmerFailure:
    """Coordinator must not update _software_dimmer_level when dimmer fails."""

    @pytest.mark.asyncio
    async def test_level_updates_on_success(self, coordinator, mock_dimmer):
        """When set_brightness returns True, level should update."""
        mock_dimmer.set_brightness.reset_mock()
        mock_dimmer.set_brightness.return_value = True

        # Put coordinator in cascaded state
        coordinator._is_software_active = True
        coordinator._software_dimmer_level = 100
        from coordinators.brightness_coordinator import CoordinatorState
        coordinator._state = CoordinatorState.CASCADED

        await coordinator._adjust_software_dimmer(-10)

        assert coordinator._software_dimmer_level == 90
        mock_dimmer.set_brightness.assert_called_once_with(90)

    @pytest.mark.asyncio
    async def test_level_does_not_update_on_failure(self, coordinator, mock_dimmer):
        """When set_brightness returns False, level must NOT update.

        This is the critical bug fix: the coordinator previously updated
        _software_dimmer_level unconditionally, causing state drift when
        the gamma dimmer's SetDeviceGammaRamp call failed.
        """
        mock_dimmer.set_brightness.reset_mock()
        mock_dimmer.set_brightness.return_value = False

        # Put coordinator in cascaded state
        coordinator._is_software_active = True
        coordinator._software_dimmer_level = 100
        from coordinators.brightness_coordinator import CoordinatorState
        coordinator._state = CoordinatorState.CASCADED

        await coordinator._adjust_software_dimmer(-10)

        # Level should remain at 100 since dimmer reported failure
        assert coordinator._software_dimmer_level == 100
        mock_dimmer.set_brightness.assert_called_once_with(90)

    @pytest.mark.asyncio
    async def test_repeated_failures_dont_drift(self, coordinator, mock_dimmer):
        """Multiple failed adjustments should not accumulate state drift."""
        mock_dimmer.set_brightness.return_value = False

        coordinator._is_software_active = True
        coordinator._software_dimmer_level = 80
        from coordinators.brightness_coordinator import CoordinatorState
        coordinator._state = CoordinatorState.CASCADED

        # Three failed attempts to dim by 5 each
        for _ in range(3):
            await coordinator._adjust_software_dimmer(-5)

        # Level should stay at 80, not drift to 65
        assert coordinator._software_dimmer_level == 80
