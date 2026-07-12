"""Windows taskbar visibility and management utilities.

This module provides utilities for controlling Windows taskbar visibility
and managing taskbar-related operations. It implements safe taskbar
manipulation with error handling for reliable system integration during
fullscreen operations or kiosk modes.

Key Functions:
  - hide_taskbar: Hides the Windows taskbar from view.
  - show_taskbar: Restores taskbar visibility.
  - Various taskbar state management utilities.

Key Features:
  - Windows taskbar visibility control via Win32 API
  - Error handling for taskbar manipulation failures
  - Taskbar state detection and validation
  - Cross-window compatibility for taskbar operations
  - Safe taskbar manipulation with permission handling

Use Cases:
  - Fullscreen application modes
  - Kiosk mode implementations
  - Distraction-free environments
  - Custom desktop shell applications

Safety Features:
  - Error handling for taskbar access failures
  - State validation before operations
  - Graceful degradation on API failures
  - Logging for troubleshooting taskbar issues

Typical Usage:
  from utils.taskbar import hide_taskbar, show_taskbar
  
  # Hide taskbar for fullscreen mode
  if hide_taskbar():
      # Run fullscreen application
      run_fullscreen_app()
      
      # Restore taskbar when done
      show_taskbar()
"""
"""
Taskbar management functions
"""

import ctypes
import logging

logger = logging.getLogger(__name__)

# Initialize ctypes for taskbar manipulation
user32 = ctypes.windll.user32


def hide_taskbar() -> bool:
    """
    Hide the Windows taskbar.

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        SW_HIDE = 0
        taskbar_hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if taskbar_hwnd:
            is_visible = user32.IsWindowVisible(taskbar_hwnd)
            if is_visible:
                user32.ShowWindow(taskbar_hwnd, SW_HIDE)
                logger.info("Taskbar hidden.")
                return True
            else:
                logger.debug("Taskbar already hidden.")
                return True
        else:
            logger.warning("Could not find taskbar window.")
            return False
    except Exception as e:
        logger.error(f"Error hiding taskbar: {e}")
        return False


def show_taskbar() -> bool:
    """
    Show the Windows taskbar.

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        SW_SHOW = 5
        taskbar_hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if taskbar_hwnd:
            is_visible = user32.IsWindowVisible(taskbar_hwnd)
            if not is_visible:
                user32.ShowWindow(taskbar_hwnd, SW_SHOW)
                logger.info("Taskbar shown.")
                return True
            else:
                logger.debug("Taskbar already visible.")
                return True
        else:
            logger.warning("Could not find taskbar window.")
            return False
    except Exception as e:
        logger.error(f"Error showing taskbar: {e}")
        return False


def is_taskbar_visible() -> bool:
    """
    Check if the taskbar is currently visible.

    Returns:
        bool: True if visible, False if hidden or error
    """
    try:
        taskbar_hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if taskbar_hwnd:
            return bool(user32.IsWindowVisible(taskbar_hwnd))
        else:
            logger.warning("Could not find taskbar window.")
            return False
    except Exception as e:
        logger.error(f"Error checking taskbar visibility: {e}")
        return False