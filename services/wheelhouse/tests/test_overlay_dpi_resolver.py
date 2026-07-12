"""Tests for the overlay physical-to-logical paint-coordinate converter
(wh-n29v.49).

The converter under test lives in
``shared.overlay_dpi_resolver`` and turns one
display-safe overlay item -- its physical-pixel virtual-desktop bounds
plus an advisory ``monitor_id`` -- into Qt LOGICAL paint coordinates
LOCAL to the overlay window that covers the item's target monitor. The
converter COMPOSES the existing
``shared.monitor_geometry`` helpers
(``_resolve_target_monitor`` and ``_match_qscreen_for_monitor``); it
does NOT re-implement monitor resolution or the QScreen bridge.

Coordinate contract under test
------------------------------

Given physical bounds ``(left, top, right, bottom)`` in virtual-desktop
physical pixels (the same space UIA rects use) and the resolved monitor
``M`` (a ``_NativeMonitor``):

    local_phys_x = left - M.rect_phys.left()
    local_phys_y = top  - M.rect_phys.top()
    local_logical_x = local_phys_x / M.dpr
    local_logical_y = local_phys_y / M.dpr
    local_logical_w = (right - left) / M.dpr
    local_logical_h = (bottom - top) / M.dpr

The origin of the result is the TARGET screen's top-left (0, 0) -- the
overlay window is positioned over that screen, so paint coordinates are
screen-local logical, NOT virtual-desktop and NOT physical.

These tests drive the converter entirely through its dependency-
injection seams (``monitors=`` and ``screens=``) with synthetic
``_NativeMonitor`` lists and ``QScreen``-shaped stubs, modelled on
``tests/test_terminal_editor_geometry.py``. No live display or
``QGuiApplication`` is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, cast

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QScreen


# ---------------------------------------------------------------------------
# Synthetic-layout fixtures: native Win32 topology in physical pixels.
# Mirrors the layouts in tests/test_terminal_editor_geometry.py so the
# two geometry suites describe the same monitor world.
# ---------------------------------------------------------------------------


@pytest.fixture
def single_monitor_100():
    """One monitor: 1080p @ 96 DPI (1x DPR)."""
    from shared.monitor_geometry import _NativeMonitor

    return [
        _NativeMonitor(hmonitor=1001, rect_phys=QRect(0, 0, 1920, 1080), dpi=96),
    ]


@pytest.fixture
def single_monitor_150():
    """One monitor: 4K physical @ 144 DPI (1.5x DPR)."""
    from shared.monitor_geometry import _NativeMonitor

    return [
        _NativeMonitor(hmonitor=1001, rect_phys=QRect(0, 0, 3840, 2160), dpi=144),
    ]


@pytest.fixture
def single_monitor_200():
    """One monitor: 4K physical @ 192 DPI (2x DPR)."""
    from shared.monitor_geometry import _NativeMonitor

    return [
        _NativeMonitor(hmonitor=1001, rect_phys=QRect(0, 0, 3840, 2160), dpi=192),
    ]


@pytest.fixture
def dual_mixed_dpi_monitors():
    """Two side-by-side monitors with MIXED DPR.

    Monitor A (primary): 1080p @ 96 DPI (1x), rect_phys=QRect(0,0,1920,1080).
    Monitor B (secondary): 4K @ 144 DPI (1.5x), placed to the right of A
    at native physical x=1920, rect_phys=QRect(1920,0,3840,2160).

    Monitor B's PHYSICAL x-origin is 1920 (the physical extent of A),
    NOT a DPR-scaled position -- the Qt "islands-of-screens" model.
    """
    from shared.monitor_geometry import _NativeMonitor

    return [
        _NativeMonitor(hmonitor=1001, rect_phys=QRect(0, 0, 1920, 1080), dpi=96),
        _NativeMonitor(hmonitor=1002, rect_phys=QRect(1920, 0, 3840, 2160), dpi=144),
    ]


@pytest.fixture
def three_monitor_mixed_dpi():
    """Three side-by-side monitors with three distinct scalings.

    Monitor A: 1080p @ 96 DPI (1x),  rect_phys=QRect(0,0,1920,1080).
    Monitor B: 4K   @ 144 DPI (1.5x), rect_phys=QRect(1920,0,3840,2160).
    Monitor C: 4K   @ 192 DPI (2x),   rect_phys=QRect(5760,0,3840,2160).
    """
    from shared.monitor_geometry import _NativeMonitor

    return [
        _NativeMonitor(hmonitor=1001, rect_phys=QRect(0, 0, 1920, 1080), dpi=96),
        _NativeMonitor(hmonitor=1002, rect_phys=QRect(1920, 0, 3840, 2160), dpi=144),
        _NativeMonitor(hmonitor=1003, rect_phys=QRect(5760, 0, 3840, 2160), dpi=192),
    ]


# ---------------------------------------------------------------------------
# QScreen-shaped stub (same shape used by test_terminal_editor_geometry.py).
# ---------------------------------------------------------------------------


@dataclass
class _StubQScreen:
    """Minimal QScreen-shaped stub for the QScreen bridge.

    Implements only the three call sites the bridge consumes:
    ``handle()``, ``geometry()``, ``devicePixelRatio()``.
    """

    _logical_geometry: QRect
    _device_pixel_ratio: float
    _handle: Optional[int] = None
    _raise_on_handle: bool = False

    def handle(self):
        if self._raise_on_handle:
            raise RuntimeError("handle() raised on this platform")
        return self._handle

    def geometry(self) -> QRect:
        return QRect(self._logical_geometry)

    def devicePixelRatio(self) -> float:
        return self._device_pixel_ratio


def _logical_size_for_native(rect_phys: QRect, dpr: float) -> QRect:
    """Derive a logical-size QRect from a physical-size rect and a DPR.

    Origin set to (0, 0) because the bridge consumes only width/height +
    DPR, never the logical origin.
    """
    return QRect(0, 0, int(rect_phys.width() / dpr), int(rect_phys.height() / dpr))


def _screens_for(monitors) -> list[QScreen]:
    """Build HMONITOR-identity QScreen stubs for a monitor list.

    The stubs are structurally compatible with the three QScreen call
    sites the bridge uses; ``cast`` presents them as ``QScreen`` so the
    converter's typed ``screens`` seam accepts them with no live
    QGuiApplication.
    """
    return [
        cast(
            QScreen,
            _StubQScreen(
                _logical_geometry=_logical_size_for_native(m.rect_phys, m.dpr),
                _device_pixel_ratio=m.dpr,
                _handle=m.hmonitor,
            ),
        )
        for m in monitors
    ]


# ---------------------------------------------------------------------------
# Single-module-domain invariant: the converter and the canonical shared
# monitor_geometry module must resolve to ONE module object per process.
#
# Both the repo root and services/wheelhouse are on sys.path and there is no
# services/wheelhouse/shared/__init__.py, so the absolute spelling
# ('services.wheelhouse.shared.monitor_geometry') and the relative spelling
# ('shared.monitor_geometry') resolve to two DISTINCT sys.modules keys -> two
# copies of every helper / dataclass. This regression test pins the converter
# onto the same module object the rest of the codebase imports (the relative
# spelling) so exactly one monitor_geometry lives per process.
# ---------------------------------------------------------------------------


def test_converter_shares_single_monitor_geometry_module():
    import shared.monitor_geometry as canonical
    import shared.overlay_dpi_resolver as conv

    # The converter must import its composed helpers from the SAME module
    # object the rest of the codebase uses (the relative spelling), not from
    # a second copy loaded under the absolute 'services.wheelhouse.shared' key.
    assert conv._NativeMonitor is canonical._NativeMonitor
    assert conv._resolve_target_monitor is canonical._resolve_target_monitor
    assert conv._match_qscreen_for_monitor is canonical._match_qscreen_for_monitor
    assert conv._overlap_area is canonical._overlap_area


# ---------------------------------------------------------------------------
# Single monitor @ 100%: physical == logical, origin unchanged.
# ---------------------------------------------------------------------------


def test_single_monitor_100_percent_physical_equals_logical(single_monitor_100):
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(single_monitor_100)
    # Physical bounds (100, 200) extent 300x100 on the only monitor.
    result = resolve_overlay_paint_rect(
        bounds=(100, 200, 400, 300),
        monitor_id=999,  # advisory only; ignored for resolution
        monitors=single_monitor_100,
        screens=screens,
    )
    assert result is not None
    # 1x DPR: logical local coords equal physical local coords, and the
    # monitor origin is (0, 0) so virtual == local.
    assert result.x == 100
    assert result.y == 200
    assert result.width == 300
    assert result.height == 100


def test_single_monitor_100_returns_resolved_identity(single_monitor_100):
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(single_monitor_100)
    result = resolve_overlay_paint_rect(
        bounds=(0, 0, 100, 100),
        monitor_id=0,
        monitors=single_monitor_100,
        screens=screens,
    )
    assert result is not None
    # The resolved monitor and QScreen identity ride back so the caller
    # can pick the right overlay window.
    assert result.monitor is single_monitor_100[0]
    assert result.screen is screens[0]
    assert result.hmonitor == single_monitor_100[0].hmonitor


# ---------------------------------------------------------------------------
# Single monitor @ 150% and @ 200%: divide local physical by the DPR.
# ---------------------------------------------------------------------------


def test_single_monitor_150_percent_divides_by_dpr(single_monitor_150):
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(single_monitor_150)
    # Physical (300, 600) extent 600x150 on a 1.5x monitor at origin 0.
    result = resolve_overlay_paint_rect(
        bounds=(300, 600, 900, 750),
        monitor_id=7,
        monitors=single_monitor_150,
        screens=screens,
    )
    assert result is not None
    # 1.5x DPR: 300/1.5=200, 600/1.5=400, 600/1.5=400, 150/1.5=100.
    assert result.x == 200
    assert result.y == 400
    assert result.width == 400
    assert result.height == 100


def test_single_monitor_200_percent_divides_by_dpr(single_monitor_200):
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(single_monitor_200)
    # Physical (200, 400) extent 800x200 on a 2x monitor at origin 0.
    result = resolve_overlay_paint_rect(
        bounds=(200, 400, 1000, 600),
        monitor_id=3,
        monitors=single_monitor_200,
        screens=screens,
    )
    assert result is not None
    # 2x DPR: 200/2=100, 400/2=200, 800/2=400, 200/2=100.
    assert result.x == 100
    assert result.y == 200
    assert result.width == 400
    assert result.height == 100


# ---------------------------------------------------------------------------
# Two side-by-side monitors, MIXED DPR: the per-screen origin transform.
# ---------------------------------------------------------------------------


def test_dual_item_on_left_monitor_resolves_left(dual_mixed_dpi_monitors):
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(dual_mixed_dpi_monitors)
    # Physical bounds well inside monitor A (x=0..1920, 1x DPR).
    result = resolve_overlay_paint_rect(
        bounds=(100, 100, 500, 300),
        monitor_id=0,
        monitors=dual_mixed_dpi_monitors,
        screens=screens,
    )
    assert result is not None
    assert result.monitor is dual_mixed_dpi_monitors[0]
    assert result.screen is screens[0]
    # 1x DPR on A, origin (0,0): local logical == physical.
    assert result.x == 100
    assert result.y == 100
    assert result.width == 400
    assert result.height == 200


def test_dual_item_on_right_monitor_origin_is_screen_local(dual_mixed_dpi_monitors):
    """The headline mixed-DPI case: an item on the RIGHT monitor must
    have its origin made relative to the RIGHT screen's physical
    top-left (1920, 0), then divided by the RIGHT monitor's DPR (1.5),
    NOT left at virtual-desktop coordinates.
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(dual_mixed_dpi_monitors)
    # Monitor B physical extent x=1920..5760 @ 1.5x DPR.
    # Physical bounds (1920+300, 600) extent 600x150 -> virtual (2220, 600).
    result = resolve_overlay_paint_rect(
        bounds=(2220, 600, 2820, 750),
        monitor_id=1,
        monitors=dual_mixed_dpi_monitors,
        screens=screens,
    )
    assert result is not None
    assert result.monitor is dual_mixed_dpi_monitors[1]
    assert result.screen is screens[1]
    # Local physical: x=2220-1920=300, y=600-0=600. Divide by 1.5:
    # 300/1.5=200, 600/1.5=400, 600/1.5=400, 150/1.5=100.
    assert result.x == 200
    assert result.y == 400
    assert result.width == 400
    assert result.height == 100


