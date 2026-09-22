"""Geometry for placing and resizing the floating button.

Three decisions drive the edge-drag resize, and all three are plain
arithmetic:

* is a press on the button's edge, or in the middle where it still moves and
  still starts push-to-talk,
* how big does the button become as the pointer drags, and where does its
  corner go if the centre is to stay put,
* if growing pushed the button past a screen edge, where does it belong.

A fourth decision has nothing to do with that gesture: a position stored under
one set of monitors has to be checked before it is applied under another, or
the button can come back where no screen is and no control is left to drag it
back (wh-floating-button-offscreen). ``correct_onto_any_screen`` answers it.

They live here rather than in ``gui.py`` so they can be tested against
numbers instead of a window and a simulated drag. Nothing in this module
imports Qt.

All coordinates are plain numbers. Button-local coordinates put (0, 0) at the
top-left corner of the square window, with the circle inscribed in the square.

Design: docs/superpowers/specs/2026-07-25-floating-button-drag-resize-design.md
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# The size limits for the button. Both the edge drag and the Ctrl + mouse wheel
# gesture resize the same button, so they import these rather than each writing
# the numbers down; a disagreement would let one gesture reach a size the other
# refuses.
MIN_BUTTON_SIZE = 15
MAX_BUTTON_SIZE = 150

# How wide the grab ring is on a button big enough to afford it.
RING_WIDTH_PX = 8


def ring_width(size: float) -> float:
    """Return the width of the resize ring on a button of ``size`` pixels.

    The ring is ``RING_WIDTH_PX`` wide, except on a button small enough that
    8 px would swallow the middle. There it narrows to a third of the radius,
    which keeps a usable move/push-to-talk area at every supported size.
    """
    radius = size / 2
    return min(RING_WIDTH_PX, radius / 3)


def is_in_resize_ring(local_x: float, local_y: float, size: float) -> bool:
    """Return whether a button-local point falls in the resize ring.

    The test is distance from the button's centre, so the square window's
    corners -- which lie outside the drawn circle -- count as ring. They are
    already outside the visible button, so treating them as edge gives a more
    forgiving target rather than a dead zone.
    """
    radius = size / 2
    distance = math.hypot(local_x - radius, local_y - radius)
    return distance >= radius - ring_width(size)


def resize_from_pointer(
    centre_x: float,
    centre_y: float,
    pointer_x: float,
    pointer_y: float,
) -> tuple[int, int, int]:
    """Resize so the button's edge sits under the pointer, centre unmoved.

    Returns ``(size, left, top)``: the new diameter clamped to the supported
    range, and the window's top-left corner for that diameter. The corner is
    derived from the clamped size, so a button held against a limit stays put
    instead of drifting with the pointer.
    """
    distance = math.hypot(pointer_x - centre_x, pointer_y - centre_y)
    size = int(round(distance * 2))
    size = max(MIN_BUTTON_SIZE, min(size, MAX_BUTTON_SIZE))

    left = int(round(centre_x)) - size // 2
    top = int(round(centre_y)) - size // 2
    return size, left, top


def correct_onto_screen(
    left: int,
    top: int,
    size: int,
    screen: tuple[int, int, int, int],
) -> tuple[int, int]:
    """Nudge a button position back inside ``screen`` if it overhangs an edge.

    ``screen`` is ``(x, y, width, height)`` in virtual-desktop coordinates, so
    a monitor that does not start at the origin clamps to its own bounds rather
    than to (0, 0). Only the position is corrected; the requested size is
    always honoured. A button larger than the screen is pinned to the screen's
    origin, which keeps its top-left visible.
    """
    screen_x, screen_y, screen_w, screen_h = screen

    corrected_left = min(left, screen_x + screen_w - size)
    corrected_left = max(screen_x, corrected_left)

    corrected_top = min(top, screen_y + screen_h - size)
    corrected_top = max(screen_y, corrected_top)

    return corrected_left, corrected_top


# How much of the button has to land on a screen before its stored position is
# left alone. Half of the button's area. Less than that is not enough for
# someone to find the button and click it, which is the whole point of the
# check; more than that would move a button a user deliberately parked on an
# edge. The amount lives here as one name so the check and its tests cannot
# disagree about it.
MIN_VISIBLE_AREA_FRACTION = 0.5


def _overlap_area(
    left: int,
    top: int,
    size: int,
    screen: tuple[int, int, int, int],
) -> int:
    """Return how many square pixels of the button land on ``screen``."""
    screen_x, screen_y, screen_w, screen_h = screen

    width = min(left + size, screen_x + screen_w) - max(left, screen_x)
    height = min(top + size, screen_y + screen_h) - max(top, screen_y)
    if width <= 0 or height <= 0:
        return 0
    return width * height


def _distance_to_screen(
    left: int,
    top: int,
    size: int,
    screen: tuple[int, int, int, int],
) -> float:
    """Return the distance from the button's centre to ``screen``.

    Zero when the centre is already inside the screen. Measuring from the
    centre rather than the corner is what sends a button stored far to the
    right of everything back to the right-hand monitor instead of the first
    one in the list.
    """
    screen_x, screen_y, screen_w, screen_h = screen

    centre_x = left + size / 2
    centre_y = top + size / 2
    nearest_x = min(max(centre_x, screen_x), screen_x + screen_w)
    nearest_y = min(max(centre_y, screen_y), screen_y + screen_h)
    return math.hypot(centre_x - nearest_x, centre_y - nearest_y)


def correct_onto_any_screen(
    left: int,
    top: int,
    size: int,
    screens: Sequence[tuple[int, int, int, int]],
) -> tuple[int, int]:
    """Bring a stored button position back where the user can reach it.

    ``correct_onto_screen`` above answers a different question. It knows one
    screen, because a resize begins on a screen and stays with it. This one is
    for a position that was stored under one set of monitors and is being
    applied under another: a monitor unplugged, a resolution changed, a Remote
    Desktop session at a different size. The button can then sit where no
    screen is at all, and nothing in the window is left to drag it back.

    ``screens`` is a sequence of ``(x, y, width, height)`` rectangles in
    virtual-desktop coordinates, the same shape ``correct_onto_screen`` takes.
    Order decides nothing except which screen wins a tie.

    The button keeps its position while at least ``MIN_VISIBLE_AREA_FRACTION``
    of its area lands on a screen. The areas are ADDED, not compared one screen
    at a time: a button sitting across the seam between two monitors can have
    less than half of itself on either one and still be fully visible, and
    moving it would take away a button the user can see. Two screens reporting
    the same rectangle are one mirrored display, so identical rectangles count
    once; counting both would make a barely visible button look reachable.

    Otherwise the whole button moves onto the screen nearest its centre, by
    ``correct_onto_screen``, so it arrives fully inside that screen.

    An empty ``screens`` leaves the position untouched. There is nothing to
    correct onto, and inventing a position would overwrite a good stored one
    with a guess.
    """
    if not screens:
        return left, top

    # Identical rectangles are one mirrored display. dict.fromkeys keeps the
    # first occurrence of each and preserves order, so ties still go to the
    # earliest screen the caller listed.
    distinct = list(dict.fromkeys(
        (screen[0], screen[1], screen[2], screen[3]) for screen in screens
    ))

    visible = sum(_overlap_area(left, top, size, screen) for screen in distinct)
    if visible >= size * size * MIN_VISIBLE_AREA_FRACTION:
        return left, top

    nearest = min(distinct, key=lambda screen: _distance_to_screen(left, top, size, screen))
    return correct_onto_screen(left, top, size, nearest)
