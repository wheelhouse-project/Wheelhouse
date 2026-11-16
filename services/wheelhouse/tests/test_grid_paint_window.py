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

from PySide6.QtCore import QRect

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
    import overlay_paint.grid

    return (
        patch.object(
            overlay_paint.grid, "_enumerate_native_monitors",
            return_value=monitors,
        ),
        patch.object(
            overlay_paint.grid, "build_layered_dib", return_value=MagicMock()
        ),
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

    def test_composite_uses_the_window_screen_origin_and_releases_dc(
        self, grid_mgr
    ):
        mgr, mod, (user32, _, _) = grid_mgr
        mon = _monitor(hmonitor=10, left=300, top=50, width=800, height=600)
        import overlay_paint.grid

        with patch.object(
            overlay_paint.grid, "_enumerate_native_monitors",
            return_value=[mon],
        ), patch.object(
            overlay_paint.grid, "build_layered_dib", return_value=MagicMock()
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
        import overlay_paint.grid

        with patch.object(
            overlay_paint.grid, "_enumerate_native_monitors",
            return_value=[mon],
        ), patch.object(
            overlay_paint.grid, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=False
        ):
            assert mgr.paint_grid(_grid_event(mon)) is False


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
        enum_p, dib_p, comp_p = _patched_render(mod, monitors)
        with enum_p, dib_p as dib_mock, comp_p:
            if event is not None:
                mgr.paint_grid(event)
            if pin is not None:
                mgr.paint_grid_pin(pin)
        # build_layered_dib(surface) receives the rendered surface as its
        # only positional argument.
        return dib_mock.call_args_list[-1].args[0]

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
