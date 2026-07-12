"""Screen dimension and display utilities.

This module provides utilities for querying screen dimensions and display
properties. It serves as a centralized interface for screen-related operations
throughout the WheelHouse application with error handling and fallback mechanisms.

Key Functions:
  - get_screen_size: Retrieves primary display dimensions with error handling.

Display Detection:
  - Uses win32api.GetSystemMetrics for primary display dimensions
    (wh-drop-pyautogui: pyautogui is not used anywhere in the app anymore;
    its win32 install dependency MouseInfo is GPLv3, unshippable under
    Apache-2.0)
  - Handles detection failures gracefully, including the Win32 convention
    of returning 0 instead of raising
  - Provides sensible defaults (1920x1080) on detection failure

Typical Usage:
  from utils.screen import get_screen_size

  width, height = get_screen_size()
"""

import logging

import win32api

logger = logging.getLogger(__name__)

# GetSystemMetrics indices for the PRIMARY monitor's dimensions.
SM_CXSCREEN = 0
SM_CYSCREEN = 1


def get_screen_size():
    """
    Get the primary screen dimensions.

    Returns:
        tuple: (width, height) of the screen
    """
    try:
        width = win32api.GetSystemMetrics(SM_CXSCREEN)
        height = win32api.GetSystemMetrics(SM_CYSCREEN)
        if width <= 0 or height <= 0:
            # GetSystemMetrics signals failure by returning 0, not raising.
            raise RuntimeError(f"GetSystemMetrics returned {width}x{height}")
        logger.debug(f"Screen size detected: {width}x{height}")
        return width, height
    except Exception as e:
        logger.error(f"Error getting screen size: {e}")
        # Return a default size if theres an error
        return 1920, 1080
