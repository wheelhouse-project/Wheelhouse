"""Software-based display dimming using overlay window technique.

This module implements a software dimming solution using a translucent overlay
window that covers all screens. It provides an alternative to hardware brightness
control when hardware dimming is not available or insufficient. The dimmer uses
Windows API to create a layered window with adjustable transparency for precise
brightness control.

Key Classes:
  - SoftwareDimmer: Main dimming controller with overlay window management.

Key Features:
  - Multi-monitor overlay support with automatic screen detection
  - Layered window with alpha blending for smooth dimming effect
  - Thread-safe window creation and management
  - Adjustable dimming levels with real-time updates
  - Windows message pump integration for proper window lifecycle
  - Automatic cleanup and resource management

Technical Implementation:
  - WS_EX_LAYERED window style for transparency effects
  - WS_EX_TOPMOST for always-on-top behavior
  - WS_EX_TRANSPARENT for click-through functionality
  - Custom window procedure for message handling
  - Multithreaded operation for non-blocking UI integration

Typical Usage:
  from handlers.software_dimmer import SoftwareDimmer
  
  dimmer = SoftwareDimmer()
  
  # Set dimming level (0.0 = no dim, 1.0 = maximum dim)
  dimmer.set_brightness(0.3)  # 30% dimming
  
  # Toggle dimming
  dimmer.toggle()
  
  # Cleanup
  dimmer.stop()
"""
# handlers/software_dimmer.py
import ctypes
from ctypes import wintypes
import threading
import logging
import asyncio
import time
from typing import Optional, Any

logger = logging.getLogger(__name__)

LRESULT = ctypes.c_ssize_t 

class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)),
                ("cbClsExtra", wintypes.INT),
                ("cbWndExtra", wintypes.INT),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HANDLE)]

class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC),
                ("fErase", wintypes.BOOL),
                ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", wintypes.BYTE * 32)]

WS_POPUP = 0x80000000
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
LWA_ALPHA = 0x00000002
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
WM_USER = 0x0400
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_MOUSEACTIVATE = 0x0021
WM_NCHITTEST = 0x0084
WM_QUIT = 0x0012
WM_USER_UPDATE_ALPHA = WM_USER + 100
MA_NOACTIVATE = 3
HTTRANSPARENT = -1
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
IDC_ARROW = 32512
COLOR_BLACK = 0x00000000
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
PM_REMOVE = 0x0001