def test_dual_right_monitor_top_left_corner_maps_to_origin(dual_mixed_dpi_monitors):
    """An item flush at the right monitor's physical top-left must map to
    local logical (0, 0) -- proving the origin subtraction, not the
    virtual-desktop coordinate, drives the result.
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(dual_mixed_dpi_monitors)
    # Physical top-left of monitor B is (1920, 0).
    result = resolve_overlay_paint_rect(
        bounds=(1920, 0, 2220, 150),
        monitor_id=1,
        monitors=dual_mixed_dpi_monitors,
        screens=screens,
    )
    assert result is not None
    assert result.x == 0
    assert result.y == 0
    # 300x150 physical / 1.5 -> 200x100 logical.
    assert result.width == 200
    assert result.height == 100


# ---------------------------------------------------------------------------
# Three monitors, three distinct scalings.
# ---------------------------------------------------------------------------


def test_three_monitor_left_item(three_monitor_mixed_dpi):
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(three_monitor_mixed_dpi)
    result = resolve_overlay_paint_rect(
        bounds=(50, 50, 450, 250),
        monitor_id=0,
        monitors=three_monitor_mixed_dpi,
        screens=screens,
    )
    assert result is not None
    assert result.monitor is three_monitor_mixed_dpi[0]
    assert result.screen is screens[0]
    # 1x DPR, origin 0: local == physical.
    assert result.x == 50
    assert result.y == 50
    assert result.width == 400
    assert result.height == 200


def test_three_monitor_middle_item(three_monitor_mixed_dpi):
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(three_monitor_mixed_dpi)
    # Monitor B physical extent x=1920..5760 @ 1.5x DPR.
    # Virtual (1920+600, 300) extent 900x150.
    result = resolve_overlay_paint_rect(
        bounds=(2520, 300, 3420, 450),
        monitor_id=1,
        monitors=three_monitor_mixed_dpi,
        screens=screens,
    )
    assert result is not None
    assert result.monitor is three_monitor_mixed_dpi[1]
    assert result.screen is screens[1]
    # Local physical x=2520-1920=600, y=300. /1.5: 400, 200, 600, 100.
    assert result.x == 400
    assert result.y == 200
    assert result.width == 600
    assert result.height == 100


def test_three_monitor_right_item(three_monitor_mixed_dpi):
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(three_monitor_mixed_dpi)
    # Monitor C physical extent x=5760..9600 @ 2x DPR.
    # Virtual (5760+400, 200) extent 800x200.
    result = resolve_overlay_paint_rect(
        bounds=(6160, 200, 6960, 400),
        monitor_id=2,
        monitors=three_monitor_mixed_dpi,
        screens=screens,
    )
    assert result is not None
    assert result.monitor is three_monitor_mixed_dpi[2]
    assert result.screen is screens[2]
    # Local physical x=6160-5760=400, y=200. /2: 200, 100, 400, 100.
    assert result.x == 200
    assert result.y == 100
    assert result.width == 400
    assert result.height == 100


# ---------------------------------------------------------------------------
# QScreen identity is returned for caller's overlay-window selection.
# ---------------------------------------------------------------------------


def test_resolved_screen_identity_threads_through(three_monitor_mixed_dpi):
    """Each item resolves to the correct monitor AND the correct QScreen,
    so the caller can select the matching overlay window.
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(three_monitor_mixed_dpi)
    cases = [
        ((10, 10, 110, 110), 0),
        ((2000, 10, 2100, 110), 1),
        ((5800, 10, 5900, 110), 2),
    ]
    for bounds, expected_idx in cases:
        result = resolve_overlay_paint_rect(
            bounds=bounds,
            monitor_id=expected_idx,
            monitors=three_monitor_mixed_dpi,
            screens=screens,
        )
        assert result is not None
        assert result.screen is screens[expected_idx]
        assert result.monitor is three_monitor_mixed_dpi[expected_idx]


