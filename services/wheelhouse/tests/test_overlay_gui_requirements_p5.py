"""Boundary tests for area P5 of OverlayPaintWindowManager: the mouse grid
drawing (wh-full-refactor.7.1, stage A of place 4).

Each test names its row GP-n of the P5 notes. A test enters only through the
public methods that GuiManager calls for the grid (paint_grid,
paint_grid_pin, clear_grid), with the events GuiManager parses
(PaintGridEvent, PaintGridPinEvent), and reads only the return values and the
calls on the fake operating system seam of
tests/overlay_paint_seam_harness.py: the windows (geometry, style), the
DestroyWindow calls and the composited DIB pixels.

Depth of the pixel checks (decision in the P5 notes): a test reads pixels only
where a rule is visible to the user. The grid lines must lie on the cell
boundaries of grid_overlay_state.cell_rects, the nine labels must sit on the
cell centers in telephone-keypad order, and the pin must mark its point.
Colors, stroke widths, the label size and the pin size are not rules; the
tests read ink (alpha), not color, and they measure the label size from the
drawing instead of from the constants.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen

from services.wheelhouse.grid_overlay_state import GridRect, cell_center, cell_rects
from shared.paint_grid import PaintGridEvent
from shared.paint_grid_pin import PaintGridPinEvent
from tests.overlay_paint_seam_harness import (
    PRIMARY,
    SECOND,
    FakeMonitors,
    Monitor,
    paint_seam,
)

# WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE |
# WS_EX_TOOLWINDOW (winuser.h): layered, always on top, click-through, never
# activated, no taskbar button.
CLICK_THROUGH_EX_STYLE = 0x00080000 | 0x00000008 | 0x00000020 | 0x08000000 | 0x00000080
SW_SHOWNOACTIVATE = 4

# A refined rectangle on the primary monitor whose sides do not divide by
# three (1000 and 601), so the cell edges are origin + (size * i) // 3.
REFINED = (101, 50, 1000, 601)
# A pin point inside REFINED, clear of its lines and of its labels.
PIN_IN_GRID = (150, 90)


def grid(monitor: Monitor, left: int, top: int, width: int, height: int):
    return PaintGridEvent(
        monitor_left=monitor.left, monitor_top=monitor.top,
        monitor_width=monitor.width, monitor_height=monitor.height,
        left=left, top=top, width=width, height=height,
    )


def full(monitor: Monitor):
    return grid(monitor, monitor.left, monitor.top, monitor.width, monitor.height)


def pin(x: int, y: int):
    return PaintGridPinEvent(x=x, y=y)


@pytest.fixture
def seam(qapp):
    with paint_seam(FakeMonitors(monitors=[PRIMARY, SECOND])) as fake:
        yield fake


def only_window(seam):
    live = seam.live_windows()
    assert len(live) == 1, live
    return live[0]


# --- GP-1: the grid window -------------------------------------------------


@pytest.mark.parametrize(
    "monitor, rect, geometry",
    [
        (PRIMARY, (0, 0, 1920, 1080), (0, 0, 1920, 1080)),
        (PRIMARY, REFINED, (97, 46, 1008, 609)),
        (SECOND, (2000, 100, 900, 600), (1994, 94, 912, 612)),
        (SECOND, (1920, 0, 2560, 1440), (1920, 0, 2560, 1440)),
    ],
    ids=["primary-full", "primary-refined", "scaled-refined", "scaled-full"],
)
def test_gp_1_grid_gets_one_window_over_its_rectangle(seam, monitor, rect, geometry):
    manager = seam.manager()

    assert manager.paint_grid(grid(monitor, *rect)) is True

    window = only_window(seam)
    assert window.geometry == geometry
    (record,) = seam.composites
    assert (record.hwnd, record.dest_x, record.dest_y) == (
        window.hwnd, geometry[0], geometry[1],
    )
    assert (record.width, record.height) == (geometry[2], geometry[3])


def test_gp_2_grid_window_is_click_through_and_never_activated(seam):
    manager = seam.manager()

    manager.paint_grid(full(PRIMARY))

    window = only_window(seam)
    assert window.ex_style & CLICK_THROUGH_EX_STYLE == CLICK_THROUGH_EX_STYLE
    assert window.shown == [SW_SHOWNOACTIVATE]


# --- GP-3: the lines lie on the cell boundaries ----------------------------


def boundaries(rect):
    cells = cell_rects(GridRect(*rect))
    xs = (cells[0].left, cells[1].left, cells[2].left, cells[2].left + cells[2].width)
    ys = (cells[0].top, cells[3].top, cells[6].top, cells[6].top + cells[6].height)
    return xs, ys


def assert_line_centered(alpha_before, alpha_on_a, alpha_on_b, alpha_after):
    """The two pixels on the boundary are fully inked and the two pixels at
    the edges of the stroke are inked equally, so the stroke is centered on
    the boundary (a stroke moved by a fraction of a pixel inks one edge pixel
    more than the other)."""
    assert alpha_on_a == 255 and alpha_on_b == 255
    assert 32 <= alpha_before <= 223
    assert abs(alpha_before - alpha_after) <= 16, (alpha_before, alpha_after)


def test_gp_3_lines_lie_on_the_cell_boundaries(seam):
    manager = seam.manager()
    manager.paint_grid(grid(PRIMARY, *REFINED))
    record = seam.composites[-1]
    xs, ys = boundaries(REFINED)
    assert xs == (101, 434, 767, 1101) and ys == (50, 250, 450, 651)

    for x in xs:
        for row_top in ys[:3]:
            y = row_top + 30
            assert_line_centered(
                record.alpha(x - 2, y), record.alpha(x - 1, y),
                record.alpha(x, y), record.alpha(x + 1, y),
            )
    for y in ys:
        for column_left in xs[:3]:
            x = column_left + 30
            assert_line_centered(
                record.alpha(x, y - 2), record.alpha(x, y - 1),
                record.alpha(x, y), record.alpha(x, y + 1),
            )
    # Between the lines (away from the labels) nothing is drawn.
    for x in xs[:3]:
        for y in ys[:3]:
            assert record.alpha(x + 20, y + 20) == 0


# --- GP-4: nine labels on the cell centers, in keypad order ----------------


def ink_mask(points):
    """The ink points moved so their bounding box starts at (0, 0)."""
    min_x = min(x for x, _ in points)
    min_y = min(y for _, y in points)
    return frozenset((x - min_x, y - min_y) for x, y in points)


def reference_digit(digit: int, pixel_size: int):
    """The ink of one digit drawn by the test with Qt alone: a bold glyph
    filled and outlined, as a label is drawn. Used only to tell the nine
    digits apart, so its size and outline need not match exactly."""
    side = pixel_size * 3
    image = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    font = QFont()
    font.setPixelSize(pixel_size)
    font.setBold(True)
    path = QPainterPath()
    path.addText(pixel_size, 2 * pixel_size, font, str(digit))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(0, 0, 0))
    pen.setWidthF(max(1.0, pixel_size * 0.07))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QColor(255, 255, 255))
    painter.drawPath(path)
    painter.end()
    return ink_mask({
        (x, y) for y in range(side) for x in range(side)
        if image.pixelColor(x, y).alpha() >= 128
    })


def overlap(a, b) -> float:
    return len(a & b) / len(a | b)


def label_ink(record, cell, reach: int):
    center = cell_center(cell)
    return record.ink(
        center.x - reach, center.y - reach, center.x + reach, center.y + reach,
    )


def test_gp_4_nine_labels_sit_on_the_cell_centers_in_keypad_order(seam):
    manager = seam.manager()
    manager.paint_grid(full(PRIMARY))
    record = seam.composites[-1]
    cells = cell_rects(GridRect(0, 0, 1920, 1080))

    labels = {}
    for number in range(1, 10):
        points = label_ink(record, cells[number - 1], reach=90)
        assert len(points) > 100, number
        center = cell_center(cells[number - 1])
        xs = [x for x, _ in points]
        ys = [y for _, y in points]
        # The label's ink is centered on the cell center (within the glyph's
        # own asymmetry).
        assert abs((min(xs) + max(xs)) / 2 - center.x) <= 12, number
        assert abs((min(ys) + max(ys)) / 2 - center.y) <= 12, number
        labels[number] = ink_mask(points)
        # The corners of the cell are empty: one label, at the center.
        cell = cells[number - 1]
        assert record.ink(cell.left + 20, cell.top + 20,
                          cell.left + 120, cell.top + 120) == set(), number

    height = max(y for _, y in labels[8]) + 1
    probe = reference_digit(8, 100)
    pixel_size = round(100 * height / (max(y for _, y in probe) + 1))
    references = {d: reference_digit(d, pixel_size) for d in range(1, 10)}
    for number, mask in labels.items():
        best = max(references, key=lambda d: overlap(mask, references[d]))
        assert best == number, (number, best)


def label_points(record, rect, reach: int = 90):
    """The ink of each label 1 to 9 of a grid on ``rect``, with the integer
    cell center of its cell."""
    cells = cell_rects(GridRect(*rect))
    result = {}
    for number in range(1, 10):
        points = label_ink(record, cells[number - 1], reach)
        assert len(points) > 100, number
        result[number] = (points, cell_center(cells[number - 1]))
    return result


@pytest.mark.parametrize(
    "rect",
    [(0, 0, 1366, 770), (203, 77, 1366, 770)],
    ids=["1366x770-at-origin", "1366x770-offset"],
)
def test_gp_4b_labels_sit_on_the_integer_cell_centers_when_the_size_does_not_divide_by_3(
    seam, rect
):
    # Reference: on 1920x1080 both rules (integer cell edges and equal float
    # thirds) give the same center, so it shows where the ink of each label
    # lies relative to its center. Every grid here has 72 px labels.
    manager = seam.manager()
    manager.paint_grid(full(PRIMARY))
    reference = {
        number: (
            ink_mask(points),
            (min(x for x, _ in points) - center.x,
             min(y for _, y in points) - center.y),
        )
        for number, (points, center)
        in label_points(seam.composites[-1], (0, 0, 1920, 1080)).items()
    }
    assert rect[2] % 3 != 0 and rect[3] % 3 != 0

    manager.paint_grid(grid(PRIMARY, *rect))
    for number, (points, center) in label_points(seam.composites[-1], rect).items():
        mask, offset = reference[number]
        # An integer center shift keeps the shape of the ink. A center on a
        # float third moves it by a fraction of a pixel or by a whole pixel.
        assert ink_mask(points) == mask, number
        assert (
            min(x for x, _ in points) - center.x,
            min(y for _, y in points) - center.y,
        ) == offset, number


def test_gp_5_label_shadow_follows_the_shadow_setting(seam):
    plain = seam.manager(badge_shadow=False)
    plain.paint_grid(full(PRIMARY))
    without = seam.composites[-1]
    plain.clear_grid()
    shadowed = seam.manager(badge_shadow=True)
    shadowed.paint_grid(full(PRIMARY))
    with_shadow = seam.composites[-1]
    middle = cell_rects(GridRect(0, 0, 1920, 1080))[4]

    plain_ink = label_ink(without, middle, reach=90)
    shadow_ink = label_ink(with_shadow, middle, reach=90)
    # The shadow adds ink and removes none. The shadow offset is smaller
    # than half the outline width at this label size, so the extra ink lies
    # inside the bounding box of the plain label; the test does not read
    # its direction or size (style, not a rule).
    assert shadow_ink > plain_ink


# --- GP-6: the pin -----------------------------------------------------------


@pytest.mark.parametrize(
    "point, geometry, arm",
    [
        ((500, 400), (483, 383, 35, 35), 10),
        ((3000, 700), (2974, 674, 53, 53), 15),
        ((5, 5), (0, 0, 23, 23), 10),
    ],
    ids=["primary", "scaled", "at-the-monitor-corner"],
)
def test_gp_6_pin_marks_its_point(seam, point, geometry, arm):
    manager = seam.manager()

    assert manager.paint_grid_pin(pin(*point)) is True

    window = only_window(seam)
    assert window.geometry == geometry
    record = seam.composites[-1]
    assert (record.dest_x, record.dest_y, record.width, record.height) == geometry
    x, y = point
    assert record.alpha(x, y) == 255
    assert record.alpha(x + arm, y) >= 200
    assert record.alpha(x, y + arm) >= 200
    assert record.alpha(x + arm, y + arm) == 0


# --- GP-7: the grid and the pin on one monitor -----------------------------


@pytest.mark.parametrize("order", ["grid-first", "pin-first"])
@pytest.mark.parametrize(
    "point, geometry",
    [
        (PIN_IN_GRID, (97, 46, 1008, 609)),
        ((1500, 900), (97, 46, 1421, 872)),
    ],
    ids=["pin-inside", "pin-outside"],
)
def test_gp_7_grid_and_pin_share_one_window_and_both_show(seam, order, point, geometry):
    manager = seam.manager()
    calls = [
        lambda: manager.paint_grid(grid(PRIMARY, *REFINED)),
        lambda: manager.paint_grid_pin(pin(*point)),
    ]
    if order == "pin-first":
        calls.reverse()

    assert [call() for call in calls] == [True, True]

    window = only_window(seam)
    assert window.geometry == geometry
    record = seam.last_composite_for(window.hwnd)
    assert record.alpha(*point) == 255
    assert record.alpha(434, 80) == 255  # a grid line


def test_gp_8_pin_on_another_monitor_gets_its_own_window(seam):
    manager = seam.manager()
    manager.paint_grid(full(PRIMARY))

    assert manager.paint_grid_pin(pin(3000, 700)) is True

    geometries = sorted(w.geometry for w in seam.live_windows())
    assert geometries == [(0, 0, 1920, 1080), (2974, 674, 53, 53)]
    pin_window = next(w for w in seam.live_windows() if w.left == 2974)
    assert seam.last_composite_for(pin_window.hwnd).alpha(3000, 700) == 255


@pytest.mark.parametrize(
    "point",
    [(-500, -500), (-3, 500)],
    # The second point is off every monitor but within the pin's own reach
    # of the primary, so a pin window on the primary would not be empty.
    ids=["far-from-every-monitor", "just-left-of-the-primary"],
)
def test_gp_9_pin_on_no_monitor_is_dropped(seam, point):
    manager = seam.manager()

    assert manager.paint_grid_pin(pin(*point)) is False
    assert seam.windows == [] and seam.composites == []

    assert manager.paint_grid(grid(PRIMARY, *REFINED)) is True
    assert only_window(seam).geometry == (97, 46, 1008, 609)


# --- GP-10: which monitor draws the grid -----------------------------------


@pytest.mark.parametrize(
    "monitors, named, rect, geometry",
    [
        ([Monitor(0, 0, 2560, 1440), Monitor(2560, 0, 2560, 1440, dpi=144)],
         PRIMARY, (100, 100, 900, 600),
         (96, 96, 908, 608)),
        ([PRIMARY, SECOND], Monitor(1900, 0, 2560, 1440), (2000, 100, 900, 600),
         (1994, 94, 912, 612)),
    ],
    ids=["resized-monitor", "most-overlap"],
)
def test_gp_10_grid_draws_on_the_monitor_its_event_names(qapp, monitors, named, rect, geometry):
    with paint_seam(FakeMonitors(monitors=monitors)) as seam:
        manager = seam.manager()

        assert manager.paint_grid(grid(named, *rect)) is True

        assert only_window(seam).geometry == geometry


@pytest.mark.parametrize(
    "gone",
    [Monitor(5000, 0, 1920, 1080), Monitor(-1920, 0, 1920, 1080)],
    # The second monitor touched the left edge of the primary: the grid's
    # ink margin would reach a few pixels onto the primary.
    ids=["far-right", "left-of-the-primary"],
)
def test_gp_11_grid_whose_monitor_is_gone_draws_nothing(seam, gone):
    manager = seam.manager()
    manager.paint_grid(full(PRIMARY))
    (old,) = [w.hwnd for w in seam.live_windows()]

    assert manager.paint_grid(full(gone)) is False

    assert seam.live_windows() == []
    assert old in seam.destroy_calls


# --- GP-12: refinement rebuilds the window; a repeat reuses it -------------


def test_gp_12_refined_grid_rebuilds_and_a_repeat_reuses_the_window(seam):
    manager = seam.manager()
    manager.paint_grid(full(PRIMARY))
    (first,) = [w.hwnd for w in seam.live_windows()]

    assert manager.paint_grid(grid(PRIMARY, 0, 0, 640, 360)) is True
    assert first in seam.destroy_calls
    second = only_window(seam)
    assert second.geometry == (0, 0, 644, 364)

    windows = len(seam.windows)
    destroys = len(seam.destroy_calls)
    composites = len(seam.composites)
    assert manager.paint_grid(grid(PRIMARY, 0, 0, 640, 360)) is True
    assert len(seam.windows) == windows
    assert seam.destroys_since(destroys) == []
    assert [r.hwnd for r in seam.composites_since(composites)] == [second.hwnd]


# --- GP-13 / GP-14: a failure leaves no grid window ---------------------------


@pytest.mark.parametrize("reused", [False, True], ids=["new-window", "reused-window"])
def test_gp_13_failed_composite_returns_false_and_removes_the_window(seam, reused):
    manager = seam.manager()
    if reused:
        manager.paint_grid(grid(PRIMARY, *REFINED))
    seam.composite_fails = True

    assert manager.paint_grid(grid(PRIMARY, *REFINED)) is False

    assert seam.live_windows() == []


def test_gp_14_failed_window_creation_returns_false_and_leaves_no_window(seam):
    manager = seam.manager()
    manager.paint_grid(full(PRIMARY))
    manager.paint_grid_pin(pin(3000, 700))
    seam.create_fails = True

    assert manager.paint_grid(grid(PRIMARY, *REFINED)) is False

    assert seam.live_windows() == []


# --- GP-15 / GP-16: clear_grid --------------------------------------------------


def test_gp_15_clear_grid_removes_the_grid_and_the_pin_and_forgets_both(seam):
    manager = seam.manager()
    manager.paint_grid(grid(PRIMARY, *REFINED))
    manager.paint_grid_pin(pin(3000, 700))
    assert len(seam.live_windows()) == 2

    assert manager.clear_grid() is None
    assert seam.live_windows() == []

    # A pin after the clear draws the pin alone: the grid is forgotten.
    assert manager.paint_grid_pin(pin(*PIN_IN_GRID)) is True
    assert only_window(seam).geometry == (133, 73, 35, 35)
    manager.clear_grid()
    # A grid after the clear draws no pin: the pin is forgotten.
    manager.paint_grid_pin(pin(*PIN_IN_GRID))
    manager.clear_grid()
    assert manager.paint_grid(grid(PRIMARY, *REFINED)) is True
    window = only_window(seam)
    assert seam.last_composite_for(window.hwnd).alpha(*PIN_IN_GRID) == 0


def test_gp_16_window_that_survives_clear_grid_is_retried_at_the_next_paint(seam):
    manager = seam.manager()
    manager.paint_grid(grid(PRIMARY, *REFINED))
    (old,) = [w.hwnd for w in seam.live_windows()]
    seam.destroy_fails = {old}

    manager.clear_grid()
    assert [w.hwnd for w in seam.live_windows()] == [old]
    seam.destroy_fails = set()
    mark = len(seam.destroy_calls)

    assert manager.paint_grid(full(PRIMARY)) is True

    assert old in seam.destroys_since(mark)
    window = only_window(seam)
    assert window.hwnd != old
