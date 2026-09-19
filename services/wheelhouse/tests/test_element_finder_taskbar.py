"""ElementFinder taskbar-walk orchestration tests (wh-overlay-taskbar-numbers.2).

The numbered overlay's standalone build path (``overlay_walk``) walks the
taskbar shell windows in addition to the focused window and its owned
popups, merging everything into ONE snapshot with ONE contiguous numbering:
focused-window matches first (reading order), then owned-popup matches,
then taskbar matches last. The by-name ``find()`` path never walks the
taskbar -- "click start" by name stays focused-window-only.

Mirrors test_element_finder_popups.py: driven with fakes, no live COM, no
real display.
"""

from typing import Any

from ui import uia_walker
from ui.element_types import ElementQuery
from ui.uia_walker import UIA_BUTTON, WalkResult

from tests.test_uia_walker import (
    FakeCachedElement,
    FakeElementArray,
    FakeRect,
)
from tests.test_element_finder import el as _interactive_el
from tests.test_element_finder import fg_top as _fg_top
from tests.test_element_finder import FakeArrayTopLevel as _MultiTopLevel
from tests.test_element_finder import FakeAutomation as _FinderFakeAutomation
from tests.test_element_finder import make_multi_finder
from tests.test_element_finder_popups import _popup_walkresult, POPUP_HWND

FOCUSED_HWND = 1000
TASKBAR_HWND = 4001


def _taskbar_walkresult(*names, source_window_hwnd=TASKBAR_HWND):
    """A WalkResult standing in for a taskbar shell-window subtree walk.

    Each match carries the shell window's HWND plus the shell marker, exactly
    as walk_taskbar_windows stamps them. Side-by-side rects along the bottom
    of the screen (like real taskbar buttons), disjoint from the focused
    fakes' rects so no collapse pass fires across the sets.
    """
    array = FakeElementArray([])
    matches = [
        uia_walker.element_match_from_cached(
            FakeCachedElement(name=n, control_type=UIA_BUTTON,
                              localized_control_type="button",
                              rect=FakeRect(10 + 70 * (i - 1), 900,
                                            70 + 70 * (i - 1), 940)),
            display_number=i,
            source_window_hwnd=source_window_hwnd,
            source_window_is_shell=True,
        )
        for i, n in enumerate(names, start=1)
    ]
    return WalkResult(
        matches=matches,
        _keepalive_automation=object(),
        _keepalive_cache_request=object(),
        _keepalive_element_array=array,
        _keepalive_top_level_element=object(),
        deadline_truncated=False,
    )


def test_overlay_walk_merges_taskbar_items_badged_contiguously():
    # Focused window has two controls; the taskbar contributes two buttons.
    # overlay_walk numbers ALL four 1..4 -- focused first, taskbar last --
    # and the stored matches keep the shell HWND + shell marker so the
    # pre-click probe can run the shell branch.
    focused = _MultiTopLevel([
        _interactive_el("Save", rect=FakeRect(10, 20, 110, 70)),
        _interactive_el("Open", rect=FakeRect(120, 20, 220, 70)),
    ])
    taskbar_result = _taskbar_walkresult("Start", "Notepad - taskbar")

    finder = make_multi_finder(
        automation=object(),
        popup_walk_fn=lambda h, **k: [],
        taskbar_walk_fn=lambda **k: [taskbar_result],
    )
    result = finder.overlay_walk(_fg_top(focused))

    assert result.outcome == "ok"
    items = result.summary.items
    assert [i.display_number for i in items] == [1, 2, 3, 4]
    assert [i.item_id for i in items] == ["uia-1", "uia-2", "uia-3", "uia-4"]
    assert [i.name for i in items] == [
        "Save", "Open", "Start", "Notepad - taskbar",
    ]
    matches = result.snapshot.matches
    focused_matches = [m for m in matches if m.source_window_hwnd == 0]
    taskbar_matches = [
        m for m in matches if m.source_window_hwnd == TASKBAR_HWND
    ]
    assert [m.name for m in focused_matches] == ["Save", "Open"]
    assert [m.name for m in taskbar_matches] == ["Start", "Notepad - taskbar"]
    # The renumber preserves the shell marker (replace() keeps every other
    # field) -- losing it would send taskbar clicks down the popup probe.
    assert all(m.source_window_is_shell for m in taskbar_matches)
    assert not any(m.source_window_is_shell for m in focused_matches)
    assert [m.display_number for m in matches] == [1, 2, 3, 4]


