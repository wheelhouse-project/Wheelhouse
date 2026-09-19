"""Exhaustive tests for the mouse-grid state machine and its cell arithmetic.

Bead ``wh-grid-state-machine`` under the ``wh-mouse-grid`` molecule. The
authoritative spec is
``docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md``
("Logic process -- grid state machine and all geometry", the Refinement
rules, and the actions table).

Two halves, matching the module:

  * **Cell arithmetic** -- pure functions over rectangles: the 3x3 split
    (all nine cells, uneven rectangles that do not divide by three, exact
    tiling with no gaps and no overlaps), cell centers, the minimum-size
    floor including its exact boundary, primary-monitor resolution, and
    next-monitor selection with wraparound over mixed-resolution monitors
    at negative virtual-desktop coordinates.
  * **State machine** -- every transition (open, refine, mark, drag,
    dismiss, next monitor, action-completed) plus the mutual exclusion
    with the numbered overlay driven against a real
    ``ClickOverlayStateMachine``.

Effects are asserted as DATA (the returned ``GridEffect`` tuples), not via
mocks: the state machine is pure and returns its side effects as a value.
This mirrors ``tests/test_click_overlay_state.py``.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.click_overlay_state import (
    ClickOverlayStateMachine,
    OverlayEvent,
    OverlayEventKind,
    OverlayState,
    PaintAckState,
)
from services.wheelhouse.grid_overlay_state import (
    DRAG_WITHOUT_MARK,
    GRID_CELL_COUNT,
    GRID_DIVISIONS,
    GridApplyResult,
    GridEffect,
    GridEffectKind,
    GridEvent,
    GridEventKind,
    GridOutcome,
    GridOverlayStateMachine,
    GridPoint,
    GridRect,
    GridState,
    can_refine,
    cell_center,
    cell_rect,
    cell_rects,
    next_monitor,
    primary_monitor,
    resolve_open_monitor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A plain 1920x1080 primary monitor at the virtual-desktop origin.
PRIMARY = GridRect(0, 0, 1920, 1080)
# A 3840x2160 monitor to the LEFT of and ABOVE the primary -- negative
# virtual-desktop coordinates are normal on a real multi-monitor desktop.
LEFT_4K = GridRect(-3840, -300, 3840, 2160)
# A 1366x768 laptop panel below the primary.
LAPTOP = GridRect(200, 1080, 1366, 768)


def effect_kinds(result: GridApplyResult) -> list[GridEffectKind]:
    return [e.kind for e in result.effects]


def covered_points(rect: GridRect) -> set[tuple[int, int]]:
    """Every integer pixel coordinate inside ``rect`` (half-open edges)."""

    return {
        (x, y)
        for x in range(rect.left, rect.left + rect.width)
        for y in range(rect.top, rect.top + rect.height)
    }


def open_grid(
    machine: GridOverlayStateMachine,
    monitor: GridRect = PRIMARY,
    monitors: tuple[GridRect, ...] = (PRIMARY,),
) -> GridApplyResult:
    return machine.apply(
        GridEvent(
            GridEventKind.OPEN_GRID,
            focused_monitor=monitor,
            monitors=monitors,
        )
    )


# ===========================================================================
# Cell arithmetic -- pure functions
# ===========================================================================


def test_divisions_and_cell_count_are_the_documented_three_by_three():
    assert GRID_DIVISIONS == 3
    assert GRID_CELL_COUNT == 9


def test_nine_cells_of_an_evenly_divisible_rectangle():
    """A 300x300 rectangle splits into nine exact 100x100 cells, in reading order."""

    parent = GridRect(0, 0, 300, 300)
    expected = (
        GridRect(0, 0, 100, 100),
        GridRect(100, 0, 100, 100),
        GridRect(200, 0, 100, 100),
        GridRect(0, 100, 100, 100),
        GridRect(100, 100, 100, 100),
        GridRect(200, 100, 100, 100),
        GridRect(0, 200, 100, 100),
        GridRect(100, 200, 100, 100),
        GridRect(200, 200, 100, 100),
    )
    assert cell_rects(parent) == expected
    for number in range(1, 10):
        assert cell_rect(parent, number) == expected[number - 1]


def test_cell_numbering_is_left_to_right_then_top_to_bottom():
    """1 is top-left, 5 the middle, 9 bottom-right (telephone-keypad order)."""

    parent = GridRect(0, 0, 300, 300)
    assert cell_rect(parent, 1) == GridRect(0, 0, 100, 100)
    assert cell_rect(parent, 5) == GridRect(100, 100, 100, 100)
    assert cell_rect(parent, 9) == GridRect(200, 200, 100, 100)


@pytest.mark.parametrize("number", [0, -1, 10, 11])
def test_cell_rect_rejects_a_number_outside_one_to_nine(number):
    with pytest.raises(ValueError):
        cell_rect(GridRect(0, 0, 300, 300), number)


def test_uneven_width_puts_the_remainder_in_the_trailing_columns():
    """Documented rounding: edges are floor(width * i / 3), so extra pixels
    land in the LATER columns and the three widths still sum to the parent."""

    parent = GridRect(0, 0, 10, 9)
    widths = [cell_rect(parent, n).width for n in (1, 2, 3)]
    assert widths == [3, 3, 4]
    assert sum(widths) == parent.width


def test_uneven_height_puts_the_remainder_in_the_trailing_rows():
    parent = GridRect(0, 0, 9, 11)
    heights = [cell_rect(parent, n).height for n in (1, 4, 7)]
    assert heights == [3, 4, 4]
    assert sum(heights) == parent.height


@pytest.mark.parametrize(
    "parent",
    [
        GridRect(0, 0, 1, 1),
        GridRect(0, 0, 2, 5),
        GridRect(0, 0, 10, 11),
        GridRect(0, 0, 1920, 1080),
        GridRect(-3840, -300, 3840, 2160),
        GridRect(-7, -13, 17, 23),
        GridRect(200, 1080, 1366, 768),
    ],
)
def test_the_nine_cells_exactly_tile_the_parent(parent):
    """No gaps, no overlaps: the nine cells partition the parent exactly.

    Checked structurally (edges line up and areas sum) for every rectangle,
    and pixel-by-pixel for the small ones where enumeration is cheap.
    """

    cells = cell_rects(parent)
    assert len(cells) == GRID_CELL_COUNT

    # Areas sum to the parent's area -- with containment below, that alone
    # rules out both gaps and overlaps.
    assert sum(c.width * c.height for c in cells) == parent.width * parent.height

    for cell in cells:
        assert cell.left >= parent.left
        assert cell.top >= parent.top
        assert cell.right <= parent.right
        assert cell.bottom <= parent.bottom

    # Column edges are shared across the three rows, row edges across the
    # three columns: the cells line up on a true grid, not a ragged one.
    for row in range(3):
        row_cells = cells[row * 3 : row * 3 + 3]
        assert row_cells[0].left == parent.left
        assert row_cells[0].right == row_cells[1].left
        assert row_cells[1].right == row_cells[2].left
        assert row_cells[2].right == parent.right
    for col in range(3):
        col_cells = cells[col :: 3]
        assert col_cells[0].top == parent.top
        assert col_cells[0].bottom == col_cells[1].top
        assert col_cells[1].bottom == col_cells[2].top
        assert col_cells[2].bottom == parent.bottom

    if parent.width * parent.height <= 4000:
        union: set[tuple[int, int]] = set()
        for cell in cells:
            points = covered_points(cell)
            assert not (union & points), "cells overlap"
            union |= points
        assert union == covered_points(parent), "cells leave a gap"


def test_tiling_holds_for_a_deep_refinement_chain():
    """Repeated refinement of an odd-sized rectangle never drifts outside it."""

    parent = GridRect(-1237, -911, 1919, 1079)
    rect = parent
    for number in (1, 5, 9, 3, 7, 2):
        rect = cell_rect(rect, number)
        assert rect.left >= parent.left
        assert rect.top >= parent.top
        assert rect.right <= parent.right
        assert rect.bottom <= parent.bottom
        assert rect.width >= 1
        assert rect.height >= 1


def test_a_one_pixel_rectangle_still_produces_nine_cells():
    """Degenerate but well-defined: eight empty cells and one 1x1 cell."""

    cells = cell_rects(GridRect(4, 9, 1, 1))
    assert len(cells) == GRID_CELL_COUNT
    non_empty = [c for c in cells if c.width and c.height]
    assert non_empty == [GridRect(4, 9, 1, 1)]


# -- centers ---------------------------------------------------------------


def test_cell_center_of_an_even_rectangle():
    assert cell_center(GridRect(0, 0, 100, 200)) == GridPoint(50, 100)


def test_cell_center_of_an_odd_rectangle_floors():
    assert cell_center(GridRect(0, 0, 101, 201)) == GridPoint(50, 100)


def test_cell_center_with_negative_origin():
    assert cell_center(GridRect(-3840, -300, 3840, 2160)) == GridPoint(-1920, 780)


def test_cell_center_is_inside_the_rectangle():
    for rect in (PRIMARY, LEFT_4K, LAPTOP, GridRect(-7, -13, 17, 23)):
        center = cell_center(rect)
        assert rect.left <= center.x < rect.right
        assert rect.top <= center.y < rect.bottom


def test_rect_center_property_matches_cell_center():
    assert PRIMARY.center == cell_center(PRIMARY)


# -- the minimum-size floor ------------------------------------------------


def test_can_refine_is_true_well_above_the_floor():
    assert can_refine(GridRect(0, 0, 1920, 1080), 24) is True


def test_can_refine_at_exactly_the_floor_on_both_sides():
    """The boundary is inclusive: a cell of exactly the floor still refines."""

    assert can_refine(GridRect(0, 0, 24, 24), 24) is True


@pytest.mark.parametrize(
    "rect",
    [
        GridRect(0, 0, 23, 24),  # one pixel under on the width
        GridRect(0, 0, 24, 23),  # one pixel under on the height
        GridRect(0, 0, 23, 23),  # under on both
    ],
)
def test_can_refine_is_false_one_pixel_below_the_floor_on_either_side(rect):
    assert can_refine(rect, 24) is False


def test_the_floor_never_drops_below_three_pixels_whatever_is_configured():
    """A side under three pixels cannot split into three non-empty cells,
    so refinement stops there even with a configured floor of one."""

    assert can_refine(GridRect(0, 0, 3, 3), 1) is True
    assert can_refine(GridRect(0, 0, 2, 3), 1) is False
    assert can_refine(GridRect(0, 0, 3, 2), 1) is False
    assert can_refine(GridRect(0, 0, 1, 1), 1) is False
    assert can_refine(GridRect(0, 0, 0, 1), 1) is False


# -- monitor selection -----------------------------------------------------


def test_primary_monitor_is_the_one_at_the_virtual_desktop_origin():
    """Windows always places the primary monitor's rectangle at (0, 0)."""

    assert primary_monitor([LEFT_4K, PRIMARY, LAPTOP]) == PRIMARY


