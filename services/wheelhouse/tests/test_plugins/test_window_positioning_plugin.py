"""Tests for WindowPositioningPlugin.

Covers: initialization, config validation, window detection, overlap checking,
clear position finding (pure math), event filtering, health status.

P3-T3 of the test coverage improvement plan.

Note: Hook thread and Windows event processing are heavily dependent on
Win32 APIs and cannot be fully tested without a live Windows desktop.
Focus is on testable logic: config validation, position math, filtering.
"""

import asyncio
import math

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock, PropertyMock

from services.wheelhouse.plugins.base import PluginState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_event_bus():
    bus = Mock()
    bus.publish = AsyncMock()
    bus.subscribe = Mock()
    return bus


def _make_config(overrides=None):
    defaults = {
        "plugins.window_positioning.target_window_names": ["On-Screen Keyboard", "osk"],
        "plugins.window_positioning.move_cooldown_seconds": 0.5,
        "plugins.window_positioning.clearance_gap_pixels": 5,
        "plugins.window_positioning.ignore_window_titles": ["Program Manager", "Task Switching"],
        "plugins.window_positioning.ignore_window_classes": ["Shell_TrayWnd", "Progman"],
    }
    if overrides:
        defaults.update(overrides)
    config = Mock()
    config.get = lambda key, default=None: defaults.get(key, default)
    return config


@pytest.fixture
def plugin():
    """Fresh plugin instance with mocked win32 imports."""
    with patch("services.wheelhouse.plugins.window_positioning_plugin.win32api") as mock_api:
        mock_api.GetSystemMetrics = Mock(return_value=1920)
        # Patch with different return values for width vs height
        mock_api.GetSystemMetrics = Mock(side_effect=lambda x: 1920 if x == 0 else 1080)
        from services.wheelhouse.plugins.window_positioning_plugin import WindowPositioningPlugin
        p = WindowPositioningPlugin()
    return p


# ---------------------------------------------------------------------------
# Constructor / name
# ---------------------------------------------------------------------------

class TestWindowPositioningInit:
    def test_name(self, plugin):
        assert plugin.name == "window_positioning"

    def test_initial_state(self, plugin):
        assert plugin.state == PluginState.UNINITIALIZED

    def test_default_config(self, plugin):
        assert plugin.target_window_names == ["On-Screen Keyboard", "osk"]
        assert plugin.move_cooldown_seconds == 0.5
        assert plugin.clearance_gap_pixels == 5


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestWindowPositioningInitialize:
    @pytest.mark.asyncio
    async def test_initialize_loads_config(self, mock_event_bus):
        config = _make_config()

        with patch("services.wheelhouse.plugins.window_positioning_plugin.win32api") as mock_api:
            mock_api.GetSystemMetrics = Mock(return_value=1920)
            with patch("services.wheelhouse.plugins.window_positioning_plugin.win32con") as mock_con:
                mock_con.SM_CXSCREEN = 0
                mock_con.SM_CYSCREEN = 1
                from services.wheelhouse.plugins.window_positioning_plugin import WindowPositioningPlugin
                p = WindowPositioningPlugin()
                await p.initialize(config, mock_event_bus)

        assert p.state == PluginState.INITIALIZED
        assert p.move_cooldown_seconds == 0.5
        assert p.clearance_gap_pixels == 5

    @pytest.mark.asyncio
    async def test_initialize_rejects_negative_cooldown(self, mock_event_bus):
        config = _make_config({"plugins.window_positioning.move_cooldown_seconds": -1})

        with patch("services.wheelhouse.plugins.window_positioning_plugin.win32api") as mock_api:
            mock_api.GetSystemMetrics = Mock(return_value=1920)
            with patch("services.wheelhouse.plugins.window_positioning_plugin.win32con"):
                from services.wheelhouse.plugins.window_positioning_plugin import WindowPositioningPlugin
                p = WindowPositioningPlugin()
                with pytest.raises(ValueError, match="move_cooldown_seconds must be non-negative"):
                    await p.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_rejects_negative_clearance(self, mock_event_bus):
        config = _make_config({"plugins.window_positioning.clearance_gap_pixels": -5})

        with patch("services.wheelhouse.plugins.window_positioning_plugin.win32api") as mock_api:
            mock_api.GetSystemMetrics = Mock(return_value=1920)
            with patch("services.wheelhouse.plugins.window_positioning_plugin.win32con"):
                from services.wheelhouse.plugins.window_positioning_plugin import WindowPositioningPlugin
                p = WindowPositioningPlugin()
                with pytest.raises(ValueError, match="clearance_gap_pixels must be non-negative"):
                    await p.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_zero_cooldown_is_valid(self, mock_event_bus):
        config = _make_config({"plugins.window_positioning.move_cooldown_seconds": 0})

        with patch("services.wheelhouse.plugins.window_positioning_plugin.win32api") as mock_api:
            mock_api.GetSystemMetrics = Mock(return_value=1920)
            with patch("services.wheelhouse.plugins.window_positioning_plugin.win32con"):
                from services.wheelhouse.plugins.window_positioning_plugin import WindowPositioningPlugin
                p = WindowPositioningPlugin()
                await p.initialize(config, mock_event_bus)

        assert p.move_cooldown_seconds == 0


