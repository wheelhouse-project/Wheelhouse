"""Safe clipboard text retrieval utilities for UI process.

This module provides robust clipboard access functionality with multiple
fallback mechanisms and error handling. It's designed to work reliably
in the UI process context where clipboard access might be contested by
other applications or system limitations.

Key Functions:
  - get_text_safe: Primary clipboard text retrieval with fallbacks.

Key Features:
  - Dual clipboard backend support (win32clipboard + pyperclip)
  - Retry logic for transient clipboard access failures
  - Never-raise error handling for maximum reliability
  - Unicode text format support with proper encoding
  - Fallback mechanisms for robustness

Implementation Strategy:
  - Primary: win32clipboard for direct Windows API access
  - Fallback: pyperclip for cross-platform compatibility
  - Retry mechanism for transient lock conflicts
  - Safe error handling with boolean success indicators

Error Handling:
  - Returns (success, text) tuple for clear error indication
  - Multiple retry attempts for clipboard lock conflicts
  - Graceful degradation on API unavailability
  - No exceptions raised to calling code

Typical Usage:
  from ui.clipboard import get_text_safe
  
  success, clipboard_text = get_text_safe()
  if success:
      process_clipboard_text(clipboard_text)
  else:
      handle_clipboard_error()
"""
# ui/clipboard.py — Safe, best-effort clipboard helpers for the UI process.
import time
from typing import Tuple

def get_text_safe() -> Tuple[bool, str]:
    """
    Returns (ok, text). Never raises.
    Tries win32clipboard first, falls back to pyperclip.
    """
    # win32clipboard path
    try:
        import win32clipboard  # type: ignore
        import win32con  # type: ignore
        for _ in range(3):
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                        data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                        return True, data or ""
                    else:
                        return True, ""
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.05)
        return False, ""
    except Exception:
        pass

    # pyperclip fallback
    try:
        import pyperclip  # type: ignore
        txt = pyperclip.paste()
        return True, txt or ""
    except Exception:
        return False, ""