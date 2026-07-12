"""Tests for the Win32 multi-monitor geometry resolver (wh-g2-refactor.16).

Section 4 of ``docs/design/2026-05-20-g2-refactor-design-refinements.md``
specifies a native Win32 monitor topology resolver that bridges UIA
physical-pixel rectangles to the correct Qt ``QScreen`` for the editor
to attach to. The implementation lives in
``services.wheelhouse.shared.monitor_geometry`` so the GUI process can
import it without dragging Input-process plumbing along (the same
constraint slices 14 and 15 followed).

The module exports:

* ``_NativeMonitor`` -- frozen dataclass describing one Win32 monitor in
  virtual-desktop physical coordinates plus its effective DPI.
* ``_overlap_area(a, b)`` -- pixel-count overlap helper, 0 if disjoint.
* ``_resolve_target_monitor(rect_phys, monitors=None)`` -- pick the
  ``_NativeMonitor`` with the largest physical overlap with
  ``rect_phys``; on no-overlap returns the first monitor (the primary
  on a normal Windows enumeration); on an empty topology returns
  ``None``.
* ``_match_qscreen_for_monitor(monitor, screens=None)`` -- map a
  ``_NativeMonitor`` back to the ``QScreen`` whose
  ``devicePixelRatio`` and physical dimensions match. The primary
  disambiguator is ``QScreen.handle() == HMONITOR`` when PySide6
  exposes it; the dimensions fallback is the safe surface beyond.

The synthetic-layout fixtures (single, dual mixed-DPI, three-monitor
mixed-DPI) drive ``_resolve_target_monitor`` through its
``monitors=`` dependency-injection seam, so no real multi-monitor rig
is required. ``_match_qscreen_for_monitor`` is exercised against
stub ``QScreen``-shaped objects with controllable
``devicePixelRatio()``, ``geometry()``, and ``handle()`` returns; no
``QApplication`` is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest
from PySide6.QtCore import QRect


# ---------------------------------------------------------------------------
# Synthetic-layout fixtures: native Win32 topology in physical pixels.
# ---------------------------------------------------------------------------


@pytest.fixture
def single_monitor():
    """One monitor: 1080p @ 96 DPI (1x DPR).

    Represents the minimum-spec laptop or single-display desktop.
    """
    from services.wheelhouse.shared.monitor_geometry import _NativeMonitor

    return [
        _NativeMonitor(
            hmonitor=1001,
            rect_phys=QRect(0, 0, 1920, 1080),
            dpi=96,
        ),
    ]


@pytest.fixture
def dual_mixed_dpi_monitors():
    """Two monitors, mixed DPI, side by side, in Win32 native coordinates.

    Monitor A (primary): 1080p @ 96 DPI (1x DPR).
        rect_phys = QRect(0, 0, 1920, 1080)
        dpi       = 96
    Monitor B (secondary): 4K @ 192 DPI (2x DPR), placed to the right
    of A at native x=1920 (the Windows positioning that round-2 codex
    flagged in wh-g2-refactor.7.1).
        rect_phys = QRect(1920, 0, 3840, 2160)
        dpi       = 192

    Note: Monitor B's PHYSICAL x-origin is 1920, NOT 3840. The
    "islands-of-screens" model Qt's high-DPI docs describe is exactly
    this: Windows positions the secondary in native pixels at the
    physical extent of the primary (1920), not at a DPR-scaled position.
    """
    from services.wheelhouse.shared.monitor_geometry import _NativeMonitor

    return [
        _NativeMonitor(
            hmonitor=1001,
            rect_phys=QRect(0, 0, 1920, 1080),
            dpi=96,
        ),
        _NativeMonitor(
            hmonitor=1002,
            rect_phys=QRect(1920, 0, 3840, 2160),
            dpi=192,
        ),
    ]


@pytest.fixture
def three_monitor_mixed_dpi():
    """Three monitors with mixed DPI, side by side.

    Monitor A (primary): 1080p @ 96 DPI (1x).
        rect_phys = QRect(0, 0, 1920, 1080), dpi = 96
    Monitor B: 4K @ 144 DPI (1.5x), to the right of A. Physical
    3840x2160 round-trips exactly to logical 2560x1440 at 1.5x DPR,
    avoiding the half-pixel ambiguity that 1440p physical * 1.5
    introduces.
        rect_phys = QRect(1920, 0, 3840, 2160), dpi = 144
    Monitor C: 4K @ 192 DPI (2x), to the right of B.
        rect_phys = QRect(5760, 0, 3840, 2160), dpi = 192
    """
    from services.wheelhouse.shared.monitor_geometry import _NativeMonitor

    return [
        _NativeMonitor(
            hmonitor=1001,
            rect_phys=QRect(0, 0, 1920, 1080),
            dpi=96,
        ),
        _NativeMonitor(
            hmonitor=1002,
            rect_phys=QRect(1920, 0, 3840, 2160),
            dpi=144,
        ),
        _NativeMonitor(
            hmonitor=1003,
            rect_phys=QRect(5760, 0, 3840, 2160),
            dpi=192,
        ),
    ]


# ---------------------------------------------------------------------------
# QScreen stub for _match_qscreen_for_monitor tests.
# ---------------------------------------------------------------------------


@dataclass
class _StubQScreen:
    """Minimal QScreen-shaped stub for _match_qscreen_for_monitor.

    Only the three call sites the resolver uses are implemented:
    ``handle()``, ``geometry()``, ``devicePixelRatio()``. The
    ``geometry()`` is the LOGICAL geometry Qt would report on Windows
    (size scales by DPR, position is per Qt's "islands-of-screens"
    model on Windows -- but the bridge only consumes the SIZE, so the
    fixture is free to set the origin in whichever way is convenient
    for the test).
    """

    _logical_geometry: QRect
    _device_pixel_ratio: float
    _handle: Optional[int] = None
    _raise_on_handle: bool = False

    def handle(self):  # noqa: D401 - matches Qt's snake-case-free convention
        if self._raise_on_handle:
            raise RuntimeError("handle() raised on this platform")
        return self._handle

    def geometry(self) -> QRect:
        return QRect(self._logical_geometry)

    def devicePixelRatio(self) -> float:
        return self._device_pixel_ratio


def _logical_size_for_native(rect_phys: QRect, dpr: float) -> QRect:
    """Helper: derive a logical-size QRect from a physical-size rect
    and a DPR. Origin is set to (0, 0) because the resolver only reads
    width/height + DPR from QScreen; origin transforms are explicitly
    avoided per the round-2 codex finding (wh-g2-refactor.7.1).
    """
    return QRect(0, 0, int(rect_phys.width() / dpr), int(rect_phys.height() / dpr))


# ---------------------------------------------------------------------------
# _overlap_area: pure geometry helper.
# ---------------------------------------------------------------------------


def test_overlap_area_returns_zero_for_disjoint_rects():
    from services.wheelhouse.shared.monitor_geometry import _overlap_area

    a = QRect(0, 0, 100, 100)
    b = QRect(200, 200, 100, 100)
    assert _overlap_area(a, b) == 0


def test_overlap_area_returns_zero_for_touching_rects():
    from services.wheelhouse.shared.monitor_geometry import _overlap_area

    a = QRect(0, 0, 100, 100)
    b = QRect(100, 0, 100, 100)
    # Touching but not overlapping: QRect.intersected returns a
    # zero-area or invalid rect.
    assert _overlap_area(a, b) == 0


def test_overlap_area_returns_full_area_for_identical_rects():
    from services.wheelhouse.shared.monitor_geometry import _overlap_area

    a = QRect(10, 20, 100, 50)
    assert _overlap_area(a, a) == 100 * 50


def test_overlap_area_returns_intersection_area_for_partial_overlap():
    from services.wheelhouse.shared.monitor_geometry import _overlap_area

    a = QRect(0, 0, 100, 100)
    b = QRect(50, 50, 100, 100)
    # Intersection: QRect(50, 50, 50, 50), area = 2500.
    assert _overlap_area(a, b) == 2500


# ---------------------------------------------------------------------------
# _resolve_target_monitor: single-monitor layout.
# ---------------------------------------------------------------------------


def test_resolver_returns_only_monitor_on_single_monitor_layout(single_monitor):
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    rect = (100, 100, 600, 400)  # anywhere on the only monitor
    target = _resolve_target_monitor(rect, monitors=single_monitor)
    assert target is single_monitor[0]
    assert target.dpr == 1.0


def test_resolver_returns_only_monitor_even_for_off_screen_rect(single_monitor):
    """Codex round-2 contract: on no-overlap, return the FIRST monitor
    (the primary on a normal enumeration). The single-monitor case
    degenerates to "the only monitor".
    """
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    rect = (-1000, -1000, -500, -500)
    target = _resolve_target_monitor(rect, monitors=single_monitor)
    assert target is single_monitor[0]


# ---------------------------------------------------------------------------
# _resolve_target_monitor: dual-monitor mixed-DPI layout.
# ---------------------------------------------------------------------------


def test_resolver_picks_monitor_a_for_rect_on_a(dual_mixed_dpi_monitors):
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    rect = (100, 100, 600, 400)  # physical px on monitor A
    target = _resolve_target_monitor(rect, monitors=dual_mixed_dpi_monitors)
    assert target is dual_mixed_dpi_monitors[0]
    assert target.dpr == 1.0


def test_resolver_picks_monitor_b_for_rect_on_b(dual_mixed_dpi_monitors):
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    # Monitor B native extent is x=1920..5760. A rect at x=2500 is on B.
    rect = (2500, 200, 3500, 600)  # physical px on monitor B
    target = _resolve_target_monitor(rect, monitors=dual_mixed_dpi_monitors)
    assert target is dual_mixed_dpi_monitors[1]
    assert target.dpr == 2.0


def test_resolver_picks_max_overlap_monitor_on_boundary(dual_mixed_dpi_monitors):
    """Rect straddles the A/B boundary at x=1920 but most lives on A."""
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    rect = (1700, 500, 1980, 600)  # 220 px on A, 60 px on B
    target = _resolve_target_monitor(rect, monitors=dual_mixed_dpi_monitors)
    assert target is dual_mixed_dpi_monitors[0]


def test_resolver_falls_back_to_primary_for_off_screen_rect(dual_mixed_dpi_monitors):
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    rect = (-1000, -1000, -500, -500)  # nowhere
    target = _resolve_target_monitor(rect, monitors=dual_mixed_dpi_monitors)
    # No overlap with any monitor; production behaviour returns the
    # first monitor (the primary on a normal enumeration). The test
    # asserts this contract; if the fallback policy changes later
    # this assertion is the trip wire.
    assert target is dual_mixed_dpi_monitors[0]


def test_resolver_equal_overlap_returns_first_monitor():
    """A rect with equal positive overlap on two side-by-side monitors
    of the same physical extent. The strict greater-than comparison in
    ``_resolve_target_monitor`` resolves the tie to the monitor that
    appears earlier in the list. The docstring documents this contract;
    this test is the trip wire if someone changes ``>`` to ``>=``.
    """
    from services.wheelhouse.shared.monitor_geometry import (
        _NativeMonitor,
        _resolve_target_monitor,
    )

    # Two same-size monitors side by side at x=0..1920 and x=1920..3840.
    monitors = [
        _NativeMonitor(
            hmonitor=1001,
            rect_phys=QRect(0, 0, 1920, 1080),
            dpi=96,
        ),
        _NativeMonitor(
            hmonitor=1002,
            rect_phys=QRect(1920, 0, 1920, 1080),
            dpi=96,
        ),
    ]
    # Rect straddles the boundary at x=1920 by exactly 500 px each side.
    # Overlap with monitor A: x=1420..1920 = 500 wide, 1080 tall.
    # Overlap with monitor B: x=1920..2420 = 500 wide, 1080 tall.
    # Equal positive overlap -> first monitor wins.
    rect = (1420, 0, 2420, 1080)
    target = _resolve_target_monitor(rect, monitors=monitors)
    assert target is monitors[0]
    # Sanity check: reversing the input list flips the winner, proving
    # the tie-break is "first in the list" not "first by HMONITOR" or
    # some other implicit ordering.
    target_reversed = _resolve_target_monitor(rect, monitors=list(reversed(monitors)))
    assert target_reversed is monitors[1]


# ---------------------------------------------------------------------------
# _resolve_target_monitor: three-monitor mixed-DPI layout.
# ---------------------------------------------------------------------------


def test_resolver_three_monitor_picks_middle_for_rect_on_middle(three_monitor_mixed_dpi):
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    # Monitor B (middle) extent: x=1920..5760.
    rect = (2500, 100, 4500, 900)
    target = _resolve_target_monitor(rect, monitors=three_monitor_mixed_dpi)
    assert target is three_monitor_mixed_dpi[1]
    assert target.dpi == 144
    assert abs(target.dpr - 1.5) < 1e-9


def test_resolver_three_monitor_picks_right_for_rect_on_right(three_monitor_mixed_dpi):
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    # Monitor C (right) extent: x=5760..9600.
    rect = (6500, 200, 8000, 1500)
    target = _resolve_target_monitor(rect, monitors=three_monitor_mixed_dpi)
    assert target is three_monitor_mixed_dpi[2]
    assert target.dpr == 2.0


def test_resolver_three_monitor_picks_left_for_rect_on_left(three_monitor_mixed_dpi):
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    rect = (50, 50, 500, 500)  # well inside A
    target = _resolve_target_monitor(rect, monitors=three_monitor_mixed_dpi)
    assert target is three_monitor_mixed_dpi[0]
    assert target.dpr == 1.0


def test_resolver_three_monitor_picks_max_overlap_across_b_and_c(three_monitor_mixed_dpi):
    """Rect spans monitor B and monitor C; most of it lives on C."""
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    # Monitor B extent x=1920..5760, monitor C extent x=5760..9600.
    # Rect 5680..6680: 80 px on B, 920 px on C -> C wins.
    rect = (5680, 100, 6680, 200)
    target = _resolve_target_monitor(rect, monitors=three_monitor_mixed_dpi)
    assert target is three_monitor_mixed_dpi[2]


# ---------------------------------------------------------------------------
# _resolve_target_monitor: degenerate / boundary cases.
# ---------------------------------------------------------------------------


def test_resolver_returns_none_for_empty_monitor_list():
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    target = _resolve_target_monitor((0, 0, 100, 100), monitors=[])
    assert target is None


def test_resolver_zero_width_rect_returns_first_monitor(dual_mixed_dpi_monitors):
    """A degenerate rect (zero width or height) has zero pixel-count
    overlap with every monitor. The fallback path returns the first
    monitor.
    """
    from services.wheelhouse.shared.monitor_geometry import _resolve_target_monitor

    rect = (500, 500, 500, 600)  # zero width
    target = _resolve_target_monitor(rect, monitors=dual_mixed_dpi_monitors)
    assert target is dual_mixed_dpi_monitors[0]


# ---------------------------------------------------------------------------
# _NativeMonitor properties.
# ---------------------------------------------------------------------------


def test_native_monitor_dpr_96_dpi_is_one():
    from services.wheelhouse.shared.monitor_geometry import _NativeMonitor

    m = _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 100, 100), dpi=96)
    assert m.dpr == 1.0


def test_native_monitor_dpr_144_dpi_is_one_and_a_half():
    from services.wheelhouse.shared.monitor_geometry import _NativeMonitor

    m = _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 100, 100), dpi=144)
    assert abs(m.dpr - 1.5) < 1e-9


def test_native_monitor_dpr_192_dpi_is_two():
    from services.wheelhouse.shared.monitor_geometry import _NativeMonitor

    m = _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 100, 100), dpi=192)
    assert m.dpr == 2.0


def test_native_monitor_is_frozen():
    """``_NativeMonitor`` is a frozen dataclass: attempting to mutate
    an instance must raise ``dataclasses.FrozenInstanceError`` (a
    subclass of ``AttributeError``).
    """
    from services.wheelhouse.shared.monitor_geometry import _NativeMonitor

    m = _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 100, 100), dpi=96)
    with pytest.raises(AttributeError):
        m.dpi = 144  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _match_qscreen_for_monitor: HMONITOR identity path (primary).
# ---------------------------------------------------------------------------


def test_match_qscreen_uses_hmonitor_identity_when_available(dual_mixed_dpi_monitors):
    """When ``QScreen.handle()`` returns the HMONITOR, the identity
    match wins regardless of dimensions or DPR similarity.
    """
    from services.wheelhouse.shared.monitor_geometry import _match_qscreen_for_monitor

    mon_a, mon_b = dual_mixed_dpi_monitors
    # Build two QScreen stubs with HMONITOR identity.
    screen_a = _StubQScreen(
        _logical_geometry=_logical_size_for_native(mon_a.rect_phys, mon_a.dpr),
        _device_pixel_ratio=mon_a.dpr,
        _handle=mon_a.hmonitor,
    )
    screen_b = _StubQScreen(
        _logical_geometry=_logical_size_for_native(mon_b.rect_phys, mon_b.dpr),
        _device_pixel_ratio=mon_b.dpr,
        _handle=mon_b.hmonitor,
    )
    # Bridge monitor B -- must pick screen_b by HMONITOR identity even
    # though the iteration order would otherwise reach screen_a first.
    result = _match_qscreen_for_monitor(mon_b, screens=[screen_a, screen_b])
    assert result is screen_b


def test_match_qscreen_identity_path_short_circuits_dimensions_check():
    """If two screens share dimensions+DPR but only one has the matching
    HMONITOR, the identity path picks the right one without falling
    through to the dimensions fallback.
    """
    from services.wheelhouse.shared.monitor_geometry import (
        _NativeMonitor,
        _match_qscreen_for_monitor,
    )

    mon = _NativeMonitor(hmonitor=42, rect_phys=QRect(0, 0, 1920, 1080), dpi=96)
    decoy = _StubQScreen(
        _logical_geometry=QRect(0, 0, 1920, 1080),
        _device_pixel_ratio=1.0,
        _handle=99,
    )
    winner = _StubQScreen(
        _logical_geometry=QRect(0, 0, 1920, 1080),
        _device_pixel_ratio=1.0,
        _handle=42,
    )
    result = _match_qscreen_for_monitor(mon, screens=[decoy, winner])
    assert result is winner


def test_match_qscreen_handle_raise_falls_through_to_dimensions(
    dual_mixed_dpi_monitors,
):
    """If ``QScreen.handle()`` raises (older PySide6 / platform plugin
    quirk), the identity path is skipped and the dimensions fallback
    fires. The dimensions fallback alone still resolves correctly
    when DPR + dimensions are unique.
    """
    from services.wheelhouse.shared.monitor_geometry import _match_qscreen_for_monitor

    mon_a, mon_b = dual_mixed_dpi_monitors
    screen_a = _StubQScreen(
        _logical_geometry=_logical_size_for_native(mon_a.rect_phys, mon_a.dpr),
        _device_pixel_ratio=mon_a.dpr,
        _raise_on_handle=True,
    )
    screen_b = _StubQScreen(
        _logical_geometry=_logical_size_for_native(mon_b.rect_phys, mon_b.dpr),
        _device_pixel_ratio=mon_b.dpr,
        _raise_on_handle=True,
    )
    result = _match_qscreen_for_monitor(mon_b, screens=[screen_a, screen_b])
    assert result is screen_b


# ---------------------------------------------------------------------------
# _match_qscreen_for_monitor: dimensions fallback.
# ---------------------------------------------------------------------------


def test_match_qscreen_uses_dimensions_fallback_when_handle_returns_none(
    dual_mixed_dpi_monitors,
):
    """``QScreen.handle()`` returning ``None`` is the historical
    PySide6 default. The fallback then picks by DPR + physical
    dimensions.
    """
    from services.wheelhouse.shared.monitor_geometry import _match_qscreen_for_monitor

    mon_a, mon_b = dual_mixed_dpi_monitors
    screen_a = _StubQScreen(
        _logical_geometry=_logical_size_for_native(mon_a.rect_phys, mon_a.dpr),
        _device_pixel_ratio=mon_a.dpr,
        _handle=None,
    )
    screen_b = _StubQScreen(
        _logical_geometry=_logical_size_for_native(mon_b.rect_phys, mon_b.dpr),
        _device_pixel_ratio=mon_b.dpr,
        _handle=None,
    )
    result = _match_qscreen_for_monitor(mon_a, screens=[screen_a, screen_b])
    assert result is screen_a
    result = _match_qscreen_for_monitor(mon_b, screens=[screen_a, screen_b])
    assert result is screen_b


def test_match_qscreen_three_monitor_dimensions_fallback(three_monitor_mixed_dpi):
    """All three monitors have distinct DPR + dimensions in the
    fixture, so the dimensions fallback resolves each one unambiguously.
    """
    from services.wheelhouse.shared.monitor_geometry import _match_qscreen_for_monitor

    mon_a, mon_b, mon_c = three_monitor_mixed_dpi
    screens = [
        _StubQScreen(
            _logical_geometry=_logical_size_for_native(m.rect_phys, m.dpr),
            _device_pixel_ratio=m.dpr,
            _handle=None,
        )
        for m in (mon_a, mon_b, mon_c)
    ]
    assert _match_qscreen_for_monitor(mon_a, screens=screens) is screens[0]
    assert _match_qscreen_for_monitor(mon_b, screens=screens) is screens[1]
    assert _match_qscreen_for_monitor(mon_c, screens=screens) is screens[2]


def test_match_qscreen_dimensions_fallback_filters_on_dpr_first(
    three_monitor_mixed_dpi,
):
    """Two screens may share width+height but differ on DPR. The
    fallback filters by DPR first, so a near-but-not-equal DPR is
    rejected.
    """
    from services.wheelhouse.shared.monitor_geometry import (
        _NativeMonitor,
        _match_qscreen_for_monitor,
    )

    # Synthesise a target monitor at 2x DPR (192 DPI), 1080p physical.
    target = _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=192)
    decoy_with_wrong_dpr = _StubQScreen(
        _logical_geometry=QRect(0, 0, 1920, 1080),
        _device_pixel_ratio=1.0,  # wrong DPR; dimensions only match
        _handle=None,
    )
    winner = _StubQScreen(
        # 1920x1080 PHYSICAL at 2x DPR -> 960x540 LOGICAL.
        _logical_geometry=QRect(0, 0, 960, 540),
        _device_pixel_ratio=2.0,
        _handle=None,
    )
    result = _match_qscreen_for_monitor(
        target, screens=[decoy_with_wrong_dpr, winner],
    )
    assert result is winner


def test_match_qscreen_identical_displays_return_first_candidate():
    """Round 1 / deepseek finding 8.4: two monitors with identical DPR
    AND identical resolution. The dimensions fallback cannot
    disambiguate; the function returns the first candidate in
    ``QGuiApplication.screens()`` order. The DEBUG log line documents
    the ambiguity for field-report triage; we don't assert on log text
    here.
    """
    from services.wheelhouse.shared.monitor_geometry import (
        _NativeMonitor,
        _match_qscreen_for_monitor,
    )

    target = _NativeMonitor(hmonitor=42, rect_phys=QRect(0, 0, 1920, 1080), dpi=96)
    twin_a = _StubQScreen(
        _logical_geometry=QRect(0, 0, 1920, 1080),
        _device_pixel_ratio=1.0,
        _handle=None,
    )
    twin_b = _StubQScreen(
        _logical_geometry=QRect(0, 0, 1920, 1080),
        _device_pixel_ratio=1.0,
        _handle=None,
    )
    result = _match_qscreen_for_monitor(target, screens=[twin_a, twin_b])
    assert result is twin_a


def test_match_qscreen_returns_none_when_no_screen_matches():
    """No DPR match, no dimensions match, no HMONITOR identity. The
    bridge cannot resolve; caller falls back to the primary screen.
    """
    from services.wheelhouse.shared.monitor_geometry import (
        _NativeMonitor,
        _match_qscreen_for_monitor,
    )

    target = _NativeMonitor(hmonitor=99, rect_phys=QRect(0, 0, 1920, 1080), dpi=192)
    # Completely different DPR AND different dimensions.
    wrong = _StubQScreen(
        _logical_geometry=QRect(0, 0, 800, 600),
        _device_pixel_ratio=1.0,
        _handle=None,
    )
    result = _match_qscreen_for_monitor(target, screens=[wrong])
    assert result is None


def test_match_qscreen_returns_none_for_empty_screen_list():
    from services.wheelhouse.shared.monitor_geometry import (
        _NativeMonitor,
        _match_qscreen_for_monitor,
    )

    target = _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 100, 100), dpi=96)
    assert _match_qscreen_for_monitor(target, screens=[]) is None


def test_match_qscreen_dpr_tolerance_uses_epsilon():
    """Qt's ``devicePixelRatio()`` returns a float; the fallback uses a
    small epsilon (0.01) to tolerate IEEE-754 round-trip noise. A DPR
    that is within epsilon of the target counts as a match.
    """
    from services.wheelhouse.shared.monitor_geometry import (
        _NativeMonitor,
        _match_qscreen_for_monitor,
    )

    target = _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 2880, 1620), dpi=144)
    # Qt-reported DPR may be 1.4999999999 instead of 1.5 on some
    # platforms; the match must still succeed.
    screen = _StubQScreen(
        _logical_geometry=QRect(0, 0, 1920, 1080),
        _device_pixel_ratio=1.4999999999,
        _handle=None,
    )
    result = _match_qscreen_for_monitor(target, screens=[screen])
    assert result is screen


# ---------------------------------------------------------------------------
# End-to-end: resolver + bridge composed against synthetic layouts.
# ---------------------------------------------------------------------------


def test_resolver_and_bridge_pick_correct_qscreen_for_dual_layout(
    dual_mixed_dpi_monitors,
):
    """End-to-end: a UIA rect on monitor B is resolved to ``mon_b``
    and bridged to ``screen_b``.
    """
    from services.wheelhouse.shared.monitor_geometry import (
        _match_qscreen_for_monitor,
        _resolve_target_monitor,
    )

    mon_a, mon_b = dual_mixed_dpi_monitors
    screen_a = _StubQScreen(
        _logical_geometry=_logical_size_for_native(mon_a.rect_phys, mon_a.dpr),
        _device_pixel_ratio=mon_a.dpr,
        _handle=mon_a.hmonitor,
    )
    screen_b = _StubQScreen(
        _logical_geometry=_logical_size_for_native(mon_b.rect_phys, mon_b.dpr),
        _device_pixel_ratio=mon_b.dpr,
        _handle=mon_b.hmonitor,
    )

    rect = (2500, 200, 3500, 600)
    target = _resolve_target_monitor(rect, monitors=[mon_a, mon_b])
    assert target is mon_b
    qs = _match_qscreen_for_monitor(target, screens=[screen_a, screen_b])
    assert qs is screen_b


def test_resolver_and_bridge_pick_correct_qscreen_for_three_monitor_layout(
    three_monitor_mixed_dpi,
):
    """End-to-end: a UIA rect on monitor C (rightmost, 4K @ 2x) is
    resolved correctly and bridged to ``screen_c`` across all three
    screens.
    """
    from services.wheelhouse.shared.monitor_geometry import (
        _match_qscreen_for_monitor,
        _resolve_target_monitor,
    )

    mon_a, mon_b, mon_c = three_monitor_mixed_dpi
    screens = [
        _StubQScreen(
            _logical_geometry=_logical_size_for_native(m.rect_phys, m.dpr),
            _device_pixel_ratio=m.dpr,
            _handle=m.hmonitor,
        )
        for m in (mon_a, mon_b, mon_c)
    ]
    # Monitor C extent x=5760..9600.
    rect = (6500, 200, 8000, 1500)
    target = _resolve_target_monitor(rect, monitors=[mon_a, mon_b, mon_c])
    assert target is mon_c
    qs = _match_qscreen_for_monitor(target, screens=screens)
    assert qs is screens[2]


# ---------------------------------------------------------------------------
# _enumerate_native_monitors: production path is exercised indirectly via
# the dependency-injection seam (the synthetic layouts above feed the
# resolver directly). The Win32 enumeration itself is covered by the
# manual operator drill in Section 4 (David's real multi-monitor desktop).
# This test asserts only that the function is callable on the host
# platform and returns a list; on Windows it produces at least one
# monitor entry, on non-Windows hosts it raises or returns an empty
# list (the production caller handles a None ``_resolve_target_monitor``
# return by falling back to the primary screen).
# ---------------------------------------------------------------------------


def test_enumerate_native_monitors_returns_list_on_windows():
    """Smoke test for the production enumeration path.

    Skipped on non-Windows; on Windows it must return a list (possibly
    empty if EnumDisplayMonitors fails, but the function must not
    raise).
    """
    import sys

    if sys.platform != "win32":
        pytest.skip("EnumDisplayMonitors is Windows-only")
    from services.wheelhouse.shared.monitor_geometry import _enumerate_native_monitors

    monitors = _enumerate_native_monitors()
    assert isinstance(monitors, list)
    # On a real Windows host with a working display, we expect at
    # least one monitor. CI runners without a display may return [].
    for m in monitors:
        assert isinstance(m.hmonitor, int)
        assert isinstance(m.dpi, int)
        assert m.dpi >= 96  # smallest plausible value
        assert m.rect_phys.width() > 0
        assert m.rect_phys.height() > 0