# ---------------------------------------------------------------------------
# Clear position finding (pure math - most testable)
# ---------------------------------------------------------------------------

class TestFindClearPosition:
    """Tests for _find_clear_position - the core positioning algorithm.

    Uses a 1920x1080 screen. Target window is the on-screen keyboard.
    Obstructed rect is whatever window overlaps the keyboard.
    """

    def _make_plugin(self, screen_w=1920, screen_h=1080, gap=5):
        from services.wheelhouse.plugins.window_positioning_plugin import WindowPositioningPlugin
        p = WindowPositioningPlugin()
        p._screen_width = screen_w
        p._screen_height = screen_h
        p.clearance_gap_pixels = gap
        return p

    def test_right_position_preferred_when_space_available(self):
        """When obstructed window is in center-left, should prefer right."""
        p = self._make_plugin()
        # Target keyboard: at (400, 700, 300, 200)
        # Obstructed window: (350, 600, 200, 150)
        result = p._find_clear_position(
            obstructed_rect=(350, 600, 200, 150),
            tx=400, ty=700, tw=300, th=200
        )
        new_x, new_y = result
        # Should have moved - not same as original
        assert (new_x, new_y) != (400, 700)
        # Should be on screen
        assert 0 <= new_x <= 1920 - 300
        assert 0 <= new_y <= 1080 - 200

    def test_returns_original_when_no_valid_position(self):
        """On tiny screen where nothing fits, returns original position."""
        p = self._make_plugin(screen_w=100, screen_h=100, gap=5)
        # Target: 80x80, obstructed fills most of the screen
        result = p._find_clear_position(
            obstructed_rect=(0, 0, 100, 100),
            tx=10, ty=10, tw=80, th=80
        )
        # No valid position exists - should return original
        assert result == (10, 10)

    def test_positions_actually_clear_obstruction(self):
        """Every candidate position must actually clear the obstructed window."""
        p = self._make_plugin()
        obstruction = (400, 300, 200, 150)  # x=400, y=300, w=200, h=150
        tx, ty, tw, th = 450, 350, 300, 200

        new_x, new_y = p._find_clear_position(obstruction, tx, ty, tw, th)

        ox, oy, ow, oh = obstruction
        # Check that new position does not overlap obstruction
        overlap = not (new_x >= ox + ow or new_x + tw <= ox or
                      new_y >= oy + oh or new_y + th <= oy)
        assert not overlap or (new_x == tx and new_y == ty)

    def test_closest_position_selected(self):
        """Should select the position closest to current when multiple valid."""
        p = self._make_plugin()
        # Target at center screen
        tx, ty, tw, th = 800, 500, 200, 100
        # Small obstruction just overlapping
        obstruction = (750, 480, 100, 50)

        new_x, new_y = p._find_clear_position(obstruction, tx, ty, tw, th)

        # Should have moved, but not far
        distance = math.sqrt((new_x - tx)**2 + (new_y - ty)**2)
        assert distance < 300  # Reasonable move distance

    def test_screen_boundary_respected(self):
        """Positions that go off-screen should be rejected."""
        p = self._make_plugin()
        # Target near right edge
        tx, ty, tw, th = 1700, 500, 300, 200
        # Obstruction overlapping target
        obstruction = (1650, 450, 200, 300)

        new_x, new_y = p._find_clear_position(obstruction, tx, ty, tw, th)

        # Must be within screen
        assert new_x >= 0
        assert new_y >= 0
        assert new_x + tw <= 1920
        assert new_y + th <= 1080