def test_primary_monitor_falls_back_to_the_first_when_none_sits_at_the_origin():
    assert primary_monitor([LEFT_4K, LAPTOP]) == LEFT_4K


def test_primary_monitor_of_an_empty_list_is_none():
    assert primary_monitor([]) is None


def test_next_monitor_advances_through_the_list():
    monitors = [LEFT_4K, PRIMARY, LAPTOP]
    assert next_monitor(monitors, LEFT_4K) == PRIMARY
    assert next_monitor(monitors, PRIMARY) == LAPTOP


def test_next_monitor_wraps_around_from_the_last_to_the_first():
    monitors = [LEFT_4K, PRIMARY, LAPTOP]
    assert next_monitor(monitors, LAPTOP) == LEFT_4K


def test_next_monitor_of_a_single_monitor_returns_that_monitor():
    assert next_monitor([PRIMARY], PRIMARY) == PRIMARY


def test_next_monitor_with_an_unknown_current_monitor_returns_the_first():
    """The monitor was disconnected or the topology changed under us."""

    assert next_monitor([LEFT_4K, PRIMARY], LAPTOP) == LEFT_4K


def test_next_monitor_with_no_current_monitor_returns_the_first():
    assert next_monitor([LEFT_4K, PRIMARY], None) == LEFT_4K


