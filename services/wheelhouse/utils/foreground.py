"""Bring a window to the foreground past the Windows foreground lock.

wh-activate-windows-terminal.3 moved these two functions here from
``terminal_editor_window`` (where they were ``_steal_foreground`` and
``_default_win32_ops``), so that the Input process can use them without
importing PySide6. This module uses ctypes only.

``terminal_editor_window`` still binds ``_steal_foreground`` to
``steal_foreground`` at module level; the GUI process's callers and the
tests that patch ``terminal_editor_window._steal_foreground`` depend on
that name.
"""
import ctypes
import logging

log = logging.getLogger(__name__)


def default_win32_ops() -> dict:
    """Return a fresh Win32 callable namespace for steal_foreground.

    Lazy-binds ctypes attributes and sets strict argtypes once per call
    so a stray non-int cannot silently corrupt a call. Returns a plain
    dict so tests can substitute a fake namespace via the ``win32_ops``
    argument to ``steal_foreground``.
    """
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [
        wintypes.DWORD, wintypes.DWORD, wintypes.BOOL,
    ]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return {
        "GetForegroundWindow": user32.GetForegroundWindow,
        "GetWindowThreadProcessId": lambda hwnd: int(
            user32.GetWindowThreadProcessId(hwnd, None)
        ),
        "GetCurrentThreadId": kernel32.GetCurrentThreadId,
        "AttachThreadInput": user32.AttachThreadInput,
        "SetForegroundWindow": user32.SetForegroundWindow,
        "BringWindowToTop": user32.BringWindowToTop,
    }


def steal_foreground(win_hwnd: int, win32_ops: dict | None = None) -> bool:
    """Bring ``win_hwnd`` to the foreground, bypassing the Windows lock.

    wh-redirect-steal-foreground. The terminal-dictation editor opens
    in response to a voice event that travels STT -> Logic -> Input ->
    GUI. By the time the GUI process calls ``SetForegroundWindow`` for
    the editor, Windows refuses the call: the GUI process has no
    recent user-input attribution and is not the current foreground.
    Without the bypass the editor stays behind the terminal until the
    user clicks it, and the focus-redirect path drops every word it
    tried to drain. The Input process's voice "activate" command has
    the same problem (wh-activate-windows-terminal.3).

    Standard Windows workaround: attach this thread's input queue to
    the current foreground window's thread, then call
    ``SetForegroundWindow``. The lock check treats the caller as if
    it were the foreground thread and lets the call through. Detach
    immediately afterwards so keyboard / mouse capture do not stay
    shared. Returns True if ``SetForegroundWindow`` reported success.

    The optional ``win32_ops`` parameter is a dependency-injection
    seam for tests; production callers leave it None.
    """
    try:
        ops = win32_ops if win32_ops is not None else default_win32_ops()
        fg_hwnd = int(ops["GetForegroundWindow"]())
        current_thread = int(ops["GetCurrentThreadId"]())
        if not fg_hwnd:
            return bool(ops["SetForegroundWindow"](win_hwnd))
        fg_thread = int(ops["GetWindowThreadProcessId"](fg_hwnd))
        if not fg_thread or fg_thread == current_thread:
            return bool(ops["SetForegroundWindow"](win_hwnd))
        attached = bool(
            ops["AttachThreadInput"](current_thread, fg_thread, True)
        )
        try:
            ops["BringWindowToTop"](win_hwnd)
            result = bool(ops["SetForegroundWindow"](win_hwnd))
        finally:
            if attached:
                ops["AttachThreadInput"](
                    current_thread, fg_thread, False,
                )
        return result
    except Exception as exc:
        log.debug("steal_foreground failed: %s", exc)
        return False
