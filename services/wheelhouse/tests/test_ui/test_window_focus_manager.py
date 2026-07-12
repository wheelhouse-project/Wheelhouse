"""Tests for WindowFocusManager.

WindowFocusManager is responsible for ensuring UI operations target the correct
window. It tracks window handles, restores minimized windows, and provides
fallback logic when the focused control is unavailable.

Critical for accessibility: if focus goes to the wrong window, voice-dictated
text ends up in the wrong application.
"""
import sys
import pytest
from unittest.mock import MagicMock, patch

from ui.window_focus_manager import WindowFocusManager


class TestEnsureFocused:
    """Tests for ensure_focused() -- window activation and focus logic."""

    # ========================================================================
    # Normal window focus
    # ========================================================================

    @patch("ui.window_focus_manager.win32gui")
    def test_focuses_normal_window(self, mock_win32gui):
        """SetForegroundWindow called for a normal (non-minimized) window."""
        mock_win32gui.IsIconic.return_value = False
        mock_win32gui.GetForegroundWindow.return_value = 12345

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is True
        mock_win32gui.SetForegroundWindow.assert_called_once_with(12345)
        mock_win32gui.ShowWindow.assert_not_called()

    # ========================================================================
    # Minimized window restoration
    # ========================================================================

    @patch("ui.window_focus_manager.win32con")
    @patch("ui.window_focus_manager.win32gui")
    def test_restores_minimized_window(self, mock_win32gui, mock_win32con):
        """Minimized window should be restored before focusing."""
        mock_win32gui.IsIconic.return_value = True
        mock_win32gui.GetForegroundWindow.return_value = 99999
        mock_win32con.SW_RESTORE = 9

        mgr = WindowFocusManager()
        mgr.ensure_focused(99999)

        mock_win32gui.ShowWindow.assert_called_once_with(99999, 9)
        mock_win32gui.SetForegroundWindow.assert_called_with(99999)

    # ========================================================================
    # Retry logic
    # ========================================================================

    @patch("ui.window_focus_manager.win32gui")
    def test_retries_once_when_first_attempt_misses_then_succeeds(
        self, mock_win32gui,
    ):
        """If first GetForegroundWindow != hwnd but second == hwnd, return True."""
        mock_win32gui.IsIconic.return_value = False
        # First check: foreground is wrong; second check (post-retry): matches.
        mock_win32gui.GetForegroundWindow.side_effect = [77777, 12345]

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is True
        # SetForegroundWindow should be called twice: initial + retry.
        assert mock_win32gui.SetForegroundWindow.call_count == 2

    @patch("ui.window_focus_manager.win32gui")
    def test_returns_false_when_foreground_never_matches_target(
        self, mock_win32gui,
    ):
        """wh-override-paste-focus-drift.1.1: SetForegroundWindow can silently
        fail on Windows when the target thread has no recent user input or
        when another app holds the foreground lock. ensure_focused must
        report False in that case so the retry handler's proceed-anyway
        branch is driven by the real refocus outcome, not by a
        no-exception-was-raised proxy.
        """

        mock_win32gui.IsIconic.return_value = False
        # Both checks: foreground stays on the wrong window.
        mock_win32gui.GetForegroundWindow.return_value = 77777

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is False
        # SetForegroundWindow tried twice (initial + retry).
        assert mock_win32gui.SetForegroundWindow.call_count == 2

    @patch("ui.window_focus_manager.win32gui")
    def test_no_retry_when_focus_achieved_first_time(self, mock_win32gui):
        """If GetForegroundWindow == hwnd on first check, no retry needed."""
        mock_win32gui.IsIconic.return_value = False
        mock_win32gui.GetForegroundWindow.return_value = 12345

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is True
        mock_win32gui.SetForegroundWindow.assert_called_once_with(12345)

    # ========================================================================
    # Error handling
    # ========================================================================

    @patch("ui.window_focus_manager.win32gui")
    def test_returns_false_on_exception(self, mock_win32gui):
        """Should return False when SetForegroundWindow raises."""
        mock_win32gui.IsIconic.return_value = False
        mock_win32gui.SetForegroundWindow.side_effect = Exception("Access denied")

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is False

    @patch("ui.window_focus_manager.win32gui")
    def test_returns_false_on_is_iconic_exception(self, mock_win32gui):
        """Should return False when IsIconic raises."""
        mock_win32gui.IsIconic.side_effect = Exception("Invalid handle")

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is False

    # ========================================================================
    # Zero / None HWND
    # ========================================================================

    def test_returns_false_for_zero_hwnd(self):
        """HWND of 0 is invalid -- should return False immediately."""
        mgr = WindowFocusManager()
        result = mgr.ensure_focused(0)

        assert result is False

    def test_returns_false_for_none_hwnd(self):
        """None HWND should return False immediately."""
        mgr = WindowFocusManager()
        result = mgr.ensure_focused(None)

        assert result is False


