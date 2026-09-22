"""Tests for the pure geometry behind resizing the floating button by its edge.

The floating button can be resized by grabbing a ring at its outer edge. The
three decisions that make that work -- is this press on the edge, how big does
the button become as the pointer moves, and where does it go if growing pushed
it off the screen -- are plain arithmetic on numbers, so they live in their own
module and are tested here without constructing a window or faking a drag.

Coordinates are in the button's own space: (0, 0) is the top-left corner of the
square window, and the circle is inscribed in that square.

See docs/superpowers/specs/2026-07-25-floating-button-drag-resize-design.md.
"""

from __future__ import annotations

import math

import pytest

from floating_button_geometry import (
    MAX_BUTTON_SIZE,
    MIN_BUTTON_SIZE,
    MIN_VISIBLE_AREA_FRACTION,
    correct_onto_any_screen,
    correct_onto_screen,
    is_in_resize_ring,
    resize_from_pointer,
    ring_width,
)


class TestRingWidth:
    """The ring is 8 px except on small buttons, where it narrows."""

    def test_default_size_gets_the_full_eight_pixel_ring(self):
        assert ring_width(50) == 8

    def test_smallest_button_narrows_the_ring_to_a_third_of_its_radius(self):
        # radius 7.5 -> 7.5/3 = 2.5, which is less than 8 and therefore wins.
        assert ring_width(MIN_BUTTON_SIZE) == 2.5

    def test_minimum_size_is_fifteen_pixels(self):
        assert MIN_BUTTON_SIZE == 15

    def test_ring_never_grows_past_eight_on_large_buttons(self):
        assert ring_width(MAX_BUTTON_SIZE) == 8

    def test_ring_never_takes_more_than_a_third_of_the_radius(self):
        # The whole point of the min() is that the middle of the button stays
        # usable at every size in the supported range.
        for size in range(MIN_BUTTON_SIZE, MAX_BUTTON_SIZE + 1):
            assert ring_width(size) <= (size / 2) / 3


class TestIsInResizeRing:
    """A press is a resize when it lands at or outside the ring's inner edge."""

    def test_centre_of_a_default_button_is_not_in_the_ring(self):
        assert is_in_resize_ring(25, 25, 50) is False

    def test_outer_edge_of_the_circle_is_in_the_ring(self):
        # Distance from centre exactly equals the radius.
        assert is_in_resize_ring(0, 25, 50) is True

    def test_exact_inner_boundary_is_in_the_ring(self):
        # radius 25 - ring 8 = 17, measured straight left of centre.
        assert is_in_resize_ring(25 - 17, 25, 50) is True

    def test_just_inside_the_boundary_is_not_in_the_ring(self):
        assert is_in_resize_ring(25 - 16.9, 25, 50) is False

    def test_square_window_corner_counts_as_the_ring(self):
        # The corners lie outside the drawn circle entirely, so treating them
        # as edge gives a bigger grab target instead of a dead zone.
        assert is_in_resize_ring(0, 0, 50) is True

    def test_narrowed_ring_on_the_smallest_button_uses_the_narrow_boundary(self):
        # radius 7.5, ring 2.5 -> boundary at distance 5 from centre.
        assert is_in_resize_ring(7.5 - 5, 7.5, MIN_BUTTON_SIZE) is True
        assert is_in_resize_ring(7.5 - 4.9, 7.5, MIN_BUTTON_SIZE) is False

    def test_boundary_is_measured_from_the_centre_not_a_corner(self):
        # (18, 18) is 9.9 px from the centre -- comfortably inside the ring's
        # 17 px boundary -- but 25.5 px from the top-left corner. Measuring
        # from (0, 0) instead of the centre would call this an edge press and
        # steal it from the move gesture.
        assert is_in_resize_ring(18, 18, 50) is False