def test_next_monitor_of_an_empty_list_is_none():
    assert next_monitor([], PRIMARY) is None


def test_resolve_open_monitor_prefers_the_focused_windows_monitor():
    assert resolve_open_monitor([LEFT_4K, PRIMARY], LAPTOP) == LAPTOP


def test_resolve_open_monitor_falls_back_to_the_primary_when_undeterminable():
    assert resolve_open_monitor([LEFT_4K, PRIMARY], None) == PRIMARY


def test_resolve_open_monitor_rejects_a_degenerate_focused_rectangle():
    assert resolve_open_monitor([LEFT_4K, PRIMARY], GridRect(0, 0, 0, 0)) == PRIMARY


def test_resolve_open_monitor_with_nothing_to_go_on_is_none():
    assert resolve_open_monitor([], None) is None


# ===========================================================================
# State machine
# ===========================================================================


def test_a_fresh_machine_is_closed_and_empty():
    m = GridOverlayStateMachine()
    assert m.state is GridState.CLOSED
    assert m.is_open is False
    assert m.monitor is None
    assert m.rect is None
    assert m.mark is None
    assert m.current_center is None


def test_default_config_values_match_the_spec():
    m = GridOverlayStateMachine()
    assert m.grid_min_cell_px == 24
    assert m.drag_duration_ms == 250


