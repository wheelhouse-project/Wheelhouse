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