def test_overlay_walk_orders_focused_then_popup_then_taskbar():
    focused = _MultiTopLevel([
        _interactive_el("Save", rect=FakeRect(10, 20, 110, 70)),
    ])
    popup_result = _popup_walkresult("Copy item")
    taskbar_result = _taskbar_walkresult("Start")

    finder = make_multi_finder(
        automation=object(),
        popup_walk_fn=lambda h, **k: [popup_result],
        taskbar_walk_fn=lambda **k: [taskbar_result],
    )
    result = finder.overlay_walk(_fg_top(focused))

    assert [i.name for i in result.summary.items] == [
        "Save", "Copy item", "Start",
    ]
    assert [i.display_number for i in result.summary.items] == [1, 2, 3]
    hwnds = [m.source_window_hwnd for m in result.snapshot.matches]
    assert hwnds == [0, POPUP_HWND, TASKBAR_HWND]


def test_overlay_walk_stored_snapshot_pins_taskbar_walkresults():
    # Keepalive contract: the stored snapshot AND the returned result pin the
    # taskbar WalkResult alongside the primary, so taskbar control_refs stay
    # alive for the life of the snapshot.
    focused = _MultiTopLevel([_interactive_el("Save")])
    taskbar_result = _taskbar_walkresult("Start")

    finder = make_multi_finder(
        automation=object(),
        popup_walk_fn=lambda h, **k: [],
        taskbar_walk_fn=lambda **k: [taskbar_result],
    )
    result = finder.overlay_walk(_fg_top(focused))

    stored = finder._stored[result.snapshot.snapshot_id]
    assert len(stored.walk_results) == 2
    assert stored.walk_results[0] is result._walk_results[0]
    assert taskbar_result in stored.walk_results
    assert list(result._walk_results) == list(stored.walk_results)


def test_overlay_walk_passes_shared_cache_request_and_exclude_to_taskbar_walk():
    # The taskbar walk shares the primary walk's automation + cache_request
    # and the one per-request deadline/clock, and excludes the focused window
    # so a focused taskbar is never walked twice.
    focused = _MultiTopLevel([_interactive_el("Save")])
    captured: dict[str, Any] = {}

    def taskbar_walk(**kwargs):
        captured.update(kwargs)
        return []

    finder = make_multi_finder(
        automation=object(),
        popup_walk_fn=lambda h, **k: [],
        taskbar_walk_fn=taskbar_walk,
    )
    finder.overlay_walk(_fg_top(focused))

    assert captured["cache_request"] is not None
    assert captured["exclude_hwnd"] == FOCUSED_HWND
    assert "clock" in captured
    # The overlay's own score hook (keep every match, re-stamp monitor_id)
    # applies to taskbar matches too.
    assert captured["score_hook"] is not None


def test_overlay_walk_shares_one_enumerator_across_popup_and_taskbar_walks():
    # Both additional-subtree walks receive the SAME caching enumerator
    # object, so one EnumWindows pass serves popup detection and taskbar
    # detection alike instead of each walk paying its own blocking Win32
    # enumeration (deepseek finding wh-overlay-taskbar-numbers.5.1).
    focused = _MultiTopLevel([_interactive_el("Save")])
    popup_captured: dict[str, Any] = {}
    taskbar_captured: dict[str, Any] = {}

    def popup_walk(h, **kwargs):
        popup_captured.update(kwargs)
        return []

    def taskbar_walk(**kwargs):
        taskbar_captured.update(kwargs)
        return []

    finder = make_multi_finder(
        automation=object(),
        popup_walk_fn=popup_walk,
        taskbar_walk_fn=taskbar_walk,
    )
    finder.overlay_walk(_fg_top(focused))

    assert popup_captured["enumerator"] is not None
    assert popup_captured["enumerator"] is taskbar_captured["enumerator"]


def test_multi_finder_fixture_defaults_are_hermetic_with_fake_automation():
    # A truthy fake automation object passes overlay_walk's `is not None`
    # gate by design (the COM root IS the production signal), so the shared
    # fixture is the structural hermeticity layer: built WITHOUT explicit
    # walk seams, make_multi_finder must never let the REAL popup/taskbar
    # defaults -- whose default enumerator is a real EnumWindows -- run and
    # find the HOST machine's Shell_TrayWnd (deepseek finding
    # wh-overlay-taskbar-numbers.5.2).
    focused = _MultiTopLevel([_interactive_el("Save")])
    finder = make_multi_finder(automation=object())
    result = finder.overlay_walk(_fg_top(focused))
    assert result.outcome == "ok"
    assert [i.name for i in result.summary.items] == ["Save"]