# -- open ------------------------------------------------------------------


def test_open_starts_at_the_focused_windows_monitor_full_rectangle():
    m = GridOverlayStateMachine()
    result = open_grid(m, monitor=LAPTOP, monitors=(PRIMARY, LAPTOP))
    assert result.outcome is GridOutcome.ACCEPTED
    assert m.state is GridState.OPEN
    assert m.is_open is True
    assert m.monitor == LAPTOP
    assert m.rect == LAPTOP
    assert m.mark is None


def test_open_falls_back_to_the_primary_monitor_when_undeterminable():
    m = GridOverlayStateMachine()
    result = m.apply(
        GridEvent(
            GridEventKind.OPEN_GRID,
            focused_monitor=None,
            monitors=(LEFT_4K, PRIMARY, LAPTOP),
        )
    )
    assert result.outcome is GridOutcome.ACCEPTED
    assert m.monitor == PRIMARY
    assert m.rect == PRIMARY


def test_open_closes_the_numbered_overlay_before_painting():
    m = GridOverlayStateMachine()
    result = open_grid(m)
    assert effect_kinds(result) == [
        GridEffectKind.CLOSE_NUMBERED_OVERLAY,
        GridEffectKind.PAINT_GRID,
    ]
    paint = result.effects[1]
    assert paint.monitor == PRIMARY
    assert paint.rect == PRIMARY


def test_open_with_no_monitors_at_all_stays_closed():
    m = GridOverlayStateMachine()
    result = m.apply(GridEvent(GridEventKind.OPEN_GRID))
    assert result.outcome is GridOutcome.NO_MONITOR
    assert result.effects == ()
    assert m.state is GridState.CLOSED


def test_re_opening_while_open_restarts_at_the_full_monitor_and_keeps_the_mark():
    m = GridOverlayStateMachine()
    open_grid(m)
    m.apply(GridEvent(GridEventKind.REFINE, number=1))
    m.apply(GridEvent(GridEventKind.MARK))
    m.apply(GridEvent(GridEventKind.REFINE, number=9))
    mark = m.mark
    assert mark is not None

    result = open_grid(m)
    assert result.outcome is GridOutcome.ACCEPTED
    assert m.rect == PRIMARY
    assert m.mark == mark
    assert effect_kinds(result) == [
        GridEffectKind.CLOSE_NUMBERED_OVERLAY,
        GridEffectKind.PAINT_GRID,
        GridEffectKind.PAINT_PIN,
    ]


# -- refine ----------------------------------------------------------------


def test_refine_narrows_the_rectangle_to_the_numbered_cell():
    m = GridOverlayStateMachine()
    open_grid(m, monitor=GridRect(0, 0, 900, 900), monitors=(GridRect(0, 0, 900, 900),))
    result = m.apply(GridEvent(GridEventKind.REFINE, number=5))
    assert result.outcome is GridOutcome.ACCEPTED
    assert m.rect == GridRect(300, 300, 300, 300)
    assert effect_kinds(result) == [GridEffectKind.PAINT_GRID]
    assert result.effects[0].rect == GridRect(300, 300, 300, 300)
    assert result.effects[0].monitor == GridRect(0, 0, 900, 900)


