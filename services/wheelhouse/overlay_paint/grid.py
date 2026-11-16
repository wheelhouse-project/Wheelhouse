"""OverlayGridPainter: the mouse grid painting (area P5).

wh-full-refactor.7.2 (stage B of place 4) moved this code out of
OverlayPaintWindowManager in overlay_paint_window.py without a change in
behavior. The stage A boundary tests (tests/test_overlay_gui_requirements_p5.py,
docs/design/overlay-gui-requirements.md, rows GP-n) pin that behavior.
OverlayPaintWindowManager creates one instance as self._grid and keeps a
delegator for paint_grid, paint_grid_pin and clear_grid. This class reads the
manager members it does not own through self._host (see host.py), and calls
destroy_all on the manager's OverlayPaintTeardown. It runs in the GUI process.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QLineF, QRect, QRectF, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen

from overlay_paint_window import (
    _GRID_INK_MARGIN_LOGICAL_PX,
    _NUMERAL_COLOR,
    _OUTLINE_COLOR,
    _SHADOW_COLOR,
    _SHADOW_OFFSET_PX,
    _OverlayWindow,
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
from shared.overlay_bitmap import build_layered_dib

if TYPE_CHECKING:
    from overlay_paint.host import OverlayGridHost, OverlayTeardownPort
    from shared.paint_grid import PaintGridEvent
    from shared.paint_grid_pin import PaintGridPinEvent

# The logger of overlay_paint_window.py, by name, so the moved code logs
# under the same logger name as before the move.
logger = logging.getLogger("overlay_paint_window")

# ---------------------------------------------------------------------------
# Mouse-grid drawing mode (wh-grid-paint-mode). wh-full-refactor.7.3 moved
# these constants here from overlay_paint_window.py, with the same names and
# values. Spec:
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


class OverlayGridPainter:
    """The mouse grid drawing of one OverlayPaintWindowManager.

    It owns the retained grid event and pin (_grid_event, _grid_pin). The
    windows, the debt cohort and the window class stay on the manager.
    """

    def __init__(
        self, host: OverlayGridHost, teardown: OverlayTeardownPort,
    ) -> None:
        """``host`` is the OverlayPaintWindowManager that owns this object;
        ``teardown`` is that manager's OverlayPaintTeardown."""
        self._host = host
        self._teardown = teardown
        # The mouse grid's retained drawing (wh-grid-paint-mode). The grid and
        # its pin arrive as SEPARATE Logic -> GUI events, and neither may
        # erase the other: "mark" paints a pin and then resets the grid to the
        # full monitor, so a paint_grid immediately follows a paint_grid_pin
        # and both must end up on screen. UpdateLayeredWindow replaces the
        # WHOLE window surface per call, so there is no way to add the pin to
        # an already-composited surface -- this painter therefore keeps the
        # last of each and repaints BOTH on every grid event. That is the only
        # state the grid mode holds, and it is a memory of what was drawn, not
        # a decision: no cell arithmetic, no refinement tracking, no monitor
        # choice. Both are dropped by clear_grid.
        self._grid_event: Optional[PaintGridEvent] = None
        self._grid_pin: Optional[PaintGridPinEvent] = None

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
    # ``clear``, so ONE manager instance must serve one feature: the
    # GuiManager constructor constructs a DEDICATED manager for the grid,
    # exactly as it already does for the dictation working badge. Mixing them on one instance would make
    # a numbered-overlay clear destroy the grid's windows. There is no
    # generation gate: the grid has no in-flight build phase, so Logic never
    # has more than one grid outstanding and every event is a complete
    # description of what should be on screen (see shared/clear_grid.py).

    def paint_grid(self, event: PaintGridEvent) -> bool:
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

    def paint_grid_pin(self, event: PaintGridPinEvent) -> bool:
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

        Forgets both retained events and tears down every window of the
        manager that owns this painter (``host.windows`` and
        ``host.pending_destroy``). The pin dies with the grid session by contract, which is why
        one teardown removes both (see shared/clear_grid.py).
        """
        self._grid_event = None
        self._grid_pin = None
        self._teardown.destroy_all()

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
            self._teardown.destroy_all()
            return False

    def _do_render_grid(self) -> bool:
        """Resolve monitors, (re)create windows, and composite the grid.

        Mirrors ``_do_paint``'s window discipline exactly: retry rebuild-path
        orphans, enumerate the topology ONCE, tear down monitors absent from
        this render BEFORE creating new ones, rebuild a window whose geometry
        changed (so no stale, differently-sized DIB is reused under a refined
        grid), then composite each monitor's surface at its screen origin.
        """
        if self._host.pending_destroy:
            self._host.pending_destroy = [
                window
                for window in self._host.pending_destroy
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
        for hmonitor in list(self._host.windows.keys()):
            if hmonitor not in regions:
                window = self._host.windows.pop(hmonitor)
                if not window.destroy():
                    self._host.windows[hmonitor] = window

        if not regions:
            return False

        h_instance = self._host.ensure_class_registered()
        for hmonitor, geom in regions.items():
            existing = self._host.windows.get(hmonitor)
            if existing is None or existing.geom_phys != geom:
                if existing is not None:
                    old = self._host.windows.pop(hmonitor)
                    if not old.destroy():
                        self._host.pending_destroy.append(old)
                self._host.windows[hmonitor] = _OverlayWindow(
                    hmonitor=hmonitor,
                    geom_phys=geom,
                    class_name=self._host.class_name,
                    user32=self._host.user32,
                    kernel32=self._host.kernel32,
                    h_instance=h_instance,
                )

        all_ok = True
        for hmonitor, geom in regions.items():
            window = self._host.windows.get(hmonitor)
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
                    self._host.windows.pop(hmonitor, None)
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

        if self._host.badge_shadow:
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
