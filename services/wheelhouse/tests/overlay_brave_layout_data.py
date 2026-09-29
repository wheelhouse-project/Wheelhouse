"""Measured and synthetic browser layouts for the overlay badge guard tests,
as numbers only (wh-overlay-toolbar-badges-cover-icons, part 2).

MEASURED: a read-only walk of Brave on 2026-09-25 09:08 (Dispatcher 0b's
diagnosis stage; the bead comment "STAGE diagnose DONE" holds the method).
One 3840 x 2160 physical-px monitor at display scale 3.0 (1280 x 720
logical px). Brave was maximized with six pinned tabs; a vertical taskbar
sat at the right monitor edge. Each item is ``(x, y, w, h, control_type)``:
the rectangle in PHYSICAL px exactly as the walk reported it, and the UIA
control-type id (50000 Button, 50002 CheckBox, 50004 Edit, 50005
Hyperlink, 50019 TabItem, 50026 Group, 50029 DataItem). The list is in
walk order, so an item's badge number is its index plus 1.
``measured_layout`` returns the rectangles in LOGICAL px (physical / DPR).

The walk's names and texts are NOT here, on purpose: the page was a mail
inbox. Only positions, sizes and control types are kept.

GOOGLE-LIKE: a synthetic page with the measured browser frame (caption
buttons, address-bar row, tabs, taskbar) and, in the page, small header
links and buttons at the top left and top right (like google.com's About,
Store, Gmail, Images, apps and sign-in controls), a search field with its
buttons, and footer links. The page rectangles are LOGICAL px, invented to
match the shape of David's 2026-09-25 08:59 screenshot, not measured.
"""

MEASURED_DPR = 3.0
MEASURED_MONITOR_PHYS = (3840, 2160)
MEASURED_MONITOR_LOGICAL = (
    MEASURED_MONITOR_PHYS[0] / MEASURED_DPR,
    MEASURED_MONITOR_PHYS[1] / MEASURED_DPR,
)