# ---------------------------------------------------------------------------
# monitor_id is advisory: resolution is bounds-driven (namespace fence).
# ---------------------------------------------------------------------------


def test_monitor_id_is_advisory_resolution_follows_bounds(dual_mixed_dpi_monitors):
    """A wrong/opaque monitor_id must not steer resolution: the bounds
    place the item on the RIGHT monitor, so it must resolve to the right
    monitor regardless of the monitor_id value. This is the cross-process
    namespace fence -- monitor_id is NOT assumed to equal any hmonitor.
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(dual_mixed_dpi_monitors)
    # Bounds clearly on monitor B, but monitor_id claims a nonsense value
    # that matches neither hmonitor and is not a valid list index either.
    result = resolve_overlay_paint_rect(
        bounds=(3000, 300, 3400, 600),
        monitor_id=987654321,
        monitors=dual_mixed_dpi_monitors,
        screens=screens,
    )
    assert result is not None
    assert result.monitor is dual_mixed_dpi_monitors[1]
    assert result.screen is screens[1]


# ---------------------------------------------------------------------------
# Degenerate: empty monitor list -> documented safe fallback, no crash.
# ---------------------------------------------------------------------------


def test_empty_monitor_list_returns_none_safely():
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    result = resolve_overlay_paint_rect(
        bounds=(0, 0, 100, 100),
        monitor_id=0,
        monitors=[],
        screens=[],
    )
    # No topology to resolve against: the converter returns the documented
    # safe fallback (None) rather than raising.
    assert result is None


def test_no_matching_qscreen_still_returns_logical_rect(dual_mixed_dpi_monitors):
    """If the QScreen bridge cannot match a screen (screens=[]), the
    converter still resolves the monitor from bounds and returns the
    local logical rect with screen=None, so the caller can fall back to
    the primary screen for the overlay window.
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    # Item on monitor B (1.5x), but no screens to bridge to.
    result = resolve_overlay_paint_rect(
        bounds=(2220, 600, 2820, 750),
        monitor_id=1,
        monitors=dual_mixed_dpi_monitors,
        screens=[],
    )
    assert result is not None
    assert result.monitor is dual_mixed_dpi_monitors[1]
    assert result.screen is None
    # Local logical math is unaffected by the missing QScreen.
    assert result.x == 200
    assert result.y == 400
    assert result.width == 400
    assert result.height == 100


