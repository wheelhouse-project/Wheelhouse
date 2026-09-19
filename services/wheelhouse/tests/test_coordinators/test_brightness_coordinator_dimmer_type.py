"""Tests for how BrightnessCoordinator chooses its software dimmer type.

gamma_dimmer is the default. The accepted values are software_dimmer,
overlay and gamma_dimmer. Any other configured value is refused with one
WARNING that names the value seen and the accepted values, after which the
coordinator uses gamma_dimmer.
"""
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

_LOGGER = "coordinators.brightness_coordinator"
_ACCEPTED = ("software_dimmer", "overlay", "gamma_dimmer")


def _config(coordinator_section):
    config = MagicMock()
    config.get_config.return_value = {"brightness_coordinator": coordinator_section}
    return config


@pytest.fixture
def mock_event_bus():
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.publish = AsyncMock()
    return bus


def _build(config, event_bus):
    from coordinators.brightness_coordinator import BrightnessCoordinator

    return BrightnessCoordinator(
        config_service=config,
        event_bus=event_bus,
        software_dimmer=MagicMock(),
    )


class TestDimmerTypeSelection:
    def test_no_software_dimmer_key_uses_gamma_dimmer(self, mock_event_bus, caplog):
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            coordinator = _build(_config({}), mock_event_bus)

        assert coordinator._software_dimmer_type == "gamma_dimmer"
        # A default that is not an accepted value would still end as
        # gamma_dimmer through the fallback, so the absence of the warning
        # is the proof.
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_unrecognised_value_is_refused_with_one_warning_and_uses_gamma_dimmer(
        self, mock_event_bus, caplog
    ):
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            coordinator = _build(_config({"software_dimmer": "nonsense"}), mock_event_bus)

        assert coordinator._software_dimmer_type == "gamma_dimmer"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        message = warnings[0].getMessage()
        assert "accepted values are" in message
        seen_part, accepted_part = message.split("accepted values are", 1)
        assert "'nonsense'" in seen_part
        # 'software_dimmer' is also in the config key name and 'gamma_dimmer'
        # in "Using gamma_dimmer.", so look for the names inside the list.
        accepted_part = accepted_part.split(".", 1)[0]
        for accepted in _ACCEPTED:
            assert accepted in accepted_part

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("software_dimmer", id="software-dimmer"),
            pytest.param("overlay", id="overlay"),
            pytest.param("gamma_dimmer", id="gamma-dimmer"),
        ],
    )
    def test_accepted_type_is_kept_without_warning(self, value, mock_event_bus, caplog):
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            coordinator = _build(_config({"software_dimmer": value}), mock_event_bus)

        assert coordinator._software_dimmer_type == value
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
