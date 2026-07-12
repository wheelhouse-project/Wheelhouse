"""Numbered-overlay paint window for Phase 1.5 voice element clicking
(slice wh-n29v.53, source leaf wh-h7cvz1).

This GUI-process module manages ONE transparent, always-on-top,
no-activate, click-through layered Win32 window PER monitor that
currently has badges. It paints centered outlined numerals (drop shadow,
no background box, configurable point size) over on-screen controls via
the existing Qt-to-GDI per-pixel-alpha bridge
(``shared/overlay_bitmap.py``), generation-gates its own painting, and
returns an ``overlay_state_changed`` wire dict for the GUI to forward
back to Logic.

What this module COMPOSES (it does not reimplement any of these):

* ``shared/overlay_dpi_resolver.py::resolve_overlay_paint_rect`` -- maps a
  control's virtual-desktop physical bounds to Qt-logical paint
  coordinates local to the resolved monitor's overlay window. The
  resolved ``hmonitor`` selects the overlay window.
* ``shared/monitor_geometry.py::_enumerate_native_monitors`` -- the Win32
  monitor topology, enumerated ONCE per render.
* ``shared/overlay_bitmap.py::build_layered_dib`` /
  ``composite_layered_window`` -- the QImage -> top-down 32-bit DIB
  bridge and the ``UpdateLayeredWindow`` / ``ULW_ALPHA`` composite. WM_PAINT
  is NOT used: per-pixel alpha is pushed via ``UpdateLayeredWindow``.
* ``shared/paint_overlay.py`` / ``shared/clear_overlay.py`` /
  ``shared/overlay_state_changed.py`` -- the IPC schemas (the GUI dispatch
  parses the inbound dicts; this manager consumes the parsed events and
  produces the ``overlay_state_changed`` reply dict).

Threading / message-pump model
-------------------------------

``paint_overlay`` / ``clear_overlay`` arrive on the GUI process's main
(Qt) thread via the ``state_to_gui_queue`` drain in ``gui.py``. This
manager therefore creates and destroys its per-monitor windows on that
SAME GUI/main thread and relies on Qt's native Windows event dispatcher
(the Qt event loop already running in the GUI process) to pump the
window messages these windows receive -- in practice only
``WM_NCHITTEST`` and ``WM_MOUSEACTIVATE``, which the window proc answers
with ``HTTRANSPARENT`` and ``MA_NOACTIVATE`` to stay click-through and
no-activate. There is deliberately NO separate message-loop thread (the
``handlers/software_dimmer.py`` precedent runs its own pump thread only
because it owns a dedicated full-virtual-desktop window outside Qt; this
overlay lives inside the Qt-owned message loop). Because the surface is
painted with ``UpdateLayeredWindow`` (per-pixel alpha) rather than
``WM_PAINT``, the window needs no paint-message handling at all, and the
Qt dispatcher pumping the click-through / no-activate replies is
sufficient.

This differs from ``software_dimmer`` in two further ways the slice spec
calls out: this feature makes one window PER ``_NativeMonitor`` that has
badges, sized to the BOUNDING BOX of that monitor's badges plus an
outline/shadow margin (NOT the monitor's full ``rect_phys``, and NOT one
window spanning the whole virtual desktop via
``GetSystemMetrics SM_*VIRTUALSCREEN``), and it paints per-pixel alpha
through ``UpdateLayeredWindow`` / ``ULW_ALPHA`` (NOT
``SetLayeredWindowAttributes``' whole-surface alpha). The bounding-box
sizing (wh-n29v.56.1) keeps the transient QImage + GDI DIB proportional to
badge count rather than monitor resolution, so a 4K/8K monitor no longer
allocates a full-resolution surface per paint; on-screen badge placement is
unchanged at every DPR because the bounding-box offset cancels between the
in-surface paint position and the composite destination origin.

Generation gating
------------------

The manager tracks the highest ``(overlay_session_id, paint_generation)``
pair it has been commanded to paint OR clear (a high-water mark, compared
lexicographically). A ``paint`` whose pair is STRICTLY OLDER than the
mark is ignored (no window churn, returns ``None``). A paint or clear at
a pair ``>=`` the mark advances it. A ``clear`` advances the mark so a
late stale paint at the prior generation cannot present (review finding
wh-n29v.15.1).
"""

from __future__ import annotations

import ctypes
import logging
import math
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QScreen,
)

from shared.monitor_geometry import _NativeMonitor, _enumerate_native_monitors
from shared.overlay_bitmap import (
    LayeredDib,
    build_layered_dib,
    composite_layered_window,
)
from shared.overlay_dpi_resolver import (
    OverlayPaintRect,
    resolve_overlay_paint_rect,
)
from shared.overlay_state_changed import OverlayStateChangedEvent


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Win32 constants (mirrors handlers/software_dimmer.py; 64-bit-safe types).
# ---------------------------------------------------------------------------

LRESULT = ctypes.c_ssize_t

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

WM_NCHITTEST = 0x0084
WM_MOUSEACTIVATE = 0x0021
HTTRANSPARENT = -1
MA_NOACTIVATE = 3

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
SW_SHOWNOACTIVATE = 4

# The composite ex-style: layered (UpdateLayeredWindow), always-on-top,
# click-through, no-activate, no taskbar button.
_OVERLAY_EX_STYLE = (
    WS_EX_LAYERED
    | WS_EX_TOPMOST
    | WS_EX_TRANSPARENT
    | WS_EX_NOACTIVATE
    | WS_EX_TOOLWINDOW
)

# Window-proc signature (64-bit-safe LRESULT = c_ssize_t).
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


def _wnd_proc_py(hwnd: Any, msg: Any, wparam: Any, lparam: Any) -> Any:
    """Click-through / no-activate window proc (module scope).

    Returns ``HTTRANSPARENT`` for ``WM_NCHITTEST`` (so clicks pass through to
    the window beneath) and ``MA_NOACTIVATE`` for ``WM_MOUSEACTIVATE`` (so
    the overlay never steals activation). Everything else delegates to
    ``DefWindowProcW``. No ``WM_PAINT`` handling: the surface is pushed via
    ``UpdateLayeredWindow``.

    Lives at MODULE scope, not on the manager (wh-overlay-shared-wndproc):
    the registered window class is process-global, and gui.py constructs TWO
    managers (numbered click overlay + dictation working badge) that share
    it. A manager-bound callback would leave the class pointing at whichever
    manager registered first; if that manager were ever destroyed, every
    still-alive window of the OTHER manager would dispatch into a freed
    ctypes thunk. ``DefWindowProcW`` is resolved at CALL time through the
    module-global ``ctypes`` name -- it inherits the restype/argtypes that
    ``_setup_prototypes`` declares on the process-global function pointer
    before any window can exist, and the per-test patch of
    ``overlay_paint_window.ctypes`` keeps working.
    """
    msg_int = int(getattr(msg, "value", msg))
    if msg_int == WM_NCHITTEST:
        return HTTRANSPARENT
    if msg_int == WM_MOUSEACTIVATE:
        return MA_NOACTIVATE
    return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# The process-lifetime callback thunk. Created eagerly at import (WNDPROC is
# built with real ctypes and instantiation makes no DLL calls) and retained
# for the life of the process because the registered window class points at
# it -- no manager GC can ever free it (wh-overlay-shared-wndproc).
_PROCESS_WND_PROC = WNDPROC(_wnd_proc_py)

# Badge colors (no config keys for color exist): white numeral, black
# outline + drop shadow, no background box.
_NUMERAL_COLOR = QColor(255, 255, 255, 255)
_OUTLINE_COLOR = QColor(0, 0, 0, 255)
_SHADOW_COLOR = QColor(0, 0, 0, 160)
# Outline pen width and shadow offset in logical pixels.
_OUTLINE_PX = 3
# The numeral needs a MUCH thinner outline than the hourglass. The outline is
# stroked centered on the glyph path edge, so for a small numeral a 3px pen
# (half of it eating inward from each edge) swallows the white fill and the
# digit reads as a solid black blob. The hourglass is a large bold shape that
# tolerates 3px; a 16pt numeral does not. 1.25 leaves the digit clearly white
# with a thin border at every scale (wh-dictation-retraction-indicator.11).
_NUMERAL_OUTLINE_PX = 1.25
_SHADOW_OFFSET_PX = 2
# Transparent breathing room (in LOGICAL pixels) added around the badge
# bounding box on every side when sizing the per-monitor paint surface, so
# anti-aliased outline/shadow edges at the bounding box's perimeter are never
# clipped. Derived from the badge decoration extents (outline pen + drop
# shadow). This is padding only -- it does NOT move any badge, because the
# bounding-box offset cancels exactly between the in-surface paint position
# and the composite destination origin (see _do_paint / _render_monitor_surface).
_SURFACE_MARGIN_PX = _OUTLINE_PX + _SHADOW_OFFSET_PX

# Which corner of a control the numeral badge anchors to. The digit sits at that
# corner and leaves the rest of the control visible. The default is the
# TOP-RIGHT corner: a Windows list row, tree item, or menu entry keeps its icon
# and label text at the LEFT, so a top-LEFT badge covered exactly the icon and
# the first letters, which is what a user reads to identify the row
# (wh-overlay-badge-occludes-label). The right side of such a row is blank or
# holds low-value columns (a file's size/date), so a top-right badge clears the
# icon and label. The value is the validated [click] setting
# overlay_badge_corner; an unknown value falls back to _DEFAULT_BADGE_CORNER.
_BADGE_CORNER_TOP_LEFT = "top_left"
_BADGE_CORNER_TOP_RIGHT = "top_right"
_BADGE_CORNER_BOTTOM_LEFT = "bottom_left"
_BADGE_CORNER_BOTTOM_RIGHT = "bottom_right"
_VALID_BADGE_CORNERS = frozenset(
    {
        _BADGE_CORNER_TOP_LEFT,
        _BADGE_CORNER_TOP_RIGHT,
        _BADGE_CORNER_BOTTOM_LEFT,
        _BADGE_CORNER_BOTTOM_RIGHT,
    }
)
_DEFAULT_BADGE_CORNER = _BADGE_CORNER_TOP_RIGHT

# A control smaller than this many badge widths AND heights is "small": the
# corner anchor centers the badge on the corner POINT (half outside on both
# axes) instead of tucking it fully inside, so only about a quarter of the
# badge covers the control (wh-overlay-small-control-cover). Packed toolbar
# icon buttons are the motivating case: their trailing strips are occupied by
# the next button, and a fully-inside badge covered most of the icon. The rule
# deliberately requires BOTH dimensions small: on a wide-but-short control (a
# list row, a column header) a half-above badge would sit visually between two
# stacked rows and read as ambiguous, so those keep the inside corner.
_BADGE_SMALL_CONTROL_FACTOR = 2.0
# Gap (LOGICAL pixels, scaled by dpr) between a badge and the already-placed
# badge it was nudged away from (wh-overlay-badge-collision). Two digits drawn
# flush against each other read as one number ("3" beside "4" reads "34"), so
# collision nudges keep this much clear space between badges.
_BADGE_COLLISION_GAP_PX = 3.0

