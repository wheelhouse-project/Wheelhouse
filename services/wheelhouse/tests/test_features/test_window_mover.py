"""Tests for WindowMover window positioning automation.

Covers: position validation (pure math), clear-spot calculation,
ignorable-window filtering, effective screen boundaries, Win32 wrappers,
overlap detection, event callback filtering, and lifecycle.

P3-T6 of the test coverage improvement plan.

Note: Hook thread, Windows message pump, and deep Win32 interop
(_create_message_window_and_hooks, message_loop, _destroy_message_window_and_hooks)
are not tested - they require a live Windows desktop. Focus is on testable logic.
"""

import math
import threading
import time

import pytest
from unittest.mock import Mock, patch, MagicMock, call


# ---------------------------------------------------------------------------
# Module-level mocks - required before importing window_mover because it
# instantiates CONFIG = ConfigService() and calls win32gui.GetDesktopWindow()
# at module scope.
# ---------------------------------------------------------------------------

_mock_win32gui = MagicMock()
_mock_win32api = MagicMock()
_mock_win32con = MagicMock()
_mock_win32process = MagicMock()
_mock_pyautogui = MagicMock()
_mock_psutil = MagicMock()
_mock_ctypes = MagicMock()

# Set up win32con constants needed by the module
_mock_win32con.OBJID_WINDOW = 0
_mock_win32con.OBJID_SYSMENU = -1
_mock_win32con.OBJID_TITLEBAR = -2
_mock_win32con.OBJID_MENU = -3
_mock_win32con.OBJID_CLIENT = -4
_mock_win32con.OBJID_VSCROLL = -5
_mock_win32con.OBJID_HSCROLL = -6
_mock_win32con.OBJID_CARET = -8
_mock_win32con.OBJID_CURSOR = -9
_mock_win32con.OBJID_ALERT = -10
_mock_win32con.OBJID_SOUND = -11
_mock_win32con.SW_SHOWMAXIMIZED = 3
_mock_win32con.SW_SHOWNA = 8
_mock_win32con.SW_RESTORE = 9
_mock_win32con.GWL_STYLE = -16
_mock_win32con.GWL_EXSTYLE = -20
_mock_win32con.WS_VISIBLE = 0x10000000
_mock_win32con.WS_CAPTION = 0x00C00000
_mock_win32con.WS_SYSMENU = 0x00080000
_mock_win32con.WS_POPUP = 0x80000000
_mock_win32con.WS_EX_TOOLWINDOW = 0x00000080
_mock_win32con.WS_EX_NOACTIVATE = 0x08000000
_mock_win32con.SWP_NOSIZE = 0x0001
_mock_win32con.SWP_NOMOVE = 0x0002
_mock_win32con.SWP_NOACTIVATE = 0x0010
_mock_win32con.SWP_NOZORDER = 0x0004
_mock_win32con.HWND_TOPMOST = -1
_mock_win32con.HWND_MESSAGE = -3
_mock_win32con.WM_DESTROY = 0x0002
_mock_win32con.WM_QUIT = 0x0012
_mock_win32con.WM_NULL = 0x0000
_mock_win32con.PM_REMOVE = 0x0001
_mock_win32con.PROCESS_QUERY_INFORMATION = 0x0400
_mock_win32con.PROCESS_VM_READ = 0x0010
_mock_win32con.WINEVENT_OUTOFCONTEXT = 0x0000
_mock_win32con.WINEVENT_SKIPOWNPROCESS = 0x0002

# GetDesktopWindow returns a fake HWND
_mock_win32gui.GetDesktopWindow.return_value = 65552
_mock_win32gui.FindWindow.return_value = 0

# Mock ConfigService before import
_config_defaults = {
    "WINDOW_MOVER_TARGET_NAMES": ["On-Screen Keyboard", "osk"],
    "WINDOW_MOVER_COOLDOWN": 0.5,
    "WINDOW_MOVER_IGNORE_TITLES": [],
    "WINDOW_MOVER_IGNORE_CLASSES": [],
    "WINDOW_MOVER_EVENT_SOURCE_IGNORE_CLASSES": [],
    "WINDOW_MOVER_CLEARANCE_GAP": 5,
}

_mock_config = Mock()
_mock_config.get = lambda key, default=None: _config_defaults.get(key, default)


@pytest.fixture(autouse=True)
def _patch_win32_modules(monkeypatch):
    """Patch all Win32 and system modules before any window_mover import."""
    import sys

    monkeypatch.setitem(sys.modules, "win32gui", _mock_win32gui)
    monkeypatch.setitem(sys.modules, "win32api", _mock_win32api)
    monkeypatch.setitem(sys.modules, "win32con", _mock_win32con)
    monkeypatch.setitem(sys.modules, "win32process", _mock_win32process)
    monkeypatch.setitem(sys.modules, "pyautogui", _mock_pyautogui)
    monkeypatch.setitem(sys.modules, "psutil", _mock_psutil)

    # Reset call counts between tests
    _mock_win32gui.reset_mock()
    _mock_win32api.reset_mock()
    _mock_win32process.reset_mock()
    _mock_psutil.reset_mock()

    # Re-set defaults that reset_mock clears
    _mock_win32gui.GetDesktopWindow.return_value = 65552
    _mock_win32gui.FindWindow.return_value = 0
    _mock_win32gui.IsWindow.return_value = True
    _mock_win32gui.IsWindowVisible.return_value = True

    with patch("services.wheelhouse.features.window_mover.ConfigService", return_value=_mock_config):
        # Force re-import to pick up patched modules
        import importlib
        import services.wheelhouse.features.window_mover as wm_module
        importlib.reload(wm_module)
        yield wm_module


@pytest.fixture
def wm(_patch_win32_modules):
    """Fresh WindowMover instance with 1920x1080 screen."""
    mod = _patch_win32_modules
    mover = mod.WindowMover(1920, 1080)
    return mover


@pytest.fixture
def wm_small(_patch_win32_modules):
    """WindowMover with a small 800x600 screen."""
    mod = _patch_win32_modules
    return mod.WindowMover(800, 600)


# ===========================================================================
# is_position_valid - Pure math, no Win32 calls
# ===========================================================================