def test_refinement_repeats_without_limit_until_the_floor():
    m = GridOverlayStateMachine(grid_min_cell_px=1)
    open_grid(m)
    seen = []
    for _ in range(20):
        result = m.apply(GridEvent(GridEventKind.REFINE, number=1))
        seen.append(result.outcome)
        if result.outcome is not GridOutcome.ACCEPTED:
            break
    assert GridOutcome.IGNORED_MIN_CELL in seen
    assert m.rect is not None
    assert m.rect.width >= 1 and m.rect.height >= 1


def test_a_number_is_ignored_silently_once_the_cell_is_below_the_floor():
    m = GridOverlayStateMachine(grid_min_cell_px=24)
    tiny = GridRect(100, 100, 23, 40)
    open_grid(m, monitor=tiny, monitors=(tiny,))
    result = m.apply(GridEvent(GridEventKind.REFINE, number=5))
    assert result.outcome is GridOutcome.IGNORED_MIN_CELL
    assert result.effects == ()
    assert m.rect == tiny
    assert m.state is GridState.OPEN


def test_a_cell_exactly_at_the_floor_still_refines():
    m = GridOverlayStateMachine(grid_min_cell_px=24)
    at_floor = GridRect(0, 0, 24, 24)
    open_grid(m, monitor=at_floor, monitors=(at_floor,))
    result = m.apply(GridEvent(GridEventKind.REFINE, number=1))
    assert result.outcome is GridOutcome.ACCEPTED
    assert m.rect == GridRect(0, 0, 8, 8)


def test_refine_while_closed_is_a_no_op():
    m = GridOverlayStateMachine()
    result = m.apply(GridEvent(GridEventKind.REFINE, number=3))
    assert result.outcome is GridOutcome.NO_OP
    assert result.effects == ()
    assert m.state is GridState.CLOSED


@pytest.mark.parametrize("number", [0, 10, -3])
def test_refine_with_a_number_outside_one_to_nine_is_a_no_op(number):
    m = GridOverlayStateMachine()
    open_grid(m)
    result = m.apply(GridEvent(GridEventKind.REFINE, number=number))
    assert result.outcome is GridOutcome.NO_OP
    assert result.effects == ()
    assert m.rect == PRIMARY


def test_current_center_tracks_the_refined_rectangle():
    m = GridOverlayStateMachine()
    open_grid(m, monitor=GridRect(0, 0, 900, 900), monitors=(GridRect(0, 0, 900, 900),))
    assert m.current_center == GridPoint(450, 450)
    m.apply(GridEvent(GridEventKind.REFINE, number=1))
    assert m.current_center == GridPoint(150, 150)


# -- mark ------------------------------------------------------------------


def test_mark_stores_the_current_cell_center_and_resets_to_the_full_monitor():
    m = GridOverlayStateMachine()
    monitor = GridRect(0, 0, 900, 900)
    open_grid(m, monitor=monitor, monitors=(monitor,))
    m.apply(GridEvent(GridEventKind.REFINE, number=1))

    result = m.apply(GridEvent(GridEventKind.MARK))
    assert result.outcome is GridOutcome.ACCEPTED
    assert m.mark == GridPoint(150, 150)
    assert m.rect == monitor
    assert m.state is GridState.OPEN
    assert effect_kinds(result) == [
        GridEffectKind.PAINT_PIN,
        GridEffectKind.PAINT_GRID,
    ]
    assert result.effects[0].point == GridPoint(150, 150)
    assert result.effects[1].rect == monitor


def test_marking_twice_replaces_the_earlier_mark():
    m = GridOverlayStateMachine()
    monitor = GridRect(0, 0, 900, 900)
    open_grid(m, monitor=monitor, monitors=(monitor,))
    m.apply(GridEvent(GridEventKind.REFINE, number=1))
    m.apply(GridEvent(GridEventKind.MARK))
    m.apply(GridEvent(GridEventKind.REFINE, number=9))
    m.apply(GridEvent(GridEventKind.MARK))
    assert m.mark == GridPoint(750, 750)


def test_mark_while_closed_is_a_no_op():
    m = GridOverlayStateMachine()
    result = m.apply(GridEvent(GridEventKind.MARK))
    assert result.outcome is GridOutcome.NO_OP
    assert result.effects == ()
    assert m.mark is None


# -- drag ------------------------------------------------------------------