class SoftwareDimmer:
    def __init__(self, loop: asyncio.AbstractEventLoop, initial_brightness: int = 100):
        self.loop = loop
        self.hwnd: Optional[wintypes.HWND] = None
        self._gui_thread: Optional[threading.Thread] = None
        self._gui_thread_id: Optional[int] = None
        self._window_destroyed_event = threading.Event()

        self.current_brightness_percent = initial_brightness
        self.current_alpha = self._brightness_to_alpha(initial_brightness)

        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.gdi32 = ctypes.windll.gdi32

        self._setup_ctypes_prototypes()

        self.window_class_name = "SoftwareDimmerOverlay_AlphaBlend_v12" 
        self._wnd_proc_instance = WNDPROC(self._wnd_proc_py)
        self.black_brush_handle: Optional[wintypes.HBRUSH] = None

        logger.info(f"SoftwareDimmer ({self.window_class_name}) initialized. Initial brightness: {initial_brightness}%, Alpha: {self.current_alpha}")

    def _setup_ctypes_prototypes(self):
        """Configure ctypes function prototypes for Windows API calls.
        
        Sets argument and return types for all Win32 functions used.
        """
        self.user32.DefWindowProcW.restype = LRESULT 
        self.user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.UpdateWindow.restype = wintypes.BOOL
        self.user32.UpdateWindow.argtypes = [wintypes.HWND] 
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.DestroyWindow.argtypes = [wintypes.HWND] 
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        self.user32.LoadCursorW.restype = wintypes.HANDLE
        self.gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
        self.gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        self.gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self.gdi32.DeleteObject.restype = wintypes.BOOL
        self.user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
        self.user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        self.user32.RegisterClassExW.restype = wintypes.ATOM
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID
        ]
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        self.user32.BeginPaint.restype = wintypes.HDC
        self.user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.user32.PeekMessageW.restype = wintypes.BOOL 
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.TranslateMessage.restype = wintypes.BOOL 
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.restype = LRESULT 
        self.user32.PostQuitMessage.argtypes = [ctypes.c_int]
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.kernel32.GetLastError.restype = wintypes.DWORD
        self.user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.user32.UnregisterClassW.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

    def _brightness_to_alpha(self, brightness_percent: int) -> int:
        """Convert brightness percentage (0-100) to alpha value (0-255).
        
        Args:
            brightness_percent: Brightness level 0-100
            
        Returns:
            int: Alpha value 0-255 (0=full dim, 255=no dim)
        """
        alpha = 255 - int(brightness_percent * 2.55)
        return max(0, min(255, alpha))

    def _alpha_to_brightness(self, alpha_value: int) -> int:
        """Convert alpha value (0-255) to brightness percentage (0-100).
        
        Args:
            alpha_value: Alpha value 0-255
            
        Returns:
            int: Brightness percentage 0-100
        """
        brightness_percent = (255 - alpha_value) / 2.55
        return int(max(0, min(100, round(brightness_percent))))

    def _wnd_proc_py(self, hwnd_val_param_raw: Any, msg: wintypes.UINT, wparam: wintypes.WPARAM, lparam: wintypes.LPARAM) -> Any: 
        hwnd_val_param: wintypes.HWND
        if isinstance(hwnd_val_param_raw, wintypes.HWND):
            hwnd_val_param = hwnd_val_param_raw
        elif isinstance(hwnd_val_param_raw, int): 
            hwnd_val_param = wintypes.HWND(hwnd_val_param_raw)
        elif hwnd_val_param_raw is None: 
             hwnd_val_param = wintypes.HWND(0)
        else:
            logger.error(f"SoftwareDimmer: _wnd_proc_py received unexpected type for hwnd_val_param: {type(hwnd_val_param_raw)}. Treating as HWND(0).")
            hwnd_val_param = wintypes.HWND(0)

        hwnd_to_use: wintypes.HWND = self.hwnd if (self.hwnd and self.hwnd.value == hwnd_val_param.value) else hwnd_val_param

        if msg == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = self.user32.BeginPaint(hwnd_to_use, ctypes.byref(ps))
            client_rect = wintypes.RECT()
            self.user32.GetClientRect(hwnd_to_use, ctypes.byref(client_rect))
            if self.black_brush_handle:
                 self.user32.FillRect(hdc, ctypes.byref(client_rect), self.black_brush_handle)
            else: 
                 logger.warning("SoftwareDimmer: WM_PAINT - black_brush_handle not set. Using temporary brush.")
                 temp_black_brush = self.gdi32.CreateSolidBrush(wintypes.COLORREF(COLOR_BLACK))
                 self.user32.FillRect(hdc, ctypes.byref(client_rect), temp_black_brush)
                 self.gdi32.DeleteObject(temp_black_brush)
            self.user32.EndPaint(hwnd_to_use, ctypes.byref(ps))
            return 0 
        elif msg == WM_NCHITTEST:
            return HTTRANSPARENT 
        elif msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE 
        elif msg == WM_USER_UPDATE_ALPHA:
            new_alpha = int(wparam.value if hasattr(wparam, 'value') else wparam) 
            if 0 <= new_alpha <= 255:
                self.current_alpha = new_alpha
                self.current_brightness_percent = self._alpha_to_brightness(new_alpha)
                if self.hwnd and self.hwnd.value == hwnd_to_use.value:
                    if not self.user32.SetLayeredWindowAttributes(self.hwnd, 0, self.current_alpha, LWA_ALPHA):
                        logger.error(f"SoftwareDimmer: SetLayeredWindowAttributes failed in WM_USER_UPDATE_ALPHA. Error: {self.kernel32.GetLastError()}")
                    else:
                        logger.debug(f"SoftwareDimmer: Alpha updated to {self.current_alpha} (Brightness: {self.current_brightness_percent}%) for HWND {self.hwnd.value}")
                else:
                    logger.warning(f"SoftwareDimmer: WM_USER_UPDATE_ALPHA received but self.hwnd is invalid ({self.hwnd}) or message is for a different HWND ({hwnd_to_use.value if hasattr(hwnd_to_use, 'value') else hwnd_to_use}).")
            else:
                logger.warning(f"SoftwareDimmer: WM_USER_UPDATE_ALPHA received invalid alpha: {new_alpha}")
            return 0 
        elif msg == WM_CLOSE:
            logger.info(f"SoftwareDimmer: WM_CLOSE received for HWND {hwnd_to_use.value if hwnd_to_use and hasattr(hwnd_to_use, 'value') else 'None'}")
            if hwnd_to_use and hasattr(hwnd_to_use, 'value') and hwnd_to_use.value: 
                try:
                    destroy_result = self.user32.DestroyWindow(hwnd_to_use) 
                    logger.info(f"SoftwareDimmer: DestroyWindow called from WM_CLOSE, result: {destroy_result}. HWND was {hwnd_to_use.value}")
                    if not destroy_result:
                        logger.error(f"SoftwareDimmer: DestroyWindow call in WM_CLOSE failed. Error: {self.kernel32.GetLastError()}")
                except Exception as e:
                     logger.error(f"SoftwareDimmer: Error during DestroyWindow in WM_CLOSE: {e}", exc_info=True)
            return 0 
        elif msg == WM_DESTROY:
            logger.info(f"SoftwareDimmer: WM_DESTROY received for HWND {hwnd_val_param.value if hwnd_val_param and hasattr(hwnd_val_param, 'value') else 'None'}.")
            if self.hwnd and hasattr(hwnd_val_param, 'value') and hwnd_val_param.value == self.hwnd.value:
                logger.info("SoftwareDimmer: WM_DESTROY is for our managed window.")
                self.hwnd = None 
                logger.info("SoftwareDimmer: self.hwnd nulled in WM_DESTROY.")
                self._window_destroyed_event.set() 
                self.user32.PostQuitMessage(0) 
            else:
                logger.debug("SoftwareDimmer: WM_DESTROY is for a different window or self.hwnd is already None. Not posting quit from here.")
            return 0 

        return self.user32.DefWindowProcW(hwnd_val_param, msg, wparam, lparam)

    def _register_dimmer_window_class(self, h_instance: wintypes.HINSTANCE) -> bool:
        """Register Windows window class for dimmer overlay.
        
        Args:
            h_instance: Module handle from GetModuleHandleW
            
        Returns:
            bool: True if registration successful or class already exists
        """
        self.black_brush_handle = self.gdi32.CreateSolidBrush(wintypes.COLORREF(COLOR_BLACK))
        if not self.black_brush_handle:
            logger.error(f"SoftwareDimmer: Failed to create black brush.")
            return False
        wnd_class = WNDCLASSEXW()
        wnd_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wnd_class.style = CS_HREDRAW | CS_VREDRAW
        wnd_class.lpfnWndProc = self._wnd_proc_instance
        wnd_class.hInstance = h_instance
        wnd_class.hCursor = self.user32.LoadCursorW(None, wintypes.LPCWSTR(IDC_ARROW)) 
        wnd_class.hbrBackground = self.black_brush_handle
        wnd_class.lpszClassName = self.window_class_name
        
        atom = self.user32.RegisterClassExW(ctypes.byref(wnd_class))
        if not atom:
            err_code = self.kernel32.GetLastError()
            if err_code == 1410: 
                logger.warning(f"SoftwareDimmer: Window class '{self.window_class_name}' already registered. This might be okay.")
            else:
                logger.error(f"SoftwareDimmer: Failed to register window class '{self.window_class_name}'. Error: {err_code}")
                if self.black_brush_handle: self.gdi32.DeleteObject(self.black_brush_handle)
                self.black_brush_handle = None
                return False
        logger.info(f"SoftwareDimmer: Window class '{self.window_class_name}' registration successful (Atom: {atom}) or already existed.")
        return True

    def _create_dimmer_overlay_window(self, h_instance: wintypes.HINSTANCE) -> Optional[wintypes.HWND]:
        """Create layered overlay window covering all screens.
        
        Args:
            h_instance: Module handle from GetModuleHandleW
            
        Returns:
            Optional[HWND]: Window handle if successful, None otherwise
        """
        border_gap = 1 
        screen_x = self.user32.GetSystemMetrics(SM_XVIRTUALSCREEN) + border_gap
        screen_y = self.user32.GetSystemMetrics(SM_YVIRTUALSCREEN) + border_gap
        screen_width = max(0, self.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) - (2 * border_gap))
        screen_height = max(0, self.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) - (2 * border_gap))

        dwExStyle = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        dwStyle = WS_POPUP

        logger.debug(f"SoftwareDimmer: Creating window with EX_STYLE: {hex(dwExStyle)}, STYLE: {hex(dwStyle)}")
        logger.debug(f"SoftwareDimmer: Window Rect: x={screen_x}, y={screen_y}, w={screen_width}, h={screen_height}")

        hwnd_raw_result = self.user32.CreateWindowExW(
            dwExStyle, self.window_class_name, self.window_class_name, 
            dwStyle, screen_x, screen_y, screen_width, screen_height,
            None, None, h_instance, None
        )
        
        final_hwnd: Optional[wintypes.HWND] = None
        if isinstance(hwnd_raw_result, wintypes.HWND):
            if hwnd_raw_result.value != 0: 
                final_hwnd = hwnd_raw_result
            else: 
                logger.debug("SoftwareDimmer: CreateWindowExW returned HWND(0).") 
        elif isinstance(hwnd_raw_result, int):
            if hwnd_raw_result != 0: 
                logger.warning(f"SoftwareDimmer: CreateWindowExW returned int: {hwnd_raw_result}. Wrapping to HWND.")
                final_hwnd = wintypes.HWND(hwnd_raw_result)
            else: 
                logger.debug("SoftwareDimmer: CreateWindowExW returned int 0.")
        
        if not final_hwnd:
            err_code = self.kernel32.GetLastError()
            logger.error(f"SoftwareDimmer: CreateWindowExW failed. Raw result: {hwnd_raw_result} (type: {type(hwnd_raw_result).__name__}). Error: {err_code}")
            return None
        
        logger.info(f"SoftwareDimmer: Window created successfully (HWND: {final_hwnd.value}).")
        return final_hwnd


    def _run_dimmer_message_loop(self):
        """Run Windows message pump until WM_QUIT received.
        
        Uses PeekMessage with 10ms sleep to avoid busy-wait.
        """
        msg = wintypes.MSG()
        p_msg = ctypes.byref(msg)
        
        logger.info("SoftwareDimmer: Starting message loop.")
        while True:
            bRet = self.user32.PeekMessageW(p_msg, None, 0, 0, PM_REMOVE)
            if bRet: 
                if msg.message == WM_QUIT:
                    logger.info("SoftwareDimmer: WM_QUIT received via PeekMessage, exiting message loop.")
                    break 
                self.user32.TranslateMessage(p_msg)
                self.user32.DispatchMessageW(p_msg) 
            else: 
                time.sleep(0.01) 
        logger.info("SoftwareDimmer: Message loop ended.")

    def _cleanup_dimmer_gui_resources(self, h_instance: Optional[wintypes.HINSTANCE]):
        """Cleanup Windows resources on shutdown.
        
        Args:
            h_instance: Module handle for window class unregistration
        """
        if h_instance and self.window_class_name: 
            if not self.user32.UnregisterClassW(self.window_class_name, h_instance):
                err_code = self.kernel32.GetLastError()
                logger.warning(f"SoftwareDimmer: Failed to unregister window class '{self.window_class_name}'. Error: {err_code}.")
            else:
                logger.info(f"SoftwareDimmer: Window class '{self.window_class_name}' unregistered.")

        if self.black_brush_handle:
            # Refined GDI brush deletion logging
            if not self.gdi32.DeleteObject(self.black_brush_handle):
                err_code = self.kernel32.GetLastError() 
                logger.warning(f"SoftwareDimmer: DeleteObject for GDI brush returned 0 (failure). GetLastError: {err_code}")
            else:
                logger.info("SoftwareDimmer: GDI brush deleted.")
            self.black_brush_handle = None
        
        if self.hwnd:
            logger.warning("SoftwareDimmer: self.hwnd was not None during _cleanup_dimmer_gui_resources.")
            self.hwnd = None 
        logger.info("SoftwareDimmer: GUI resources cleanup attempted.")


    def _gui_thread_target(self):
        """:flow: Software Display Dimming
        :step: 1
        :description: Background thread creates and manages layered overlay window
        :data_in: Initial brightness from configuration
        :data_out: Overlay window with alpha transparency applied
        :notes: Core dimming implementation. Runs in dedicated thread (required for Windows message loop). Registers window class, creates full-screen overlay with WS_EX_LAYERED + WS_EX_TOPMOST + WS_EX_TRANSPARENT styles. Sets initial alpha via SetLayeredWindowAttributes (step 2). Runs message pump processing WM_USER_UPDATE_ALPHA for brightness changes (step 3). On shutdown, unregisters class and deletes GDI brush. This thread enables non-blocking dimming control from main logic process.
        """
        self._gui_thread_id = self.kernel32.GetCurrentThreadId()
        self._window_destroyed_event.clear() 
        logger.info(f"SoftwareDimmer: GUI thread started (ID: {self._gui_thread_id}).")

        h_instance = self.kernel32.GetModuleHandleW(None)
        if not h_instance:
            logger.error("SoftwareDimmer: Failed to get module handle. Aborting GUI thread.")
            return

        if not self._register_dimmer_window_class(h_instance):
            logger.error("SoftwareDimmer: Failed to register window class in GUI thread. Aborting.")
            return

        self.hwnd = self._create_dimmer_overlay_window(h_instance)
        if not self.hwnd: 
            logger.error("SoftwareDimmer: Failed to create overlay window. Aborting GUI thread.")
            self._cleanup_dimmer_gui_resources(h_instance) 
            return

        logger.info(f"SoftwareDimmer: Setting initial alpha to {self.current_alpha} (Brightness: {self.current_brightness_percent}%) for HWND {self.hwnd.value}.")
        if not self.user32.SetLayeredWindowAttributes(self.hwnd, 0, self.current_alpha, LWA_ALPHA):
            err_code = self.kernel32.GetLastError()
            logger.error(f"SoftwareDimmer: Initial SetLayeredWindowAttributes failed. Error: {err_code}")
            self.user32.DestroyWindow(self.hwnd) 
            self._cleanup_dimmer_gui_resources(h_instance)
            return 
        else:
            logger.info(f"SoftwareDimmer: Initial LWA_ALPHA set for HWND {self.hwnd.value}.")

        self.user32.ShowWindow(self.hwnd, ctypes.c_int(5)) 
        
        if not self.user32.UpdateWindow(self.hwnd):
            logger.debug(f"SoftwareDimmer: UpdateWindow called for HWND {self.hwnd.value}.")
        
        self._run_dimmer_message_loop() 
        logger.info("SoftwareDimmer: GUI thread message loop exited.")

        if not self._window_destroyed_event.is_set():
            logger.warning("SoftwareDimmer: _window_destroyed_event not set after loop exit.")
            if self.hwnd and self.hwnd.value and self.user32.IsWindow(self.hwnd):
                 logger.warning(f"SoftwareDimmer: Forcing DestroyWindow for HWND {self.hwnd.value} as a fallback.")
                 if not self.user32.DestroyWindow(self.hwnd): 
                     logger.error(f"SoftwareDimmer: Fallback DestroyWindow failed. Error: {self.kernel32.GetLastError()}")
            self.hwnd = None 

        self._cleanup_dimmer_gui_resources(h_instance)
        logger.info("SoftwareDimmer: GUI thread finished.")


    def set_brightness(self, brightness_percent: int) -> bool:
        """:flow: Brightness Control Abstraction
        :step: 3a
        :description: Software implementation of brightness control via overlay window
        :data_in: Brightness percentage (0-100)
        :data_out: Window Alpha Update
        :notes: Used when hardware brightness is at minimum (extended range).

        :flow: Software Display Dimming
        :step: 2
        :description: Public API for brightness changes - posts message to GUI thread
        :data_in: Brightness percentage 0-100 from caller
        :data_out: WM_USER_UPDATE_ALPHA message to GUI thread window
        :notes: Thread-safe brightness control. Converts brightness to alpha (0=no dim, 255=full dim), posts WM_USER_UPDATE_ALPHA to GUI thread via PostMessageW. GUI thread processes in step 3. If window not yet created, updates internal state variables for use during window initialization. This message-based approach avoids cross-thread direct API calls.

        Returns:
            True if brightness change was posted or stored,
            False if PostMessageW failed.
        """
        new_alpha = self._brightness_to_alpha(brightness_percent)
        logger.info(f"SoftwareDimmer: set_brightness called. Brightness: {brightness_percent}%, Target Alpha: {new_alpha}")

        if self.hwnd and self.hwnd.value and self._gui_thread_id:
            if self.user32.IsWindow(self.hwnd):
                if self.user32.PostMessageW(self.hwnd, WM_USER_UPDATE_ALPHA, wintypes.WPARAM(new_alpha), 0):
                    logger.debug(f"SoftwareDimmer: Posted WM_USER_UPDATE_ALPHA with alpha {new_alpha} to HWND {self.hwnd.value}.")
                    return True
                else:
                    logger.warning(f"SoftwareDimmer: Failed to post WM_USER_UPDATE_ALPHA to HWND {self.hwnd.value}. Error: {self.kernel32.GetLastError()}")
                    return False
            else:
                logger.warning(f"SoftwareDimmer: set_brightness - window HWND {self.hwnd.value if hasattr(self.hwnd, 'value') else self.hwnd} is no longer valid.")
                self.current_alpha = new_alpha
                self.current_brightness_percent = brightness_percent
                return True
        else:
            logger.warning("SoftwareDimmer: set_brightness called, but window not available/initialized or GUI thread not started.")
            self.current_alpha = new_alpha
            self.current_brightness_percent = brightness_percent
            return True


    def adjust_brightness(self, delta_percent: int):
        """Adjust brightness by relative delta.
        
        Args:
            delta_percent: Change in brightness (-100 to +100)
        """
        current_brightness = self.current_brightness_percent
        new_brightness = max(0, min(100, current_brightness + delta_percent))
        logger.info(f"SoftwareDimmer: adjust_brightness called. Delta: {delta_percent}%, Current Brightness: {current_brightness}%, New Target Brightness: {new_brightness}%")
        self.set_brightness(new_brightness)


    def start(self):
        """Start software dimmer by launching GUI thread.
        
        Creates background thread running _gui_thread_target.
        """
        if not self.user32 or not self.gdi32 or not self.kernel32 :
            logger.error("SoftwareDimmer: Cannot start, essential DLLs not loaded.")
            return
        if self._gui_thread and self._gui_thread.is_alive():
            logger.warning("SoftwareDimmer: GUI thread already running.")
            return

        self._window_destroyed_event.clear() 
        self._gui_thread = threading.Thread(target=self._gui_thread_target, name=self.window_class_name, daemon=True)
        self._gui_thread.start()
        logger.info(f"SoftwareDimmer: GUI thread '{self.window_class_name}' initiated.")

    def stop(self):
        """Stop software dimmer by posting WM_CLOSE and waiting for thread.
        
        Waits up to 5s for graceful GUI thread shutdown.
        """
        logger.info(f"SoftwareDimmer: Initiating stop sequence for '{self.window_class_name}'...")

        if self.hwnd and self.hwnd.value and self._gui_thread_id:
            if self.user32.IsWindow(self.hwnd):
                logger.info(f"SoftwareDimmer: Posting WM_CLOSE to HWND {self.hwnd.value}")
                if not self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0): 
                    logger.error(f"SoftwareDimmer: Failed to post WM_CLOSE. Error: {self.kernel32.GetLastError()}.")
                    self._window_destroyed_event.set() 
            else:
                logger.info("SoftwareDimmer: Window already invalid during stop. Setting destroyed event.")
                self._window_destroyed_event.set() 
        else:
            logger.info("SoftwareDimmer: No valid HWND or GUI thread not started. Setting destroyed event.")
            self._window_destroyed_event.set() 

        if self._gui_thread and self._gui_thread.is_alive():
            logger.info(f"SoftwareDimmer: Waiting for GUI thread '{self._gui_thread.name}' to join...")
            self._gui_thread.join(timeout=5.0) 
            if self._gui_thread.is_alive():
                logger.warning("SoftwareDimmer: GUI thread did not stop gracefully after join timeout.")
            else:
                logger.info("SoftwareDimmer: GUI thread joined successfully.")
        
        if self.hwnd: 
             logger.warning(f"SoftwareDimmer: self.hwnd (HWND {self.hwnd.value if hasattr(self.hwnd, 'value') else self.hwnd}) still set after GUI thread join. Nulling explicitly.")
             self.hwnd = None

        logger.info(f"SoftwareDimmer: Stop sequence completed for '{self.window_class_name}'.")