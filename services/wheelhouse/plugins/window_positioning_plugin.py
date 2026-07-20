"""Window positioning plugin for automatic on-screen keyboard repositioning.

Monitors Windows accessibility events and automatically repositions the on-screen
keyboard (or other target windows) when they overlap with active application windows.
Uses a background thread for Windows event hooks with EventBus communication.

Configuration:
    [plugins.window_positioning]
    enabled = true
    target_window_names = ["On-Screen Keyboard", "osk"]
    move_cooldown_seconds = 0.5
    clearance_gap_pixels = 5
    ignore_window_titles = ["Program Manager", "Task Switching"]
    ignore_window_classes = ["Shell_TrayWnd", "Progman"]

Events Published:
    - WindowFocusChangedEvent: Internal event when significant window changes detected
    - WindowRepositionCommand: Internal event to trigger window repositioning

Integration:
    Operates independently - no external event subscriptions needed.
    Uses Windows accessibility hooks in background thread.
"""
import asyncio
import ctypes
import logging
import math
import os
import threading
import time
from ctypes import wintypes
from typing import TYPE_CHECKING, Optional, List, Tuple

import win32api
import win32con
import win32gui
import win32process

from services.wheelhouse.plugins.base import BasePlugin, PluginState
from services.wheelhouse.events import WindowFocusChangedEvent, WindowRepositionCommand
from services.wheelhouse.ui.elevation_check import (
    ELEVATED,
    elevation_state_of_hwnd,
)

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.event_bus import EventBus

logger = logging.getLogger(__name__)

# Windows Event Constants
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_SYSTEM_MENUPOPUPSTART = 0x0006

# Windows Event Proc Type
WinEventProcType = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD
)