# (x, y, w, h physical px, UIA control-type id), in walk order. The comment
# on each line is the badge number.
MEASURED_ITEMS_PHYS = (
    (3285, 0, 135, 120, 50000),  # 1
    (3420, 0, 138, 120, 50000),  # 2
    (3558, 0, 138, 120, 50000),  # 3
    (0, 138, 102, 84, 50000),  # 4
    (108, 138, 84, 84, 50000),  # 5
    (198, 138, 84, 84, 50000),  # 6
    (288, 138, 84, 84, 50000),  # 7
    (405, 138, 84, 84, 50000),  # 8
    (513, 138, 2235, 84, 50004),  # 9
    (2793, 138, 90, 84, 50000),  # 10
    (2910, 138, 84, 84, 50000),  # 11
    (3000, 138, 84, 84, 50000),  # 12
    (3090, 138, 84, 84, 50000),  # 13
    (3180, 138, 84, 84, 50000),  # 14
    (3270, 138, 84, 84, 50000),  # 15
    (3360, 138, 84, 84, 50000),  # 16
    (3450, 138, 84, 84, 50000),  # 17
    (3594, 138, 102, 84, 50000),  # 18
    (36, 267, 144, 144, 50000),  # 19
    (192, 279, 327, 132, 50005),  # 20
    (933, 309, 1791, 60, 50004),  # 21
    (2700, 267, 168, 138, 50000),  # 22
    (768, 267, 168, 138, 50000),  # 23
    (2958, 279, 120, 120, 50000),  # 24
    (3102, 279, 120, 120, 50000),  # 25
    (3246, 279, 120, 120, 50000),  # 26
    (3390, 279, 120, 120, 50000),  # 27
    (3534, 279, 120, 120, 50000),  # 28
    (48, 459, 120, 120, 50000),  # 29
    (192, 612, 113, 53, 50005),  # 30
    (192, 684, 141, 53, 50005),  # 31
    (192, 756, 165, 53, 50005),  # 32
    (192, 828, 87, 53, 50005),  # 33
    (192, 900, 135, 53, 50005),  # 34
    (192, 972, 104, 53, 50005),  # 35
    (72, 1035, 72, 72, 50000),  # 36
    (78, 1191, 60, 60, 50000),  # 37
    (192, 1296, 131, 53, 50005),  # 38
    (192, 1368, 65, 53, 50005),  # 39
    (192, 1440, 98, 53, 50005),  # 40
    (192, 1512, 63, 53, 50005),  # 41
    (72, 1575, 72, 72, 50000),  # 42
    (264, 477, 120, 60, 50000),  # 43
    (264, 477, 60, 60, 50002),  # 44
    (396, 477, 200, 60, 50000),  # 45
    (607, 477, 150, 60, 50000),  # 46
    (3096, 453, 240, 108, 50000),  # 47
    (3372, 477, 60, 60, 50000),  # 48
    (3492, 477, 60, 60, 50000),  # 49
    (216, 591, 9, 60, 50029),  # 50
    (264, 591, 60, 60, 50002),  # 51
    (354, 591, 60, 60, 50000),  # 52
    (444, 591, 90, 60, 50029),  # 53
    (534, 591, 600, 60, 50029),  # 54
    (1134, 591, 2136, 60, 50005),  # 55
    (3300, 591, 84, 60, 50029),  # 56
    (3384, 591, 216, 60, 50029),  # 57
    (216, 675, 9, 60, 50029),  # 58
    (264, 675, 60, 60, 50002),  # 59
    (354, 675, 60, 60, 50000),  # 60
    (444, 675, 90, 60, 50029),  # 61
    (534, 675, 600, 60, 50029),  # 62
    (1134, 675, 2136, 60, 50005),  # 63
    (3300, 675, 84, 60, 50029),  # 64
    (3384, 675, 216, 60, 50029),  # 65
    (216, 759, 9, 60, 50029),  # 66
    (264, 759, 60, 60, 50002),  # 67
    (354, 759, 60, 60, 50000),  # 68
    (444, 759, 90, 60, 50029),  # 69
    (534, 759, 600, 60, 50029),  # 70
    (1134, 759, 2136, 60, 50005),  # 71
    (3300, 759, 84, 60, 50029),  # 72
    (3384, 759, 216, 60, 50029),  # 73
    (216, 843, 9, 60, 50029),  # 74
    (264, 843, 60, 60, 50002),  # 75
    (354, 843, 60, 60, 50000),  # 76
    (444, 843, 90, 60, 50029),  # 77
    (534, 843, 600, 60, 50029),  # 78
    (1134, 843, 2136, 60, 50005),  # 79
    (3300, 843, 84, 60, 50029),  # 80
    (3384, 843, 216, 60, 50029),  # 81
    (216, 927, 9, 60, 50029),  # 82
    (264, 927, 60, 60, 50002),  # 83
    (354, 927, 60, 60, 50000),  # 84
    (444, 927, 90, 60, 50029),  # 85
    (534, 927, 600, 60, 50029),  # 86
    (1134, 927, 2136, 60, 50005),  # 87
    (3300, 927, 84, 60, 50029),  # 88
    (3384, 927, 216, 60, 50029),  # 89
    (264, 1470, 660, 120, 50005),  # 90
    (1634, 1477, 100, 45, 50005),  # 91
    (1760, 1477, 119, 45, 50005),  # 92
    (1906, 1477, 273, 45, 50005),  # 93
    (3437, 1537, 112, 45, 50005),  # 94
    (3600, 1992, 168, 168, 50000),  # 95
    (1677, 1929, 297, 96, 50005),  # 96
    (3085, 1923, 518, 108, 50026),  # 97
    (3133, 1923, 422, 108, 50000),  # 98
    (0, 0, 114, 120, 50019),  # 99
    (108, 0, 114, 120, 50019),  # 100
    (216, 0, 114, 120, 50019),  # 101
    (324, 0, 114, 120, 50019),  # 102
    (432, 0, 114, 120, 50019),  # 103
    (540, 0, 114, 120, 50019),  # 104
    (648, 0, 744, 120, 50019),  # 105
    (1404, 0, 84, 120, 50000),  # 106
    (3696, 0, 144, 78, 50000),  # 107
    (3696, 2139, 144, 21, 50000),  # 108
    (3696, 2067, 144, 72, 50000),  # 109
    (3696, 1947, 144, 120, 50000),  # 110
    (3696, 1767, 144, 180, 50000),  # 111
    (3696, 1767, 72, 90, 50000),  # 112
    (3768, 1767, 72, 90, 50000),  # 113
    (3696, 1857, 72, 90, 50000),  # 114
    (3696, 1623, 72, 72, 50000),  # 115
    (3768, 1623, 72, 72, 50000),  # 116
    (3696, 1695, 72, 72, 50000),  # 117
    (3696, 1551, 144, 72, 50000),  # 118
    (3696, 80, 144, 78, 50000),  # 119
    (3696, 239, 144, 78, 50000),  # 120
    (3696, 398, 144, 78, 50000),  # 121
    (3696, 479, 144, 78, 50000),  # 122
    (3696, 560, 144, 78, 50000),  # 123
    (3696, 641, 144, 78, 50000),  # 124
    (3696, 722, 144, 78, 50000),  # 125
    (3696, 803, 144, 78, 50000),  # 126
    (3696, 884, 144, 78, 50000),  # 127
    (3696, 965, 144, 78, 50000),  # 128
    (3696, 1046, 144, 78, 50000),  # 129
)

