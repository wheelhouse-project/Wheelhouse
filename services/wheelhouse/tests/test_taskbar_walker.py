"""Tests for the taskbar shell-window walker (wh-overlay-taskbar-numbers.1).

v1 scope (bead wh-overlay-taskbar-numbers, design doc
2026-08-04-overlay-bubble-badges-design-v1.md "Follow-up"): in addition to
the focused window's subtree, the numbered overlay walks the taskbar shell
windows so taskbar buttons get numbers too. The shell windows are:

* ``Shell_TrayWnd`` -- the primary taskbar.
* ``Shell_SecondaryTrayWnd`` -- one per extra monitor.
* ``NotifyIconOverflowWindow`` -- the Win10 tray-overflow flyout.
* ``TopLevelWindowForOverflowXamlIsland`` -- the Win11 tray-overflow flyout.

Detection is class-name + visibility only: shell windows are UNOWNED
top-levels (no owner check) and the class check needs no live COM (no
control-type probe). Each walked match is stamped with
``source_window_hwnd`` = the shell window's HWND and
``source_window_is_shell=True`` so the pre-click probe verifies the SHELL
window (visible, no owner requirement) instead of running the popup probe,
which requires an owner the taskbar never has.

Drives the pure detection + subtree-walk logic with the same fake
Win32/automation surface test_popup_walker.py established. No live COM, no
real display.
"""

import pytest

from ui import uia_walker
from ui.uia_walker import (
    UIA_BUTTON,
    WalkResult,
    element_match_from_cached,
    enumerate_taskbar_windows,
    walk_taskbar_windows,
    walk_window,
)

# Reuse the established fakes.
from tests.test_popup_walker import (
    FakeDesktop,
    FakeWindow,
    PopupAwareAutomation,
)
from tests.test_uia_walker import (
    FakeCachedElement,
    FakeCacheRequest,
)

FOCUSED_HWND = 1000
PRIMARY_TASKBAR = "Shell_TrayWnd"
SECONDARY_TASKBAR = "Shell_SecondaryTrayWnd"
WIN10_OVERFLOW = "NotifyIconOverflowWindow"
WIN11_OVERFLOW = "TopLevelWindowForOverflowXamlIsland"


def _taskbar_window(hwnd, *, class_name=PRIMARY_TASKBAR, visible=True,
                    elements=None):
    # Shell windows are unowned top-levels; owner=0 models that.
    return FakeWindow(hwnd, owner=0, class_name=class_name, visible=visible,
                      control_type=UIA_BUTTON, elements=elements)


# ---------------------------------------------------------------------------
# make_caching_enumerator: one EnumWindows pass shared across the popup and
# taskbar walks (deepseek finding wh-overlay-taskbar-numbers.5.1).
# ---------------------------------------------------------------------------


def test_make_caching_enumerator_calls_underlying_once():
    calls: list[int] = []

    def underlying():
        calls.append(1)
        return [11, 22]

    cached = uia_walker.make_caching_enumerator(underlying)
    assert cached() == [11, 22]
    assert cached() == [11, 22]
    assert len(calls) == 1


def test_make_caching_enumerator_caches_an_empty_result():
    # An empty desktop enumeration is a valid answer and must be cached too --
    # a None-sentinel cache would re-run the underlying enumerator for it.
    calls: list[int] = []

    def underlying():
        calls.append(1)
        return []

    cached = uia_walker.make_caching_enumerator(underlying)
    assert cached() == []
    assert cached() == []
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# enumerate_taskbar_windows: the detection predicate.
# ---------------------------------------------------------------------------