# Whether to place the numeral in the empty space just BEYOND the control's
# trailing edge (the corner's horizontal side) instead of inside its corner,
# when that strip is clear of other walked controls and stays on the monitor
# (wh-overlay-badge-occludes-label follow-up). A left-aligned vertical list --
# the File Explorer navigation tree, a Details-view file list, a menu -- has
# blank space to the right of every item after its icon+label, so a
# corner-anchored badge still landed on the label (a nav item whose box hugs a
# short folder name) or on a trailing value (a file's size). Placing the badge
# just past the control clears both. When the strip is occupied (a grid tile or
# a packed toolbar button has a neighbour immediately to its right) or the badge
# would run off-screen, it falls back to the corner, so those layouts are
# unchanged. False restores pure corner placement. The value is the validated
# [click] setting overlay_badge_trailing_space.
_DEFAULT_BADGE_TRAILING_SPACE = True

# Working/busy badge (wh-dictation-retraction-indicator.2): a single static
# glyph painted at an arbitrary screen point to signal that dictated text is
# still provisional (could be retracted by the final). It rides the SAME paint
# pipeline as the numbered badges; this sentinel display number routes the
# badge render to the working glyph instead of a numeral. -1 can never collide
# with a real 1-based overlay number.
WORKING_BADGE_NUMBER = -1
# Perceived on-screen size of the working badge, in LOGICAL (device-
# independent) pixels. paint_working_badge multiplies this by the target
# monitor's device pixel ratio to build the physical box centered on the
# point, so the badge keeps the SAME perceived size across mixed-DPI monitors
# instead of shrinking on hi-DPI displays. On a 200% monitor it is rendered at
# 128 physical px, which looks the same size as 64 px at 100%. The first live
# check at 36 read as a shrunken blob, so the default is larger
# (wh-dictation-retraction-indicator.11).
WORKING_BADGE_LOGICAL_PX = 64


@dataclass(frozen=True)
class _PointBadgeItem:
    """One synthetic badge for ``paint_working_badge``.

    The minimal shape the paint pipeline reads from a summary item:
    ``bounds`` (the same ``(x, y, width, height)`` convention as
    ``WalkSnapshotSummaryItem.bounds`` -- virtual-desktop physical pixels;
    ``_do_paint`` converts it to the resolver's ``(left, top, right, bottom)``
    at its single call site, so this MUST NOT be passed as ``(l, t, r, b)`` or
    it is double-converted), an advisory ``monitor_id`` (the resolver ignores
    it), and ``display_number`` (the working sentinel). It deliberately does
    NOT reuse ``WalkSnapshotSummaryItem`` -- a working badge is not a walk
    match and has no item_id / name / role / score.
    """

    bounds: tuple[int, int, int, int]
    monitor_id: int
    display_number: int


@dataclass(frozen=True)
class _PointBadgeSummary:
    """A summary-shaped wrapper for ``paint_working_badge``.

    Mirrors the duck-typed surface ``paint`` reads: ``.items`` and an
    optional ``.snapshot_id`` (absent -> ``None`` via ``getattr``).
    """

    items: list[_PointBadgeItem]
    snapshot_id: Optional[str] = None


class WNDCLASSEXW(ctypes.Structure):
    """``WNDCLASSEXW`` from ``winuser.h`` (mirrors software_dimmer)."""

    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        (
            "lpfnWndProc",
            ctypes.WINFUNCTYPE(
                LRESULT,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ),
        ),
        ("cbClsExtra", wintypes.INT),
        ("cbWndExtra", wintypes.INT),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HANDLE),
    ]


# ---------------------------------------------------------------------------
# Per-monitor badge bounding box (monitor-local PHYSICAL pixels).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _MonitorBBox:
    """The bounding box of all badges on one monitor, in that monitor's
    LOCAL PHYSICAL pixels (origin (0, 0) at the monitor's physical top-left),
    expanded by ``_SURFACE_MARGIN_PX`` * dpr on every side.

    ``offset_x`` / ``offset_y`` are the bounding box's top-left in
    monitor-local physical pixels (MAY be negative when a control sits partly
    off the monitor's left/top edge). ``width`` / ``height`` are the surface
    dimensions in physical pixels (always >= 1). The overlay window's SCREEN
    origin is ``(monitor.rect_phys.left() + offset_x,
    monitor.rect_phys.top() + offset_y)``; that screen rectangle is the window
    geometry AND the ``UpdateLayeredWindow`` composite destination origin.
    """

    offset_x: int
    offset_y: int
    width: int
    height: int


# ---------------------------------------------------------------------------
# QScreen enumeration seam (so tests can patch screens without a display).
# ---------------------------------------------------------------------------


def _screens() -> list[QScreen]:
    """Return ``QGuiApplication.screens()`` (patched in tests).

    Wrapped in a module-level function so the per-render snapshot of the
    Qt screen list goes through one seam the tests can override, mirroring
    the ``_enumerate_native_monitors`` seam.
    """
    return list(QGuiApplication.screens())


# ---------------------------------------------------------------------------
# Generation gate (high-water mark).
# ---------------------------------------------------------------------------


class GenerationGate:
    """Lexicographic high-water mark on ``(overlay_session_id, paint_generation)``.

    A paint whose pair is STRICTLY OLDER than the mark is ignored. A paint
    or clear at a pair ``>=`` the mark advances the mark. A clear advances
    the mark so a later stale paint at the prior generation is ignored
    (review finding wh-n29v.15.1).
    """

    def __init__(self) -> None:
        # Start below any real (session, generation) pair. Sessions and
        # generations are non-negative monotonic ints, so (-1, -1) is a
        # safe floor that the first real paint always exceeds.
        self._mark: tuple[int, int] = (-1, -1)
        # The highest pair a CLEAR has been applied at. A clear at pair P
        # blocks any subsequent paint at pair <= P (not just < P): once the
        # overlay for generation G is explicitly cleared, a late paint that
        # also carries generation G is stale and must not re-present. A
        # re-paint at the SAME pair after a plain paint (no intervening
        # clear) is still allowed, which is why this is tracked separately
        # from the plain high-water mark.
        self._cleared_at: tuple[int, int] = (-1, -1)

    def accept_paint(self, overlay_session_id: int, paint_generation: int) -> bool:
        """True if this paint may present; advances the mark when accepted.

        Returns False (no-op) when the pair is strictly older than the
        current high-water mark, OR when the pair is ``<=`` the pair a
        clear was last applied at (a clear blocks a same-or-older-generation
        paint that arrives late).
        """
        pair = (overlay_session_id, paint_generation)
        if pair < self._mark or pair <= self._cleared_at:
            return False
        self._mark = pair
        return True

    def accept_clear(self, overlay_session_id: int, paint_generation: int) -> bool:
        """Advance the mark for a clear at ``pair >= mark``.

        A clear always advances the high-water mark when its pair is ``>=``
        the current mark, and records the cleared pair so a stale paint at
        the same (or prior) generation cannot present afterwards. Returns
        False only when the clear itself is strictly older than the mark; in
        that case the mark is not moved backwards, no forward clear-block is
        recorded, and the caller (``OverlayPaintWindowManager.clear``) must
        NOT tear down windows -- a newer paint/clear already advanced the
        mark, so honoring a stale clear would destroy a newer overlay that
        must stay on screen (wh-n29v.55.1).
        """
        pair = (overlay_session_id, paint_generation)
        if pair < self._mark:
            return False
        self._mark = pair
        if pair > self._cleared_at:
            self._cleared_at = pair
        return True


# ---------------------------------------------------------------------------
# One per-monitor overlay window.
# ---------------------------------------------------------------------------


class _OverlayWindow:
    """One layered click-through window bounding the badges on a single
    monitor.

    Created on the GUI/main thread; relies on Qt's Windows event
    dispatcher to pump its (minimal) window messages. Painted via
    ``UpdateLayeredWindow`` (per-pixel alpha), so it never receives a
    ``WM_PAINT`` it must answer.

    The window is sized to ``geom_phys`` -- the badge BOUNDING BOX (plus
    margin) in SCREEN physical pixels, NOT the monitor's full physical
    resolution (wh-n29v.56.1). ``geom_phys`` is therefore also the reuse key:
    a later render whose bounding box differs rebuilds the window so a stale,
    differently-sized window/DIB is never reused.
    """

    def __init__(
        self,
        hmonitor: int,
        geom_phys: QRect,
        class_name: str,
        user32: Any,
        kernel32: Any,
        h_instance: Any,
    ) -> None:
        self.hmonitor = hmonitor
        self.geom_phys = geom_phys
        self._user32 = user32
        self._kernel32 = kernel32
        self.hwnd: Optional[wintypes.HWND] = None

        # Raw Python ints are passed for the style and geometry args; the
        # manager declares CreateWindowExW's argtypes once (see
        # _setup_prototypes) so ctypes coerces them to the 64-bit-safe C
        # signature. WS_POPUP (0x80000000) and the layered ex-style are
        # passed as their raw int values. The geometry is the badge bounding
        # box in SCREEN physical pixels (origin + size), not the full monitor.
        hwnd_raw = user32.CreateWindowExW(
            _OVERLAY_EX_STYLE,
            class_name,
            class_name,
            WS_POPUP,
            geom_phys.left(),
            geom_phys.top(),
            geom_phys.width(),
            geom_phys.height(),
            None,
            None,
            h_instance,
            None,
        )
        self.hwnd = _coerce_hwnd(hwnd_raw)
        if self.hwnd is None or not _hwnd_value(self.hwnd):
            err = kernel32.GetLastError()
            raise OSError(
                f"CreateWindowExW failed for monitor {hmonitor}, err={err}"
            )
        # Show without stealing focus.
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def composite(self, dib: LayeredDib, dest_x: int, dest_y: int) -> bool:
        """Push this monitor's badge surface onto the window via
        UpdateLayeredWindow.

        Acquires a screen DC (``GetDC(0)``) for the source-over composite
        and always releases it. ``dest_x`` / ``dest_y`` are this window's
        screen origin = the badge bounding box's top-left in screen physical
        pixels (``geom_phys.left()`` / ``geom_phys.top()``).
        """
        screen_dc = self._user32.GetDC(None)
        try:
            return composite_layered_window(
                _hwnd_value(self.hwnd),
                _dc_value(screen_dc),
                dib,
                dest_x,
                dest_y,
            )
        finally:
            self._user32.ReleaseDC(None, screen_dc)

    def destroy(self) -> bool:
        """Destroy the window (idempotent). Returns True when the window is
        gone (or was already gone), False when ``DestroyWindow`` failed and
        the window is still on screen.

        On a falsy ``DestroyWindow`` return the window is still visible
        (always-on-top, click-through), so the handle is KEPT (not cleared)
        and ``GetLastError`` is logged at warning level naming the monitor,
        so a later teardown can retry. The broad-except path also keeps the
        handle, because the window may have survived a raising call. A
        destroy on an already-None / zero hwnd is a no-op that returns True
        and leaves ``hwnd`` None.
        """
        if self.hwnd is None or not _hwnd_value(self.hwnd):
            self.hwnd = None
            return True
        try:
            ok = self._user32.DestroyWindow(self.hwnd)
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.warning(
                "overlay_paint_window: DestroyWindow raised for monitor %s; "
                "retaining handle for retry",
                self.hmonitor,
                exc_info=True,
            )
            return False
        if not ok:
            err = self._kernel32.GetLastError()
            logger.warning(
                "overlay_paint_window: DestroyWindow returned 0 for monitor "
                "%s (err=%s); retaining handle for retry",
                self.hmonitor,
                err,
            )
            return False
        self.hwnd = None
        return True