def test_drag_runs_from_the_mark_to_the_current_cell_center_and_closes():
    m = GridOverlayStateMachine(drag_duration_ms=400)
    monitor = GridRect(0, 0, 900, 900)
    open_grid(m, monitor=monitor, monitors=(monitor,))
    m.apply(GridEvent(GridEventKind.REFINE, number=1))
    m.apply(GridEvent(GridEventKind.MARK))
    m.apply(GridEvent(GridEventKind.REFINE, number=9))

    result = m.apply(GridEvent(GridEventKind.DRAG_REQUESTED))
    assert result.outcome is GridOutcome.ACCEPTED
    assert effect_kinds(result) == [
        GridEffectKind.PERFORM_DRAG,
        GridEffectKind.CLEAR_GRID,
    ]
    drag = result.effects[0]
    assert drag.start_point == GridPoint(150, 150)
    assert drag.end_point == GridPoint(750, 750)
    assert drag.duration_ms == 400

    assert m.state is GridState.CLOSED
    assert m.mark is None
    assert m.rect is None
    assert m.monitor is None


def test_drag_without_a_mark_sends_no_input_and_keeps_the_grid_open():
    m = GridOverlayStateMachine()
    open_grid(m)
    result = m.apply(GridEvent(GridEventKind.DRAG_REQUESTED))
    assert result.outcome is GridOutcome.NO_MARK
    assert effect_kinds(result) == [GridEffectKind.FIRE_NOTICE]
    assert result.effects[0].notice_reason == DRAG_WITHOUT_MARK
    assert m.state is GridState.OPEN
    assert m.rect == PRIMARY


def test_drag_while_closed_is_a_no_op():
    m = GridOverlayStateMachine()
    result = m.apply(GridEvent(GridEventKind.DRAG_REQUESTED))
    assert result.outcome is GridOutcome.NO_OP
    assert result.effects == ()


def test_a_drag_across_two_monitors_keeps_both_endpoints():
    """Mark on one monitor, "grid next screen", drag: the mark survives."""

    m = GridOverlayStateMachine()
    monitors = (LEFT_4K, PRIMARY)
    open_grid(m, monitor=LEFT_4K, monitors=monitors)
    m.apply(GridEvent(GridEventKind.MARK))
    mark = m.mark
    m.apply(GridEvent(GridEventKind.NEXT_MONITOR, monitors=monitors))
    result = m.apply(GridEvent(GridEventKind.DRAG_REQUESTED))
    assert result.outcome is GridOutcome.ACCEPTED
    assert result.effects[0].start_point == mark
    assert result.effects[0].end_point == cell_center(PRIMARY)


# -- next monitor ----------------------------------------------------------


def test_next_monitor_restarts_at_the_next_monitors_full_rectangle():
    m = GridOverlayStateMachine()
    monitors = (LEFT_4K, PRIMARY, LAPTOP)
    open_grid(m, monitor=LEFT_4K, monitors=monitors)
    m.apply(GridEvent(GridEventKind.REFINE, number=5))

    result = m.apply(GridEvent(GridEventKind.NEXT_MONITOR, monitors=monitors))
    assert result.outcome is GridOutcome.ACCEPTED
    # wh-mouse-grid.1.35: "grid next screen" KEEPS the session open (the
    # help text once said otherwise). The effect-list equality already
    # excludes CLEAR_GRID; the state assertion catches a regression that
    # closes the machine without emitting a clear.
    assert m.state is GridState.OPEN
    assert m.monitor == PRIMARY
    assert m.rect == PRIMARY
    assert effect_kinds(result) == [GridEffectKind.PAINT_GRID]


def test_next_monitor_wraps_around_the_desktop():
    m = GridOverlayStateMachine()
    monitors = (LEFT_4K, PRIMARY)
    open_grid(m, monitor=PRIMARY, monitors=monitors)
    m.apply(GridEvent(GridEventKind.NEXT_MONITOR, monitors=monitors))
    assert m.monitor == LEFT_4K


def test_next_monitor_repaints_the_pin_when_a_mark_is_set():
    m = GridOverlayStateMachine()
    monitors = (LEFT_4K, PRIMARY)
    open_grid(m, monitor=LEFT_4K, monitors=monitors)
    m.apply(GridEvent(GridEventKind.MARK))
    result = m.apply(GridEvent(GridEventKind.NEXT_MONITOR, monitors=monitors))
    assert effect_kinds(result) == [
        GridEffectKind.PAINT_GRID,
        GridEffectKind.PAINT_PIN,
    ]