# Index constants (0-based list indexes; badge number = index + 1).
CAPTION_BUTTONS = (0, 1, 2)
# The address-bar row, left of the address field: Back, Forward, Reload,
# split view, site information. Back touches the left monitor edge.
BACK = 3
FORWARD = 4
LEFT_GROUP = (3, 4, 5, 6, 7)
ADDRESS_FIELD = 8
# The address-bar row, right of the address field: Brave Shields, seven
# extension and toolbar icons, and the menu button.
RIGHT_GROUP = (9, 10, 11, 12, 13, 14, 15, 16, 17)
# Everything the walk found inside the web page (below the toolbar band).
PAGE = tuple(range(18, 98))
PINNED_TABS = (98, 99, 100, 101, 102, 103)
ACTIVE_TAB = 104
NEW_TAB = 105
# The vertical taskbar at the right monitor edge.
TASKBAR = tuple(range(106, 129))
# Bottom of the address-bar row band, physical px (y 138 + h 84).
TOOLBAR_BAND_BOTTOM_PHYS = 222


def _logical(rect_phys):
    x, y, w, h = rect_phys
    return (x / MEASURED_DPR, y / MEASURED_DPR, w / MEASURED_DPR,
            h / MEASURED_DPR)


def measured_layout(*, page=True):
    """The measured walk as ``(number, x, y, w, h, control_type)`` in
    LOGICAL px, numbered in walk order.

    ``page=False`` drops the PAGE items and renumbers the rest 1..K in the
    same order; the index constants before PAGE (BACK, LEFT_GROUP,
    RIGHT_GROUP, ...) still index the same controls."""
    kept = [
        item for index, item in enumerate(MEASURED_ITEMS_PHYS)
        if page or index not in PAGE
    ]
    return [
        (number, *_logical(item[:4]), item[4])
        for number, item in enumerate(kept, start=1)
    ]


# Synthetic google.com-like page controls, LOGICAL px:
# (x, y, w, h, UIA control-type id). The page starts at y = 74, the bottom
# of the measured toolbar band.
GOOGLE_PAGE_ITEMS_LOGICAL = (
    # Header, top left: two short text links.
    (14, 89, 52, 30, 50005),
    (70, 89, 44, 30, 50005),
    # Header, top right: two short text links, the apps button and the
    # sign-in button.
    (948, 89, 44, 30, 50005),
    (998, 89, 54, 30, 50005),
    (1060, 84, 40, 40, 50000),
    (1112, 84, 96, 40, 50000),
    # Search field with two small buttons inside its right end.
    (330, 340, 560, 46, 50004),
    (840, 348, 30, 30, 50000),
    (876, 348, 30, 30, 50000),
    # The two search buttons.
    (470, 420, 140, 36, 50000),
    (622, 420, 150, 36, 50000),
    # Footer links.
    (16, 690, 90, 24, 50005),
    (112, 690, 70, 24, 50005),
    (188, 690, 126, 24, 50005),
    (980, 690, 60, 24, 50005),
    (1046, 690, 50, 24, 50005),
    (1102, 690, 64, 24, 50005),
)
# Indexes of the six header controls in google_like_layout().
GOOGLE_HEADER = tuple(range(18, 24))


def google_like_layout():
    """The measured browser frame with the synthetic google.com-like page
    in place of the measured page, as ``(number, x, y, w, h,
    control_type)`` in LOGICAL px. The frame keeps its measured order and
    the page items take the PAGE position, so BACK, FORWARD, LEFT_GROUP
    and RIGHT_GROUP index the same controls here."""
    frame_before = [
        (*_logical(item[:4]), item[4])
        for item in MEASURED_ITEMS_PHYS[:PAGE[0]]
    ]
    frame_after = [
        (*_logical(item[:4]), item[4])
        for item in MEASURED_ITEMS_PHYS[PAGE[-1] + 1:]
    ]
    page = [
        tuple(float(v) for v in item[:4]) + (item[4],)
        for item in GOOGLE_PAGE_ITEMS_LOGICAL
    ]
    items = frame_before + page + frame_after
    return [(number, *item) for number, item in enumerate(items, start=1)]
