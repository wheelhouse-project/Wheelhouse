"""Numbered-overlay paint window for Phase 1.5 voice element clicking
(slice wh-n29v.53, source leaf wh-h7cvz1).

This GUI-process module manages ONE transparent, always-on-top,
no-activate, click-through layered Win32 window PER monitor that
currently has badges. It paints centered outlined numerals (optional
drop shadow, no background box, configurable point size) over on-screen
controls via
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
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass, fields
from typing import Any, Optional

from PySide6.QtCore import QLineF, QPointF, QRect, QRectF, Qt
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

from services.wheelhouse.grid_overlay_state import (
    GridRect as _GridRect,
    cell_center as _grid_cell_center,
    cell_rects as _grid_cell_rects,
)
from shared.monitor_geometry import (
    _NativeMonitor,
    _enumerate_native_monitors,
    _overlap_area,
)
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
# Outline pen width dedicated to the working/busy hourglass, in logical
# pixels. WORKING_BADGE_LOGICAL_PX was halved (64 -> 32); a live check
# already found this same 3px outline unreadable on a hourglass that small
# (36 logical px read as a shrunken blob -- wh-dictation-retraction-
# indicator.11), so the outline is scaled down by the same ratio (3/64) to
# preserve the outline-to-shape proportion that read cleanly at 64.
_HOURGLASS_OUTLINE_PX = 1.5
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
# A numbered control whose width is at least this many times its height is a
# "wide row" (wh-vscode-menu-badge-misplaced): its numeral goes in the LEADING
# gutter, beside the edge where the label starts, instead of past the far
# trailing edge. A VS Code menu row is 861 pixels wide, so the trailing
# placement put the number about 870 pixels from the word it labels. The
# threshold is a settled decision: 10 x height, accepted by David 2026-09-14.
_WIDE_ROW_MIN_ASPECT = 10
# Gap (LOGICAL pixels, scaled by dpr) between a badge and the already-placed
# badge it was nudged away from (wh-overlay-badge-collision). Two digits drawn
# flush against each other read as one number ("3" beside "4" reads "34"), so
# collision nudges keep this much clear space between badges.
_BADGE_COLLISION_GAP_PX = 3.0
# How many rings of collision-nudge candidates to search around the base
# anchor (wh-overlay-bubble-badges.4). Each ring tries eight directions at
# ring-many badge-size steps out, so 3 rings is 24 candidates. The old
# three-candidate list ran out in packed clusters at monitor corners and
# stacked the digits; a badge pushed past the attach threshold draws as a
# detached bubble with a leader line, so a far candidate stays readable.
_BADGE_COLLISION_RINGS = 3

# Edge-cluster layout (wh-taskbar-badge-mispoint, option 1): several small
# controls packed against a monitor edge -- the tray corner of a vertical or
# horizontal taskbar -- get their badges in ONE single-file column (left/right
# edge) or row (bottom/top edge) beside the cluster, in target order along
# the edge, instead of collision-ring scatter (which places in ring order,
# not target order, so the leader lines crossed into a tangle). A run must
# have at least this many members to form a cluster; the collision nudge
# already handles a pair unambiguously.
_EDGE_CLUSTER_MIN_COUNT = 3
# A cluster member must lie ENTIRELY within this many badge heights of the
# monitor edge. The band is deliberately narrow: a full-width list row (nav
# pane, file list) flush to the window edge extends far past it and must
# never trade its trailing-space placement for a column.
_EDGE_CLUSTER_BAND_FACTOR = 3.0
# Consecutive run members may be separated by at most this many badge heights
# of gap along the edge; a larger gap splits the run (two separate icon
# groups).
_EDGE_CLUSTER_JOIN_GAP_FACTOR = 1.0

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

# The bubble badge's own color scheme (wh-overlay-bubble-badges): "light" is a
# white bubble with a black digit, "dark" a near-black bubble with a white
# digit, and "auto" picks per paint from the Windows app theme -- system dark
# theme -> light bubble, system light theme -> dark bubble, so the bubble
# contrasts with typical screen content. The values name the BUBBLE color, not
# the system theme. The value is the validated [click] setting
# overlay_badge_theme; an unknown value falls back to _DEFAULT_BADGE_THEME.
_BADGE_THEME_AUTO = "auto"
_BADGE_THEME_LIGHT = "light"
_BADGE_THEME_DARK = "dark"
_VALID_BADGE_THEMES = frozenset(
    {_BADGE_THEME_AUTO, _BADGE_THEME_LIGHT, _BADGE_THEME_DARK}
)
_DEFAULT_BADGE_THEME = _BADGE_THEME_AUTO

# Bubble geometry (wh-overlay-bubble-badges). The bubble's padding around the
# digit is proportional to the digit's cap height, with an absolute floor so a
# tiny font still reads as a bubble rather than a box hugging the digit.
_BUBBLE_PAD_X_CAP_FACTOR = 0.4
_BUBBLE_PAD_Y_CAP_FACTOR = 0.3
_BUBBLE_PAD_MIN_LOGICAL_PX = 3.0

# The three per-badge drawing states, decided from the shortest straight-line
# distance between the bubble's final placed rectangle and its control's
# rectangle: intersecting by area -> bubble + a pointer tail aimed inward
# toward the control's center (user decision 2026-08-07: every bubble points
# at its control, matching Voice Access); within
# _BUBBLE_ATTACH_GAP_LOGICAL_PX (scaled by dpr) -> bubble + pointer tail
# across the gap; further away (a collision nudge or monitor-edge shift moved
# it) -> bubble + thin leader line. A single threshold, deliberately NOT a
# config setting (user decision 2026-08-04: judge it visually, adjust the
# constant if needed).
_BUBBLE_STATE_OVERLAP = "overlap"
_BUBBLE_STATE_ADJACENT = "adjacent"
_BUBBLE_STATE_DETACHED = "detached"
_BUBBLE_ATTACH_GAP_LOGICAL_PX = 8.0

# Bubble scheme colors (wh-overlay-bubble-badges): (fill, digit, border) per
# bubble scheme. The digit is a plain fill in the digit color -- no outline
# pen -- because the bubble fill provides the contrast, so the old
# outline-swallows-glyph failure mode cannot occur for numerals.
_BUBBLE_SCHEME_COLORS: "dict[str, tuple[QColor, QColor, QColor]]" = {
    _BADGE_THEME_LIGHT: (
        QColor(255, 255, 255, 255),
        QColor(0, 0, 0, 255),
        QColor(102, 102, 102, 255),
    ),
    _BADGE_THEME_DARK: (
        QColor(32, 32, 32, 255),
        QColor(255, 255, 255, 255),
        QColor(170, 170, 170, 255),
    ),
}
# Corner radius as a fraction of the bubble's height, and the border stroke
# width. The border equals _NUMERAL_OUTLINE_PX so the decoration margin
# _numeral_badge_size reserves is exactly the pen half-width the stroke needs.
_BUBBLE_CORNER_RADIUS_FACTOR = 0.3
_BUBBLE_BORDER_PX = _NUMERAL_OUTLINE_PX
# Pointer tail: base width on the bubble edge; how far past the control's
# nearest boundary point the apex reaches (i.e. INTO the control); and how far
# the base is rooted inside the bubble so the triangle overlaps the rounded
# rect and the union has no seam at a rounded corner.
_BUBBLE_TAIL_BASE_LOGICAL_PX = 8.0
_BUBBLE_TAIL_APEX_INSET_LOGICAL_PX = 3.0
_BUBBLE_TAIL_ROOT_INSET_LOGICAL_PX = 2.0
# Inward tail (the OVERLAP state, or a degenerate zero anchor distance): how
# far the apex reaches past the bubble's edge toward the center of the
# control's ON-monitor part. May exceed the badge box by up to (this minus
# the 5.25 logical-px box margin), so the overhang must land inside the
# on-monitor control -- the region _compute_monitor_bbox preserves after its
# monitor clamp. The reach is therefore confined to the slab interval where
# the aim ray is inside the clipped control (an exit-only cap is not enough:
# the ray can leave the bubble outside the control and enter it only past
# this reach, wh-overlay-bubble-badges.3.1 and .3.3); the slab rectangle is
# further inset from the monitor edges by the rendered-ink extents (pen
# half-width left/top, shadow offset right/bottom), because the border
# stroke and shadow paint past the apex point and an apex ON a monitor edge
# still bleeds clipped ink (wh-overlay-bubble-badges.3.4); when no point of
# the reach lies in that interval, the apex falls back to the badge-box ink
# budget (box margin minus the shadow offset, 3.25 logical px), which
# placement keeps on-monitor unconditionally.
_BUBBLE_TAIL_INWARD_APEX_LOGICAL_PX = 7.0
# Leader line: a border-color outer stroke with a fill-color core.
_LEADER_OUTER_LOGICAL_PX = 3.0
_LEADER_CORE_LOGICAL_PX = 1.5

# ---------------------------------------------------------------------------
# Mouse-grid drawing mode (wh-grid-paint-mode). Spec:
# docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md -- "Grid
# styling as user configuration" is deliberately out of scope, so these are
# hard-coded constants beside the numbered overlay's styling, in LOGICAL
# pixels (every use multiplies by the target monitor's dpr).
# ---------------------------------------------------------------------------

# The grid lines and the pin are stroked TWICE -- a wide dark outer stroke
# with a narrow light core on top -- exactly like the numbered badge's leader
# line (_LEADER_OUTER_LOGICAL_PX / _LEADER_CORE_LOGICAL_PX). That is what
# makes a one-pixel-thin line readable over both a white document and a dark
# editor without knowing anything about the pixels underneath; a single-color
# line disappears against a background of its own color. The grid covers a
# whole monitor, so it always crosses both kinds of content at once and a
# theme-dependent single color could never work.
_GRID_LINE_OUTER_LOGICAL_PX = 3.0
_GRID_LINE_CORE_LOGICAL_PX = 1.25
# Reuses the numbered overlay's palette choices: white ink, black surround.
_GRID_LINE_OUTER_COLOR = _OUTLINE_COLOR
_GRID_LINE_CORE_COLOR = _NUMERAL_COLOR

# Cell label size. The label is proportional to the SHORTER side of the cell
# so a wide-but-short cell (a 16:9 monitor's cell is 1.78:1) never gets a
# digit taller than the cell, then clamped: the floor keeps a deeply refined
# cell's digit legible, and the cap stops a full-monitor cell (640x360 on a
# 1080p screen) from painting a 144-pixel numeral over the user's window.
_GRID_LABEL_CELL_FACTOR = 0.4
_GRID_LABEL_MIN_LOGICAL_PX = 11.0
_GRID_LABEL_MAX_LOGICAL_PX = 72.0
# The label's outline pen, as a FRACTION of the label's pixel size rather
# than an absolute width. The numbered badge learned this the hard way: an
# absolute 3px outline is stroked centered on the glyph edge, so half of it
# eats inward and a small numeral reads as a solid black blob
# (wh-dictation-retraction-indicator.11). A proportional width keeps the same
# outline-to-glyph ratio at every cell size, from a full-monitor cell down to
# the refinement floor. The absolute floor keeps the outline visible at all.
_GRID_LABEL_OUTLINE_FACTOR = 0.07
_GRID_LABEL_OUTLINE_MIN_LOGICAL_PX = 1.0

# The drag-anchor pin: a crosshair (two arms crossing at the point) inside a
# small ring. The arms locate the exact pixel the drag starts from -- a
# filled dot would hide it -- and the ring makes the mark findable on a busy
# screen.
_GRID_PIN_ARM_LOGICAL_PX = 14.0
_GRID_PIN_RADIUS_LOGICAL_PX = 5.0

# Transparent breathing room added around the drawn rectangle when sizing the
# per-monitor paint surface, so the outer stroke on the grid's own border
# (stroked centered, half of it outside the rectangle) and its anti-aliased
# edge are not clipped. Half of _GRID_LINE_OUTER_LOGICAL_PX plus slack. The
# same role _SURFACE_MARGIN_PX plays for badges.
_GRID_INK_MARGIN_LOGICAL_PX = 4.0


def _resolve_bubble_scheme(badge_theme: str, system_scheme: Qt.ColorScheme) -> str:
    """Map the validated overlay_badge_theme plus the system color scheme to
    the bubble's OWN scheme -- ``_BADGE_THEME_LIGHT`` or ``_BADGE_THEME_DARK``,
    never "auto".

    "auto" INVERTS the system theme for contrast: a system dark theme means
    mostly-dark screen content, so the bubble goes light (white); a system
    light theme gets the near-black bubble. An unknown scheme falls back to
    the light bubble. Pinned "light"/"dark" ignore the system scheme -- the
    override for content that does not match the system theme (a dark editor
    on a light-themed system)."""
    if badge_theme == _BADGE_THEME_LIGHT or badge_theme == _BADGE_THEME_DARK:
        return badge_theme
    if system_scheme == Qt.ColorScheme.Light:
        return _BADGE_THEME_DARK
    return _BADGE_THEME_LIGHT


def _system_color_scheme() -> Qt.ColorScheme:
    """The Windows app light/dark scheme via Qt's style hints (Qt >= 6.5).

    The one seam that touches ``QGuiApplication.styleHints()``; read fresh at
    each paint so a system theme switch shows on the next repaint. Degrades to
    ``Unknown`` (never raises) so a paint cannot crash on the theme read."""
    try:
        return QGuiApplication.styleHints().colorScheme()
    except Exception:  # noqa: BLE001 - theme read is best-effort
        return Qt.ColorScheme.Unknown


def _bubble_drawing_state(
    bubble: tuple[float, float, float, float],
    control: tuple[float, float, float, float],
    dpr: float,
) -> str:
    """Pick the drawing state for one badge (see the state constants above).

    Both rectangles are ``(left, top, right, bottom)`` in PHYSICAL pixels;
    the threshold is logical, so it is scaled by ``dpr``. The gap is the
    shortest straight-line distance between the two rectangles -- zero when
    they touch. Touching is NOT area overlap (matching
    ``OverlayPaintWindowManager._rects_overlap_phys``), so a bubble placed
    flush against its control's edge -- the common trailing-space placement --
    gets the pointer tail."""
    if OverlayPaintWindowManager._rects_overlap_phys(bubble, control):
        return _BUBBLE_STATE_OVERLAP
    gap_x = max(0.0, bubble[0] - control[2], control[0] - bubble[2])
    gap_y = max(0.0, bubble[1] - control[3], control[1] - bubble[3])
    gap = math.hypot(gap_x, gap_y)
    if gap <= _BUBBLE_ATTACH_GAP_LOGICAL_PX * dpr:
        return _BUBBLE_STATE_ADJACENT
    return _BUBBLE_STATE_DETACHED


def _bubble_anchor_points(
    bubble: tuple[float, float, float, float],
    control: tuple[float, float, float, float],
) -> "tuple[tuple[float, float], tuple[float, float]]":
    """The attachment anchors between a bubble and its control, in PHYSICAL
    pixels: the center of the bubble edge nearest the control, and the point
    on the control rectangle nearest that edge center. The pointer tail grows
    from the first toward (and past) the second; the leader line connects the
    two. Both rectangles are ``(left, top, right, bottom)``."""
    bl, bt, br, bb = bubble
    cx0, cy0, cx1, cy1 = control
    edge_centers = (
        (bl, (bt + bb) / 2.0),
        (br, (bt + bb) / 2.0),
        ((bl + br) / 2.0, bt),
        ((bl + br) / 2.0, bb),
    )
    best_pa = edge_centers[0]
    best_pb = best_pa
    best_dist: Optional[float] = None
    for pa in edge_centers:
        pb = (
            min(max(pa[0], cx0), cx1),
            min(max(pa[1], cy0), cy1),
        )
        dist = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        if best_dist is None or dist < best_dist:
            best_pa, best_pb, best_dist = pa, pb, dist
    return best_pa, best_pb


def _leader_anchor_points(
    bubble: tuple[float, float, float, float],
    control: tuple[float, float, float, float],
    mon_w_phys: float,
    mon_h_phys: float,
) -> "tuple[tuple[float, float], tuple[float, float]]":
    """The endpoints of a DETACHED badge's leader line, in PHYSICAL pixels:
    the center of the bubble edge nearest the target, and the CENTER of the
    control's on-monitor part (wh-taskbar-badge-mispoint).

    This is deliberately NOT :func:`_bubble_anchor_points` (which the pointer
    tail keeps): the nearest POINT on the control's rectangle is wrong for a
    leader line. A taskbar button reports a full-row rectangle with its
    visible icon centered inside, so a nearest-point endpoint lands on blank
    pixels at the row's edge -- and two stacked rows share a corner, so two
    leader lines converge on the SAME point and neither reads as labeling
    anything. The visible-part center is distinct per control and sits on the
    glyph the user actually sees.

    The control is clipped to the monitor (``[0, mon_w_phys] x
    [0, mon_h_phys]``, monitor-local like every placement rectangle) before
    taking the center, so a control overhanging the monitor edge aims the
    line at the middle of its VISIBLE part, not at off-screen pixels. A
    control entirely off the monitor (nothing visible to aim at) falls back
    to its raw center. Both rectangles are ``(left, top, right, bottom)``."""
    cx0, cy0, cx1, cy1 = control
    vx0, vy0 = max(cx0, 0.0), max(cy0, 0.0)
    vx1, vy1 = min(cx1, mon_w_phys), min(cy1, mon_h_phys)
    if vx1 <= vx0 or vy1 <= vy0:
        pb = ((cx0 + cx1) / 2.0, (cy0 + cy1) / 2.0)
    else:
        pb = ((vx0 + vx1) / 2.0, (vy0 + vy1) / 2.0)
    bl, bt, br, bb = bubble
    edge_centers = (
        (bl, (bt + bb) / 2.0),
        (br, (bt + bb) / 2.0),
        ((bl + br) / 2.0, bt),
        ((bl + br) / 2.0, bb),
    )
    pa = min(
        edge_centers, key=lambda p: math.hypot(pb[0] - p[0], pb[1] - p[1])
    )
    return pa, pb


def _ray_rect_interval(
    px: float,
    py: float,
    ux: float,
    uy: float,
    rect: tuple[float, float, float, float],
) -> tuple[float, float]:
    """The parameter interval ``[t_enter, t_exit]`` where the line
    ``(px, py) + t * (ux, uy)`` lies inside the axis-aligned ``(left, top,
    right, bottom)`` rectangle (slab intersection, one slab per axis). Empty
    is signaled by ``t_exit < t_enter``: an axis-parallel ray whose fixed
    coordinate misses the rectangle's span, an INVERTED rectangle (right <
    left or bottom < top, the empty result of an intersection), or a line
    that never crosses the rectangle all return ``(inf, -inf)`` or a crossed
    pair the caller's ``t_exit < t_enter`` check rejects. A zero-width or
    zero-height rectangle is a valid degenerate slab (a segment): a line
    crossing it returns the point interval where it does."""
    if rect[2] < rect[0] or rect[3] < rect[1]:
        return (math.inf, -math.inf)
    t_enter = -math.inf
    t_exit = math.inf
    for p_axis, u_axis, lo, hi in (
        (px, ux, rect[0], rect[2]),
        (py, uy, rect[1], rect[3]),
    ):
        if u_axis == 0.0:
            if not (lo <= p_axis <= hi):
                return (math.inf, -math.inf)
            continue
        t0 = (lo - p_axis) / u_axis
        t1 = (hi - p_axis) / u_axis
        if t0 > t1:
            t0, t1 = t1, t0
        t_enter = max(t_enter, t0)
        t_exit = min(t_exit, t1)
    return (t_enter, t_exit)

# Working/busy badge (wh-dictation-retraction-indicator.2): a single static
# glyph painted at an arbitrary screen point to signal that dictated text is
# still provisional (could be retracted by the final). It rides the SAME paint
# pipeline as the numbered badges; this sentinel display number routes the
# badge render to the working glyph instead of a numeral. -1 can never collide
# with a real 1-based overlay number.
WORKING_BADGE_NUMBER = -1
# Perceived on-screen HEIGHT (vertical extent) of the working badge, in
# LOGICAL (device-independent) pixels. paint_working_badge multiplies this by
# the target monitor's device pixel ratio to build the physical box centered
# on the point, so the badge keeps the SAME perceived size across mixed-DPI
# monitors instead of shrinking on hi-DPI displays. On a 200% monitor it is
# rendered at 64 physical px, which looks the same size as 32 px at 100%.
# Halved from the original 64 at the user's request; _HOURGLASS_OUTLINE_PX
# was retuned in the same change so the outline does not swallow the smaller
# shape the way the original 3px outline did at 36
# (wh-dictation-retraction-indicator.11).
WORKING_BADGE_LOGICAL_PX = 32
# Perceived on-screen WIDTH (horizontal extent), in the same LOGICAL px unit.
# A live check of the halved 32x32 box found it too wide even though the 32px
# vertical size read fine, so width is narrowed to ~70% of the height
# (32 * 0.7 = 22.4, rounded to 22) instead of matching it. Independent from
# WORKING_BADGE_LOGICAL_PX so the vertical size can stay unchanged.
# _render_working_glyph insets and sizes the top/bottom cap bars from the
# HEIGHT only and the left/right span from the WIDTH only, so narrowing the
# width alone does not change the vertical proportions.
WORKING_BADGE_WIDTH_LOGICAL_PX = 22


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


@dataclass(frozen=True, slots=True)
class _TargetPaintRect(OverlayPaintRect):
    """A resolved paint rect that also carries what the wide-row rule reads
    about its control (wh-vscode-menu-badge-misplaced).

    ``_do_paint`` builds one for each summary item that has a name, so the
    placement code can read the name and the walk-time mark without a new
    argument on the placement methods or a third member in the
    ``(rect, display_number)`` badge tuples. A plain ``OverlayPaintRect`` (the
    working badge, a direct caller) never takes the wide-row path.

    ``target_name`` is the control's accessible name; its first strong bidi
    character decides which side is the leading side. ``bounds_outside_menu``
    is the summary item's field of the same name: True means the rectangle may
    be wrong, so the badge keeps its normal placement.
    """

    target_name: str = ""
    bounds_outside_menu: bool = False


def _name_is_right_to_left(name: str) -> bool:
    """True when the first strong bidi character of ``name`` is right-to-left.

    Bidi class ``R`` (Hebrew and similar) or ``AL`` (Arabic letters) means
    right-to-left; ``L`` means left-to-right. Digits, spaces, and punctuation
    are weak or neutral and are skipped. A name with no strong character is
    left-to-right.
    """
    for char in name:
        direction = unicodedata.bidirectional(char)
        if direction in ("R", "AL"):
            return True
        if direction == "L":
            return False
    return False


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
        badge_font_pt: int = 10,
        badge_shadow: bool = False,
        badge_corner: str = _DEFAULT_BADGE_CORNER,
        badge_trailing_space: bool = _DEFAULT_BADGE_TRAILING_SPACE,
        badge_theme: str = _DEFAULT_BADGE_THEME,
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
        # The bubble color scheme (see _DEFAULT_BADGE_THEME). ClickConfig
        # already validates the config value, but normalize defensively so an
        # unexpected string can never select an unknown scheme.
        self._badge_theme = (
            badge_theme if badge_theme in _VALID_BADGE_THEMES
            else _DEFAULT_BADGE_THEME
        )

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
        # The mouse grid's retained drawing (wh-grid-paint-mode). The grid and
        # its pin arrive as SEPARATE Logic -> GUI events, and neither may
        # erase the other: "mark" paints a pin and then resets the grid to the
        # full monitor, so a paint_grid immediately follows a paint_grid_pin
        # and both must end up on screen. UpdateLayeredWindow replaces the
        # WHOLE window surface per call, so there is no way to add the pin to
        # an already-composited surface -- the manager therefore keeps the
        # last of each and repaints BOTH on every grid event. That is the only
        # state the grid mode holds, and it is a memory of what was drawn, not
        # a decision: no cell arithmetic, no refinement tracking, no monitor
        # choice. Both are dropped by clear_grid.
        self._grid_event: Optional[Any] = None
        self._grid_pin: Optional[Any] = None
        # Windows whose destroy() failed on the REBUILD path (monitor
        # moved/resized): the new window claims the hmonitor slot in
        # _windows, so the still-on-screen old window cannot be retained
        # there. It is parked here and retried by _destroy_all (called by
        # clear / clear_all / paint's except-path) so it is not orphaned
        # (wh-n29v.55.4 rebuild-path gap).
        self._pending_destroy: list[_OverlayWindow] = []
        # Teardown-completion tracking (wh-overlay-slow-uia-stale-badges.18.4).
        # _teardown_pending is True only while a TEARDOWN (clear /
        # expire_lease / reset) left a window whose DestroyWindow failed.
        # A container check cannot stand in for it: _windows non-empty is
        # the NORMAL painted state. Any _destroy_all sweep that ends with
        # both containers empty clears the flag. _deferred_teardown_ack
        # parks the cleared/expired event an incomplete teardown withheld;
        # retry_teardown releases it once a sweep ends clean.
        self._teardown_pending = False
        self._deferred_teardown_ack: Optional[dict[str, Any]] = None

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
        Single source of truth shared by ``_draw_numeral_bubble`` (which draws
        the digit) and ``_numeral_badge_size`` (which sizes the badge box)."""
        font = QFont()
        font.setPointSizeF(self._badge_font_pt * dpr)
        font.setBold(True)
        return font

    @staticmethod
    def _badge_box_margin_phys(dpr: float) -> float:
        """Per-side decoration margin between the badge BOX and the visible
        bubble, in physical px: the border pen, the drop shadow, and
        antialiasing slack. Single source shared by ``_numeral_badge_size``
        (which reserves it) and the bubble/leader drawing (which insets by
        it), so the box and the drawing cannot disagree."""
        return _NUMERAL_OUTLINE_PX * dpr + _SHADOW_OFFSET_PX * dpr + 2.0 * dpr

    def _numeral_badge_size(
        self,
        number: int,
        dpr: float,
        metrics: Optional[QFontMetricsF] = None,
    ) -> tuple[int, int]:
        """Physical-pixel size of a numeral badge: the BUBBLE box
        (wh-overlay-bubble-badges). The glyph advance and cap height, plus the
        bubble's proportional padding per side (``_BUBBLE_PAD_X_CAP_FACTOR`` /
        ``_BUBBLE_PAD_Y_CAP_FACTOR`` x cap height, floored at
        ``_BUBBLE_PAD_MIN_LOGICAL_PX`` logical px), plus a per-side margin for
        the border pen, the drop shadow, and antialiasing slack. Sized to the
        bubble, not to the control it labels, so a number over a large control
        does not allocate a control-sized (dpr^2) transient surface region
        (wh-overlay-badge-alloc-decouple). Because the placement pass and the
        collision nudges consume this size, they operate on the bubble's true
        box with no placement-code changes.

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
        pad_x = max(_BUBBLE_PAD_X_CAP_FACTOR * cap_h, _BUBBLE_PAD_MIN_LOGICAL_PX * dpr)
        pad_y = max(_BUBBLE_PAD_Y_CAP_FACTOR * cap_h, _BUBBLE_PAD_MIN_LOGICAL_PX * dpr)
        margin = self._badge_box_margin_phys(dpr)
        w = int(math.ceil(text_w + 2.0 * pad_x + 2.0 * margin))
        h = int(math.ceil(cap_h + 2.0 * pad_y + 2.0 * margin))
        return max(1, w), max(1, h)

    def _render_badge(
        self, number: int, width: int, height: int, dpr: float = 1.0
    ) -> QImage:
        """Render the WORKING badge image: the ``WORKING_BADGE_NUMBER``
        sentinel routes to ``_render_working_glyph`` (the busy/working
        hourglass, wh-dictation-retraction-indicator.2), sized ``width`` x
        ``height`` in PHYSICAL pixels with ``dpr`` scaling the outline pen
        and shadow offset.

        Numerals no longer render here: each numeral badge is a speech-bubble
        ``QPainterPath`` drawn directly on the per-monitor surface by
        ``_draw_numeral_bubble`` (wh-overlay-bubble-badges), so a per-badge
        image exists only for the working glyph. A numeral argument is a
        programming error and raises, so nothing can silently exercise the
        deleted numeral-image path (``paint`` maps an unexpected raise to a
        "failed" state).
        """
        if number != WORKING_BADGE_NUMBER:
            raise ValueError(
                "numeral badges draw as bubbles on the monitor surface "
                "(_draw_numeral_bubble); _render_badge renders only the "
                "working glyph"
            )
        return self._render_working_glyph(width, height, dpr)

    def _draw_numeral_bubble(
        self,
        painter: QPainter,
        number: int,
        placement: "tuple[int, int, tuple[float, float, float, float]]",
        control_phys: "tuple[float, float, float, float]",
        dpr: float,
        scheme: str,
        metrics: QFontMetricsF,
        font: QFont,
        mon_w_phys: float,
        mon_h_phys: float,
    ) -> None:
        """Draw ONE numeral badge as a speech bubble directly on the monitor
        surface (wh-overlay-bubble-badges): a rounded rectangle inset from the
        badge box by the decoration margin, merged with a pointer tail unless
        the box is DETACHED (``_bubble_drawing_state``) -- toward the control
        across the gap when ADJACENT, inward toward the control's center when
        the box OVERLAPS it -- painted shadow -> fill -> border stroke, then
        the digit filled centered in the bubble. Coordinates are monitor-local
        PHYSICAL pixels (the caller's painter is already translated to
        bounding-box-local).

        Drawing the path directly replaces the per-badge tight numeral image:
        no per-badge allocation at all, and a tail can never be clipped by an
        image edge. The badge box from ``_numeral_badge_size`` still bounds
        the bubble and its decoration, so the placement, collision, and
        bounding-box math are unchanged.
        """
        fill, digit, border = _BUBBLE_SCHEME_COLORS[scheme]
        _bw, _bh, (fl, ft, fr, fb) = placement
        margin = self._badge_box_margin_phys(dpr)
        bubble = QRectF(
            fl + margin,
            ft + margin,
            (fr - fl) - 2.0 * margin,
            (fb - ft) - 2.0 * margin,
        )
        radius = _BUBBLE_CORNER_RADIUS_FACTOR * bubble.height()
        path = QPainterPath()
        path.addRoundedRect(bubble, radius, radius)

        state = _bubble_drawing_state((fl, ft, fr, fb), control_phys, dpr)
        if state in (_BUBBLE_STATE_ADJACENT, _BUBBLE_STATE_OVERLAP):
            path = path.united(
                self._bubble_tail_path(
                    bubble, control_phys, dpr, mon_w_phys, mon_h_phys
                )
            )

        if self._badge_shadow:
            shadow = QPainterPath(path)
            shadow_off = _SHADOW_OFFSET_PX * dpr
            shadow.translate(shadow_off, shadow_off)
            painter.fillPath(shadow, _SHADOW_COLOR)
        border_pen = QPen(border)
        border_pen.setWidthF(_BUBBLE_BORDER_PX * dpr)
        border_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(border_pen)
        painter.setBrush(fill)
        painter.drawPath(path)

        # The digit, centered in the BUBBLE (not the badge box) so a tail
        # never shifts it. A plain fill in the digit color: the bubble fill
        # provides the contrast, so there is no outline pen that could swallow
        # the glyph. ``metrics`` and ``font`` are the surface-shared instances
        # (wh-overlay-4bug-review.2, wh-overlay-bubble-badges.1.1).
        text = str(number)
        text_path = QPainterPath()
        text_path.addText(
            bubble.center().x() - metrics.horizontalAdvance(text) / 2.0,
            bubble.center().y() + metrics.capHeight() / 2.0,
            font,
            text,
        )
        painter.fillPath(text_path, digit)

    def _bubble_tail_path(
        self,
        bubble: QRectF,
        control_phys: "tuple[float, float, float, float]",
        dpr: float,
        mon_w_phys: float,
        mon_h_phys: float,
    ) -> QPainterPath:
        """The pointer-tail triangle for an ADJACENT or OVERLAPPING bubble.
        The base is ``_BUBBLE_TAIL_BASE_LOGICAL_PX`` wide on the bubble edge
        nearest the control, rooted slightly INSIDE the bubble so the union
        with the rounded rect has no seam at a rounded corner.

        Every apex must land where the surface clamp preserves ink: the
        surface bounding box is the union of control rects and badge boxes
        CLAMPED to the monitor (``_compute_monitor_bbox``), and placement
        clamps every badge box onto the monitor, so the safe region is the
        badge box union the control's ON-monitor part -- never the raw
        control, which can continue past a monitor edge
        (wh-overlay-bubble-badges.3.1, .3.3). And because the border pen and
        the shadow paint INK past the apex point itself, the apex must
        additionally stop short of the monitor edges by the rendered-ink
        extents -- half the pen width on the left and top, the shadow offset
        on the right and bottom (wh-overlay-bubble-badges.3.4).

        Adjacent (anchor distance nonzero): the apex ends
        ``_BUBBLE_TAIL_APEX_INSET_LOGICAL_PX`` past the control's nearest
        boundary point -- just inside the control, which is what makes the
        bubble read as pointing AT it -- shortened where the stroke or
        shadow would leave the control's ink-inset on-monitor part (a
        control hanging off a monitor edge, or thinner than the reach). When
        the anchor itself sits in a monitor-edge ink band (an on-monitor
        sliver thinner than the inset), no apex at the control is safe, so
        the apex falls back to the badge-box ink budget measured from the
        bubble anchor.

        Overlapping (anchor distance zero -- the bubble sits ON the control,
        so there is no gap to span): the tail aims from the bubble's edge
        inward toward the center of the control's ON-monitor part, the apex
        reaching ``_BUBBLE_TAIL_INWARD_APEX_LOGICAL_PX`` past the edge (user
        decision 2026-08-07, matching Voice Access), kept inside the safe
        region by the slab cap below; when no point of that reach lies in
        the ink-inset on-monitor control (the ray enters it only past the
        requested reach, or never), the apex falls back to the badge-box ink
        budget so a visible tail always remains and can never be cut
        flat."""
        rect = (bubble.left(), bubble.top(), bubble.right(), bubble.bottom())
        (pax, pay), (pbx, pby) = _bubble_anchor_points(rect, control_phys)
        # The control's on-monitor part, in monitor-local physical px. Only
        # ink inside it (or inside the badge box) survives the surface clamp.
        clipped = (
            max(control_phys[0], 0.0),
            max(control_phys[1], 0.0),
            min(control_phys[2], mon_w_phys),
            min(control_phys[3], mon_h_phys),
        )
        # Where the apex may LAND is tighter than ``clipped``: the border pen
        # is centered on the path (half its width of ink past the apex on
        # every side, with a round join at the vertex) and the shadow is the
        # whole path translated down-right by the shadow offset, so an apex
        # exactly ON a monitor edge still paints stroke and shadow past it,
        # which the surface clamp cuts flat (wh-overlay-bubble-badges.3.4).
        # ``apex_safe`` is the control intersected with the monitor rect
        # inset by those per-side ink extents. Control edges interior to the
        # monitor need no inset: the surface bounding box pads every element
        # by the full decoration margin.
        pen_half = _BUBBLE_BORDER_PX * dpr / 2.0
        shadow_off = _SHADOW_OFFSET_PX * dpr
        apex_safe = (
            max(control_phys[0], pen_half),
            max(control_phys[1], pen_half),
            min(control_phys[2], mon_w_phys - shadow_off),
            min(control_phys[3], mon_h_phys - shadow_off),
        )
        dx = pbx - pax
        dy = pby - pay
        length = math.hypot(dx, dy)
        if length > 0.0:
            ux, uy = dx / length, dy / length
            apex_reach = _BUBBLE_TAIL_APEX_INSET_LOGICAL_PX * dpr
            if (
                apex_safe[0] <= pbx <= apex_safe[2]
                and apex_safe[1] <= pby <= apex_safe[3]
            ):
                # The anchor point is on the raw control's boundary and
                # inside the ink-inset monitor, so it is on the safe
                # rectangle's boundary or inside it; the exit distance caps
                # the apex where stroke or shadow would leave the preserved
                # region.
                _t_enter, t_exit = _ray_rect_interval(
                    pbx, pby, ux, uy, apex_safe
                )
                apex_reach = min(apex_reach, max(t_exit, 0.0))
                apex = QPointF(pbx + ux * apex_reach, pby + uy * apex_reach)
            else:
                # The anchor sits in a monitor-edge ink band (an on-monitor
                # sliver thinner than the ink inset): no apex position at
                # the control keeps the stroke and shadow on the monitor, so
                # fall back to the badge-box ink budget measured from the
                # BUBBLE anchor -- always safe (placement clamps the box
                # onto the monitor), always visible.
                fallback = self._badge_box_margin_phys(dpr) - shadow_off
                reach = min(length + apex_reach, fallback)
                apex = QPointF(pax + ux * reach, pay + uy * reach)
        else:
            # Inward tail: aim from the bubble's center toward the center of
            # the control's ON-monitor part (diagonal down-right when they
            # coincide) -- the raw center can sit far off-monitor and point
            # the tail at pixels the user cannot see -- and root the base
            # where that ray exits the bubble's rectangle.
            cx, cy = bubble.center().x(), bubble.center().y()
            dx = (clipped[0] + clipped[2]) / 2.0 - cx
            dy = (clipped[1] + clipped[3]) / 2.0 - cy
            length = math.hypot(dx, dy)
            if length <= 0.0:
                dx, dy = 1.0, 1.0
                length = math.hypot(dx, dy)
            ux, uy = dx / length, dy / length
            hw = bubble.width() / 2.0
            hh = bubble.height() / 2.0
            tx = hw / abs(ux) if ux != 0.0 else float("inf")
            ty = hh / abs(uy) if uy != 0.0 else float("inf")
            t = min(tx, ty)
            pax, pay = cx + ux * t, cy + uy * t
            requested = _BUBBLE_TAIL_INWARD_APEX_LOGICAL_PX * dpr
            # Keep the apex (and its stroke and shadow ink) inside the
            # safe rectangle: the reach may use [t_enter, t_exit] only when
            # that interval is ahead of the base AND starts within the
            # requested reach. An exit-only cap is not enough -- the base is
            # where the ray exits the BUBBLE, which can sit outside the
            # control even at anchor distance zero (the zero-distance anchor
            # is a different bubble-edge point), so the ray may enter the
            # control only PAST the requested reach and an apex at the
            # requested reach lands between bubble and control, where the
            # monitor clamp cuts it flat (wh-overlay-bubble-badges.3.3).
            t_enter, t_exit = _ray_rect_interval(pax, pay, ux, uy, apex_safe)
            if t_exit >= max(t_enter, 0.0) and max(t_enter, 0.0) <= requested:
                apex_reach = min(requested, t_exit)
            else:
                apex_reach = 0.0
            # Unconditional floor: an apex within the badge box is always
            # safe (placement clamps the box onto the monitor and the box is
            # unioned into the surface), so a visible tail always remains.
            # The box margin is the full decoration budget; the shadow
            # offset is the largest ink extent past the apex point (the
            # border pen's half-width is smaller), so the floor is the
            # margin minus the shadow offset: 3.25 logical px at the current
            # constants.
            fallback = self._badge_box_margin_phys(dpr) - shadow_off
            apex_reach = max(apex_reach, min(requested, fallback))
            apex = QPointF(pax + ux * apex_reach, pay + uy * apex_reach)
        root_inset = _BUBBLE_TAIL_ROOT_INSET_LOGICAL_PX * dpr
        root_x = pax - ux * root_inset
        root_y = pay - uy * root_inset
        half = _BUBBLE_TAIL_BASE_LOGICAL_PX * dpr / 2.0
        # Base endpoints sit half the base width along the unit perpendicular,
        # clamped into the bubble so a short bubble edge cannot let the base
        # poke past the rounded corners.
        perp_x, perp_y = -uy, ux
        base_ax = min(max(root_x + perp_x * half, bubble.left()), bubble.right())
        base_ay = min(max(root_y + perp_y * half, bubble.top()), bubble.bottom())
        base_bx = min(max(root_x - perp_x * half, bubble.left()), bubble.right())
        base_by = min(max(root_y - perp_y * half, bubble.top()), bubble.bottom())
        tail = QPainterPath()
        tail.moveTo(QPointF(base_ax, base_ay))
        tail.lineTo(apex)
        tail.lineTo(QPointF(base_bx, base_by))
        tail.closeSubpath()
        return tail

    def _draw_leader_line(
        self,
        painter: QPainter,
        placement: "tuple[int, int, tuple[float, float, float, float]]",
        control_phys: "tuple[float, float, float, float]",
        dpr: float,
        scheme: str,
        mon_w_phys: float,
        mon_h_phys: float,
    ) -> None:
        """Draw a DETACHED badge's leader line: from the center of the bubble
        edge nearest the target to the center of the control's on-monitor
        part (``_leader_anchor_points``; wh-taskbar-badge-mispoint), a
        border-color outer stroke under a fill-color core so the line reads
        in the same palette as its bubble on any background. The render pass
        draws EVERY leader line before ANY bubble, so a line can end under a
        bubble but never crosses over one."""
        fill, _digit, border = _BUBBLE_SCHEME_COLORS[scheme]
        _bw, _bh, (fl, ft, fr, fb) = placement
        margin = self._badge_box_margin_phys(dpr)
        bubble = (fl + margin, ft + margin, fr - margin, fb - margin)
        (pax, pay), (pbx, pby) = _leader_anchor_points(
            bubble, control_phys, mon_w_phys, mon_h_phys
        )
        if pax == pbx and pay == pby:
            return
        line = QLineF(pax, pay, pbx, pby)
        outer_pen = QPen(border)
        outer_pen.setWidthF(_LEADER_OUTER_LOGICAL_PX * dpr)
        outer_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(outer_pen)
        painter.drawLine(line)
        core_pen = QPen(fill)
        core_pen.setWidthF(_LEADER_CORE_LOGICAL_PX * dpr)
        core_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(core_pen)
        painter.drawLine(line)

    def _render_working_glyph(
        self, width: int, height: int, dpr: float = 1.0
    ) -> QImage:
        """Render the working/busy glyph (a static hourglass) as a
        premultiplied ARGB32 ``QImage`` sized ``width`` x ``height`` (physical
        pixels: ``_render_monitor_surface`` passes ``logical * dpr``, and the
        working badge box is ``WORKING_BADGE_WIDTH_LOGICAL_PX * dpr`` by
        ``WORKING_BADGE_LOGICAL_PX * dpr`` physical px, so the perceived size
        is constant across mixed-DPI monitors).

        Same visual treatment as the numeral badge -- white fill, black
        outline, optional drop shadow, transparent background (no box) -- but
        a shape rather than a numeral, so it reads as "busy / still settling"
        instead of a count. v1 does NOT animate, so the shape must read as
        "wait" while frozen; an hourglass silhouette (two end-cap bars and two
        funnels meeting at the center) is the universally recognized static
        busy glyph (wh-dictation-retraction-indicator.2).

        ``dpr`` is the monitor's scaling factor. The hourglass GEOMETRY (insets,
        cap bars, funnels) is proportional to ``width``/``height`` and already
        scales with the physical image size, but the outline pen width
        (``_HOURGLASS_OUTLINE_PX``) and the shadow offset are absolute, so they
        are multiplied by ``dpr`` -- exactly as the numeral path does -- to keep
        the perceived stroke thickness constant at every scale
        (wh-glm52-proving-round.1). At ``dpr == 1.0`` the render is unchanged.

        ``width`` and ``height`` are inset INDEPENDENTLY (horizontal inset
        derived from ``width`` only, vertical inset from ``height`` only), so
        a narrower box (``WORKING_BADGE_WIDTH_LOGICAL_PX`` <
        ``WORKING_BADGE_LOGICAL_PX``) only compresses the shape horizontally;
        the vertical cap-bar/funnel proportions are unaffected by the box's
        width.
        """
        w = max(1, int(width))
        h = max(1, int(height))
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)  # fully transparent: no background box

        # Inset on every side so the outline/shadow are never clipped at the
        # image edge (and a corner pixel stays transparent -- this is a shape,
        # not a filled box). Horizontal and vertical insets are computed from
        # their own dimension so width and height can be tuned independently.
        inset_x = max(2.0, w * 0.18)
        inset_y = max(2.0, h * 0.18)
        left = inset_x
        right = w - inset_x
        top = inset_y
        bottom = h - inset_y
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
            outline_pen.setWidthF(_HOURGLASS_OUTLINE_PX * dpr)
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
        one-item synthetic summary whose bounds are a box
        ``WORKING_BADGE_WIDTH_LOGICAL_PX`` wide by ``WORKING_BADGE_LOGICAL_PX``
        tall (perceived, before the dpr scaling below) centered on the point,
        whose display number is the ``WORKING_BADGE_NUMBER`` sentinel (so
        ``_render_badge`` draws the working glyph, not a numeral), then delegates
        to ``paint``. The resolver places the box on whichever monitor the
        point lands on. A point off EVERY enumerated monitor paints nothing:
        this method checks monitor containment up front and, when no monitor
        contains the point, delegates an EMPTY summary to ``paint`` (which
        returns a ``painted`` event with no monitors). It must do this itself
        rather than relying on the resolver to drop the badge -- the box is
        ``WORKING_BADGE_WIDTH_LOGICAL_PX * dpr`` by
        ``WORKING_BADGE_LOGICAL_PX * dpr`` and centered on the point, so a
        point just past a monitor edge still OVERLAPS that monitor, and the
        resolver paints an overlapping box (clipped at the edge). Building the
        box for an off-monitor point would therefore paint a clipped glyph and
        report a painted monitor for an off-screen / stale point
        (wh-overlay-4bug-review-r2.1).

        The box is sized in LOGICAL pixels: the physical box is
        ``WORKING_BADGE_WIDTH_LOGICAL_PX * dpr`` by
        ``WORKING_BADGE_LOGICAL_PX * dpr`` where ``dpr`` is the device pixel
        ratio of the monitor the point lands on. The resolver divides the
        physical box back by that monitor's ``dpr`` and
        ``_render_monitor_surface`` multiplies by ``dpr`` again, so the glyph is
        rendered at that same physical size -- a CONSTANT PERCEIVED size on
        every monitor (a fixed physical box would instead shrink on hi-DPI;
        wh-dictation-retraction-indicator.11). The monitor topology is
        enumerated here once to find the monitor under the point and read its
        ``dpr`` (``paint`` enumerates again for the actual placement); a point
        on NO monitor paints nothing (see above).

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
        height_phys = max(1, int(round(WORKING_BADGE_LOGICAL_PX * dpr)))
        width_phys = max(1, int(round(WORKING_BADGE_WIDTH_LOGICAL_PX * dpr)))
        half_w = width_phys // 2
        half_h = height_phys // 2
        bounds = (
            center_x - half_w,
            center_y - half_h,
            width_phys,
            height_phys,
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
        # paths) and by retry_teardown's _destroy_pending (.18.10). This
        # prune does NOT pay off _teardown_pending; the next retry finds
        # the cohort empty, pays the debt, and releases the parked ack.
        # The geom_phys rebuild key (wh-n29v.62) makes rebuilds routine,
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
            # wh-vscode-menu-badge-misplaced: carry the control's name and the
            # walk-time mark to the placement code on the rect itself. Only a
            # summary item has a name; the working badge keeps a plain rect,
            # which never takes the wide-row path. Any value of the mark other
            # than exactly False counts as suspect, so a malformed item keeps
            # today's placement.
            target_name = getattr(item, "name", None)
            if isinstance(target_name, str) and isinstance(rect, OverlayPaintRect):
                rect = _TargetPaintRect(
                    **{f.name: getattr(rect, f.name) for f in fields(OverlayPaintRect)},
                    target_name=target_name,
                    bounds_outside_menu=(
                        getattr(item, "bounds_outside_menu", False) is not False
                    ),
                )
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

        When the canonical corner footprint (after the monitor clamp) lands on
        a NEIGHBORING numbered control, a fallback ladder runs before
        accepting the intrusion (wh-taskbar-badge-mispoint; the live trigger
        is a vertical right-edge taskbar, where every button reports a
        full-row rectangle: the half-outside corner-point badge of one row
        overhangs the row ABOVE, covering its icon, and the monitor clamp
        pushes the bottom sliver's badge up onto the control above it):

        1. The LEADING gutter -- the badge fully beside the control's leading
           edge (for a right corner, the badge's right edge on the control's
           left edge), top/bottom-aligned per the corner and vertically
           clamped onto the monitor. Fully outside, covering nothing.
        2. For a SMALL control (whose canonical spot is the half-outside
           corner point), the fully-INSIDE corner anchor: covering the
           control's own icon beats covering a neighbor's.
        3. The canonical footprint plus the collision nudge -- the shipped
           floor: an intruding badge is still unambiguous when attached.

        A ladder step is accepted only when clean of every other control AND
        every already-placed badge; a canonical spot clean of other controls
        skips the ladder entirely (the shipped placements are unchanged).
        """
        placed = placed_badges if placed_badges is not None else []

        def _hits_placed(c: tuple[float, float, float, float]) -> bool:
            return any(self._rects_overlap_phys(c, p) for p in placed)

        left_phys = rect.x * dpr
        top_phys = rect.y * dpr
        right_phys = (rect.x + rect.width) * dpr
        bottom_phys = (rect.y + rect.height) * dpr
        is_right = corner in (_BADGE_CORNER_TOP_RIGHT, _BADGE_CORNER_BOTTOM_RIGHT)
        is_bottom = corner in (
            _BADGE_CORNER_BOTTOM_LEFT, _BADGE_CORNER_BOTTOM_RIGHT
        )
        # The control's OWN box is excluded from the intrusion and nudge
        # avoidance: the corner anchor already sits on it, so landing there is
        # fine. Only OTHER controls' boxes make a digit read as labeling the
        # wrong control (wh-overlay-collision-review.1).
        own_box = (left_phys, top_phys, right_phys, bottom_phys)
        others = [b for b in ctrl_rects_phys if b != own_box]

        def _hits_others(c: tuple[float, float, float, float]) -> bool:
            return any(self._rects_overlap_phys(c, o) for o in others)

        def _clean_gutter(
            on_left: bool,
        ) -> Optional[tuple[float, float, float, float]]:
            """The badge fully beside the control's left (``on_left``) or
            right edge, top/bottom-aligned per the corner and vertically
            clamped onto the monitor; ``None`` when it leaves the monitor
            HORIZONTALLY or lands on another control or a placed badge."""
            g_left = left_phys - badge_w_phys if on_left else right_phys
            g_top = bottom_phys - badge_h_phys if is_bottom else top_phys
            if mon_h_phys is not None:
                g_top = max(0.0, min(g_top, mon_h_phys - badge_h_phys))
            gutter = (
                g_left, g_top,
                g_left + badge_w_phys, g_top + badge_h_phys,
            )
            on_monitor = gutter[0] >= 0.0 and (
                mon_w_phys is None or gutter[2] <= mon_w_phys
            )
            if (
                on_monitor
                and not _hits_others(gutter)
                and not _hits_placed(gutter)
            ):
                return gutter
            return None

        # Wide-row rule (wh-vscode-menu-badge-misplaced): a row at least
        # _WIDE_ROW_MIN_ASPECT times wider than tall puts the numeral in the
        # LEADING gutter -- left of the row for left-to-right text, right of
        # it for right-to-left text -- so the number sits by the label instead
        # of past the far edge. It trusts the row's rectangle, so it runs only
        # when the walk did not mark the rectangle suspect and the whole row
        # lies on its monitor. A gutter that fails the ladder step 1 checks
        # falls through to the placement below, unchanged.
        if (
            isinstance(rect, _TargetPaintRect)
            and not rect.bounds_outside_menu
            and mon_w_phys is not None
            and mon_h_phys is not None
            and left_phys >= 0.0
            and top_phys >= 0.0
            and right_phys <= mon_w_phys
            and bottom_phys <= mon_h_phys
            and (right_phys - left_phys)
            >= _WIDE_ROW_MIN_ASPECT * (bottom_phys - top_phys)
        ):
            leading = _clean_gutter(
                on_left=not _name_is_right_to_left(rect.target_name)
            )
            if leading is not None:
                return leading

        def _corner_resolved() -> tuple[float, float, float, float]:
            base = self._numeral_badge_footprint_phys(
                rect, badge_w_phys, badge_h_phys, dpr,
                mon_w_phys, mon_h_phys, corner=corner,
            )
            if _hits_others(base):
                # Ladder step 1: the leading gutter. Rejected only when it
                # leaves the monitor HORIZONTALLY (its vertical position is
                # clamped on instead, matching the canonical clamp).
                gutter = _clean_gutter(on_left=is_right)
                if gutter is not None:
                    return gutter
                # Ladder step 2: a small control's canonical spot is the
                # half-outside corner point; its fully-inside corner anchor
                # is a distinct spot worth trying before intruding.
                small = (
                    (right_phys - left_phys)
                    < badge_w_phys * _BADGE_SMALL_CONTROL_FACTOR
                    and (bottom_phys - top_phys)
                    < badge_h_phys * _BADGE_SMALL_CONTROL_FACTOR
                )
                if small:
                    i_left = (
                        right_phys - badge_w_phys if is_right else left_phys
                    )
                    i_top = (
                        bottom_phys - badge_h_phys if is_bottom else top_phys
                    )
                    if mon_w_phys is not None:
                        i_left = max(
                            0.0, min(i_left, mon_w_phys - badge_w_phys)
                        )
                    if mon_h_phys is not None:
                        i_top = max(
                            0.0, min(i_top, mon_h_phys - badge_h_phys)
                        )
                    inside = (
                        i_left, i_top,
                        i_left + badge_w_phys, i_top + badge_h_phys,
                    )
                    if not _hits_others(inside) and not _hits_placed(inside):
                        return inside
            return self._resolve_badge_collision(
                base, badge_w_phys, badge_h_phys, dpr,
                mon_w_phys, mon_h_phys, placed, others, corner=corner,
                own_box=own_box,
            )

        if (
            not self._badge_trailing_space
            or mon_w_phys is None
            or mon_h_phys is None
        ):
            return _corner_resolved()

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
        own_box: tuple[float, float, float, float],
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
        the base anchor itself, then rings of eight directions (INWARD along
        the corner's horizontal side, BELOW, ABOVE, OUTWARD, then the four
        diagonals) at one, two, and three badge-size steps out
        (``_BADGE_COLLISION_RINGS``; wh-overlay-bubble-badges.4 -- packed
        clusters at a monitor corner exhausted the old three-candidate list
        and stacked digits). Each step keeps ``_BADGE_COLLISION_GAP_PX``
        (scaled by dpr) of clear space -- two digits drawn flush read as one
        number. A candidate that leaves the monitor is skipped. A badge moved
        beyond the attach threshold draws as a detached bubble with a leader
        line back to its control, so a far spot stays unambiguous.

        Among the on-monitor, badge-free candidates, the ranking (each tier
        scanned in ring order) prefers, first, a spot that keeps the bubble
        ATTACHED to its own control (``own_box``; on it or within the attach
        gap, per ``_bubble_drawing_state``) AND overlapping no OTHER
        control's box; then an attached spot even on another control -- the
        pointer tail disambiguates a badge touching its own control, while a
        detached bubble needs a leader line that itself can mislead
        (wh-taskbar-badge-mispoint: on a vertical taskbar every candidate
        inside the column overlaps some full-width row, so preferring
        control-free spots systematically exiled badges to the desktop);
        then the old first preference, a spot merely clean of other
        controls' boxes -- a digit nudged fully onto a neighbouring numbered
        control reads as labeling that control
        (wh-overlay-collision-review.1). When every such spot is taken, the
        first badge-free candidate wins anyway -- sitting on a neighbour
        still beats stacking on another digit. When every candidate collides
        with a badge (a pathological pile-up of identical controls), the
        base anchor is returned: an overlapped number is still clickable by
        voice, a dropped number is not.
        """
        def _hits_placed(c: tuple[float, float, float, float]) -> bool:
            return any(self._rects_overlap_phys(c, p) for p in placed)

        if not placed or not _hits_placed(base):
            return base
        gap = _BADGE_COLLISION_GAP_PX * dpr
        is_right = corner in (_BADGE_CORNER_TOP_RIGHT, _BADGE_CORNER_BOTTOM_RIGHT)
        inward_sign = -1.0 if is_right else 1.0
        offsets: "list[tuple[float, float]]" = []
        for ring in range(1, _BADGE_COLLISION_RINGS + 1):
            step_x = ring * (badge_w_phys + gap)
            step_y = ring * (badge_h_phys + gap)
            offsets.extend((
                (inward_sign * step_x, 0.0),
                (0.0, step_y),
                (0.0, -step_y),
                (-inward_sign * step_x, 0.0),
                (inward_sign * step_x, step_y),
                (inward_sign * step_x, -step_y),
                (-inward_sign * step_x, step_y),
                (-inward_sign * step_x, -step_y),
            ))
        viable: "list[tuple[float, float, float, float]]" = []
        for dx, dy in offsets:
            cand_left = base[0] + dx
            cand_top = base[1] + dy
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

        def _clean(c: tuple[float, float, float, float]) -> bool:
            return not any(
                self._rects_overlap_phys(c, o) for o in other_ctrl_rects
            )

        def _attached(c: tuple[float, float, float, float]) -> bool:
            return (
                _bubble_drawing_state(c, own_box, dpr)
                != _BUBBLE_STATE_DETACHED
            )

        for cand in viable:
            if _attached(cand) and _clean(cand):
                return cand
        for cand in viable:
            if _attached(cand):
                return cand
        for cand in viable:
            if _clean(cand):
                return cand
        if viable:
            return viable[0]
        return base

    def _edge_cluster_placements_phys(
        self,
        badges: "list[tuple[OverlayPaintRect, int]]",
        dpr: float,
        mon_w_phys: float,
        mon_h_phys: float,
        metrics: QFontMetricsF,
    ) -> "dict[int, tuple[int, int, tuple[float, float, float, float]]]":
        """Column placements for edge-cluster badges, keyed by badge index
        (wh-taskbar-badge-mispoint, option 1).

        The live trigger: the tray corner of a vertical right-edge taskbar.
        The icons are packed tighter than one badge height, so every corner
        anchor collided and ``_resolve_badge_collision`` scattered the
        badges in RING order -- not target order -- and the leader lines
        crossed into an unreadable tangle. This pass detects such clusters
        up front and lays their badges out as one single-file column just
        beside the cluster, in the same top-to-bottom order as the targets:
        every leader line is short and roughly parallel, and crossings are
        impossible by construction (badge order matches target order, and
        the column x is constant).

        All four monitor edges are covered: a vertical stack against the
        left or right edge gets a COLUMN beside it, and a horizontal run
        against the top or bottom edge (the tray of a horizontal taskbar)
        gets a ROW beside it -- the same layout rotated. A control joins a
        cluster when ALL of:

        * it lies ENTIRELY within ``_EDGE_CLUSTER_BAND_FACTOR`` badge
          heights of the monitor edge (full-width rows -- a nav pane, a
          file list -- extend past the band and never qualify);
        * its size ALONG the run direction (height for a vertical run,
          width for a horizontal one) is under the
          ``_BADGE_SMALL_CONTROL_FACTOR`` x badge-height cap (a tall pane
          edge, a wide labeled taskbar button, or a clock is not an icon);
        * it belongs to a run of at least ``_EDGE_CLUSTER_MIN_COUNT`` such
          controls along the edge, split wherever the gap along the run
          exceeds ``_EDGE_CLUSTER_JOIN_GAP_FACTOR`` badge heights;
        * the run EXTENDS along the edge: its extent along the run
          direction is at least its extent across it. Without this a
          same-top horizontal row reaching into the right-edge band would
          form a bogus vertical column at the corner (and a vertical
          taskbar's bottom few rows a bogus horizontal row);
        * the run is PACKED: more badge slots (badge height for a column,
          each badge's own width for a row, plus the collision gap) than
          fit single-file in the run's own extent. An unpacked run --
          spaced taskbar app buttons -- keeps the shipped placement,
          which already handles it without tangling.

        Geometry: the badges' shared inner edge sits
        ``_BADGE_COLLISION_GAP_PX`` beyond the cluster's inner side (a
        right-edge cluster's column ends just left of the cluster; a
        bottom-edge cluster's row ends just above it). Each badge wants
        its control's center along the run; a packed run's excess length
        extends AWAY from the nearer monitor corner (a chained pass in
        that direction spaces the badges, and a second pass pulls the
        run back onto the monitor if the first pushed it off), keeping
        the corner free for the badges of nearby controls the cluster
        excluded. A column or row that cannot fit on the monitor
        abandons the cluster (no dict entries; the normal per-badge
        path applies).

        Deterministic pure float math over the same inputs, like the rest of
        the placement pass, so the bbox and render calls always agree.
        """
        gap = _BADGE_COLLISION_GAP_PX * dpr
        # Badge height is constant per (font, dpr); only the width varies
        # with the digit count. Probe with any number for the height.
        _probe_w, badge_h = self._numeral_badge_size(1, dpr, metrics=metrics)
        band = _EDGE_CLUSTER_BAND_FACTOR * badge_h
        join_gap = _EDGE_CLUSTER_JOIN_GAP_FACTOR * badge_h
        max_member_cross = badge_h * _BADGE_SMALL_CONTROL_FACTOR

        # (index, left, top, right, bottom) per numeral badge, physical px.
        boxes = []
        for i, (rect, number) in enumerate(badges):
            if number == WORKING_BADGE_NUMBER:
                continue
            boxes.append((
                i,
                rect.x * dpr,
                rect.y * dpr,
                (rect.x + rect.width) * dpr,
                (rect.y + rect.height) * dpr,
            ))

        out: "dict[int, tuple[int, int, tuple[float, float, float, float]]]" = {}
        claimed: "set[int]" = set()
        # Vertical edges first: a corner cluster lying inside both a
        # vertical and a horizontal band (a vertical taskbar's bottom tray)
        # is claimed by the vertical pass and keeps its column layout.
        for side in ("right", "left", "bottom", "top"):
            vertical = side in ("right", "left")
            if side == "right":
                members = [
                    b for b in boxes
                    if b[0] not in claimed
                    and b[1] >= mon_w_phys - band
                    and (b[4] - b[2]) < max_member_cross
                ]
            elif side == "left":
                members = [
                    b for b in boxes
                    if b[0] not in claimed
                    and b[3] <= band
                    and (b[4] - b[2]) < max_member_cross
                ]
            elif side == "bottom":
                members = [
                    b for b in boxes
                    if b[0] not in claimed
                    and b[2] >= mon_h_phys - band
                    and (b[3] - b[1]) < max_member_cross
                ]
            else:  # top
                members = [
                    b for b in boxes
                    if b[0] not in claimed
                    and b[4] <= band
                    and (b[3] - b[1]) < max_member_cross
                ]
            # Order along the run; within one same-position row (or column,
            # for a horizontal run), the icon FARTHER from the badge line
            # goes first so its badge sits earlier along the run. The line
            # is on the cluster's inner side (left of a right-edge cluster,
            # above a bottom-edge one), so an earlier badge pointing at the
            # NEARER icon would send the later badge's leader line swapping
            # over it to the farther icon -- the pair's lines would cross.
            # Farther-first nests them. Farther means larger left/top for
            # the right/bottom edges; for the left/top edges the plain
            # ascending cross coordinate already orders farther-first.
            if side == "right":
                members.sort(key=lambda b: (b[2], -b[1]))
            elif side == "left":
                members.sort(key=lambda b: (b[2], b[1]))
            elif side == "bottom":
                members.sort(key=lambda b: (b[1], -b[2]))
            else:  # top
                members.sort(key=lambda b: (b[1], b[2]))
            run: "list[tuple[int, float, float, float, float]]" = []
            run_end = 0.0

            def _flush(run_members) -> None:
                if len(run_members) < _EDGE_CLUSTER_MIN_COUNT:
                    return
                y_extent = (
                    max(b[4] for b in run_members)
                    - min(b[2] for b in run_members)
                )
                x_extent = (
                    max(b[3] for b in run_members)
                    - min(b[1] for b in run_members)
                )
                main_extent = y_extent if vertical else x_extent
                cross_extent = x_extent if vertical else y_extent
                # A run must EXTEND along its edge. A same-top row reaching
                # into the right-edge band is a row, not a column; without
                # this it would flush here as a bogus one-per-icon column
                # (and a vertical tray's bottom rows a bogus row).
                if main_extent < cross_extent:
                    return
                sizes = [
                    self._numeral_badge_size(
                        badges[b[0]][1], dpr, metrics=metrics
                    )
                    for b in run_members
                ]
                # Slot along the run: constant badge height for a column,
                # each badge's own digit-count-dependent width for a row.
                slots = [badge_h if vertical else w for w, _h in sizes]
                if sum(s + gap for s in slots) <= main_extent:
                    return
                max_bw = max(w for w, _h in sizes)
                if side == "right":
                    inner = min(b[1] for b in run_members) - gap
                    if inner - max_bw < 0.0:
                        return
                elif side == "left":
                    inner = max(b[3] for b in run_members) + gap
                    if inner + max_bw > mon_w_phys:
                        return
                elif side == "bottom":
                    inner = min(b[2] for b in run_members) - gap
                    if inner - badge_h < 0.0:
                        return
                else:  # top
                    inner = max(b[4] for b in run_members) + gap
                    if inner + badge_h > mon_h_phys:
                        return
                # Each badge wants its control's center along the run; a
                # packed run cannot give every badge its center, so the
                # excess length must extend somewhere. Extend it AWAY from
                # the nearer monitor corner: a run hugging the far corner
                # (the tray at the right end of a bottom taskbar) grows
                # toward the monitor center, keeping the corner free for
                # the badges of nearby controls the cluster excluded (a
                # wide clock, a two-member group). Otherwise both groups
                # converge on the corner and their leader lines cross
                # (wh-taskbar-badge-mispoint, 2026-08-08 screenshot).
                main_limit = mon_h_phys if vertical else mon_w_phys
                run_lo = min(
                    (b[2] if vertical else b[1]) for b in run_members
                )
                run_hi = max(
                    (b[4] if vertical else b[3]) for b in run_members
                )
                centers = [
                    ((b[2] + b[4]) / 2.0 if vertical
                     else (b[1] + b[3]) / 2.0)
                    for b in run_members
                ]
                n = len(run_members)
                pos: "list[float]" = [0.0] * n
                if (main_limit - run_hi) < run_lo:
                    # The run is nearer the FAR corner: reversed chain
                    # from the ideals so the excess extends backward,
                    # then push the run forward if it underflows the
                    # monitor start.
                    for k in range(n - 1, -1, -1):
                        ideal = centers[k] - slots[k] / 2.0
                        limit = (
                            pos[k + 1] - gap - slots[k]
                            if k < n - 1
                            else main_limit - slots[k]
                        )
                        pos[k] = min(ideal, limit)
                    if pos[0] < 0.0:
                        pos[0] = 0.0
                        for k in range(1, n):
                            lo = pos[k - 1] + slots[k - 1] + gap
                            if pos[k] < lo:
                                pos[k] = lo
                    # A run longer than the monitor pushes the last badge
                    # past the far edge; clamp it on (accepting overlap)
                    # -- an overlapped number is still clickable by
                    # voice, a dropped one is not.
                    for k in range(n):
                        pos[k] = min(pos[k], main_limit - slots[k])
                else:
                    # The run is nearer the monitor start: forward chain
                    # from the ideals so the excess extends forward, then
                    # move the run back onto the monitor if it overflows
                    # the far end.
                    for k in range(n):
                        ideal = centers[k] - slots[k] / 2.0
                        if k and ideal < pos[k - 1] + slots[k - 1] + gap:
                            ideal = pos[k - 1] + slots[k - 1] + gap
                        pos[k] = ideal
                    if pos[-1] + slots[-1] > main_limit:
                        pos[-1] = main_limit - slots[-1]
                        for k in range(n - 2, -1, -1):
                            limit = pos[k + 1] - slots[k] - gap
                            if pos[k] > limit:
                                pos[k] = limit
                    # A run longer than the monitor pushes the first
                    # badge past the edge; clamp it on (accepting
                    # overlap).
                    for k in range(n):
                        pos[k] = max(0.0, pos[k])
                for b, (bw, _bh), p in zip(run_members, sizes, pos):
                    if side == "right":
                        rect = (inner - bw, p, inner, p + badge_h)
                    elif side == "left":
                        rect = (inner, p, inner + bw, p + badge_h)
                    elif side == "bottom":
                        rect = (p, inner - badge_h, p + bw, inner)
                    else:  # top
                        rect = (p, inner, p + bw, inner + badge_h)
                    out[b[0]] = (bw, badge_h, rect)
                    claimed.add(b[0])

            for b in members:
                main_lo = b[2] if vertical else b[1]
                main_hi = b[4] if vertical else b[3]
                if run and main_lo - run_end > join_gap:
                    _flush(run)
                    run = []
                run_end = main_hi if not run else max(run_end, main_hi)
                run.append(b)
            _flush(run)
        return out

    def _numeral_badge_placements_phys(
        self,
        badges: "list[tuple[OverlayPaintRect, int]]",
        dpr: float,
        mon_w_phys: Optional[float],
        mon_h_phys: Optional[float],
        *,
        corner: str,
        metrics: Optional[QFontMetricsF] = None,
    ) -> "list[Optional[tuple[int, int, tuple[float, float, float, float]]]]":
        """Place every badge for one monitor in ONE sequential pass.

        ``metrics`` is an optional pre-built numeral ``QFontMetricsF``:
        ``_render_monitor_surface`` builds one per surface and shares it with
        this pass AND the digit drawing (wh-overlay-4bug-review.2). When
        omitted it is built lazily here, so a standalone call (the bounding
        box) stays self-contained and a working-glyph-only pass pays nothing.

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
        collision checks. Edge-cluster badges
        (``_edge_cluster_placements_phys``) are placed FIRST -- their column
        footprints seed ``placed`` before the sequential walk, so every
        non-cluster badge avoids the column. The pass is deterministic (pure
        float math over the same inputs), so the bbox call and the render
        call always produce identical placements.
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
        numeral_metrics: Optional[QFontMetricsF] = metrics
        has_numeral = any(n != WORKING_BADGE_NUMBER for _r, n in badges)
        cluster: "dict[int, tuple[int, int, tuple[float, float, float, float]]]" = {}
        if has_numeral and mon_w_phys is not None and mon_h_phys is not None:
            if numeral_metrics is None:
                numeral_metrics = QFontMetricsF(self._numeral_font(dpr))
            cluster = self._edge_cluster_placements_phys(
                badges, dpr, mon_w_phys, mon_h_phys, numeral_metrics
            )
        placed: "list[tuple[float, float, float, float]]" = [
            cluster[i][2] for i in sorted(cluster)
        ]
        out: "list[Optional[tuple[int, int, tuple[float, float, float, float]]]]" = []
        for i, (rect, number) in enumerate(badges):
            if number == WORKING_BADGE_NUMBER:
                out.append(None)
                continue
            if i in cluster:
                out.append(cluster[i])
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
        every badge is drawn at PHYSICAL resolution (all path geometry, pen
        widths, and the font are scaled by ``dpr``) so its edges stay sharp on
        a display scaled above 100%. The painter is translated by the bounding
        box's monitor-local physical offset (``-bbox.offset_x`` /
        ``-bbox.offset_y``), and the window is composited at
        ``rect_phys.left()/top() + bbox.offset``, so the offset cancels and a
        badge's on-screen physical position matches the full-monitor-surface
        model.

        Two badge kinds are drawn differently:

        - A NUMERAL is a speech bubble (wh-overlay-bubble-badges): one
          ``QPainterPath`` per badge -- rounded rect, pointer tail when the
          badge box is adjacent to its control, leader line when detached
          (``_bubble_drawing_state``) -- drawn DIRECTLY on the surface by
          ``_draw_numeral_bubble`` / ``_draw_leader_line``, inside the badge
          box the shared placement pass produced (trailing-edge or corner
          anchor, monitor-edge inward shift, collision nudges -- all
          unchanged). No per-badge image is allocated at all (the previous
          design's tight numeral image is gone entirely, extending
          wh-overlay-badge-alloc-decouple), and a tail can never be clipped by
          an image edge. All leader lines draw BEFORE all bubbles, so a line
          can end under a bubble but never crosses over one. The bubble scheme
          is resolved ONCE per surface from the validated badge theme plus the
          CURRENT system color scheme, so a Windows light/dark switch shows up
          on the next repaint.
        - The WORKING glyph FILLS its box, so it is rendered at the box
          physical size (``logical * dpr``) via ``_render_badge`` and drawn at
          the box top-left, putting the box center on the requested point.
        """
        dpr = monitor.dpr if monitor.dpr > 0 else 1.0
        surface = QImage(
            bbox.width, bbox.height, QImage.Format.Format_ARGB32_Premultiplied
        )
        surface.fill(0)  # transparent: only the badges draw

        painter = QPainter(surface)
        # The bubble scheme, the numeral font, and its metrics are fixed for
        # the whole surface (the font's only variable, dpr, is constant), so
        # resolve/build each once. The metrics are shared with the placement
        # pass and the digit drawing (wh-overlay-4bug-review.2), the font with
        # each digit's addText (wh-overlay-bubble-badges.1.1); a
        # working-glyph-only paint builds none of them.
        scheme = _resolve_bubble_scheme(
            self._badge_theme, _system_color_scheme()
        )
        numeral_font: Optional[QFont] = None
        numeral_metrics: Optional[QFontMetricsF] = None
        if any(n != WORKING_BADGE_NUMBER for _r, n in badges):
            numeral_font = self._numeral_font(dpr)
            numeral_metrics = QFontMetricsF(numeral_font)
        # The SAME sequential placement pass the bounding box ran (identical
        # inputs, deterministic math), so the surface never clips a badge it
        # did not budget for and collision nudges land where the bbox expected
        # them (see _numeral_badge_placements_phys).
        placements = self._numeral_badge_placements_phys(
            badges, dpr,
            monitor.rect_phys.width(), monitor.rect_phys.height(),
            corner=self._badge_corner,
            metrics=numeral_metrics,
        )
        # Each badge's control box in physical px: the tail and leader-line
        # geometry point the bubble at the control it labels.
        ctrl_boxes = [
            (r.x * dpr, r.y * dpr, (r.x + r.width) * dpr, (r.y + r.height) * dpr)
            for r, _n in badges
        ]
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            # Offset by the bounding box origin in PHYSICAL pixels. No painter
            # scale: everything is drawn in physical coordinates (sharp, not
            # enlarged).
            painter.translate(-bbox.offset_x, -bbox.offset_y)
            # Pass 1: every DETACHED badge's leader line, before any bubble.
            for placement, control in zip(placements, ctrl_boxes):
                if placement is None:
                    continue
                if (
                    _bubble_drawing_state(placement[2], control, dpr)
                    == _BUBBLE_STATE_DETACHED
                ):
                    self._draw_leader_line(
                        painter, placement, control, dpr, scheme,
                        monitor.rect_phys.width(), monitor.rect_phys.height(),
                    )
            # Pass 2: working glyphs and bubbles.
            for (rect, number), placement, control in zip(
                badges, placements, ctrl_boxes
            ):
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
                # A numeral exists, so both were built above.
                assert numeral_metrics is not None
                assert numeral_font is not None
                self._draw_numeral_bubble(
                    painter, number, placement, control, dpr, scheme,
                    numeral_metrics, numeral_font,
                    monitor.rect_phys.width(), monitor.rect_phys.height(),
                )
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

        An ACCEPTED clear whose destroy sweep left a survivor (a
        DestroyWindow failed) also returns ``None``: the ack is parked in
        ``_deferred_teardown_ack`` and ``teardown_pending`` turns True, so
        the GUI retry timer finishes the job (see ``retry_teardown``,
        wh-overlay-slow-uia-stale-badges.18.4).
        """
        if not self._gate.accept_clear(overlay_session_id, paint_generation):
            logger.debug(
                "overlay_paint_window: stale clear (%s, %s) ignored",
                overlay_session_id,
                paint_generation,
            )
            return None
        ack = OverlayStateChangedEvent(
            state="cleared",
            overlay_session_id=overlay_session_id,
            paint_generation=paint_generation,
            monitor_ids=(),
            snapshot_id=None,
        ).to_dict()
        if not self._destroy_all():
            # wh-overlay-slow-uia-stale-badges.18.4: a window survived its
            # DestroyWindow, so badges may still be on screen. A "cleared"
            # ack now would resolve Logic's clear-ack watchdog and cancel
            # the GUI badge lease -- the only retry drivers -- while the
            # survivor stays visible. Park the ack instead. Logic's
            # 5000 ms watchdog then fires a truthful "badges may still be
            # on the screen" ERROR, and the deferred ack arrives after a
            # later successful retry as bookkeeping. That is the designed
            # behavior, not a bug. The newest teardown owns the ack, so
            # any older parked ack is overwritten.
            self._teardown_pending = True
            self._deferred_teardown_ack = ack
            logger.warning(
                "overlay_paint_window: clear (%s, %s) left surviving badge "
                "windows; ack deferred until a retry destroys them",
                overlay_session_id,
                paint_generation,
            )
            return None
        return ack

    def expire_lease(
        self,
        overlay_session_id: int,
        paint_generation: int,
    ) -> Optional[dict[str, Any]]:
        """Tear down ALL overlay windows and report ``state="expired"``.

        The GUI-side badge-lease timer calls this when Logic stopped
        renewing the lease (wh-overlay-slow-uia-stale-badges.9): the badges
        must not outlive a Logic that no longer believes they are on
        screen. Gate semantics are identical to ``clear`` -- the pair
        advances the high-water mark and records the clear-block, so a
        late paint at the expired pair cannot re-present dead badges. A
        STALE expiry (the armed pair lost a race with a newer paint or
        clear) returns ``None`` and tears down nothing: a newer overlay
        owns the screen and its own lease.

        An ACCEPTED expiry whose destroy sweep left a survivor also
        returns ``None`` and parks the ack, exactly like ``clear``
        (wh-overlay-slow-uia-stale-badges.18.4).
        """
        if not self._gate.accept_clear(overlay_session_id, paint_generation):
            logger.debug(
                "overlay_paint_window: stale lease expiry (%s, %s) ignored",
                overlay_session_id,
                paint_generation,
            )
            return None
        ack = OverlayStateChangedEvent(
            state="expired",
            overlay_session_id=overlay_session_id,
            paint_generation=paint_generation,
            monitor_ids=(),
            snapshot_id=None,
        ).to_dict()
        if not self._destroy_all():
            # See clear(): no success claim while a window survived. The
            # GUI retry timer re-runs the destroy and releases this ack.
            self._teardown_pending = True
            self._deferred_teardown_ack = ack
            logger.warning(
                "overlay_paint_window: lease expiry (%s, %s) left surviving "
                "badge windows; ack deferred until a retry destroys them",
                overlay_session_id,
                paint_generation,
            )
            return None
        return ack

    def reset(self) -> None:
        """Destroy every overlay window and start a FRESH generation gate.

        The ``reset_overlay`` startup action calls this when a new Logic
        process announces itself (wh-overlay-slow-uia-stale-badges.9). A
        restarted Logic numbers its (overlay_session_id, paint_generation)
        pairs from zero again, so the old high-water mark would gate every
        new paint forever; and any badge windows a dead Logic left behind
        must not stay on screen. Emits nothing -- there is no live pair to
        report, and the new Logic starts from ``closed``.

        Drops any parked cleared/expired ack: the Logic process that
        asked for it is gone, so the ack must not surface later. A
        survivor still sets ``teardown_pending`` so the GUI retry keeps
        running (wh-overlay-slow-uia-stale-badges.18.4).
        """
        self._gate = GenerationGate()
        if not self._destroy_all():
            self._teardown_pending = True
        self._deferred_teardown_ack = None

    def retry_teardown(self) -> Optional[dict[str, Any]]:
        """Retry destroying the teardown debt cohort; release the parked ack.

        The GUI's 2000 ms retry timer drives this after an incomplete
        clear / expire_lease / reset left ``teardown_pending`` True
        (wh-overlay-slow-uia-stale-badges.18.4). It never touches the
        generation gate -- the original teardown already advanced it.

        COHORT-OWNED (wh-overlay-slow-uia-stale-badges.18.10): a
        teardown moves its survivors into the ``_pending_destroy`` debt
        cohort (see ``_destroy_all``), so the debt branch sweeps ONLY
        that cohort via ``_destroy_pending``. It never sweeps
        ``_windows``, so a retry structurally cannot destroy a window
        that a later accepted paint owns. The paint path prunes the
        cohort on every paint but does not pay the debt; the next retry
        then finds the cohort empty, pays the debt, and releases the
        parked ack.

        Without debt (wh-overlay-slow-uia-stale-badges.18.8) this
        method sweeps nothing: a later clean clear / expire_lease /
        reset paid the debt before the timer fired, and a fresh paint
        may own the screen. It only releases a still-parked ack (the
        accepted late-bookkeeping residual) or does nothing.

        Returns the deferred cleared/expired ack exactly once, when no
        teardown debt remains (the cohort sweep just ended clean, or a
        later teardown already paid the debt) while an ack is parked;
        otherwise ``None``.
        """
        if self._teardown_pending and not self._destroy_pending():
            return None
        if self._deferred_teardown_ack is not None:
            ack = self._deferred_teardown_ack
            self._deferred_teardown_ack = None
            return ack
        return None

    @property
    def teardown_pending(self) -> bool:
        """True while a teardown left a window its DestroyWindow failed on.

        The survivor lives in the ``_pending_destroy`` debt cohort, not
        in ``_windows`` (wh-overlay-slow-uia-stale-badges.18.10). The
        GUI checks this after every clear / expire_lease / reset and
        after every retry, and keeps its retry timer armed while it is
        True (wh-overlay-slow-uia-stale-badges.18.4).
        """
        return self._teardown_pending

    @property
    def has_deferred_teardown_ack(self) -> bool:
        """True while a deferred cleared/expired ack is still parked.

        The GUI reads this beside ``teardown_pending`` to decide
        whether an armed retry timer is still needed
        (wh-overlay-slow-uia-stale-badges.18.8): with no teardown debt
        and no parked ack the timer has nothing left to do and is
        stopped; while an ack is parked the timer stays armed so
        ``retry_teardown`` can release it as late bookkeeping.
        """
        return self._deferred_teardown_ack is not None

    def clear_all(self) -> None:
        """Destroy every overlay window without emitting an event.

        Used on GUI teardown / fixture cleanup.
        """
        self._destroy_all()

    def _destroy_all(self) -> bool:
        """Destroy every overlay window; move failures into the debt cohort.

        A window whose ``destroy()`` returned False is still on screen, so
        it is kept for a later teardown retry. This sweeps two sources:

        * ``self._pending_destroy`` -- the teardown debt cohort:
          rebuild-path orphans and survivors of earlier sweeps. Retried
          first; still-failing ones stay.
        * ``self._windows`` -- the live per-monitor windows. A survivor
          MOVES into ``self._pending_destroy``
          (wh-overlay-slow-uia-stale-badges.18.10), so ``_windows`` is
          always empty after this method returns. A later paint then
          owns ``_windows`` alone, and ``retry_teardown`` sweeps only
          the cohort.

        Returns True when the cohort ended empty (a clean sweep) and
        False when a survivor remains. A clean sweep also pays off any
        recorded teardown debt (``_teardown_pending`` goes False). Only
        the TEARDOWN callers (clear / expire_lease / reset) set the flag
        on a False return; the paint except-path and ``clear_all`` do
        not, because they own no ack and the teardowns re-check the
        containers themselves (wh-overlay-slow-uia-stale-badges.18.4).
        """
        # Retry cohort members first; keep the ones that still fail.
        self._pending_destroy = [
            window for window in self._pending_destroy if not window.destroy()
        ]
        for window in self._windows.values():
            if not window.destroy():
                self._pending_destroy.append(window)
        self._windows = {}
        clean = not self._pending_destroy
        if clean:
            self._teardown_pending = False
        return clean

    def _destroy_pending(self) -> bool:
        """Sweep ONLY the ``_pending_destroy`` teardown debt cohort.

        The same keep-the-failures filter as ``_destroy_all``, restricted
        to the cohort. It never reads or writes ``self._windows``, so it
        cannot touch a window that a later paint owns
        (wh-overlay-slow-uia-stale-badges.18.10). Returns True when the
        cohort ended empty; a clean sweep also pays off any recorded
        teardown debt (``_teardown_pending`` goes False).
        """
        self._pending_destroy = [
            window for window in self._pending_destroy if not window.destroy()
        ]
        clean = not self._pending_destroy
        if clean:
            self._teardown_pending = False
        return clean

    # -- mouse-grid drawing mode (wh-grid-paint-mode) ------------------------
    #
    # A SECOND drawing mode on the same per-monitor click-through layered
    # windows: three-by-three grid lines with nine large cell numbers, plus
    # the drag-anchor pin. The GUI decides nothing here -- it draws exactly
    # the rectangle and the point the events carry. All cell arithmetic,
    # refinement, monitor choice, and the minimum-cell-size rule live in the
    # Logic process (``grid_overlay_state.py``).
    #
    # These methods share the manager's window dictionary with ``paint`` /
    # ``clear``, so ONE manager instance must serve one feature: gui.py
    # constructs a DEDICATED manager for the grid, exactly as it already does
    # for the dictation working badge. Mixing them on one instance would make
    # a numbered-overlay clear destroy the grid's windows. There is no
    # generation gate: the grid has no in-flight build phase, so Logic never
    # has more than one grid outstanding and every event is a complete
    # description of what should be on screen (see shared/clear_grid.py).

    def paint_grid(self, event: Any) -> bool:
        """Draw the mouse grid described by a ``PaintGridEvent``.

        ``event`` is duck-typed on the schema's eight flat int fields
        (``monitor_left`` / ``monitor_top`` / ``monitor_width`` /
        ``monitor_height`` name the monitor, ``left`` / ``top`` / ``width`` /
        ``height`` the rectangle to divide into nine), all in virtual-desktop
        PHYSICAL pixels. The rectangle shrinks as the user refines; the
        overlay window is sized to the rectangle plus the ink margin, so the
        transient surface shrinks with it.

        Retains the event and repaints the pin alongside it, so a refinement
        never erases an existing drag anchor. Returns True when every
        involved monitor composited successfully, False when the monitor
        could not be resolved (it was disconnected), when there was nothing
        to draw, or when a composite failed.
        """
        self._grid_event = event
        return self._render_grid()

    def paint_grid_pin(self, event: Any) -> bool:
        """Draw the drag-anchor pin described by a ``PaintGridPinEvent``.

        ``event`` is duck-typed on the schema's ``x`` / ``y`` fields, in
        virtual-desktop PHYSICAL pixels (either may be negative). The pin
        lands on whichever monitor contains the point -- deliberately not
        necessarily the grid's monitor, because a drag from one screen to
        another is legitimate. A point on no monitor is dropped.

        Retains the pin and repaints the grid alongside it, so marking never
        erases the grid. Same return value as :meth:`paint_grid`.
        """
        self._grid_pin = event
        return self._render_grid()

    def clear_grid(self) -> None:
        """Remove all grid drawing, including the pin.

        Forgets both retained events and tears down every window this manager
        owns. The pin dies with the grid session by contract, which is why
        one teardown removes both (see shared/clear_grid.py).
        """
        self._grid_event = None
        self._grid_pin = None
        self._destroy_all()

    def _render_grid(self) -> bool:
        """Repaint the retained grid and pin; never raises.

        A rendering failure is logged and the windows are torn down, so a
        half-drawn grid is never left on screen -- the same fail-safe
        ``paint`` applies to the numbered overlay.
        """
        try:
            return self._do_render_grid()
        except Exception:  # noqa: BLE001 - a paint failure must not escape
            logger.error(
                "overlay_paint_window: grid paint failed", exc_info=True
            )
            self._destroy_all()
            return False

    def _do_render_grid(self) -> bool:
        """Resolve monitors, (re)create windows, and composite the grid.

        Mirrors ``_do_paint``'s window discipline exactly: retry rebuild-path
        orphans, enumerate the topology ONCE, tear down monitors absent from
        this render BEFORE creating new ones, rebuild a window whose geometry
        changed (so no stale, differently-sized DIB is reused under a refined
        grid), then composite each monitor's surface at its screen origin.
        """
        if self._pending_destroy:
            self._pending_destroy = [
                window
                for window in self._pending_destroy
                if not window.destroy()
            ]

        monitors = _enumerate_native_monitors()
        grid_monitor = self._grid_monitor(monitors)
        pin_monitor = self._pin_monitor(monitors)

        # The drawn region per monitor, in SCREEN physical pixels, clamped to
        # that monitor so a window never spills onto a neighbour. Both pieces
        # can land on the same monitor, in which case one window carries both.
        regions: dict[int, QRect] = {}
        if grid_monitor is not None:
            margin = self._grid_ink_margin_phys(grid_monitor.dpr)
            region = QRect(
                int(self._grid_event.left) - margin,
                int(self._grid_event.top) - margin,
                int(self._grid_event.width) + 2 * margin,
                int(self._grid_event.height) + 2 * margin,
            ).intersected(grid_monitor.rect_phys)
            if not region.isEmpty():
                regions[grid_monitor.hmonitor] = region
        if pin_monitor is not None:
            margin = self._grid_pin_ink_margin_phys(pin_monitor.dpr)
            region = QRect(
                int(self._grid_pin.x) - margin,
                int(self._grid_pin.y) - margin,
                2 * margin + 1,
                2 * margin + 1,
            ).intersected(pin_monitor.rect_phys)
            if not region.isEmpty():
                existing = regions.get(pin_monitor.hmonitor)
                regions[pin_monitor.hmonitor] = (
                    region if existing is None else existing.united(region)
                )

        by_hmonitor = {mon.hmonitor: mon for mon in monitors}

        # Tear down windows for monitors absent from THIS render before
        # creating any new ones.
        for hmonitor in list(self._windows.keys()):
            if hmonitor not in regions:
                window = self._windows.pop(hmonitor)
                if not window.destroy():
                    self._windows[hmonitor] = window

        if not regions:
            return False

        h_instance = self._ensure_class_registered()
        for hmonitor, geom in regions.items():
            existing = self._windows.get(hmonitor)
            if existing is None or existing.geom_phys != geom:
                if existing is not None:
                    old = self._windows.pop(hmonitor)
                    if not old.destroy():
                        self._pending_destroy.append(old)
                self._windows[hmonitor] = _OverlayWindow(
                    hmonitor=hmonitor,
                    geom_phys=geom,
                    class_name=self._CLASS_NAME,
                    user32=self._user32,
                    kernel32=self._kernel32,
                    h_instance=h_instance,
                )

        all_ok = True
        for hmonitor, geom in regions.items():
            window = self._windows.get(hmonitor)
            if window is None:
                all_ok = False
                continue
            monitor = by_hmonitor[hmonitor]
            grid_rect = None
            if grid_monitor is not None and grid_monitor.hmonitor == hmonitor:
                grid_rect = QRectF(
                    float(self._grid_event.left),
                    float(self._grid_event.top),
                    float(self._grid_event.width),
                    float(self._grid_event.height),
                )
            pin_point = None
            if pin_monitor is not None and pin_monitor.hmonitor == hmonitor:
                pin_point = (
                    float(self._grid_pin.x),
                    float(self._grid_pin.y),
                )
            surface = self._render_grid_surface(
                monitor, geom, grid_rect, pin_point
            )
            dib = build_layered_dib(surface)
            try:
                ok = window.composite(dib, geom.left(), geom.top())
            finally:
                dib.release()
            if not ok:
                # Same reasoning as _do_paint: a failed UpdateLayeredWindow on
                # a REUSED window leaves the previous rectangle's grid
                # visible, which would point the user at the wrong cells.
                all_ok = False
                logger.warning(
                    "overlay_paint_window: grid composite failed for monitor "
                    "%s; destroying its window so no stale DIB lingers",
                    hmonitor,
                )
                if window.destroy():
                    self._windows.pop(hmonitor, None)
        return all_ok

    def _grid_monitor(self, monitors: list[_NativeMonitor]):
        """The enumerated monitor the retained grid event names, or ``None``.

        Prefers an exact rectangle match; otherwise the monitor with the
        largest physical overlap with the named rectangle (reusing
        ``_overlap_area``), which absorbs a resolution change between the
        event being built and being drawn. Deliberately does NOT fall back to
        the first/primary monitor the way ``_resolve_target_monitor`` does:
        a grid whose monitor is gone must draw nothing rather than appear on
        a screen the user did not ask for. Logic closes the grid on a monitor
        disconnect, so this is the window between the two.
        """
        event = self._grid_event
        if event is None:
            return None
        wanted = QRect(
            int(event.monitor_left),
            int(event.monitor_top),
            int(event.monitor_width),
            int(event.monitor_height),
        )
        for monitor in monitors:
            if monitor.rect_phys == wanted:
                return monitor
        best = None
        best_area = 0
        for monitor in monitors:
            area = _overlap_area(monitor.rect_phys, wanted)
            if area > best_area:
                best, best_area = monitor, area
        return best

    def _pin_monitor(self, monitors: list[_NativeMonitor]):
        """The enumerated monitor CONTAINING the retained pin point, or
        ``None`` when the pin is unset or lands on no monitor.

        Containment, not overlap: the pin marks one exact point, and the
        containment test is the same one ``paint_working_badge`` uses to
        decide whether a point-anchored drawing has a monitor at all.
        """
        pin = self._grid_pin
        if pin is None:
            return None
        for monitor in monitors:
            if monitor.rect_phys.contains(int(pin.x), int(pin.y)):
                return monitor
        return None

    @staticmethod
    def _grid_ink_margin_phys(dpr: float) -> int:
        """Physical-pixel breathing room around the grid rectangle."""
        scale = dpr if dpr > 0 else 1.0
        return int(math.ceil(_GRID_INK_MARGIN_LOGICAL_PX * scale))

    @staticmethod
    def _grid_pin_ink_margin_phys(dpr: float) -> int:
        """Physical-pixel half-extent of the pin drawing: the longer of the
        crosshair arm and the ring radius, plus the outer stroke's half width
        and antialiasing slack."""
        scale = dpr if dpr > 0 else 1.0
        reach = max(_GRID_PIN_ARM_LOGICAL_PX, _GRID_PIN_RADIUS_LOGICAL_PX)
        return int(
            math.ceil((reach + _GRID_LINE_OUTER_LOGICAL_PX) * scale)
        )

    @staticmethod
    def _grid_label_pixel_size(
        cell_w: float, cell_h: float, dpr: float
    ) -> int:
        """The cell label's font size in PHYSICAL pixels.

        Proportional to the cell's shorter side so the digit always fits,
        clamped between the legibility floor and the cap (see
        ``_GRID_LABEL_MIN_LOGICAL_PX`` / ``_GRID_LABEL_MAX_LOGICAL_PX``).
        Both clamp bounds are logical, so they scale with the monitor's dpr
        and the label keeps a constant PERCEIVED size across a mixed-DPI
        desktop -- the same rule the working badge follows.
        """
        scale = dpr if dpr > 0 else 1.0
        target = min(cell_w, cell_h) * _GRID_LABEL_CELL_FACTOR
        low = _GRID_LABEL_MIN_LOGICAL_PX * scale
        high = _GRID_LABEL_MAX_LOGICAL_PX * scale
        return max(1, int(round(min(max(target, low), high))))

    def _render_grid_surface(
        self,
        monitor: _NativeMonitor,
        geom: QRect,
        grid_rect: Optional[QRectF],
        pin_point: Optional[tuple[float, float]],
    ) -> QImage:
        """Compose one monitor's grid surface.

        The surface is ``geom`` (the drawn region in SCREEN physical pixels)
        and everything is drawn in screen-physical coordinates: the painter is
        translated by the region's screen origin, and the window is
        composited at that same origin, so the translation cancels. Working
        in physical pixels throughout is what keeps the drawing sharp on a
        scaled display -- ``UpdateLayeredWindow`` blits the DIB 1:1 against
        the window's device pixels and never scales.
        """
        dpr = monitor.dpr if monitor.dpr > 0 else 1.0
        surface = QImage(
            max(1, geom.width()),
            max(1, geom.height()),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        surface.fill(0)  # transparent: only the grid draws

        painter = QPainter(surface)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            painter.translate(-geom.left(), -geom.top())
            if grid_rect is not None:
                self._draw_grid_cells(painter, grid_rect, dpr)
            if pin_point is not None:
                self._draw_grid_pin(painter, pin_point[0], pin_point[1], dpr)
        finally:
            painter.end()
        return surface

    def _draw_grid_cells(
        self, painter: QPainter, rect: QRectF, dpr: float
    ) -> None:
        """Draw the three-by-three lines over ``rect`` and the nine labels.

        Four vertical and four horizontal lines: the two interior divisions
        plus the rectangle's own border, which is what shows the user how far
        the grid has been refined once the rectangle is smaller than the
        monitor. The cell boundaries come from
        ``grid_overlay_state.cell_rects`` -- the SAME integer edge rule the
        Logic-side state machine uses to resolve a spoken number
        (``origin + (size * i) // 3``) -- so for a rectangle whose side does
        not divide by three the painted line sits exactly on the boundary
        the click arithmetic uses, never a fraction of a pixel away
        (wh-mouse-grid.1.13). Labels sit on ``cell_center`` of each cell for
        the same reason.
        """
        grid = _GridRect(
            left=round(rect.left()),
            top=round(rect.top()),
            width=round(rect.width()),
            height=round(rect.height()),
        )
        cells = _grid_cell_rects(grid)
        xs = (
            cells[0].left,
            cells[1].left,
            cells[2].left,
            cells[2].left + cells[2].width,
        )
        ys = (
            cells[0].top,
            cells[3].top,
            cells[6].top,
            cells[6].top + cells[6].height,
        )
        lines = [
            QLineF(x, ys[0], x, ys[3]) for x in xs
        ] + [
            QLineF(xs[0], y, xs[3], y) for y in ys
        ]
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for color, width in (
            (_GRID_LINE_OUTER_COLOR, _GRID_LINE_OUTER_LOGICAL_PX),
            (_GRID_LINE_CORE_COLOR, _GRID_LINE_CORE_LOGICAL_PX),
        ):
            pen = QPen(color)
            pen.setWidthF(width * dpr)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            painter.drawLines(lines)

        # Telephone-keypad order: 1-2-3 top row, 4-5-6 middle, 7-8-9 bottom
        # (the layout every phone keypad and every competing grid tool uses).
        # cell_rects returns the cells in exactly this order, and
        # cell_center is where the pointer actually lands for the spoken
        # number, so the label marks the true click point.
        for number in range(1, 10):
            cell = cells[number - 1]
            center = _grid_cell_center(cell)
            self._draw_grid_label(
                painter,
                number,
                float(center.x),
                float(center.y),
                float(cell.width),
                float(cell.height),
                dpr,
            )

    def _draw_grid_label(
        self,
        painter: QPainter,
        number: int,
        center_x: float,
        center_y: float,
        cell_w: float,
        cell_h: float,
        dpr: float,
    ) -> None:
        """Draw one cell number centered on ``(center_x, center_y)``.

        White glyph with a black outline (and the optional drop shadow the
        accessibility setting controls) -- the numbered overlay's original
        badge palette, chosen for the same reason it was chosen there: the
        pair reads over any background without knowing what is underneath.
        The outline width is proportional to the glyph (see
        ``_GRID_LABEL_OUTLINE_FACTOR``), so it never swallows a small digit.
        """
        pixel_size = self._grid_label_pixel_size(cell_w, cell_h, dpr)
        font = QFont()
        font.setPixelSize(pixel_size)
        font.setBold(True)
        metrics = QFontMetricsF(font)
        text = str(number)
        path = QPainterPath()
        path.addText(
            center_x - metrics.horizontalAdvance(text) / 2.0,
            center_y + metrics.capHeight() / 2.0,
            font,
            text,
        )

        if self._badge_shadow:
            shadow = QPainterPath(path)
            shadow_offset = _SHADOW_OFFSET_PX * dpr
            shadow.translate(shadow_offset, shadow_offset)
            painter.fillPath(shadow, _SHADOW_COLOR)
        outline = QPen(_OUTLINE_COLOR)
        outline.setWidthF(
            max(
                _GRID_LABEL_OUTLINE_MIN_LOGICAL_PX * dpr,
                pixel_size * _GRID_LABEL_OUTLINE_FACTOR,
            )
        )
        outline.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(outline)
        painter.setBrush(_NUMERAL_COLOR)
        painter.drawPath(path)

    def _draw_grid_pin(
        self, painter: QPainter, x: float, y: float, dpr: float
    ) -> None:
        """Draw the drag-anchor pin: a crosshair inside a ring, centered on
        the point, in the same two-stroke palette as the grid lines."""
        arm = _GRID_PIN_ARM_LOGICAL_PX * dpr
        radius = _GRID_PIN_RADIUS_LOGICAL_PX * dpr
        lines = [
            QLineF(x - arm, y, x + arm, y),
            QLineF(x, y - arm, x, y + arm),
        ]
        ring = QRectF(x - radius, y - radius, 2.0 * radius, 2.0 * radius)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for color, width in (
            (_GRID_LINE_OUTER_COLOR, _GRID_LINE_OUTER_LOGICAL_PX),
            (_GRID_LINE_CORE_COLOR, _GRID_LINE_CORE_LOGICAL_PX),
        ):
            pen = QPen(color)
            pen.setWidthF(width * dpr)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLines(lines)
            painter.drawEllipse(ring)