class TestResizeFromPointer:
    """The button's edge follows the pointer while the centre stays put."""

    def test_diameter_is_twice_the_distance_from_centre_to_pointer(self):
        size, _left, _top = resize_from_pointer(100, 100, 140, 100)
        assert size == 80

    def test_diameter_uses_diagonal_distance_not_one_axis(self):
        # 3-4-5 triangle: distance 50, so diameter 100.
        size, _left, _top = resize_from_pointer(100, 100, 130, 140)
        assert size == 100

    def test_centre_stays_fixed_as_the_button_grows(self):
        size, left, top = resize_from_pointer(100, 100, 140, 100)
        assert left + size / 2 == 100
        assert top + size / 2 == 100

    def test_returned_corner_matches_the_returned_size(self):
        size, left, top = resize_from_pointer(200, 300, 200, 355)
        assert left == 200 - size // 2
        assert top == 300 - size // 2

    def test_clamps_up_to_the_minimum(self):
        size, _left, _top = resize_from_pointer(100, 100, 102, 100)
        assert size == MIN_BUTTON_SIZE

    def test_clamps_down_to_the_maximum(self):
        size, _left, _top = resize_from_pointer(100, 100, 400, 100)
        assert size == MAX_BUTTON_SIZE

    def test_centre_still_holds_when_the_size_is_clamped(self):
        # Clamping must not be applied after the corner is computed, or the
        # button would drift while pinned at a limit.
        size, left, top = resize_from_pointer(100, 100, 400, 100)
        assert left + size // 2 == 100
        assert top + size // 2 == 100

    def test_pointer_exactly_on_the_centre_clamps_instead_of_collapsing(self):
        size, _left, _top = resize_from_pointer(100, 100, 100, 100)
        assert size == MIN_BUTTON_SIZE

    def test_the_two_limits_are_ordered(self):
        assert MIN_BUTTON_SIZE < MAX_BUTTON_SIZE

    # The check that the Ctrl + wheel gesture honours these same limits lives
    # in test_floating_button_resize_drag.py, because it has to drive the real
    # wheel handler. Asserting the constants equal two literals here would look
    # like an agreement check while never reading the wheel code at all.


class TestCorrectOntoScreen:
    """Growing around a fixed centre can push the button off screen."""

    SCREEN = (0, 0, 1920, 1080)

    def test_button_already_fully_visible_is_left_alone(self):
        assert correct_onto_screen(500, 500, 50, self.SCREEN) == (500, 500)

    def test_button_past_the_left_edge_is_pulled_back(self):
        assert correct_onto_screen(-20, 500, 50, self.SCREEN) == (0, 500)

    def test_button_past_the_top_edge_is_pulled_back(self):
        assert correct_onto_screen(500, -20, 50, self.SCREEN) == (500, 0)

    def test_button_past_the_right_edge_is_pulled_back(self):
        assert correct_onto_screen(1900, 500, 50, self.SCREEN) == (1870, 500)

    def test_button_past_the_bottom_edge_is_pulled_back(self):
        assert correct_onto_screen(500, 1060, 50, self.SCREEN) == (500, 1030)

    def test_correction_respects_a_screen_that_does_not_start_at_the_origin(self):
        # A second monitor to the right of the primary one. Clamping to 0
        # instead of the screen's own origin would fling the button to the
        # wrong display.
        secondary = (1920, 0, 1920, 1080)
        assert correct_onto_screen(1900, 500, 50, secondary) == (1920, 500)

    def test_corrects_both_axes_at_once_in_a_corner(self):
        assert correct_onto_screen(-10, -10, 50, self.SCREEN) == (0, 0)

    def test_button_larger_than_the_screen_is_pinned_to_the_origin(self):
        tiny_screen = (0, 0, 40, 40)
        assert correct_onto_screen(-5, -5, 50, tiny_screen) == (0, 0)


class TestGeometryAgree:
    """The pieces have to compose: a resize result must stay grabbable."""

    @pytest.mark.parametrize("size", [MIN_BUTTON_SIZE, 50, 100, MAX_BUTTON_SIZE])
    def test_edge_of_every_supported_size_reads_as_the_ring(self, size):
        radius = size / 2
        # A point on the circle, at an arbitrary angle.
        x = radius + radius * math.cos(math.radians(35))
        y = radius + radius * math.sin(math.radians(35))
        assert is_in_resize_ring(x, y, size) is True

    @pytest.mark.parametrize("size", [MIN_BUTTON_SIZE, 50, 100, MAX_BUTTON_SIZE])
    def test_centre_of_every_supported_size_stays_available_for_moving(self, size):
        radius = size / 2
        assert is_in_resize_ring(radius, radius, size) is False


