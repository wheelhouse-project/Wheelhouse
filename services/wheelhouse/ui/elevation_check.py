"""Pre-insertion detection of elevated (administrator) targets.

wh-elevated-target-notice: Windows UIPI silently discards input sent
from a medium-integrity process to a higher-integrity window. SendInput
reports success either way (the documentation is explicit that neither
the return value nor GetLastError indicates UIPI blocking), the
foreground checks still pass, and the clipboard verification checks the
clipboard rather than the target -- so post-insertion detection cannot
work and both insertion strategies would record a false verified
success. The only reliable signal is asking BEFORE typing: does the
process that owns the focused window run at a higher integrity level
than WheelHouse?

The comparison uses INTEGRITY LEVELS, never the TokenElevation flag.
The elevation flag is true for both sides when WheelHouse itself runs
as administrator (typing works fine then) and behaves differently on
UAC-disabled machines; the integrity-level comparison is correct in
both cases.

Access rights: the target process is opened with
PROCESS_QUERY_LIMITED_INFORMATION, the documented right that succeeds
across integrity levels (even on protected processes), and the token
with TOKEN_QUERY, which suffices for TokenIntegrityLevel. Cost is a
handful of kernel calls, well under the milliseconds the UIA text-target
check already spends.

Fail open: ANY failure anywhere returns UNKNOWN and the caller proceeds
through the existing pipeline unchanged. Dictation is never suppressed
on an unproven elevation claim.

WheelHouse's own integrity level cannot change for the lifetime of the
process, so it is computed once and cached. The target is computed per
call -- the focused window changes constantly, and caching it would
reopen the time-of-check gap this module exists to close.
"""

from __future__ import annotations

import logging
from typing import Optional

import win32api
import win32con
import win32gui
import win32process
import win32security

logger = logging.getLogger(__name__)


ELEVATED = "elevated"
NOT_ELEVATED = "not_elevated"
UNKNOWN = "unknown"

# Own-process integrity RID, computed lazily on first use. None means
# "not yet computed or the last attempt failed"; failures retry on the
# next call (they are cheap, and a transient failure must not pin the
# checker to UNKNOWN for the rest of the session).
_cached_own_rid: Optional[int] = None


def _reset_cached_own_rid() -> None:
    """Test hook: forget the cached own-process RID."""
    global _cached_own_rid
    _cached_own_rid = None


def _close_quietly(handle) -> None:
    try:
        close = getattr(handle, "Close", None)
        if callable(close):
            close()
    except Exception:
        pass


def _integrity_rid_from_token(token) -> int:
    """Read the integrity RID from an open token, closing it always.

    An integrity SID is S-1-16-<rid>; the RID is the last (only)
    sub-authority. Raises on failure -- callers translate to None /
    UNKNOWN.
    """
    try:
        sid, _attributes = win32security.GetTokenInformation(
            token, win32security.TokenIntegrityLevel,
        )
        return sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)
    finally:
        _close_quietly(token)


def _own_integrity_rid() -> Optional[int]:
    """WheelHouse's own integrity RID, cached after the first success."""
    global _cached_own_rid
    if _cached_own_rid is not None:
        return _cached_own_rid
    try:
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY,
        )
        rid = _integrity_rid_from_token(token)
    except Exception as e:
        logger.debug(
            "elevation check: cannot read own integrity level: %s", e,
        )
        return None
    _cached_own_rid = rid
    return rid


def _integrity_rid_of_pid(pid: int) -> Optional[int]:
    """Integrity RID of another process, or None on any failure."""
    try:
        process_handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
        )
    except Exception as e:
        logger.debug(
            "elevation check: OpenProcess(pid=%s) failed: %s", pid, e,
        )
        return None
    try:
        token = win32security.OpenProcessToken(
            process_handle, win32con.TOKEN_QUERY,
        )
        return _integrity_rid_from_token(token)
    except Exception as e:
        logger.debug(
            "elevation check: token read for pid=%s failed: %s", pid, e,
        )
        return None
    finally:
        _close_quietly(process_handle)


def _resolve_target_hwnd(focused_control) -> int:
    """Window handle for the focused target, 0 when none can be found.

    Tries the UIA control's top-level window first, then falls back to
    GetForegroundWindow -- UIA visibility into elevated windows is
    exactly what may be broken, so a failed control lookup must not end
    the check. No GA_ROOT normalization: UIPI filters on the process
    that owns the window receiving the input, and
    GetWindowThreadProcessId answers that for child handles too.
    """
    hwnd = 0
    if focused_control is not None:
        try:
            top = focused_control.GetTopLevelControl()
            hwnd = int(top.NativeWindowHandle) if top else 0
        except Exception:
            hwnd = 0
    if not hwnd:
        try:
            hwnd = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            hwnd = 0
    return hwnd


def elevation_state_of_hwnd(hwnd) -> str:
    """Compare a window's integrity level against our own, by handle.

    Entry point for callers that already hold an hwnd (the Window
    Positioning plugin's keyboard mover, wh-winpos-silent-failure --
    SetWindowPos against a higher-integrity window fails the same way
    typing does). Returns ELEVATED when the owning process runs at a
    strictly higher integrity level, NOT_ELEVATED at ours or lower,
    UNKNOWN on any failure (fail open).
    """
    try:
        if not hwnd:
            return UNKNOWN
        own_rid = _own_integrity_rid()
        if own_rid is None:
            return UNKNOWN
        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return UNKNOWN
        target_rid = _integrity_rid_of_pid(pid)
        if target_rid is None:
            return UNKNOWN
        if target_rid > own_rid:
            logger.debug(
                "elevation check: target pid=%s integrity 0x%x > own "
                "0x%x -> elevated", pid, target_rid, own_rid,
            )
            return ELEVATED
        return NOT_ELEVATED
    except Exception as e:
        logger.debug("elevation check failed open: %s", e)
        return UNKNOWN


def target_elevation_state(focused_control) -> str:
    """Compare the focused window's integrity level against our own.

    Returns ELEVATED when the target runs at a strictly higher
    integrity level (typing would be silently discarded by UIPI),
    NOT_ELEVATED when it runs at ours or lower, and UNKNOWN on any
    failure (fail open -- the caller proceeds unchanged).
    """
    try:
        hwnd = _resolve_target_hwnd(focused_control)
        if not hwnd:
            return UNKNOWN
        return elevation_state_of_hwnd(hwnd)
    except Exception as e:
        logger.debug("elevation check failed open: %s", e)
        return UNKNOWN
