"""Tests for the f.lux hotkey branch of _adjust_software_dimmer.

wh-drop-pyautogui: the flux branch used pyautogui.hotkey, and pyautogui's
win32 install dependency MouseInfo is GPLv3 -- unshippable in an
Apache-2.0 release. The branch now sends the configured hotkey through
utils.win_input_sender.press_keys, the same SendInput path the rest of
the app uses. These tests pin the replacement, and the repo-wide guard
pins pyautogui's absence from the whole source tree.
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_MOD = "coordinators.brightness_coordinator"


@pytest.fixture
def mock_config_service():
    config = MagicMock()
    config.get_config.return_value = {
        "brightness_coordinator": {
            "software_dimmer": "flux",
            "flux_transition_percent": 2,
            "flux_dim_hotkey": ["alt", "pagedown"],
            "flux_brighten_hotkey": ["alt", "pageup"],
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
def coordinator(mock_config_service, mock_event_bus):
    from coordinators.brightness_coordinator import BrightnessCoordinator

    return BrightnessCoordinator(
        config_service=mock_config_service,
        event_bus=mock_event_bus,
    )


class TestFluxHotkeyPath:
    @pytest.mark.asyncio
    async def test_dim_sends_configured_hotkey_via_press_keys(self, coordinator):
        with patch(f"{_MOD}.press_keys") as mock_press:
            coordinator._software_dimmer_level = 100
            await coordinator._adjust_software_dimmer(-4)

        # 4% change at 2% per press = 2 presses of the dim hotkey.
        assert mock_press.call_count == 2
        for call in mock_press.call_args_list:
            assert call.args == ("alt", "pagedown")
        assert coordinator._software_dimmer_level == 96

    @pytest.mark.asyncio
    async def test_brighten_sends_brighten_hotkey(self, coordinator):
        with patch(f"{_MOD}.press_keys") as mock_press:
            coordinator._software_dimmer_level = 90
            await coordinator._adjust_software_dimmer(4)

        assert mock_press.call_count == 2
        for call in mock_press.call_args_list:
            assert call.args == ("alt", "pageup")
        assert coordinator._software_dimmer_level == 94


class TestNoGplImports:
    def test_no_pyautogui_or_mouseinfo_imports_in_source_tree(self):
        """pyautogui pulls in MouseInfo (GPLv3) on win32. Neither may be
        imported anywhere in the shipped source tree."""
        root = Path(__file__).resolve().parents[2]
        banned = ("import " + "pyautogui", "import " + "mouseinfo")
        offenders = []
        for py in root.rglob("*.py"):
            if ".venv" in py.parts or "site-packages" in py.parts:
                continue
            text = py.read_text(encoding="utf-8", errors="ignore")
            if any(b in text for b in banned):
                offenders.append(str(py))
        assert offenders == [], f"GPL-adjacent imports found: {offenders}"

    def test_pyautogui_not_declared_in_pyproject(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        assert "pyautogui" not in text
