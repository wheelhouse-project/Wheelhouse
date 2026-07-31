"""Geometry for resizing the floating button by dragging its edge.

Three decisions drive that gesture, and all three are plain arithmetic:

* is a press on the button's edge, or in the middle where it still moves and
  still starts push-to-talk,
* how big does the button become as the pointer drags, and where does its
  corner go if the centre is to stay put,
* if growing pushed the button past a screen edge, where does it belong.

They live here rather than in ``gui.py`` so they can be tested against
numbers instead of a window and a simulated drag. Nothing in this module
imports Qt.

All coordinates are plain numbers. Button-local coordinates put (0, 0) at the
top-left corner of the square window, with the circle inscribed in the square.

Design: docs/superpowers/specs/2026-07-25-floating-button-drag-resize-design.md
"""

from __future__ import annotations

import math

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
