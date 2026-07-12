"""Unit tests for the restricted window-walk fall-back policy (wh-86qdm).

These exercise the PURE policy of ui/window_fallback.py -- the
overlay-deprioritisation signal detection, the same-monitor restriction (and
its off-monitor opt-in), and the candidate ordering -- against synthetic
FallbackWindow records with no real display. The real-Win32 enumerator is not
exercised here (it is injected as a fake into ElementFinder; see
test_element_finder.py).
"""

from ui.window_fallback import (
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    FallbackWindow,
    is_noactivate,
    is_topmost_toolwindow,
    is_very_small,
    order_candidates,
    resolve_monitor_rect,
    restrict_to_monitor,
)


def win(hwnd, *, pid=100, process_name="app.exe", ex_style=0, rect=(0, 0, 800, 600)):
    return FallbackWindow(
        hwnd=hwnd, pid=pid, process_name=process_name, ex_style=ex_style, rect=rect
    )


# ---------------------------------------------------------------------------
# Overlay-deprioritisation signal detection.
# ---------------------------------------------------------------------------

def test_topmost_plus_toolwindow_is_floating_utility():
    w = win(1, ex_style=WS_EX_TOPMOST | WS_EX_TOOLWINDOW)
    assert is_topmost_toolwindow(w) is True
    # Either flag alone is NOT the floating-utility signal.
    assert is_topmost_toolwindow(win(2, ex_style=WS_EX_TOPMOST)) is False
    assert is_topmost_toolwindow(win(3, ex_style=WS_EX_TOOLWINDOW)) is False


def test_noactivate_signal():
    assert is_noactivate(win(1, ex_style=WS_EX_NOACTIVATE)) is True
    assert is_noactivate(win(2, ex_style=0)) is False


def test_very_small_relative_to_screen():
    monitor = (0, 0, 1920, 1080)  # ~2.07M px
    # 100x50 = 5000 px, well under 5% (~103k px) -> very small.
    assert is_very_small(win(1, rect=(0, 0, 100, 50)), monitor) is True
    # A half-screen window is not very small.
    assert is_very_small(win(2, rect=(0, 0, 960, 540)), monitor) is False
    # Zero-area window is treated as very small (degenerate widget).
    assert is_very_small(win(3, rect=(0, 0, 0, 0)), monitor) is True


def test_very_small_with_degenerate_monitor_is_false():
    # A degenerate (zero-area) monitor rect must not deprioritise every window.
    assert is_very_small(win(1, rect=(0, 0, 100, 50)), (0, 0, 0, 0)) is False


def test_very_small_with_none_monitor_is_false():
    # Finding 46.2/46.3: a None monitor rect (focused monitor unresolved) must
    # not mark every candidate small.
    assert is_very_small(win(1, rect=(0, 0, 100, 50)), None) is False


# ---------------------------------------------------------------------------
# FINDING 46.4: the very-small AREA signal deprioritises ONLY when the window
# also carries a window-STYLE overlay flag. A small plain dialog keeps its
# position; a small window WITH a style flag is deprioritised.
# ---------------------------------------------------------------------------

def test_small_plain_dialog_not_deprioritised_on_area_alone():
    monitor = (0, 0, 1920, 1080)
    large_app = win(1, ex_style=0, rect=(0, 0, 1600, 900))
    small_plain = win(2, ex_style=0, rect=(0, 0, 200, 200))  # tiny, but PLAIN
    # Enumerate the small plain dialog FIRST. Because it has no style overlay,
    # 46.4 does not deprioritise it on area alone, so it keeps its position.
    ordered = order_candidates([small_plain, large_app], monitor_rect=monitor)
    assert [w.hwnd for w in ordered] == [2, 1]


def test_small_window_with_style_overlay_is_deprioritised():
    monitor = (0, 0, 1920, 1080)
    large_app = win(1, ex_style=0, rect=(0, 0, 1600, 900))
    # Small AND noactivate -> the small area now counts (one style + small = 2),
    # so it sorts after the plain large window (count 0).
    small_overlay = win(2, ex_style=WS_EX_NOACTIVATE, rect=(0, 0, 200, 200))
    ordered = order_candidates([small_overlay, large_app], monitor_rect=monitor)
    assert [w.hwnd for w in ordered] == [1, 2]


def test_order_tolerates_none_monitor_rect():
    # Finding 46.2/46.3: order_candidates must not divide by a bogus area when
    # the monitor rect is None; it falls back to the two pure style signals.
    plain = win(1, ex_style=0, rect=(0, 0, 100, 50))           # small but plain
    overlay = win(2, ex_style=WS_EX_NOACTIVATE, rect=(0, 0, 100, 50))
    ordered = order_candidates([overlay, plain], monitor_rect=None)
    # plain has 0 signals (very-small skipped on None rect), overlay has 1.
    assert [w.hwnd for w in ordered] == [1, 2]


# ---------------------------------------------------------------------------
# FINDING 46.3: resolve_monitor_rect returns None when the box overlaps no
# monitor, instead of falling through to the primary monitor.
# ---------------------------------------------------------------------------