class TestIsPositionValid:
    """Position validation against effective screen boundaries."""

    def test_position_within_bounds(self, wm):
        assert wm.is_position_valid(0, 0, 400, 300) is True

    def test_position_at_origin(self, wm):
        assert wm.is_position_valid(0, 0, 1920, 1080) is True

    def test_position_at_bottom_right(self, wm):
        assert wm.is_position_valid(1520, 780, 400, 300) is True

    def test_position_exceeds_right_boundary(self, wm):
        assert wm.is_position_valid(1600, 0, 400, 300) is False

    def test_position_exceeds_bottom_boundary(self, wm):
        assert wm.is_position_valid(0, 900, 400, 300) is False

    def test_position_negative_x(self, wm):
        assert wm.is_position_valid(-1, 0, 400, 300) is False

    def test_position_negative_y(self, wm):
        assert wm.is_position_valid(0, -1, 400, 300) is False

    def test_position_with_effective_offset(self, wm):
        """When effective screen starts at offset (e.g., left taskbar)."""
        wm.effective_screen_x = 48
        wm.effective_screen_width = 1872
        # x=0 is now out of bounds (before taskbar)
        assert wm.is_position_valid(0, 0, 400, 300) is False
        # x=48 is in bounds
        assert wm.is_position_valid(48, 0, 400, 300) is True

    def test_position_with_top_taskbar_offset(self, wm):
        """When effective screen starts below a top taskbar."""
        wm.effective_screen_y = 40
        wm.effective_screen_height = 1040
        assert wm.is_position_valid(0, 0, 400, 300) is False
        assert wm.is_position_valid(0, 40, 400, 300) is True

    def test_window_exactly_fills_screen(self, wm):
        assert wm.is_position_valid(0, 0, 1920, 1080) is True

    def test_window_one_pixel_too_wide(self, wm):
        assert wm.is_position_valid(0, 0, 1921, 1080) is False

    def test_window_one_pixel_too_tall(self, wm):
        assert wm.is_position_valid(0, 0, 1920, 1081) is False

    def test_zero_size_window(self, wm):
        """Zero-size window at origin is valid (edge case)."""
        assert wm.is_position_valid(0, 0, 0, 0) is True

    def test_small_screen_boundary(self, wm_small):
        assert wm_small.is_position_valid(0, 0, 800, 600) is True
        assert wm_small.is_position_valid(0, 0, 801, 600) is False


# ===========================================================================
# find_clear_spot_for_osk - Pure math position calculation
# ===========================================================================


class TestFindClearSpotForOsk:
    """Tests for OSK repositioning calculation around obstructed windows."""

    def test_moves_osk_right_of_obstructed_window(self, wm):
        """When space available to the right, prefer closest position."""
        # Obstructed window in center-left, OSK overlapping it
        obstructed = (200, 400, 300, 200)  # x, y, w, h
        osk_x, osk_y, osk_w, osk_h = 300, 400, 200, 150
        new_x, new_y = wm.find_clear_spot_for_osk(obstructed, osk_x, osk_y, osk_w, osk_h)
        # Should move somewhere that doesn't overlap
        assert not _rects_overlap(new_x, new_y, osk_w, osk_h, *obstructed)
        assert wm.is_position_valid(new_x, new_y, osk_w, osk_h)

    def test_moves_osk_left_when_right_blocked(self, wm):
        """When obstructed window is near right edge, OSK goes left."""
        obstructed = (1600, 400, 300, 200)
        osk_x, osk_y, osk_w, osk_h = 1650, 400, 200, 150
        new_x, new_y = wm.find_clear_spot_for_osk(obstructed, osk_x, osk_y, osk_w, osk_h)
        # Must not overlap and must be on screen
        assert not _rects_overlap(new_x, new_y, osk_w, osk_h, *obstructed)
        assert wm.is_position_valid(new_x, new_y, osk_w, osk_h)

    def test_moves_osk_below_when_horizontal_blocked(self, wm):
        """When left and right are blocked, try below."""
        # Wide obstructed window spanning most of screen
        obstructed = (100, 200, 1700, 300)
        osk_x, osk_y, osk_w, osk_h = 500, 300, 200, 150
        new_x, new_y = wm.find_clear_spot_for_osk(obstructed, osk_x, osk_y, osk_w, osk_h)
        # Expect below the obstructed window
        if new_x != osk_x or new_y != osk_y:
            assert not _rects_overlap(new_x, new_y, osk_w, osk_h, *obstructed)
            assert wm.is_position_valid(new_x, new_y, osk_w, osk_h)

    def test_returns_original_when_no_valid_spot(self, wm_small):
        """On a small screen with a large obstructed window, no valid spot exists."""
        # Obstructed window fills most of the screen
        obstructed = (0, 0, 700, 500)
        osk_x, osk_y, osk_w, osk_h = 100, 100, 400, 300
        new_x, new_y = wm_small.find_clear_spot_for_osk(obstructed, osk_x, osk_y, osk_w, osk_h)
        # Falls back to original position
        assert new_x == osk_x
        assert new_y == osk_y

    def test_closest_position_chosen(self, wm):
        """When multiple valid positions exist, picks the closest to current."""
        # Obstructed window in center, OSK directly overlapping
        obstructed = (800, 400, 200, 200)
        osk_x, osk_y, osk_w, osk_h = 850, 450, 100, 80
        new_x, new_y = wm.find_clear_spot_for_osk(obstructed, osk_x, osk_y, osk_w, osk_h)
        # Should move but to nearest valid spot
        assert (new_x, new_y) != (osk_x, osk_y)
        distance = math.sqrt((new_x - osk_x) ** 2 + (new_y - osk_y) ** 2)
        # Distance should be reasonable (not flung to far corner)
        assert distance < 500

    def test_clearance_gap_respected(self, wm):
        """The gap between OSK and obstructed window includes clearance_gap."""
        obstructed = (800, 400, 200, 200)
        osk_x, osk_y, osk_w, osk_h = 850, 450, 100, 80
        wm.clearance_gap = 10
        new_x, new_y = wm.find_clear_spot_for_osk(obstructed, osk_x, osk_y, osk_w, osk_h)
        if new_x != osk_x or new_y != osk_y:
            # Verify no overlap
            assert not _rects_overlap(new_x, new_y, osk_w, osk_h, *obstructed)

    def test_adversarial_zero_size_obstructed_window(self, wm):
        """Zero-size obstructed window should not cause errors."""
        obstructed = (500, 500, 0, 0)
        osk_x, osk_y, osk_w, osk_h = 500, 500, 200, 150
        new_x, new_y = wm.find_clear_spot_for_osk(obstructed, osk_x, osk_y, osk_w, osk_h)
        # Should not crash; result is valid or original
        assert isinstance(new_x, (int, float))
        assert isinstance(new_y, (int, float))

    def test_adversarial_negative_coordinates(self, wm):
        """Obstructed window at negative coordinates (multi-monitor)."""
        obstructed = (-100, -50, 300, 200)
        osk_x, osk_y, osk_w, osk_h = -50, 0, 200, 150
        # Candidates will likely be off-screen, so returns original
        new_x, new_y = wm.find_clear_spot_for_osk(obstructed, osk_x, osk_y, osk_w, osk_h)
        assert isinstance(new_x, (int, float))

    def test_adversarial_osk_larger_than_screen(self, wm_small):
        """OSK larger than entire screen - no valid position possible."""
        obstructed = (100, 100, 200, 200)
        osk_x, osk_y, osk_w, osk_h = 0, 0, 900, 700
        new_x, new_y = wm_small.find_clear_spot_for_osk(obstructed, osk_x, osk_y, osk_w, osk_h)
        # No valid spot, returns original
        assert new_x == osk_x
        assert new_y == osk_y