def test_enumerate_matches_primary_taskbar_class():
    desktop = FakeDesktop([_taskbar_window(4001)])
    hwnds = enumerate_taskbar_windows(
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert hwnds == [4001]


def test_enumerate_matches_secondary_and_overflow_classes():
    desktop = FakeDesktop([
        _taskbar_window(4001, class_name=PRIMARY_TASKBAR),
        _taskbar_window(4002, class_name=SECONDARY_TASKBAR),
        _taskbar_window(4003, class_name=WIN10_OVERFLOW),
        _taskbar_window(4004, class_name=WIN11_OVERFLOW),
    ])
    hwnds = enumerate_taskbar_windows(
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert hwnds == [4001, 4002, 4003, 4004]


def test_enumerate_skips_hidden_taskbar_window():
    # A closed overflow flyout keeps its HWND but is not visible; it must not
    # be walked.
    desktop = FakeDesktop([
        _taskbar_window(4003, class_name=WIN10_OVERFLOW, visible=False),
    ])
    hwnds = enumerate_taskbar_windows(
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert hwnds == []


def test_enumerate_skips_non_taskbar_class():
    desktop = FakeDesktop([
        FakeWindow(5000, owner=0, class_name="Notepad",
                   control_type=UIA_BUTTON),
    ])
    hwnds = enumerate_taskbar_windows(
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert hwnds == []


def test_enumerate_skips_the_excluded_focused_hwnd():
    # When the taskbar itself IS the focused window the primary overlay walk
    # already covers it; walking it again would double-badge every button.
    desktop = FakeDesktop([
        _taskbar_window(4001),
        _taskbar_window(4002, class_name=SECONDARY_TASKBAR),
    ])
    hwnds = enumerate_taskbar_windows(
        exclude_hwnd=4001,
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert hwnds == [4002]


def test_enumerate_tolerates_seam_errors_per_window():
    # A window whose class lookup raises (closed between enumeration and the
    # probe) is skipped, not fatal.
    desktop = FakeDesktop([
        _taskbar_window(4001),
        _taskbar_window(4002, class_name=SECONDARY_TASKBAR),
    ])
    real_class_of = desktop.class_name_of

    def flaky_class_of(hwnd):
        if hwnd == 4001:
            raise OSError("gone")
        return real_class_of(hwnd)

    hwnds = enumerate_taskbar_windows(
        enumerator=desktop.enumerate,
        class_name_fn=flaky_class_of,
        visible_fn=desktop.is_visible,
    )
    assert hwnds == [4002]


# ---------------------------------------------------------------------------
# walk_taskbar_windows: subtree walks under the SHARED CacheRequest.
# ---------------------------------------------------------------------------


def test_walk_taskbar_windows_uses_shared_cache_request_and_stamps_shell():
    desktop = FakeDesktop([
        _taskbar_window(4001, elements=[
            FakeCachedElement(name="Start", control_type=UIA_BUTTON),
        ]),
    ])
    automation = PopupAwareAutomation(desktop)
    shared_cache = FakeCacheRequest()

    results = walk_taskbar_windows(
        automation=automation,
        cache_request=shared_cache,
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert len(results) == 1
    # No taskbar walk builds its own cache request -- the shared one is reused.
    assert automation.cache_requests_created == 0
    result = results[0]
    assert [m.name for m in result.matches] == ["Start"]
    # Every taskbar-sourced match carries the shell window's HWND AND the
    # shell marker, so the pre-click probe runs the shell branch, not the
    # popup branch (whose owner check the unowned taskbar always fails).
    assert all(m.source_window_hwnd == 4001 for m in result.matches)
    assert all(m.source_window_is_shell for m in result.matches)


def test_walk_taskbar_windows_returns_one_walkresult_per_window():
    desktop = FakeDesktop([
        _taskbar_window(4001, elements=[
            FakeCachedElement(name="Start", control_type=UIA_BUTTON),
        ]),
        _taskbar_window(4002, class_name=SECONDARY_TASKBAR, elements=[
            FakeCachedElement(name="Notepad - 2", control_type=UIA_BUTTON),
        ]),
    ])
    automation = PopupAwareAutomation(desktop)

    results = walk_taskbar_windows(
        automation=automation,
        cache_request=FakeCacheRequest(),
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert len(results) == 2
    assert all(isinstance(r, WalkResult) for r in results)
    assert results[0].matches[0].source_window_hwnd == 4001
    assert results[1].matches[0].source_window_hwnd == 4002


def test_walk_taskbar_windows_no_taskbar_skips_element_from_handle():
    """No visible taskbar window -> no extra COM round-trip at all."""
    desktop = FakeDesktop([
        FakeWindow(5000, owner=0, class_name="Other",
                   control_type=UIA_BUTTON),
    ])
    automation = PopupAwareAutomation(desktop)

    results = walk_taskbar_windows(
        automation=automation,
        cache_request=FakeCacheRequest(),
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert results == []
    assert automation.element_from_handle_calls == 0


def test_walk_taskbar_windows_spent_deadline_skips_enumeration():
    """When the shared deadline is already spent on entry, no enumeration and
    no COM happens at all -- the taskbar walk is best-effort suffix work."""
    desktop = FakeDesktop([
        _taskbar_window(4001, elements=[
            FakeCachedElement(name="Start", control_type=UIA_BUTTON),
        ]),
    ])
    automation = PopupAwareAutomation(desktop)

    enum_calls = {"n": 0}

    def counting_enumerate():
        enum_calls["n"] += 1
        return desktop.enumerate()

    results = walk_taskbar_windows(
        automation=automation,
        cache_request=FakeCacheRequest(),
        enumerator=counting_enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        deadline=0.1,
        clock=lambda: 5.0,  # already past the deadline on entry
    )
    assert results == []
    assert enum_calls["n"] == 0
    assert automation.element_from_handle_calls == 0


def test_walk_taskbar_windows_truncated_walk_dropped():
    """A per-window walk the deadline cuts short is DROPPED (fail closed),
    not appended as a partial prefix of the taskbar's controls."""
    desktop = FakeDesktop([
        _taskbar_window(4001, elements=[
            FakeCachedElement(name="Start", control_type=UIA_BUTTON),
        ]),
    ])
    automation = PopupAwareAutomation(desktop)

    # The clock passes the deadline AFTER the entry check, so the per-window
    # walk itself is what gets truncated.
    ticks = iter([0.0, 5.0])

    def stepping_clock():
        try:
            return next(ticks)
        except StopIteration:
            return 5.0

    results = walk_taskbar_windows(
        automation=automation,
        cache_request=FakeCacheRequest(),
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        deadline=1.0,
        clock=stepping_clock,
    )
    assert results == []


def test_walk_taskbar_windows_skips_stale_window():
    """A shell window whose ElementFromHandle raises (the overflow flyout
    closed between enumeration and the walk) is skipped; later windows still
    walk."""
    desktop = FakeDesktop([
        _taskbar_window(4003, class_name=WIN10_OVERFLOW, elements=[
            FakeCachedElement(name="Tray icon", control_type=UIA_BUTTON),
        ]),
        _taskbar_window(4001, elements=[
            FakeCachedElement(name="Start", control_type=UIA_BUTTON),
        ]),
    ])

    class FlakyAutomation(PopupAwareAutomation):
        def ElementFromHandle(self, hwnd):
            if hwnd == 4003:
                raise OSError("window gone")
            return super().ElementFromHandle(hwnd)

    automation = FlakyAutomation(desktop)

    results = walk_taskbar_windows(
        automation=automation,
        cache_request=FakeCacheRequest(),
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert len(results) == 1
    assert results[0].matches[0].source_window_hwnd == 4001


def test_walk_taskbar_windows_drops_window_reused_by_non_shell_class():
    """HWND recycling (codex finding wh-overlay-taskbar-numbers.5.3): the
    handle passed the shell-class predicate at enumeration, but by walk time
    Windows recycled it for an unrelated window. The pre-walk class re-read
    drops it -- otherwise the walk badges a live unrelated window and the
    executor could later invoke into it. Later windows still walk."""
    desktop = FakeDesktop([
        _taskbar_window(4003, class_name=WIN10_OVERFLOW, elements=[
            FakeCachedElement(name="Tray icon", control_type=UIA_BUTTON),
        ]),
        _taskbar_window(4001, elements=[
            FakeCachedElement(name="Start", control_type=UIA_BUTTON),
        ]),
    ])
    automation = PopupAwareAutomation(desktop)

    # First read per hwnd (enumeration) reports the real class; the second
    # read (the pre-walk re-check) sees 4003 recycled to a non-shell class.
    reads: dict[int, int] = {}

    def recycling_class_name_of(hwnd):
        reads[hwnd] = reads.get(hwnd, 0) + 1
        if hwnd == 4003 and reads[hwnd] > 1:
            return "ReusedWindow"
        return desktop.class_name_of(hwnd)

    results = walk_taskbar_windows(
        automation=automation,
        cache_request=FakeCacheRequest(),
        enumerator=desktop.enumerate,
        class_name_fn=recycling_class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert len(results) == 1
    assert results[0].matches[0].source_window_hwnd == 4001
    # The recycled window never cost a COM walk.
    assert automation.element_from_handle_calls == 1


def test_walk_taskbar_windows_drops_window_hidden_between_enumeration_and_walk():
    """The overflow flyout closed (hidden, not destroyed) between enumeration
    and the walk: the pre-walk visibility re-read drops it."""
    desktop = FakeDesktop([
        _taskbar_window(4003, class_name=WIN10_OVERFLOW, elements=[
            FakeCachedElement(name="Tray icon", control_type=UIA_BUTTON),
        ]),
        _taskbar_window(4001, elements=[
            FakeCachedElement(name="Start", control_type=UIA_BUTTON),
        ]),
    ])
    automation = PopupAwareAutomation(desktop)

    reads: dict[int, int] = {}

    def closing_is_visible(hwnd):
        reads[hwnd] = reads.get(hwnd, 0) + 1
        if hwnd == 4003 and reads[hwnd] > 1:
            return False
        return desktop.is_visible(hwnd)

    results = walk_taskbar_windows(
        automation=automation,
        cache_request=FakeCacheRequest(),
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=closing_is_visible,
    )
    assert len(results) == 1
    assert results[0].matches[0].source_window_hwnd == 4001
    assert automation.element_from_handle_calls == 1


def test_walk_taskbar_windows_prewalk_recheck_error_drops_window():
    """A pre-walk re-read that raises (the window died mid-probe) drops that
    window like any stale window; later windows still walk."""
    desktop = FakeDesktop([
        _taskbar_window(4003, class_name=WIN10_OVERFLOW, elements=[
            FakeCachedElement(name="Tray icon", control_type=UIA_BUTTON),
        ]),
        _taskbar_window(4001, elements=[
            FakeCachedElement(name="Start", control_type=UIA_BUTTON),
        ]),
    ])
    automation = PopupAwareAutomation(desktop)

    reads: dict[int, int] = {}

    def dying_class_name_of(hwnd):
        reads[hwnd] = reads.get(hwnd, 0) + 1
        if hwnd == 4003 and reads[hwnd] > 1:
            raise OSError("window gone mid-probe")
        return desktop.class_name_of(hwnd)

    results = walk_taskbar_windows(
        automation=automation,
        cache_request=FakeCacheRequest(),
        enumerator=desktop.enumerate,
        class_name_fn=dying_class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert len(results) == 1
    assert results[0].matches[0].source_window_hwnd == 4001
    assert automation.element_from_handle_calls == 1


def test_walk_taskbar_windows_propagates_non_stale_hook_error():
    """A programming error in the score_hook must surface, not be swallowed
    as a skipped window (same contract as walk_owned_popups)."""
    desktop = FakeDesktop([
        _taskbar_window(4001, elements=[
            FakeCachedElement(name="Start", control_type=UIA_BUTTON),
        ]),
    ])
    automation = PopupAwareAutomation(desktop)

    def broken_hook(matches):
        raise ValueError("bug in hook")

    with pytest.raises(ValueError):
        walk_taskbar_windows(
            automation=automation,
            cache_request=FakeCacheRequest(),
            score_hook=broken_hook,
            enumerator=desktop.enumerate,
            class_name_fn=desktop.class_name_of,
            visible_fn=desktop.is_visible,
        )


def test_walk_taskbar_windows_skips_offscreen_controls():
    """The taskbar walk always skips off-screen / zero-area controls: an
    auto-hidden taskbar reports IsWindowVisible=True while its buttons sit
    off-screen, and badging an invisible button wastes a number the user
    cannot see."""
    desktop = FakeDesktop([
        _taskbar_window(4001, elements=[
            FakeCachedElement(name="Start", control_type=UIA_BUTTON),
            FakeCachedElement(name="Hidden", control_type=UIA_BUTTON,
                              is_offscreen=True),
        ]),
    ])
    automation = PopupAwareAutomation(desktop)

    results = walk_taskbar_windows(
        automation=automation,
        cache_request=FakeCacheRequest(),
        enumerator=desktop.enumerate,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
    )
    assert [m.name for m in results[0].matches] == ["Start"]


# ---------------------------------------------------------------------------
# The source_window_is_shell stamp threading.
# ---------------------------------------------------------------------------


def test_walk_window_stamps_source_window_is_shell():
    desktop = FakeDesktop([
        _taskbar_window(4001, elements=[
            FakeCachedElement(name="Start", control_type=UIA_BUTTON),
        ]),
    ])
    automation = PopupAwareAutomation(desktop)

    result = walk_window(
        4001,
        automation=automation,
        cache_request=FakeCacheRequest(),
        source_window_hwnd=4001,
        source_window_is_shell=True,
    )
    assert result.matches
    assert all(m.source_window_is_shell for m in result.matches)


def test_element_match_source_window_is_shell_defaults_false():
    # Every pre-existing construction site (primary walk, popup walk, tests)
    # must keep building non-shell matches unchanged.
    match = element_match_from_cached(
        FakeCachedElement(name="OK"), display_number=1
    )
    assert match.source_window_is_shell is False