def test_resolve_monitor_rect_none_when_box_overlaps_no_monitor(monkeypatch):
    # Inject a fake monitor_geometry layer so this stays headless: one monitor
    # at (0,0,1920,1080). A box far off-screen overlaps it by 0 area, so
    # _resolve_target_monitor falls through to the primary monitor -- and
    # resolve_monitor_rect's overlap guard must reject that fall-through (-> None).
    import sys
    import types

    from PySide6.QtCore import QRect

    class _FakeMonitor:
        def __init__(self, rect):
            self.rect_phys = rect

    primary = _FakeMonitor(QRect(0, 0, 1920, 1080))

    def _fake_overlap_area(a, b):
        inter = a.intersected(b)
        if not inter.isValid() or inter.width() <= 0 or inter.height() <= 0:
            return 0
        return inter.width() * inter.height()

    def _fake_resolve_target_monitor(_ltrb, monitors=None):
        # Mimics the real fall-through: always returns the primary monitor.
        return primary

    fake_mod = types.ModuleType("shared.monitor_geometry")
    fake_mod._overlap_area = _fake_overlap_area  # type: ignore[attr-defined]
    fake_mod._resolve_target_monitor = _fake_resolve_target_monitor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "shared.monitor_geometry", fake_mod)

    # Box at x=10000 overlaps the (0,0,1920,1080) monitor by 0 area -> None.
    assert resolve_monitor_rect((10000, 0, 100, 100)) is None
    # A box that DOES overlap the monitor resolves to its rect.
    assert resolve_monitor_rect((0, 0, 100, 100)) == (0, 0, 1920, 1080)


# ---------------------------------------------------------------------------
# Same-monitor restriction + off-monitor opt-in.
# ---------------------------------------------------------------------------

# Focused monitor 1 is the (0,0,1920,1080) rectangle in these tests.
_MON1 = (0, 0, 1920, 1080)


def test_restrict_keeps_only_overlapping_monitor_when_offmonitor_disabled():
    a = win(1, rect=(0, 0, 800, 600))        # fully on monitor 1
    b = win(2, rect=(3000, 0, 800, 600))     # fully on monitor 2 (no overlap)
    c = win(3, rect=(100, 100, 400, 300))    # fully on monitor 1

    kept = restrict_to_monitor(
        [a, b, c],
        focused_monitor_rect=_MON1,
        enable_offmonitor_fallback=False,
    )
    assert [w.hwnd for w in kept] == [1, 3]


def test_restrict_includes_window_straddling_focused_monitor():
    # Finding 45.3: a window straddling the focused monitor and a neighbour --
    # with MORE area on the neighbour -- still OVERLAPS the focused monitor, so
    # the v5 spec ("screen rectangle overlaps the focused window's monitor")
    # INCLUDES it. The old largest-overlap-id rule wrongly dropped it.
    # Monitor 1 right edge is x=1920. This window spans x=1820..2620 (width
    # 800), so only 100px of width sits on monitor 1 and 700px on monitor 2 --
    # most area on the neighbour, but it still overlaps monitor 1.
    straddler = win(5, rect=(1820, 0, 800, 600))
    kept = restrict_to_monitor(
        [straddler],
        focused_monitor_rect=_MON1,
        enable_offmonitor_fallback=False,
    )
    assert [w.hwnd for w in kept] == [5]


def test_restrict_keeps_all_when_offmonitor_enabled():
    a = win(1, rect=(0, 0, 800, 600))
    b = win(2, rect=(3000, 0, 800, 600))

    kept = restrict_to_monitor(
        [a, b],
        focused_monitor_rect=_MON1,
        enable_offmonitor_fallback=True,
    )
    assert [w.hwnd for w in kept] == [1, 2]


# ---------------------------------------------------------------------------
# Ordering: overlays sort after plain windows; stable among equal counts.
# ---------------------------------------------------------------------------

def test_order_plain_window_before_overlay():
    monitor = (0, 0, 1920, 1080)
    plain = win(1, rect=(0, 0, 960, 540))
    overlay = win(2, ex_style=WS_EX_TOPMOST | WS_EX_TOOLWINDOW, rect=(0, 0, 960, 540))
    ordered = order_candidates([overlay, plain], monitor_rect=monitor)
    assert [w.hwnd for w in ordered] == [1, 2]


def test_order_stable_among_equal_signal_count():
    monitor = (0, 0, 1920, 1080)
    a = win(10, rect=(0, 0, 960, 540))
    b = win(20, rect=(0, 0, 960, 540))
    c = win(30, rect=(0, 0, 960, 540))
    ordered = order_candidates([a, b, c], monitor_rect=monitor)
    assert [w.hwnd for w in ordered] == [10, 20, 30]


def test_order_more_signals_sorts_later():
    monitor = (0, 0, 1920, 1080)
    plain = win(1, rect=(0, 0, 960, 540))
    one_signal = win(2, ex_style=WS_EX_NOACTIVATE, rect=(0, 0, 960, 540))
    # noactivate + topmost+toolwindow + very-small = 3 signals.
    three_signals = win(
        3,
        ex_style=WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
        rect=(0, 0, 100, 50),
    )
    ordered = order_candidates([three_signals, one_signal, plain], monitor_rect=monitor)
    assert [w.hwnd for w in ordered] == [1, 2, 3]