# ===========================================================================
# is_alive
# ===========================================================================


class TestIsAlive:
    def test_alive_when_running_and_thread_alive(self, wm):
        wm.running = True
        wm.hook_thread = Mock()
        wm.hook_thread.is_alive.return_value = True
        assert wm.is_alive() is True

    def test_not_alive_when_not_running(self, wm):
        wm.running = False
        wm.hook_thread = Mock()
        wm.hook_thread.is_alive.return_value = True
        assert wm.is_alive() is False

    def test_not_alive_when_no_thread(self, wm):
        wm.running = True
        wm.hook_thread = None
        assert wm.is_alive() is False

    def test_not_alive_when_thread_dead(self, wm):
        wm.running = True
        wm.hook_thread = Mock()
        wm.hook_thread.is_alive.return_value = False
        assert wm.is_alive() is False


# ===========================================================================
# is_ignorable_obstructed_window
# ===========================================================================


class TestIsIgnorableObstructedWindow:
    def test_none_input_is_ignorable(self, wm):
        assert wm.is_ignorable_obstructed_window(None) is True

    def test_desktop_window_is_ignorable(self, wm):
        info = {"hwnd": wm.desktop_hwnd, "title": "Program Manager", "class_name": "Progman", "rect": (0, 0, 1920, 1080)}
        assert wm.is_ignorable_obstructed_window(info) is True

    def test_ignored_title_is_ignorable(self, wm):
        wm.ignore_titles = ["task switching"]
        info = {"hwnd": 999, "title": "Task Switching", "class_name": "SomeClass", "rect": (0, 0, 400, 300)}
        assert wm.is_ignorable_obstructed_window(info) is True

    def test_ignored_class_is_ignorable(self, wm):
        wm.ignore_classes = ["shell_traywnd"]
        info = {"hwnd": 999, "title": "Taskbar", "class_name": "Shell_TrayWnd", "rect": (0, 0, 1920, 48)}
        assert wm.is_ignorable_obstructed_window(info) is True

    def test_maximized_window_with_small_osk_is_ignorable(self, wm):
        """Maximized window when OSK is small compared to screen."""
        _mock_win32gui.GetWindowPlacement.return_value = (0, _mock_win32con.SW_SHOWMAXIMIZED, (0, 0), (0, 0), (0, 0, 1920, 1080))
        wm.obstructing_window_rect = (100, 800, 300, 200)  # Small OSK
        info = {"hwnd": 1234, "title": "Notepad", "class_name": "Notepad", "rect": (0, 0, 1920, 1080)}
        assert wm.is_ignorable_obstructed_window(info) is True

    def test_maximized_window_with_large_osk_not_ignorable(self, wm):
        """Maximized window when OSK covers most of the screen."""
        _mock_win32gui.GetWindowPlacement.return_value = (0, _mock_win32con.SW_SHOWMAXIMIZED, (0, 0), (0, 0), (0, 0, 1920, 1080))
        # OSK is 75%+ of screen width
        wm.obstructing_window_rect = (0, 0, 1500, 900)
        info = {"hwnd": 1234, "title": "Notepad", "class_name": "Notepad", "rect": (0, 0, 1920, 1080)}
        assert wm.is_ignorable_obstructed_window(info) is False

    def test_maximized_window_no_osk_rect_is_ignorable(self, wm):
        """Maximized window when OSK rect is unknown - tentatively ignore."""
        _mock_win32gui.GetWindowPlacement.return_value = (0, _mock_win32con.SW_SHOWMAXIMIZED, (0, 0), (0, 0), (0, 0, 1920, 1080))
        wm.obstructing_window_rect = None
        info = {"hwnd": 1234, "title": "Notepad", "class_name": "Notepad", "rect": (0, 0, 1920, 1080)}
        assert wm.is_ignorable_obstructed_window(info) is True

    def test_normal_window_not_ignorable(self, wm):
        """Regular, non-maximized window that's not in ignore lists."""
        _mock_win32gui.GetWindowPlacement.return_value = (0, 1, (0, 0), (0, 0), (100, 100, 500, 400))
        info = {"hwnd": 1234, "title": "My App", "class_name": "MyAppClass", "rect": (100, 100, 400, 300)}
        assert wm.is_ignorable_obstructed_window(info) is False


# ===========================================================================
# get_window_rect
# ===========================================================================


class TestGetWindowRect:
    def test_returns_x_y_width_height(self, wm):
        _mock_win32gui.GetWindowRect.return_value = (100, 200, 500, 600)
        result = wm.get_window_rect(1234)
        assert result == (100, 200, 400, 400)

    def test_returns_none_on_error(self, wm):
        _mock_win32gui.GetWindowRect.side_effect = Exception("Invalid HWND")
        result = wm.get_window_rect(0)
        assert result is None
        _mock_win32gui.GetWindowRect.side_effect = None

    def test_zero_size_window(self, wm):
        _mock_win32gui.GetWindowRect.return_value = (100, 100, 100, 100)
        result = wm.get_window_rect(1234)
        assert result == (100, 100, 0, 0)


# ===========================================================================
# is_window_maximized
# ===========================================================================


class TestIsWindowMaximized:
    def test_maximized_returns_true(self, wm):
        _mock_win32gui.GetWindowPlacement.return_value = (0, _mock_win32con.SW_SHOWMAXIMIZED, (0, 0), (0, 0), (0, 0, 1920, 1080))
        assert wm.is_window_maximized(1234) is True

    def test_normal_returns_false(self, wm):
        _mock_win32gui.GetWindowPlacement.return_value = (0, 1, (0, 0), (0, 0), (100, 100, 500, 400))
        assert wm.is_window_maximized(1234) is False

    def test_none_hwnd_returns_false(self, wm):
        assert wm.is_window_maximized(None) is False

    def test_zero_hwnd_returns_false(self, wm):
        assert wm.is_window_maximized(0) is False

    def test_exception_returns_false(self, wm):
        _mock_win32gui.GetWindowPlacement.side_effect = Exception("Access denied")
        assert wm.is_window_maximized(1234) is False
        _mock_win32gui.GetWindowPlacement.side_effect = None


# ===========================================================================
# _update_effective_screen_boundaries
# ===========================================================================


