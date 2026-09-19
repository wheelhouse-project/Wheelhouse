"""Unit tests for the GUI-side mouse-grid drawing mode of
``overlay_paint_window.py`` (bead wh-grid-paint-mode under the
``wh-mouse-grid`` molecule).

Spec: ``docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md``,
section "GUI process -- painting only": Logic sends "paint this rectangle
as a grid" / "paint a pin at this point" / "clear"; the GUI draws and
decides nothing. No cell arithmetic beyond dividing the rectangle it was
handed into nine equal parts, and no state beyond remembering the last
grid rectangle and the last pin so that either can be repainted when the
other changes.

The drawing mode reuses the numbered overlay's per-monitor click-through
layered-window machinery (``_OverlayWindow``, the process-global window
class, ``build_layered_dib`` / ``composite_layered_window``), so these
tests mirror ``tests/test_overlay_paint_window.py``: ctypes (the Win32
window lifecycle) and the bitmap bridge are mocked, and the native-monitor
enumeration is patched, so no real on-screen window is required.

Groups:
1. Window lifecycle (one window on the event's monitor, click-through
   ex-style, geometry, negative virtual-desktop origins, teardown).
2. Cell geometry (three-by-three subdivision, telephone-keypad order).
3. Real rendering (ink actually lands on the lines and in every cell).
4. Grid + pin composition (neither event erases the other).
5. Coexistence with the numbered overlay (separate managers, no
   cross-teardown).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from unittest.mock import MagicMock, patch

import pytest

# Keep GuiManager construction free of real QDialogs in this file
# (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")

from PySide6.QtCore import QRect, QRectF

from services.wheelhouse.grid_overlay_state import (
    GridRect,
    cell_center,
    cell_rects,
)
from services.wheelhouse.shared.clear_grid import ClearGridEvent
from services.wheelhouse.shared.paint_grid import PaintGridEvent
from services.wheelhouse.shared.paint_grid_pin import PaintGridPinEvent
from shared.monitor_geometry import _NativeMonitor
from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


_WS_POPUP = 0x80000000
_EXPECTED_EX = (
    0x00080000  # WS_EX_LAYERED
    | 0x00000008  # WS_EX_TOPMOST
    | 0x00000020  # WS_EX_TRANSPARENT
    | 0x08000000  # WS_EX_NOACTIVATE
    | 0x00000080  # WS_EX_TOOLWINDOW
)


def _monitor(
    hmonitor: int = 10,
    left: int = 0,
    top: int = 0,
    width: int = 1920,
    height: int = 1080,
    dpi: int = 96,
) -> _NativeMonitor:
    return _NativeMonitor(
        hmonitor=hmonitor,
        rect_phys=QRect(left, top, width, height),
        dpi=dpi,
    )


def _grid_event(monitor: _NativeMonitor, rect=None) -> PaintGridEvent:
    """A PaintGridEvent whose inner rectangle defaults to the whole monitor."""
    mon = monitor.rect_phys
    left, top, width, height = rect or (
        mon.left(), mon.top(), mon.width(), mon.height()
    )
    return PaintGridEvent(
        monitor_left=mon.left(),
        monitor_top=mon.top(),
        monitor_width=mon.width(),
        monitor_height=mon.height(),
        left=left,
        top=top,
        width=width,
        height=height,
    )


@pytest.fixture
def mock_win_apis():
    """Mock Windows DLLs for the overlay paint window."""
    user32 = MagicMock()
    gdi32 = MagicMock()
    kernel32 = MagicMock()

    user32.DefWindowProcW.return_value = 0
    user32.RegisterClassExW.return_value = 1
    user32.CreateWindowExW.return_value = wintypes.HWND(0xABCD)
    user32.DestroyWindow.return_value = True
    user32.ShowWindow.return_value = True
    user32.GetDC.return_value = wintypes.HDC(0x1111)
    user32.ReleaseDC.return_value = 1
    kernel32.GetModuleHandleW.return_value = wintypes.HMODULE(1)
    kernel32.GetLastError.return_value = 0
    return user32, gdi32, kernel32


@pytest.fixture
def grid_mgr(mock_win_apis, qapp):
    """An OverlayPaintWindowManager with mocked ctypes, used for the grid.

    Mirrors ``tests/test_overlay_paint_window.py::overlay_mgr``: real
    ctypes is passed through for POINTER / byref / sizeof / WINFUNCTYPE so
    the WNDCLASS registration builds, while the DLL calls are intercepted.
    """
    user32, gdi32, kernel32 = mock_win_apis
    with patch("overlay_paint_window.ctypes") as mock_ctypes:
        mock_ctypes.windll.user32 = user32
        mock_ctypes.windll.gdi32 = gdi32
        mock_ctypes.windll.kernel32 = kernel32
        mock_ctypes.POINTER = ctypes.POINTER
        mock_ctypes.byref = ctypes.byref
        mock_ctypes.sizeof = ctypes.sizeof
        mock_ctypes.WINFUNCTYPE = ctypes.WINFUNCTYPE
        mock_ctypes.WinError = ctypes.WinError
        mock_ctypes.c_ssize_t = ctypes.c_ssize_t
        mock_ctypes.c_int = ctypes.c_int
        mock_ctypes.Structure = ctypes.Structure

        import overlay_paint_window

        mgr = overlay_paint_window.OverlayPaintWindowManager()
        yield mgr, overlay_paint_window, (user32, gdi32, kernel32)
        mgr.clear_all()


def _patched_render(mod, monitors):
    """Context patches shared by every paint_grid call: the monitor topology
    plus the bitmap bridge (no real DIB / UpdateLayeredWindow)."""
    return (
        patch.object(mod, "_enumerate_native_monitors", return_value=monitors),
        patch.object(mod, "build_layered_dib", return_value=MagicMock()),
        patch.object(mod, "composite_layered_window", return_value=True),
    )


def _paint(mgr, mod, monitors, event):
    enum_p, dib_p, comp_p = _patched_render(mod, monitors)
    with enum_p, dib_p, comp_p:
        return mgr.paint_grid(event)


def _paint_pin(mgr, mod, monitors, event):
    enum_p, dib_p, comp_p = _patched_render(mod, monitors)
    with enum_p, dib_p, comp_p:
        return mgr.paint_grid_pin(event)


# ===========================================================================
# Group 1: window lifecycle
# ===========================================================================


class TestGridWindowLifecycle:
    def test_paint_grid_creates_one_clickthrough_window_on_its_monitor(
        self, grid_mgr
    ):
        mgr, mod, (user32, _, _) = grid_mgr
        mon = _monitor(hmonitor=10)
        other = _monitor(hmonitor=20, left=1920)

        assert _paint(mgr, mod, [mon, other], _grid_event(mon)) is True

        assert user32.CreateWindowExW.call_count == 1
        args = user32.CreateWindowExW.call_args[0]
        # Click-through, no-activate, layered, topmost, tool window -- the
        # same ex-style the numbered badges use.
        assert args[0] == _EXPECTED_EX
        assert args[3] == _WS_POPUP
        # Only the grid's own monitor gets a window.
        assert list(mgr._windows.keys()) == [10]

    def test_full_monitor_grid_window_covers_the_monitor(self, grid_mgr):
        mgr, mod, (user32, _, _) = grid_mgr
        mon = _monitor(hmonitor=10, left=300, top=50, width=800, height=600)

        _paint(mgr, mod, [mon], _grid_event(mon))

        args = user32.CreateWindowExW.call_args[0]
        # x, y, width, height: the ink margin is clamped away at the monitor
        # edges, so a full-monitor grid gets a full-monitor window.
        assert (args[4], args[5], args[6], args[7]) == (300, 50, 800, 600)

    def test_refined_grid_window_is_the_inner_rect_plus_ink_margin(
        self, grid_mgr
    ):
        """A refined (shrunken) rectangle gets a window sized to it, not to
        the monitor -- the transient surface scales with the drawing, the
        same reasoning as the numbered overlay's bounding-box surface."""
        mgr, mod, (user32, _, _) = grid_mgr
        mon = _monitor(hmonitor=10)
        margin = mod._GRID_INK_MARGIN_LOGICAL_PX  # dpr 1.0

        _paint(mgr, mod, [mon], _grid_event(mon, rect=(400, 300, 300, 300)))

        args = user32.CreateWindowExW.call_args[0]
        assert args[4] == 400 - int(margin)
        assert args[5] == 300 - int(margin)
        assert args[6] == 300 + 2 * int(margin)
        assert args[7] == 300 + 2 * int(margin)

    def test_negative_virtual_desktop_origin_is_preserved(self, grid_mgr):
        mgr, mod, (user32, _, _) = grid_mgr
        mon = _monitor(hmonitor=10, left=-1920, top=-200)

        _paint(mgr, mod, [mon], _grid_event(mon))

        args = user32.CreateWindowExW.call_args[0]
        assert args[4] == -1920
        assert args[5] == -200

    def test_refinement_rebuilds_the_window_for_the_new_rect(self, grid_mgr):
        mgr, mod, (user32, _, _) = grid_mgr
        mon = _monitor(hmonitor=10)

        _paint(mgr, mod, [mon], _grid_event(mon))
        user32.reset_mock()
        _paint(mgr, mod, [mon], _grid_event(mon, rect=(0, 0, 640, 360)))

        # The old, differently-sized window is destroyed and a new one made,
        # so no stale full-monitor DIB lingers under the refined grid.
        assert user32.DestroyWindow.call_count == 1
        assert user32.CreateWindowExW.call_count == 1

    def test_unknown_monitor_paints_nothing(self, grid_mgr):
        """A grid whose monitor rectangle matches no enumerated monitor (it
        was disconnected) draws nothing rather than guessing a monitor."""
        mgr, mod, (user32, _, _) = grid_mgr
        gone = _monitor(hmonitor=99, left=5000, top=5000)
        present = _monitor(hmonitor=10)

        assert _paint(mgr, mod, [present], _grid_event(gone)) is False
        assert user32.CreateWindowExW.call_count == 0

    def test_clear_grid_destroys_windows_and_forgets_grid_and_pin(
        self, grid_mgr
    ):
        mgr, mod, (user32, _, _) = grid_mgr
        mon = _monitor(hmonitor=10)
        _paint(mgr, mod, [mon], _grid_event(mon))
        _paint_pin(mgr, mod, [mon], PaintGridPinEvent(x=100, y=100))
        user32.reset_mock()

        mgr.clear_grid()

        assert user32.DestroyWindow.call_count >= 1
        assert mgr._windows == {}
        assert mgr._grid_event is None
        assert mgr._grid_pin is None

    def test_composite_uses_the_window_screen_origin_and_releases_dc(
        self, grid_mgr
    ):
        mgr, mod, (user32, _, _) = grid_mgr
        mon = _monitor(hmonitor=10, left=300, top=50, width=800, height=600)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ) as composite:
            mgr.paint_grid(_grid_event(mon))

        # composite_layered_window(hwnd, screen_dc, dib, dest_x, dest_y)
        assert composite.call_args[0][3] == 300
        assert composite.call_args[0][4] == 50
        user32.ReleaseDC.assert_called_once()

    def test_composite_failure_reports_false(self, grid_mgr):
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=False
        ):
            assert mgr.paint_grid(_grid_event(mon)) is False