def test_secondary_monitor_no_qscreen_returns_secondary_hmonitor(
    dual_mixed_dpi_monitors,
):
    """When the QScreen bridge fails (screens=[]), the placement key is the
    resolved monitor's hmonitor -- which here is the SECONDARY monitor, not
    the primary. A hmonitor-keyed caller therefore places the badge on the
    correct screen; painting these monitor-local coords on the primary would
    put the badge on the wrong screen.
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    # Monitor B (secondary) physical extent x=1920..5760 @ 1.5x DPR.
    result = resolve_overlay_paint_rect(
        bounds=(2220, 600, 2820, 750),
        monitor_id=1,
        monitors=dual_mixed_dpi_monitors,
        screens=[],
    )
    assert result is not None
    assert result.screen is None
    assert result.monitor is dual_mixed_dpi_monitors[1]
    # The placement key identifies the SECONDARY monitor (hmonitor 1002).
    assert result.hmonitor == dual_mixed_dpi_monitors[1].hmonitor
    assert result.hmonitor == 1002


# ---------------------------------------------------------------------------
# Rounding rule: round-half-to-even at int paint boundaries, documented.
# ---------------------------------------------------------------------------


def test_fractional_dpr_rounding_is_deterministic(single_monitor_150):
    """A physical extent that does not divide evenly by 1.5 must round to
    an int deterministically. 100 phys / 1.5 = 66.666... -> 67.
    The converter's documented rule is int(round(...)) (Python 3
    banker's rounding at exact .5 boundaries, nearest-int otherwise);
    this case is unambiguous (not a .5 boundary) and just pins the int
    conversion.
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    screens = _screens_for(single_monitor_150)
    result = resolve_overlay_paint_rect(
        bounds=(0, 0, 100, 100),
        monitor_id=0,
        monitors=single_monitor_150,
        screens=screens,
    )
    assert result is not None
    # 100 / 1.5 = 66.666... -> 67 (nearest int).
    assert result.width == 67
    assert result.height == 67