class WindowPositioningPlugin(BasePlugin):
    """Monitors window events and repositions on-screen keyboard automatically.
    
    Uses Windows accessibility hooks to detect window focus changes and repositions
    the on-screen keyboard when it overlaps with active windows. Operates in a
    background thread for Windows API compatibility, communicating via EventBus.
    """

    def __init__(self):
        super().__init__()
        self._config = None
        self._event_bus = None
        self._hook_thread = None
        self._running = False
        self._thread_ready = threading.Event()

        # Configuration
        self.target_window_names: List[str] = ["On-Screen Keyboard", "osk"]
        self.move_cooldown_seconds = 0.5
        self.clearance_gap_pixels = 5
        self.ignore_window_titles: List[str] = []
        self.ignore_window_classes: List[str] = []

        # State tracking
        self._target_window_hwnd: Optional[int] = None
        self._target_window_rect: Optional[Tuple[int, int, int, int]] = None
        self._last_move_time = 0.0
        self._last_move_position: Optional[Tuple[int, int]] = None  # Track last target position
        self._screen_width = 0
        self._screen_height = 0
        
        # Hook management
        self._event_hooks = []
        self._win_event_proc_cb = None
        self._message_window = None
        self._window_class_name = f"WindowPositioningMsgWnd_{os.getpid()}"

    @property
    def name(self) -> str:
        """Return plugin identifier for registration.
        
        Returns:
            str: Plugin name 'window_positioning'
        """
        return "window_positioning"

    async def initialize(self, config: "ConfigService", event_bus: "EventBus") -> None:
        """Initialize with configuration.
        
        Args:
            config: ConfigService instance for plugin settings
            event_bus: EventBus for publishing reposition events
        """
        self._config = config
        self._event_bus = event_bus

        # Load configuration
        self.target_window_names = config.get(
            "plugins.window_positioning.target_window_names",
            ["On-Screen Keyboard", "osk"]
        )
        self.move_cooldown_seconds = config.get(
            "plugins.window_positioning.move_cooldown_seconds",
            0.5
        )
        self.clearance_gap_pixels = config.get(
            "plugins.window_positioning.clearance_gap_pixels",
            5
        )
        self.ignore_window_titles = config.get(
            "plugins.window_positioning.ignore_window_titles",
            ["Program Manager", "Task Switching"]
        )
        self.ignore_window_classes = config.get(
            "plugins.window_positioning.ignore_window_classes",
            ["Shell_TrayWnd", "Progman"]
        )

        # Get screen dimensions
        self._screen_width = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        self._screen_height = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)

        # Validate configuration
        if self.move_cooldown_seconds < 0:
            raise ValueError("move_cooldown_seconds must be non-negative")
        if self.clearance_gap_pixels < 0:
            raise ValueError("clearance_gap_pixels must be non-negative")

        self._state = PluginState.INITIALIZED

    async def start(self) -> None:
        """:flow: Window Repositioning
        :step: 1
        :description: Plugin startup - validates Windows API and launches background hook thread
        :data_in: Configuration values (target_window_names, cooldown, clearance_gap)
        :data_out: Running Windows event hook thread monitoring system events
        :notes: Entry point for window repositioning flow. Tests Win32 API availability before starting. Launches background thread (step 2) for Windows accessibility hooks - required because Windows hooks must be on a thread with a message loop. Thread communicates back via EventBus for async integration. If API unavailable, plugin enters FAILED state and flow terminates.
        """
        try:
            self._state = PluginState.STARTING
            logger.debug("[Startup] WindowPositioning plugin starting...")

            # Start hook thread
            self._running = True
            self._thread_ready.clear()
            self._hook_thread = threading.Thread(target=self._hook_thread_main, daemon=True)
            self._hook_thread.name = "WindowPositioningHookThread"
            self._hook_thread.start()

            # Wait for thread to be ready
            if not self._thread_ready.wait(timeout=3.0):
                logger.error("Hook thread failed to start within timeout")
                self._running = False
                self._state = PluginState.FAILED
                return

            self._state = PluginState.RUNNING

        except Exception as e:
            logger.error(f"WindowPositioning failed to start: {e}", exc_info=True)
            self._running = False
            self._state = PluginState.FAILED

    async def stop(self) -> None:
        """Stop window positioning.
        
        Signals hook thread to terminate and waits up to 2s for shutdown.
        """
        self._state = PluginState.STOPPING
        self._running = False

        if self._hook_thread and self._hook_thread.is_alive():
            # Post message to wake up message loop if window exists
            if self._message_window:
                try:
                    win32gui.PostMessage(self._message_window, win32con.WM_QUIT, 0, 0)
                except:
                    pass

            self._hook_thread.join(timeout=2.0)
            if self._hook_thread.is_alive():
                logger.warning("Hook thread did not stop gracefully")

        self._state = PluginState.STOPPED

    def get_health_status(self) -> dict:
        """Return current health status.
        
        Returns:
            dict: Health status with state, target window detection, and config
        """
        status = "healthy"
        if self._state != PluginState.RUNNING:
            status = "unhealthy"

        return {
            "status": status,
            "state": self._state.value,
            "target_window_found": self._target_window_hwnd is not None,
            "move_cooldown_seconds": self.move_cooldown_seconds,
            "screen_dimensions": (self._screen_width, self._screen_height)
        }

    # ========================================================================
    # HOOK THREAD MAIN (runs in background thread)
    # ========================================================================

    def _hook_thread_main(self):
        """:flow: Window Repositioning
        :step: 2
        :description: Background thread monitors Windows accessibility events via hooks
        :data_in: Windows system events (foreground change, window create, window show, menu popup)
        :data_out: Filtered window events processed for overlap detection (step 3)
        :notes: Core monitoring logic. Sets up Windows accessibility hooks for key events: foreground change, window create/show, menu popups. CRITICAL: Filters out cursor movement events completely (EVENT_OBJECT_LOCATIONCHANGE + OBJID_CURSOR) to eliminate log spam. Only processes significant window events that might require keyboard repositioning. Runs Windows message loop required for hooks. On events, triggers overlap detection (step 3) which may lead to repositioning (step 4).
        """
        try:
            # Create message window and hooks
            if not self._create_hooks():
                logger.error("Failed to create Windows event hooks")
                self._thread_ready.set()
                return

            self._thread_ready.set()

            # Find target window initially
            self._target_window_hwnd = self._find_target_window()
            if self._target_window_hwnd:
                self._target_window_rect = self._get_window_rect(self._target_window_hwnd)

            # Run message loop
            msg = wintypes.MSG()
            while self._running:
                # Check for messages (non-blocking peek)
                if ctypes.windll.user32.PeekMessageW(
                    ctypes.byref(msg), None, 0, 0, win32con.PM_REMOVE
                ):
                    if msg.message == win32con.WM_QUIT:
                        break
                    ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                    ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.05)  # Brief sleep to avoid busy loop

        except Exception as e:
            logger.error(f"Hook thread error: {e}", exc_info=True)
        finally:
            self._cleanup_hooks()

    def _create_hooks(self) -> bool:
        """Create Windows accessibility event hooks.
        
        Hooks FOREGROUND, MENUPOPUPSTART, SHOW, HIDE, DESTROY events.
        
        Returns:
            bool: True if at least one hook created successfully
        """
        try:
            # Create callback
            self._win_event_proc_cb = WinEventProcType(self._win_event_proc)

            # Register hooks for specific events only
            # FOREGROUND = User clicked/focused a window (native apps)
            # MENUPOPUPSTART = Native Windows menus (works for Brave)
            # SHOW = Custom UI dropdowns (VS Code Electron menus don't fire MENUPOPUPSTART)
            # HIDE/DESTROY = Detect when keyboard is closed/hidden
            # Note: SHOW fires for internal UI too, but overlap check filters false positives
            events_to_hook = [
                EVENT_SYSTEM_FOREGROUND,
                EVENT_SYSTEM_MENUPOPUPSTART,
                EVENT_OBJECT_SHOW,
                EVENT_OBJECT_HIDE,
                EVENT_OBJECT_DESTROY,
            ]

            for event_type in events_to_hook:
                hook = ctypes.windll.user32.SetWinEventHook(
                    event_type, event_type, 0, self._win_event_proc_cb,
                    0, 0, win32con.WINEVENT_OUTOFCONTEXT | win32con.WINEVENT_SKIPOWNPROCESS
                )
                if hook:
                    self._event_hooks.append(hook)
                else:
                    logger.error(f"Failed to set hook for event 0x{event_type:04X}")

            if not self._event_hooks:
                return False

            return True

        except Exception as e:
            logger.error(f"Error creating hooks: {e}", exc_info=True)
            return False

    def _cleanup_hooks(self):
        """Cleanup Windows event hooks.
        
        Unhooks all registered event hooks and clears hook list.
        """
        for hook in self._event_hooks:
            try:
                ctypes.windll.user32.UnhookWinEvent(hook)
            except Exception as e:
                logger.error(f"Error unhooking: {e}")
        self._event_hooks = []

    def _win_event_proc(self, hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
        """Windows event hook callback.
        
        CRITICAL: Filters out cursor movement to prevent log spam.
        Only processes significant window events.
        """
        if not self._running:
            return

        # CRITICAL: Filter cursor movements completely (major source of log spam)
        if event == EVENT_OBJECT_LOCATIONCHANGE and idObject == win32con.OBJID_CURSOR:
            return  # Silent filter - no logging
        
        # CRITICAL: Filter HIDE/DESTROY for non-target windows (massive spam source)
        if event == EVENT_OBJECT_HIDE or event == EVENT_OBJECT_DESTROY:
            # Only care about these events if it's the target keyboard
            if hwnd == self._target_window_hwnd:
                self._target_window_hwnd = None
                self._target_window_rect = None
            return  # Silent filter for all HIDE/DESTROY events

        # Ignore invalid handles
        if hwnd == 0 or hwnd is None:
            return  # Silent filter

        # Ignore our own message window
        if hwnd == self._message_window:
            return  # Silent filter
        
        # Ignore target window itself (we already handle HIDE/DESTROY above)
        if hwnd == self._target_window_hwnd:
            return  # Silent filter

        # Process significant window event
        try:
            # Respond to focus changes, native menus, and custom UI (Electron menus)
            should_check = (
                event == EVENT_SYSTEM_FOREGROUND or
                event == EVENT_SYSTEM_MENUPOPUPSTART or
                event == EVENT_OBJECT_SHOW
            )

            if should_check:
                # Get window info early for filtering
                try:
                    title = win32gui.GetWindowText(hwnd)
                    class_name = win32gui.GetClassName(hwnd)
                except:
                    title = ""
                    class_name = ""
                
                # Filter out internal UI elements that fire SHOW or FOREGROUND events
                internal_ui_classes = [
                    'GhostDivider',           # VS Code dividers
                    'ANIMATION_TIMER_HWND',   # Animation timers
                    'DirectUIHWND',           # Windows UI framework
                    'EdgeUiInputWndClass',    # Edge/Chrome input popups (fires FOREGROUND!)
                    'TaskListThumbnailWnd',   # Taskbar thumbnails
                    'Chrome_RenderWidgetHostHWND',  # Chrome rendering
                    'tooltips_class32',       # Tooltips
                    'Button',                 # Individual buttons
                ]
                
                if class_name in internal_ui_classes:
                    return  # Silent filter - internal UI element
                
                # Filter windows in ignore lists early (before any logging)
                if self._is_window_ignorable(hwnd, title, class_name):
                    return  # Silent filter - explicitly ignored window
                
                # Additional filter for SHOW events - must be visible
                if event == EVENT_OBJECT_SHOW:
                    try:
                        if not win32gui.IsWindowVisible(hwnd):
                            return  # Silent filter
                    except:
                        pass  # Continue if can't check visibility
                
                # Trigger overlap check asynchronously for this specific window
                asyncio.run(self._check_and_reposition(focus_hwnd=hwnd))

        except Exception as e:
            logger.error(f"Error in event callback: {e}", exc_info=True)

    # ========================================================================
    # WINDOW DETECTION AND POSITIONING LOGIC
    # ========================================================================

    def _find_target_window(self) -> Optional[int]:
        """Find target window by title or process name.
        
        Returns:
            Optional[int]: HWND of first matching window, or None
        """
        result = []

        def callback(hwnd, param):
            """EnumWindows callback to find matching window.
            
            Args:
                hwnd: Window handle being enumerated
                param: Result list to append matches
            """
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                title = win32gui.GetWindowText(hwnd)
                for name in self.target_window_names:
                    if name.lower() in title.lower():
                        param.append(hwnd)
                        return
            except:
                pass

        win32gui.EnumWindows(callback, result)
        return result[0] if result else None

    def _get_window_rect(self, hwnd: int) -> Optional[Tuple[int, int, int, int]]:
        """Get window rectangle as (x, y, width, height).
        
        Args:
            hwnd: Window handle
            
        Returns:
            Optional[Tuple]: (x, y, width, height) or None on error
        """
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            return (left, top, right - left, bottom - top)
        except Exception:
            return None

    def _is_window_ignorable(self, hwnd: int, title: str, class_name: str) -> bool:
        """Check if focused window should be ignored for overlap detection.
        
        Since we only check focused windows (user just clicked), only ignore:
        - Windows explicitly in ignore lists (desktop, taskbar, etc.)
        
        Everything else matters - if user clicked it, it's relevant.
        """
        title_lower = title.lower() if title else ""
        class_lower = class_name.lower() if class_name else ""

        if title_lower in [t.lower() for t in self.ignore_window_titles]:
            return True
        if class_lower in [c.lower() for c in self.ignore_window_classes]:
            return True

        return False

    async def _check_and_reposition(self, focus_hwnd: int):
        """:flow: Window Repositioning
        :step: 3
        :description: Detect overlaps and calculate optimal clear position
        :data_in: Focused window HWND from accessibility event
        :data_out: Target position (x, y) if overlap detected, otherwise no action
        :notes: Core overlap detection logic. Gets current target window position and checks if focused window overlaps it. Skips empty title and large window checks for focused windows (menus/dropdowns often have no title or large parents). If overlap found with non-ignorable window, calculates best clear position using find_clear_position() - tries 4 positions (right, left, below, above) and picks closest valid position within screen bounds. If clear position found and different from current position, triggers step 4 (window move). Simple focus-driven approach - only checks the window user just interacted with.
        """
        # CRITICAL: If keyboard not on screen, do nothing (no logging, no processing)
        if not self._running:
            return
            
        # Check if target window (keyboard) exists and is visible
        if not self._target_window_hwnd:
            self._target_window_hwnd = self._find_target_window()
            if not self._target_window_hwnd:
                return  # Keyboard not found - silent exit
            self._target_window_rect = self._get_window_rect(self._target_window_hwnd)
        
        # Verify keyboard still visible - if not, clear state and exit silently
        if not win32gui.IsWindow(self._target_window_hwnd) or \
           not win32gui.IsWindowVisible(self._target_window_hwnd):
            self._target_window_hwnd = None
            self._target_window_rect = None
            return  # Keyboard not visible - silent exit

        # Get current target window position
        current_rect = self._get_window_rect(self._target_window_hwnd)
        if not current_rect:
            return

        self._target_window_rect = current_rect
        tx, ty, tw, th = current_rect

        # Get focused window info
        try:
            title = win32gui.GetWindowText(focus_hwnd)
            class_name = win32gui.GetClassName(focus_hwnd)
            rect = self._get_window_rect(focus_hwnd)
            
            if not rect:
                return
        except Exception:
            return

        # Check for overlap
        wx, wy, ww, wh = rect
        overlap = not (tx + tw <= wx or tx >= wx + ww or
                      ty + th <= wy or ty >= wy + wh)

        if not overlap:
            return
        
        # Ignore maximized/full-screen windows (main app windows)
        # Only ignore if window is BOTH very wide AND very tall (90%+ each dimension)
        # This allows tall dropdowns (like full-height menus) to trigger repositioning
        fullscreen_threshold = 0.9  # 90% of screen in BOTH dimensions
        is_fullscreen = (ww > self._screen_width * fullscreen_threshold and 
                        wh > self._screen_height * fullscreen_threshold)
        
        if is_fullscreen:
            return

        # Calculate clear position
        new_x, new_y = self._find_clear_position(rect, tx, ty, tw, th)

        if new_x != tx or new_y != ty:
            await self._reposition_window(new_x, new_y, f"overlap with {title}")

    def _find_clear_position(
        self, obstructed_rect: Tuple[int, int, int, int],
        tx: int, ty: int, tw: int, th: int
    ) -> Tuple[int, int]:
        """Find best clear position for target window.
        
        Tries 4 positions: right, left, below, above the obstructed window.
        Returns position closest to current that fits on screen.
        """
        ox, oy, ow, oh = obstructed_rect
        gap = self.clearance_gap_pixels

        # Calculate candidate positions
        candidates = [
            (ox + ow + gap, oy + (oh - th) // 2),  # Right
            (ox - tw - gap, oy + (oh - th) // 2),  # Left
            (ox + (ow - tw) // 2, oy + oh + gap),  # Below
            (ox + (ow - tw) // 2, oy - th - gap),  # Above
        ]

        # Find valid positions
        valid_positions = []
        for x, y in candidates:
            # Check screen bounds
            if (0 <= x <= self._screen_width - tw and
                0 <= y <= self._screen_height - th):
                # Check it actually clears the obstructed window
                clears = (x >= ox + ow or x + tw <= ox or
                         y >= oy + oh or y + th <= oy)
                if clears:
                    distance = math.sqrt((x - tx)**2 + (y - ty)**2)
                    valid_positions.append((distance, x, y))

        if not valid_positions:
            return tx, ty  # No valid position found

        # Return closest valid position
        valid_positions.sort(key=lambda p: p[0])
        return valid_positions[0][1], valid_positions[0][2]

    async def _reposition_window(self, target_x: int, target_y: int, reason: str):
        """:flow: Window Repositioning
        :step: 4
        :description: Execute window move via Windows SetWindowPos API
        :data_in: Target coordinates (x, y) and window handle (HWND)
        :data_out: SetWindowPos API call that physically moves window on screen
        :notes: Final step - actually moves the window. Checks cooldown to prevent excessive repositioning (default 0.5s). Ensures window is visible and not minimized before moving. Calls SetWindowPos with NOACTIVATE and NOZORDER flags to avoid stealing focus or changing Z-order. Verifies move succeeded by checking new window position matches target (within 10 pixels tolerance). Updates last_move_time to enforce cooldown. Logs INFO-level message only when move completes successfully - provides visibility without spam. This is the ONLY place that generates logs visible to user during normal operation.
        """
        # Check cooldown - only applies if trying to move to same position
        # Different target position = different dropdown, allow immediate move
        current_time = time.time()
        target_pos = (target_x, target_y)
        
        if (current_time - self._last_move_time < self.move_cooldown_seconds and 
            self._last_move_position == target_pos):
            return

        try:
            # Ensure window is visible and not minimized
            if win32gui.IsIconic(self._target_window_hwnd):
                win32gui.ShowWindow(self._target_window_hwnd, win32con.SW_RESTORE)
                time.sleep(0.1)

            _, _, width, height = self._target_window_rect

            # Move window (NOACTIVATE to avoid focus steal, NOZORDER to keep stacking)
            flags = win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER
            result = win32gui.SetWindowPos(
                self._target_window_hwnd, 0,
                int(target_x), int(target_y), int(width), int(height),
                flags
            )

            # Verify move succeeded
            new_rect = self._get_window_rect(self._target_window_hwnd)
            if new_rect and abs(new_rect[0] - target_x) < 10 and abs(new_rect[1] - target_y) < 10:
                self._target_window_rect = new_rect
                self._last_move_time = current_time
                self._last_move_position = target_pos
            elif self._target_runs_elevated():
                # wh-winpos-silent-failure: SetWindowPos against a
                # higher-integrity window can fail without raising --
                # the window just does not move. Expected boundary,
                # not a defect; stay out of the WARNING stream.
                logger.debug(
                    "Keyboard window did not move: it runs as "
                    "administrator and Wheelhouse does not. Run "
                    "Wheelhouse as administrator to control it."
                )
            else:
                logger.warning(
                    f"Window move verification failed: target=({target_x}, {target_y}), "
                    f"actual={new_rect[:2] if new_rect else 'None'}"
                )

        except Exception as e:
            if self._target_runs_elevated():
                # Same boundary as above, surfaced as an exception on
                # some Windows builds. Quiet by design (David's
                # 2026-07-19 direction): the refusal is expected
                # whenever the on-screen keyboard runs elevated.
                logger.debug(
                    "Keyboard window move refused: it runs as "
                    "administrator and Wheelhouse does not. Run "
                    "Wheelhouse as administrator to control it. (%s)", e,
                )
            else:
                logger.error(f"Failed to reposition window: {e}", exc_info=True)

    def _target_runs_elevated(self) -> bool:
        """True when the target window's process outranks ours.

        Fail open: an UNKNOWN elevation state returns False so the
        existing ERROR/WARNING diagnostics are never suppressed on an
        unproven claim.
        """
        return (
            elevation_state_of_hwnd(self._target_window_hwnd) == ELEVATED
        )