# ===========================================================================
# Group 2: cell geometry (three-by-three, telephone-keypad order)
# ===========================================================================


class TestGridCellGeometry:
    def _label_calls(self, mgr, mod, monitors, event):
        calls: list[tuple[int, float, float]] = []

        def _record(_painter, number, cx, cy, *_a, **_k):
            calls.append((number, cx, cy))

        enum_p, dib_p, comp_p = _patched_render(mod, monitors)
        with enum_p, dib_p, comp_p, patch.object(
            mgr, "_draw_grid_label", side_effect=_record
        ):
            mgr.paint_grid(event)
        return calls

    def test_nine_labels_in_telephone_keypad_order(self, grid_mgr):
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10, width=1920, height=1080)

        calls = self._label_calls(mgr, mod, [mon], _grid_event(mon))

        assert [n for n, _x, _y in calls] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        # 1-2-3 top row, 4-5-6 middle, 7-8-9 bottom; each centered in its
        # cell of the 1920x1080 rectangle (cells 640x360).
        expected = {
            1: (320.0, 180.0), 2: (960.0, 180.0), 3: (1600.0, 180.0),
            4: (320.0, 540.0), 5: (960.0, 540.0), 6: (1600.0, 540.0),
            7: (320.0, 900.0), 8: (960.0, 900.0), 9: (1600.0, 900.0),
        }
        assert {n: (x, y) for n, x, y in calls} == expected

    def test_labels_follow_the_refined_rectangle(self, grid_mgr):
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10)

        calls = self._label_calls(
            mgr, mod, [mon], _grid_event(mon, rect=(600, 300, 300, 150))
        )

        # Cells are 100x50; cell 1's center is half a cell in from the
        # rectangle's top-left, cell 9's is half a cell in from its
        # bottom-right.
        assert calls[0] == (1, 650.0, 325.0)
        assert calls[8] == (9, 850.0, 425.0)

    def _expected_centers(self, rect: GridRect) -> dict:
        cells = cell_rects(rect)
        return {
            number: (
                float(cell_center(cells[number - 1]).x),
                float(cell_center(cells[number - 1]).y),
            )
            for number in range(1, 10)
        }

    def test_labels_sit_at_the_state_machines_integer_cell_centers(
        self, grid_mgr
    ):
        # 1366x770: neither dimension divides by 3, so equal float thirds
        # and the state machine's integer edge rule disagree by up to a
        # pixel. The spoken numbers act on grid_overlay_state.cell_rects /
        # cell_center; the painted labels must sit exactly there
        # (wh-mouse-grid.1.13).
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10, width=1366, height=770)

        calls = self._label_calls(mgr, mod, [mon], _grid_event(mon))

        expected = self._expected_centers(
            GridRect(left=0, top=0, width=1366, height=770)
        )
        assert {n: (x, y) for n, x, y in calls} == expected

    def test_labels_match_the_state_machine_on_a_negative_origin_monitor(
        self, grid_mgr
    ):
        # A monitor left of and above the primary: the integer edge rule
        # floors the non-negative size before adding the origin, so a
        # painter that floors the absolute coordinate instead would drift
        # (wh-mouse-grid.1.13).
        mgr, mod, _ = grid_mgr
        mon = _monitor(
            hmonitor=10, left=-1366, top=-770, width=1366, height=770
        )

        calls = self._label_calls(mgr, mod, [mon], _grid_event(mon))

        expected = self._expected_centers(
            GridRect(left=-1366, top=-770, width=1366, height=770)
        )
        assert {n: (x, y) for n, x, y in calls} == expected

    def test_refined_cell_labels_match_the_state_machine(self, grid_mgr):
        # A refined sub-rectangle whose sides do not divide by 3 -- the
        # case a user actually reaches after one refinement on a
        # non-divisible monitor (wh-mouse-grid.1.13).
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10)

        calls = self._label_calls(
            mgr, mod, [mon], _grid_event(mon, rect=(600, 300, 301, 151))
        )

        expected = self._expected_centers(
            GridRect(left=600, top=300, width=301, height=151)
        )
        assert {n: (x, y) for n, x, y in calls} == expected

    def test_division_lines_sit_at_the_state_machines_integer_edges(
        self, grid_mgr
    ):
        # The lines must sit on the same integer edges cell_rects tiles by
        # ((size * i) // 3), not on float thirds, or a click lands in a
        # cell whose painted boundary is a pixel away from the boundary the
        # state machine used (wh-mouse-grid.1.13).
        mgr, _mod, _ = grid_mgr
        painter = MagicMock()

        mgr._draw_grid_cells(painter, QRectF(0, 0, 1366, 770), 1.0)

        lines = painter.drawLines.call_args[0][0]
        xs = sorted({ln.x1() for ln in lines if ln.x1() == ln.x2()})
        ys = sorted({ln.y1() for ln in lines if ln.y1() == ln.y2()})
        # (1366 * 1) // 3 == 455, (1366 * 2) // 3 == 910;
        # (770 * 1) // 3 == 256, (770 * 2) // 3 == 513.
        assert xs == [0.0, 455.0, 910.0, 1366.0]
        assert ys == [0.0, 256.0, 513.0, 770.0]

    def test_label_size_scales_with_the_cell(self, grid_mgr):
        mgr, mod, _ = grid_mgr
        big = mod.OverlayPaintWindowManager._grid_label_pixel_size(
            600.0, 600.0, 1.0
        )
        small = mod.OverlayPaintWindowManager._grid_label_pixel_size(
            40.0, 40.0, 1.0
        )
        assert big > small
        # Readable at full-monitor size but never larger than the cap, and
        # never smaller than the legibility floor at tiny cell sizes.
        assert big == int(round(mod._GRID_LABEL_MAX_LOGICAL_PX))
        tiny = mod.OverlayPaintWindowManager._grid_label_pixel_size(
            6.0, 6.0, 1.0
        )
        assert tiny == int(round(mod._GRID_LABEL_MIN_LOGICAL_PX))

    def test_label_size_scales_with_monitor_dpr(self, grid_mgr):
        """A 200%-scaled monitor gets twice the physical pixel size for the
        same perceived label size (mixed-resolution desktops)."""
        mgr, mod, _ = grid_mgr
        at_1 = mod.OverlayPaintWindowManager._grid_label_pixel_size(
            600.0, 600.0, 1.0
        )
        at_2 = mod.OverlayPaintWindowManager._grid_label_pixel_size(
            600.0, 600.0, 2.0
        )
        assert at_2 == at_1 * 2