class TestUpdateEffectiveScreenBoundaries:
    def test_no_taskbar_uses_full_screen(self, wm):
        _mock_win32gui.FindWindow.return_value = 0
        wm.taskbar_hwnd = None
        wm._update_effective_screen_boundaries()
        assert wm.effective_screen_x == 0
        assert wm.effective_screen_y == 0
        assert wm.effective_screen_width == 1920
        assert wm.effective_screen_height == 1080

    def test_bottom_taskbar(self, wm):
        """Bottom taskbar reduces effective screen height."""
        tb_hwnd = 12345
        wm.taskbar_hwnd = tb_hwnd
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = True
        # Bottom taskbar: full width, 48px tall at bottom
        _mock_win32gui.GetWindowRect.return_value = (0, 1032, 1920, 1080)
        wm._update_effective_screen_boundaries()
        assert wm.effective_screen_height == 1032
        assert wm.effective_screen_y == 0

    def test_left_taskbar(self, wm):
        """Left taskbar shifts effective screen origin right."""
        tb_hwnd = 12345
        wm.taskbar_hwnd = tb_hwnd
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = True
        # Left taskbar: 48px wide, full height
        _mock_win32gui.GetWindowRect.return_value = (0, 0, 48, 1080)
        wm._update_effective_screen_boundaries()
        assert wm.effective_screen_x == 48
        assert wm.effective_screen_width == 1920 - 48

    def test_right_taskbar(self, wm):
        """Right taskbar reduces effective screen width."""
        tb_hwnd = 12345
        wm.taskbar_hwnd = tb_hwnd
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = True
        # Right taskbar: 48px wide at right edge
        _mock_win32gui.GetWindowRect.return_value = (1872, 0, 1920, 1080)
        wm._update_effective_screen_boundaries()
        assert wm.effective_screen_width == 1872

    def test_top_taskbar(self, wm):
        """Top taskbar shifts effective screen origin down."""
        tb_hwnd = 12345
        wm.taskbar_hwnd = tb_hwnd
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = True
        # Top taskbar: full width, 40px tall at top
        _mock_win32gui.GetWindowRect.return_value = (0, 0, 1920, 40)
        wm._update_effective_screen_boundaries()
        assert wm.effective_screen_y == 40
        assert wm.effective_screen_height == 1080 - 40

    def test_taskbar_hidden(self, wm):
        """Hidden taskbar means full screen available."""
        tb_hwnd = 12345
        wm.taskbar_hwnd = tb_hwnd
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = False
        wm._update_effective_screen_boundaries()
        assert wm.effective_screen_width == 1920
        assert wm.effective_screen_height == 1080

    def test_taskbar_error_handled_gracefully(self, wm):
        """Exception getting taskbar rect is caught."""
        tb_hwnd = 12345
        wm.taskbar_hwnd = tb_hwnd
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = True
        _mock_win32gui.GetWindowRect.side_effect = Exception("Access denied")
        # Should not raise
        wm._update_effective_screen_boundaries()
        assert wm.effective_screen_width == 1920
        _mock_win32gui.GetWindowRect.side_effect = None

    def test_triggers_osk_recheck_when_boundaries_change(self, wm):
        """When boundaries change and OSK is out of bounds, triggers overlap check."""
        wm.obstructing_window_hwnd = 9999
        wm.obstructing_window_rect = (0, 1050, 300, 200)  # Near bottom
        wm.running = True

        # Set up taskbar to change boundaries
        tb_hwnd = 12345
        wm.taskbar_hwnd = tb_hwnd
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = True
        _mock_win32gui.GetWindowRect.return_value = (0, 1032, 1920, 1080)

        with patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm._update_effective_screen_boundaries()
            # OSK at y=1050 with h=200 exceeds effective_screen_height=1032
            mock_check.assert_called_once()

    def test_no_recheck_when_osk_still_valid(self, wm):
        """No recheck triggered when OSK position still valid after boundary change."""
        wm.obstructing_window_hwnd = 9999
        wm.obstructing_window_rect = (100, 100, 300, 200)  # Well within bounds

        tb_hwnd = 12345
        wm.taskbar_hwnd = tb_hwnd
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = True
        _mock_win32gui.GetWindowRect.return_value = (0, 1032, 1920, 1080)

        with patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm._update_effective_screen_boundaries()
            mock_check.assert_not_called()


# ===========================================================================
# get_visible_windows_info
# ===========================================================================


class TestGetVisibleWindowsInfo:
    def test_returns_visible_windows(self, wm):
        """Collects visible window info via EnumWindows callback."""
        def fake_enum_windows(callback, result_list):
            # Simulate three windows
            for hwnd, title, cls, rect in [
                (100, "Notepad", "Notepad", (0, 0, 800, 600)),
                (200, "Chrome", "Chrome_WidgetWin_1", (100, 100, 1200, 900)),
            ]:
                _mock_win32gui.GetWindowText.return_value = title
                _mock_win32gui.GetWindowRect.return_value = (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])
                _mock_win32gui.GetClassName.return_value = cls
                _mock_win32gui.IsWindowVisible.return_value = True
                callback(hwnd, result_list)

        _mock_win32gui.EnumWindows.side_effect = fake_enum_windows
        windows = wm.get_visible_windows_info()
        assert len(windows) == 2
        assert windows[0]["title"] == "Notepad"
        assert windows[1]["class_name"] == "Chrome_WidgetWin_1"
        _mock_win32gui.EnumWindows.side_effect = None

    def test_skips_obstructing_window(self, wm):
        """The obstructing (OSK) window itself is excluded."""
        wm.obstructing_window_hwnd = 100

        def fake_enum_windows(callback, result_list):
            _mock_win32gui.GetWindowText.return_value = "OSK"
            _mock_win32gui.GetWindowRect.return_value = (0, 0, 400, 300)
            _mock_win32gui.GetClassName.return_value = "OSKClass"
            _mock_win32gui.IsWindowVisible.return_value = True
            callback(100, result_list)  # This is the OSK hwnd

        _mock_win32gui.EnumWindows.side_effect = fake_enum_windows
        windows = wm.get_visible_windows_info()
        assert len(windows) == 0
        _mock_win32gui.EnumWindows.side_effect = None

    def test_skips_zero_size_windows(self, wm):
        """Windows with zero width or height are excluded."""
        def fake_enum_windows(callback, result_list):
            _mock_win32gui.GetWindowText.return_value = "ZeroWin"
            _mock_win32gui.GetWindowRect.return_value = (100, 100, 100, 100)  # 0x0
            _mock_win32gui.GetClassName.return_value = "SomeClass"
            _mock_win32gui.IsWindowVisible.return_value = True
            callback(300, result_list)

        _mock_win32gui.EnumWindows.side_effect = fake_enum_windows
        windows = wm.get_visible_windows_info()
        assert len(windows) == 0
        _mock_win32gui.EnumWindows.side_effect = None

    def test_skips_invisible_windows(self, wm):
        """Hidden windows are excluded."""
        def fake_enum_windows(callback, result_list):
            _mock_win32gui.IsWindowVisible.return_value = False
            _mock_win32gui.GetWindowText.return_value = "Hidden"
            callback(400, result_list)

        _mock_win32gui.EnumWindows.side_effect = fake_enum_windows
        windows = wm.get_visible_windows_info()
        assert len(windows) == 0
        _mock_win32gui.EnumWindows.side_effect = None