# ---------------------------------------------------------------------------
# Window ignorability
# ---------------------------------------------------------------------------

class TestWindowIgnorable:
    def _make_plugin(self, ignore_titles=None, ignore_classes=None):
        from services.wheelhouse.plugins.window_positioning_plugin import WindowPositioningPlugin
        p = WindowPositioningPlugin()
        p.ignore_window_titles = ignore_titles or ["Program Manager", "Task Switching"]
        p.ignore_window_classes = ignore_classes or ["Shell_TrayWnd", "Progman"]
        return p

    def test_ignored_title(self):
        p = self._make_plugin()
        assert p._is_window_ignorable(0, "Program Manager", "") is True

    def test_ignored_class(self):
        p = self._make_plugin()
        assert p._is_window_ignorable(0, "", "Shell_TrayWnd") is True

    def test_case_insensitive_title(self):
        p = self._make_plugin()
        assert p._is_window_ignorable(0, "program manager", "") is True

    def test_case_insensitive_class(self):
        p = self._make_plugin()
        assert p._is_window_ignorable(0, "", "shell_traywnd") is True

    def test_normal_window_not_ignored(self):
        p = self._make_plugin()
        assert p._is_window_ignorable(0, "Visual Studio Code", "Chrome_WidgetWin_1") is False

    def test_empty_title_and_class_not_ignored(self):
        p = self._make_plugin()
        assert p._is_window_ignorable(0, "", "") is False

    def test_none_title_not_ignored(self):
        p = self._make_plugin()
        assert p._is_window_ignorable(0, None, "SomeClass") is False


# ---------------------------------------------------------------------------
# Window rect helper
# ---------------------------------------------------------------------------

class TestGetWindowRect:
    def test_returns_xywh_tuple(self):
        from services.wheelhouse.plugins.window_positioning_plugin import WindowPositioningPlugin
        p = WindowPositioningPlugin()

        with patch("services.wheelhouse.plugins.window_positioning_plugin.win32gui") as mock_gui:
            mock_gui.GetWindowRect = Mock(return_value=(100, 200, 500, 600))
            result = p._get_window_rect(12345)

        assert result == (100, 200, 400, 400)  # (x, y, width, height)

    def test_returns_none_on_error(self):
        from services.wheelhouse.plugins.window_positioning_plugin import WindowPositioningPlugin
        p = WindowPositioningPlugin()

        with patch("services.wheelhouse.plugins.window_positioning_plugin.win32gui") as mock_gui:
            mock_gui.GetWindowRect = Mock(side_effect=RuntimeError("invalid hwnd"))
            result = p._get_window_rect(0)

        assert result is None


# ---------------------------------------------------------------------------
# Health status
# ---------------------------------------------------------------------------

class TestWindowPositioningHealth:
    def test_healthy_when_running(self, plugin):
        plugin._state = PluginState.RUNNING
        status = plugin.get_health_status()
        assert status["status"] == "healthy"
        assert status["state"] == "running"

    def test_unhealthy_when_not_running(self, plugin):
        plugin._state = PluginState.FAILED
        status = plugin.get_health_status()
        assert status["status"] == "unhealthy"

    def test_health_includes_target_window_state(self, plugin):
        plugin._target_window_hwnd = 12345
        status = plugin.get_health_status()
        assert status["target_window_found"] is True

    def test_health_no_target_window(self, plugin):
        plugin._target_window_hwnd = None
        status = plugin.get_health_status()
        assert status["target_window_found"] is False

    def test_health_includes_screen_dimensions(self, plugin):
        plugin._screen_width = 1920
        plugin._screen_height = 1080
        status = plugin.get_health_status()
        assert status["screen_dimensions"] == (1920, 1080)


