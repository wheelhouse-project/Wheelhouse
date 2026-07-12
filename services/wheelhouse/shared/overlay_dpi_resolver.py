"""Physical-pixel-bounds to Qt-logical-paint-coordinate converter for
the multi-monitor mixed-DPI numbered overlay (wh-n29v.49).

Phase 1.5 voice element clicking paints a numbered badge over each
candidate control. On a multi-monitor desktop with MIXED DPI scaling
(one screen at 100%, another at 150%, etc.) the overlay is built as one
window per screen, each window positioned over its screen. To paint a
badge for a control, the painter needs the control's bounds expressed
in Qt LOGICAL coordinates LOCAL to that screen's overlay window -- NOT
the virtual-desktop physical-pixel rectangle UIA reports.

This module is the pure converter that performs exactly that transform
for one overlay item. It COMPOSES the existing
``shared.monitor_geometry`` helpers; it does NOT
re-implement monitor resolution or the QScreen bridge, and it does NOT
modify ``monitor_geometry.py``. It is the only new piece of geometry
math here: everything else (which monitor owns the rect, which QScreen
corresponds to that monitor) is delegated.

What this module is NOT
-----------------------

It does not build the per-monitor overlay paint window (a separate
slice), it does not wire into ``gui.py`` or any paint loop, and it adds
no IPC schema. It is a single pure function plus its result shape.

The coordinate transform
-------------------------

Input ``bounds`` is ``(left, top, right, bottom)`` in virtual-desktop
PHYSICAL pixels -- the same coordinate system UIA bounding rectangles
and the Win32 ``rcMonitor`` rectangles use. Given the resolved monitor
``M`` (a ``_NativeMonitor`` whose ``rect_phys`` is that monitor's
physical rectangle and whose ``dpr`` is its device pixel ratio):

    local_phys_left = left - M.rect_phys.left()
    local_phys_top  = top  - M.rect_phys.top()
    local_logical_x = local_phys_left / M.dpr
    local_logical_y = local_phys_top  / M.dpr
    local_logical_w = (right - left)  / M.dpr
    local_logical_h = (bottom - top)  / M.dpr

Step 1 subtracts the target monitor's physical top-left so the rect is
LOCAL to that screen (origin (0, 0) at the screen's top-left). Step 2
divides by that monitor's DPR (``physical = logical * dpr``) to get
LOGICAL paint coordinates. The result origin is the target screen's
top-left, because the overlay window for a screen is positioned over
that screen and its painter uses (0, 0)-based logical coordinates.

The DPR comes from the resolved ``_NativeMonitor`` (``M.dpr``), not from
``QScreen.devicePixelRatio()``. The native value is deterministic in
tests (driven entirely through the ``monitors=`` seam) and is the same
effective-DPI value Qt derives its DPR from on Windows under Per-Monitor
v2 awareness. The QScreen is still resolved and returned so the caller
can pick the right overlay window, but it is not consulted for the math.

Rounding rule
-------------

Qt logical coordinates are floats but paint targets are integer pixels.
Each of x, y, width, height is converted independently with
``int(round(value))`` -- Python 3's banker's rounding (round-half-to-
even) at exact .5 boundaries, nearest-int otherwise. x and y round
independently of width and height; this module does NOT round the
right/bottom edge and back-derive the size, so a 1px rounding wobble on
the origin never silently shrinks or grows the painted rect. The wobble
is at most 1 logical pixel per edge, invisible for a badge overlay.

Each positive physical extent is additionally floored to at least 1
logical pixel for ``width`` and ``height`` (``max(1, int(round(...)))``),
so a real but sub-logical-pixel control -- e.g. a 1px-tall control on a
2x monitor where 1/2 = 0.5 rounds half-to-even to 0 -- never collapses to
a zero-size paint rect with nothing for its badge to anchor to. (Any
bounds reaching this point have strictly positive physical width AND
height because the non-positive-extent guard returned None earlier.)
``x`` and ``y`` are NOT floored: a screen-local origin may legitimately
be 0 or negative (a control partly off the screen's left/top edge).

Performance / call pattern
--------------------------

Production callers MUST enumerate the native monitors and the QScreens
ONCE per overlay render (per ``paint_overlay``) and pass them to every
per-item call via the ``monitors=`` and ``screens=`` seams. Passing
``None`` per item re-runs the synchronous Win32 topology enumeration
(``_enumerate_native_monitors`` -> ``EnumDisplayMonitors`` /
``GetMonitorInfoW`` / ``GetDpiForMonitor``) and ``QGuiApplication.screens()``
on EVERY badge, which is avoidable latency on the overlay paint path.
The seams exist precisely so the caller can snapshot the topology once
and reuse it for all badges in one render. (There is intentionally no
batch API; the per-item signature plus the seams is sufficient.)

monitor_id namespace decision
-----------------------------

``WalkSnapshotSummaryItem.monitor_id`` is produced in the INPUT process
by an injected ``monitor_resolver`` callable (``ui/element_finder.py``).
It is an OPAQUE integer from that resolver. This converter runs in the
GUI process, which has its OWN monitor enumeration
(``_enumerate_native_monitors``) keyed by ``hmonitor``. There is NO
guarantee that ``monitor_id`` equals any ``_NativeMonitor.hmonitor``
across the process boundary -- the two are different namespaces.

Therefore this converter resolves the target monitor from the item's
PHYSICAL BOUNDS via ``_resolve_target_monitor(bounds)``. Bounds are
namespace-independent virtual-desktop physical pixels shared by UIA and
``GetMonitorInfo``, so bounds-based resolution is correct regardless of
how ``monitor_id`` was minted. ``monitor_id`` is accepted on the API
for forward compatibility and call-site symmetry with
``WalkSnapshotSummaryItem`` but is treated as ADVISORY ONLY: it never
steers resolution. The test
``test_monitor_id_is_advisory_resolution_follows_bounds`` pins this
contract -- a nonsense ``monitor_id`` does not change the result.

Qt high-DPI policy dependency
-----------------------------

For a fractional-scaling monitor (125% / 150%) to report a fractional
DPR (1.25 / 1.5) rather than a rounded integer, the GUI process must set
``Qt.HighDpiScaleFactorRoundingPolicy.PassThrough`` at startup, before
the ``QGuiApplication`` is constructed. That policy is NOT set anywhere
in the tree today and setting it is OUT OF SCOPE for this slice (it
lives in ``gui.py`` startup wiring). This converter does not depend on
that policy for its own math: it takes the DPR from the resolved
``_NativeMonitor``, whose effective DPI is read straight from
``GetDpiForMonitor`` (144 -> 1.5 exactly), independent of any Qt
rounding policy. The tests simulate fractional DPRs directly via the
``monitors=`` seam, so they pass with or without the policy set. The
ONE place the policy matters downstream is if the eventual overlay
painter ever uses ``QScreen.devicePixelRatio()`` for the math instead of
the native monitor DPR -- which this converter deliberately does not.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QRect

from shared.monitor_geometry import (
    _NativeMonitor,
    _match_qscreen_for_monitor,
    _overlap_area,
    _resolve_target_monitor,
)


if TYPE_CHECKING:  # pragma: no cover - import guard for static checkers.
    from PySide6.QtGui import QScreen


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OverlayPaintRect:
    """Result of converting one overlay item's physical bounds.

    ``x``, ``y``, ``width``, ``height`` are Qt LOGICAL paint coordinates
    LOCAL to ``screen``'s overlay window -- origin (0, 0) at the target
    screen's top-left, already divided by the target monitor's DPR and
    rounded to integer pixels.

    ``monitor`` is the resolved ``_NativeMonitor`` and ``hmonitor`` is
    its handle, carried so the caller can correlate the rect with the
    overlay window it must paint into. The caller MUST select the
    overlay window by ``hmonitor`` (the resolved monitor's handle,
    always present), NOT by ``screen``: ``x``/``y`` are LOCAL to the
    resolved (possibly secondary) monitor, so painting them on any other
    monitor's overlay window would place the badge on the wrong screen.

    ``screen`` is an OPTIONAL Qt convenience -- the bridged ``QScreen``
    for callers that key overlay windows by ``QScreen`` -- or ``None``
    when the QScreen bridge could not match a screen. When ``screen`` is
    ``None`` the caller locates the window by ``hmonitor`` instead; it
    MUST NOT paint these monitor-local coordinates on any other
    monitor's overlay window, and if no overlay window matches the
    ``hmonitor`` it skips the badge. The logical rect is valid even when
    ``screen`` is ``None`` because the math uses the native monitor DPR,
    not the QScreen.
    """

    x: int
    y: int
    width: int
    height: int
    monitor: _NativeMonitor
    hmonitor: int
    screen: Optional["QScreen"]


def resolve_overlay_paint_rect(
    bounds: tuple[int, int, int, int],
    monitor_id: int,
    monitors: Sequence[_NativeMonitor] | None = None,
    screens: Sequence["QScreen"] | None = None,
) -> OverlayPaintRect | None:
    """Convert one overlay item's physical bounds to screen-local logical
    paint coordinates.

    Parameters
    ----------
    bounds:
        ``(left, top, right, bottom)`` in virtual-desktop PHYSICAL
        pixels -- the coordinate system UIA rects and Win32
        ``rcMonitor`` rectangles share. This is the namespace-safe input
        that drives monitor resolution.
    monitor_id:
        The opaque, Input-process-minted monitor identifier from the
        overlay item. ADVISORY ONLY (see the module docstring's
        "monitor_id namespace decision"): it is NOT used to resolve the
        target monitor and does NOT change the result. Accepted for
        forward compatibility and call-site symmetry with
        ``WalkSnapshotSummaryItem``.
    monitors:
        Dependency-injection seam forwarded to
        ``_resolve_target_monitor``. ``None`` -> production enumeration
        via ``_enumerate_native_monitors()``. Tests pass synthetic
        ``_NativeMonitor`` lists.
    screens:
        Dependency-injection seam forwarded to
        ``_match_qscreen_for_monitor``. ``None`` -> production
        ``QGuiApplication.screens()``. Tests pass synthetic
        ``QScreen``-shaped stubs.

    Returns
    -------
    OverlayPaintRect | None
        The screen-local logical rect plus the resolved monitor / QScreen
        identity. Returns ``None`` (the documented safe fallback) in two
        cases, so the caller never crashes on a degenerate layout and
        never paints a wrong-screen / off-window badge: (1) the topology
        is empty / no monitor can be resolved at all -- i.e. an empty
        ``monitors`` list; OR (2) the control's bounds have no positive-
        area overlap with the resolved monitor -- i.e. the control is
        entirely off all enumerated monitors, or the bounds are
        degenerate (zero-area, or inverted -> invalid rect -> zero
        overlap). A partially-off-screen control that still overlaps a
        monitor returns a valid rect, whose local origin MAY be negative
        (the badge clips at the overlay window edge). A successfully
        resolved monitor with no matching QScreen still yields an
        ``OverlayPaintRect`` (with ``screen=None``); the math does not
        depend on the QScreen. In that case the caller places the rect
        by ``hmonitor`` (always present) and MUST NOT paint it on a
        different monitor's overlay window -- the coordinates are local
        to the resolved monitor, so a wrong-window paint would put the
        badge on the wrong screen.
    """
    # The composed helpers declare invariant ``list | None`` seams.
    # Materialize the covariant ``Sequence`` inputs into concrete lists
    # before forwarding (only when present) so a caller may pass any
    # sequence while the helper signatures stay satisfied; ``None`` is
    # forwarded unchanged to trigger the helpers' production enumeration.
    monitors_list = None if monitors is None else list(monitors)
    screens_list = None if screens is None else list(screens)

    monitor = _resolve_target_monitor(bounds, monitors=monitors_list)
    if monitor is None:
        # Empty topology: nothing to paint against. Documented safe
        # fallback -- the caller treats None as "skip this item / use the
        # primary screen" rather than receiving an exception.
        return None

    left, top, right, bottom = bounds
    bounds_width = right - left
    bounds_height = bottom - top
    # Guard degenerate bounds (zero-area or inverted) BEFORE the overlap
    # math: ``QRect.intersected`` silently normalizes an inverted rect, so
    # an inverted input would otherwise report a positive overlap. A non-
    # positive extent on either axis cannot host a meaningful badge.
    bounds_rect = QRect(left, top, bounds_width, bounds_height)
    if (
        bounds_width <= 0
        or bounds_height <= 0
        or _overlap_area(bounds_rect, monitor.rect_phys) <= 0
    ):
        # The resolved monitor is the max-overlap monitor, so a zero overlap
        # here means the control is off ALL enumerated monitors (off-screen),
        # OR the bounds are degenerate (zero-area, or inverted -> non-positive
        # extent). A badge cannot be placed meaningfully; return None so the
        # caller skips this item rather than painting a wrong-screen /
        # off-window badge silently.
        logger.warning(
            "resolve_overlay_paint_rect: bounds %s have no positive overlap "
            "with resolved monitor %s (off-screen or degenerate); skipping",
            bounds,
            monitor.hmonitor,
        )
        return None

    # Bridge to the QScreen so the caller can select the right overlay
    # window. A None here is non-fatal: the logical math below uses the
    # native monitor DPR, not the QScreen, so the rect is still valid.
    screen = _match_qscreen_for_monitor(monitor, screens=screens_list)

    origin_x = monitor.rect_phys.left()
    origin_y = monitor.rect_phys.top()
    dpr = monitor.dpr
    if dpr <= 0:
        logger.warning(
            "resolve_overlay_paint_rect: monitor %s reported non-positive "
            "DPR %s; using 1.0 to keep the paint path from dividing by zero",
            monitor.hmonitor,
            dpr,
        )
        dpr = 1.0

    # Step 1: make the rect LOCAL to the target screen (subtract the
    # monitor's physical top-left). Step 2: divide by the monitor DPR to
    # get LOGICAL coordinates. x/y and width/height are converted
    # independently so origin rounding never distorts the size.
    local_phys_left = left - origin_x
    local_phys_top = top - origin_y
    phys_width = right - left
    phys_height = bottom - top

    return OverlayPaintRect(
        x=int(round(local_phys_left / dpr)),
        y=int(round(local_phys_top / dpr)),
        # Floor each positive physical extent to at least 1 logical pixel:
        # the non-positive-extent guard above already returned None for
        # zero-area / inverted bounds, so phys_width and phys_height are both
        # strictly positive here. A sub-logical-pixel control (e.g. 1px on a
        # 2x monitor: 1/2=0.5 -> round-half-to-even -> 0) must not collapse to
        # a zero-size paint rect, or its badge would have nothing to anchor to.
        width=max(1, int(round(phys_width / dpr))),
        height=max(1, int(round(phys_height / dpr))),
        monitor=monitor,
        hmonitor=monitor.hmonitor,
        screen=screen,
    )