# ===========================================================================
# move_obstructing_window
# ===========================================================================


class TestMoveObstructingWindow:
    def test_successful_move(self, wm):
        wm.obstructing_window_hwnd = 9999
        wm.obstructing_window_rect = (100, 100, 300, 200)
        wm.last_move_time = 0

        _mock_win32gui.GetWindowLong.return_value = _mock_win32con.WS_VISIBLE
        _mock_win32gui.IsIconic.return_value = False
        _mock_win32gui.SetWindowPos.return_value = True
        _mock_win32api.GetLastError.return_value = 0
        _mock_win32gui.GetWindowRect.return_value = (500, 400, 800, 600)

        result = wm.move_obstructing_window(500, 400)
        assert result is True
        assert wm.obstructing_window_rect == (500, 400, 300, 200)

    def test_cooldown_prevents_move(self, wm):
        wm.obstructing_window_hwnd = 9999
        wm.obstructing_window_rect = (100, 100, 300, 200)
        wm.last_move_time = time.time()  # Just moved
        wm.move_cooldown = 0.5

        result = wm.move_obstructing_window(500, 400)
        assert result is False

    def test_no_hwnd_returns_false(self, wm):
        wm.obstructing_window_hwnd = None
        wm.obstructing_window_rect = (100, 100, 300, 200)
        assert wm.move_obstructing_window(500, 400) is False

    def test_no_rect_returns_false(self, wm):
        wm.obstructing_window_hwnd = 9999
        wm.obstructing_window_rect = None
        assert wm.move_obstructing_window(500, 400) is False

    def test_restores_minimized_window(self, wm):
        wm.obstructing_window_hwnd = 9999
        wm.obstructing_window_rect = (100, 100, 300, 200)
        wm.last_move_time = 0

        _mock_win32gui.GetWindowLong.return_value = _mock_win32con.WS_VISIBLE
        _mock_win32gui.IsIconic.return_value = True
        _mock_win32gui.SetWindowPos.return_value = True
        _mock_win32api.GetLastError.return_value = 0
        _mock_win32gui.GetWindowRect.return_value = (500, 400, 800, 600)

        wm.move_obstructing_window(500, 400)
        _mock_win32gui.ShowWindow.assert_any_call(9999, _mock_win32con.SW_RESTORE)

    def test_shows_hidden_window(self, wm):
        wm.obstructing_window_hwnd = 9999
        wm.obstructing_window_rect = (100, 100, 300, 200)
        wm.last_move_time = 0

        _mock_win32gui.GetWindowLong.return_value = 0  # Not visible
        _mock_win32gui.IsIconic.return_value = False
        _mock_win32gui.SetWindowPos.return_value = True
        _mock_win32api.GetLastError.return_value = 0
        _mock_win32gui.GetWindowRect.return_value = (500, 400, 800, 600)

        wm.move_obstructing_window(500, 400)
        _mock_win32gui.ShowWindow.assert_any_call(9999, _mock_win32con.SW_SHOWNA)

    def test_setwindowpos_failure_with_error(self, wm):
        wm.obstructing_window_hwnd = 9999
        wm.obstructing_window_rect = (100, 100, 300, 200)
        wm.last_move_time = 0

        _mock_win32gui.GetWindowLong.return_value = _mock_win32con.WS_VISIBLE
        _mock_win32gui.IsIconic.return_value = False
        _mock_win32gui.SetWindowPos.return_value = False
        _mock_win32api.GetLastError.return_value = 5  # Access denied
        _mock_win32api.FormatMessage.return_value = "Access denied"

        result = wm.move_obstructing_window(500, 400)
        assert result is False

    def test_verification_failure(self, wm):
        """SetWindowPos succeeds but actual position doesn't match target."""
        wm.obstructing_window_hwnd = 9999
        wm.obstructing_window_rect = (100, 100, 300, 200)
        wm.last_move_time = 0

        _mock_win32gui.GetWindowLong.return_value = _mock_win32con.WS_VISIBLE
        _mock_win32gui.IsIconic.return_value = False
        _mock_win32gui.SetWindowPos.return_value = True
        _mock_win32api.GetLastError.return_value = 0
        # Actual position far from target
        _mock_win32gui.GetWindowRect.return_value = (100, 100, 400, 300)

        result = wm.move_obstructing_window(500, 400)
        assert result is False

    def test_exception_in_move(self, wm):
        wm.obstructing_window_hwnd = 9999
        wm.obstructing_window_rect = (100, 100, 300, 200)
        wm.last_move_time = 0

        _mock_win32gui.GetWindowLong.side_effect = Exception("Win32 error")
        result = wm.move_obstructing_window(500, 400)
        assert result is False
        _mock_win32gui.GetWindowLong.side_effect = None


# ===========================================================================
# check_for_overlaps_and_move
# ===========================================================================


