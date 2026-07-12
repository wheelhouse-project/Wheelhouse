"""Window focus management and activation.

This module handles window activation, focus restoration, and HWND tracking
for ensuring UI operations target the correct window.
"""
import logging
import win32gui
import win32con
from typing import Optional

logger = logging.getLogger(__name__)


class WindowFocusManager:
    """Manages window focus and activation state.

    Tracks target window handles and ensures proper focus for UI operations.
    Handles minimized windows, maximized state preservation, and focus retries.
    """

    def __init__(self):
        """Initialize the window focus manager."""
        self._last_target_hwnd: Optional[int] = None

    def ensure_focused(self, hwnd: int) -> bool:
        """Ensure the specified window is focused and active.

        Handles:
        - Restoring minimized windows (preserves maximized state)
        - Setting foreground focus with retry logic
        - Graceful error handling

        Args:
            hwnd: Window handle to focus

        Returns:
            bool: True if window was successfully focused, False otherwise
        """
        if not hwnd:
            return False

        try:
            # Restore only if minimized; keep maximized windows maximized
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

            win32gui.SetForegroundWindow(hwnd)

            # Verify without delay; retry once if not active
            if win32gui.GetForegroundWindow() != hwnd:
                win32gui.SetForegroundWindow(hwnd)

            # wh-override-paste-focus-drift.1.1: report whether foreground
            # actually changed. SetForegroundWindow does not raise on the
            # common Windows restriction where the target thread has not
            # received recent user input; it silently leaves foreground
            # alone. Returning True unconditionally would let the retry
            # handler proceed with capture_context() against the wrong
            # window, which is exactly the production failure this fix
            # is meant to close.
            return win32gui.GetForegroundWindow() == hwnd
        except Exception as e:
            logger.debug(f"SetForegroundWindow failed: {e}")
            return False

    def remember_target(self, control) -> None:
        """Store the top-level HWND of a control for later foreground restoration.

        Args:
            control: UIA control object to extract HWND from
        """
        try:
            if not control:
                return
            top = control.GetTopLevelControl()
            hwnd = top.NativeWindowHandle if top else 0
            if hwnd:
                self._last_target_hwnd = hwnd
        except Exception:
            pass

    def get_target_window(self, focused_control):
        """Identify the target window handle and control for focus operations.

        Handles fallback logic:
        1. Use provided focused_control if keyboard focusable
        2. Otherwise query current focused control
        3. Extract window handle from control's top-level window
        4. Fallback to last remembered target window if no handle found

        Args:
            focused_control: Control object that should have focus, or None

        Returns:
            tuple: (hwnd, target_control) where:
                - hwnd: Window handle (int) or None
                - target_control: UIA Control object or None
        """
        import uiautomation as auto

        target_control = focused_control
        if not target_control or not getattr(target_control, 'IsKeyboardFocusable', False):
            try:
                target_control = auto.GetFocusedControl()
            except Exception:
                target_control = None

        hwnd = None
        try:
            if target_control:
                top = target_control.GetTopLevelControl()
                hwnd = top.NativeWindowHandle if top else None
        except Exception:
            hwnd = None

        if not hwnd:
            hwnd = self._last_target_hwnd

        return hwnd, target_control
