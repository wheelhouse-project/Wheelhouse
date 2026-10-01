"""Second try for a spoken close when Alt+F4 does nothing (wh-xray-close-app-fails).

"x-ray close notepad" and "close window" send Alt+F4 to the window in
front. Some windows ignore that chord (David's 2026-10-01 run left
Notepad open). This module waits a short time after the chord, and when
the same window is still open, in front, enabled and has no owned popup,
it posts ``WM_SYSCOMMAND`` / ``SC_CLOSE`` to it. That is the message the
window's own close button sends, so a window that asks "save changes?"
still asks.

The fallback never posts when:

* the window is a shell window (desktop, taskbar): Alt+F4 there opens the
  shutdown dialog and SC_CLOSE would be wrong;
* the window now holding the handle is not the one Alt+F4 was sent to (the
  provenance marker set before the chord is missing or different), or no
  marker could be set;
* the window is gone, no longer in front (a save prompt or the shutdown
  dialog took the front), disabled (a modal dialog is open), or has an
  enabled owned popup (a save prompt);
* any probe fails.

crewcut: a save prompt drawn INSIDE the application's own window (no
separate popup window, the window stays enabled and in front) cannot be
seen here, so SC_CLOSE is posted while that prompt shows. To remove the
limit later, add a UI Automation check for a dialog inside the window
before the post.
"""
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

import win32gui

from .hwnd_utils import (
    normalize_hwnd_for_foreground_compare,
    read_hwnd_provenance,
)

logger = logging.getLogger(__name__)

CLOSE_WAIT_S = 0.5
CLOSE_POLL_S = 0.05
SHELL_WINDOW_CLASSES = frozenset(
    {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
)
WM_SYSCOMMAND = 0x0112
SC_CLOSE = 0xF060
# win32con.GW_ENABLEDPOPUP, inlined like the constants in hwnd_utils.
GW_ENABLEDPOPUP = 6


def is_close_chord(keys: Any) -> bool:
    """True only for exactly Alt and F4, in either order."""
    if not isinstance(keys, (list, tuple)):
        return False
    try:
        return sorted(str(k).lower() for k in keys) == ["alt", "f4"]
    except Exception:
        return False


def _foreground_root() -> Optional[int]:
    return normalize_hwnd_for_foreground_compare(win32gui.GetForegroundWindow())


def default_close_ops() -> Dict[str, Callable]:
    """The real Win32 calls. Tests pass their own dict to close_if_still_open."""
    return {
        "IsWindow": win32gui.IsWindow,
        "GetForegroundRoot": _foreground_root,
        "IsWindowEnabled": win32gui.IsWindowEnabled,
        "GetWindow": win32gui.GetWindow,
        "GetClassName": win32gui.GetClassName,
        "ReadProvenance": read_hwnd_provenance,
        "PostMessage": win32gui.PostMessage,
        "sleep": time.sleep,
        "monotonic": time.monotonic,
    }


def close_if_still_open(
    hwnd: int, marker: int, *, ops: Optional[dict] = None
) -> str:
    """Post SC_CLOSE to ``hwnd`` when Alt+F4 left it open. Returns the outcome.

    ``marker`` is the provenance marker ``hotkey_action`` stored on the
    window before the chord (ui.hwnd_utils.tag_hwnd_provenance; 0 when
    tagging failed).

    Outcomes: "shell", "closed", "not_in_front", "disabled", "popup",
    "identity_mismatch", "posted", "probe_failed". Only "posted" posts
    anything.
    """
    try:
        o = default_close_ops()
        if ops:
            o.update(ops)
        class_name = o["GetClassName"](hwnd)
        if class_name in SHELL_WINDOW_CLASSES:
            logger.debug("close fallback: hwnd=%s is a shell window (%s)",
                         hwnd, class_name)
            return "shell"
        start = o["monotonic"]()
        while o["IsWindow"](hwnd) and o["monotonic"]() - start < CLOSE_WAIT_S:
            o["sleep"](CLOSE_POLL_S)
        if not o["IsWindow"](hwnd):
            logger.debug("close fallback: hwnd=%s closed after Alt+F4", hwnd)
            return "closed"
        if o["GetForegroundRoot"]() != hwnd:
            logger.debug("close fallback: hwnd=%s no longer in front", hwnd)
            return "not_in_front"
        if not o["IsWindowEnabled"](hwnd):
            logger.debug("close fallback: hwnd=%s is disabled (modal open)", hwnd)
            return "disabled"
        popup = o["GetWindow"](hwnd, GW_ENABLEDPOPUP)
        if popup not in (0, None, hwnd):
            logger.debug("close fallback: hwnd=%s has an enabled popup %s",
                         hwnd, popup)
            return "popup"
        # A numeric handle is not an identity: Windows can give the handle
        # of a window that Alt+F4 just closed to a new foreground window,
        # and every probe above passes for that new window too. The window
        # property set before the chord dies with the original window, so
        # only the original reads back the marker. Read it last, right
        # before the post; a zero marker (tagging failed) never matches.
        if not marker or o["ReadProvenance"](hwnd) != marker:
            logger.debug("close fallback: hwnd=%s is not the window Alt+F4 "
                         "was sent to", hwnd)
            return "identity_mismatch"
        o["PostMessage"](hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)
        logger.info("close fallback: posted SC_CLOSE to hwnd=%s class=%s",
                    hwnd, class_name)
        return "posted"
    except Exception as e:
        logger.debug("close fallback: probe failed for hwnd=%s: %s", hwnd, e,
                     exc_info=True)
        return "probe_failed"


def start_close_fallback(hwnd: int, marker: int) -> threading.Thread:
    """Run close_if_still_open on a daemon thread; never raises."""
    def _run() -> None:
        try:
            close_if_still_open(hwnd, marker)
        except Exception as e:
            logger.debug("close fallback thread failed for hwnd=%s: %s", hwnd, e)

    thread = threading.Thread(target=_run, name="close-fallback", daemon=True)
    thread.start()
    return thread