# ---------------------------------------------------------------------------
# ctypes handle coercion helpers (handle mocks return wintypes.HWND or int).
# ---------------------------------------------------------------------------


def _coerce_hwnd(raw: Any) -> Optional[wintypes.HWND]:
    if isinstance(raw, wintypes.HWND):
        return raw
    if isinstance(raw, int):
        return wintypes.HWND(raw)
    return None


def _hwnd_value(hwnd: Any) -> int:
    if hwnd is None:
        return 0
    value = getattr(hwnd, "value", hwnd)
    return int(value) if value else 0


def _dc_value(dc: Any) -> int:
    value = getattr(dc, "value", dc)
    return int(value) if value else 0


# ---------------------------------------------------------------------------
# The manager.
# ---------------------------------------------------------------------------


class OverlayPaintWindowManager:
    """Owns the per-monitor overlay windows and the paint/clear lifecycle.

    Construction registers the window class lazily on first paint (it
    needs the module handle / DLLs, which the test harness patches via
    ``overlay_paint_window.ctypes``). ``paint`` and ``clear`` are driven
    from the GUI/main thread by ``gui.py``'s state-queue dispatch and each
    return an ``overlay_state_changed`` wire dict (or ``None`` for a
    stale-gated paint) for the GUI to forward to Logic.
    """

    _CLASS_NAME = "WheelHouseOverlayPaintWindow_v1"

    def __init__(
        self,
        badge_font_pt: int = 16,
        badge_shadow: bool = True,
        badge_corner: str = _DEFAULT_BADGE_CORNER,
        badge_trailing_space: bool = _DEFAULT_BADGE_TRAILING_SPACE,
    ) -> None:
        self._badge_font_pt = badge_font_pt
        self._badge_shadow = badge_shadow
        # The corner the numeral anchors to. ClickConfig already validates the
        # config value, but normalize defensively so an unexpected string can
        # never place a badge off-corner -- it falls back to the default.
        self._badge_corner = (
            badge_corner if badge_corner in _VALID_BADGE_CORNERS
            else _DEFAULT_BADGE_CORNER
        )
        # Whether to prefer the empty space just past the control's trailing edge
        # over the inside corner (see _DEFAULT_BADGE_TRAILING_SPACE). Coerced to a
        # plain bool so a truthy non-bool can never leak into the placement test.
        self._badge_trailing_space = bool(badge_trailing_space)

        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._gdi32 = ctypes.windll.gdi32

        self._setup_prototypes()

        # The WNDPROC thunk is _PROCESS_WND_PROC at module scope -- shared by
        # every manager instance and retained for the process lifetime, so
        # the registered class can never point at a freed callback
        # (wh-overlay-shared-wndproc).
        self._class_registered = False

        self._gate = GenerationGate()
        # hmonitor -> _OverlayWindow for the monitors currently painted.
        self._windows: dict[int, _OverlayWindow] = {}
        # Windows whose destroy() failed on the REBUILD path (monitor
        # moved/resized): the new window claims the hmonitor slot in
        # _windows, so the still-on-screen old window cannot be retained
        # there. It is parked here and retried by _destroy_all (called by
        # clear / clear_all / paint's except-path) so it is not orphaned
        # (wh-n29v.55.4 rebuild-path gap).
        self._pending_destroy: list[_OverlayWindow] = []

    # -- ctypes prototypes --------------------------------------------------

    def _setup_prototypes(self) -> None:
        """Declare 64-bit-safe argtypes / restype for the Win32 calls used.

        Pointer-sized handle types (HWND / HINSTANCE / HMENU / HDC /
        HMODULE) and the DWORD style words MUST be declared so ctypes does
        not default them to a 32-bit ``c_int`` and truncate a 64-bit handle
        (the project's 64-bit-safety rule, mirrored from
        ``handlers/software_dimmer.py`` and ``shared/monitor_geometry.py``).

        Setting an ``argtypes`` / ``restype`` attribute on the MagicMock the
        tests inject is harmless (it just records the attribute), so this
        runs unconditionally.

        Every ``argtypes`` set here uses only SHARED ``wintypes`` / ``c_int``
        types, NOT this module's local ``WNDCLASSEXW`` -- because these
        user32/kernel32 function pointers are process-global singletons that
        ``handlers/software_dimmer.py`` ALSO configures, with the SAME shared
        types. Declaring identical shared-type signatures is idempotent and
        cannot collide between the two modules. ``RegisterClassExW`` is the
        one call whose argument is module-local (``WNDCLASSEXW``), so its
        ``argtypes`` is deliberately NOT declared here (it would fight
        software_dimmer's distinct-but-same-named ``WNDCLASSEXW`` over the
        shared function pointer). ``RegisterClassExW`` is instead called with
        ``ctypes.byref(...)`` directly, which ctypes accepts without argtypes;
        only its scalar ``restype`` (``ATOM``) -- safe to share -- is set.
        Because the function pointer is process-global, another module
        (``software_dimmer``) may have set a CONFLICTING
        ``argtypes = [POINTER(its-own-WNDCLASSEXW)]`` on it; declaring nothing
        here is not enough, so ``_ensure_class_registered`` explicitly resets
        ``RegisterClassExW.argtypes = None`` at call time, immediately before
        the ``byref`` call, to defend against that cross-module collision.
        """
        u = self._user32
        k = self._kernel32
        u.DefWindowProcW.restype = LRESULT
        u.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        u.RegisterClassExW.restype = wintypes.ATOM
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        u.CreateWindowExW.restype = wintypes.HWND
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.DestroyWindow.restype = wintypes.BOOL
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetDC.argtypes = [wintypes.HWND]
        u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        u.ReleaseDC.restype = ctypes.c_int
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k.GetModuleHandleW.restype = wintypes.HMODULE
        k.GetLastError.restype = wintypes.DWORD

    # -- window class registration ------------------------------------------

    def _ensure_class_registered(self) -> Any:
        """Register the window class once; return the module handle."""
        h_instance = self._kernel32.GetModuleHandleW(None)
        if self._class_registered:
            return h_instance
        wnd_class = WNDCLASSEXW()
        wnd_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wnd_class.style = CS_HREDRAW | CS_VREDRAW
        wnd_class.lpfnWndProc = _PROCESS_WND_PROC
        wnd_class.hInstance = h_instance
        # No hCursor / hbrBackground: the window is click-through
        # (WM_NCHITTEST -> HTTRANSPARENT, so it never owns the cursor) and
        # painted entirely via UpdateLayeredWindow (no WM_ERASEBKGND /
        # background brush). Leaving both NULL is correct and avoids a
        # LoadCursorW call whose HANDLE return cannot be assigned through a
        # mocked user32 in tests.
        wnd_class.hbrBackground = None
        wnd_class.lpszClassName = self._CLASS_NAME
        # user32.RegisterClassExW is a process-global function pointer.
        # handlers/software_dimmer.py sets its argtypes to
        # [POINTER(software_dimmer.WNDCLASSEXW)] -- a DISTINCT-but-same-named
        # Structure. If that module ran first, ctypes would reject this
        # module's byref(WNDCLASSEXW) with an ArgumentError (type mismatch),
        # permanently breaking the overlay. Clear argtypes to None so ctypes
        # skips the cross-module type check and accepts the byref pointer.
        # (ctypes documents argtypes=None as the way to clear a prior
        # signature; the typeshed stub omits None, hence the ignore.)
        self._user32.RegisterClassExW.argtypes = None  # type: ignore[assignment]
        atom = self._user32.RegisterClassExW(ctypes.byref(wnd_class))
        if not atom:
            err = self._kernel32.GetLastError()
            # 1410 == ERROR_CLASS_ALREADY_EXISTS -- benign (a prior manager
            # instance in the same process registered it). Any OTHER error
            # means the class is NOT registered: leave _class_registered False
            # and return so the next paint RETRIES registration. Latching the
            # flag True on a genuine failure would make one transient failure
            # (low memory / OS resource exhaustion) permanent for this
            # manager's lifetime -- every later CreateWindowExW would fail and
            # the overlay would be silently dead (wh-overlay-class-register-retry).
            if err != 1410:
                logger.error(
                    "overlay_paint_window: RegisterClassExW failed, err=%s",
                    err,
                )
                return h_instance
        self._class_registered = True
        return h_instance

    # -- window proc: module-scope _wnd_proc_py / _PROCESS_WND_PROC ----------

    # -- badge render -------------------------------------------------------

    def _numeral_font(self, dpr: float) -> QFont:
        """The bold numeral font at ``self._badge_font_pt`` scaled by ``dpr``.
        Single source of truth shared by ``_render_badge`` (which draws the
        glyph) and ``_numeral_badge_size`` (which sizes the tight image)."""
        font = QFont()
        font.setPointSizeF(self._badge_font_pt * dpr)
        font.setBold(True)
        return font

    def _numeral_badge_size(
        self,
        number: int,
        dpr: float,
        metrics: Optional[QFontMetricsF] = None,
    ) -> tuple[int, int]:
        """Tight physical-pixel size for a numeral badge image: the glyph
        advance and cap height plus a per-side margin for the outline pen, the
        drop shadow, and antialiasing slack. The image is sized to the NUMERAL,
        not to the control it labels, so a number over a large control no longer
        allocates a per-badge control-sized (dpr^2) transient QImage
        (wh-overlay-badge-alloc-decouple).

        ``metrics`` lets the caller pass a ``QFontMetricsF`` built once for the
        whole surface (the font is fixed per surface because ``dpr`` is), so a
        dense "show numbers" paint does not rebuild the metrics per badge
        (wh-overlay-4bug-review.2). When omitted it is built from
        ``_numeral_font(dpr)`` so the standalone call stays self-contained."""
        if metrics is None:
            metrics = QFontMetricsF(self._numeral_font(dpr))
        text = str(number)
        # Match _render_badge's own centering metrics (horizontalAdvance for the
        # width, capHeight for the vertical block) so the glyph fits the box.
        text_w = metrics.horizontalAdvance(text)
        cap_h = metrics.capHeight()
        margin = _NUMERAL_OUTLINE_PX * dpr + _SHADOW_OFFSET_PX * dpr + 2.0 * dpr
        w = int(math.ceil(text_w + 2.0 * margin))
        h = int(math.ceil(cap_h + 2.0 * margin))
        return max(1, w), max(1, h)

    def _render_badge(
        self, number: int, width: int, height: int, dpr: float = 1.0
    ) -> QImage:
        """Render one centered, outlined numeral badge as a premultiplied
        ARGB32 ``QImage`` sized ``width`` x ``height`` (PHYSICAL pixels).

        White fill, black outline, optional black drop shadow, and NO
        background box (the image is transparent except the numeral and
        its outline/shadow). Point size comes from ``overlay_badge_font_pt``;
        the shadow is drawn only when ``overlay_badge_shadow`` is set.

        ``dpr`` is the monitor's scaling factor. The image is sized in
        physical pixels (``_render_monitor_surface`` passes ``logical * dpr``)
        and the font point size, outline width, and shadow offset are scaled
        by ``dpr`` too, so the perceived size and thickness are unchanged but
        the glyph is rendered at full screen resolution instead of being drawn
        small and enlarged (which softens every edge on a scaled display).
        At ``dpr == 1.0`` the geometry (glyph position and size) is
        pixel-identical to the pre-scaling render. The numeral itself is NOT
        byte-identical at dpr 1.0: the same change reduced the numeral outline
        pen from 3px to ``_NUMERAL_OUTLINE_PX`` (1.25px), so the outline is
        thinner at every scale, including 100% (wh-dictation-retraction-indicator.11).

        The ``WORKING_BADGE_NUMBER`` sentinel routes to
        ``_render_working_glyph`` (the busy/working glyph) instead of a
        numeral (wh-dictation-retraction-indicator.2).
        """
        if number == WORKING_BADGE_NUMBER:
            return self._render_working_glyph(width, height, dpr)
        w = max(1, int(width))
        h = max(1, int(height))
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)  # fully transparent: no background box

        text = str(number)
        painter = QPainter(img)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

            font = self._numeral_font(dpr)
            painter.setFont(font)

            # Build a vector glyph path so we can stroke an outline and
            # fill the interior independently (a plain drawText cannot
            # outline). Center it in the badge rect.
            path = QPainterPath()
            metrics = painter.fontMetrics()
            text_width = metrics.horizontalAdvance(text)
            # baseline y: vertically center the cap-height block.
            baseline_x = (w - text_width) / 2.0
            baseline_y = (h + metrics.capHeight()) / 2.0
            path.addText(baseline_x, baseline_y, font, text)

            if self._badge_shadow:
                shadow = QPainterPath(path)
                shadow_off = _SHADOW_OFFSET_PX * dpr
                shadow.translate(shadow_off, shadow_off)
                painter.fillPath(shadow, _SHADOW_COLOR)

            # Outline stroke under the white fill. Use the thin numeral pen,
            # NOT the hourglass _OUTLINE_PX -- a heavy pen swallows the glyph
            # (wh-dictation-retraction-indicator.11).
            outline_pen = QPen(_OUTLINE_COLOR)
            outline_pen.setWidthF(_NUMERAL_OUTLINE_PX * dpr)
            outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(outline_pen)
            painter.setBrush(_NUMERAL_COLOR)
            painter.drawPath(path)
        finally:
            painter.end()
        return img

    def _render_working_glyph(
        self, width: int, height: int, dpr: float = 1.0
    ) -> QImage:
        """Render the working/busy glyph (a static hourglass) as a
        premultiplied ARGB32 ``QImage`` sized ``width`` x ``height`` (physical
        pixels: ``_render_monitor_surface`` passes ``logical * dpr``, and the
        working badge box is ``WORKING_BADGE_LOGICAL_PX * dpr`` physical px, so
        the perceived size is constant across mixed-DPI monitors).

        Same visual treatment as the numeral badge -- white fill, black
        outline, optional drop shadow, transparent background (no box) -- but
        a shape rather than a numeral, so it reads as "busy / still settling"
        instead of a count. v1 does NOT animate, so the shape must read as
        "wait" while frozen; an hourglass silhouette (two end-cap bars and two
        funnels meeting at the center) is the universally recognized static
        busy glyph (wh-dictation-retraction-indicator.2).

        ``dpr`` is the monitor's scaling factor. The hourglass GEOMETRY (insets,
        cap bars, funnels) is proportional to ``width``/``height`` and already
        scales with the physical image size, but the outline pen width and the
        shadow offset are absolute, so they are multiplied by ``dpr`` -- exactly
        as the numeral path does -- to keep the perceived stroke thickness
        constant at every scale (wh-glm52-proving-round.1). At ``dpr == 1.0`` the
        render is unchanged.
        """
        w = max(1, int(width))
        h = max(1, int(height))
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)  # fully transparent: no background box

        # Inset on every side so the outline/shadow are never clipped at the
        # image edge (and a corner pixel stays transparent -- this is a shape,
        # not a filled box).
        inset = max(2.0, min(w, h) * 0.18)
        left = inset
        right = w - inset
        top = inset
        bottom = h - inset
        cx = w / 2.0
        cy = h / 2.0
        # End-cap bar thickness; what turns a bare bowtie into an hourglass.
        cap_h = max(1.5, (bottom - top) * 0.14)

        path = QPainterPath()
        # Top and bottom cap bars.
        path.addRect(left, top, right - left, cap_h)
        path.addRect(left, bottom - cap_h, right - left, cap_h)
        # Top funnel: from under the top cap down to the center pinch.
        path.moveTo(left, top + cap_h)
        path.lineTo(right, top + cap_h)
        path.lineTo(cx, cy)
        path.closeSubpath()
        # Bottom funnel: from the center pinch out to above the bottom cap.
        path.moveTo(left, bottom - cap_h)
        path.lineTo(right, bottom - cap_h)
        path.lineTo(cx, cy)
        path.closeSubpath()

        painter = QPainter(img)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            if self._badge_shadow:
                shadow = QPainterPath(path)
                shadow_off = _SHADOW_OFFSET_PX * dpr
                shadow.translate(shadow_off, shadow_off)
                painter.fillPath(shadow, _SHADOW_COLOR)

            outline_pen = QPen(_OUTLINE_COLOR)
            outline_pen.setWidthF(_OUTLINE_PX * dpr)
            outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(outline_pen)
            painter.setBrush(_NUMERAL_COLOR)
            painter.drawPath(path)
        finally:
            painter.end()
        return img

    # -- public lifecycle ---------------------------------------------------

    def paint(
        self,
        summary: Any,
        overlay_session_id: int,
        paint_generation: int,
    ) -> Optional[dict[str, Any]]:
        """Paint the numbered overlay for ``summary``.

        Returns an ``overlay_state_changed`` wire dict (``state="painted"``
        on success, ``state="failed"`` on an internal error), or ``None``
        when the paint is stale-gated (a strictly-older
        ``(overlay_session_id, paint_generation)`` than the high-water
        mark) and is a no-op.

        Steps: gate -> enumerate native monitors ONCE + QScreens ONCE ->
        resolve each item (passing both seams) -> group rects by
        ``rect.hmonitor`` -> tear down monitors absent from this render
        BEFORE creating new windows -> composite each badge onto its
        monitor's window.
        """
        if not self._gate.accept_paint(overlay_session_id, paint_generation):
            logger.debug(
                "overlay_paint_window: stale paint (%s, %s) ignored",
                overlay_session_id,
                paint_generation,
            )
            return None

        snapshot_id = getattr(summary, "snapshot_id", None)
        try:
            painted_hmonitors, any_failed = self._do_paint(summary)
        except Exception:  # noqa: BLE001 - never let a paint failure escape
            logger.error(
                "overlay_paint_window: paint failed for session=%s gen=%s",
                overlay_session_id,
                paint_generation,
                exc_info=True,
            )
            # Best-effort teardown so we do not leave half-painted windows.
            self._destroy_all()
            return OverlayStateChangedEvent(
                state="failed",
                overlay_session_id=overlay_session_id,
                paint_generation=paint_generation,
                monitor_ids=(),
                snapshot_id=snapshot_id,
            ).to_dict()

        if any_failed:
            # At least one monitor's composite (UpdateLayeredWindow) failed.
            # _do_paint already destroyed/removed each failed monitor's
            # window so no stale prior-generation DIB lingers on screen.
            # Report "failed" (NOT "painted") so Logic does not believe the
            # new badge numbers are visible while the user still sees old or
            # no badges on the failed monitor(s) (wh-n29v.55.3).
            return OverlayStateChangedEvent(
                state="failed",
                overlay_session_id=overlay_session_id,
                paint_generation=paint_generation,
                monitor_ids=tuple(painted_hmonitors),
                snapshot_id=snapshot_id,
            ).to_dict()

        return OverlayStateChangedEvent(
            state="painted",
            overlay_session_id=overlay_session_id,
            paint_generation=paint_generation,
            monitor_ids=tuple(painted_hmonitors),
            snapshot_id=snapshot_id,
        ).to_dict()

    def paint_working_badge(
        self,
        center_x: int,
        center_y: int,
        overlay_session_id: int,
        paint_generation: int,
    ) -> Optional[dict[str, Any]]:
        """Paint a single working/busy badge centered on the virtual-desktop
        PHYSICAL point ``(center_x, center_y)``.

        Reuses the entire numbered-overlay paint pipeline: it builds a
        one-item synthetic summary whose bounds are a
        ``WORKING_BADGE_LOGICAL_PX``-perceived box centered on the point and
        whose display number is the ``WORKING_BADGE_NUMBER`` sentinel (so
        ``_render_badge`` draws the working glyph, not a numeral), then delegates
        to ``paint``. The resolver places the box on whichever monitor the
        point lands on. A point off EVERY enumerated monitor paints nothing:
        this method checks monitor containment up front and, when no monitor
        contains the point, delegates an EMPTY summary to ``paint`` (which
        returns a ``painted`` event with no monitors). It must do this itself
        rather than relying on the resolver to drop the badge -- the box is
        ``WORKING_BADGE_LOGICAL_PX * dpr`` wide and centered on the point, so a
        point just past a monitor edge still OVERLAPS that monitor, and the
        resolver paints an overlapping box (clipped at the edge). Building the
        box for an off-monitor point would therefore paint a clipped glyph and
        report a painted monitor for an off-screen / stale point
        (wh-overlay-4bug-review-r2.1).

        The box is sized in LOGICAL pixels: the physical box is
        ``WORKING_BADGE_LOGICAL_PX * dpr`` where ``dpr`` is the device pixel
        ratio of the monitor the point lands on. The resolver divides the
        physical box back by that monitor's ``dpr`` and
        ``_render_monitor_surface`` multiplies by ``dpr`` again, so the glyph is
        rendered at ``WORKING_BADGE_LOGICAL_PX * dpr`` physical px -- a CONSTANT
        PERCEIVED size on every monitor (a fixed physical box would instead
        shrink on hi-DPI; wh-dictation-retraction-indicator.11). The monitor
        topology is enumerated here once to find the monitor under the point
        and read its ``dpr`` (``paint`` enumerates again for the actual
        placement); a point on NO monitor paints nothing (see above).

        Bounds use the ``(x, y, width, height)`` convention that
        ``WalkSnapshotSummaryItem.bounds`` documents and that the rest of the
        codebase uses; ``_do_paint`` converts to the resolver's
        ``(left, top, right, bottom)`` form at the single call site
        (wh-overlay-bounds-format-mismatch).

        v1 is a STATIC one-shot badge: paint once here, clear via ``clear``;
        there is no per-frame following of a moving pointer (that would rebuild
        the window every frame). Returns the same ``overlay_state_changed``
        wire dict as ``paint`` (``None`` when stale-gated).
        """
        # Find the monitor under the point: its dpr sizes the badge in LOGICAL
        # pixels (constant perceived size, not constant physical px), and a
        # point on NO monitor must paint nothing. A box centered just past a
        # monitor edge still overlaps that monitor, so we cannot rely on the
        # resolver to drop it -- it would paint a clipped glyph and report a
        # painted monitor for an off-screen / stale point
        # (wh-overlay-4bug-review-r2.1). Detect "off all monitors" here.
        dpr = 1.0
        on_monitor = False
        for mon in _enumerate_native_monitors():
            if mon.rect_phys.contains(center_x, center_y):
                dpr = mon.dpr if mon.dpr > 0 else 1.0
                on_monitor = True
                break
        if not on_monitor:
            # Off every monitor: paint nothing. The empty summary runs the same
            # gate + return path as a real paint, so the result is the normal
            # no-monitor 'painted' event (or None when stale-gated) and any
            # prior overlay windows are torn down.
            return self.paint(
                _PointBadgeSummary(items=[]),
                overlay_session_id,
                paint_generation,
            )
        size_phys = max(1, int(round(WORKING_BADGE_LOGICAL_PX * dpr)))
        half = size_phys // 2
        bounds = (
            center_x - half,
            center_y - half,
            size_phys,
            size_phys,
        )
        summary = _PointBadgeSummary(
            items=[
                _PointBadgeItem(
                    bounds=bounds,
                    monitor_id=0,
                    display_number=WORKING_BADGE_NUMBER,
                )
            ]
        )
        return self.paint(summary, overlay_session_id, paint_generation)

    def _do_paint(self, summary: Any) -> tuple[list[int], bool]:
        """Resolve, group, (re)create windows, and composite badges.

        Returns ``(painted_hmonitors, any_failed)``: the ordered list of
        hmonitors that actually got a badge, and a flag that is True when
        ANY monitor's composite (``UpdateLayeredWindow``) returned False.
        When a monitor's composite fails, its overlay window is destroyed
        and removed from ``self._windows`` here so no stale prior-generation
        DIB lingers on screen (wh-n29v.55.3); ``paint`` maps the flag to
        ``state="failed"``.

        Surface model (wh-n29v.56.1, bounding-box bounded): each monitor's
        paint surface and overlay window are sized to the BOUNDING BOX of
        that monitor's badge rects (plus ``_SURFACE_MARGIN_PX`` margin), in
        PHYSICAL pixels, NOT the monitor's full physical resolution. So the
        transient QImage + GDI DIB scale with badge count, not monitor
        resolution. ``UpdateLayeredWindow`` replaces the WHOLE window surface
        per call and positions the window at a single screen origin: each
        monitor's badge surface is composited ONCE at the bounding box's
        SCREEN origin (``monitor.rect_phys.left()/top()`` + the bounding box's
        monitor-local physical offset). Every badge is drawn into the surface
        at its physical position MINUS that bounding-box offset, so the offset
        cancels exactly between the in-surface paint position and the
        composite destination -- on-screen placement is pixel-identical to
        the old full-monitor-surface model at every DPR.
        """
        # Retry any rebuild-path orphans parked on a PRIOR paint whose
        # DestroyWindow failed (wh-n29v.63.1). _pending_destroy is otherwise
        # swept only by _destroy_all (the clear / clear_all / paint-except
        # paths); the geom_phys rebuild key (wh-n29v.62) makes rebuilds routine,
        # so without a hot-path retry a long run of repaints with no intervening
        # clear would accumulate live, on-screen, always-on-top, click-through
        # windows. Each orphan is retried on the very next paint (same filter
        # _destroy_all uses) and dropped once its DestroyWindow finally succeeds.
        if self._pending_destroy:
            self._pending_destroy = [
                window
                for window in self._pending_destroy
                if not window.destroy()
            ]

        # Enumerate the topology EXACTLY ONCE per render (review findings
        # wh-n29v.51.2 / .51.3) and pass both to every resolve call.
        monitors = _enumerate_native_monitors()
        screens = _screens()

        items = list(getattr(summary, "items", []) or [])

        # Only an ENUMERATED monitor can own an overlay window. A resolved
        # rect whose hmonitor is not in the enumerated topology has no
        # window and its badge is skipped (rather than painted on the wrong
        # monitor). Index the enumerated monitors by hmonitor.
        enumerated_by_hmonitor: dict[int, _NativeMonitor] = {
            mon.hmonitor: mon for mon in monitors
        }

        # Resolve every item, grouping (rect, badge_number) by hmonitor.
        # Each badge is selected onto its window by rect.hmonitor (always
        # present) -- NOT rect.screen (which may be None).
        per_monitor: dict[int, list[tuple[OverlayPaintRect, int]]] = {}
        for item in items:
            # WalkSnapshotSummaryItem.bounds is (x, y, width, height) -- the
            # ElementMatch convention used everywhere else in the codebase
            # (uia_walker._rect_to_bounds, clear_winner_rule, click_executor).
            # resolve_overlay_paint_rect expects (left, top, right, bottom),
            # the raw UIA BoundingRectangle convention. Convert here so the two
            # contracts meet. Without this the resolver reads width as 'right'
            # and drops every control whose x exceeds its width as degenerate,
            # so nearly all badges vanish and the few that survive land at the
            # top-left corner (wh-overlay-bounds-format-mismatch).
            bx, by, bw, bh = item.bounds
            rect = resolve_overlay_paint_rect(
                (bx, by, bx + bw, by + bh),
                item.monitor_id,
                monitors=monitors,
                screens=screens,
            )
            if rect is None:
                # Off-screen / degenerate / empty-topology: skip this badge.
                continue
            if rect.hmonitor not in enumerated_by_hmonitor:
                # No overlay window can exist for an hmonitor outside the
                # enumerated topology; skip rather than mis-paint.
                logger.debug(
                    "overlay_paint_window: skipping badge for hmonitor %s "
                    "with no enumerated monitor / window",
                    rect.hmonitor,
                )
                continue
            per_monitor.setdefault(rect.hmonitor, []).append(
                (rect, item.display_number)
            )

        wanted = set(per_monitor.keys())

        # Compute the badge BOUNDING BOX per wanted monitor ONCE. The bounding
        # box drives BOTH the window geometry (its reuse key) and the surface
        # size, and its screen origin is the composite destination -- so the
        # geometry the window is created at and the offset the surface is
        # painted with come from the SAME computation (no drift). A monitor
        # not in ``wanted`` has no badges and gets no bounding box / window.
        bboxes: dict[int, _MonitorBBox] = {
            hmonitor: self._compute_monitor_bbox(
                enumerated_by_hmonitor[hmonitor], per_monitor[hmonitor]
            )
            for hmonitor in wanted
        }
        # The bounding box's SCREEN rectangle: window geometry + composite
        # origin. Origin = monitor physical top-left + monitor-local offset.
        geoms: dict[int, QRect] = {
            hmonitor: QRect(
                enumerated_by_hmonitor[hmonitor].rect_phys.left()
                + bboxes[hmonitor].offset_x,
                enumerated_by_hmonitor[hmonitor].rect_phys.top()
                + bboxes[hmonitor].offset_y,
                bboxes[hmonitor].width,
                bboxes[hmonitor].height,
            )
            for hmonitor in wanted
        }

        # Tear down windows for monitors absent from THIS render BEFORE
        # creating any new ones (so a monitor that lost all badges does not
        # linger, and a freshly-created window never collides with a stale
        # one of the same hmonitor).
        for hmonitor in list(self._windows.keys()):
            if hmonitor not in wanted:
                window = self._windows.pop(hmonitor)
                if not window.destroy():
                    # Still on screen -- keep it for a later teardown retry.
                    self._windows[hmonitor] = window

        # Create any newly-needed windows.
        h_instance = self._ensure_class_registered()
        for hmonitor in wanted:
            geom = geoms[hmonitor]
            existing = self._windows.get(hmonitor)
            if existing is None or existing.geom_phys != geom:
                # Rebuild when the bounding-box geometry changed (the monitor
                # moved/resized OR the badge layout changed); otherwise reuse.
                # Keying on the bounding box (not the full monitor rect)
                # guarantees a stale, differently-sized window/DIB is never
                # reused across a layout change (wh-n29v.56.1).
                if existing is not None:
                    old = self._windows.pop(hmonitor)
                    if not old.destroy():
                        # Still on screen, but the new window needs this
                        # hmonitor slot -- park the old one for a later
                        # teardown retry so it is not orphaned
                        # (wh-n29v.55.4 rebuild-path gap).
                        self._pending_destroy.append(old)
                # A fresh window for this hmonitor replaces any retained
                # stale one (the create below overwrites the dict slot).
                self._windows[hmonitor] = _OverlayWindow(
                    hmonitor=hmonitor,
                    geom_phys=geom,
                    class_name=self._CLASS_NAME,
                    user32=self._user32,
                    kernel32=self._kernel32,
                    h_instance=h_instance,
                )

        # Composite each monitor's bounding-box-sized badge surface ONCE at
        # the bounding box's SCREEN origin.
        painted: list[int] = []
        any_failed = False
        for hmonitor in wanted:
            window = self._windows.get(hmonitor)
            if window is None:
                # Defensive: a badge whose hmonitor has no overlay window is
                # skipped rather than painted on the wrong monitor.
                continue
            monitor = enumerated_by_hmonitor[hmonitor]
            geom = geoms[hmonitor]
            surface = self._render_monitor_surface(
                monitor, per_monitor[hmonitor], bboxes[hmonitor]
            )
            dib = build_layered_dib(surface)
            try:
                ok = window.composite(
                    dib,
                    geom.left(),
                    geom.top(),
                )
            finally:
                dib.release()
            if ok:
                painted.append(hmonitor)
            else:
                # UpdateLayeredWindow failed. On a REUSED window this leaves
                # the prior generation's DIB (old badge numbers) visible, so
                # destroy and remove the window so no stale DIB lingers, and
                # flag the whole paint as failed (wh-n29v.55.3). A window that
                # itself fails to destroy is retained for a later retry.
                any_failed = True
                logger.warning(
                    "overlay_paint_window: composite failed for monitor %s; "
                    "destroying its window so no stale DIB lingers",
                    hmonitor,
                )
                if window.destroy():
                    self._windows.pop(hmonitor, None)
        return painted, any_failed

    def _compute_monitor_bbox(
        self,
        monitor: _NativeMonitor,
        badges: list[tuple[OverlayPaintRect, int]],
    ) -> _MonitorBBox:
        """Compute the badge bounding box for one monitor in monitor-local
        PHYSICAL pixels, expanded by ``_SURFACE_MARGIN_PX`` * dpr on every
        side.

        Each badge rect from ``resolve_overlay_paint_rect`` is in Qt LOGICAL
        coordinates local to the monitor. Its PHYSICAL footprint depends on the
        badge kind. A WORKING glyph fills its box, so its footprint is the
        control box ``[rect.x*dpr, rect.y*dpr]`` .. ``[(rect.x+rect.width)*dpr,
        (rect.y+rect.height)*dpr]``. A NUMERAL is drawn in a tight image
        CENTERED on the control, so its footprint is the union of the control
        box and the centered tight-image rectangle; for a control narrower or
        shorter than the numeral that union extends past the control so the
        surface still fully contains the numeral and never clips a digit
        (wh-overlay-4bug-review-r1.1). The union of every badge's footprint,
        floored / ceiled to integer physical pixels and padded by the margin,
        is the bounding box. The offset MAY be negative when a control sits partly
        off the monitor's left/top edge (``rect.x`` / ``rect.y`` can be
        negative per the resolver contract); the surface still bounds only
        the on-monitor portion plus margin and the badge clips at the window
        edge exactly as before.

        ``badges`` is non-empty by construction (a monitor with no badges is
        never in ``wanted``); the floor of ``min``/``max`` over the rects is
        therefore well defined. Width/height are floored to >= 1 so a
        degenerate single-pixel layout still yields a valid surface.

        Monitor-bounds clamp (wh-n29v.64.1): ``resolve_overlay_paint_rect``
        deliberately allows a control that hangs partly off the monitor's
        left/top edge (negative local ``x``/``y``) or past its right/bottom
        edge, and returns the control's FULL local rect. The old full-monitor
        QImage/window clipped every such overhang at local ``0 .. monitor
        size``. The bounding-box surface must reproduce that exactly, so the
        integer bounding box is clamped to the monitor's LOCAL physical
        rectangle ``[0, rect_phys.width()] x [0, rect_phys.height()]`` AFTER
        the margin is applied. Two reasons: (1) without the clamp the window's
        screen origin (``rect_phys.left()/top() + offset``) could start before
        the monitor or extend past it, painting the off-monitor portion -- on a
        multi-monitor desktop that lands on an ADJACENT monitor, so placement
        would NOT be pixel-identical to the old model; (2) a malformed or very
        large UIA bounds rectangle that overlaps the monitor by a sliver but
        reports a huge off-screen extent would otherwise allocate a QImage/GDI
        DIB far larger than the monitor on the paint path. The old full-monitor
        surface was always bounded by monitor resolution; the clamp restores
        that allocation cap. The clamp does NOT move any on-monitor badge: the
        translate(-offset)/scale(dpr) cancellation holds for any offset, so a
        clamped offset only clips the off-monitor pixels at the surface edge --
        identical to the old behaviour. Clamping strictly to the monitor (not
        monitor-plus-margin) is what makes the on-screen result pixel-identical
        to the old monitor-sized clip; a badge decoration that fell past the
        monitor edge was off-screen in the old model too.
        """
        dpr = monitor.dpr if monitor.dpr > 0 else 1.0
        margin = _SURFACE_MARGIN_PX * dpr
        mon_w = monitor.rect_phys.width()
        mon_h = monitor.rect_phys.height()

        lefts: list[float] = []
        tops: list[float] = []
        rights: list[float] = []
        bottoms: list[float] = []
        # One shared sequential placement pass; the paint path consumes the
        # same pass output shape, so the surface always contains exactly the
        # badges that are drawn (see _numeral_badge_placements_phys).
        placements = self._numeral_badge_placements_phys(
            badges, dpr, mon_w, mon_h, corner=self._badge_corner,
        )
        for (rect, _number), placement in zip(badges, placements):
            cl = rect.x * dpr
            ct = rect.y * dpr
            cr = (rect.x + rect.width) * dpr
            cb = (rect.y + rect.height) * dpr
            if placement is None:
                # The working glyph FILLS its box (drawn at the control
                # top-left), so its footprint IS the control box.
                lefts.append(cl)
                tops.append(ct)
                rights.append(cr)
                bottoms.append(cb)
                continue
            # A numeral is drawn in a TIGHT image placed just past the control's
            # trailing edge when that strip is clear, else anchored to the
            # configured corner (default top-right; wh-overlay-badge-occludes-
            # label), possibly nudged off an earlier badge
            # (wh-overlay-badge-collision). Either way the image can overhang
            # the control, so union the control box (which keeps the surface
            # contract unchanged for normal-size controls) with the badge
            # footprint so the surface always fully contains the numeral and
            # never clips a digit.
            _bw, _bh, (bl, bt, br, bb) = placement
            lefts.append(min(cl, bl))
            tops.append(min(ct, bt))
            rights.append(max(cr, br))
            bottoms.append(max(cb, bb))

        left = min(lefts)
        top = min(tops)
        right = max(rights)
        bottom = max(bottoms)

        # Floor the top-left and ceil the bottom-right (after the margin) so
        # the integer bounding box fully CONTAINS every badge's physical
        # footprint -- no sub-pixel clipping at the surface edges.
        min_x = math.floor(left - margin)
        min_y = math.floor(top - margin)
        max_x = math.ceil(right + margin)
        max_y = math.ceil(bottom + margin)

        # Clamp to the monitor's local physical rectangle so the surface/window
        # never starts before the monitor, never extends past it, and never
        # exceeds monitor-resolution allocation (wh-n29v.64.1). Every badge
        # here overlaps the monitor (resolve_overlay_paint_rect returns None
        # otherwise), so the clamped box is non-empty; max(1, ...) guards the
        # degenerate single-pixel edge.
        min_x = max(0, min_x)
        min_y = max(0, min_y)
        max_x = min(mon_w, max_x)
        max_y = min(mon_h, max_y)

        return _MonitorBBox(
            offset_x=min_x,
            offset_y=min_y,
            width=max(1, max_x - min_x),
            height=max(1, max_y - min_y),
        )

    @staticmethod
    def _numeral_badge_footprint_phys(
        rect: OverlayPaintRect,
        badge_w_phys: float,
        badge_h_phys: float,
        dpr: float,
        mon_w_phys: Optional[float] = None,
        mon_h_phys: Optional[float] = None,
        *,
        corner: str,
    ) -> tuple[float, float, float, float]:
        """Physical ``(left, top, right, bottom)`` a numeral badge occupies.

        The numeral is anchored to ONE CORNER of the control (``corner``), not
        centered on it, so the digit covers only that corner and leaves the rest
        of the control's label/icon visible (wh-overlay-badge-occludes-label).
        The default corner is TOP-RIGHT (see ``_DEFAULT_BADGE_CORNER``): a
        Windows list row, tree item, or menu entry keeps its icon and label at
        the LEFT, so a top-LEFT badge covered exactly the icon and the first
        letters -- the part the user reads to identify the row. ``corner`` is the
        validated ``overlay_badge_corner`` setting threaded from the manager; it
        is required so every caller states it and the paint path and the bounding
        box can never disagree.

        When the monitor's physical size is supplied (``mon_w_phys`` /
        ``mon_h_phys``), the anchor is shifted INWARD just enough to keep the
        whole badge on the monitor (wh-review-click-overlay-codex.2). A control
        against the monitor edge (or narrower/shorter than the numeral) would
        otherwise push the anchored badge past the monitor, where the surface
        clamp in ``_compute_monitor_bbox`` truncates the digit. The shift moves
        only such an edge badge; a control with room keeps its exact anchor.

        A control small in BOTH dimensions (under ``_BADGE_SMALL_CONTROL_FACTOR``
        badge widths and heights) gets the badge centered on the corner POINT
        instead -- half outside on both axes -- so only about a quarter of the
        badge covers the control (wh-overlay-small-control-cover). A packed
        toolbar icon button has no clear trailing strip, and a fully-inside
        corner badge covered most of its icon. Wide controls keep the exact
        inside-corner anchor: a half-above badge on a list row would sit
        visually between two stacked rows and read as ambiguous. The monitor
        clamp below applies to the corner-point placement too, so a small
        control at the screen edge pulls its badge back on-screen.

        Both the paint path (``_render_monitor_surface``) and the surface
        bounding box (``_compute_monitor_bbox``) call this with the SAME monitor
        bounds AND the same corner so they can never disagree on where the badge
        is placed. The rectangle is in PHYSICAL pixels local to the monitor; the
        paint path rounds the top-left to integer pixels for ``drawImage``.
        """
        left_phys = rect.x * dpr
        top_phys = rect.y * dpr
        right_phys = (rect.x + rect.width) * dpr
        bottom_phys = (rect.y + rect.height) * dpr
        is_right = corner in (_BADGE_CORNER_TOP_RIGHT, _BADGE_CORNER_BOTTOM_RIGHT)
        is_bottom = corner in (_BADGE_CORNER_BOTTOM_LEFT, _BADGE_CORNER_BOTTOM_RIGHT)
        small = (
            (right_phys - left_phys)
            < badge_w_phys * _BADGE_SMALL_CONTROL_FACTOR
            and (bottom_phys - top_phys)
            < badge_h_phys * _BADGE_SMALL_CONTROL_FACTOR
        )
        if small:
            # Center the badge on the corner point: half outside on both axes.
            corner_x = right_phys if is_right else left_phys
            corner_y = bottom_phys if is_bottom else top_phys
            left = corner_x - badge_w_phys / 2.0
            top = corner_y - badge_h_phys / 2.0
        else:
            # Anchor the badge to the requested corner: a right anchor aligns
            # the badge's right edge to the control's right edge (subtract the
            # badge width); a bottom anchor aligns the badge's bottom edge to
            # the control's bottom edge (subtract the badge height).
            left = right_phys - badge_w_phys if is_right else left_phys
            top = bottom_phys - badge_h_phys if is_bottom else top_phys
        # Keep the whole badge on the monitor. The clamp is corner-agnostic: the
        # min() pulls in a right/bottom anchor that overhangs the far edge, and
        # the max(0.0, ...) pushes in a left/top anchor -- or a right anchor on a
        # control narrower than the badge, whose left can go negative.
        if mon_w_phys is not None:
            left = max(0.0, min(left, mon_w_phys - badge_w_phys))
        if mon_h_phys is not None:
            top = max(0.0, min(top, mon_h_phys - badge_h_phys))
        return (left, top, left + badge_w_phys, top + badge_h_phys)

    @staticmethod
    def _rects_overlap_phys(
        a: tuple[float, float, float, float],
        b: tuple[float, float, float, float],
    ) -> bool:
        """Whether two ``(left, top, right, bottom)`` rectangles overlap by area.

        Edge-touching is NOT overlap (strict inequalities): a badge placed with
        its left edge exactly on a control's right edge does not count as
        overlapping that control, which is what lets the placement pass the
        badge's OWN control box in the rect list without self-blocking.
        """
        return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]

    def _numeral_badge_placement_phys(
        self,
        rect: OverlayPaintRect,
        badge_w_phys: float,
        badge_h_phys: float,
        dpr: float,
        mon_w_phys: Optional[float],
        mon_h_phys: Optional[float],
        ctrl_rects_phys: "list[tuple[float, float, float, float]]",
        *,
        corner: str,
        placed_badges: "Optional[list[tuple[float, float, float, float]]]" = None,
    ) -> tuple[float, float, float, float]:
        """Physical ``(left, top, right, bottom)`` the numeral badge occupies.

        Prefer the empty space just BEYOND the control's trailing edge (the
        corner's horizontal side) over the inside corner, when that placement is
        safe (wh-overlay-badge-occludes-label follow-up). A left-aligned
        vertical list -- the File Explorer navigation tree, a Details-view file
        list, a menu -- keeps its icon and label at the LEFT and has blank space
        to the right of every item, so a corner-anchored badge still landed on
        the label (a nav item whose box hugs a short folder name) or on a
        trailing value (a file's size). The trailing placement clears both.

        The outside placement is used ONLY when it is safe: the badge must stay
        fully on the monitor AND must not overlap any OTHER walked control's box.
        A grid tile or a packed toolbar button has a neighbour immediately to its
        right, so the overlap test fails and the badge falls back to the inside
        corner (``_numeral_badge_footprint_phys``) unchanged -- no regression for
        those layouts. A control flush against the monitor edge also falls back
        (and is then shifted inward by the corner clamp). When
        ``self._badge_trailing_space`` is False, or the monitor bounds are not
        supplied, the corner placement is used directly.

        ``ctrl_rects_phys`` is the physical ``(l, t, r, b)`` box of EVERY badge
        on the monitor (the caller passes the full list, unsliced). The badge's
        own box may be present: the outside candidate shares exactly one edge
        with it, and :meth:`_rects_overlap_phys` uses strict inequalities, so the
        own box never blocks. Both the paint path (``_render_monitor_surface``)
        and the surface bounding box (``_compute_monitor_bbox``) call this with
        the SAME list and corner so the two can never disagree on placement.

        Known limitation: ``ctrl_rects_phys`` holds only the NUMBERED controls,
        so the strip can be judged clear when a control that was not numbered (a
        scrollbar, a static label) actually sits there, and the number is then
        drawn over it. This is cosmetic only -- the overlay never takes mouse
        input -- and ``overlay_badge_trailing_space=False`` restores the corner
        placement. Kept as-is on review (2026-07-04): closing the gap would need
        a second UI-tree walk plus a new Input-to-GUI message for the obstacle
        rectangles, which is disproportionate to a cosmetic overlap.

        ``placed_badges`` is the footprint of every badge ALREADY placed on
        this monitor, in list order (wh-overlay-badge-collision). The trailing
        candidate must not land on one, and a corner anchor that collides is
        nudged away (``_resolve_badge_collision``). ``None``/empty keeps the
        stateless behaviour for direct callers.
        """
        placed = placed_badges if placed_badges is not None else []

        def _hits_placed(c: tuple[float, float, float, float]) -> bool:
            return any(self._rects_overlap_phys(c, p) for p in placed)

        def _corner_resolved() -> tuple[float, float, float, float]:
            base = self._numeral_badge_footprint_phys(
                rect, badge_w_phys, badge_h_phys, dpr,
                mon_w_phys, mon_h_phys, corner=corner,
            )
            # The control's OWN box is excluded from the nudge avoidance: the
            # corner anchor already sits on it, so landing there is fine. Only
            # OTHER controls' boxes make a nudged digit read as labeling the
            # wrong control (wh-overlay-collision-review.1).
            own_box = (
                rect.x * dpr, rect.y * dpr,
                (rect.x + rect.width) * dpr, (rect.y + rect.height) * dpr,
            )
            others = [b for b in ctrl_rects_phys if b != own_box]
            return self._resolve_badge_collision(
                base, badge_w_phys, badge_h_phys, dpr,
                mon_w_phys, mon_h_phys, placed, others, corner=corner,
            )

        if (
            not self._badge_trailing_space
            or mon_w_phys is None
            or mon_h_phys is None
        ):
            return _corner_resolved()

        left_phys = rect.x * dpr
        top_phys = rect.y * dpr
        right_phys = (rect.x + rect.width) * dpr
        bottom_phys = (rect.y + rect.height) * dpr
        is_right = corner in (_BADGE_CORNER_TOP_RIGHT, _BADGE_CORNER_BOTTOM_RIGHT)
        is_bottom = corner in (
            _BADGE_CORNER_BOTTOM_LEFT, _BADGE_CORNER_BOTTOM_RIGHT
        )
        # Beyond the trailing edge: a right corner puts the badge's LEFT edge on
        # the control's right edge; a left corner puts the badge's RIGHT edge on
        # the control's left edge (the left gutter). Vertical alignment follows
        # the corner's top/bottom side, as inside placement does.
        cand_left = right_phys if is_right else left_phys - badge_w_phys
        cand_top = (bottom_phys - badge_h_phys) if is_bottom else top_phys
        cand_right = cand_left + badge_w_phys
        cand_bottom = cand_top + badge_h_phys

        # Must stay fully on the monitor; otherwise the surface clamp would cut
        # the digit, so fall back to the corner (which shifts inward instead).
        if (
            cand_left < 0.0
            or cand_top < 0.0
            or cand_right > mon_w_phys
            or cand_bottom > mon_h_phys
        ):
            return _corner_resolved()

        # Must not land on any other walked control's box, nor on a badge that
        # is already placed there (wh-overlay-badge-collision).
        cand = (cand_left, cand_top, cand_right, cand_bottom)
        for other in ctrl_rects_phys:
            if self._rects_overlap_phys(cand, other):
                return _corner_resolved()
        if _hits_placed(cand):
            return _corner_resolved()
        return cand

    def _resolve_badge_collision(
        self,
        base: tuple[float, float, float, float],
        badge_w_phys: float,
        badge_h_phys: float,
        dpr: float,
        mon_w_phys: Optional[float],
        mon_h_phys: Optional[float],
        placed: "list[tuple[float, float, float, float]]",
        other_ctrl_rects: "list[tuple[float, float, float, float]]",
        *,
        corner: str,
    ) -> tuple[float, float, float, float]:
        """Move a corner-anchored badge off the badges already placed there
        (wh-overlay-badge-collision).

        The live-test trigger: a column header and the thin column-resize
        splitter beside it share an edge; both trailing strips are occupied, so
        both corner anchors land in the same spot and the two digits stack into
        an unreadable blob (Explorer's Size header drew 39 on 40). Placement is
        sequential (``_numeral_badge_placements_phys``), so each badge only has
        to avoid the badges placed BEFORE it.

        Candidates are tried in a fixed order so both placement passes agree:
        the base anchor itself, then one badge width INWARD along the corner's
        horizontal side, then one badge height BELOW, then ABOVE. Each nudge
        keeps ``_BADGE_COLLISION_GAP_PX`` (scaled by dpr) of clear space --
        two digits drawn flush read as one number. A candidate that leaves the
        monitor is skipped.

        Among the on-monitor, badge-free candidates, one that overlaps no
        OTHER control's box is preferred (``other_ctrl_rects``; the caller
        excludes the control's own box): a digit nudged fully onto a
        neighbouring numbered control reads as labeling that control
        (wh-overlay-collision-review.1). When every such spot is taken, the
        first badge-free candidate wins anyway -- sitting on a neighbour still
        beats stacking on another digit. When every candidate collides with a
        badge (a pathological pile-up of identical controls), the base anchor
        is returned: an overlapped number is still clickable by voice, a
        dropped number is not.
        """
        def _hits_placed(c: tuple[float, float, float, float]) -> bool:
            return any(self._rects_overlap_phys(c, p) for p in placed)

        if not placed or not _hits_placed(base):
            return base
        gap = _BADGE_COLLISION_GAP_PX * dpr
        is_right = corner in (_BADGE_CORNER_TOP_RIGHT, _BADGE_CORNER_BOTTOM_RIGHT)
        inward_dx = -(badge_w_phys + gap) if is_right else (badge_w_phys + gap)
        viable: "list[tuple[float, float, float, float]]" = []
        for cand_left, cand_top in (
            (base[0] + inward_dx, base[1]),
            (base[0], base[1] + badge_h_phys + gap),
            (base[0], base[1] - badge_h_phys - gap),
        ):
            cand = (
                cand_left, cand_top,
                cand_left + badge_w_phys, cand_top + badge_h_phys,
            )
            if mon_w_phys is not None and (
                cand[0] < 0.0 or cand[2] > mon_w_phys
            ):
                continue
            if mon_h_phys is not None and (
                cand[1] < 0.0 or cand[3] > mon_h_phys
            ):
                continue
            if not _hits_placed(cand):
                viable.append(cand)
        for cand in viable:
            if not any(
                self._rects_overlap_phys(cand, o) for o in other_ctrl_rects
            ):
                return cand
        if viable:
            return viable[0]
        return base

    def _numeral_badge_placements_phys(
        self,
        badges: "list[tuple[OverlayPaintRect, int]]",
        dpr: float,
        mon_w_phys: Optional[float],
        mon_h_phys: Optional[float],
        *,
        corner: str,
    ) -> "list[Optional[tuple[int, int, tuple[float, float, float, float]]]]":
        """Place every badge for one monitor in ONE sequential pass.

        Returns a list parallel to ``badges``: ``None`` for the WORKING glyph
        (which fills its own control box and needs no numeral placement), else
        ``(badge_w, badge_h, (left, top, right, bottom))`` in physical pixels.

        This is the single placement entry point for both the surface bounding
        box (``_compute_monitor_bbox``) and the paint path
        (``_render_monitor_surface``): both consume the SAME pass output shape,
        so the surface always contains exactly the badges that are drawn --
        agreement by construction, not by matching two hand-built input lists.

        The pass is sequential so each badge can avoid the badges already
        placed (wh-overlay-badge-collision): ``placed`` accumulates every
        numeral footprint in list order and feeds the next placement's
        collision checks. The pass is deterministic (pure float math over the
        same inputs), so the bbox call and the render call always produce
        identical placements.
        """
        # Every badge's physical control box, so the trailing-space placement
        # can tell whether the strip just past a control is occupied by another
        # walked control (a grid tile / packed toolbar button). The badge's own
        # box is included -- it shares one edge with the outside candidate and
        # never self-blocks (see _numeral_badge_placement_phys).
        ctrl_rects_phys = [
            (r.x * dpr, r.y * dpr,
             (r.x + r.width) * dpr, (r.y + r.height) * dpr)
            for r, _n in badges
        ]
        # The numeral font metrics are fixed for the whole monitor (the font's
        # only variable, dpr, is constant), so build them once and share them
        # across badges (wh-overlay-4bug-review.2). Built lazily so a
        # working-glyph-only paint pays nothing.
        numeral_metrics: Optional[QFontMetricsF] = None
        placed: "list[tuple[float, float, float, float]]" = []
        out: "list[Optional[tuple[int, int, tuple[float, float, float, float]]]]" = []
        for rect, number in badges:
            if number == WORKING_BADGE_NUMBER:
                out.append(None)
                continue
            if numeral_metrics is None:
                numeral_metrics = QFontMetricsF(self._numeral_font(dpr))
            bw, bh = self._numeral_badge_size(
                number, dpr, metrics=numeral_metrics
            )
            footprint = self._numeral_badge_placement_phys(
                rect, bw, bh, dpr, mon_w_phys, mon_h_phys, ctrl_rects_phys,
                corner=corner, placed_badges=placed,
            )
            placed.append(footprint)
            out.append((bw, bh, footprint))
        return out

    def _render_monitor_surface(
        self,
        monitor: _NativeMonitor,
        badges: list[tuple[OverlayPaintRect, int]],
        bbox: _MonitorBBox,
    ) -> QImage:
        """Compose all badges for one monitor onto one BOUNDING-BOX-sized
        surface (wh-n29v.56.1).

        The surface is the badge bounding box in PHYSICAL pixels
        (``bbox.width`` x ``bbox.height``), NOT the monitor's full physical
        resolution, so transient paint memory scales with badge count, not
        monitor resolution. ``UpdateLayeredWindow`` (via
        ``composite_layered_window``) blits the DIB 1:1 against the window's
        physical device pixels -- it does NOT scale -- and the window is
        positioned at the bounding box's screen origin, so the surface is
        painted in bounding-box-LOCAL physical pixels.

        The badge rects are in Qt LOGICAL coordinates local to the monitor, and
        every badge is rendered at PHYSICAL resolution (``dpr`` is forwarded to
        ``_render_badge``) so its edges stay sharp on a display scaled above
        100%. The painter is translated by the bounding box's monitor-local
        physical offset (``-bbox.offset_x`` / ``-bbox.offset_y``), and the
        window is composited at ``rect_phys.left()/top() + bbox.offset``, so the
        offset cancels and a badge's on-screen physical position matches the
        full-monitor-surface model.

        Two badge kinds are positioned differently:

        - A NUMERAL is a small glyph. It is rendered into a TIGHT image sized to
          the numeral (``_numeral_badge_size``), not to the control, and drawn
          anchored to the control's configured corner (default top-right;
          ``_numeral_badge_footprint_phys``) so the digit covers only a corner
          and leaves the control's own label/icon visible
          (wh-overlay-badge-occludes-label). A badge on a control at the
          monitor's right or bottom edge is shifted inward so the surface clamp
          does not truncate the digit (wh-review-click-overlay-codex.2). The
          tight image also avoids a PER-BADGE control-sized (dpr^2) transient
          QImage for a small number over a large control
          (wh-overlay-badge-alloc-decouple). The per-monitor
          SURFACE itself is still bounding-box-sized (``_compute_monitor_bbox``),
          so for one large control the surface alone is still control-sized --
          that fix removes the per-badge image, halving the peak for that case,
          not the surface (wh-overlay-4bug-review.2).
        - The WORKING glyph FILLS its box, so it is rendered at the box physical
          size (``logical * dpr``) and drawn at the box top-left, putting the box
          center on the requested point.

        The original model rendered every badge at LOGICAL size and applied
        ``painter.scale(dpr, dpr)``, which enlarged a low-resolution image and
        softened every edge on a scaled display. Rendering at physical
        resolution and forwarding ``dpr`` to ``_render_badge`` (which scales the
        font/outline/shadow by ``dpr``) keeps the same perceived size while
        drawing at full screen resolution (wh-dictation-retraction-indicator.11).
        """
        dpr = monitor.dpr if monitor.dpr > 0 else 1.0
        surface = QImage(
            bbox.width, bbox.height, QImage.Format.Format_ARGB32_Premultiplied
        )
        surface.fill(0)  # transparent: only the badges draw

        painter = QPainter(surface)
        # The SAME sequential placement pass the bounding box ran (identical
        # inputs, deterministic math), so the surface never clips a badge it
        # did not budget for and collision nudges land where the bbox expected
        # them (see _numeral_badge_placements_phys).
        placements = self._numeral_badge_placements_phys(
            badges, dpr,
            monitor.rect_phys.width(), monitor.rect_phys.height(),
            corner=self._badge_corner,
        )
        try:
            # Offset by the bounding box origin in PHYSICAL pixels. No painter
            # scale: each badge is already rendered at physical size and is
            # drawn 1:1 at its integer physical position (sharp, not enlarged).
            painter.translate(-bbox.offset_x, -bbox.offset_y)
            for (rect, number), placement in zip(badges, placements):
                if placement is None:
                    # The working glyph FILLS its box: the box size (logical *
                    # dpr) IS the intended perceived size, so render at that
                    # physical size and draw at the box top-left, which puts the
                    # box center on the requested point.
                    w_phys = max(1, int(round(rect.width * dpr)))
                    h_phys = max(1, int(round(rect.height * dpr)))
                    badge = self._render_badge(number, w_phys, h_phys, dpr)
                    painter.drawImage(
                        int(round(rect.x * dpr)),
                        int(round(rect.y * dpr)),
                        badge,
                    )
                    continue
                # A numeral is a small glyph: render it into a TIGHT image sized
                # to the numeral (not the control) and draw it just past the
                # control's trailing edge when that strip is clear, else anchored
                # to the configured corner (default top-right;
                # wh-overlay-badge-occludes-label), nudged off any earlier badge
                # (wh-overlay-badge-collision), so the digit leaves the
                # control's own label/icon visible. The tight image also avoids a
                # control-sized (dpr^2) transient allocation
                # (wh-overlay-badge-alloc-decouple).
                bw, bh, (bl, bt, _br, _bb) = placement
                badge = self._render_badge(number, bw, bh, dpr)
                painter.drawImage(int(round(bl)), int(round(bt)), badge)
        finally:
            painter.end()
        return surface

    def clear(
        self,
        overlay_session_id: int,
        paint_generation: int,
    ) -> Optional[dict[str, Any]]:
        """Tear down ALL overlay windows and report ``state="cleared"``.

        Advances the generation high-water mark (when the clear's pair is
        ``>=`` the mark) so a late stale paint at the prior generation
        cannot present afterwards.

        A clear whose ``(overlay_session_id, paint_generation)`` pair is
        STRICTLY OLDER than the mark is STALE -- a newer paint or newer
        clear already advanced the mark, so honoring it would destroy a
        NEWER overlay that must stay on screen. A stale clear is a no-op:
        it returns ``None`` and tears down nothing (wh-n29v.55.1). The GUI
        caller drops a ``None`` result.
        """
        if not self._gate.accept_clear(overlay_session_id, paint_generation):
            logger.debug(
                "overlay_paint_window: stale clear (%s, %s) ignored",
                overlay_session_id,
                paint_generation,
            )
            return None
        self._destroy_all()
        return OverlayStateChangedEvent(
            state="cleared",
            overlay_session_id=overlay_session_id,
            paint_generation=paint_generation,
            monitor_ids=(),
            snapshot_id=None,
        ).to_dict()

    def clear_all(self) -> None:
        """Destroy every overlay window without emitting an event.

        Used on GUI teardown / fixture cleanup.
        """
        self._destroy_all()

    def _destroy_all(self) -> None:
        """Destroy every overlay window, retaining any that failed to destroy.

        A window whose ``destroy()`` returned False is still on screen, so it
        is kept for a later teardown to retry; only the windows that
        successfully destroyed are dropped. This sweeps two sources:

        * ``self._pending_destroy`` -- rebuild-path orphans (the new window
          took their hmonitor slot), retried first; still-failing ones stay.
        * ``self._windows`` -- the live per-monitor windows; still-failing
          ones stay in the dict under their hmonitor.
        """
        # Retry rebuild-path orphans first; keep the ones that still fail.
        self._pending_destroy = [
            window for window in self._pending_destroy if not window.destroy()
        ]
        survivors: dict[int, _OverlayWindow] = {}
        for hmonitor, window in list(self._windows.items()):
            if not window.destroy():
                survivors[hmonitor] = window
        self._windows = survivors