class TestRememberTarget:
    """Tests for remember_target() -- storing top-level HWND from UIA control."""

    # ========================================================================
    # Stores HWND from UIA control
    # ========================================================================

    def test_stores_hwnd_from_control(self):
        """Should store the NativeWindowHandle of the top-level control."""
        mock_control = MagicMock()
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 54321
        mock_control.GetTopLevelControl.return_value = mock_top

        mgr = WindowFocusManager()
        mgr.remember_target(mock_control)

        assert mgr._last_target_hwnd == 54321

    # ========================================================================
    # None control
    # ========================================================================

    def test_handles_none_control(self):
        """None control should be a no-op (no crash, no state change)."""
        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 11111

        mgr.remember_target(None)

        # Should not change the existing stored HWND
        assert mgr._last_target_hwnd == 11111

    def test_handles_falsy_control(self):
        """Falsy control (0, empty) should be a no-op."""
        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 22222

        mgr.remember_target(0)

        assert mgr._last_target_hwnd == 22222

    # ========================================================================
    # Exception handling
    # ========================================================================

    def test_handles_exception_in_get_top_level(self):
        """Should silently handle exception from GetTopLevelControl."""
        mock_control = MagicMock()
        mock_control.GetTopLevelControl.side_effect = Exception("COM error")

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 33333

        mgr.remember_target(mock_control)

        # Should not change stored HWND on exception
        assert mgr._last_target_hwnd == 33333

    # ========================================================================
    # Edge cases
    # ========================================================================

    def test_does_not_store_zero_hwnd(self):
        """If NativeWindowHandle is 0, should not overwrite stored HWND."""
        mock_control = MagicMock()
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 0
        mock_control.GetTopLevelControl.return_value = mock_top

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 44444

        mgr.remember_target(mock_control)

        assert mgr._last_target_hwnd == 44444

    def test_stores_when_top_level_is_none(self):
        """If GetTopLevelControl returns None, hwnd=0 so no store."""
        mock_control = MagicMock()
        mock_control.GetTopLevelControl.return_value = None

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 55555

        mgr.remember_target(mock_control)

        assert mgr._last_target_hwnd == 55555