def test_next_monitor_with_an_empty_monitor_list_changes_nothing():
    m = GridOverlayStateMachine()
    open_grid(m)
    result = m.apply(GridEvent(GridEventKind.NEXT_MONITOR, monitors=()))
    assert result.outcome is GridOutcome.NO_MONITOR
    assert result.effects == ()
    assert m.monitor == PRIMARY


def test_next_monitor_while_closed_is_a_no_op():
    m = GridOverlayStateMachine()
    result = m.apply(
        GridEvent(GridEventKind.NEXT_MONITOR, monitors=(PRIMARY, LAPTOP))
    )
    assert result.outcome is GridOutcome.NO_OP
    assert result.effects == ()
    assert m.state is GridState.CLOSED


# -- dismiss and action-completed -----------------------------------------


def test_dismiss_clears_everything_including_the_mark():
    m = GridOverlayStateMachine()
    open_grid(m)
    m.apply(GridEvent(GridEventKind.MARK))

    result = m.apply(GridEvent(GridEventKind.DISMISS))
    assert result.outcome is GridOutcome.ACCEPTED
    assert effect_kinds(result) == [GridEffectKind.CLEAR_GRID]
    assert m.state is GridState.CLOSED
    assert m.mark is None
    assert m.rect is None
    assert m.monitor is None


def test_dismiss_while_closed_emits_a_defensive_clear():
    # wh-mouse-grid.1.3: if a paint outlived the state machine (e.g. the GUI
    # painted after Logic already gave up), a spoken "grid close" while CLOSED
    # must still send CLEAR_GRID so the stranded overlay can be recovered.
    m = GridOverlayStateMachine()
    result = m.apply(GridEvent(GridEventKind.DISMISS))
    assert result.outcome is GridOutcome.ACCEPTED
    assert effect_kinds(result) == [GridEffectKind.CLEAR_GRID]
    assert m.state is GridState.CLOSED
    assert m.mark is None


def test_action_completed_closes_the_grid_and_the_mark_dies_with_it():
    m = GridOverlayStateMachine()
    open_grid(m)
    m.apply(GridEvent(GridEventKind.MARK))

    result = m.apply(GridEvent(GridEventKind.ACTION_COMPLETED))
    assert result.outcome is GridOutcome.ACCEPTED
    assert effect_kinds(result) == [GridEffectKind.CLEAR_GRID]
    assert m.state is GridState.CLOSED
    assert m.mark is None


def test_a_mark_never_survives_into_the_next_grid_session():
    """A stale mark must not be able to cause a surprise drag later."""

    m = GridOverlayStateMachine()
    open_grid(m)
    m.apply(GridEvent(GridEventKind.MARK))
    m.apply(GridEvent(GridEventKind.ACTION_COMPLETED))

    open_grid(m)
    assert m.mark is None
    result = m.apply(GridEvent(GridEventKind.DRAG_REQUESTED))
    assert result.outcome is GridOutcome.NO_MARK


def test_action_completed_while_closed_is_a_no_op():
    m = GridOverlayStateMachine()
    result = m.apply(GridEvent(GridEventKind.ACTION_COMPLETED))
    assert result.outcome is GridOutcome.NO_OP
    assert result.effects == ()


# -- recovery --------------------------------------------------------------


def test_reset_to_closed_clears_a_live_grid():
    """The GUI process restarted; Logic drops its grid state (spec failure
    handling), exactly as the numbered overlay does."""

    m = GridOverlayStateMachine()
    open_grid(m)
    m.apply(GridEvent(GridEventKind.MARK))

    effects = m.reset_to_closed()
    assert [e.kind for e in effects] == [GridEffectKind.CLEAR_GRID]
    assert m.state is GridState.CLOSED
    assert m.mark is None
    assert m.rect is None
    assert m.monitor is None


def test_reset_to_closed_on_an_already_closed_machine_emits_nothing():
    m = GridOverlayStateMachine()
    assert m.reset_to_closed() == ()


def test_apply_never_raises_for_any_event_kind_in_either_state():
    for kind in GridEventKind:
        closed = GridOverlayStateMachine()
        assert isinstance(closed.apply(GridEvent(kind)), GridApplyResult)
        live = GridOverlayStateMachine()
        open_grid(live)
        assert isinstance(live.apply(GridEvent(kind)), GridApplyResult)