# ---------------------------------------------------------------------------
# Round-half-to-even at exact .5 boundaries (size and origin).
# ---------------------------------------------------------------------------


def test_exact_half_pixel_size_rounds_half_to_even(single_monitor_200):
    """At an exact .5 boundary the size rounds half-to-even (Python 3
    banker's rounding via int(round(...))).
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    # bounds extent 5x3 on a 2x monitor at origin 0:
    #   width  5/2 = 2.5 -> 2 (round-half-to-even)
    #   height 3/2 = 1.5 -> 2 (round-half-to-even)
    result = resolve_overlay_paint_rect(
        bounds=(0, 0, 5, 3),
        monitor_id=0,
        monitors=single_monitor_200,
        screens=[],
    )
    assert result is not None
    assert result.width == 2
    assert result.height == 2

    # bounds extent 7x1 on the same 2x monitor:
    #   width  7/2 = 3.5 -> 4 (round-half-to-even)
    #   height 1/2 = 0.5 -> 0, floored to 1 (a real positive control never
    #   collapses to zero height)
    result = resolve_overlay_paint_rect(
        bounds=(0, 0, 7, 1),
        monitor_id=0,
        monitors=single_monitor_200,
        screens=[],
    )
    assert result is not None
    assert result.width == 4
    assert result.height == 1


def test_subpixel_positive_control_floors_to_one_logical_pixel(single_monitor_200):
    """A real positive-area control whose extents are sub-logical-pixel must
    never collapse to a zero-size paint rect. On a 2x monitor a 1x1 physical
    control is 0.5x0.5 logical -> round-half-to-even gives 0; the converter
    floors each positive extent to at least 1 logical pixel for width/height.
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    result = resolve_overlay_paint_rect(
        bounds=(0, 0, 1, 1),
        monitor_id=0,
        monitors=single_monitor_200,
        screens=[],
    )
    assert result is not None
    # 1/2 = 0.5 -> 0, floored to 1 on both axes.
    assert result.width == 1
    assert result.height == 1