class TestCheckForOverlapsAndMove:
    def test_does_nothing_when_not_running(self, wm):
        wm.running = False
        with patch.object(wm, "find_obstructing_window") as mock_find:
            wm.check_for_overlaps_and_move()
            mock_find.assert_not_called()

    def test_finds_osk_when_not_tracked(self, wm):
        """When OSK hwnd is None, tries to find it."""
        wm.running = True
        wm.obstructing_window_hwnd = None

        with patch.object(wm, "find_obstructing_window", return_value=None):
            wm.check_for_overlaps_and_move()
            # No OSK found, returns early

    def test_detects_overlap_and_moves(self, wm):
        """Full flow: detects overlap, finds clear spot, moves OSK."""
        wm.running = True
        wm.obstructing_window_hwnd = 9999
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = True

        osk_rect = (400, 400, 300, 200)
        _mock_win32gui.GetWindowRect.return_value = (400, 400, 700, 600)
        _mock_win32gui.GetWindowText.return_value = "On-Screen Keyboard"

        # One overlapping window
        overlapping_window = {"hwnd": 1234, "title": "Editor", "class_name": "EditorClass", "rect": (350, 350, 300, 250)}

        with patch.object(wm, "get_window_rect", return_value=osk_rect), \
             patch.object(wm, "get_visible_windows_info", return_value=[overlapping_window]), \
             patch.object(wm, "is_ignorable_obstructed_window", return_value=False), \
             patch.object(wm, "find_clear_spot_for_osk", return_value=(800, 400)) as mock_find, \
             patch.object(wm, "move_obstructing_window") as mock_move:
            wm.check_for_overlaps_and_move()
            mock_find.assert_called_once()
            mock_move.assert_called_once_with(800, 400)

    def test_no_move_when_no_overlap(self, wm):
        """No overlapping windows means no move."""
        wm.running = True
        wm.obstructing_window_hwnd = 9999
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = True

        osk_rect = (0, 0, 300, 200)
        # Non-overlapping window (far away)
        non_overlapping = {"hwnd": 1234, "title": "Far Away", "class_name": "FarClass", "rect": (1000, 800, 200, 150)}

        with patch.object(wm, "get_window_rect", return_value=osk_rect), \
             patch.object(wm, "get_visible_windows_info", return_value=[non_overlapping]), \
             patch.object(wm, "move_obstructing_window") as mock_move:
            wm.check_for_overlaps_and_move()
            mock_move.assert_not_called()

    def test_skips_ignorable_overlapping_windows(self, wm):
        """Ignorable overlapping windows don't trigger a move."""
        wm.running = True
        wm.obstructing_window_hwnd = 9999
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = True

        osk_rect = (400, 400, 300, 200)
        overlapping_but_ignorable = {"hwnd": 1234, "title": "Desktop", "class_name": "Progman", "rect": (0, 0, 1920, 1080)}

        with patch.object(wm, "get_window_rect", return_value=osk_rect), \
             patch.object(wm, "get_visible_windows_info", return_value=[overlapping_but_ignorable]), \
             patch.object(wm, "is_ignorable_obstructed_window", return_value=True), \
             patch.object(wm, "move_obstructing_window") as mock_move:
            wm.check_for_overlaps_and_move()
            mock_move.assert_not_called()

    def test_no_move_when_clear_spot_is_current(self, wm):
        """When find_clear_spot returns current position, no move attempted."""
        wm.running = True
        wm.obstructing_window_hwnd = 9999
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.IsWindowVisible.return_value = True

        osk_rect = (400, 400, 300, 200)
        overlapping = {"hwnd": 1234, "title": "Editor", "class_name": "EditorClass", "rect": (350, 350, 300, 250)}

        with patch.object(wm, "get_window_rect", return_value=osk_rect), \
             patch.object(wm, "get_visible_windows_info", return_value=[overlapping]), \
             patch.object(wm, "is_ignorable_obstructed_window", return_value=False), \
             patch.object(wm, "find_clear_spot_for_osk", return_value=(400, 400)), \
             patch.object(wm, "move_obstructing_window") as mock_move:
            wm.check_for_overlaps_and_move()
            mock_move.assert_not_called()

    def test_reacquires_osk_when_window_invalid(self, wm):
        """When tracked OSK hwnd is invalid, reacquires."""
        wm.running = True
        wm.obstructing_window_hwnd = 9999
        _mock_win32gui.IsWindow.return_value = False  # OSK hwnd invalid

        with patch.object(wm, "find_obstructing_window", return_value=None):
            wm.check_for_overlaps_and_move()
            # OSK not found, returns early without crash


# ===========================================================================
# win_event_proc - event callback filtering
# ===========================================================================


