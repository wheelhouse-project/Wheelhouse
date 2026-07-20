"""Opt-in Windows screen-reader flag for voice clicking (wh-c169t).

The voice element-clicking feature (epic wh-l4h.1) can opt in to setting the
system-wide Windows screen-reader flag via ``SystemParametersInfoW`` with the
``SPI_SETSCREENREADER`` action. Setting the flag asks UIA-aware applications to
expose a richer accessibility tree, which speeds element discovery for voice
clicking in some apps.

Why this is opt-in and torn down (the PSReadLine trade-off):
============================================================
The flag is system-wide. While it is set, PSReadLine detects it and disables
itself in EVERY PowerShell session on the machine -- the user sees
``WARNING: PowerShell detected that you might be using a screen reader and has
disabled PSReadLine for compatibility purposes`` and loses syntax highlighting,
tab completion, and Ctrl+R history search. WheelHouse's own terminal-dictation
flow targets the user's shell, so silently neutering PSReadLine is a
hands-free-usability regression. Therefore the flag defaults OFF, is set only
when the user opts in, and is cleared on graceful shutdown.

Startup self-recovery (ownership-gated -- wh-l4h.1.13):
======================================================
A crash or ungraceful exit can leave the flag set. So when the opt-in is OFF,
startup may CLEAR the flag (``uiParam=0``) as idempotent self-recovery. As of
wh-l4h.1.13 that OFF-path startup clear is GATED on an on-disk ownership marker
rather than firing unconditionally: WheelHouse clears the flag at startup ONLY
when the marker file exists (meaning WheelHouse itself set the flag and may have
crashed before clearing it). When the marker is absent the setting is left
untouched. Startup must never ENABLE the flag (``uiParam=1``) without the
opt-in. See :func:`apply_screen_reader_flag` and the reconciled-intent note in
the wh-c169t bead (which supersedes the older v5 phrasing of regression bead
wh-69sk8). This ownership gate supersedes the earlier unconditional-clear
contract that the v5 design doc and wh-9f3t.38.1 described as mandatory.

Co-existence with real screen readers (wh-9f3t.38.1 / wh-l4h.1.13):
==================================================================
This is the SAME Windows flag that NVDA / JAWS / Narrator set for their own
use. The earlier unconditional OFF-path startup clear therefore also cleared
the flag out from under a running screen reader. wh-l4h.1.13 closes that hole
with an ownership marker: WheelHouse writes ``<app_data>/screen_reader_flag_owned``
when it SETs the flag and deletes it when it clears the flag. The OFF-path
startup clear fires ONLY when that marker is present, so a flag a real screen
reader owns (no WheelHouse marker on disk) is never cleared. The marker check
fails SAFE: any error reading it is treated as 'marker absent', i.e. WheelHouse
does NOT clear, which is the less-harmful direction (it preserves a real screen
reader's flag at the cost of not self-recovering a stale WheelHouse-set flag in
the rare read-error case). A user who runs a screen reader AND voice clicking
still sets ``enable_screen_reader_flag = true`` (the opt-in this module gates
on), so WheelHouse leaves the flag SET instead of clearing it on that path too.

Residual risk and the ordering choice (wh-l4h.1.13 / wh-9f3t.85.1):
==================================================================
The marker is on-disk state that can desynchronise from the OS flag across an
ungraceful exit. Both syscall/marker orderings fail toward "not owned" so a
crash never leaves a marker that falsely claims ownership of a flag WheelHouse
did not set (which a later startup would clear, possibly out from under a real
screen reader):

* ON-path ordering. ``apply_screen_reader_flag(True)`` calls ``setter(1)`` and
  only THEN writes the marker. A crash in that window leaves the flag SET with
  no marker, so the OFF-path startup clear will not self-recover it -- the flag
  stays set until the user re-enables the opt-in (which rewrites the marker) or
  runs ``wheelhouse --clear-screen-reader-flag`` (wh-94b7e). This is the SAFE
  direction. The alternative -- write the marker first -- would, on the same
  crash, leave a marker that falsely claims ownership of a flag WheelHouse
  never set; the next opt-in-OFF startup would then issue ``setter(0)``, the
  exact call that can clear a REAL screen reader's flag. Set-first fails toward
  "do not touch a flag we are unsure we own"; marker-first fails toward "clear a
  flag we may not own". For an accessibility-critical invariant the
  conservative direction wins, even though it costs automatic recovery of
  WheelHouse's own stale flag (the CLI is the recovery for that case).
* Clear-path ordering (wh-9f3t.85.1). Both clear sites -- the OFF-path startup
  clear and :func:`clear_screen_reader_flag` -- delete the marker BEFORE calling
  ``setter(0)``, not after. Deleting first means a CRASH between the syscall and
  the delete leaves no stale marker; the worst case is WheelHouse's own flag
  staying set with no marker (the same recoverable stuck-own-flag state as the
  ON path), never a stale marker that a later startup uses to clear a real
  screen reader's flag. The cost is that a clear whose ``setter(0)`` reports
  failure is not retried at the next startup (the marker is already gone); the
  CLI is the recovery for that rare case. One residual remains (wh-9f3t.86.1):
  if :func:`_delete_marker` itself fails silently -- a ``PermissionError`` from
  an antivirus lock, say -- the marker survives while the clear still runs, so a
  stale marker results. That delete failure is bounded (a file in the user's own
  app-data dir) and is logged at ERROR so an operator can find and remove it.
* Stale marker meets a real screen reader (inherent residual). If WheelHouse
  sets the flag and marker and then exits UNGRACEFULLY -- crashing before any
  clear path runs, so the marker correctly persists -- a real screen reader then
  starts (the flag is already on, so it relies on it), and WheelHouse restarts
  with the opt-in OFF, the OFF path sees the marker, concludes ownership, and
  clears the flag -- disrupting the running screen reader. The window is narrow
  (it needs an ungraceful exit, then a screen-reader start, then a
  WheelHouse restart with the feature disabled, all before any clean shutdown
  deletes the marker) but the consequence is severe. Windows exposes no way to
  ask "did a screen reader set this flag?", so there is no fully safe automatic
  resolution. The manual escape hatch is ``wheelhouse --clear-screen-reader-flag``
  (wh-94b7e); a user whose screen reader was cleared can also just restart it.

Never raises:
=============
Every public function is best-effort. The syscall is wrapped so any failure is
logged (with ``ctypes.get_last_error()``) and reported as a ``False`` return; it
never propagates, so it can never abort Logic startup or block clean shutdown.
The real syscall is dependency-injected via a ``setter`` callable (mirroring
``utils.file_version_info``'s inject-a-callable style) so unit tests pass a fake
and assert the arguments without touching real system state.

Spec source: docs/plans/2026-05-21-voice-element-clicking-design-v5.md section
'Behaviour at shutdown' (lines ~480-489).
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# Windows SDK: SPI_SETSCREENREADER = 0x0047 (71). The GET counterpart,
# SPI_GETSCREENREADER, is 0x0046 (70) -- do NOT confuse the two; this module
# only ever SETs the flag.
SPI_SETSCREENREADER = 0x0047


def _set_screen_reader_flag_via_win32(ui_param: int) -> bool:
    """Call ``SystemParametersInfoW(SPI_SETSCREENREADER, ui_param, None, 0)``.

    ``ui_param`` is 1 to set the screen-reader flag, 0 to clear it. ``pvParam``
    is NULL and ``fWinIni`` is 0 (no settings broadcast) per the v5 spec.

    Returns True when the call reports success, False otherwise. Never raises:
    on any failure it logs a warning with the last Win32 error and returns
    False. The caller treats this as a best-effort side effect.
    """

    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001 -- ctypes unavailable; degrade silently.
        logger.warning("screen_reader_flag: ctypes unavailable; flag not changed")
        return False

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError:
        logger.warning("screen_reader_flag: could not load user32; flag not changed")
        return False

    SystemParametersInfoW = user32.SystemParametersInfoW
    # BOOL SystemParametersInfoW(UINT uiAction, UINT uiParam, PVOID pvParam, UINT fWinIni)
    SystemParametersInfoW.argtypes = [
        wintypes.UINT,
        wintypes.UINT,
        wintypes.LPVOID,
        wintypes.UINT,
    ]
    SystemParametersInfoW.restype = wintypes.BOOL

    ok = SystemParametersInfoW(SPI_SETSCREENREADER, ui_param, None, 0)
    if not ok:
        logger.warning(
            "screen_reader_flag: SystemParametersInfoW(SPI_SETSCREENREADER, %d) "
            "failed (last error %d)",
            ui_param,
            ctypes.get_last_error(),
        )
        return False
    return True


# Name of the on-disk ownership marker. Existence == WheelHouse currently has
# the screen-reader setting turned on. It lives in the app-data dir alongside
# the other machine-managed runtime state (approved-control lists, counters);
# it is NOT user config and is never written to config.toml.
_MARKER_FILENAME = "screen_reader_flag_owned"


def _default_marker_path() -> str:
    """Resolve the default ownership-marker path under the app-data dir.

    Best-effort: any failure resolving the app-data dir degrades to an empty
    string so the caller treats the marker as unresolved (absent on read,
    no-op on write/delete). Never raises.
    """

    try:
        from utils.system import get_app_data_path

        return os.path.join(get_app_data_path(), _MARKER_FILENAME)
    except Exception:  # noqa: BLE001 -- best-effort; never abort the flag op.
        logger.warning(
            "screen_reader_flag: could not resolve app-data marker path; "
            "treating ownership marker as unavailable",
            exc_info=True,
        )
        return ""


def _marker_exists(marker_path: Optional[str]) -> bool:
    """Return True iff the ownership marker exists. Fails SAFE to False.

    A missing/empty path or any error checking the path is treated as 'marker
    absent' (the less-harmful direction): the OFF-path startup clear then does
    NOT fire, so a real screen reader's flag is preserved. Never raises.
    """

    path = marker_path if marker_path is not None else _default_marker_path()
    if not path:
        return False
    try:
        return os.path.exists(path)
    except Exception:  # noqa: BLE001 -- fail safe to 'absent'.
        logger.warning(
            "screen_reader_flag: ownership-marker existence check failed; "
            "treating as absent (will not clear)",
            exc_info=True,
        )
        return False


def _write_marker(marker_path: Optional[str]) -> None:
    """Create the ownership marker. Best-effort; logs and swallows failures."""

    path = marker_path if marker_path is not None else _default_marker_path()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("owned")
    except Exception:  # noqa: BLE001 -- best-effort; never abort startup.
        logger.warning(
            "screen_reader_flag: could not write ownership marker at %s",
            path,
            exc_info=True,
        )


def _delete_marker(marker_path: Optional[str]) -> None:
    """Delete the ownership marker. Best-effort; logs and swallows failures.

    A missing file is fine (already deleted). A REAL delete failure (a
    ``PermissionError`` from an antivirus lock, a permissions problem, any
    other ``OSError``) is swallowed so it never blocks shutdown, but it is
    logged at ERROR (wh-9f3t.86.1): the caller proceeds to clear the OS flag
    regardless, so a surviving marker becomes a STALE marker (flag cleared,
    marker present) that a later opt-in-OFF startup would use to clear a flag a
    real screen reader set in the meantime. Logging at ERROR lets an operator
    find this in the logs; deleting the stale file by hand (or running
    ``wheelhouse --clear-screen-reader-flag``) clears the risk.
    """

    path = marker_path if marker_path is not None else _default_marker_path()
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        # Already absent -- nothing to do.
        return
    except Exception:  # noqa: BLE001 -- best-effort; never block shutdown.
        logger.error(
            "screen_reader_flag: could not delete ownership marker at %s; a "
            "stale marker may cause a future startup to clear a real screen "
            "reader's flag if one is running -- delete the file by hand or run "
            "wheelhouse --clear-screen-reader-flag",
            path,
            exc_info=True,
        )


# The default setter is the real ctypes implementation. Tests inject a fake.
_Setter = Callable[[int], bool]


def _invoke_setter(setter: Optional[_Setter], ui_param: int) -> bool:
    """Invoke ``setter(ui_param)``; never raises.

    Uses the real Win32 setter when ``setter`` is None. Any exception from the
    setter is swallowed (logged) and reported as False so callers stay
    best-effort.
    """

    real_setter = setter if setter is not None else _set_screen_reader_flag_via_win32
    try:
        return bool(real_setter(ui_param))
    except Exception:  # noqa: BLE001 -- best-effort; the syscall must not abort us.
        logger.warning(
            "screen_reader_flag: setter raised for uiParam=%d; treating as failure",
            ui_param,
            exc_info=True,
        )
        return False


def apply_screen_reader_flag(
    enabled: bool,
    *,
    setter: Optional[_Setter] = None,
    marker_path: Optional[str] = None,
) -> bool:
    """Apply the screen-reader flag at Logic startup; never raises.

    When ``enabled`` is True the flag is SET (``setter(1)``) because the user
    opted in; on a successful set the on-disk ownership marker is written so a
    later OFF-path startup knows WheelHouse owns the flag.

    When ``enabled`` is False the OFF-path startup clear is OWNERSHIP-GATED
    (wh-l4h.1.13): the flag is CLEARED (``setter(0)``) ONLY when the ownership
    marker is present (WheelHouse set the flag and may have crashed without
    clearing it). The marker is deleted BEFORE the clear (wh-9f3t.85.1) so a
    crash between the syscall and the delete cannot leave a stale marker. When
    the marker is ABSENT -- or its existence check fails (fail-safe to absent) -- the
    setting is LEFT UNTOUCHED and no setter call is made, so a real screen
    reader's flag is never cleared. The flag is NEVER enabled in this branch.

    This ownership gate supersedes the earlier unconditional-clear contract
    (wh-c169t / wh-69sk8 / v5 design doc): startup must never call ``setter(1)``
    without the opt-in, and it calls ``setter(0)`` only when it owns the flag.

    ``setter`` performs the real ``SystemParametersInfoW`` call; tests inject a
    fake. ``marker_path`` overrides the default app-data marker path so tests
    stay hermetic; production leaves it None. Returns True when the underlying
    call reported success, False on any failure (including the setter raising),
    and True for the marker-absent no-op OFF path (nothing to do is success).
    Best-effort: the result never aborts startup.
    """

    if enabled:
        ok = _invoke_setter(setter, 1)
        if ok:
            _write_marker(marker_path)
        return ok

    # OFF path: ownership-gated clear (wh-l4h.1.13).
    if not _marker_exists(marker_path):
        logger.debug(
            "screen_reader_flag: OFF-path startup clear skipped -- Wheelhouse "
            "does not own the screen-reader setting (no ownership marker)",
        )
        return True

    # Delete the marker BEFORE issuing the clear (wh-9f3t.85.1). Once we begin
    # clearing we drop the ownership claim, so a crash -- or a swallowed delete
    # failure -- between the syscall and the delete cannot leave a STALE marker.
    # A stale marker (flag cleared, marker still present) would let a later
    # opt-in-OFF startup call setter(0) against a flag a real screen reader set
    # in the meantime, the exact failure the marker exists to prevent. This
    # matches the set-first ON-path direction: fail toward "not owned".
    _delete_marker(marker_path)
    return _invoke_setter(setter, 0)


def clear_screen_reader_flag(
    *, setter: Optional[_Setter] = None, marker_path: Optional[str] = None,
) -> bool:
    """Clear the screen-reader flag at graceful shutdown; never raises.

    Calls ``setter(0)`` UNCONDITIONALLY -- the clear path is NOT ownership-gated
    (only the startup OFF-path in :func:`apply_screen_reader_flag` is). Used on
    the graceful-shutdown path and the ``wheelhouse --clear-screen-reader-flag``
    CLI when the flag was enabled, so PSReadLine recovers in subsequent
    PowerShell sessions. The ownership marker is deleted BEFORE the clear
    (wh-9f3t.85.1) -- not after -- so a crash between the syscall and the delete
    cannot leave a stale marker that a later startup would use to clear a flag a
    real screen reader set in the meantime. This is the same
    fail-toward-"not owned" ordering the startup OFF-path uses.

    ``setter`` performs the real ``SystemParametersInfoW`` call; tests inject a
    fake. ``marker_path`` overrides the default app-data marker path so tests
    stay hermetic. Returns True on reported success, False on any failure
    (including the setter raising). Best-effort: the result never blocks clean
    shutdown.
    """

    # Delete the marker BEFORE the clear (wh-9f3t.85.1); see the OFF-path note
    # in apply_screen_reader_flag for the fail-toward-"not owned" rationale.
    _delete_marker(marker_path)
    return _invoke_setter(setter, 0)


__all__ = [
    "SPI_SETSCREENREADER",
    "apply_screen_reader_flag",
    "clear_screen_reader_flag",
]