# ===========================================================================
# Group 3: real rendering (ink lands where it should)
# ===========================================================================


def _opaque_count(image, x0: int, y0: int, x1: int, y1: int) -> int:
    """Count pixels with non-zero alpha in [x0, x1) x [y0, y1)."""
    total = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            if (image.pixel(x, y) >> 24) & 0xFF:
                total += 1
    return total


class TestGridRendering:
    def _surface(self, mgr, mod, monitors, event, pin=None):
        surfaces: list = []
        real = mgr._render_grid_surface

        def _capture(*args, **kwargs):
            image = real(*args, **kwargs)
            surfaces.append(image)
            return image

        enum_p, dib_p, comp_p = _patched_render(mod, monitors)
        with enum_p, dib_p, comp_p, patch.object(
            mgr, "_render_grid_surface", side_effect=_capture
        ):
            if event is not None:
                mgr.paint_grid(event)
            if pin is not None:
                mgr.paint_grid_pin(pin)
        return surfaces[-1]

    def test_every_cell_gets_a_visible_number(self, grid_mgr):
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10, width=300, height=300)

        surface = self._surface(mgr, mod, [mon], _grid_event(mon))

        assert surface.width() == 300 and surface.height() == 300
        for row in range(3):
            for col in range(3):
                cx, cy = col * 100 + 50, row * 100 + 50
                ink = _opaque_count(surface, cx - 30, cy - 30, cx + 30, cy + 30)
                assert ink > 0, f"cell r{row}c{col} has no label ink"

    def test_interior_division_lines_are_drawn(self, grid_mgr):
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10, width=300, height=300)

        surface = self._surface(mgr, mod, [mon], _grid_event(mon))

        # A vertical line at x = 100 and a horizontal at y = 200, sampled
        # away from the cell labels.
        assert _opaque_count(surface, 98, 5, 103, 10) > 0
        assert _opaque_count(surface, 5, 198, 10, 203) > 0
        # ...and the space between lines, away from any label, is clear.
        assert _opaque_count(surface, 130, 5, 150, 12) == 0

    def test_pin_draws_ink_at_its_point(self, grid_mgr):
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10, width=300, height=300)

        surface = self._surface(
            mgr,
            mod,
            [mon],
            _grid_event(mon),
            pin=PaintGridPinEvent(x=150, y=20),
        )

        # The crosshair sits on the point; the strip beyond the arm's reach
        # (clear of the grid line at x = 100 and of any label) stays empty.
        assert _opaque_count(surface, 145, 15, 156, 26) > 0
        assert _opaque_count(surface, 118, 15, 133, 26) == 0