def test_overlay_walk_excludes_the_int_top_level_hwnd():
    # Production shape (ForegroundContext docstring): ``top_level`` is the
    # resolved top-level HWND -- an int -- while the fake-element shape above
    # exercises only the else arm of the exclude expression. The exclude
    # handed to the taskbar walk must be THAT resolved HWND, not
    # foreground.foreground_window: when the taskbar itself is focused, the
    # foreground HWND can be a child while the walked top-level is
    # Shell_TrayWnd, and excluding the wrong one double-badges every button.
    # (Mutation-gate survivor: finder-exclude-zero passed until this test.)
    focused = _MultiTopLevel([_interactive_el("Save")])
    captured: dict[str, Any] = {}

    def walk_fn(top_level, **kwargs):
        # Production walk_window would resolve the int HWND itself; the fake
        # ignores the token and walks the fixed fake tree.
        assert top_level == 7777
        kwargs.pop("automation", None)
        return uia_walker.walk_window(
            focused, automation=_FinderFakeAutomation(), **kwargs
        )

    def taskbar_walk(**kwargs):
        captured.update(kwargs)
        return []

    finder = make_multi_finder(
        automation=object(),
        walk_fn=walk_fn,
        popup_walk_fn=lambda h, **k: [],
        taskbar_walk_fn=taskbar_walk,
    )
    finder.overlay_walk(_fg_top(7777))

    assert captured["exclude_hwnd"] == 7777


def test_overlay_walk_skips_taskbar_when_primary_overran_its_share():
    # Same budget discipline as the popup walk: a primary walk that overran
    # its 0.7 share ships the focused-only overlay; no taskbar walk fires.
    focused = _MultiTopLevel([_interactive_el("Save")])

    times = iter([0.0, 0.0, 0.5, 0.85])

    def clock():
        return next(times, 0.85)

    taskbar_calls: list = []

    def taskbar_walk(**kwargs):
        taskbar_calls.append(kwargs)
        return [_taskbar_walkresult("Start")]

    finder = make_multi_finder(
        automation=object(),
        popup_walk_fn=lambda h, **k: [],
        taskbar_walk_fn=taskbar_walk,
        clock=clock,
        walk_deadline_ms=1000,
    )
    result = finder.overlay_walk(_fg_top(focused))

    assert [i.name for i in result.summary.items] == ["Save"]
    assert taskbar_calls == []


def test_overlay_walk_runs_popup_but_skips_taskbar_when_share_spent_between():
    # The gate is re-consulted before the taskbar walk: a budget that still
    # allowed the popup walk but is past the checkpoint afterwards ships
    # focused + popup without the taskbar suffix.
    focused = _MultiTopLevel([_interactive_el("Save")])
    popup_result = _popup_walkresult("Copy item")

    # walk_start=0.0, deadline 1.0, checkpoint 0.7. Popup-gate read: 0.65
    # (allowed); taskbar-gate read: 0.75 (skipped).
    times = iter([0.0, 0.0, 0.5, 0.65])

    def clock():
        return next(times, 0.75)

    taskbar_calls: list = []

    def taskbar_walk(**kwargs):
        taskbar_calls.append(kwargs)
        return [_taskbar_walkresult("Start")]

    finder = make_multi_finder(
        automation=object(),
        popup_walk_fn=lambda h, **k: [popup_result],
        taskbar_walk_fn=taskbar_walk,
        clock=clock,
        walk_deadline_ms=1000,
    )
    result = finder.overlay_walk(_fg_top(focused))

    assert [i.name for i in result.summary.items] == ["Save", "Copy item"]
    assert taskbar_calls == []


def test_overlay_walk_taskbar_absent_behaves_like_focused_only():
    # No visible taskbar window (or a headless host) -> [] from the walk fn;
    # the overlay is byte-identical to the pre-taskbar behaviour.
    focused = _MultiTopLevel([
        _interactive_el("Save", rect=FakeRect(10, 20, 110, 70)),
        _interactive_el("Open", rect=FakeRect(120, 20, 220, 70)),
    ])

    finder = make_multi_finder(
        automation=object(),
        popup_walk_fn=lambda h, **k: [],
        taskbar_walk_fn=lambda **k: [],
    )
    result = finder.overlay_walk(_fg_top(focused))

    assert [i.name for i in result.summary.items] == ["Save", "Open"]
    stored = finder._stored[result.snapshot.snapshot_id]
    assert len(stored.walk_results) == 1


def test_find_never_walks_taskbar():
    # The by-name path numbers nothing and must not pay the taskbar walk:
    # "click start" by name stays a focused-window (+ owned popup) search.
    focused = _MultiTopLevel([_interactive_el("Save")])
    taskbar_calls: list = []

    def taskbar_walk(**kwargs):
        taskbar_calls.append(kwargs)
        return [_taskbar_walkresult("Start")]

    finder = make_multi_finder(
        automation=object(),
        popup_walk_fn=lambda h, **k: [],
        taskbar_walk_fn=taskbar_walk,
    )
    finder.find(
        ElementQuery("save", "button", None, None, "save"),
        _fg_top(focused),
    )

    assert taskbar_calls == []
