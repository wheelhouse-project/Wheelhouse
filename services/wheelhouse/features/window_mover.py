"""Window movement and positioning automation system.

This module provides automated window management functionality using Windows API
hooks and accessibility events. It monitors window creation, focus changes, and
movement events to automatically position and resize windows according to
predefined rules and user preferences. The system integrates with the WheelHouse
state management to provide user-controllable window automation.

Key Classes:
  - WindowMover: Main class for window positioning automation.

Key Features:
  - Windows accessibility event monitoring
  - Automatic window positioning based on application rules
  - Integration with system tray for manual control
  - Process-aware positioning strategies
  - Window state tracking and restoration

Key Functions:
  - Window detection and classification
  - Automatic positioning rule execution
  - Event-driven window management

Typical Usage:
  from features.window_mover import WindowMover
  
  window_mover = WindowMover()
  window_mover.start()  # Begin monitoring window events
  
  # Window positioning happens automatically based on rules
  
  window_mover.stop()   # Stop monitoring
"""
import logging
import threading
import time
import win32api
import win32con
import win32gui
import win32process
import ctypes
from ctypes import wintypes
import math
import os
import psutil 

from services.wheelhouse.config_service import ConfigService

CONFIG = ConfigService()

logger = logging.getLogger(__name__)

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8001
EVENT_OBJECT_DESTROY = 0x8003
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_OBJECT_FOCUS = 0x8005
EVENT_OBJECT_SELECTION = 0x8006
EVENT_OBJECT_STATECHANGE = 0x800A
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_SYSTEM_MENUSTART = 0x0004
EVENT_SYSTEM_MENUEND = 0x0005
EVENT_SYSTEM_MENUPOPUPSTART = 0x0006
EVENT_SYSTEM_MENUPOPUPEND = 0x0007


WinEventProcType = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD
)

EVENT_MAP = {
    EVENT_SYSTEM_FOREGROUND: "EVENT_SYSTEM_FOREGROUND",
    EVENT_OBJECT_CREATE: "EVENT_OBJECT_CREATE",
    EVENT_OBJECT_SHOW: "EVENT_OBJECT_SHOW",
    EVENT_OBJECT_HIDE: "EVENT_OBJECT_HIDE",
    EVENT_OBJECT_DESTROY: "EVENT_OBJECT_DESTROY",
    EVENT_SYSTEM_MOVESIZEEND: "EVENT_SYSTEM_MOVESIZEEND",
    EVENT_OBJECT_FOCUS: "EVENT_OBJECT_FOCUS",
    EVENT_OBJECT_SELECTION: "EVENT_OBJECT_SELECTION",
    EVENT_OBJECT_STATECHANGE: "EVENT_OBJECT_STATECHANGE",
    EVENT_OBJECT_LOCATIONCHANGE: "EVENT_OBJECT_LOCATIONCHANGE",
    EVENT_SYSTEM_MENUSTART: "EVENT_SYSTEM_MENUSTART",
    EVENT_SYSTEM_MENUEND: "EVENT_SYSTEM_MENUEND",
    EVENT_SYSTEM_MENUPOPUPSTART: "EVENT_SYSTEM_MENUPOPUPSTART",
    EVENT_SYSTEM_MENUPOPUPEND: "EVENT_SYSTEM_MENUPOPUPEND",
}

OBJID_MAP = {
    win32con.OBJID_WINDOW: "OBJID_WINDOW",
    win32con.OBJID_SYSMENU: "OBJID_SYSMENU",
    win32con.OBJID_TITLEBAR: "OBJID_TITLEBAR",
    win32con.OBJID_MENU: "OBJID_MENU", 
    win32con.OBJID_CLIENT: "OBJID_CLIENT",
    win32con.OBJID_VSCROLL: "OBJID_VSCROLL",
    win32con.OBJID_HSCROLL: "OBJID_HSCROLL",
    win32con.OBJID_CARET: "OBJID_CARET",
    win32con.OBJID_CURSOR: "OBJID_CURSOR", 
    win32con.OBJID_ALERT: "OBJID_ALERT",
    win32con.OBJID_SOUND: "OBJID_SOUND",
    0xFFFFFFF0: "OBJID_MENUPOPUP_ALT" 
}