# ===========================================================================
# Group 4: grid + pin composition
# ===========================================================================


class TestGridPinComposition:
    def test_a_later_grid_paint_keeps_the_pin(self, grid_mgr):
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10)
        _paint_pin(mgr, mod, [mon], PaintGridPinEvent(x=500, y=400))

        drawn: list[tuple[float, float]] = []
        enum_p, dib_p, comp_p = _patched_render(mod, [mon])
        with enum_p, dib_p, comp_p, patch.object(
            mgr,
            "_draw_grid_pin",
            side_effect=lambda _p, x, y, *_a, **_k: drawn.append((x, y)),
        ):
            mgr.paint_grid(_grid_event(mon, rect=(0, 0, 640, 360)))

        assert drawn == [(500.0, 400.0)]

    def test_a_later_pin_keeps_the_grid(self, grid_mgr):
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10)
        _paint(mgr, mod, [mon], _grid_event(mon, rect=(0, 0, 640, 360)))

        labels: list[int] = []
        enum_p, dib_p, comp_p = _patched_render(mod, [mon])
        with enum_p, dib_p, comp_p, patch.object(
            mgr,
            "_draw_grid_label",
            side_effect=lambda _p, n, *_a, **_k: labels.append(n),
        ):
            mgr.paint_grid_pin(PaintGridPinEvent(x=500, y=400))

        assert labels == [1, 2, 3, 4, 5, 6, 7, 8, 9]

    def test_pin_alone_paints_without_a_grid(self, grid_mgr):
        mgr, mod, (user32, _, _) = grid_mgr
        mon = _monitor(hmonitor=10)

        assert _paint_pin(mgr, mod, [mon], PaintGridPinEvent(x=50, y=60)) is True
        assert user32.CreateWindowExW.call_count == 1

    def test_pin_on_another_monitor_gets_its_own_window(self, grid_mgr):
        mgr, mod, (user32, _, _) = grid_mgr
        mon_a = _monitor(hmonitor=10, left=0)
        mon_b = _monitor(hmonitor=20, left=1920)
        _paint(mgr, mod, [mon_a, mon_b], _grid_event(mon_a))
        user32.reset_mock()

        _paint_pin(mgr, mod, [mon_a, mon_b], PaintGridPinEvent(x=2500, y=300))

        assert sorted(mgr._windows.keys()) == [10, 20]

    def test_pin_off_every_monitor_is_dropped(self, grid_mgr):
        mgr, mod, (user32, _, _) = grid_mgr
        mon = _monitor(hmonitor=10)

        _paint_pin(mgr, mod, [mon], PaintGridPinEvent(x=99999, y=99999))

        assert user32.CreateWindowExW.call_count == 0

    def test_clear_then_paint_starts_from_a_clean_slate(self, grid_mgr):
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10)
        _paint_pin(mgr, mod, [mon], PaintGridPinEvent(x=500, y=400))
        mgr.clear_grid()

        drawn: list[tuple[float, float]] = []
        enum_p, dib_p, comp_p = _patched_render(mod, [mon])
        with enum_p, dib_p, comp_p, patch.object(
            mgr,
            "_draw_grid_pin",
            side_effect=lambda _p, x, y, *_a, **_k: drawn.append((x, y)),
        ):
            mgr.paint_grid(_grid_event(mon))

        assert drawn == []