class TestCorrectOntoAnyScreen:
    """A stored position has to survive the screens changing underneath it.

    ``correct_onto_screen`` above knows one screen, which is right for a
    resize: the gesture began on a screen and stays with it. A stored position
    is a different problem. It was written on whatever screens existed then,
    and it is read back on whatever screens exist now -- a monitor unplugged, a
    resolution changed, a Remote Desktop session at another size. So the
    question here is not "does it overhang an edge" but "can the user still
    see enough of it to click it".

    The rule is one line of arithmetic: add up how much of the button's square
    lands on a screen, and leave the position alone while that is at least
    ``MIN_VISIBLE_AREA_FRACTION`` of the button. Adding the areas rather than
    testing each screen on its own is what lets a button sit across the seam
    between two monitors, where no single screen holds half of it but the user
    can see all of it.

    wh-floating-button-offscreen.
    """

    LEFT = (0, 0, 1920, 1080)
    RIGHT = (1920, 0, 1920, 1080)
    SIZE = 50

    def test_the_fraction_is_a_half(self):
        # The boss set the amount; the tests below read it from the constant,
        # so this is the one place the number itself is checked.
        assert MIN_VISIBLE_AREA_FRACTION == 0.5

    def test_a_button_well_inside_a_screen_is_left_alone(self):
        assert correct_onto_any_screen(500, 500, self.SIZE, [self.LEFT]) == (500, 500)

    def test_a_button_across_the_seam_between_two_monitors_is_left_alone(self):
        # 20 px of the button on the left monitor, 30 px on the right one.
        # Neither screen holds half of it; together they show all of it, so
        # moving it would take away a button the user can see and click.
        assert correct_onto_any_screen(
            1900, 500, self.SIZE, [self.LEFT, self.RIGHT]
        ) == (1900, 500)

    def test_a_button_split_across_a_seam_counts_both_screens_together(self):
        # The test above cannot tell the summed rule from a per-screen one.
        # A 50 px button fully covered by two monitors has 2500 px of area
        # against a 1250 px threshold, so whichever way it splits, one screen
        # always holds at least half. Only a button that ALSO hangs off an
        # outer edge separates them.
        #
        # Here 26 of the 50 rows are on screen: 20 columns on the left
        # monitor and 30 on the right. Left holds 520 px, right holds 780 px,
        # and neither reaches 1250. Added they come to 1300, so the user can
        # see more than half the button and it stays where it is.
        #
        # This is the case the mutation gate found unguarded: replacing sum()
        # with max() in correct_onto_any_screen left every other test green.
        assert correct_onto_any_screen(
            1900, -24, self.SIZE, [self.LEFT, self.RIGHT]
        ) == (1900, -24)

    def test_a_button_exactly_half_off_an_outer_edge_is_left_alone(self):
        # 25 of 50 px of width, all 50 px of height: exactly half the area.
        # "At least half" keeps it, so a position parked on an edge survives.
        assert correct_onto_any_screen(-25, 500, self.SIZE, [self.LEFT]) == (-25, 500)

    def test_a_button_less_than_half_visible_is_moved_fully_onto_the_screen(self):
        # One pixel further out than the case above.
        assert correct_onto_any_screen(-26, 500, self.SIZE, [self.LEFT]) == (0, 500)

    def test_a_button_on_no_screen_at_all_comes_back(self):
        # The monitor it was stored on is gone. This is the case David hit.
        assert correct_onto_any_screen(3000, 2000, self.SIZE, [self.LEFT]) == (1870, 1030)

    def test_a_duplicated_display_does_not_count_the_same_pixels_twice(self):
        # Two screens reporting identical bounds are one display, mirrored.
        # Counting both would make 20 px of visible button look like 40.
        duplicated = [self.LEFT, self.LEFT]
        assert correct_onto_any_screen(-30, 500, self.SIZE, duplicated) == (0, 500)

    def test_a_monitor_left_of_the_primary_one_keeps_its_negative_coordinates(self):
        # Windows gives a monitor placed to the left of the primary display
        # negative x. A button living there is fully visible and must not be
        # dragged onto the primary screen.
        negative = (-1920, 0, 1920, 1080)
        assert correct_onto_any_screen(
            -1000, 500, self.SIZE, [negative, self.LEFT]
        ) == (-1000, 500)

    def test_a_lost_position_returns_to_the_nearest_screen_not_the_first_one(self):
        # Stored far to the right of all three monitors. The right-hand one is
        # nearest, so the button belongs against its right edge, not on either
        # of the others.
        negative = (-1920, 0, 1920, 1080)
        assert correct_onto_any_screen(
            5000, 500, self.SIZE, [negative, self.LEFT, self.RIGHT]
        ) == (3790, 500)

    def test_a_lost_position_above_every_screen_comes_down_to_the_top_edge(self):
        assert correct_onto_any_screen(500, -4000, self.SIZE, [self.LEFT]) == (500, 0)

    def test_no_screens_at_all_leaves_the_position_untouched(self):
        # Qt can report an empty screen list while a session is being torn
        # down or handed over. There is nothing to correct onto, and inventing
        # (0, 0) would overwrite a good stored position with a guess.
        assert correct_onto_any_screen(500, 500, self.SIZE, []) == (500, 500)

    @pytest.mark.parametrize("size", [MIN_BUTTON_SIZE, 50, 100, MAX_BUTTON_SIZE])
    def test_a_button_of_any_supported_size_ends_up_fully_on_a_screen(self, size):
        left, top = correct_onto_any_screen(9000, 9000, size, [self.LEFT])
        screen_x, screen_y, screen_w, screen_h = self.LEFT
        assert left >= screen_x and top >= screen_y
        assert left + size <= screen_x + screen_w
        assert top + size <= screen_y + screen_h