class WindowMover:
    """:flow: On-Screen Keyboard Repositioning
    :step: 1
    :description: The `WindowMover` class is initialized. It identifies the on-screen keyboard window by its title or process name and stores its handle (`obstructing_window_hwnd`). It also sets up WinEvent hooks to listen for system events like window focus changes.
    :data_in: Configuration settings for target window names.
    :data_out: An initialized `WindowMover` instance with the target window handle.
    """
    def __init__(self, screen_width, screen_height):
        self.obstructing_window_hwnd = None
        self.obstructing_window_rect = None
        
        self.true_screen_width = screen_width
        self.true_screen_height = screen_height
        self.effective_screen_x = 0
        self.effective_screen_y = 0
        self.effective_screen_width = screen_width
        self.effective_screen_height = screen_height
        
        self.event_hooks = []
        self.hook_thread = None
        self.running = False
        self.last_move_time = 0

        self.target_names = CONFIG.get("WINDOW_MOVER_TARGET_NAMES", ["On-Screen Keyboard", "osk"])
        self.move_cooldown = CONFIG.get("WINDOW_MOVER_COOLDOWN", 0.5)
        self.ignore_titles = [t.lower() for t in CONFIG.get("WINDOW_MOVER_IGNORE_TITLES", [])]
        self.ignore_classes = [c.lower() for c in CONFIG.get("WINDOW_MOVER_IGNORE_CLASSES", [])]
        self.event_source_ignore_classes = [c.lower() for c in CONFIG.get("WINDOW_MOVER_EVENT_SOURCE_IGNORE_CLASSES", [])]
        self.clearance_gap = CONFIG.get("WINDOW_MOVER_CLEARANCE_GAP", 5)

        self.message_only_hwnd = None
        self.win_event_proc_cb = None
        self._gui_thread_id = None
        self.window_class_name = f"WindowMoverMsgWnd_{os.getpid()}"
        self._thread_ready_event = threading.Event()
        self.desktop_hwnd = win32gui.GetDesktopWindow()
        self.taskbar_hwnd = None

    def is_alive(self) -> bool:
        """Returns True if the hook thread is running."""
        return self.running and self.hook_thread is not None and self.hook_thread.is_alive()

    def _update_effective_screen_boundaries(self):
        old_effective_rect = (self.effective_screen_x, self.effective_screen_y, self.effective_screen_width, self.effective_screen_height)
        self.effective_screen_x = 0
        self.effective_screen_y = 0
        self.effective_screen_width = self.true_screen_width
        self.effective_screen_height = self.true_screen_height
        if not self.taskbar_hwnd or not win32gui.IsWindow(self.taskbar_hwnd):
            self.taskbar_hwnd = win32gui.FindWindow("Shell_TrayWnd", None) 
        if self.taskbar_hwnd and win32gui.IsWindowVisible(self.taskbar_hwnd):
            try:
                tb_left, tb_top, tb_right, tb_bottom = win32gui.GetWindowRect(self.taskbar_hwnd)
                tb_width = tb_right - tb_left; tb_height = tb_bottom - tb_top
                if tb_left == 0 and tb_width > 0 and tb_width < self.true_screen_width / 2 : 
                    self.effective_screen_x = tb_right
                    self.effective_screen_width = self.true_screen_width - tb_right
                elif tb_right == self.true_screen_width and tb_width > 0 and tb_width < self.true_screen_width / 2: 
                    self.effective_screen_width = tb_left
                elif tb_top == 0 and tb_height > 0 and tb_height < self.true_screen_height / 2: 
                    self.effective_screen_y = tb_bottom
                    self.effective_screen_height = self.true_screen_height - tb_bottom
                elif tb_bottom == self.true_screen_height and tb_height > 0 and tb_height < self.true_screen_height / 2: 
                    self.effective_screen_height = tb_top
            except Exception as e: logger.error(f"WindowMover: Error processing taskbar: {e}")
        new_effective_rect = (self.effective_screen_x, self.effective_screen_y, self.effective_screen_width, self.effective_screen_height)
        if old_effective_rect != new_effective_rect:
            logger.info(f"WindowMover: Effective screen updated: x={self.effective_screen_x}, y={self.effective_screen_y}, w={self.effective_screen_width}, h={self.effective_screen_height}")
            if self.obstructing_window_hwnd and self.obstructing_window_rect:
                osk_x, osk_y, osk_w, osk_h = self.obstructing_window_rect
                if not self.is_position_valid(osk_x, osk_y, osk_w, osk_h):
                    logger.info("WindowMover: OSK out of new bounds. Triggering check.")
                    """:flow: On-Screen Keyboard Repositioning
                    :step: 2
                    :description: A system event is triggered (e.g., a new window gaining focus, the caret moving, or the taskbar changing). The WinEvent hook callback (`_win_event_proc`) is invoked by the OS.
                    :data_in: System event details (event type, window handle, etc.).
                    :data_out: A call to `check_for_overlaps_and_move`.
                    """
                    self.check_for_overlaps_and_move()

    def find_obstructing_window(self):
        """
        :flow: Window Repositioning
        :step: 2
        :produces_for: Window Repositioning
        :description: Locates the on-screen keyboard (OSK) window handle by title search and process enumeration.
        :data_in: self.target_names (list of OSK window title patterns).
        :data_out: Returns OSK window handle (HWND) or None if not found.
        :notes: Uses two-phase detection: first searches visible windows by title match, then falls back to
            process enumeration to find osk.exe if title search fails. This is necessary because the OSK window
            title can vary and the window may not always match expected patterns.
        """
        result = []
        def callback(hwnd, param):
            """EnumWindows callback to find OSK by title.
            
            Args:
                hwnd: Window handle being enumerated
                param: Result list to append matches
            """
            if not win32gui.IsWindowVisible(hwnd): return
            try:
                title = win32gui.GetWindowText(hwnd)
                for name in self.target_names:
                    if name.lower() in title.lower():
                        param.append(hwnd); logger.debug(f"WindowMover: Found OSK (obstructing window): {hwnd} - {title}"); return
            except Exception: pass
        win32gui.EnumWindows(callback, result)
        if result: logger.info(f"WindowMover: Found OSK (obstructing window) by title: {result[0]}"); return result[0]
        if any("osk" in name.lower() for name in self.target_names):
            try:
                for pid in win32process.EnumProcesses():
                    try:
                        h_process = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
                        try:
                            exe = win32process.GetModuleFileNameEx(h_process, 0)
                            if exe.lower().endswith("\\osk.exe") or "\\osk.exe" in exe.lower():
                                proc_windows = []
                                def enum_proc_windows_callback(hwnd_proc, lparam_proc):
                                    """EnumWindows callback to find windows for specific PID.
                                    
                                    Args:
                                        hwnd_proc: Window handle being enumerated
                                        lparam_proc: Result list for matched windows
                                    """
                                    try:
                                        _, found_pid = win32process.GetWindowThreadProcessId(hwnd_proc)
                                        if found_pid == pid and win32gui.IsWindowVisible(hwnd_proc): lparam_proc.append(hwnd_proc)
                                    except Exception: pass
                                win32gui.EnumWindows(enum_proc_windows_callback, proc_windows)
                                if proc_windows:
                                    win32api.CloseHandle(h_process)
                                    logger.info(f"WindowMover: Found OSK (obstructing window) via process: {proc_windows[0]}"); return proc_windows[0]
                        except Exception: pass
                        finally: win32api.CloseHandle(h_process)
                    except Exception: pass
            except Exception as e: logger.error(f"WindowMover: Error searching OSK process: {e}")
        return None

    def get_window_rect(self, hwnd):
        """Get window rectangle as (x, y, width, height) tuple.
        
        Args:
            hwnd: Window handle
            
        Returns:
            Tuple (x, y, width, height) or None on error
        """
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            return (left, top, right - left, bottom - top)
        except Exception as e:
            logger.warning(f"WindowMover: GetWindowRect failed for HWND {hwnd}. Error: {e}"); return None

    def get_visible_windows_info(self):
        """Enumerate all visible windows and return their information.
        
        Returns:
            List of dicts with hwnd, title, class_name, rect for each window
        """
        windows = []
        def callback(hwnd, param):
            """EnumWindows callback to collect visible window information.
            
            Args:
                hwnd: Window handle being enumerated
                param: List to append window info dicts
            """
            if hwnd == self.message_only_hwnd or hwnd == self.obstructing_window_hwnd: return
            if not win32gui.IsWindowVisible(hwnd): return
            try:
                title = win32gui.GetWindowText(hwnd)
                rect_tuple = win32gui.GetWindowRect(hwnd)
                x, y, r, b = rect_tuple; width, height = r - x, b - y
                if width <= 0 or height <= 0: return
                class_name = win32gui.GetClassName(hwnd)
                param.append({'hwnd': hwnd, 'title': title, 'class_name': class_name, 'rect': (x, y, width, height)})
            except Exception: pass
        win32gui.EnumWindows(callback, windows)
        return windows

    def is_window_maximized(self, hwnd):
        """Check if window is currently maximized.
        
        Args:
            hwnd: Window handle
            
        Returns:
            Boolean: True if maximized, False otherwise
        """
        if not hwnd: return False
        try:
            placement = win32gui.GetWindowPlacement(hwnd)
            return placement[1] == win32con.SW_SHOWMAXIMIZED
        except Exception as e:
            logger.debug(f"WindowMover: Could not get window placement for HWND {hwnd}: {e}")
            return False

    def is_ignorable_obstructed_window(self, obstructed_window_info):
        """Check if obstructed window should be ignored for repositioning.
        
        Args:
            obstructed_window_info: Dict with hwnd, title, class_name, rect
            
        Returns:
            bool: True if window should be ignored, False if requires action
        """
        if not obstructed_window_info: return True
        hwnd = obstructed_window_info['hwnd']
        title = obstructed_window_info['title'].lower()
        class_name = obstructed_window_info['class_name'].lower()
        if hwnd == self.desktop_hwnd:
            logger.debug(f"WindowMover: Obstructed window is desktop, ignoring.")
            return True
        if title in self.ignore_titles:
            logger.debug(f"WindowMover: Obstructed window title '{title}' in general ignore list.")
            return True
        if class_name in self.ignore_classes: 
            logger.debug(f"WindowMover: Obstructed window class '{class_name}' in general ignore list.")
            return True
        if self.is_window_maximized(hwnd):
            if self.obstructing_window_rect:
                osk_w, osk_h = self.obstructing_window_rect[2], self.obstructing_window_rect[3]
                if osk_w < self.true_screen_width * 0.75 and osk_h < self.true_screen_height * 0.75:
                    logger.debug(f"WindowMover: Obstructed window '{title}' is maximized, OSK is smaller, ignoring.")
                    return True
            else:
                logger.debug(f"WindowMover: Obstructed window '{title}' is maximized, OSK rect unavailable, tentatively ignoring.")
                return True
        return False

    def is_position_valid(self, x, y, width, height):
        """
        :flow: OSK Position Validation
        :step: 1
        :description: Validates proposed window position against screen boundaries.
        :data_in: Candidate position (x, y) and dimensions (width, height).
        :data_out: Boolean indicating if position fits within effective screen area.
        :notes: Checks if the given window rect would fit entirely within the effective screen
        boundaries (accounting for taskbar and reserved areas). Used by WindowMover
        to validate OSK repositioning before attempting the move.
        """
        right_boundary = self.effective_screen_x + self.effective_screen_width
        bottom_boundary = self.effective_screen_y + self.effective_screen_height
        return (x >= self.effective_screen_x and
                y >= self.effective_screen_y and
                x + width <= right_boundary and
                y + height <= bottom_boundary)

    def find_clear_spot_for_osk(self, obstructed_window_rect, osk_x, osk_y, osk_width, osk_height):
        """
        :flow: Window Repositioning
        :step: 3
        :produces_for: Window Repositioning
        :description: Calculates optimal OSK position adjacent to obstructed window using distance-based ranking.
        :data_in: obstructed_window_rect (tuple: x, y, width, height), current OSK position and dimensions.
        :data_out: Tuple (new_x, new_y) for OSK placement, or original position if no valid spot found.
        :notes: Tests four candidate positions (right, left, below, above the obstructed window) with clearance gap.
            Filters candidates by screen bounds and collision avoidance, then selects the closest valid position
            to minimize visual disruption. Returns original position as fallback.
        """
        ow_x, ow_y, ow_width, ow_height = obstructed_window_rect
        gap = self.clearance_gap
        candidate_positions = [
            (ow_x + ow_width + gap, ow_y + (ow_height - osk_height) // 2),
            (ow_x - osk_width - gap, ow_y + (ow_height - osk_height) // 2),
            (ow_x + (ow_width - osk_width) // 2, ow_y + ow_height + gap),
            (ow_x + (ow_width - osk_width) // 2, ow_y - osk_height - gap)
        ]
        valid_clear_spots = []
        for new_x, new_y in candidate_positions:
            if not self.is_position_valid(new_x, new_y, osk_width, osk_height): continue
            clears_obstructed = (new_x >= ow_x + ow_width or new_x + osk_width <= ow_x or
                                 new_y >= ow_y + ow_height or new_y + osk_height <= ow_y)
            if clears_obstructed:
                distance = math.sqrt((new_x - osk_x)**2 + (new_y - osk_y)**2)
                valid_clear_spots.append({'x': new_x, 'y': new_y, 'distance': distance})
        if not valid_clear_spots:
            logger.debug("WindowMover: No adjacent clear spot found for OSK.")
            return osk_x, osk_y
        valid_clear_spots.sort(key=lambda p: p['distance'])
        chosen_spot = valid_clear_spots[0]
        logger.debug(f"WindowMover: Found clear spot for OSK at ({chosen_spot['x']}, {chosen_spot['y']}) with distance {chosen_spot['distance']:.1f}")
        return chosen_spot['x'], chosen_spot['y']

    def move_obstructing_window(self, x, y):
        """
        :flow: Window Repositioning
        :step: 4
        :description: Executes the actual OSK window move using Win32 API with state restoration and verification.
        :data_in: x, y (target coordinates), self.obstructing_window_hwnd (OSK window handle).
        :data_out: Boolean indicating move success, updates self.obstructing_window_rect on success.
        :notes: Handles multiple edge cases: restore minimized windows, ensure window visibility, set topmost z-order
            temporarily, apply position with SetWindowPos, verify actual position against target with 10px tolerance.
            Includes cooldown throttling to prevent rapid repeated moves.
        """
        if not self.obstructing_window_hwnd or not self.obstructing_window_rect: return False
        current_time = time.time()
        if current_time - self.last_move_time < self.move_cooldown:
            logger.debug("WindowMover: Move cooldown."); return False
        try:
            _, _, width, height = self.obstructing_window_rect
            target_x, target_y = int(x), int(y)
            current_style = win32gui.GetWindowLong(self.obstructing_window_hwnd, win32con.GWL_STYLE)
            if not (current_style & win32con.WS_VISIBLE):
                win32gui.ShowWindow(self.obstructing_window_hwnd, win32con.SW_SHOWNA); time.sleep(0.05)
            if win32gui.IsIconic(self.obstructing_window_hwnd):
                win32gui.ShowWindow(self.obstructing_window_hwnd, win32con.SW_RESTORE); time.sleep(0.1)
            flags = win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER
            win32gui.SetWindowPos(self.obstructing_window_hwnd, win32con.HWND_TOPMOST, 0,0,0,0, flags | win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            time.sleep(0.05)
            set_pos_result = win32gui.SetWindowPos(self.obstructing_window_hwnd, 0, target_x, target_y, int(width), int(height), flags)
            win32api.SetLastError(0); error_code = win32api.GetLastError()
            if not set_pos_result and error_code != 0:
                logger.error(f"WindowMover: SetWindowPos failed. HWND: {self.obstructing_window_hwnd}, Pos: ({target_x},{target_y}). Error: {error_code} ({win32api.FormatMessage(error_code).strip()})")
                return False
            elif not set_pos_result and error_code == 0:
                 logger.debug(f"WindowMover: SetWindowPos returned failure (0) but GetLastError is 0. HWND: {self.obstructing_window_hwnd}, Pos: ({target_x},{target_y}).")
            new_rect_tuple = self.get_window_rect(self.obstructing_window_hwnd)
            if new_rect_tuple and abs(new_rect_tuple[0] - target_x) < 10 and abs(new_rect_tuple[1] - target_y) < 10 :
                self.obstructing_window_rect = new_rect_tuple; self.last_move_time = current_time
                logger.info(f"WindowMover: Moved OSK near ({target_x},{target_y}). Actual: {new_rect_tuple[:2]}")
                return True
            logger.warning(f"WindowMover: OSK did not verify move. Expected ({target_x},{target_y}), got {new_rect_tuple[:2] if new_rect_tuple else 'None'}. SetWindowPos: {set_pos_result}, LastError: {error_code}")
            return False
        except Exception as e:
            logger.error(f"WindowMover: Exception in move_obstructing_window: {e}", exc_info=True); return False

    def check_for_overlaps_and_move(self):
        """:flow: On-Screen Keyboard Repositioning
        :step: 3
        :description: The function gets the current position of the on-screen keyboard and a list of all other visible windows on the screen.
        :data_in: The handle to the on-screen keyboard window (`self.obstructing_window_hwnd`).
        :data_out: The keyboard's current rectangle and a list of dictionaries, each containing info about a visible window (handle, title, rectangle).
        """
        if not self.running: return
        if not self.obstructing_window_hwnd or \
           not win32gui.IsWindow(self.obstructing_window_hwnd) or \
           not win32gui.IsWindowVisible(self.obstructing_window_hwnd):
            self.obstructing_window_hwnd = self.find_obstructing_window()
            if not self.obstructing_window_hwnd: return
            self.obstructing_window_rect = self.get_window_rect(self.obstructing_window_hwnd)
            if not self.obstructing_window_rect: self.obstructing_window_hwnd = None; return
            logger.info(f"WindowMover: Acquired OSK {self.obstructing_window_hwnd} ('{win32gui.GetWindowText(self.obstructing_window_hwnd)}') at {self.obstructing_window_rect}")

        current_osk_rect_tuple = self.get_window_rect(self.obstructing_window_hwnd)
        if not current_osk_rect_tuple: self.obstructing_window_hwnd = None; return
        self.obstructing_window_rect = current_osk_rect_tuple
        
        osk_x, osk_y, osk_w, osk_h = self.obstructing_window_rect
        visible_windows = self.get_visible_windows_info()
        
        problematic_obstructed_window = None
        """:flow: On-Screen Keyboard Repositioning
        :step: 4
        :description: It iterates through the visible windows to find one that significantly overlaps with the on-screen keyboard. It ignores overlaps with windows that are maximized or on an ignore list.
        :data_in: The keyboard's rectangle and the list of visible windows.
        :data_out: The window information dictionary for the first problematic overlapping window found.
        """
        for other_window_info in visible_windows:
            ow_x, ow_y, ow_width, ow_height = other_window_info['rect']
            overlap = not (osk_x + osk_w <= ow_x or osk_x >= ow_x + ow_width or
                           osk_y + osk_h <= ow_y or osk_y >= ow_y + ow_height)
            if overlap:
                if self.is_ignorable_obstructed_window(other_window_info):
                    logger.debug(f"WindowMover: OSK overlaps with ignorable window '{other_window_info['title']}' (Class: {other_window_info['class_name']}). No action based on this obstruction.")
                    continue
                else:
                    problematic_obstructed_window = other_window_info
                    logger.info(f"WindowMover: OSK at ({osk_x},{osk_y}) obstructs '{problematic_obstructed_window['title']}' (Class: {problematic_obstructed_window['class_name']}) at ({ow_x},{ow_y}).")
                    break 

        if problematic_obstructed_window:
            """:flow: On-Screen Keyboard Repositioning
            :step: 5
            :description: If a problematic overlap is found, this function calculates the best new position for the keyboard. It checks four candidate positions (right, left, below, above the overlapping window) and chooses the one that is on-screen and closest to the keyboard's original position.
            :data_in: The rectangles for both the keyboard and the obstructed window.
            :data_out: The `(x, y)` coordinates for the best new position.
            """
            new_x, new_y = self.find_clear_spot_for_osk(
                problematic_obstructed_window['rect'], osk_x, osk_y, osk_w, osk_h
            )
            if new_x != osk_x or new_y != osk_y:
                """:flow: On-Screen Keyboard Repositioning
                :step: 6
                :description: The function calls the `SetWindowPos` Win32 API to move the on-screen keyboard to the newly calculated clear spot.
                :data_in: The target `(x, y)` coordinates.
                :data_out: A `SetWindowPos` API call that moves the window on the screen.
                """
                logger.info(f"WindowMover: Attempting to move OSK to clear '{problematic_obstructed_window['title']}'. New pos: ({new_x},{new_y})")
                self.move_obstructing_window(new_x, new_y)
            else:
                logger.info(f"WindowMover: OSK obstructs '{problematic_obstructed_window['title']}', but no suitable clear spot found or current spot is best. OSK remains at ({osk_x},{osk_y}).")
        else:
            logger.debug("WindowMover: No problematic overlaps detected for OSK.")

    def win_event_proc(self, hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
        """
        :flow: Window Repositioning
        :step: 1
        :produces_for: Window Repositioning
        :description: Windows UI event hook callback - detects window creation, foreground changes, and position updates.
        :data_in: Win32 event parameters from SetWinEventHook (event type, window handle, object ID, thread ID).
        :data_out: Triggers check_and_move_if_needed() when relevant window events detected.
        :notes: Registered via SetWinEventHook to receive all UI automation events from Windows. Filters out noise
            (cursor moves, non-window events) and analyzes window properties (title, class, style, rect) to determine
            if OSK positioning logic should run. Critical events: EVENT_SYSTEM_FOREGROUND (window activation),
            EVENT_OBJECT_CREATE/SHOW (new windows), EVENT_OBJECT_LOCATIONCHANGE (window moves). This is the
            entry point that triggers the entire Window Repositioning flow when windows appear or change.
        """
        if not self.running: return
        
        # --- Initial RAW Log (Conditional to reduce noise) ---
        # Only log raw if it's NOT a cursor location change or if HWND is not None
        # (OBJID_CURSOR is -9, which is 0xFFFFFFF7)
        if not (event == EVENT_OBJECT_LOCATIONCHANGE and idObject == win32con.OBJID_CURSOR and hwnd == 0):
            event_name_raw = EVENT_MAP.get(event, f"0x{event:X}")
            id_object_name_raw = OBJID_MAP.get(idObject, f"0x{idObject:X}")
            logger.debug(f"RAW_CALLBACK_ENTRY: event={event_name_raw}, hwnd={hwnd}, idObject={id_object_name_raw}, idChild={idChild}, Thread={dwEventThread}")

        # --- Basic Filters ---
        if hwnd == 0 or hwnd is None: return # Ignore events with no valid HWND
        if hwnd == self.obstructing_window_hwnd or hwnd == self.message_only_hwnd: return
        
        # --- Get Event Window Details ---
        event_name_str = EVENT_MAP.get(event, f"UnknownEvent_0x{event:04X}")
        log_prefix_detail = f"WindowMover EventProc ({event_name_str} for HWND: {hwnd}):"
        
        event_window_title = "N/A"; event_window_class_name = "N/A"; pid = 0; process_name = "N/A"
        style = 0; ex_style = 0; parent_hwnd = 0; rect_str = "N/A"

        try:
            event_window_title = win32gui.GetWindowText(hwnd)
            event_window_class_name = win32gui.GetClassName(hwnd).lower()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid != 0:
                try: process_name = psutil.Process(pid).name()
                except (psutil.NoSuchProcess, psutil.AccessDenied): pass
            
            if win32gui.IsWindow(hwnd):
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                parent_hwnd = win32gui.GetParent(hwnd)
                temp_rect = self.get_window_rect(hwnd)
                if temp_rect: rect_str = f"({temp_rect[0]},{temp_rect[1]} {temp_rect[2]}x{temp_rect[3]})"
        except Exception as e:
            logger.debug(f"{log_prefix_detail} Exception getting event window details: {e}. Details: Title='{event_window_title}', Class='{event_window_class_name}'")

        # Log detailed info only for potentially interesting events to reduce noise
        if event in [EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW, EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUSTART, EVENT_OBJECT_FOCUS]:
            logger.debug(f"{log_prefix_detail} Details: Title='{event_window_title}', Class='{event_window_class_name}', PID={pid}, Process='{process_name}', Style=0x{style:08X}, ExStyle=0x{ex_style:08X}, ParentHWND={parent_hwnd}, Rect={rect_str}")

        # --- Specific Event Handling ---
        if event_window_class_name == "shell_traywnd":
            if event in [EVENT_OBJECT_SHOW, EVENT_OBJECT_HIDE, EVENT_SYSTEM_MOVESIZEEND, EVENT_OBJECT_CREATE, EVENT_OBJECT_DESTROY]:
                logger.info(f"{log_prefix_detail} Taskbar event. Updating effective screen boundaries.")
                self._update_effective_screen_boundaries()
            return 

        should_trigger_overlap_check = False
        if event == EVENT_SYSTEM_FOREGROUND:
            current_rect = self.get_window_rect(hwnd) 
            if current_rect and self.is_ignorable_obstructed_window({'hwnd': hwnd, 'title': event_window_title, 'class_name': event_window_class_name, 'rect': current_rect}):
                logger.debug(f"{log_prefix_detail} Foreground changed to an ignorable window. No OSK move.")
            else:
                logger.debug(f"{log_prefix_detail} Foreground changed. Queuing overlap check.")
                should_trigger_overlap_check = True
        
        elif event in [EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW, EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUSTART]:
            if ex_style & win32con.WS_EX_TOOLWINDOW:
                logger.debug(f"{log_prefix_detail} Event from TOOLWINDOW. Ignoring."); return
            
            is_known_menu_class = (event_window_class_name == "#32768" or event_window_class_name == "xaml_windowedpopupclass")

            if ex_style & win32con.WS_EX_NOACTIVATE and not is_known_menu_class:
                logger.debug(f"{log_prefix_detail} Event from NOACTIVATE window (and not known menu class). Ignoring."); return
            
            if event_window_class_name in self.event_source_ignore_classes:
                logger.debug(f"{log_prefix_detail} Event from explicitly ignored source class '{event_window_class_name}'. Ignoring."); return
            
            is_dialog_class = (event_window_class_name == "#32770")
            has_caption_sysmenu = (style & win32con.WS_CAPTION) and (style & win32con.WS_SYSMENU)
            
            if is_dialog_class or is_known_menu_class or has_caption_sysmenu:
                logger.debug(f"{log_prefix_detail} Event window type significant (Dialog/KnownMenu/Caption+SysMenu). Queuing overlap check.")
                should_trigger_overlap_check = True
            else:
                # For other popups (not explicitly dialog/menu/captioned)
                is_popup_style = bool(style & win32con.WS_POPUP)
                if is_popup_style: # Check if it's a general popup
                    current_rect = self.get_window_rect(hwnd) 
                    # Consider it significant if it's a popup and has a reasonable size
                    if current_rect and (current_rect[2] > 50 and current_rect[3] > 20) or \
                                        (current_rect[3] > 50 and current_rect[2] > 20):
                        logger.debug(f"{log_prefix_detail} Event from reasonably sized POPUP window. Queuing overlap check.")
                        should_trigger_overlap_check = True
                    else:
                        logger.debug(f"{log_prefix_detail} Event from POPUP window, but too small or rect error. Size: {current_rect[2]}x{current_rect[3] if current_rect else 'N/A'}. Ignoring.")
                else:
                    logger.debug(f"{log_prefix_detail} Event window did not meet significance criteria. Ignoring.")
        
        if should_trigger_overlap_check:
            logger.info(f"{log_prefix_detail} Event triggering OSK overlap check.")
            self.check_for_overlaps_and_move()

    def _wnd_proc_map(self, hwnd_param, msg, wparam, lparam):
        if msg == win32con.WM_DESTROY:
            logger.debug(f"WindowMover: WM_DESTROY received by message window {hwnd_param}. Posting WM_QUIT.")
            win32gui.PostQuitMessage(0); return 0
        return win32gui.DefWindowProc(hwnd_param, msg, wparam, lparam)

    def _create_message_window_and_hooks(self):
        self._gui_thread_id = threading.get_ident()
        if not self.win_event_proc_cb: self.win_event_proc_cb = WinEventProcType(self.win_event_proc)
        wndclass = win32gui.WNDCLASS()
        wndclass.hInstance = win32api.GetModuleHandle(None)
        wndclass.lpszClassName = self.window_class_name
        wndclass.lpfnWndProc = self._wnd_proc_map
        try:
            try: win32gui.UnregisterClass(wndclass.lpszClassName, wndclass.hInstance)
            except Exception: pass
            class_atom = win32gui.RegisterClass(wndclass)
            self.message_only_hwnd = win32gui.CreateWindowEx(0, class_atom, "WindowMoverMessageOnly", 0,0,0,0,0, win32con.HWND_MESSAGE, None, wndclass.hInstance, None)
            if not self.message_only_hwnd:
                logger.error(f"WindowMover: Failed to create message-only window. Error: {win32api.GetLastError()}"); return False
            logger.debug(f"WindowMover: Message-only window created: {self.message_only_hwnd}")
            self._thread_ready_event.set()
        except Exception as e:
            logger.error(f"WindowMover: Error creating message window: {e}"); self._thread_ready_event.set(); return False
        self.cleanup_hooks()
        events_to_hook = [
            EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW, 
            EVENT_OBJECT_HIDE, EVENT_OBJECT_DESTROY, EVENT_SYSTEM_MOVESIZEEND,
            EVENT_OBJECT_FOCUS, EVENT_OBJECT_STATECHANGE, EVENT_OBJECT_LOCATIONCHANGE,
            EVENT_SYSTEM_MENUSTART, EVENT_SYSTEM_MENUEND,
            EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUPOPUPEND
        ]
        successful_hooks = 0
        for event_type in events_to_hook:
            event_name_for_log = EVENT_MAP.get(event_type, f"0x{event_type:04X}")
            hook = ctypes.windll.user32.SetWinEventHook(event_type, event_type, 0, self.win_event_proc_cb, 0, 0, win32con.WINEVENT_OUTOFCONTEXT | win32con.WINEVENT_SKIPOWNPROCESS)
            if hook: 
                self.event_hooks.append(hook)
                successful_hooks +=1
                logger.debug(f"WindowMover: Successfully set hook for event {event_name_for_log} (Handle: {hook})")
            else: 
                logger.error(f"WindowMover: Failed to set hook for event {event_name_for_log}. Error: {win32api.GetLastError()}")
        
        logger.info(f"WindowMover: Set up {successful_hooks}/{len(events_to_hook)} event hooks.")
        if not self.event_hooks:
             logger.error("WindowMover: Failed to set any event hooks at all.")
             return False
        return True

    def message_loop(self):
        """:flow: Window Repositioning
        :step: 5
        :description: Background thread runs Windows message pump and monitors hook events
        :data_in: Windows accessibility events via registered hooks
        :data_out: OSK repositioning triggers via check_for_overlaps_and_move()
        :notes: Core monitoring loop running in dedicated thread. Creates message window and hooks via _create_message_window_and_hooks(), finds initial OSK position, then runs PeekMessage/DispatchMessage loop processing Windows events. Accessibility hooks fire win_event_proc() callback (step 1) which triggers overlap detection (steps 2-4). Uses 50ms sleep between PeekMessage polls to avoid busy-wait. Cleanup via _destroy_message_window_and_hooks() on thread exit.
        """
        if not self._create_message_window_and_hooks():
            logger.error("WindowMover: Failed to set up hooks/message window. Thread exiting.")
            self.running = False; self._thread_ready_event.set(); return

        self._update_effective_screen_boundaries()

        if not self.obstructing_window_hwnd: self.obstructing_window_hwnd = self.find_obstructing_window()
        if self.obstructing_window_hwnd:
            if not self.obstructing_window_rect: self.obstructing_window_rect = self.get_window_rect(self.obstructing_window_hwnd)
            if self.obstructing_window_rect:
                 logger.info(f"WindowMover: Initial OSK '{win32gui.GetWindowText(self.obstructing_window_hwnd)}' at {self.obstructing_window_rect}")
                 if not self.is_position_valid(*self.obstructing_window_rect):
                     logger.info("WindowMover: Initial OSK position invalid based on effective screen. Triggering move.")
                 self.check_for_overlaps_and_move()
            else:
                 logger.warning("WindowMover: Found initial OSK but could not get its rect.")
                 self.obstructing_window_hwnd = None
        else: logger.info("WindowMover: OSK not found initially.")
        
        msg = wintypes.MSG()
        logger.info("WindowMover: Starting message loop (event-driven only).")
        
        while self.running:
            if ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), self.message_only_hwnd, 0, 0, win32con.PM_REMOVE):
                if msg.message == win32con.WM_QUIT:
                    logger.info("WindowMover: WM_QUIT received via PeekMessage, exiting message loop.")
                    self.running = False; break 
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
            if not self.running: break
            time.sleep(0.05)
            
        logger.info("WindowMover: Message loop has exited.")
        self._destroy_message_window_and_hooks()

    def cleanup_hooks(self):
        """Unhook all Windows accessibility event hooks.
        
        Called during shutdown to release hook resources.
        """
        if self.event_hooks:
            for hook in self.event_hooks:
                if hook:
                    try: ctypes.windll.user32.UnhookWinEvent(hook)
                    except Exception as e: logger.error(f"WindowMover: Error unhooking event {hook}: {e}")
            self.event_hooks = []
            logger.debug("WindowMover: Cleaned up event hooks.")

    def _destroy_message_window_and_hooks(self):
        self.cleanup_hooks()
        current_thread_id = threading.get_ident()
        if self._gui_thread_id is not None and self._gui_thread_id != current_thread_id:
            logger.warning(f"WindowMover: _destroy_message_window_and_hooks called from wrong thread ({current_thread_id} vs {self._gui_thread_id}). Skipping explicit DestroyWindow and UnregisterClass from here.")
            return

        if self.message_only_hwnd and win32gui.IsWindow(self.message_only_hwnd):
             logger.debug(f"WindowMover: Message-only HWND {self.message_only_hwnd} still exists. Attempting explicit DestroyWindow.")
             try:
                 win32gui.DestroyWindow(self.message_only_hwnd)
                 logger.debug(f"WindowMover: Explicit DestroyWindow called for {self.message_only_hwnd}.")
             except Exception as e:
                 logger.error(f"WindowMover: Error explicitly destroying message window {self.message_only_hwnd}: {e}")
        self.message_only_hwnd = None
        
        try:
            h_instance = win32api.GetModuleHandle(None)
            time.sleep(0.1) 
            if win32gui.UnregisterClass(self.window_class_name, h_instance):
                logger.info(f"WindowMover: Window class '{self.window_class_name}' unregistered by GUI thread.")
            else:
                err = win32api.GetLastError()
                if err != 0 and err != 1411:
                     logger.warning(f"WindowMover: Could not unregister class '{self.window_class_name}' by GUI thread. Error: {err} ({win32api.FormatMessage(err).strip()})")
                elif err == 1411:
                     logger.debug(f"WindowMover: Class '{self.window_class_name}' already unregistered or never registered (Error {err}).")
        except Exception as e:
            logger.error(f"WindowMover: Exception unregistering window class by GUI thread: {e}")

    def start(self):
        """Start WindowMover by launching background hook thread.
        
        Returns:
            bool: True if thread started successfully, False on timeout
        """
        if self.running: logger.warning("WindowMover: Start called but already running."); return True
        self.running = True
        self._thread_ready_event.clear()
        self.hook_thread = threading.Thread(target=self.message_loop, daemon=True)
        self.hook_thread.name = "WindowMoverThread"
        self.hook_thread.start()
        if not self._thread_ready_event.wait(timeout=3.0):
            logger.error("WindowMover: Timeout waiting for WindowMoverThread to become ready.")
            self.running = False; return False
        logger.info("WindowMover: Started successfully and thread is ready.")
        return True

    def stop(self):
        """Stop WindowMover by signaling thread and waiting for graceful shutdown.
        
        Waits up to 4s for thread join, logs warning if thread doesn't stop.
        """
        if not self.running:
            logger.info("WindowMover: Stop called but not running or already stopped.")
            return
        
        logger.info("WindowMover: Initiating stop sequence...")
        self.running = False 

        if self.message_only_hwnd and self._gui_thread_id:
            if win32gui.IsWindow(self.message_only_hwnd):
                logger.debug(f"WindowMover: Posting WM_NULL to message window HWND {self.message_only_hwnd} to wake up its loop.")
                if not win32gui.PostMessage(self.message_only_hwnd, win32con.WM_NULL, 0, 0):
                    logger.debug(f"WindowMover: Failed to post WM_NULL. Error: {win32api.GetLastError()}")
            else:
                logger.debug(f"WindowMover: Message window HWND {self.message_only_hwnd} is no longer valid during stop().")
        
        if self.hook_thread and self.hook_thread.is_alive():
            logger.debug(f"WindowMover: Waiting for thread {self.hook_thread.name} to join...")
            self.hook_thread.join(timeout=4.0) 
            if self.hook_thread.is_alive():
                logger.warning(f"WindowMover: Thread {self.hook_thread.name} did not stop gracefully after join timeout.")
            else:
                logger.info(f"WindowMover: Thread {self.hook_thread.name} joined successfully.")
        
        self.hook_thread = None
        logger.info("WindowMover: Stop sequence completed.")