def test_exact_half_pixel_origin_rounds_half_to_even(single_monitor_200):
    """At an exact .5 boundary the origin rounds half-to-even, independent
    of the size rounding.
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    # origin (5, 3) on a 2x monitor at origin 0:
    #   x 5/2 = 2.5 -> 2 (round-half-to-even)
    #   y 3/2 = 1.5 -> 2 (round-half-to-even)
    result = resolve_overlay_paint_rect(
        bounds=(5, 3, 105, 103),
        monitor_id=0,
        monitors=single_monitor_200,
        screens=[],
    )
    assert result is not None
    assert result.x == 2
    assert result.y == 2


# ---------------------------------------------------------------------------
# Degenerate DPR: a non-positive monitor DPR must not crash (divide guard).
# ---------------------------------------------------------------------------


def test_zero_dpr_monitor_does_not_crash():
    """A monitor injected with dpi=0 (an unenforced invariant breach via
    the monitors= seam) yields dpr==0.0; the converter must guard the
    divisions and fall back to dpr=1.0 rather than raise ZeroDivisionError.
    """
    from shared.monitor_geometry import _NativeMonitor
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    monitor = _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=0)
    result = resolve_overlay_paint_rect(
        bounds=(0, 0, 100, 100),
        monitor_id=0,
        monitors=[monitor],
        screens=[],
    )
    assert result is not None
    # dpr guard -> 1.0, so the rect is the physical extent unchanged.
    assert result.width == 100
    assert result.height == 100


# ---------------------------------------------------------------------------
# Zero-overlap / degenerate bounds: the resolved monitor does not actually
# contain the control -> return None rather than paint a wrong-screen badge.
# ---------------------------------------------------------------------------


def test_offscreen_bounds_return_none(single_monitor_100):
    """Bounds entirely off all monitors resolve to the fallback monitor
    (monitors[0]) but have zero overlap with it; the converter must return
    None so the caller skips the item rather than painting off-window.
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    # Entirely to the right of the only monitor (x 0..1920).
    result = resolve_overlay_paint_rect(
        bounds=(5000, 100, 5100, 300),
        monitor_id=0,
        monitors=single_monitor_100,
        screens=[],
    )
    assert result is None


def test_inverted_bounds_return_none(single_monitor_100):
    """Inverted bounds (right < left, bottom < top) form an invalid QRect
    with zero overlap; the converter must return None."""
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    result = resolve_overlay_paint_rect(
        bounds=(500, 500, 100, 100),
        monitor_id=0,
        monitors=single_monitor_100,
        screens=[],
    )
    assert result is None


def test_zero_size_bounds_return_none(single_monitor_100):
    """Zero-area bounds (a single point) have zero overlap; the converter
    must return None."""
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    result = resolve_overlay_paint_rect(
        bounds=(100, 100, 100, 100),
        monitor_id=0,
        monitors=single_monitor_100,
        screens=[],
    )
    assert result is None


def test_partial_offscreen_left_keeps_negative_origin(single_monitor_100):
    """A control partly off the LEFT edge still overlaps the monitor, so it
    resolves to a valid rect whose local origin is negative (the badge
    clips at the overlay window edge). A positive overlap is NOT None.
    """
    from shared.overlay_dpi_resolver import (
        resolve_overlay_paint_rect,
    )

    # Bounds x=-100..100 overlap monitor A (x 0..100); 1x DPR, origin 0.
    result = resolve_overlay_paint_rect(
        bounds=(-100, 100, 100, 300),
        monitor_id=0,
        monitors=single_monitor_100,
        screens=[],
    )
    assert result is not None
    assert result.x == -100
    assert result.y == 100
    assert result.width == 200
    assert result.height == 200