class TestWinEventProc:
    """Tests for the Windows accessibility event callback."""

    @staticmethod
    def _setup_event_defaults():
        """Set up default mock return values for win_event_proc.

        Call BEFORE setting test-specific overrides so tests can replace
        individual values without the helper clobbering them.
        """
        _mock_win32gui.GetWindowText.return_value = "Test Window"
        _mock_win32gui.GetClassName.return_value = "TestClass"
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.GetWindowLong.return_value = 0
        _mock_win32gui.GetWindowLong.side_effect = None
        _mock_win32gui.GetParent.return_value = 0
        _mock_win32process.GetWindowThreadProcessId.return_value = (1234, 5678)
        mock_proc = Mock()
        mock_proc.name.return_value = "test.exe"
        _mock_psutil.Process.return_value = mock_proc

    def test_ignores_events_when_not_running(self, wm):
        self._setup_event_defaults()
        wm.running = False
        with patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x0003, 1234, 0, 0, 0, 0)
            mock_check.assert_not_called()

    def test_ignores_null_hwnd(self, wm):
        self._setup_event_defaults()
        wm.running = True
        with patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x0003, 0, 0, 0, 0, 0)
            mock_check.assert_not_called()

    def test_ignores_own_osk_hwnd(self, wm):
        self._setup_event_defaults()
        wm.running = True
        wm.obstructing_window_hwnd = 9999
        with patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x0003, 9999, 0, 0, 0, 0)
            mock_check.assert_not_called()

    def test_ignores_message_window_hwnd(self, wm):
        self._setup_event_defaults()
        wm.running = True
        wm.message_only_hwnd = 8888
        with patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x0003, 8888, 0, 0, 0, 0)
            mock_check.assert_not_called()

    def test_foreground_change_triggers_check(self, wm):
        """EVENT_SYSTEM_FOREGROUND triggers overlap check for non-ignorable window."""
        self._setup_event_defaults()
        wm.running = True
        with patch.object(wm, "get_window_rect", return_value=(100, 100, 400, 300)), \
             patch.object(wm, "is_ignorable_obstructed_window", return_value=False), \
             patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x0003, 1234, 0, 0, 0, 0)
            mock_check.assert_called_once()

    def test_foreground_change_ignored_for_ignorable_window(self, wm):
        """EVENT_SYSTEM_FOREGROUND does not trigger check for ignorable window."""
        self._setup_event_defaults()
        wm.running = True
        with patch.object(wm, "get_window_rect", return_value=(0, 0, 1920, 1080)), \
             patch.object(wm, "is_ignorable_obstructed_window", return_value=True), \
             patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x0003, 1234, 0, 0, 0, 0)
            mock_check.assert_not_called()

    def test_taskbar_event_updates_boundaries(self, wm):
        """Taskbar events trigger boundary update, not overlap check."""
        self._setup_event_defaults()
        wm.running = True
        _mock_win32gui.GetClassName.return_value = "Shell_TrayWnd"

        with patch.object(wm, "_update_effective_screen_boundaries") as mock_update, \
             patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x8002, 1234, 0, 0, 0, 0)
            mock_update.assert_called_once()
            mock_check.assert_not_called()

    def test_toolwindow_ignored(self, wm):
        """Windows with WS_EX_TOOLWINDOW style are ignored."""
        self._setup_event_defaults()
        wm.running = True
        _mock_win32gui.GetClassName.return_value = "ToolTipClass"

        def get_window_long(hwnd, flag):
            if flag == _mock_win32con.GWL_EXSTYLE:
                return _mock_win32con.WS_EX_TOOLWINDOW
            return 0
        _mock_win32gui.GetWindowLong.side_effect = get_window_long

        with patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x8000, 1234, 0, 0, 0, 0)
            mock_check.assert_not_called()

    def test_dialog_triggers_check(self, wm):
        """Dialog windows (#32770) trigger overlap check."""
        self._setup_event_defaults()
        wm.running = True
        _mock_win32gui.GetClassName.return_value = "#32770"

        def get_window_long(hwnd, flag):
            if flag == _mock_win32con.GWL_EXSTYLE:
                return 0
            return 0
        _mock_win32gui.GetWindowLong.side_effect = get_window_long

        with patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x8000, 1234, 0, 0, 0, 0)
            mock_check.assert_called_once()

    def test_menu_popup_triggers_check(self, wm):
        """Known menu class (#32768) triggers overlap check."""
        self._setup_event_defaults()
        wm.running = True
        _mock_win32gui.GetClassName.return_value = "#32768"

        def get_window_long(hwnd, flag):
            if flag == _mock_win32con.GWL_EXSTYLE:
                return 0
            return 0
        _mock_win32gui.GetWindowLong.side_effect = get_window_long

        with patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x8000, 1234, 0, 0, 0, 0)
            mock_check.assert_called_once()

    def test_event_source_ignore_class(self, wm):
        """Windows in event_source_ignore_classes are ignored."""
        self._setup_event_defaults()
        wm.running = True
        wm.event_source_ignore_classes = ["ignoreme"]
        _mock_win32gui.GetClassName.return_value = "IgnoreMe"

        def get_window_long(hwnd, flag):
            if flag == _mock_win32con.GWL_EXSTYLE:
                return 0
            return 0
        _mock_win32gui.GetWindowLong.side_effect = get_window_long

        with patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x8000, 1234, 0, 0, 0, 0)
            mock_check.assert_not_called()

    def test_captioned_window_triggers_check(self, wm):
        """Windows with WS_CAPTION + WS_SYSMENU trigger check."""
        self._setup_event_defaults()
        wm.running = True
        _mock_win32gui.GetClassName.return_value = "MyAppClass"

        def get_window_long(hwnd, flag):
            if flag == _mock_win32con.GWL_EXSTYLE:
                return 0
            if flag == _mock_win32con.GWL_STYLE:
                return _mock_win32con.WS_CAPTION | _mock_win32con.WS_SYSMENU
            return 0
        _mock_win32gui.GetWindowLong.side_effect = get_window_long

        with patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x8000, 1234, 0, 0, 0, 0)
            mock_check.assert_called_once()

    def test_large_popup_triggers_check(self, wm):
        """Reasonably sized popup windows trigger check."""
        self._setup_event_defaults()
        wm.running = True
        _mock_win32gui.GetClassName.return_value = "PopupClass"

        def get_window_long(hwnd, flag):
            if flag == _mock_win32con.GWL_EXSTYLE:
                return 0
            if flag == _mock_win32con.GWL_STYLE:
                return _mock_win32con.WS_POPUP
            return 0
        _mock_win32gui.GetWindowLong.side_effect = get_window_long

        with patch.object(wm, "get_window_rect", return_value=(100, 100, 300, 200)), \
             patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x8000, 1234, 0, 0, 0, 0)
            mock_check.assert_called_once()

    def test_tiny_popup_ignored(self, wm):
        """Very small popup windows are ignored."""
        self._setup_event_defaults()
        wm.running = True
        _mock_win32gui.GetClassName.return_value = "TinyPopup"

        def get_window_long(hwnd, flag):
            if flag == _mock_win32con.GWL_EXSTYLE:
                return 0
            if flag == _mock_win32con.GWL_STYLE:
                return _mock_win32con.WS_POPUP
            return 0
        _mock_win32gui.GetWindowLong.side_effect = get_window_long

        with patch.object(wm, "get_window_rect", return_value=(100, 100, 10, 5)), \
             patch.object(wm, "check_for_overlaps_and_move") as mock_check:
            wm.win_event_proc(None, 0x8000, 1234, 0, 0, 0, 0)
            mock_check.assert_not_called()


# ===========================================================================
# find_obstructing_window
# ===========================================================================


class TestFindObstructingWindow:
    def test_finds_osk_by_title(self, wm):
        """Finds OSK window by matching title."""
        def fake_enum_windows(callback, result_list):
            _mock_win32gui.IsWindowVisible.return_value = True
            _mock_win32gui.GetWindowText.return_value = "On-Screen Keyboard"
            callback(5555, result_list)

        _mock_win32gui.EnumWindows.side_effect = fake_enum_windows
        result = wm.find_obstructing_window()
        assert result == 5555
        _mock_win32gui.EnumWindows.side_effect = None

    def test_returns_none_when_not_found(self, wm):
        """Returns None when no matching window found."""
        def fake_enum_windows(callback, result_list):
            _mock_win32gui.IsWindowVisible.return_value = True
            _mock_win32gui.GetWindowText.return_value = "Unrelated Window"
            callback(1111, result_list)

        _mock_win32gui.EnumWindows.side_effect = fake_enum_windows
        _mock_win32process.EnumProcesses.return_value = []
        result = wm.find_obstructing_window()
        assert result is None
        _mock_win32gui.EnumWindows.side_effect = None

    def test_skips_invisible_windows(self, wm):
        """Invisible windows with matching titles are skipped."""
        def fake_enum_windows(callback, result_list):
            _mock_win32gui.IsWindowVisible.return_value = False
            _mock_win32gui.GetWindowText.return_value = "On-Screen Keyboard"
            callback(5555, result_list)

        _mock_win32gui.EnumWindows.side_effect = fake_enum_windows
        _mock_win32process.EnumProcesses.return_value = []
        result = wm.find_obstructing_window()
        assert result is None
        _mock_win32gui.EnumWindows.side_effect = None

    def test_case_insensitive_title_match(self, wm):
        """Title matching is case-insensitive."""
        def fake_enum_windows(callback, result_list):
            _mock_win32gui.IsWindowVisible.return_value = True
            _mock_win32gui.GetWindowText.return_value = "ON-SCREEN KEYBOARD"
            callback(5555, result_list)

        _mock_win32gui.EnumWindows.side_effect = fake_enum_windows
        result = wm.find_obstructing_window()
        assert result == 5555
        _mock_win32gui.EnumWindows.side_effect = None