class TestGetTargetWindow:
    """Tests for get_target_window() -- fallback logic for target window.

    Note: get_target_window() does `import uiautomation as auto` locally inside
    the function body. We patch sys.modules["uiautomation"] so the local import
    picks up the mock.
    """

    # ========================================================================
    # Uses provided control if keyboard focusable
    # ========================================================================

    def test_uses_provided_control_when_keyboard_focusable(self):
        """Should use provided control if it has IsKeyboardFocusable=True."""
        mock_auto = MagicMock()
        mock_control = MagicMock()
        mock_control.IsKeyboardFocusable = True
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 12345
        mock_control.GetTopLevelControl.return_value = mock_top

        mgr = WindowFocusManager()
        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(mock_control)

        assert hwnd == 12345
        assert ctrl is mock_control
        mock_auto.GetFocusedControl.assert_not_called()

    # ========================================================================
    # Falls back to GetFocusedControl
    # ========================================================================

    def test_falls_back_to_get_focused_control_when_not_focusable(self):
        """Should query auto.GetFocusedControl when provided control not focusable."""
        mock_auto = MagicMock()
        mock_provided = MagicMock()
        mock_provided.IsKeyboardFocusable = False

        mock_focused = MagicMock()
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 67890
        mock_focused.GetTopLevelControl.return_value = mock_top
        mock_auto.GetFocusedControl.return_value = mock_focused

        mgr = WindowFocusManager()
        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(mock_provided)

        assert hwnd == 67890
        assert ctrl is mock_focused

    def test_falls_back_to_get_focused_control_when_none_provided(self):
        """Should query auto.GetFocusedControl when no control provided."""
        mock_auto = MagicMock()
        mock_focused = MagicMock()
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 11111
        mock_focused.GetTopLevelControl.return_value = mock_top
        mock_auto.GetFocusedControl.return_value = mock_focused

        mgr = WindowFocusManager()
        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(None)

        assert hwnd == 11111
        assert ctrl is mock_focused

    # ========================================================================
    # Falls back to last remembered HWND
    # ========================================================================

    def test_falls_back_to_last_remembered_hwnd(self):
        """When no control yields an HWND, should use _last_target_hwnd."""
        mock_auto = MagicMock()
        mock_auto.GetFocusedControl.return_value = None

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 99999

        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(None)

        assert hwnd == 99999
        assert ctrl is None

    def test_returns_none_hwnd_when_no_fallback(self):
        """When nothing found and no remembered HWND, returns (None, None)."""
        mock_auto = MagicMock()
        mock_auto.GetFocusedControl.return_value = None

        mgr = WindowFocusManager()
        # _last_target_hwnd defaults to None
        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(None)

        assert hwnd is None
        assert ctrl is None

    # ========================================================================
    # GetFocusedControl exception
    # ========================================================================

    def test_handles_get_focused_control_exception(self):
        """Should fall back to remembered HWND when GetFocusedControl raises."""
        mock_auto = MagicMock()
        mock_auto.GetFocusedControl.side_effect = Exception("COM error")

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 88888

        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(None)

        assert hwnd == 88888
        assert ctrl is None

    # ========================================================================
    # GetTopLevelControl exception
    # ========================================================================

    def test_handles_get_top_level_control_exception(self):
        """Should fall back to remembered HWND when GetTopLevelControl raises."""
        mock_auto = MagicMock()
        mock_focused = MagicMock()
        mock_focused.IsKeyboardFocusable = True
        mock_focused.GetTopLevelControl.side_effect = Exception("COM error")

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 77777

        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(mock_focused)

        assert hwnd == 77777
        assert ctrl is mock_focused

    # ========================================================================
    # Top-level control returns None
    # ========================================================================

    def test_falls_back_when_top_level_is_none(self):
        """If GetTopLevelControl returns None, falls back to remembered HWND."""
        mock_auto = MagicMock()
        mock_control = MagicMock()
        mock_control.IsKeyboardFocusable = True
        mock_control.GetTopLevelControl.return_value = None

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 66666

        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(mock_control)

        assert hwnd == 66666
        assert ctrl is mock_control

    # ========================================================================
    # Control lacks IsKeyboardFocusable attribute
    # ========================================================================

    def test_falls_back_when_control_lacks_focusable_attr(self):
        """Control without IsKeyboardFocusable attr triggers fallback."""
        mock_auto = MagicMock()
        mock_provided = MagicMock(spec=[])  # no attributes
        mock_focused = MagicMock()
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 55555
        mock_focused.GetTopLevelControl.return_value = mock_top
        mock_auto.GetFocusedControl.return_value = mock_focused

        mgr = WindowFocusManager()
        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(mock_provided)

        assert hwnd == 55555
        assert ctrl is mock_focused