# ===========================================================================
# Mutual exclusion with the numbered overlay
# ===========================================================================


def drive_overlay_to_painted(
    overlay: ClickOverlayStateMachine, snapshot_id: str = "snap"
) -> None:
    overlay.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sess, gen = overlay.overlay_session_id, overlay.paint_generation
    overlay.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE,
            overlay_session_id=sess,
            paint_generation=gen,
            snapshot_id=snapshot_id,
        )
    )
    overlay.apply(
        OverlayEvent(
            OverlayEventKind.PAINT_ACK,
            overlay_session_id=sess,
            paint_generation=gen,
            paint_state=PaintAckState.PAINTED,
        )
    )
    assert overlay.state is OverlayState.PAINTED


def perform_close_numbered_overlay(
    result: GridApplyResult, overlay: ClickOverlayStateMachine
) -> bool:
    """Act on a CLOSE_NUMBERED_OVERLAY effect the way the integration must.

    The documented hook is ``OverlayEvent(HIDE_NUMBERS)`` on the numbered
    overlay's own state machine; this helper stands in for the integration
    layer that the later grid-routing task ships.
    """

    closed = False
    for effect in result.effects:
        if effect.kind is GridEffectKind.CLOSE_NUMBERED_OVERLAY:
            overlay.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
            closed = True
    return closed


def test_opening_the_grid_closes_a_painted_numbered_overlay():
    overlay = ClickOverlayStateMachine()
    drive_overlay_to_painted(overlay)

    grid = GridOverlayStateMachine()
    result = open_grid(grid)
    assert perform_close_numbered_overlay(result, overlay) is True
    assert overlay.state is OverlayState.CLOSED
    assert grid.state is GridState.OPEN


def test_the_close_effect_is_emitted_even_when_the_overlay_is_already_closed():
    """The hook is idempotent, so the machine does not have to know."""

    overlay = ClickOverlayStateMachine()
    assert overlay.state is OverlayState.CLOSED

    grid = GridOverlayStateMachine()
    result = open_grid(grid)
    assert perform_close_numbered_overlay(result, overlay) is True
    assert overlay.state is OverlayState.CLOSED


def test_opening_the_numbered_overlay_closes_the_grid():
    grid = GridOverlayStateMachine()
    open_grid(grid)
    grid.apply(GridEvent(GridEventKind.MARK))

    overlay = ClickOverlayStateMachine()
    overlay.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    result = grid.apply(GridEvent(GridEventKind.NUMBERED_OVERLAY_OPENED))

    assert result.outcome is GridOutcome.ACCEPTED
    assert effect_kinds(result) == [GridEffectKind.CLEAR_GRID]
    assert grid.state is GridState.CLOSED
    assert grid.mark is None
    assert overlay.state is OverlayState.WALK_IN_FLIGHT


def test_numbered_overlay_opened_while_the_grid_is_closed_is_a_no_op():
    grid = GridOverlayStateMachine()
    result = grid.apply(GridEvent(GridEventKind.NUMBERED_OVERLAY_OPENED))
    assert result.outcome is GridOutcome.NO_OP
    assert result.effects == ()


def test_the_two_overlays_are_never_open_at_the_same_time():
    """Alternate between the two overlays; at most one is ever live."""

    overlay = ClickOverlayStateMachine()
    grid = GridOverlayStateMachine()

    def both_live() -> int:
        overlay_live = overlay.state is not OverlayState.CLOSED
        return int(overlay_live) + int(grid.is_open)

    drive_overlay_to_painted(overlay)
    assert both_live() == 1

    result = open_grid(grid)
    perform_close_numbered_overlay(result, overlay)
    assert both_live() == 1

    overlay.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    grid.apply(GridEvent(GridEventKind.NUMBERED_OVERLAY_OPENED))
    assert both_live() == 1

    overlay.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert both_live() == 0


def test_a_grid_effect_carries_only_the_fields_its_kind_defines():
    """Unused GridEffect fields stay at their defaults, mirroring Effect."""

    effect = GridEffect(kind=GridEffectKind.CLEAR_GRID)
    assert effect.monitor is None
    assert effect.rect is None
    assert effect.point is None
    assert effect.start_point is None
    assert effect.end_point is None
    assert effect.duration_ms == 0
    assert effect.notice_reason == ""