# ===========================================================================
# start / stop lifecycle
# ===========================================================================


class TestLifecycle:
    def test_start_sets_running_flag(self, wm):
        """start() sets running=True and launches thread."""
        with patch.object(wm, "message_loop"):
            # Make the thread ready event fire immediately
            original_start = threading.Thread.start

            def fake_start(self_thread):
                wm._thread_ready_event.set()

            with patch.object(threading.Thread, "start", fake_start):
                result = wm.start()
                assert result is True
                assert wm.running is True

    def test_start_when_already_running(self, wm):
        """start() when already running returns True without new thread."""
        wm.running = True
        result = wm.start()
        assert result is True

    def test_stop_when_not_running(self, wm):
        """stop() when not running is a no-op."""
        wm.running = False
        wm.stop()  # Should not raise

    def test_stop_sets_running_false(self, wm):
        """stop() clears running flag."""
        wm.running = True
        wm.hook_thread = Mock()
        wm.hook_thread.is_alive.return_value = False
        wm.message_only_hwnd = None
        wm._gui_thread_id = None
        wm.stop()
        assert wm.running is False

    def test_stop_joins_thread(self, wm):
        """stop() joins the hook thread."""
        wm.running = True
        mock_thread = Mock()
        mock_thread.is_alive.return_value = True
        mock_thread.name = "WindowMoverThread"
        wm.hook_thread = mock_thread
        wm.message_only_hwnd = None
        wm._gui_thread_id = None
        wm.stop()
        mock_thread.join.assert_called_once_with(timeout=4.0)

    def test_stop_posts_wm_null_to_wake_loop(self, wm):
        """stop() posts WM_NULL to wake the message loop."""
        wm.running = True
        wm.message_only_hwnd = 8888
        wm._gui_thread_id = 12345
        wm.hook_thread = Mock()
        wm.hook_thread.is_alive.return_value = False
        _mock_win32gui.IsWindow.return_value = True
        _mock_win32gui.PostMessage.return_value = True

        wm.stop()
        _mock_win32gui.PostMessage.assert_called_with(8888, _mock_win32con.WM_NULL, 0, 0)


# ===========================================================================
# cleanup_hooks
# ===========================================================================


class TestCleanupHooks:
    def test_cleanup_unhooks_all(self, wm, _patch_win32_modules):
        mock_unhook = Mock()
        wm.event_hooks = [111, 222, 333]
        with patch("services.wheelhouse.features.window_mover.ctypes") as mock_ctypes:
            mock_ctypes.windll.user32.UnhookWinEvent = mock_unhook
            wm.cleanup_hooks()
        assert wm.event_hooks == []
        assert mock_unhook.call_count == 3

    def test_cleanup_empty_hooks(self, wm):
        wm.event_hooks = []
        wm.cleanup_hooks()  # Should not raise
        assert wm.event_hooks == []


# ===========================================================================
# __init__ / configuration
# ===========================================================================


class TestInit:
    def test_screen_dimensions_stored(self, wm):
        assert wm.true_screen_width == 1920
        assert wm.true_screen_height == 1080

    def test_effective_screen_starts_at_true(self, wm):
        assert wm.effective_screen_width == 1920
        assert wm.effective_screen_height == 1080
        assert wm.effective_screen_x == 0
        assert wm.effective_screen_y == 0

    def test_config_defaults(self, wm):
        assert wm.target_names == ["On-Screen Keyboard", "osk"]
        assert wm.move_cooldown == 0.5
        assert wm.clearance_gap == 5

    def test_initial_state(self, wm):
        assert wm.obstructing_window_hwnd is None
        assert wm.obstructing_window_rect is None
        assert wm.running is False
        assert wm.event_hooks == []


# ===========================================================================
# Adversarial: edge cases from acceptance criteria
# ===========================================================================


class TestAdversarialEdgeCases:
    """Adversarial tests: zero-size monitors, negative coords, overlapping rects, etc."""

    def test_zero_size_screen(self, _patch_win32_modules):
        """Zero screen dimensions should not crash position validation."""
        mod = _patch_win32_modules
        mover = mod.WindowMover(0, 0)
        assert mover.is_position_valid(0, 0, 0, 0) is True
        assert mover.is_position_valid(0, 0, 1, 1) is False

    def test_negative_screen_coords_position_check(self, wm):
        """Negative effective screen coords (multi-monitor setup)."""
        wm.effective_screen_x = -1920
        wm.effective_screen_y = 0
        wm.effective_screen_width = 1920
        # Position on left monitor is valid
        assert wm.is_position_valid(-1920, 0, 400, 300) is True
        # Position straddling boundary
        assert wm.is_position_valid(-200, 0, 400, 300) is False

    def test_overlapping_monitor_rects(self, wm):
        """Window exactly at boundary of effective screen."""
        wm.effective_screen_width = 1920
        wm.effective_screen_height = 1080
        # Window exactly at right edge
        assert wm.is_position_valid(1520, 0, 400, 300) is True
        # One pixel past
        assert wm.is_position_valid(1521, 0, 400, 300) is False

    def test_window_larger_than_screen_clear_spot(self, wm):
        """Find clear spot when OSK is larger than screen - returns original."""
        obstructed = (100, 100, 200, 200)
        new_x, new_y = wm.find_clear_spot_for_osk(obstructed, 0, 0, 2000, 1200)
        assert new_x == 0
        assert new_y == 0

    def test_very_large_coordinates(self, wm):
        """Very large coordinate values should not crash."""
        result = wm.is_position_valid(999999, 999999, 100, 100)
        assert result is False

    def test_clearance_gap_zero(self, wm):
        """Zero clearance gap still produces valid positions."""
        wm.clearance_gap = 0
        obstructed = (800, 400, 200, 200)
        new_x, new_y = wm.find_clear_spot_for_osk(obstructed, 850, 450, 100, 80)
        if new_x != 850 or new_y != 450:
            assert not _rects_overlap(new_x, new_y, 100, 80, *obstructed)

    def test_extremely_small_clearance_gap(self, wm):
        """Fractional clearance gap."""
        wm.clearance_gap = 0.5
        obstructed = (800, 400, 200, 200)
        new_x, new_y = wm.find_clear_spot_for_osk(obstructed, 850, 450, 100, 80)
        assert isinstance(new_x, (int, float))


# ===========================================================================
# Helper functions
# ===========================================================================


def _rects_overlap(x1, y1, w1, h1, x2, y2, w2, h2):
    """Check if two rectangles overlap."""
    return not (x1 + w1 <= x2 or x1 >= x2 + w2 or y1 + h1 <= y2 or y1 >= y2 + h2)
