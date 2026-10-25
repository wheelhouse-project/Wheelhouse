"""Check that a Ctrl+V paste reached the text of a RichEditD2DPT control.

wh-dictation-paste-loss-load: Windows 11 Notepad (control class
RichEditD2DPT) sometimes does not paste after WheelHouse sends Ctrl+V.
Measured 2026-10-19: 10 of 1,161 dictated words without processor load,
and Notepad never opened the clipboard for any lost word. verified_paste
checks its own copy and the key send, so it reported success for every
lost word. This module gives verified_paste the text length of the
control before and after the paste, so it can paste once more.

Scope is one class on purpose (boss ruling 2026-10-19 21:35): every
window answers WM_GETTEXTLENGTH, and a top-level window answers with its
caption length, which a paste never changes. A browser or Electron
window would then look like a lost paste on every word and get every
word twice. Another class needs its own evidence first.

Every query uses SendMessageTimeout with SMTO_ABORTIFHUNG, so a hung
target cannot stop the Input process. A query with no answer means "do
not know", and the caller then does not paste a second time.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import functools
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

import win32gui

logger = logging.getLogger(__name__)

PASTE_CHECK_CLASSES: frozenset[str] = frozenset({"RichEditD2DPT"})

WM_GETTEXTLENGTH = 0x000E
EM_GETSEL = 0x00B0
SMTO_ABORTIFHUNG = 0x0002
QUERY_TIMEOUT_MS = 100
POLL_INTERVAL_S = 0.005


@dataclass(frozen=True)
class PasteCheck:
    """The control to watch, its text length before the paste, and its window.

    root_hwnd is the window the paste check was prepared for (the caller's
    target window). The second Ctrl+V is sent only while the keyboard focus
    of that window is still ``hwnd``.
    """

    hwnd: int
    length_before: int
    root_hwnd: int


@dataclass(frozen=True)
class LengthWait:
    """The result of wait_for_length_change.

    status is "changed", "unchanged" (no change before the deadline), or
    "query_failed" (the control did not answer; length is then None).
    elapsed_s counts from the start of the wait.
    """

    status: str
    length: Optional[int]
    elapsed_s: float


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("flags", wt.DWORD),
        ("hwndActive", wt.HWND),
        ("hwndFocus", wt.HWND),
        ("hwndCapture", wt.HWND),
        ("hwndMenuOwner", wt.HWND),
        ("hwndMoveSize", wt.HWND),
        ("hwndCaret", wt.HWND),
        ("rcCaret", wt.RECT),
    ]


def max_growth_of_one_paste(text: str) -> int:
    """The most the control's text length can grow from ONE copy of ``text``.

    WM_GETTEXTLENGTH counts UTF-16 code units, and a rich edit control
    stores a line break as two ("\\r\\n"). A Python character outside the
    BMP (an emoji) is two units. Counting ``len(text)`` made the growth of
    one emoji look like two copies.
    """
    return len(text.encode("utf-16-le")) // 2 + text.count("\n")


def _send_with_timeout(hwnd: int, msg: int) -> Optional[int]:
    """Send ``msg`` (wParam 0, lParam 0); None on a timeout or an error."""
    try:
        _ok, result = win32gui.SendMessageTimeout(
            hwnd, msg, 0, 0, SMTO_ABORTIFHUNG, QUERY_TIMEOUT_MS,
        )
    except Exception as exc:  # pywintypes.error on a timeout or a hung target
        logger.debug("paste check: message %#x to %s failed: %s", msg, hwnd, exc)
        return None
    return int(result) & 0xFFFFFFFF


def focused_control_hwnd(root_hwnd: int) -> int:
    """The window with keyboard focus on the GUI thread of ``root_hwnd``.

    This is the window that receives the Ctrl+V. 0 when it cannot be read.
    """
    try:
        user32 = ctypes.windll.user32
        thread_id = user32.GetWindowThreadProcessId(wt.HWND(root_hwnd), None)
        if not thread_id:
            return 0
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return 0
        return int(info.hwndFocus or 0)
    except Exception as exc:
        logger.debug("paste check: GetGUIThreadInfo failed: %s", exc)
        return 0


def _class_name(hwnd: int) -> str:
    try:
        return win32gui.GetClassName(hwnd)
    except Exception:
        return ""


def _text_length(hwnd: int) -> Optional[int]:
    return _send_with_timeout(hwnd, WM_GETTEXTLENGTH)


@functools.lru_cache(maxsize=None)
def _user32() -> ctypes.WinDLL:
    """A private user32 handle, so the argtypes set below reach no other code."""
    return ctypes.WinDLL("user32")


def _query_selection(hwnd: int) -> Optional[tuple[int, int]]:
    """(start, end) of the selection of ``hwnd``; None when it did not answer.

    EM_GETSEL with null pointers packs the start and the end in 16 bits
    each and returns -1 past 65535 characters. With two DWORD pointers
    the control writes both positions in full. The pointers are valid in
    this process only, and Windows copies an EM_GETSEL result across the
    process boundary (measured 2026-10-19 on a Notepad window with 70,000
    characters, in another process: (70000, 70000), and (10, 35000) after
    EM_SETSEL(10, 35000)). A zero return of SendMessageTimeoutW (a timeout
    or a hung target) or an exception means "do not know".
    """
    start = wt.DWORD(0)
    end = wt.DWORD(0)
    result = ctypes.c_size_t(0)
    try:
        send = _user32().SendMessageTimeoutW
        send.argtypes = [
            wt.HWND, wt.UINT, ctypes.c_void_p, ctypes.c_void_p,
            wt.UINT, wt.UINT, ctypes.c_void_p,
        ]
        send.restype = wt.LPARAM
        answered = send(
            wt.HWND(hwnd), EM_GETSEL, ctypes.byref(start), ctypes.byref(end),
            SMTO_ABORTIFHUNG, QUERY_TIMEOUT_MS, ctypes.byref(result),
        )
    except Exception as exc:
        logger.debug("paste check: EM_GETSEL to %s failed: %s", hwnd, exc)
        return None
    if not answered:
        return None
    return int(start.value), int(end.value)


def _selection_is_empty(hwnd: int) -> Optional[bool]:
    """True for a caret with no selection, None when the control did not answer."""
    selection = _query_selection(hwnd)
    if selection is None:
        return None
    start, end = selection
    return start == end


def prepare_paste_check(
    target_hwnd: Optional[int], target_class_name: str,
) -> tuple[Optional[PasteCheck], str]:
    """Read the control's length before the paste.

    Returns (check, "ready"), or (None, reason) when the paste gets no
    check. A non-empty selection gets no check: the paste replaces it, so
    the length can stay the same after a good paste.
    """
    if target_class_name not in PASTE_CHECK_CLASSES:
        return None, "class_out_of_scope"
    if not target_hwnd:
        return None, "no_target_window"
    hwnd = focused_control_hwnd(target_hwnd)
    if not hwnd:
        return None, "no_focused_control"
    if _class_name(hwnd) not in PASTE_CHECK_CLASSES:
        return None, "focused_class_mismatch"
    empty = _selection_is_empty(hwnd)
    if empty is None:
        return None, "selection_query_failed"
    if not empty:
        return None, "selection_not_empty"
    length = _text_length(hwnd)
    if length is None:
        return None, "length_query_failed"
    return PasteCheck(hwnd=hwnd, length_before=length, root_hwnd=target_hwnd), "ready"


def wait_for_length_change(
    hwnd: int,
    length_before: int,
    wait_s: float,
    *,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> LengthWait:
    """Poll the length until it differs from ``length_before`` or ``wait_s`` ends.

    Reads at least once, so a wait of 0 is one read.
    """
    start = clock()
    while True:
        length = _text_length(hwnd)
        elapsed = clock() - start
        if length is None:
            return LengthWait("query_failed", None, elapsed)
        if length != length_before:
            return LengthWait("changed", length, elapsed)
        if elapsed >= wait_s:
            return LengthWait("unchanged", length, elapsed)
        sleep(POLL_INTERVAL_S)