# ---------------------------------------------------------------------------
# Event proc filtering
# ---------------------------------------------------------------------------

class TestEventProcFiltering:
    def test_filters_cursor_location_change(self, plugin):
        """Cursor movement events should be silently filtered."""
        from services.wheelhouse.plugins.window_positioning_plugin import (
            EVENT_OBJECT_LOCATIONCHANGE,
        )
        import win32con

        plugin._running = True
        plugin._win_event_proc(
            None,                          # hWinEventHook
            EVENT_OBJECT_LOCATIONCHANGE,   # event
            123,                           # hwnd
            win32con.OBJID_CURSOR,         # idObject
            0, 0, 0                        # idChild, dwEventThread, dwmsEventTime
        )
        # If we got here without error, the filter worked (no crash, no processing)

    def test_filters_hide_for_non_target_window(self, plugin):
        """HIDE events for non-target windows should be silent."""
        from services.wheelhouse.plugins.window_positioning_plugin import EVENT_OBJECT_HIDE

        plugin._running = True
        plugin._target_window_hwnd = 999

        plugin._win_event_proc(None, EVENT_OBJECT_HIDE, 123, 0, 0, 0, 0)
        # Target should remain unchanged
        assert plugin._target_window_hwnd == 999

    def test_hide_clears_target_when_matching(self, plugin):
        """HIDE event for target window should clear target state."""
        from services.wheelhouse.plugins.window_positioning_plugin import EVENT_OBJECT_HIDE

        plugin._running = True
        plugin._target_window_hwnd = 123
        plugin._target_window_rect = (0, 0, 300, 200)

        plugin._win_event_proc(None, EVENT_OBJECT_HIDE, 123, 0, 0, 0, 0)

        assert plugin._target_window_hwnd is None
        assert plugin._target_window_rect is None

    def test_destroy_clears_target_when_matching(self, plugin):
        """DESTROY event for target window should clear target state."""
        from services.wheelhouse.plugins.window_positioning_plugin import EVENT_OBJECT_DESTROY

        plugin._running = True
        plugin._target_window_hwnd = 456
        plugin._target_window_rect = (0, 0, 300, 200)

        plugin._win_event_proc(None, EVENT_OBJECT_DESTROY, 456, 0, 0, 0, 0)

        assert plugin._target_window_hwnd is None
        assert plugin._target_window_rect is None

    def test_filters_invalid_hwnd(self, plugin):
        """hwnd of 0 or None should be silently filtered."""
        from services.wheelhouse.plugins.window_positioning_plugin import EVENT_SYSTEM_FOREGROUND

        plugin._running = True
        # Should not raise
        plugin._win_event_proc(None, EVENT_SYSTEM_FOREGROUND, 0, 0, 0, 0, 0)
        plugin._win_event_proc(None, EVENT_SYSTEM_FOREGROUND, None, 0, 0, 0, 0)

    def test_filters_own_message_window(self, plugin):
        """Plugin's own message window should be ignored."""
        from services.wheelhouse.plugins.window_positioning_plugin import EVENT_SYSTEM_FOREGROUND

        plugin._running = True
        plugin._message_window = 777

        plugin._win_event_proc(None, EVENT_SYSTEM_FOREGROUND, 777, 0, 0, 0, 0)

    def test_not_running_does_nothing(self, plugin):
        """When plugin is stopped, events should be ignored."""
        from services.wheelhouse.plugins.window_positioning_plugin import EVENT_SYSTEM_FOREGROUND

        plugin._running = False
        plugin._win_event_proc(None, EVENT_SYSTEM_FOREGROUND, 123, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Stop lifecycle
# ---------------------------------------------------------------------------

class TestWindowPositioningStop:
    @pytest.mark.asyncio
    async def test_stop_sets_stopped(self, plugin):
        plugin._state = PluginState.RUNNING
        plugin._running = True
        plugin._hook_thread = None

        await plugin.stop()
        assert plugin.state == PluginState.STOPPED
        assert plugin._running is False