# ===========================================================================
# Group 5: coexistence with the numbered overlay
# ===========================================================================


class TestOverlayCoexistence:
    """The Logic side guarantees the two overlays are mutually exclusive; the
    GUI does not enforce it, but a grid painted while badges are up (or the
    reverse) must not crash or tear down the other manager's windows."""

    def _paint_badges(self, mgr, mod, monitor):
        from shared.overlay_dpi_resolver import OverlayPaintRect

        rect = OverlayPaintRect(
            x=5, y=5, width=100, height=40,
            monitor=monitor, hmonitor=monitor.hmonitor, screen=None,
        )
        item = WalkSnapshotSummaryItem(
            item_id="item-1", display_number=1, name="ok",
            role="Button", bounds=(10, 20, 100, 40), monitor_id=0,
        )
        summary = WalkSnapshotSummary(
            snapshot_id="snap-1", items=[item], created_at_monotonic=1.0,
        )
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[monitor]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=rect
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            mgr.paint(summary, overlay_session_id=1, paint_generation=0)

    def test_grid_paint_leaves_badge_windows_alone(self, grid_mgr):
        badge_mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10)
        self._paint_badges(badge_mgr, mod, mon)
        badge_windows = dict(badge_mgr._windows)
        assert badge_windows

        grid = mod.OverlayPaintWindowManager()
        try:
            assert _paint(grid, mod, [mon], _grid_event(mon)) is True
            assert badge_mgr._windows == badge_windows
            grid.clear_grid()
            assert badge_mgr._windows == badge_windows
        finally:
            grid.clear_all()

    def test_badge_paint_leaves_grid_windows_alone(self, grid_mgr):
        badge_mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10)
        grid = mod.OverlayPaintWindowManager()
        try:
            _paint(grid, mod, [mon], _grid_event(mon))
            grid_windows = dict(grid._windows)
            assert grid_windows

            self._paint_badges(badge_mgr, mod, mon)

            assert grid._windows == grid_windows
        finally:
            grid.clear_all()

    def test_clear_grid_event_schema_drives_the_teardown(self, grid_mgr):
        """``ClearGridEvent`` carries no fields; the manager's clear takes no
        arguments, so the schema and the handler agree."""
        mgr, mod, _ = grid_mgr
        mon = _monitor(hmonitor=10)
        _paint(mgr, mod, [mon], _grid_event(mon))

        ClearGridEvent.from_dict(ClearGridEvent().to_dict())
        mgr.clear_grid()

        assert mgr._windows == {}
