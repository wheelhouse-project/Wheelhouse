"""Window matching abstraction for input process.

Consolidates duplicated window matching logic from input_proc.py:
- enum_callback (lines 70-114): Window enumeration matching
- verification loop (lines 202-231): Foreground verification
- handle_activate (lines 274-275): Activation target matching

This class provides a unified, testable interface for:
- Determining if a target is a process name (.exe) or title pattern
- Getting process names from window handles
- Matching windows by either process name or title regex
"""
import re
import logging
from typing import Optional

import psutil
import win32gui
import win32process

logger = logging.getLogger(__name__)


class WindowMatcher:
    """Utility class for matching windows by process name or title pattern.

    All methods are static since no instance state is needed.
    This makes it easy to use in callbacks and loops.
    """

    @staticmethod
    def is_process_target(target: str) -> bool:
        """Check if target represents a process name (ends with .exe).

        Args:
            target: Window target string

        Returns:
            True if target is a process name, False if title pattern
        """
        return target.lower().endswith(".exe")

    @staticmethod
    def get_process_name(hwnd: int) -> Optional[str]:
        """Get the process name for a window handle.

        Args:
            hwnd: Window handle

        Returns:
            Lowercase process name (e.g., "brave.exe") or None on error
        """
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return psutil.Process(pid).name().lower()
        except Exception:
            return None

    @staticmethod
    def is_visible(hwnd: int) -> bool:
        """Check if a window is visible.

        Args:
            hwnd: Window handle

        Returns:
            True if window is visible, False otherwise
        """
        return win32gui.IsWindowVisible(hwnd)

    @staticmethod
    def get_window_title(hwnd: int) -> str:
        """Get the title of a window.

        Args:
            hwnd: Window handle

        Returns:
            Window title string, or empty string on error
        """
        try:
            return win32gui.GetWindowText(hwnd)
        except Exception:
            return ""

    @staticmethod
    def matches(hwnd: int, target: str) -> bool:
        """Check if a window matches the given target.

        Matches by:
        - Process name if target ends with .exe (case-insensitive exact match)
        - Title pattern otherwise (case-insensitive regex search)

        Args:
            hwnd: Window handle to check
            target: Process name (e.g., "brave.exe") or title pattern

        Returns:
            True if the window matches the target
        """
        if WindowMatcher.is_process_target(target):
            # Match by process name (case-insensitive)
            proc_name = WindowMatcher.get_process_name(hwnd)
            return proc_name == target.lower() if proc_name else False
        else:
            # Match by title pattern (case-insensitive regex)
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return False
            return bool(re.search(target, title, re.IGNORECASE))
