"""Tests for the ElementFinder coordinator + UIAStrategy (wh-agd2v).

These exercise the composed find() path end to end with fakes -- no live COM,
no real display. The walker's COM interop is driven through the same fake
cached-element / element-array surface that test_uia_walker.py defines, so the
composition tests fold-then-score-then-decide against Chromium-shaped trees.

Three classes of test live here:

* snapshot store / replace / invalidate (new walk, foreground change, TTL via
  an injected clock) and the WalkSnapshotSummary projection;
* browser vs non-browser wiring (query_has_role=False for Chromium so all
  three fold rules are reachable; apply_dom_corrections NOT applied for
  non-browser processes; score_hook filters to eligible);
* the three v5 composition tests (full Chromium shape with all three fold
  rules; folding removes the only exact-name match -> not_found; folding
  promotes a sibling 1->2 exercising margin + tiebreaker).
"""

import gc
import logging
from typing import Any

from ui.element_types import (
    ElementQuery,
    WalkSnapshot,
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)
from ui import uia_walker
from ui.element_finder import ElementFinder, ForegroundContext, FindResult
from ui.strategies.uia_strategy import UIAStrategy

# Reuse the walker's fake cached-element / element-array surface.
from tests.test_uia_walker import FakeCachedElement, FakeElementArray, FakeRect


# UIA control-type ids for the Chromium-shape fakes.
UIA_BUTTON = uia_walker.UIA_BUTTON
UIA_HYPERLINK = uia_walker.UIA_HYPERLINK
UIA_LISTITEM = uia_walker.UIA_LISTITEM
UIA_EDIT = uia_walker.UIA_EDIT
UIA_DATAITEM = uia_walker.UIA_DATAITEM
UIA_TREEITEM = uia_walker.UIA_TREEITEM
UIA_CHECKBOX = uia_walker.UIA_CHECKBOX
UIA_TEXT = 50020  # UIA_TextControlTypeId
UIA_GROUP = 50026  # UIA_GroupControlTypeId
UIA_HEADING = 50036  # UIA_HeaderControlTypeId stand-in for "heading"


# ---------------------------------------------------------------------------
# Fake walk wiring.
#
# ElementFinder must call walk_window via an injectable walk_fn. We pass a
# fake walk_fn that drives the REAL walk_window against a fake element array,
# so the real interactive filter + the real browser_correction_hook +
# score_hook all run. This is what makes these true composition tests.
# ---------------------------------------------------------------------------

class FakeArrayTopLevel:
    """A fake top-level element whose FindAllBuildCache returns a fixed array."""

    def __init__(self, elements):
        self._array = FakeElementArray(elements)

    def FindAllBuildCache(self, _scope, _cond, _cache):
        return self._array


class FakeAutomation:
    """Minimal IUIAutomation stand-in for the walker's COM calls."""

    def CreateCacheRequest(self):
        class _Req:
            TreeScope = 0

            def AddProperty(self, _):
                pass

            def AddPattern(self, _):
                pass

        return _Req()

    def CreateTrueCondition(self):
        return object()

    def ElementFromHandle(self, _hwnd):
        raise AssertionError("tests pass a resolved top-level, never an HWND")


def make_real_walk_fn(top_level_element):
    """Return a walk_fn that drives the REAL walk_window over a fake tree.

    The returned callable matches the slice's expected walk_fn signature:
    walk_fn(top_level, *, query_has_role, monitor_id, browser_correction_hook,
    score_hook) -> WalkResult. It injects a FakeAutomation and the supplied
    fake top-level element so the real walk_window runs its real interactive
    filter and invokes the hooks the coordinator wired.
    """

    def _walk_fn(top_level, **kwargs):
        # The finder forwards automation=None (no real COM root); replace it
        # with our FakeAutomation so the real walk_window runs against the
        # fake top-level element + fake array.
        kwargs.pop("automation", None)
        return uia_walker.walk_window(
            top_level_element,
            automation=FakeAutomation(),
            **kwargs,
        )

    return _walk_fn


def fixed_dpi_resolver(_monitor_id):
    return 96.0


def zero_monitor_resolver(_bounds):
    """Default fake: every match resolves to monitor 0 (matches fg() cursor)."""
    return 0


def make_finder(top_level_element, *, clock=None, **overrides):
    """Build an ElementFinder wired to a fake walk over the given fake tree."""
    captured = {"walk_kwargs": []}

    base_walk = make_real_walk_fn(top_level_element)

    def _recording_walk(top_level, **kwargs):
        captured["walk_kwargs"].append(kwargs)
        return base_walk(top_level, **kwargs)

    kwargs: dict[str, Any] = {
        "walk_fn": _recording_walk,
        "dpi_resolver": fixed_dpi_resolver,
        "monitor_resolver": zero_monitor_resolver,
        # Default to an empty enumerator so the focused-window composition tests
        # stay headless and never trigger the real-Win32 fall-back enumerator on
        # a not_found outcome. The dedicated fall-back tests inject their own.
        "window_enumerator": lambda: [],
    }
    if clock is not None:
        kwargs["clock"] = clock
    kwargs.update(overrides)
    finder = ElementFinder(**kwargs)
    return finder, captured


def fg(process_name="notepad.exe", hwnd=1000, pid=4321, creation_time=99):
    """Build a ForegroundContext for the finder to walk against."""
    return ForegroundContext(
        foreground_window=hwnd,
        foreground_pid=pid,
        foreground_process_name=process_name,
        foreground_window_creation_time=creation_time,
        cursor_at_walk=(500, 500),
        cursor_monitor_id=0,
    )


def el(name, *, control_type=UIA_BUTTON, role="button", rect=None,
       is_enabled=True, invoke_supported=True):
    return FakeCachedElement(
        name=name,
        control_type=control_type,
        localized_control_type=role,
        rect=rect or FakeRect(10, 20, 110, 70),
        is_enabled=is_enabled,
        invoke_supported=invoke_supported,
    )


# ---------------------------------------------------------------------------
# Snapshot store / replace / invalidate.
# ---------------------------------------------------------------------------

def test_find_returns_findresult_with_snapshot_and_summary():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, _ = make_finder(top)
    result = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                          fg())
    assert isinstance(result, FindResult)
    assert isinstance(result.snapshot, WalkSnapshot)
    assert isinstance(result.summary, WalkSnapshotSummary)
    assert result.outcome.outcome == "ok"
    assert result.snapshot.snapshot_id == result.summary.snapshot_id


def test_snapshot_stored_as_latest():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, _ = make_finder(top)
    assert finder.latest_snapshot() is None
    result = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                         fg())
    assert finder.latest_snapshot() is result.snapshot


def test_new_walk_replaces_snapshot():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, _ = make_finder(top)
    first = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                        fg())
    second = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                         fg())
    assert first.snapshot.snapshot_id != second.snapshot.snapshot_id
    assert finder.latest_snapshot() is second.snapshot


def test_get_snapshot_returns_none_after_ttl_expiry():
    top = FakeArrayTopLevel([el("Cancel")])
    now = {"t": 100.0}
    finder, _ = make_finder(top, clock=lambda: now["t"], snapshot_ttl_seconds=30)
    result = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                         fg())
    sid = result.snapshot.snapshot_id
    now["t"] = 129.0  # still inside TTL
    assert finder.get_snapshot(sid) is result.snapshot
    now["t"] = 131.0  # past 30s TTL
    assert finder.get_snapshot(sid) is None
    assert finder.latest_snapshot() is None


def test_get_snapshot_expires_exactly_at_ttl_boundary():
    # Finding 25.2: age == ttl fails closed. created_at=100, ttl=30 -> at
    # exactly t=130 the snapshot is expired (None) and the keepalive dropped.
    top = FakeArrayTopLevel([el("Cancel")])
    now = {"t": 100.0}
    finder, _ = make_finder(top, clock=lambda: now["t"], snapshot_ttl_seconds=30)
    result = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                         fg())
    sid = result.snapshot.snapshot_id
    now["t"] = 129.999  # just inside TTL -> still valid
    assert finder.get_snapshot(sid) is result.snapshot
    now["t"] = 130.0  # exactly at TTL boundary -> expired (fail closed)
    assert finder.get_snapshot(sid) is None
    assert finder.latest_snapshot() is None


def test_get_snapshot_invalidated_on_foreground_change():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, _ = make_finder(top)
    result = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                         fg(hwnd=1000))
    sid = result.snapshot.snapshot_id
    # Same foreground -> still valid.
    assert finder.get_snapshot(sid, current_foreground_window=1000) is result.snapshot
    # Different foreground HWND -> invalidated.
    assert finder.get_snapshot(sid, current_foreground_window=2222) is None
    assert finder.latest_snapshot() is None


# ---------------------------------------------------------------------------
# WalkSnapshotSummary projection.
# ---------------------------------------------------------------------------

def test_summary_carries_only_display_safe_primitives():
    top = FakeArrayTopLevel([el("Cancel"), el("Save")])
    finder, _ = make_finder(top)
    result = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                         fg())
    summary = result.summary
    assert summary.snapshot_id == result.snapshot.snapshot_id
    assert len(summary.items) == len(result.snapshot.matches)
    for item, match in zip(summary.items, result.snapshot.matches):
        assert isinstance(item, WalkSnapshotSummaryItem)
        assert item.item_id == match.item_id
        assert item.display_number == match.display_number
        assert item.name == match.name
        assert item.role == match.role
        assert item.bounds == match.bounds
        assert item.monitor_id == match.monitor_id
        # No control_ref on the summary item at all.
        assert not hasattr(item, "control_ref")


# ---------------------------------------------------------------------------
# Browser vs non-browser wiring.
# ---------------------------------------------------------------------------

def test_browser_process_walks_with_query_has_role_false():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, captured = make_finder(top)
    finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                fg(process_name="chrome.exe"))
    assert captured["walk_kwargs"], "walk_fn was never called"
    last = captured["walk_kwargs"][-1]
    assert last["query_has_role"] is False
    assert last["browser_correction_hook"] is not None


def test_non_browser_process_does_not_apply_dom_corrections():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, captured = make_finder(top)
    finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                fg(process_name="notepad.exe"))
    last = captured["walk_kwargs"][-1]
    assert last["browser_correction_hook"] is None
    # Non-browser keeps the standard role-driven interactive filter.
    assert last["query_has_role"] is True


# ---------------------------------------------------------------------------
# Per-request walk deadline (wh-9f3t.54.2): the finder computes ONE absolute
# deadline and threads it (plus its injected clock) into every walk_window call
# so a whole multi-walk request is bounded by one budget; a truncated walk
# fails closed to not_found.
# ---------------------------------------------------------------------------

def test_finder_derives_absolute_deadline_from_walk_deadline_ms():
    """With no explicit deadline, find() anchors ONE absolute deadline from the
    configured walk_deadline_ms via the injected clock and threads it into the
    walk."""
    top = FakeArrayTopLevel([el("Cancel")])
    finder, captured = make_finder(top, clock=lambda: 0.0, walk_deadline_ms=2500)
    finder.find(ElementQuery("cancel", "button", None, None, "cancel"), fg())
    last = captured["walk_kwargs"][-1]
    # 0.0 + 2500/1000 == 2.5 absolute monotonic timestamp.
    assert last["deadline"] == 2.5
    # The same injected clock seam drives the walk deadline.
    assert last["clock"] is not None
    # The old per-call duration kwarg is gone.
    assert "walk_deadline_ms" not in last


def test_finder_caller_supplied_deadline_threaded_unchanged():
    """An explicit (dequeue-anchored) absolute deadline is passed unchanged to
    the walk, taking precedence over the configured walk_deadline_ms."""
    top = FakeArrayTopLevel([el("Cancel")])
    finder, captured = make_finder(top, clock=lambda: 0.0, walk_deadline_ms=2500)
    finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg(),
        deadline=7.5,
    )
    last = captured["walk_kwargs"][-1]
    assert last["deadline"] == 7.5


def test_finder_default_deadline_is_none_no_bound():
    """When walk_deadline_ms is not configured and no deadline is passed, the
    finder forwards None so the walk processes the whole subtree."""
    top = FakeArrayTopLevel([el("Cancel")])
    finder, captured = make_finder(top)
    finder.find(ElementQuery("cancel", "button", None, None, "cancel"), fg())
    last = captured["walk_kwargs"][-1]
    assert last["deadline"] is None


def test_finder_truncated_walk_fails_closed_to_not_found():
    """End-to-end FINDING 2: a deadline already in the past makes walk_window
    fail closed (deadline_truncated=True, empty matches), and the finder maps
    that to not_found WITHOUT scoring -- never a wrong "ok" winner."""
    elements = [el(f"Cancel{i}") for i in range(10)]
    top = FakeArrayTopLevel(elements)
    # An explicit absolute deadline already in the past; the clock reads 5.0.
    finder, captured = make_finder(top, clock=lambda: 5.0)
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg(),
        deadline=0.1,
    )
    last = captured["walk_kwargs"][-1]
    assert last["deadline"] == 0.1
    # Fail closed: not_found with the deadline reason, zero matches scored.
    assert result.outcome.outcome == "not_found"
    assert result.outcome.reason == "walk_deadline_exceeded"
    assert result.snapshot.matches == []


def test_non_browser_no_role_walks_query_has_role_false():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, captured = make_finder(top)
    finder.find(ElementQuery("cancel", None, None, None, "cancel"),
                fg(process_name="notepad.exe"))
    last = captured["walk_kwargs"][-1]
    assert last["query_has_role"] is False
    assert last["browser_correction_hook"] is None


def test_score_hook_filters_to_eligible_and_sets_score():
    # "Cancel" exact -> eligible & scored; "Unrelated" -> ineligible, dropped.
    top = FakeArrayTopLevel([el("Cancel"), el("Unrelated")])
    finder, _ = make_finder(top)
    result = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                         fg())
    names = [m.name for m in result.snapshot.matches]
    assert names == ["Cancel"]
    assert result.snapshot.matches[0].is_eligible is True
    assert result.snapshot.matches[0].score > 0.0


def test_find_surfaces_execution_failed_disabled():
    # The only match is an exact-name disabled control -> execution_failed:disabled.
    top = FakeArrayTopLevel([el("Cancel", is_enabled=False, invoke_supported=False)])
    finder, _ = make_finder(top)
    result = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                         fg())
    assert result.outcome.outcome == "execution_failed"
    assert result.outcome.reason == "disabled"
    assert result.outcome.winner is not None
    assert result.outcome.winner.name == "Cancel"


# ---------------------------------------------------------------------------
# COM keepalive: the stored snapshot's control_ref survives a gc.collect().
# ---------------------------------------------------------------------------

def test_keepalive_survives_gc_between_walk_and_use():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, _ = make_finder(top)
    result = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                         fg())
    sid = result.snapshot.snapshot_id
    del result
    gc.collect()
    snap = finder.get_snapshot(sid)
    assert snap is not None
    # control_ref still reachable (the WalkResult keepalive chain held it).
    assert snap.matches[0].control_ref is not None


# ---------------------------------------------------------------------------
# Multi-snapshot store (wh-n29v.33): self._stored is a dict keyed by
# snapshot_id with bounded LRU eviction (default capacity 4, never evicting a
# pinned entry), TTL eviction across ALL snapshots on get_snapshot (TTL DOES
# evict pinned -- stale-pin cleanup), an explicit pin()/unpin() mechanism, and
# per-snapshot COM keepalive isolation.
# ---------------------------------------------------------------------------

# Each find() over the same FakeArrayTopLevel re-walks the same fake tree and
# gets a fresh snapshot_id. To give each stored snapshot its OWN COM keepalive
# chain (so eviction isolation can be observed), the multi-snapshot finder walks
# the DISTINCT fake top-level element handed in via ForegroundContext.top_level
# (find() forwards foreground.top_level to walk_fn as the first arg).

def fg_top(top_level, *, process_name="notepad.exe", hwnd=1000, pid=4321,
           creation_time=99):
    return ForegroundContext(
        foreground_window=hwnd,
        foreground_pid=pid,
        foreground_process_name=process_name,
        foreground_window_creation_time=creation_time,
        cursor_at_walk=(500, 500),
        cursor_monitor_id=0,
        top_level=top_level,
    )


def _multi_walk_fn(top_level, **kwargs):
    """walk_fn that drives the REAL walk_window over the PASSED top-level token.

    Unlike make_real_walk_fn (which captures a fixed tree), this honours the
    per-find foreground.top_level so each find() walks its own FakeArrayTopLevel
    and gets a distinct control_ref object -- the prerequisite for observing
    per-snapshot COM keepalive isolation on eviction.
    """
    kwargs.pop("automation", None)
    return uia_walker.walk_window(top_level, automation=FakeAutomation(), **kwargs)


def make_multi_finder(**overrides):
    """Build an ElementFinder whose walk_fn honours foreground.top_level."""
    kwargs: dict[str, Any] = {
        "walk_fn": _multi_walk_fn,
        "dpi_resolver": fixed_dpi_resolver,
        "monitor_resolver": zero_monitor_resolver,
        "window_enumerator": lambda: [],
    }
    kwargs.update(overrides)
    return ElementFinder(**kwargs)


def _store_walk(finder, name="Cancel"):
    """Drive one find() over a FRESH fake tree; return its FindResult.

    A new FakeArrayTopLevel per call means each stored snapshot owns a distinct
    control_ref object, so per-snapshot keepalive isolation is observable.
    Requires a finder built by make_multi_finder (its walk_fn honours top_level).
    """
    top = FakeArrayTopLevel([el(name)])
    return finder.find(
        ElementQuery(name.lower(), "button", None, None, name.lower()),
        fg_top(top),
    )


def test_store_holds_up_to_capacity_snapshots():
    finder = make_multi_finder(snapshot_store_capacity=4)
    sids = [_store_walk(finder).snapshot.snapshot_id for _ in range(4)]
    # All four coexist (capacity 4): each is still retrievable by id.
    for sid in sids:
        assert finder.get_snapshot(sid) is not None


def test_store_evicts_oldest_unpinned_beyond_capacity():
    finder = make_multi_finder(snapshot_store_capacity=3)
    sids = [_store_walk(finder).snapshot.snapshot_id for _ in range(5)]
    # Capacity 3: the two oldest unpinned snapshots are evicted; size stays <= 3.
    assert finder.get_snapshot(sids[0]) is None
    assert finder.get_snapshot(sids[1]) is None
    assert finder.get_snapshot(sids[2]) is not None
    assert finder.get_snapshot(sids[3]) is not None
    assert finder.get_snapshot(sids[4]) is not None


def test_default_snapshot_store_capacity_is_4():
    # No explicit capacity -> default 4. Fifth walk evicts the oldest.
    finder = make_multi_finder()
    sids = [_store_walk(finder).snapshot.snapshot_id for _ in range(5)]
    assert finder.get_snapshot(sids[0]) is None
    for sid in sids[1:]:
        assert finder.get_snapshot(sid) is not None


def test_pinned_snapshot_survives_lru_eviction():
    finder = make_multi_finder(snapshot_store_capacity=3)
    oldest = _store_walk(finder).snapshot.snapshot_id
    assert finder.pin(oldest) is True
    # Fill well past capacity with younger unpinned snapshots.
    younger = [_store_walk(finder).snapshot.snapshot_id for _ in range(5)]
    # The pinned oldest survives even though it is the least-recently-used.
    assert finder.get_snapshot(oldest) is not None
    # A younger unpinned snapshot was evicted instead.
    assert finder.get_snapshot(younger[0]) is None


def test_unpin_re_enables_lru_eviction():
    finder = make_multi_finder(snapshot_store_capacity=2)
    oldest = _store_walk(finder).snapshot.snapshot_id
    assert finder.pin(oldest) is True
    # Two younger walks at capacity 2: with oldest pinned, LRU evicts the
    # younger unpinned ones instead, so oldest survives while pinned.
    second = _store_walk(finder).snapshot.snapshot_id
    _store_walk(finder)  # evicts `second` (younger unpinned), oldest stays
    assert finder.get_snapshot(second) is None      # younger one evicted
    # Unpin oldest WITHOUT touching it via get_snapshot (a get_snapshot hit
    # would mark it most-recently-used and skew the LRU order this test checks).
    assert finder.unpin(oldest) is True
    # oldest is now the least-recently-used UNPINNED entry; the next walk evicts
    # it (it is no longer protected by the pin).
    _store_walk(finder)
    assert finder.get_snapshot(oldest) is None


def test_pin_unpin_return_false_for_unknown_id():
    finder = make_multi_finder()
    assert finder.pin("walk-999") is False
    assert finder.unpin("walk-999") is False
    sid = _store_walk(finder).snapshot.snapshot_id
    assert finder.pin(sid) is True
    assert finder.unpin(sid) is True


def test_fresh_find_survives_when_all_existing_slots_pinned():
    # reviewer_2 finding 36.1: when every existing slot is pinned and a fresh
    # unpinned walk pushes the store over capacity, the just-produced snapshot
    # must stay retrievable. LRU never evicts a pinned entry, and it must not
    # evict the brand-new entry either -- the four blocked Input handlers do
    # find() then pin(its id), so a find() that immediately discards its own
    # result would make pin() fail. The store sits one over capacity (the pins
    # plus the fresh entry); TTL bounds the pins.
    finder = make_multi_finder(snapshot_store_capacity=2)
    first = _store_walk(finder).snapshot.snapshot_id
    second = _store_walk(finder).snapshot.snapshot_id  # store now at capacity 2
    assert finder.pin(first) is True
    assert finder.pin(second) is True
    # A third unpinned walk: both existing slots are pinned (not LRU-evictable),
    # so the fresh newcomer is protected from its own insert's eviction pass.
    extra = _store_walk(finder).snapshot.snapshot_id
    assert finder.get_snapshot(first) is not None
    assert finder.get_snapshot(second) is not None
    assert finder.get_snapshot(extra) is not None  # newcomer retained...
    assert finder.pin(extra) is True               # ...and pinnable
    assert len(finder._stored) == 3                # one over capacity, TTL-bounded


def test_fresh_find_survives_when_all_pinned_at_capacity_1():
    # reviewer_2 finding 36.1: capacity=1 (the floor after the max(1, ...) clamp)
    # with a pinned entry. A fresh find() must still be retrievable and pinnable,
    # not silently discarded -- otherwise a single pin makes the store write-once
    # until the pin's TTL elapses.
    finder = make_multi_finder(snapshot_store_capacity=1)
    pinned = _store_walk(finder).snapshot.snapshot_id
    assert finder.pin(pinned) is True
    fresh = _store_walk(finder).snapshot.snapshot_id
    assert finder.get_snapshot(fresh) is not None
    assert finder.pin(fresh) is True
    assert finder.get_snapshot(pinned) is not None  # pinned entry also retained


def test_fresh_find_overage_is_reclaimed_by_next_find():
    # reviewer_2 finding 36.1: the just-inserted entry is protected from its OWN
    # eviction pass only. On the NEXT find() it is an older unpinned entry again
    # and becomes LRU-evictable, so the all-pinned overage never grows beyond one
    # extra slot.
    finder = make_multi_finder(snapshot_store_capacity=1)
    pinned = _store_walk(finder).snapshot.snapshot_id
    assert finder.pin(pinned) is True
    first_fresh = _store_walk(finder).snapshot.snapshot_id   # protected this pass
    assert finder.get_snapshot(first_fresh) is not None
    second_fresh = _store_walk(finder).snapshot.snapshot_id  # first_fresh now older
    assert finder.get_snapshot(first_fresh) is None          # reclaimed
    assert finder.get_snapshot(second_fresh) is not None     # newly protected
    assert finder.get_snapshot(pinned) is not None           # pin still held


# ---------------------------------------------------------------------------
# Direct ElementFinder.overlay_walk coverage (reviewer_0 wh-n29v.38.3).
#
# The wh-n29v.37 slice exercised overlay_walk only INDIRECTLY through the
# Input-side start_overlay_walk handler test, which used a non-browser tree.
# That left the browser DOM-fold renumbering path and the multi-snapshot
# store-reuse invariants unverified -- the exact gap that let reviewer_0
# finding 38.1 (non-contiguous display_numbers after a browser fold) ship.
# These drive overlay_walk directly.
# ---------------------------------------------------------------------------

def _overlay_walk(finder, top, *, process_name="notepad.exe"):
    """Drive one overlay_walk over a FRESH fake tree; return its OverlayWalkResult.

    Mirrors ``_store_walk`` for the overlay path: ``make_multi_finder``'s
    walk_fn honours ``foreground.top_level`` so each call walks its own
    FakeArrayTopLevel and owns a distinct control_ref object.
    """
    return finder.overlay_walk(fg_top(top, process_name=process_name))


def test_overlay_walk_browser_renumbers_contiguously():
    # reviewer_0 finding 38.1: the walker assigns display_number 1..N BEFORE the
    # browser DOM-fold drops scaffolding controls, so a kept survivor keeps its
    # original (now-gapped) number. The full Chromium shape walks 6 controls
    # (query_has_role=False), and the three fold rules drop the GROUP (#1), the
    # LISTITEM (#3), and the HEADING (#6), leaving survivors originally numbered
    # 2, 4, 5. overlay_walk MUST renumber the survivors 1..K contiguously (and
    # regenerate item_id to match) so the Logic resolver's exact display_number
    # match resolves "click 1".
    finder = make_multi_finder()
    top = FakeArrayTopLevel(_chromium_full_shape_tree())
    result = _overlay_walk(finder, top, process_name="chrome.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    items = result.summary.items
    # Three survivors, contiguous 1..3 (NOT the pre-fold 2, 4, 5).
    assert [i.display_number for i in items] == [1, 2, 3]
    assert [i.item_id for i in items] == ["uia-1", "uia-2", "uia-3"]
    # The surviving controls (group/list-item/heading folded away).
    assert [i.name for i in items] == ["Cancel", "Sign in", "Welcome"]
    assert [i.role for i in items] == ["text", "hyperlink", "hyperlink"]
    # The stored snapshot.matches MUST agree with the summary on BOTH keys:
    # the Logic resolver keys on display_number, click_snapshot_item on item_id.
    assert result.snapshot is not None
    matches = result.snapshot.matches
    assert [m.display_number for m in matches] == [1, 2, 3]
    assert [m.item_id for m in matches] == ["uia-1", "uia-2", "uia-3"]


def test_overlay_walk_non_browser_contiguous_idempotent():
    # The renumber is idempotent for the already-contiguous non-browser case:
    # no fold runs, the walker's 1..K is already contiguous, and renumbering
    # produces the same numbers and item_ids. Distinct side-by-side rects:
    # same-rect fakes would read as one visual control and collapse to one
    # badge (wh-overlay-nested-dupes).
    finder = make_multi_finder()
    top = FakeArrayTopLevel([
        el("Save", rect=FakeRect(10, 20, 110, 70)),
        el("Open", rect=FakeRect(120, 20, 220, 70)),
        el("Cancel", rect=FakeRect(230, 20, 330, 70)),
    ])
    result = _overlay_walk(finder, top, process_name="notepad.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    items = result.summary.items
    assert [i.display_number for i in items] == [1, 2, 3]
    assert [i.item_id for i in items] == ["uia-1", "uia-2", "uia-3"]
    assert [i.name for i in items] == ["Save", "Open", "Cancel"]


def test_overlay_walk_ok_with_targets_stores_retrievable_snapshot():
    # ok-with-targets: the snapshot is stored and retrievable by id, and is the
    # newest snapshot.
    finder = make_multi_finder()
    top = FakeArrayTopLevel([el("Save"), el("Open")])
    result = _overlay_walk(finder, top)

    assert result.outcome == "ok"
    assert result.snapshot is not None
    sid = result.snapshot.snapshot_id
    assert finder.get_snapshot(sid) is result.snapshot
    assert finder.latest_snapshot() is result.snapshot
    assert finder.latest_summary() is result.summary


def test_overlay_walk_no_targets_empty_summary_still_stored():
    # A focused window with no interactive controls -> no_targets with an
    # EMPTY-items summary, but the snapshot is STILL stored (design v4: a valid
    # painted-but-no-badges state needs a snapshot id for a later paint/pin).
    finder = make_multi_finder()
    top = FakeArrayTopLevel([el("label", control_type=UIA_TEXT, role="text")])
    result = _overlay_walk(finder, top, process_name="notepad.exe")

    assert result.outcome == "no_targets"
    assert result.summary is not None
    assert result.summary.items == []
    assert result.snapshot is not None
    sid = result.snapshot.snapshot_id
    assert finder.get_snapshot(sid) is result.snapshot


def test_overlay_walk_stores_nothing_on_deadline_truncation():
    # A walk the deadline cut short -> execution_failed with
    # reason=walk_deadline_exceeded and NO snapshot stored (a partial subtree
    # must never become a paintable overlay).
    finder = make_multi_finder(walk_deadline_ms=0, clock=lambda: 1000.0)
    top = FakeArrayTopLevel([el("Save"), el("Open")])
    result = _overlay_walk(finder, top)

    assert result.outcome == "execution_failed"
    assert result.reason == "walk_deadline_exceeded"
    assert result.snapshot is None
    assert result.summary is None
    assert finder.latest_snapshot() is None


def test_overlay_walk_snapshot_is_pinnable():
    # The Logic overlay state machine pins the overlay snapshot via the
    # pin_snapshot IPC action; pin() must succeed on an overlay-produced id.
    finder = make_multi_finder()
    top = FakeArrayTopLevel([el("Save")])
    snapshot = _overlay_walk(finder, top).snapshot
    assert snapshot is not None
    sid = snapshot.snapshot_id
    assert finder.pin(sid) is True
    assert finder.unpin(sid) is True


def test_overlay_walk_sweeps_ttl_expired_entries_on_insert():
    # overlay_walk runs _sweep_ttl before inserting (same store-maintenance
    # sequence as find()): an entry whose age has reached the TTL is dropped
    # from the store on the next overlay_walk, not left as a COM-keepalive leak.
    now = {"t": 100.0}
    finder = make_multi_finder(clock=lambda: now["t"], snapshot_ttl_seconds=30)
    top1 = FakeArrayTopLevel([el("Save")])
    snapshot1 = _overlay_walk(finder, top1).snapshot
    assert snapshot1 is not None
    sid1 = snapshot1.snapshot_id
    assert sid1 in finder._stored
    now["t"] = 131.0  # past the 30s TTL of sid1
    top2 = FakeArrayTopLevel([el("Open")])
    _overlay_walk(finder, top2)
    # The insert's TTL sweep removed sid1 from the store outright (not merely
    # masked by get_snapshot's own expiry check).
    assert sid1 not in finder._stored


def test_overlay_walk_self_eviction_protection_at_capacity_1():
    # reviewer_0 finding 38.3 / reviewer_2 finding 36.1 for the overlay path:
    # at capacity=1 with the single slot pinned, a fresh overlay_walk must stay
    # retrievable and pinnable -- the four blocked Input handlers do
    # overlay_walk then pin(its id), so a walk that immediately discarded its
    # own result would make pin() fail.
    finder = make_multi_finder(snapshot_store_capacity=1)
    pinned = _store_walk(finder).snapshot.snapshot_id
    assert finder.pin(pinned) is True
    top = FakeArrayTopLevel([el("Save")])
    fresh_snapshot = _overlay_walk(finder, top).snapshot
    assert fresh_snapshot is not None
    fresh = fresh_snapshot.snapshot_id
    assert finder.get_snapshot(fresh) is not None
    assert finder.pin(fresh) is True
    assert finder.get_snapshot(pinned) is not None  # pinned entry also retained


def test_overlay_walk_browser_drops_non_interactive_scaffolding_survivor():
    # reviewer_1 finding 39.1: a browser overlay walks with query_has_role=False
    # so the fold rules can see Text/Group/Heading scaffolding. A GroupControl
    # wrapping a ListItem -> Hyperlink does NOT match any fold rule (after the
    # list-item folds away, the Group's sole descendant is a Hyperlink, not a
    # same-name Text), so the non-interactive Group SURVIVES the fold. It must
    # NOT get a numbered badge: "click N" on it would resolve to a Group item_id
    # the later click_snapshot_item cannot invoke. overlay_walk drops any
    # post-fold survivor that is neither an interactive control type nor
    # InvokePattern-capable, keeping only the genuinely clickable Hyperlink, and
    # the survivors stay contiguous 1..K.
    finder = make_multi_finder()
    top = FakeArrayTopLevel(_chromium_group_wrapping_listitem_tree())
    result = _overlay_walk(finder, top, process_name="chrome.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    items = result.summary.items
    # Only the clickable Hyperlink is numbered; the Group wrapper got NO badge.
    assert [i.name for i in items] == ["Sign in"]
    assert [i.role for i in items] == ["hyperlink"]
    assert [i.display_number for i in items] == [1]
    assert [i.item_id for i in items] == ["uia-1"]
    # The stored snapshot.matches agree with the summary on BOTH keys.
    assert result.snapshot is not None
    matches = result.snapshot.matches
    assert [m.name for m in matches] == ["Sign in"]
    assert [m.display_number for m in matches] == [1]
    assert [m.item_id for m in matches] == ["uia-1"]


def test_overlay_walk_browser_keeps_interactive_type_without_invoke():
    # The clickable filter is a UNION: an interactive control type is numbered
    # even when it exposes no InvokePattern (a CheckBox uses TogglePattern; a
    # Slider/ComboBox/TreeItem use other patterns). An invoke-only filter would
    # wrongly drop these. Here the CheckBox (interactive type, invoke=False) keeps
    # its badge while the decorative Group (non-interactive, invoke=False) does
    # not, and the kept set stays contiguous 1..K.
    finder = make_multi_finder()
    top = FakeArrayTopLevel(_chromium_checkbox_and_pane_tree())
    result = _overlay_walk(finder, top, process_name="chrome.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    items = result.summary.items
    assert [i.name for i in items] == ["Remember me"]
    assert [i.role for i in items] == ["check box"]
    assert [i.display_number for i in items] == [1]
    assert [i.item_id for i in items] == ["uia-1"]


def test_per_snapshot_keepalive_isolation_on_eviction():
    import weakref

    finder = make_multi_finder(snapshot_store_capacity=2)
    first = _store_walk(finder, "First")
    second = _store_walk(finder, "Second")
    ref_first = weakref.ref(first.snapshot.matches[0].control_ref)
    ref_second = weakref.ref(second.snapshot.matches[0].control_ref)
    sid_first = first.snapshot.snapshot_id
    # Drop the FindResults so only the store pins the keepalives.
    del first, second
    gc.collect()
    assert ref_first() is not None and ref_second() is not None
    # A third walk at capacity 2 evicts the oldest (first), releasing ONLY its
    # WalkResult keepalive; the second snapshot's control_ref must survive.
    _store_walk(finder, "Third")
    gc.collect()
    assert finder.get_snapshot(sid_first) is None
    assert ref_first() is None       # first's keepalive released by eviction
    assert ref_second() is not None  # second's keepalive isolated, still held


def test_get_snapshot_ttl_evicts_all_snapshots_including_pinned():
    # TTL eviction runs across ALL snapshots on every get_snapshot, and TTL
    # evicts a PINNED snapshot too (stale-pin cleanup hard boundary).
    now = {"t": 100.0}
    finder = make_multi_finder(
        clock=lambda: now["t"],
        snapshot_ttl_seconds=30,
        snapshot_store_capacity=4,
    )
    pinned = _store_walk(finder).snapshot.snapshot_id
    other = _store_walk(finder).snapshot.snapshot_id
    assert finder.pin(pinned) is True
    now["t"] = 131.0  # past the 30s TTL for both snapshots
    # get_snapshot of the pinned id returns None (TTL beats pin)...
    assert finder.get_snapshot(pinned) is None
    # ...and the cross-snapshot TTL sweep also dropped the OTHER expired entry.
    assert finder.get_snapshot(other) is None


def test_get_snapshot_ttl_sweep_does_not_drop_fresh_snapshots():
    # The cross-snapshot TTL sweep on get_snapshot drops only EXPIRED entries;
    # a still-fresh snapshot survives a get_snapshot for an expired sibling.
    now = {"t": 100.0}
    finder = make_multi_finder(
        clock=lambda: now["t"],
        snapshot_ttl_seconds=30,
        snapshot_store_capacity=4,
    )
    old = _store_walk(finder).snapshot.snapshot_id
    now["t"] = 120.0
    fresh = _store_walk(finder).snapshot.snapshot_id  # created_at=120
    now["t"] = 131.0  # old (created 100) expired; fresh (created 120) age 11 ok
    assert finder.get_snapshot(old) is None
    assert finder.get_snapshot(fresh) is not None


# ---------------------------------------------------------------------------
# find()-path TTL sweep + pinned-count warning (reviewer_0 round 1 findings
# 34.1 / 34.2 / 34.3 / 34.4). Before this fix _sweep_ttl ran ONLY in
# get_snapshot, so a stale pin (lost unpin) leaked its COM keepalive for the
# life of the Input process whenever the overlay get_snapshot path was never
# exercised again. find() -- the path that keeps running after the overlay is
# dismissed -- now sweeps TTL before inserting, so it reclaims expired entries
# (pinned or not) and bounds the store on the path that actually grows it.
# ---------------------------------------------------------------------------

def test_find_sweeps_ttl_and_reclaims_stale_pinned_entry_without_get_snapshot():
    # Finding 34.1: a pinned snapshot whose unpin was lost must be reclaimed by
    # the next find() once its TTL elapses, WITHOUT any get_snapshot call. The
    # store is inspected directly so no get_snapshot sweep can be credited with
    # the reclaim -- this proves find() itself swept.
    now = {"t": 100.0}
    finder = make_multi_finder(
        clock=lambda: now["t"],
        snapshot_ttl_seconds=30,
        snapshot_store_capacity=4,
    )
    stale = _store_walk(finder).snapshot.snapshot_id  # created_at=100
    assert finder.pin(stale) is True
    now["t"] = 131.0  # past the 30s TTL -- the pinned entry is now stale
    _store_walk(finder)  # a later find(): its TTL sweep reclaims the stale pin
    assert stale not in finder._stored


def test_find_releases_expired_siblings_keepalive_via_sweep():
    # Finding 34.1 (COM-lifetime proof): the find()-path sweep does not just
    # remove the dict entry, it drops the expired entry's WalkResult keepalive,
    # releasing its COM control_ref proxies -- proven with a weakref.
    import weakref

    now = {"t": 100.0}
    finder = make_multi_finder(
        clock=lambda: now["t"],
        snapshot_ttl_seconds=30,
        snapshot_store_capacity=4,
    )
    sib = _store_walk(finder, "Sibling")  # created_at=100
    ref_sib = weakref.ref(sib.snapshot.matches[0].control_ref)
    del sib  # only the store now pins the keepalive
    gc.collect()
    assert ref_sib() is not None
    now["t"] = 131.0  # the stored sibling is now expired
    _store_walk(finder, "Newcomer")  # find() sweeps the expired sibling
    gc.collect()
    assert ref_sib() is None  # sibling's COM keepalive released by find()'s sweep


def test_two_pins_do_not_warn_and_pinned_growth_is_ttl_bounded(caplog):
    # Finding 34.5: the overlay state machine legitimately holds TWO pinned
    # snapshots transiently across a refresh paint leg -- _refresh_build_ok pins
    # the new snapshot but DEFERS the prior unpin until a successful paint-ack
    # (click_overlay_state.py). So the store must NOT warn at pinned>1: that
    # would cry wolf on every normal refresh and bury a genuine contract break.
    # The store lacks the session/generation context to tell a legitimate
    # transient two-pin from a real break, so contract-break detection belongs
    # in the Logic state machine, not here.
    #
    # Finding 34.2 core (still upheld): pinned entries are bounded by TTL, not
    # LRU (NO hard pinned-eviction ceiling), and a later find() past TTL reclaims
    # the expired pinned entries so the store is bounded on the find() path too.
    now = {"t": 100.0}
    finder = make_multi_finder(
        clock=lambda: now["t"],
        snapshot_ttl_seconds=30,
        snapshot_store_capacity=2,
    )
    with caplog.at_level(logging.WARNING, logger="ui.element_finder"):
        first = _store_walk(finder).snapshot.snapshot_id
        assert finder.pin(first) is True
        second = _store_walk(finder).snapshot.snapshot_id
        assert finder.pin(second) is True  # legitimate transient two-pin (refresh)
    # Two pinned entries coexist at capacity 2 (pins are not LRU-evictable)...
    assert len(finder._stored) == 2
    # ...and the store logged NO warning for the legitimate two-pin case (34.5).
    assert not any(
        "pinned" in record.getMessage().lower() for record in caplog.records
    ), "pin() must not warn on the legitimate transient two-pin refresh case"
    # Advance past TTL; one more find() sweeps BOTH expired pinned entries, so
    # the store falls back to just the fresh newcomer (TTL bounds the pins on
    # the find() path, not only on get_snapshot).
    now["t"] = 131.0
    _store_walk(finder)
    assert len(finder._stored) == 1


def test_get_snapshot_releases_expired_siblings_keepalive():
    # Finding 34.3: get_snapshot(B) sweeps TTL across ALL snapshots first, so an
    # unrelated expired sibling A's WalkResult keepalive is dropped as a side
    # effect of merely asking about B. This documents the cross-snapshot side
    # effect with a weakref proof (the existing TTL-evicts-all test only checks
    # the None return, not the keepalive release).
    import weakref

    now = {"t": 100.0}
    finder = make_multi_finder(
        clock=lambda: now["t"],
        snapshot_ttl_seconds=30,
        snapshot_store_capacity=4,
    )
    sib = _store_walk(finder, "Sibling")  # created_at=100
    target = _store_walk(finder, "Target")  # created_at=100
    ref_sib = weakref.ref(sib.snapshot.matches[0].control_ref)
    sib_id = sib.snapshot.snapshot_id
    target_id = target.snapshot.snapshot_id
    del sib, target  # only the store pins the keepalives
    gc.collect()
    assert ref_sib() is not None
    now["t"] = 131.0  # both expired
    # Asking about TARGET sweeps ALL snapshots, dropping the SIBLING as well.
    assert finder.get_snapshot(target_id) is None
    gc.collect()
    assert sib_id not in finder._stored
    assert ref_sib() is None  # sibling's keepalive released by the cross-snapshot sweep


def test_pinning_does_not_refresh_ttl():
    now = {"t": 100.0}
    finder = make_multi_finder(
        clock=lambda: now["t"],
        snapshot_ttl_seconds=30,
    )
    sid = _store_walk(finder).snapshot.snapshot_id  # created_at=100
    now["t"] = 125.0
    assert finder.pin(sid) is True  # pin LATE -- must not reset created_at
    now["t"] = 131.0  # age from the ORIGINAL created_at (100) is 31 > 30
    assert finder.get_snapshot(sid) is None  # expired despite the late pin


def test_get_snapshot_unknown_id_returns_none():
    finder = make_multi_finder()
    _store_walk(finder)
    assert finder.get_snapshot("walk-does-not-exist") is None


def test_latest_snapshot_is_newest_added_not_lru_accessed():
    # latest_snapshot()/latest_summary() track the most-recently-ADDED snapshot,
    # NOT the most-recently-LRU-accessed one. Accessing an older snapshot via
    # get_snapshot (which marks it MRU for LRU purposes) must NOT change which
    # snapshot is reported as the newest.
    finder = make_multi_finder(snapshot_store_capacity=4)
    first = _store_walk(finder)
    newest = _store_walk(finder)
    assert finder.latest_snapshot() is newest.snapshot
    assert finder.latest_summary() is newest.summary
    # Touch the older snapshot (marks it MRU for LRU); newest is unchanged.
    assert finder.get_snapshot(first.snapshot.snapshot_id) is first.snapshot
    assert finder.latest_snapshot() is newest.snapshot
    assert finder.latest_summary() is newest.summary


def test_lru_access_protects_recently_used_snapshot_from_eviction():
    # get_snapshot marks a snapshot most-recently-used, so a touched older
    # snapshot survives LRU pressure that would otherwise evict it.
    finder = make_multi_finder(snapshot_store_capacity=2)
    a = _store_walk(finder).snapshot.snapshot_id
    b = _store_walk(finder).snapshot.snapshot_id
    # Touch A so it becomes MRU; B is now the least-recently-used.
    assert finder.get_snapshot(a) is not None
    c = _store_walk(finder).snapshot.snapshot_id  # capacity 2 -> evict LRU (B)
    assert finder.get_snapshot(a) is not None  # touched -> survived
    assert finder.get_snapshot(b) is None      # least-recently-used -> evicted
    assert finder.get_snapshot(c) is not None


def test_invalidate_clears_entire_multi_snapshot_store():
    finder = make_multi_finder(snapshot_store_capacity=4)
    sids = [_store_walk(finder).snapshot.snapshot_id for _ in range(3)]
    finder.invalidate()
    for sid in sids:
        assert finder.get_snapshot(sid) is None
    assert finder.latest_snapshot() is None
    assert finder.latest_summary() is None


def test_foreground_identity_check_is_per_requested_snapshot():
    # The foreground-identity invalidation applies to the REQUESTED snapshot
    # only, and drops just that snapshot -- other stored snapshots survive.
    finder = make_multi_finder(snapshot_store_capacity=4)
    top_a = FakeArrayTopLevel([el("Cancel")])
    a = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                    fg_top(top_a, hwnd=1000))
    top_b = FakeArrayTopLevel([el("Save")])
    b = finder.find(ElementQuery("save", "button", None, None, "save"),
                    fg_top(top_b, hwnd=2000))
    # A foreground-HWND mismatch against A drops ONLY A.
    assert finder.get_snapshot(
        a.snapshot.snapshot_id, current_foreground_window=9999
    ) is None
    # B is untouched and still retrievable with its matching identity.
    assert finder.get_snapshot(
        b.snapshot.snapshot_id, current_foreground_window=2000
    ) is b.snapshot


# ---------------------------------------------------------------------------
# refresh_snapshot_ttl (wh-overlay-snapshot-keepalive): the Logic-side 15s
# keepalive slides the Input store's TTL for a still-visible pinned snapshot,
# so a numbered overlay left on screen past the TTL stays clickable. Distinct
# from pin() (which blocks LRU only and deliberately does NOT slide TTL).
# ---------------------------------------------------------------------------
def test_refresh_snapshot_ttl_slides_expiry_window():
    now = {"t": 100.0}
    finder = make_multi_finder(clock=lambda: now["t"], snapshot_ttl_seconds=30)
    sid = _store_walk(finder).snapshot.snapshot_id  # created_at=100
    now["t"] = 125.0
    assert finder.refresh_snapshot_ttl(sid) is True  # slide the window to 125
    # Age from the ORIGINAL created_at (100) is 50 > 30, but from the refresh
    # (125) it is 25 < 30, so the snapshot survives.
    now["t"] = 150.0
    assert finder.get_snapshot(sid) is not None


def test_refresh_snapshot_ttl_unknown_id_returns_false():
    finder = make_multi_finder()
    _store_walk(finder)
    assert finder.refresh_snapshot_ttl("walk-does-not-exist") is False


def test_refresh_snapshot_ttl_does_not_revive_already_expired():
    # A refresh that arrives AFTER the snapshot has already aged past the TTL
    # must not revive it (the age>=ttl boundary fails closed). The keepalive
    # cadence (15s) prevents this in practice; this guards the pathological
    # "Logic stalled past the TTL" case.
    now = {"t": 100.0}
    finder = make_multi_finder(clock=lambda: now["t"], snapshot_ttl_seconds=30)
    sid = _store_walk(finder).snapshot.snapshot_id  # created_at=100
    now["t"] = 131.0  # age 31 >= 30, already expired (no sweep has run yet)
    assert finder.refresh_snapshot_ttl(sid) is False
    assert finder.get_snapshot(sid) is None


def test_refresh_snapshot_ttl_is_independent_of_pin():
    # refresh slides TTL; pin does not. The two are orthogonal: a refreshed but
    # UNPINNED snapshot still survives TTL, and a pinned but un-refreshed one
    # still expires (the test_pinning_does_not_refresh_ttl invariant holds).
    now = {"t": 100.0}
    finder = make_multi_finder(clock=lambda: now["t"], snapshot_ttl_seconds=30)
    sid = _store_walk(finder).snapshot.snapshot_id
    now["t"] = 120.0
    assert finder.refresh_snapshot_ttl(sid) is True  # no pin() call
    now["t"] = 145.0  # 25s since the refresh
    assert finder.get_snapshot(sid) is not None


# ---------------------------------------------------------------------------
# describe_snapshot_miss (wh-overlay-snapshot-keepalive): a NON-mutating query
# that names why a get_snapshot would miss, so the click_snapshot_item log can
# distinguish a TTL expiry, a missing id, and a foreground change (today all
# three print the same "snapshot_expired"). Returns None when the snapshot is
# present and would resolve.
# ---------------------------------------------------------------------------
def test_describe_snapshot_miss_names_ttl_expiry():
    now = {"t": 100.0}
    finder = make_multi_finder(clock=lambda: now["t"], snapshot_ttl_seconds=30)
    sid = _store_walk(finder).snapshot.snapshot_id
    now["t"] = 131.0
    assert finder.describe_snapshot_miss(sid) == "ttl_expired"


def test_describe_snapshot_miss_names_not_found():
    finder = make_multi_finder()
    _store_walk(finder)
    assert finder.describe_snapshot_miss("walk-does-not-exist") == "not_found"


def test_describe_snapshot_miss_names_foreground_change():
    finder = make_multi_finder(snapshot_store_capacity=4)
    top = FakeArrayTopLevel([el("Cancel")])
    r = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_top(top, hwnd=1000),
    )
    sid = r.snapshot.snapshot_id
    assert finder.describe_snapshot_miss(
        sid, current_foreground_window=2000
    ) == "foreground_changed"


def test_describe_snapshot_miss_is_none_and_non_mutating_when_resolvable():
    finder = make_multi_finder()
    sid = _store_walk(finder).snapshot.snapshot_id
    # Present, valid, would resolve -> None reason.
    assert finder.describe_snapshot_miss(sid) is None
    # NON-mutating: describe must not drop, sweep, or touch the entry.
    assert finder.get_snapshot(sid) is not None


# ---------------------------------------------------------------------------
# v5 composition test 1: Chromium full-shape with all three fold rules.
#
# Tree (UIA pre-order: parent before child; identical-rect ties broken by
# tree order). Each pair is geometrically nested so the corrections recover
# ancestry from bounds + order.
#
#   GroupControl  "Cancel"   (wrapper)        rect A
#     TextControl "Cancel"   (inner)          rect A (identical -> child)
#   ListItem      "Sign in"                   rect B
#     Hyperlink   "Sign in"                   rect B (identical -> child)
#   Hyperlink     "Welcome"                   rect C
#     HeadingControl "Welcome"                rect C (identical -> child)
#
# After folding: rule 1 drops the group (keeps inner Cancel text), rule 2
# drops the list item (keeps Sign in hyperlink), rule 3 drops the heading
# (keeps outer Welcome hyperlink). Three survivors.
# ---------------------------------------------------------------------------

def _chromium_full_shape_tree():
    rect_a = FakeRect(0, 0, 100, 40)
    rect_b = FakeRect(0, 100, 100, 140)
    rect_c = FakeRect(0, 200, 100, 240)
    return [
        el("Cancel", control_type=UIA_GROUP, role="group", rect=rect_a),
        el("Cancel", control_type=UIA_TEXT, role="text", rect=rect_a),
        el("Sign in", control_type=UIA_LISTITEM, role="list item", rect=rect_b),
        el("Sign in", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_b),
        el("Welcome", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_c),
        el("Welcome", control_type=UIA_HEADING, role="heading", rect=rect_c),
    ]


def _chromium_group_wrapping_listitem_tree():
    # reviewer_1 finding 39.1 shape. A non-interactive GroupControl geometrically
    # wraps a ListItem -> Hyperlink pair. Pre-order (parent before child):
    #
    #   GroupControl "Container"  (wrapper, NOT a same-name text fold)  rect outer
    #     ListItem  "Sign in"                                           rect inner
    #       Hyperlink "Sign in"                          rect inner (== -> child)
    #
    # Rule 2 folds the ListItem into the Hyperlink. Rule 1 then inspects the
    # Group: its surviving sole direct descendant is a Hyperlink (not a same-name
    # TextControl), so rule 1 does NOT fold the Group -- it survives the fold as a
    # non-interactive scaffolding wrapper. It is given invoke_supported=False so
    # neither arm of the overlay clickable filter keeps it.
    rect_outer = FakeRect(0, 0, 200, 100)
    rect_inner = FakeRect(10, 10, 180, 80)
    return [
        el("Container", control_type=UIA_GROUP, role="group",
           rect=rect_outer, invoke_supported=False),
        el("Sign in", control_type=UIA_LISTITEM, role="list item",
           rect=rect_inner),
        el("Sign in", control_type=UIA_HYPERLINK, role="hyperlink",
           rect=rect_inner),
    ]


def _chromium_checkbox_and_pane_tree():
    # A CheckBox is an INTERACTIVE control type driven by TogglePattern, NOT
    # InvokePattern, so invoke_supported=False is realistic for it. A lone
    # decorative GroupControl (invoke_supported=False) matches no fold rule and
    # survives. The overlay clickable filter is a UNION (interactive control type
    # OR InvokePattern-capable): it must KEEP the CheckBox via its interactive
    # control type and DROP the Group. An invoke-only filter would wrongly drop
    # the clickable CheckBox too -- this fixture locks the union choice.
    return [
        el("Remember me", control_type=UIA_CHECKBOX, role="check box",
           rect=FakeRect(0, 0, 100, 40), invoke_supported=False),
        el("decorative", control_type=UIA_GROUP, role="group",
           rect=FakeRect(0, 100, 100, 140), invoke_supported=False),
    ]


def test_composition_chromium_all_three_fold_rules():
    top = FakeArrayTopLevel(_chromium_full_shape_tree())
    finder, _ = make_finder(top)

    # Query "Cancel" -- the surviving inner TextControl is the winner. A text
    # control's role is "text", so the query must be role-less to match it.
    result = finder.find(ElementQuery("cancel", None, None, None, "cancel"),
                         fg(process_name="chrome.exe"))
    assert result.outcome.outcome == "ok"
    assert result.outcome.winner is not None
    assert result.outcome.winner.name == "Cancel"
    assert result.outcome.winner.role == "text"

    # Query "Sign in" -- surviving Hyperlink wins.
    top2 = FakeArrayTopLevel(_chromium_full_shape_tree())
    finder2, _ = make_finder(top2)
    result2 = finder2.find(ElementQuery("sign in", None, None, None, "sign in"),
                           fg(process_name="chrome.exe"))
    assert result2.outcome.outcome == "ok"
    assert result2.outcome.winner is not None
    assert result2.outcome.winner.name == "Sign in"
    assert result2.outcome.winner.role == "hyperlink"

    # Query "Welcome" -- surviving outer Hyperlink wins (heading folded away).
    top3 = FakeArrayTopLevel(_chromium_full_shape_tree())
    finder3, _ = make_finder(top3)
    result3 = finder3.find(ElementQuery("welcome", None, None, None, "welcome"),
                           fg(process_name="chrome.exe"))
    assert result3.outcome.outcome == "ok"
    assert result3.outcome.winner is not None
    assert result3.outcome.winner.name == "Welcome"
    assert result3.outcome.winner.role == "hyperlink"


# ---------------------------------------------------------------------------
# v5 composition test 2: folding removes the only exact-name match.
#
# A GroupControl named exactly "Cancel" wraps a TextControl whose name is a
# LONG string that merely CONTAINS "cancel" as a substring. Rule 1 only folds
# group->text when names match, so it does NOT fire here. Instead: the query
# is role-less "cancel"; the group's exact name would be eligible, but the
# group is paste-capable scaffolding... actually rule 1 needs same name. To
# make the exact match fold away we use the same-name fold and a separate
# long-name substring sibling that fails the ratio gate.
#
#   GroupControl "Cancel"   rect A   (exact-name match, but...)
#     TextControl "Cancel"  rect A   (same name -> rule 1 folds the GROUP)
#   Button "Cancellation settings panel"  rect D  (substring only, role-less,
#                                                   fails ratio gate -> ineligible)
#
# After folding the group is gone; the surviving "Cancel" TEXT is exact-name
# eligible. To exercise "folding removes the only exact match -> not_found"
# we instead make the EXACT match the one that gets folded and the inner a
# NON-matching name. See the tree below.
# ---------------------------------------------------------------------------

def test_composition_folding_removes_exact_match_yields_not_found():
    # GroupControl "cancel" (exact) wraps TextControl "cancel" (same name):
    # rule 1 drops the GROUP, keeping the inner text -- which is ALSO exact.
    # That would still match. To make the exact match disappear we instead
    # build: a ListItem whose sole child is a Hyperlink named "cancel"
    # (rule 2 drops the list item, keeps the hyperlink) is not removal either.
    #
    # The real "removal" case: a Hyperlink "cancel" (exact) that CONTAINS a
    # HeadingControl "cancel" -- rule 3 drops the HEADING, keeping the
    # hyperlink. Still present.
    #
    # The only way folding removes the exact match entirely is when the exact
    # match is the WRAPPER that rule 1/2 drops AND the surviving inner has a
    # different (non-matching) name. Build that:
    #   ListItem "cancel" (exact)  rect B   -> rule 2 drops it
    #     Hyperlink "go home"      rect B   (surviving, does NOT match "cancel")
    #   Button "please cancel order" rect D (substring (NOT prefix) of the name,
    #                                         role-less -> ineligible by ratio)
    rect_b = FakeRect(0, 100, 100, 140)
    rect_d = FakeRect(0, 300, 200, 340)
    tree = [
        el("cancel", control_type=UIA_LISTITEM, role="list item", rect=rect_b),
        el("go home", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_b),
        el("please cancel order", control_type=UIA_BUTTON, role="button", rect=rect_d),
    ]
    top = FakeArrayTopLevel(tree)
    finder, _ = make_finder(top)
    # Role-less "cancel": after folding, the exact-name ListItem is gone;
    # "go home" hyperlink does not contain "cancel"; "please cancel order"
    # button contains "cancel" as a NON-prefix substring (so not exact, not
    # starts-with) but has no role match and fails the length-ratio gate
    # (query 6 chars, name 19 chars: 6 < 0.6*19=11.4), so it is ineligible.
    # Result: not_found.
    result = finder.find(ElementQuery("cancel", None, None, None, "cancel"),
                         fg(process_name="chrome.exe"))
    assert result.outcome.outcome == "not_found"
    assert result.outcome.winner is None


# ---------------------------------------------------------------------------
# v5 composition test 3: folding promotes a sibling 1 -> 2, exercising margin
# + tiebreaker.
#
# Pre-fold there is one obvious winner. Folding promotes a second sibling into
# a valid candidate with an equal score, so the clear-winner margin is 0 and
# the tiebreaker must fire on the post-fold geometry.
#
#   Hyperlink "Open" (winner-A)   rect near cursor   contains a HeadingControl
#     HeadingControl "Open"       rect (heading folded away by rule 3)
#   Hyperlink "Open" (sibling-B)  rect far from cursor
#
# Both hyperlinks are named "Open" (exact, role-less query). The heading fold
# does not change the candidate names but removes the heading from the count.
# Both hyperlinks have identical score -> margin 0 -> tiebreaker decides by
# cursor distance. We place A's centre near the cursor and B's far, with a
# separation comfortably above tiebreaker_min_separation_logical_px so A wins.
# ---------------------------------------------------------------------------

def _eligible_open(matches):
    """Count matches whose name == 'Open' (the query-eligible set for 'open').

    Used to observe the eligible-candidate count at the pre-fold stage (the
    list handed TO apply_dom_corrections) versus the post-fold stage (the list
    it returns), so the composition test proves folding changed the eligible
    count rather than passing as a plain two-link case.
    """
    return [m for m in matches if m.name.casefold() == "open"]


def test_composition_folding_promotes_sibling_margin_and_tiebreaker():
    # Finding 25.3: a GENUINE pre-fold 1 -> post-fold 2 transition.
    #
    # Pre-fold (the list the walker hands to apply_dom_corrections, with
    # query_has_role=False so Text/Group/Heading survive) the ONLY match named
    # "Open" is candidate A's inner heading-wrapped hyperlink scaffolding -- the
    # second sibling B is NOT yet a standalone "Open" candidate because its own
    # name-bearing control is wrapped. Folding promotes B's inner control to a
    # standalone "Open" candidate, taking the eligible set from 1 to 2, and the
    # clear-winner margin + tiebreaker then run on the post-fold pair.
    #
    # Construction using the real fold rules:
    #   A: Hyperlink "Open" (rect_a) wrapping a Heading "Open" (rect_a_inner).
    #      Rule 3 drops the heading, keeps the hyperlink. A is the SINGLE
    #      pre-fold "Open" candidate that the scorer would keep on its own --
    #      but pre-fold its heading is ALSO named "Open", so the raw pre-fold
    #      "Open" set is {A-hyperlink, A-heading} = a wrapper/inner pair that
    #      collapses to one true target.
    #   B: ListItem "Open" (rect_b) wrapping a Hyperlink "weird-inner-id"
    #      ... no -- to PROMOTE B we make B's standalone clickable control
    #      appear only after folding: a Group "Open" (rect_b) whose sole inner
    #      Text is "Open" (rect_b). Rule 1 folds the group, leaving the inner
    #      Text "Open" as a distinct second candidate at B's location.
    #
    # So: pre-fold the distinct VISUAL "Open" targets are A (the heading/link
    # scaffold) = 1 resolved target; folding resolves A to its hyperlink AND
    # resolves B's group to its text, yielding TWO distinct post-fold "Open"
    # candidates A and B. We assert the eligible count via a spy on the fold
    # input/output, then assert margin+tiebreaker on the post-fold pair.
    rect_a = FakeRect(0, 0, 100, 100)        # centre (50,50) == cursor
    rect_a_inner = FakeRect(10, 10, 90, 90)  # heading inside A -> rule 3 folds
    rect_b = FakeRect(0, 150, 100, 250)      # centre (50,200), d=150
    tree = [
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_a),
        el("Open", control_type=UIA_HEADING, role="heading", rect=rect_a_inner),
        el("Open", control_type=UIA_GROUP, role="group", rect=rect_b),
        el("Open", control_type=UIA_TEXT, role="text", rect=rect_b),
    ]

    # Spy on apply_dom_corrections to capture the pre-fold (input) and post-fold
    # (output) match lists WITHOUT adding any production hook.
    captured = {}
    import ui.element_finder as ef_mod
    real_fold = ef_mod.apply_dom_corrections

    def spy_fold(matches):
        captured["pre"] = list(matches)
        out = real_fold(matches)
        captured["post"] = list(out)
        return out

    ef_mod.apply_dom_corrections = spy_fold
    try:
        top = FakeArrayTopLevel(tree)
        finder, _ = make_finder(top)
        context = ForegroundContext(
            foreground_window=1000,
            foreground_pid=4321,
            foreground_process_name="chrome.exe",
            foreground_window_creation_time=99,
            cursor_at_walk=(50, 50),
            cursor_monitor_id=0,
        )
        result = finder.find(ElementQuery("open", None, None, None, "open"),
                             context)
    finally:
        ef_mod.apply_dom_corrections = real_fold

    # Pre-fold: four "Open" raw matches, but they form TWO wrapper/inner
    # scaffolding pairs -> exactly ONE resolved clickable target per pair would
    # survive. The distinguishing assertion (vs a plain two-link case): folding
    # REDUCED the raw set, and the post-fold survivors are exactly the two inner
    # resolved controls (A's hyperlink + B's text), one per location.
    assert len(_eligible_open(captured["pre"])) == 4
    post_open = _eligible_open(captured["post"])
    assert len(post_open) == 2
    post_bounds = sorted(m.bounds for m in post_open)
    assert post_bounds == [(0, 0, 100, 100), (0, 150, 100, 100)]
    # A is the hyperlink (heading folded away); B is the text (group folded).
    by_bounds = {m.bounds: m.role for m in post_open}
    assert by_bounds[(0, 0, 100, 100)] == "hyperlink"
    assert by_bounds[(0, 150, 100, 100)] == "text"

    # Post-fold pair has equal score -> margin 0 -> tiebreaker fires; A is at
    # the cursor (d=0), B is 150px away (sep 150 >= 30 threshold) -> A wins.
    assert result.outcome.outcome == "ok"
    assert result.outcome.winner is not None
    assert result.outcome.winner.bounds == (0, 0, 100, 100)


def test_composition_promoted_siblings_too_close_is_ambiguous():
    # Same shape but B is placed so its separation from A is BELOW the 30px
    # logical threshold -> tiebreaker abstains -> ambiguous with both
    # candidates returned.
    rect_a = FakeRect(0, 0, 100, 100)        # centre (50,50), cursor at (50,50)
    rect_a_inner = FakeRect(10, 10, 90, 90)
    rect_b = FakeRect(0, 10, 100, 110)       # centre (50,60), d=10; sep 10 < 30
    tree = [
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_a),
        el("Open", control_type=UIA_HEADING, role="heading", rect=rect_a_inner),
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_b),
    ]
    top = FakeArrayTopLevel(tree)
    finder, _ = make_finder(top)
    context = ForegroundContext(
        foreground_window=1000,
        foreground_pid=4321,
        foreground_process_name="chrome.exe",
        foreground_window_creation_time=99,
        cursor_at_walk=(50, 50),
        cursor_monitor_id=0,
    )
    result = finder.find(ElementQuery("open", None, None, None, "open"), context)
    assert result.outcome.outcome == "ambiguous"
    assert len(result.outcome.candidates) == 2


# ---------------------------------------------------------------------------
# UIAStrategy thin wrapper.
# ---------------------------------------------------------------------------

def test_uia_strategy_find_delegates_to_finder():
    top = FakeArrayTopLevel([el("Cancel")])
    base_walk = make_real_walk_fn(top)
    finder = ElementFinder(
        walk_fn=base_walk,
        dpi_resolver=fixed_dpi_resolver,
        monitor_resolver=zero_monitor_resolver,
    )
    strategy = UIAStrategy(finder)
    result = strategy.find(ElementQuery("cancel", "button", None, None, "cancel"),
                           fg())
    assert isinstance(result, FindResult)
    assert result.outcome.outcome == "ok"


# ---------------------------------------------------------------------------
# FINDING 24.1: per-match monitor_id via injected monitor_resolver.
#
# decide()'s tiebreaker cross-monitor gate drops candidates whose monitor_id
# != cursor_monitor_id. The finder must re-stamp each kept match's monitor_id
# from monitor_resolver(bounds) BEFORE decide, so a candidate physically on
# another monitor is excluded rather than corrupting the tiebreaker with a
# huge cross-monitor distance.
# ---------------------------------------------------------------------------

def test_per_match_monitor_id_excludes_cross_monitor_candidate():
    # Two "Open" hyperlinks with equal score -> margin 0 -> tiebreaker fires.
    # A is near the cursor on monitor 1; B is far AND on monitor 2. With
    # per-match monitor_id, the gate drops B (different monitor) leaving only
    # A on the cursor monitor -> fewer than two -> tiebreaker would abstain...
    # so instead we place a THIRD on-monitor candidate to keep >=2 on monitor 1
    # and assert the cross-monitor B is excluded from the candidate math.
    #
    # Simpler and directly to the point: A near cursor (monitor 1), B far
    # (monitor 2). monitor_resolver maps B's bounds to 2, everything else to 1.
    # cursor_monitor_id=1. With the per-match fix, B is gated out; only A is
    # left on the cursor monitor -> <2 candidates -> ambiguous abstain. To
    # assert the WIN path we add a second on-monitor candidate C close behind A
    # but far enough that A wins by separation.
    # FakeRect args are (left, top, right, bottom); _rect_to_bounds converts to
    # (x, y, w, h). A: left/top 0..100 -> bounds (0,0,100,100), centre (50,50).
    # B: left 5000, right 5100 -> bounds (5000,0,100,100), centre (5050,50).
    rect_a = FakeRect(0, 0, 100, 100)
    rect_b = FakeRect(5000, 0, 5100, 100)
    tree = [
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_a),
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_b),
    ]

    def monitor_resolver(bounds):
        # bounds is (x, y, w, h); anything with x >= 1000 is on monitor 2.
        return 2 if bounds[0] >= 1000 else 1

    top = FakeArrayTopLevel(tree)
    finder, _ = make_finder(top, monitor_resolver=monitor_resolver)
    context = ForegroundContext(
        foreground_window=1000,
        foreground_pid=4321,
        foreground_process_name="chrome.exe",
        foreground_window_creation_time=99,
        cursor_at_walk=(50, 50),
        cursor_monitor_id=1,
    )
    result = finder.find(ElementQuery("open", None, None, None, "open"), context)
    # B is on monitor 2 (cross-monitor); the gate leaves only A on the cursor
    # monitor 1, so fewer than two candidates remain -> tiebreaker abstains ->
    # ambiguous. The KEY assertion: the stored snapshot has per-match monitor
    # ids, NOT a uniform cursor_monitor_id stamped by the walker.
    monitor_ids = {m.bounds: m.monitor_id for m in result.snapshot.matches}
    assert monitor_ids[(0, 0, 100, 100)] == 1
    assert monitor_ids[(5000, 0, 100, 100)] == 2
    # With the cross-monitor candidate gated out, only one on-monitor candidate
    # remains -> ambiguous (tiebreaker abstains at <2 on cursor monitor).
    assert result.outcome.outcome == "ambiguous"


def test_per_match_monitor_id_on_monitor_candidate_wins():
    # Both candidates on the cursor monitor (1): A near cursor, B far. Per-match
    # resolver stamps both to monitor 1, so the gate keeps both and the
    # tiebreaker resolves to A (closest, separation above threshold).
    # FakeRect args are (left, top, right, bottom). A: bounds (0,0,100,100),
    # centre (50,50) == cursor, d=0. B: bounds (0,150,100,100), centre
    # (50,200), d=150 logical -- separation 150 >= 30 threshold so A wins.
    rect_a = FakeRect(0, 0, 100, 100)
    rect_b = FakeRect(0, 150, 100, 250)
    tree = [
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_a),
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_b),
    ]

    def monitor_resolver(_bounds):
        return 1

    top = FakeArrayTopLevel(tree)
    finder, _ = make_finder(top, monitor_resolver=monitor_resolver)
    context = ForegroundContext(
        foreground_window=1000,
        foreground_pid=4321,
        foreground_process_name="chrome.exe",
        foreground_window_creation_time=99,
        cursor_at_walk=(50, 50),
        cursor_monitor_id=1,
    )
    result = finder.find(ElementQuery("open", None, None, None, "open"), context)
    assert result.outcome.outcome == "ok"
    assert result.outcome.winner is not None
    assert result.outcome.winner.bounds == (0, 0, 100, 100)


def test_per_match_monitor_id_disabled_winner_still_reaches_decide():
    # A single exact-name disabled control must still reach decide (re-stamped
    # monitor_id, but still eligible) and surface execution_failed:disabled.
    top = FakeArrayTopLevel(
        [el("Cancel", is_enabled=False, invoke_supported=False)]
    )

    def monitor_resolver(_bounds):
        return 7

    finder, _ = make_finder(top, monitor_resolver=monitor_resolver)
    result = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                         fg())
    assert result.outcome.outcome == "execution_failed"
    assert result.outcome.reason == "disabled"
    assert result.snapshot.matches[0].monitor_id == 7


def test_cursor_monitor_id_derived_from_resolver_not_passed_field():
    # Finding 25.1: the cross-monitor gate must use a cursor monitor id from the
    # SAME resolver namespace as the candidate ids, NOT the passed
    # foreground.cursor_monitor_id. Here the passed field is DELIBERATELY wrong
    # (999, a value in no real namespace), yet the resolver maps the cursor
    # point to monitor 1 and candidate A to 1, candidate B to 2. The gate must
    # still drop B and keep A purely from resolver-derived ids.
    #
    # FakeRect (left, top, right, bottom). A: (0,0,100,100) centre (50,50).
    # B: (5000,0,5100,100) centre (5050,50).
    rect_a = FakeRect(0, 0, 100, 100)
    rect_b = FakeRect(5000, 0, 5100, 100)
    tree = [
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_a),
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_b),
    ]

    def monitor_resolver(bounds):
        # bounds (x, y, w, h). The cursor (50,50,0,0) and A both resolve to 1;
        # B (x=5000) resolves to 2. 999 (the wrong passed value) never appears.
        return 2 if bounds[0] >= 1000 else 1

    top = FakeArrayTopLevel(tree)
    finder, _ = make_finder(top, monitor_resolver=monitor_resolver)
    context = ForegroundContext(
        foreground_window=1000,
        foreground_pid=4321,
        foreground_process_name="chrome.exe",
        foreground_window_creation_time=99,
        cursor_at_walk=(50, 50),
        cursor_monitor_id=999,  # deliberately WRONG / wrong namespace
    )
    result = finder.find(ElementQuery("open", None, None, None, "open"), context)
    # Resolver-derived cursor monitor is 1 (not 999). B is on monitor 2 ->
    # gated out, leaving only A on the cursor monitor -> <2 -> ambiguous.
    # If the gate had used the passed 999, NEITHER candidate would be on the
    # cursor monitor and the result would still be ambiguous, so to make the
    # test discriminating we assert the stored cursor_monitor_id is the
    # resolver value (1), not the passed 999.
    assert result.snapshot.cursor_monitor_id == 1
    assert result.outcome.outcome == "ambiguous"


def test_cursor_monitor_id_resolver_keeps_both_on_one_monitor():
    # Companion to 25.1: with the resolver mapping the cursor AND both
    # candidates to the same monitor, the gate keeps both and the tiebreaker
    # resolves to the nearer one -- even though the passed cursor_monitor_id is
    # a wrong namespace value. This proves the WIN path depends only on the
    # resolver-derived cursor monitor.
    rect_a = FakeRect(0, 0, 100, 100)        # centre (50,50) == cursor
    rect_b = FakeRect(0, 150, 100, 250)       # centre (50,200), d=150
    tree = [
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_a),
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_b),
    ]

    def monitor_resolver(_bounds):
        return 5  # cursor and both candidates share monitor 5

    top = FakeArrayTopLevel(tree)
    finder, _ = make_finder(top, monitor_resolver=monitor_resolver)
    context = ForegroundContext(
        foreground_window=1000,
        foreground_pid=4321,
        foreground_process_name="chrome.exe",
        foreground_window_creation_time=99,
        cursor_at_walk=(50, 50),
        cursor_monitor_id=999,  # wrong; ignored for the gate
    )
    result = finder.find(ElementQuery("open", None, None, None, "open"), context)
    assert result.snapshot.cursor_monitor_id == 5
    assert result.outcome.outcome == "ok"
    assert result.outcome.winner is not None
    assert result.outcome.winner.bounds == (0, 0, 100, 100)


def test_cursor_box_is_1x1_so_overlap_area_resolver_picks_correct_monitor():
    # Finding 26.1: the cursor box passed to monitor_resolver must be 1x1, not
    # 0x0, so an OVERLAP-AREA resolver (the documented production resolver,
    # shared/monitor_geometry.py:_resolve_target_monitor via _overlap_area)
    # assigns the cursor to the monitor it is actually on. _overlap_area returns
    # 0 for any zero-area box, so a 0x0 cursor box would overlap every monitor
    # by 0 and fall through to the primary monitor -- breaking the gate when the
    # cursor is on a secondary display.
    #
    # The fake resolver below mimics overlap-area semantics:
    #   - a zero-area box (w<=0 or h<=0) returns the PRIMARY sentinel (1), the
    #     fall-through an overlap-area resolver takes for a degenerate box;
    #   - a positive-area box returns the monitor whose region contains its
    #     top-left point. Monitor 1 = x in [0,1000); monitor 2 = x in
    #     [2000, 4000). The cursor and candidate A are on monitor 2 (secondary);
    #     candidate B is on monitor 1 (primary).
    PRIMARY = 1
    SECONDARY = 2

    def overlap_area_resolver(bounds):
        x, _y, w, h = bounds
        if w <= 0 or h <= 0:
            return PRIMARY  # degenerate box overlaps nothing -> primary
        if 2000 <= x < 4000:
            return SECONDARY
        return PRIMARY

    # FakeRect (left, top, right, bottom). Cursor at (2500, 50) is on the
    # secondary monitor. A: (2400,0,2500,100) -> centre (2450,50), on secondary,
    # near cursor. B: (0,0,100,100) -> centre (50,50), on primary monitor.
    rect_a = FakeRect(2400, 0, 2500, 100)
    rect_b = FakeRect(0, 0, 100, 100)
    tree = [
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_a),
        el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_b),
    ]
    top = FakeArrayTopLevel(tree)
    finder, _ = make_finder(top, monitor_resolver=overlap_area_resolver)
    context = ForegroundContext(
        foreground_window=1000,
        foreground_pid=4321,
        foreground_process_name="chrome.exe",
        foreground_window_creation_time=99,
        cursor_at_walk=(2500, 50),
        cursor_monitor_id=2,
    )
    result = finder.find(ElementQuery("open", None, None, None, "open"), context)
    # With the 1x1 cursor box the cursor resolves to the SECONDARY monitor (2),
    # NOT the primary (which a 0x0 box would have produced). The gate keeps the
    # secondary-monitor candidate A and drops the primary-monitor candidate B.
    assert result.snapshot.cursor_monitor_id == SECONDARY
    monitor_ids = {m.bounds: m.monitor_id for m in result.snapshot.matches}
    assert monitor_ids[(2400, 0, 100, 100)] == SECONDARY  # A
    assert monitor_ids[(0, 0, 100, 100)] == PRIMARY        # B gated out
    # Only A remains on the cursor monitor -> <2 candidates -> ambiguous abstain.
    # (If the cursor had wrongly resolved to PRIMARY, B would be the lone
    # on-monitor candidate instead -- the cursor_monitor_id assertion above is
    # the discriminating check that the 1x1 box picked the right monitor.)
    assert result.outcome.outcome == "ambiguous"


# ---------------------------------------------------------------------------
# FINDING 24.2: FindResult pins the WalkResult keepalive chain.
#
# Holding a returned FindResult must keep its snapshot's COM control_refs alive
# independent of later walks (which replace self._stored). FindResult carries a
# private _walk_result that pins the chain.
# ---------------------------------------------------------------------------

def test_findresult_pins_walkresult_keepalive_across_next_walk():
    import weakref

    # Build a fake whose control_ref is a weakref-able object. The walker uses
    # the cached element itself as control_ref, and our FakeCachedElement is a
    # normal Python object (weakref-able), so we can weakref a match's
    # control_ref directly.
    top = FakeArrayTopLevel([el("Cancel")])
    finder, _ = make_finder(top)
    result1 = finder.find(ElementQuery("cancel", "button", None, None, "cancel"),
                          fg())
    ref = weakref.ref(result1.snapshot.matches[0].control_ref)
    assert ref() is not None

    # A second walk replaces self._stored, dropping the finder's hold on the
    # first WalkResult. result1._walk_result must still pin the chain.
    top2 = FakeArrayTopLevel([el("Save")])
    # Reuse the same finder so _stored is replaced.
    finder.find(ElementQuery("save", "button", None, None, "save"),
                ForegroundContext(
                    foreground_window=1000,
                    foreground_pid=4321,
                    foreground_process_name="notepad.exe",
                    foreground_window_creation_time=99,
                    cursor_at_walk=(500, 500),
                    cursor_monitor_id=0,
                    top_level=top2,
                ))
    gc.collect()
    # result1 still held -> its control_ref proxy survives.
    assert ref() is not None
    assert result1._walk_result is not None


# ---------------------------------------------------------------------------
# FINDING 24.3: get_snapshot invalidates on full foreground IDENTITY change,
# not just HWND (Windows reuses HWND values).
# ---------------------------------------------------------------------------

def _store_one(finder):
    return finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        ForegroundContext(
            foreground_window=1000,
            foreground_pid=4321,
            foreground_process_name="notepad.exe",
            foreground_window_creation_time=99,
            cursor_at_walk=(500, 500),
            cursor_monitor_id=0,
        ),
    )


def test_get_snapshot_invalidated_on_pid_change():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, _ = make_finder(top)
    result = _store_one(finder)
    sid = result.snapshot.snapshot_id
    # Same HWND, DIFFERENT pid -> recycled HWND -> invalidate.
    assert finder.get_snapshot(
        sid, current_foreground_window=1000, current_foreground_pid=9999
    ) is None
    assert finder.latest_snapshot() is None


def test_get_snapshot_invalidated_on_creation_time_change():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, _ = make_finder(top)
    result = _store_one(finder)
    sid = result.snapshot.snapshot_id
    assert finder.get_snapshot(
        sid,
        current_foreground_window=1000,
        current_foreground_pid=4321,
        current_foreground_window_creation_time=123456,
    ) is None
    assert finder.latest_snapshot() is None


def test_get_snapshot_invalidated_on_process_name_change():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, _ = make_finder(top)
    result = _store_one(finder)
    sid = result.snapshot.snapshot_id
    assert finder.get_snapshot(
        sid,
        current_foreground_window=1000,
        current_foreground_process_name="evil.exe",
    ) is None
    assert finder.latest_snapshot() is None


def test_get_snapshot_full_identity_match_returns_snapshot():
    top = FakeArrayTopLevel([el("Cancel")])
    finder, _ = make_finder(top)
    result = _store_one(finder)
    sid = result.snapshot.snapshot_id
    assert finder.get_snapshot(
        sid,
        current_foreground_window=1000,
        current_foreground_pid=4321,
        current_foreground_process_name="notepad.exe",
        current_foreground_window_creation_time=99,
    ) is result.snapshot


# ---------------------------------------------------------------------------
# wh-86qdm: v5 restricted window-walk fall-back.
#
# When the focused-window walk yields not_found, the finder enumerates the
# other visible top-level windows, restricts them to the focused window's
# monitor (unless enable_offmonitor_fallback is True), orders them by the v5
# overlay heuristics, and walks them in order until one decides a match.
#
# Test wiring: a dispatching walk_fn maps each top_level token (the focused
# window's fake element OR a fall-back window's HWND int) to a fake tree, so the
# real walk_window runs per window. A fake window_enumerator returns synthetic
# FallbackWindow records. The monitor_resolver maps both candidate rects and the
# cursor box to monitor ids so the same-monitor gate is exercised.
# ---------------------------------------------------------------------------

from ui.window_fallback import (  # noqa: E402
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    FallbackWindow,
)


def _empty_enumerator():
    return []


class DispatchWalk:
    """A walk_fn that drives the REAL walk_window over a per-token fake tree.

    ``trees`` maps a top_level token to the element list to walk. The focused
    window is keyed by the object the finder passes as top_level (the fake
    top-level element); each fall-back window is keyed by its HWND int. Records
    every walk's kwargs and the token walked so tests can assert which windows
    were walked, in what order, with what query_has_role.
    """

    def __init__(self, trees):
        self._trees = trees
        self.calls = []  # list of (token, kwargs)

    def __call__(self, top_level, **kwargs):
        self.calls.append((top_level, kwargs))
        elements = self._trees[top_level]
        # A token mapped to an Exception models a window that closed in the gap
        # between enumeration and the walk: the walk raises (finding 45.1).
        if isinstance(elements, BaseException):
            raise elements
        kwargs.pop("automation", None)
        return uia_walker.walk_window(
            FakeArrayTopLevel(elements),
            automation=FakeAutomation(),
            **kwargs,
        )


# The fall-back default monitor-rect resolver models a single primary monitor
# spanning x in [0, 2000). Same-monitor fall-back windows (rect x < 2000)
# overlap it; off-monitor windows (rect x >= ~3000) do not, so the overlap-based
# restrict_to_monitor (finding 45.3) drops them when the off-monitor flag is
# False. Tests that need a different topology inject their own resolver.
def _primary_monitor_rect_resolver(_box):
    return (0, 0, 2000, 2000)


# Default focused-window-rect resolver (finding 46.2): the focused window is not
# in most fall-back-test enumerations, so the finder calls this resolver on the
# focused HWND. Returns a rect inside the primary monitor so the focused monitor
# resolves to (0,0,2000,2000). Tests that need the focused rect to be
# unresolvable inject a resolver returning None.
def _focused_window_rect_resolver(_hwnd):
    return (0, 0, 800, 600)


def make_fallback_finder(
    trees,
    enumerator,
    *,
    monitor_resolver,
    monitor_rect_resolver=_primary_monitor_rect_resolver,
    focused_window_rect_resolver=_focused_window_rect_resolver,
    **overrides,
):
    """Build an ElementFinder wired for fall-back tests."""
    walk = DispatchWalk(trees)
    kwargs: dict[str, Any] = {
        "walk_fn": walk,
        "dpi_resolver": fixed_dpi_resolver,
        "monitor_resolver": monitor_resolver,
        "monitor_rect_resolver": monitor_rect_resolver,
        "focused_window_rect_resolver": focused_window_rect_resolver,
        "window_enumerator": enumerator,
    }
    kwargs.update(overrides)
    finder = ElementFinder(**kwargs)
    return finder, walk


def fg_focused(token, *, process_name="notepad.exe", hwnd=1000, cursor=(50, 50)):
    """ForegroundContext whose top_level is the focused window's walk token."""
    return ForegroundContext(
        foreground_window=hwnd,
        foreground_pid=4321,
        foreground_process_name=process_name,
        foreground_window_creation_time=99,
        cursor_at_walk=cursor,
        cursor_monitor_id=1,
        top_level=token,
    )


def fbwin(hwnd, *, pid=200, process_name="other.exe", ex_style=0, rect=(0, 0, 800, 600)):
    return FallbackWindow(
        hwnd=hwnd, pid=pid, process_name=process_name, ex_style=ex_style, rect=rect
    )


def test_fallback_not_run_when_focused_window_decides_ok():
    # Focused window has an exact match -> ok. Enumerator must NEVER be called.
    focused = object()
    trees = {focused: [el("Cancel")]}
    enumerator_calls = {"n": 0}

    def enumerator():
        enumerator_calls["n"] += 1
        return []

    finder, walk = make_fallback_finder(
        trees, enumerator, monitor_resolver=lambda _b: 1
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "ok"
    assert enumerator_calls["n"] == 0
    # Only the focused window was walked.
    assert len(walk.calls) == 1


def test_fallback_not_run_when_focused_window_ambiguous():
    # Two equal "Open" hyperlinks too close to disambiguate -> ambiguous.
    # Fall-back must NOT run.
    focused = object()
    rect_a = FakeRect(0, 0, 100, 100)
    rect_b = FakeRect(0, 10, 100, 110)  # sep 10 < 30 threshold
    trees = {
        focused: [
            el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_a),
            el("Open", control_type=UIA_HYPERLINK, role="hyperlink", rect=rect_b),
        ]
    }
    enumerator_calls = {"n": 0}

    def enumerator():
        enumerator_calls["n"] += 1
        return []

    finder, _ = make_fallback_finder(
        trees, enumerator, monitor_resolver=lambda _b: 1
    )
    result = finder.find(
        ElementQuery("open", None, None, None, "open"),
        fg_focused(focused, cursor=(50, 50)),
    )
    assert result.outcome.outcome == "ambiguous"
    assert enumerator_calls["n"] == 0


def test_fallback_not_run_when_focused_window_execution_failed():
    focused = object()
    trees = {focused: [el("Cancel", is_enabled=False, invoke_supported=False)]}
    enumerator_calls = {"n": 0}

    def enumerator():
        enumerator_calls["n"] += 1
        return []

    finder, _ = make_fallback_finder(
        trees, enumerator, monitor_resolver=lambda _b: 1
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "execution_failed"
    assert enumerator_calls["n"] == 0


def test_fallback_walks_same_monitor_window_on_not_found():
    # Focused window has no match. A same-monitor fall-back window has "Cancel".
    focused = object()
    trees = {
        focused: [el("Nothing")],
        7: [el("Cancel")],  # fall-back window hwnd=7
    }
    candidates = [fbwin(7, rect=(0, 0, 800, 600))]
    finder, walk = make_fallback_finder(
        trees, lambda: candidates, monitor_resolver=lambda _b: 1
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "ok"
    assert result.outcome.winner is not None
    assert result.outcome.winner.name == "Cancel"
    # Focused window walked first, then the fall-back window (hwnd 7).
    tokens = [c[0] for c in walk.calls]
    assert tokens == [focused, 7]


def test_fallback_shares_one_deadline_across_all_walks():
    """FINDING 1: the focused walk AND every fall-back walk receive the SAME
    absolute deadline value -- it is computed ONCE per request, not re-derived
    per walk_window call. Otherwise N fall-back windows would each get a fresh
    full budget and the total block would be (1+N) * budget."""
    focused = object()
    trees = {
        focused: [el("Nothing")],
        7: [el("AlsoNothing")],
        9: [el("Cancel")],
    }
    candidates = [fbwin(7, rect=(0, 0, 800, 600)), fbwin(9, rect=(0, 0, 800, 600))]
    finder, walk = make_fallback_finder(
        trees,
        lambda: candidates,
        monitor_resolver=lambda _b: 1,
        clock=lambda: 0.0,
        walk_deadline_ms=1000,
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    # All three windows walked (focused + 2 fall-backs), all within budget.
    assert result.outcome.outcome == "ok"
    deadlines = {c[1]["deadline"] for c in walk.calls}
    # ONE shared absolute deadline (0.0 + 1000/1000 == 1.0), not three.
    assert deadlines == {1.0}
    assert len(walk.calls) == 3


def test_fallback_loop_stops_once_shared_deadline_passes():
    """FINDING 1: once the single per-request deadline has passed, the fall-back
    loop stops enumerating candidates instead of giving each a fresh budget. The
    focused walk runs within budget (not_found); by the time the fall-back loop
    runs the clock is past the deadline, so NO fall-back window is walked and the
    focused not_found stands."""
    focused = object()
    trees = {
        focused: [el("Nothing")],
        7: [el("Cancel")],  # would match, but must never be walked
        9: [el("Cancel")],
    }
    candidates = [fbwin(7, rect=(0, 0, 800, 600)), fbwin(9, rect=(0, 0, 800, 600))]

    # Clock stays at 0.0 for the focused walk (deadline=1.0, within budget),
    # then jumps to 5.0 once the focused walk's reads are exhausted -- so the
    # fall-back loop's top-of-loop deadline check trips immediately.
    reads = {"n": 0}

    def clock():
        reads["n"] += 1
        # First few reads (focused walk pre-check + per-element loop) are 0.0;
        # everything after is past the 1.0 deadline.
        return 0.0 if reads["n"] <= 3 else 5.0

    finder, walk = make_fallback_finder(
        trees,
        lambda: candidates,
        monitor_resolver=lambda _b: 1,
        clock=clock,
        walk_deadline_ms=1000,
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    # The focused not_found stands; NO fall-back window was walked.
    assert result.outcome.outcome == "not_found"
    tokens = [c[0] for c in walk.calls]
    assert tokens == [focused]


def test_fallback_excludes_offmonitor_window_by_default():
    # The only candidate carrying "Cancel" sits at x=3000, which does NOT overlap
    # the focused monitor rect (0,0,2000,2000); default flag False drops it via
    # the overlap-based restriction, so it is never walked -> not_found.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        8: [el("Cancel")],
    }
    # Window 8 is on the neighbouring monitor (rect x large, no overlap).
    candidates = [fbwin(8, rect=(3000, 0, 800, 600))]

    finder, walk = make_fallback_finder(
        trees, lambda: candidates, monitor_resolver=lambda _b: 1
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "not_found"
    # The off-monitor window was NOT walked (only the focused window).
    tokens = [c[0] for c in walk.calls]
    assert tokens == [focused]


def test_fallback_includes_offmonitor_window_when_enabled():
    focused = object()
    trees = {
        focused: [el("Nothing")],
        8: [el("Cancel")],
    }
    candidates = [fbwin(8, rect=(3000, 0, 800, 600))]

    finder, walk = make_fallback_finder(
        trees,
        lambda: candidates,
        monitor_resolver=lambda _b: 1,
        enable_offmonitor_fallback=True,
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "ok"
    assert result.outcome.winner is not None
    assert result.outcome.winner.name == "Cancel"
    tokens = [c[0] for c in walk.calls]
    assert tokens == [focused, 8]


def test_fallback_excludes_focused_window_from_candidates():
    # The enumerator returns the focused window itself (same hwnd as
    # foreground_window) plus a real fall-back window. The focused window must
    # NOT be walked twice.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        9: [el("Cancel")],
    }
    candidates = [
        fbwin(1000, rect=(0, 0, 800, 600)),  # the focused window's HWND
        fbwin(9, rect=(0, 0, 800, 600)),
    ]
    finder, walk = make_fallback_finder(
        trees, lambda: candidates, monitor_resolver=lambda _b: 1
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused, hwnd=1000),
    )
    assert result.outcome.outcome == "ok"
    tokens = [c[0] for c in walk.calls]
    # Focused window (token `focused`) then window 9. HWND 1000 never appears
    # as a fall-back token (it was the focused window, excluded).
    assert tokens == [focused, 9]
    assert 1000 not in tokens


def test_fallback_no_candidates_returns_not_found():
    focused = object()
    trees = {focused: [el("Nothing")]}
    finder, walk = make_fallback_finder(
        trees, _empty_enumerator, monitor_resolver=lambda _b: 1
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "not_found"
    assert result.outcome.winner is None
    tokens = [c[0] for c in walk.calls]
    assert tokens == [focused]


def test_fallback_skips_empty_overlay_window_no_interactive_children():
    # v5 signal 4: a window with no interactive children (its walk yields no
    # decided match) is skipped and the next candidate is tried. Window 11 has
    # only static text (role-bearing query drops it -> not_found); window 12 has
    # the real "Cancel" button. Both same-monitor; ordering keeps enumeration
    # order (equal signal count). The finder must walk 11, get not_found, then
    # walk 12 and win.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        11: [el("Just a label", control_type=UIA_TEXT, role="text")],
        12: [el("Cancel")],
    }
    candidates = [
        fbwin(11, rect=(0, 0, 800, 600)),
        fbwin(12, rect=(0, 0, 800, 600)),
    ]
    finder, walk = make_fallback_finder(
        trees, lambda: candidates, monitor_resolver=lambda _b: 1
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "ok"
    assert result.outcome.winner is not None
    assert result.outcome.winner.name == "Cancel"
    tokens = [c[0] for c in walk.calls]
    assert tokens == [focused, 11, 12]


def test_fallback_overlay_window_deprioritised_in_walk_order():
    # Two same-monitor candidates BOTH carry "Cancel"; the overlay (topmost+
    # toolwindow) must be walked AFTER the plain window. Since the first decided
    # match wins, the plain window (13) wins and the overlay (14) is never
    # walked.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        13: [el("Cancel")],
        14: [el("Cancel")],
    }
    overlay = fbwin(
        14, ex_style=WS_EX_TOPMOST | WS_EX_TOOLWINDOW, rect=(0, 0, 800, 600)
    )
    plain = fbwin(13, rect=(0, 0, 800, 600))
    # Enumerate the overlay FIRST to prove ordering (not enumeration order)
    # decides: the plain window must still be walked first.
    candidates = [overlay, plain]
    finder, walk = make_fallback_finder(
        trees, lambda: candidates, monitor_resolver=lambda _b: 1
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "ok"
    tokens = [c[0] for c in walk.calls]
    # Plain window 13 walked before overlay; 13 wins so 14 is never reached.
    assert tokens == [focused, 13]


def test_fallback_window_browser_walks_query_has_role_false():
    # A Chromium-family fall-back window must be walked with query_has_role=False
    # so the load-bearing browser wiring holds for the fall-back path too.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        15: [el("Cancel")],
    }
    candidates = [fbwin(15, process_name="chrome.exe", rect=(0, 0, 800, 600))]
    finder, walk = make_fallback_finder(
        trees, lambda: candidates, monitor_resolver=lambda _b: 1
    )
    finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused, process_name="notepad.exe"),
    )
    # The focused (notepad) walk used query_has_role=True (role-bearing query);
    # the chrome fall-back walk used query_has_role=False AND a browser hook.
    by_token = {c[0]: c[1] for c in walk.calls}
    assert by_token[focused]["query_has_role"] is True
    assert by_token[focused]["browser_correction_hook"] is None
    assert by_token[15]["query_has_role"] is False
    assert by_token[15]["browser_correction_hook"] is not None


def test_fallback_winner_snapshot_and_walkresult_are_the_fallback_walk():
    # COM keepalive: the returned FindResult's snapshot + _walk_result must come
    # from the fall-back walk that produced the outcome, not the focused walk.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        16: [el("Cancel")],
    }
    candidates = [fbwin(16, rect=(0, 0, 800, 600))]
    finder, _ = make_fallback_finder(
        trees, lambda: candidates, monitor_resolver=lambda _b: 1  # noqa: F821 -- lambda runs before the later `del candidates`
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "ok"
    # The stored snapshot is the fall-back walk's snapshot (has the Cancel match
    # the fall-back window produced, not the focused window's empty result).
    assert [m.name for m in result.snapshot.matches] == ["Cancel"]
    assert finder.latest_snapshot() is result.snapshot
    # The pinned WalkResult holds the fall-back walk's matches (keepalive comes
    # from the winning walk). Survives a gc.collect() because _walk_result pins
    # it.
    ctrl_ref = result.snapshot.matches[0].control_ref
    assert ctrl_ref is not None
    del candidates
    gc.collect()
    assert result._walk_result is not None
    assert result._walk_result.matches[0].name == "Cancel"


def test_fallback_all_candidates_not_found_returns_not_found():
    # Two same-monitor candidates, neither has the query -> not_found after the
    # whole same-monitor fall-back. Both are walked.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        17: [el("Other")],
        18: [el("Else")],
    }
    candidates = [fbwin(17, rect=(0, 0, 800, 600)), fbwin(18, rect=(0, 0, 800, 600))]
    finder, walk = make_fallback_finder(
        trees, lambda: candidates, monitor_resolver=lambda _b: 1
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "not_found"
    tokens = [c[0] for c in walk.calls]
    assert tokens == [focused, 17, 18]


# ---------------------------------------------------------------------------
# FINDING 45.1: a window that closes mid-fall-back (its walk raises) is skipped,
# not fatal -- the loop continues to the next candidate.
# ---------------------------------------------------------------------------

def test_fallback_skips_window_whose_walk_raises():
    focused = object()
    trees = {
        focused: [el("Nothing")],
        # A stale-handle failure: OSError is the class ElementFromHandle /
        # FindAllBuildCache raise when the window closed (finding 45.1/46.1).
        19: OSError("window closed: ElementFromHandle failed"),
        20: [el("Cancel")],
    }
    candidates = [
        fbwin(19, rect=(0, 0, 800, 600)),
        fbwin(20, rect=(0, 0, 800, 600)),
    ]
    finder, walk = make_fallback_finder(
        trees, lambda: candidates, monitor_resolver=lambda _b: 1
    )
    # The first candidate's walk raises; find() must NOT propagate it. The loop
    # continues to candidate 20, which decides ok.
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "ok"
    assert result.outcome.winner is not None
    assert result.outcome.winner.name == "Cancel"
    # Both fall-back windows were attempted; the raise did not abort the loop.
    tokens = [c[0] for c in walk.calls]
    assert tokens == [focused, 19, 20]


def test_fallback_all_candidates_raise_returns_not_found():
    # When every fall-back candidate's walk raises, find() returns the focused
    # window's not_found rather than propagating the exception.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        21: OSError("closed"),
        22: OSError("closed"),
    }
    candidates = [fbwin(21, rect=(0, 0, 800, 600)), fbwin(22, rect=(0, 0, 800, 600))]
    finder, walk = make_fallback_finder(
        trees, lambda: candidates, monitor_resolver=lambda _b: 1
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused),
    )
    assert result.outcome.outcome == "not_found"
    tokens = [c[0] for c in walk.calls]
    assert tokens == [focused, 21, 22]


# ---------------------------------------------------------------------------
# FINDING 45.2 / 46.4: the very-small overlay signal is measured against the
# focused window's MONITOR rectangle, AND only deprioritises a window that ALSO
# carries a window-STYLE overlay flag (46.4). A small *plain* dialog keeps its
# z-order; a small window WITH an overlay style is still deprioritised.
# ---------------------------------------------------------------------------

def test_fallback_very_small_overlay_measured_against_monitor():
    # Two same-monitor candidates BOTH carry "Cancel". Candidate 23 is small
    # relative to the MONITOR (1920x1080) AND carries WS_EX_NOACTIVATE (a style
    # overlay), so per 46.4 its small area counts and it is deprioritised behind
    # the large plain window 24, which is walked first and wins. The monitor
    # rect (not the focused window's own rect) is the very-small reference.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        23: [el("Cancel")],  # small + style overlay -> deprioritised
        24: [el("Cancel")],  # large plain -> walked first, wins
    }
    # Small WITH a style overlay flag (46.4 requires the pairing).
    small = fbwin(23, ex_style=WS_EX_NOACTIVATE, rect=(0, 0, 80, 40))
    large = fbwin(24, rect=(0, 0, 1600, 900))
    # Enumerate the small one FIRST to prove ordering (not enumeration order)
    # decides via the monitor-relative very-small + style signal.
    candidates = [small, large]

    def monitor_rect_resolver(_box):
        return (0, 0, 1920, 1080)  # the focused window's MONITOR

    finder, walk = make_fallback_finder(
        trees,
        lambda: list(candidates),
        monitor_resolver=lambda _b: 1,
        monitor_rect_resolver=monitor_rect_resolver,
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused, hwnd=1000),
    )
    assert result.outcome.outcome == "ok"
    tokens = [c[0] for c in walk.calls]
    # Large window 24 walked before the small overlay 23; 24 wins so 23 is
    # never reached.
    assert tokens == [focused, 24]


def test_fallback_small_plain_dialog_keeps_zorder():
    # Finding 46.4: a SMALL PLAIN dialog (no overlay style flags) is NOT
    # deprioritised on area alone. The small dialog 27 is enumerated FIRST and a
    # large app window 28 second; both carry "Cancel". Because the small dialog
    # has no style overlay, it keeps its enumeration/z-order position and is
    # walked first -- and wins.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        27: [el("Cancel")],  # small PLAIN dialog -> keeps position
        28: [el("Cancel")],
    }
    small_plain = fbwin(27, ex_style=0, rect=(0, 0, 322, 322))  # ~9% but plain
    large_app = fbwin(28, ex_style=0, rect=(0, 0, 1600, 900))
    candidates = [small_plain, large_app]

    def monitor_rect_resolver(_box):
        return (0, 0, 1920, 1080)

    finder, walk = make_fallback_finder(
        trees,
        lambda: list(candidates),
        monitor_resolver=lambda _b: 1,
        monitor_rect_resolver=monitor_rect_resolver,
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused, hwnd=1000),
    )
    assert result.outcome.outcome == "ok"
    tokens = [c[0] for c in walk.calls]
    # The small plain dialog 27 keeps its position and is walked first; it wins
    # so 28 is never reached.
    assert tokens == [focused, 27]


# ---------------------------------------------------------------------------
# FINDING 46.1: a non-stale exception (from score_hook / decide), not the
# stale-handle class, must PROPAGATE out of find() rather than be turned into
# not_found.
# ---------------------------------------------------------------------------

def test_fallback_non_stale_exception_propagates():
    import pytest

    # The candidate WALK succeeds, but the score_hook raises a programming-error
    # type (ValueError). That is NOT the stale-handle class, so it must escape
    # find() rather than being swallowed into not_found.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        29: [el("Cancel")],
    }
    candidates = [fbwin(29, rect=(0, 0, 800, 600))]

    finder, _ = make_fallback_finder(
        trees, lambda: candidates, monitor_resolver=lambda _b: 1
    )

    # Monkeypatch the finder's score-hook factory so the FOCUSED walk succeeds
    # (first invocation returns []), driving not_found and triggering the
    # fall-back; the FALL-BACK walk's score_hook (second invocation) then raises
    # a non-stale ValueError, which must propagate out of find().
    calls = {"n": 0}

    def boom_hook(matches):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # focused walk -> no matches -> not_found -> fall-back
        raise ValueError("score_hook programming error")

    finder._make_score_hook = lambda _query: boom_hook  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="score_hook programming error"):
        finder.find(
            ElementQuery("cancel", "button", None, None, "cancel"),
            fg_focused(focused),
        )
    # Proof the fall-back walk (second invocation) is where it raised.
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# FINDING 46.2: the focused monitor anchors to the focused WINDOW's rect (via
# the injected focused-window-rect resolver), never the cursor's monitor; and
# when it cannot be resolved the same-monitor fall-back fails closed.
# ---------------------------------------------------------------------------

def test_fallback_focused_monitor_unresolved_fails_closed():
    # The focused HWND is absent from the enumeration AND the focused-window-rect
    # resolver returns None -> the focused monitor is unresolved. With
    # off-monitor disabled (default), the fall-back must NOT run (not_found),
    # rather than walking the cursor's monitor.
    focused = object()
    trees = {
        focused: [el("Nothing")],
        30: [el("Cancel")],  # would win IF the fall-back ran
    }
    candidates = [fbwin(30, rect=(0, 0, 800, 600))]

    finder, walk = make_fallback_finder(
        trees,
        lambda: candidates,
        monitor_resolver=lambda _b: 1,
        focused_window_rect_resolver=lambda _hwnd: None,  # cannot resolve
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused, hwnd=1000),
    )
    assert result.outcome.outcome == "not_found"
    # No fall-back window was walked -- only the focused window.
    tokens = [c[0] for c in walk.calls]
    assert tokens == [focused]


def test_fallback_offmonitor_enabled_runs_when_focused_monitor_unresolved():
    # Same unresolved-monitor situation, but with off-monitor ENABLED the
    # monitor restriction does not apply, so the fall-back still walks all
    # candidates (order_candidates tolerates a None monitor rect).
    focused = object()
    trees = {
        focused: [el("Nothing")],
        31: [el("Cancel")],
    }
    candidates = [fbwin(31, rect=(0, 0, 800, 600))]

    finder, walk = make_fallback_finder(
        trees,
        lambda: candidates,
        monitor_resolver=lambda _b: 1,
        focused_window_rect_resolver=lambda _hwnd: None,
        monitor_rect_resolver=lambda _box: None,  # no monitor resolvable
        enable_offmonitor_fallback=True,
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"),
        fg_focused(focused, hwnd=1000),
    )
    assert result.outcome.outcome == "ok"
    assert result.outcome.winner is not None
    assert result.outcome.winner.name == "Cancel"
    tokens = [c[0] for c in walk.calls]
    assert tokens == [focused, 31]


def test_overlay_walk_opts_into_transient_retry():
    # The overlay's PRIMARY focused-window walk must opt into the transient
    # stale-window retry; a regression that dropped the opt-in would bring back
    # the browser focus-follow failure (wh-overlay-walk-com-retry). The
    # owned-popup walk must NOT opt in -- covered by the walk_window default-off
    # test and the popup-skip tests; here the popup walk is stubbed to empty.
    captured: dict[str, Any] = {"primary_kwargs": None}
    top = FakeArrayTopLevel([el("Cancel")])

    def recording_walk(top_level, **kwargs):
        # The primary focused-window walk is the FIRST (and only) _walk_fn call
        # in overlay_walk; record its kwargs, then drive the real walk_window.
        if captured["primary_kwargs"] is None:
            captured["primary_kwargs"] = dict(kwargs)
        kwargs.pop("automation", None)
        return uia_walker.walk_window(
            top_level, automation=FakeAutomation(), **kwargs
        )

    finder = ElementFinder(
        walk_fn=recording_walk,
        popup_walk_fn=lambda h, **k: [],
        dpi_resolver=fixed_dpi_resolver,
        monitor_resolver=zero_monitor_resolver,
        window_enumerator=lambda: [],
    )
    finder.overlay_walk(fg_top(top))

    assert captured["primary_kwargs"] is not None, "primary walk never ran"
    assert (
        captured["primary_kwargs"].get("transient_retries")
        == uia_walker.WALK_TRANSIENT_RETRY_ATTEMPTS
    )


# ---------------------------------------------------------------------------
# Near-identical-rect duplicate collapse (wh-overlay-nested-dupes).
#
# The walker numbers BOTH a container control and its interactive descendant
# (no identity / rect / ancestry dedup anywhere), so one visual control gets
# two badges -- a ListItem and the Hyperlink filling it, a Chromium wrapper
# div and the real link inside it. The overlay path collapses the pair when
# the inner element covers (nearly) the outer's whole area: two clickable
# elements occupying the same pixels are ONE visual target. Deliberately
# conservative: a tab's small close button (a few percent of the tab's area)
# keeps its own badge -- tab-click and close-click are different actions.
# Overlay-only: the by-name find() path never collapses.
# ---------------------------------------------------------------------------

def _cm(
    bounds,
    *,
    n=1,
    name="ctl",
    control_type_id=UIA_BUTTON,
    source_window_hwnd=0,
    invoke_supported=True,
):
    """Build a minimal ElementMatch for the pure collapse-function tests."""
    from ui.element_types import ElementMatch

    return ElementMatch(
        item_id=f"uia-{n}",
        display_number=n,
        name=name,
        role="button",
        bounds=bounds,
        monitor_id=0,
        score=1.0,
        is_eligible=True,
        source="uia",
        invoke_supported=invoke_supported,
        is_enabled=True,
        control_ref=object(),
        control_type_id=control_type_id,
        source_window_hwnd=source_window_hwnd,
    )


def test_collapse_drops_container_with_identical_rect_inner():
    from ui.element_finder import collapse_near_identical_containers

    outer = _cm((0, 0, 200, 20), n=1, name="Row", control_type_id=UIA_LISTITEM)
    inner = _cm((0, 0, 200, 20), n=2, name="Open", control_type_id=UIA_HYPERLINK)
    survivors = collapse_near_identical_containers([outer, inner])
    assert survivors == [inner]


def test_collapse_drops_container_when_inner_covers_ninety_percent():
    from ui.element_finder import collapse_near_identical_containers

    # Outer 200x20 = 4000; inner 190x19 = 3610 -> ratio 0.9025 >= 0.90.
    outer = _cm((0, 0, 200, 20), n=1)
    inner = _cm((5, 0, 190, 19), n=2)
    survivors = collapse_near_identical_containers([outer, inner])
    assert survivors == [inner]


def test_collapse_keeps_container_with_small_corner_inner():
    from ui.element_finder import collapse_near_identical_containers

    # The tab + close-button case: the inner covers ~4% of the outer. Both are
    # real, distinct actions (activate tab vs close tab) -> both keep badges.
    tab = _cm((0, 0, 240, 36), n=1, name="Tab")
    close = _cm((220, 10, 16, 16), n=2, name="Close")
    survivors = collapse_near_identical_containers([tab, close])
    assert survivors == [tab, close]


def test_collapse_keeps_pair_just_below_area_threshold():
    from ui.element_finder import collapse_near_identical_containers

    # Outer 200x20 = 4000; inner 170x20 = 3400 -> ratio 0.85 < 0.90: keep both.
    # Distinct names on purpose: this test probes ONLY the rule-1 area
    # boundary. Same-named stacked pairs now collapse via rule 3 regardless
    # of the area ratio (round 2), which the same-name tests below cover.
    outer = _cm((0, 0, 200, 20), n=1, name="Row")
    inner = _cm((15, 0, 170, 20), n=2, name="Open")
    survivors = collapse_near_identical_containers([outer, inner])
    assert survivors == [outer, inner]


def test_collapse_never_crosses_source_window_boundary():
    from ui.element_finder import collapse_near_identical_containers

    # A menu item painted OVER a page control can share its rectangle, but the
    # two live in different walked windows (primary hwnd sentinel 0 vs the
    # popup's hwnd). Pre-order ancestry only holds WITHIN one walked subtree,
    # so the collapse must never pair matches across source windows.
    page_ctl = _cm((0, 0, 200, 20), n=1, source_window_hwnd=0)
    menu_item = _cm((0, 0, 200, 20), n=2, source_window_hwnd=555)
    survivors = collapse_near_identical_containers([page_ctl, menu_item])
    assert survivors == [page_ctl, menu_item]


def test_collapse_chain_keeps_only_innermost():
    from ui.element_finder import collapse_near_identical_containers

    # Chromium wrapper chains: wrapper > wrapper > link, all (nearly) the same
    # rect. Every container that near-identically encloses a LATER match drops,
    # judged against the ORIGINAL list (not survivors), so the whole chain
    # collapses to the innermost leaf in one pass. The leaf is 198x19 = 3762
    # inside 200x20 = 4000 -> ratio 0.9405 >= 0.90 at each link.
    a = _cm((0, 0, 200, 20), n=1)
    b = _cm((0, 0, 200, 20), n=2)
    c = _cm((1, 0, 198, 19), n=3)
    survivors = collapse_near_identical_containers([a, b, c])
    assert survivors == [c]


def test_collapse_ignores_zero_area_rects():
    from ui.element_finder import collapse_near_identical_containers

    # Degenerate rects never participate in containment (same contract as the
    # browser-fold geometry): a zero-area match neither collapses others nor
    # is collapsed, so the pass cannot invent a drop from meaningless geometry.
    degenerate = _cm((0, 0, 0, 0), n=1)
    real = _cm((0, 0, 200, 20), n=2)
    survivors = collapse_near_identical_containers([degenerate, real])
    assert survivors == [degenerate, real]


def test_collapse_preserves_input_order_of_survivors():
    from ui.element_finder import collapse_near_identical_containers

    first = _cm((0, 0, 50, 20), n=1)
    outer = _cm((100, 0, 200, 20), n=2)
    inner = _cm((100, 0, 200, 20), n=3)
    last = _cm((400, 0, 50, 20), n=4)
    survivors = collapse_near_identical_containers([first, outer, inner, last])
    assert survivors == [first, inner, last]


def test_overlay_walk_collapses_same_rect_container_and_renumbers():
    # End-to-end: a non-browser walk yields ListItem + same-rect Hyperlink +
    # a separate Button. The overlay must badge TWO controls (the link and the
    # button), renumbered 1..2 with regenerated item_ids, in both the summary
    # the GUI paints and the stored snapshot click_snapshot_item resolves
    # against.
    finder = make_multi_finder()
    top = FakeArrayTopLevel([
        el("Row", control_type=UIA_LISTITEM, role="list item",
           rect=FakeRect(0, 0, 200, 20)),
        el("Open item", control_type=UIA_HYPERLINK, role="hyperlink",
           rect=FakeRect(0, 0, 200, 20)),
        el("Save", rect=FakeRect(300, 0, 350, 20)),
    ])
    result = _overlay_walk(finder, top, process_name="notepad.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    items = result.summary.items
    assert [i.name for i in items] == ["Open item", "Save"]
    assert [i.display_number for i in items] == [1, 2]
    assert [i.item_id for i in items] == ["uia-1", "uia-2"]
    assert result.snapshot is not None
    matches = result.snapshot.matches
    assert [m.display_number for m in matches] == [1, 2]
    assert [m.item_id for m in matches] == ["uia-1", "uia-2"]


def test_find_does_not_collapse_same_rect_container():
    # Guard: the collapse is OVERLAY-ONLY. The by-name find() path keeps the
    # container AND its same-rect inner control -- the scorer and clear-winner
    # rule must keep seeing both (a spoken name may match the container, and
    # find()'s stored snapshot holds the ELIGIBLE matches for the query, so
    # both same-name same-rect elements must survive to that snapshot).
    top = FakeArrayTopLevel([
        el("Open item", control_type=UIA_LISTITEM, role="list item",
           rect=FakeRect(0, 0, 200, 20)),
        el("Open item", control_type=UIA_HYPERLINK, role="hyperlink",
           rect=FakeRect(0, 0, 200, 20)),
        el("Save", rect=FakeRect(300, 0, 350, 20)),
    ])
    finder, _ = make_finder(top)
    result = finder.find(
        ElementQuery("open item", None, None, None, "open item"), fg(),
    )
    same_rect = [m for m in result.snapshot.matches if m.name == "Open item"]
    assert len(same_rect) == 2


def test_overlay_walk_drops_details_view_row_cells_and_renumbers():
    # End-to-end round 3: a File Explorer Details-view row is a clickable
    # ListItem (Invoke) that contains four passive Edit cells (Name / Date /
    # Type / Size, no Invoke) -- the Size cell overhangs the row's right edge
    # by 12px, exactly as the real control does. The overlay must badge the ROW
    # once, not the row plus four cells, while a separate toolbar button keeps
    # its own badge. Survivors renumber 1..2 in both the painted summary and
    # the stored snapshot the click resolver uses.
    #
    # The model is pinned to a live inspect of a real Windows 11 Details view
    # (2026-07-03): every row exposes pat=Invoke,Legacy,SelectionItem,Toggle
    # and is named with the file value; every cell is an Edit named after its
    # COLUMN (Name / Date modified / Type / Size), no Invoke, samename=False vs
    # the row. So the same-name and unnamed-wrapper rules in the earlier pass
    # never fire on this shape (they would otherwise drop the row), and the
    # row's Invoke satisfies this pass's container check -- both verified here
    # by asserting the ROW, not a cell, is the survivor.
    finder = make_multi_finder()
    top = FakeArrayTopLevel([
        el("report.pdf", control_type=UIA_LISTITEM, role="list item",
           rect=FakeRect(891, 490, 3273, 556), invoke_supported=True),
        el("Name", control_type=UIA_EDIT, role="edit",
           rect=FakeRect(1003, 490, 2175, 556), invoke_supported=False),
        el("Date modified", control_type=UIA_EDIT, role="edit",
           rect=FakeRect(2175, 490, 2607, 556), invoke_supported=False),
        el("Type", control_type=UIA_EDIT, role="edit",
           rect=FakeRect(2607, 490, 2967, 556), invoke_supported=False),
        el("Size", control_type=UIA_EDIT, role="edit",
           rect=FakeRect(2967, 490, 3285, 556), invoke_supported=False),
        el("New", control_type=UIA_BUTTON, role="button",
           rect=FakeRect(20, 20, 120, 60), invoke_supported=True),
    ])
    result = _overlay_walk(finder, top, process_name="explorer.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    items = result.summary.items
    assert [i.name for i in items] == ["report.pdf", "New"]
    assert [i.display_number for i in items] == [1, 2]
    assert [i.item_id for i in items] == ["uia-1", "uia-2"]
    assert result.snapshot is not None
    matches = result.snapshot.matches
    assert [m.display_number for m in matches] == [1, 2]
    assert [m.item_id for m in matches] == ["uia-1", "uia-2"]


def test_overlay_walk_browser_keeps_passive_edit_in_action_container():
    # Round 3 gate (reviewer_0 finding 3): the passive-cell collapse is
    # NATIVE-only. On a browser walk a web <input> is an Edit with no Invoke; if
    # it sits inside a named, clickable container (a labeled clickable div) the
    # native-table rule would otherwise drop the input's badge -- but an input
    # is exactly what a hands-free user wants to click to start typing. Browser
    # nesting is handled by the wrapper fold rules, so the pass must not run on
    # browser walks: BOTH the container and the input keep their badges.
    finder = make_multi_finder()
    top = FakeArrayTopLevel([
        el("Search box", control_type=UIA_GROUP, role="group",
           rect=FakeRect(0, 0, 200, 100), invoke_supported=True),
        el("query", control_type=UIA_EDIT, role="edit",
           rect=FakeRect(10, 10, 130, 60), invoke_supported=False),
    ])
    result = _overlay_walk(finder, top, process_name="chrome.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    items = result.summary.items
    assert [i.name for i in items] == ["Search box", "query"]
    assert [i.display_number for i in items] == [1, 2]


def test_overlay_walk_same_named_row_keeps_the_row_not_the_passive_cell():
    # reviewer_2 (codex) finding: on a native grid whose ROW shares its
    # accessible name with a contained passive cell (a WPF DataGrid row named
    # after its primary column value, or any list row whose first cell carries
    # the row's text), the earlier same-name rule in
    # collapse_near_identical_containers would drop the clickable ROW and keep
    # the passive Edit cell -- the badge then points at a control with no
    # action. The passive-cell collapse must run BEFORE the near-identical pass
    # on native walks so the cell is dropped first and the actionable row
    # survives. Only the row (Invoke) may carry a badge here.
    finder = make_multi_finder()
    top = FakeArrayTopLevel([
        el("report.pdf", control_type=UIA_LISTITEM, role="list item",
           rect=FakeRect(100, 100, 900, 140), invoke_supported=True),
        el("report.pdf", control_type=UIA_EDIT, role="edit",
           rect=FakeRect(110, 105, 410, 135), invoke_supported=False),
        el("12 KB", control_type=UIA_EDIT, role="edit",
           rect=FakeRect(420, 105, 600, 135), invoke_supported=False),
    ])
    result = _overlay_walk(finder, top, process_name="explorer.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    items = result.summary.items
    assert [i.name for i in items] == ["report.pdf"]
    assert [i.display_number for i in items] == [1]
    assert result.snapshot is not None
    matches = result.snapshot.matches
    assert [m.invoke_supported for m in matches] == [True]


# ---------------------------------------------------------------------------
# wh-overlay-nested-dupes ROUND 2 (Gmail left-nav double badges, 2026-07-03).
#
# Live-window evidence (Brave/Gmail walk): every left-nav row is an UNNAMED
# invoke-capable Chromium Group (819x83) wrapping the real named link
# (160x61) -- 14% coverage, so the >=90% area rule keeps both and one visual
# row gets two badges. Gmail's mail rows additionally duplicate one visual
# control across a grid cell and the control inside it with the SAME
# accessible name ('Not starred' item + 'Not starred' button). Two new drop
# rules, both judged on overlap share = intersection area / LATER element's
# area >= 0.5 (share, not strict containment: Chromium reports the search
# box sticking 21px out the bottom of its wrapper):
#
#   A. an EARLIER clickable with an empty/whitespace name that a later
#      clickable lies at least half inside is a wrapper div -> drop it.
#   B. an EARLIER clickable whose name equals a later at-least-half-inside
#      clickable's name (strip+casefold, non-empty) is the same visual
#      target -> drop it.
#
# A named container with a DIFFERENT-named small child keeps both badges
# (tab + close button; Gmail's 'Select' button + its unnamed checkbox).
# Geometry in these tests is copied from the live walk.
# ---------------------------------------------------------------------------


def test_collapse_drops_unnamed_wrapper_around_named_link():
    from ui.element_finder import collapse_near_identical_containers

    wrapper = _cm((12, 843, 819, 83), n=1, name="", control_type_id=UIA_GROUP)
    link = _cm(
        (230, 854, 160, 61), n=2, name="Starred", control_type_id=UIA_HYPERLINK
    )
    survivors = collapse_near_identical_containers([wrapper, link])
    assert survivors == [link]


def test_collapse_drops_whitespace_named_wrapper():
    from ui.element_finder import collapse_near_identical_containers

    wrapper = _cm((12, 843, 819, 83), n=1, name="  ", control_type_id=UIA_GROUP)
    link = _cm(
        (230, 854, 160, 61), n=2, name="Starred", control_type_id=UIA_HYPERLINK
    )
    survivors = collapse_near_identical_containers([wrapper, link])
    assert survivors == [link]


def test_collapse_drops_unnamed_wrapper_with_overhanging_inner():
    # Live geometry: the Gmail search-bar wrapper (1072,380,1555,96) and the
    # 'Ask Gmail' edit (1072,428,1555,69), which sticks 21px out the bottom.
    # Strict containment misses it; the overlap share is 48/69 = 0.696.
    from ui.element_finder import collapse_near_identical_containers

    wrapper = _cm(
        (1072, 380, 1555, 96), n=1, name="", control_type_id=UIA_GROUP
    )
    edit = _cm(
        (1072, 428, 1555, 69), n=2, name="Ask Gmail",
        control_type_id=UIA_BUTTON,
    )
    survivors = collapse_near_identical_containers([wrapper, edit])
    assert survivors == [edit]


def test_collapse_keeps_unnamed_element_with_slight_overlap():
    # Overlap share below 0.5 (10x10 of a 100x100 later element = 0.01):
    # two distinct controls with sloppy bounds, not a wrapper pair.
    from ui.element_finder import collapse_near_identical_containers

    unnamed = _cm((0, 0, 100, 100), n=1, name="")
    neighbour = _cm((90, 90, 100, 100), n=2, name="Save")
    survivors = collapse_near_identical_containers([unnamed, neighbour])
    assert survivors == [unnamed, neighbour]


def test_collapse_keeps_unnamed_element_with_no_overlap():
    from ui.element_finder import collapse_near_identical_containers

    unnamed = _cm((0, 0, 69, 70), n=1, name="", control_type_id=UIA_CHECKBOX)
    distant = _cm((300, 0, 100, 70), n=2, name="Archive")
    survivors = collapse_near_identical_containers([unnamed, distant])
    assert survivors == [unnamed, distant]


def test_collapse_keeps_named_container_with_unnamed_inner():
    # Live geometry: Gmail's 'Select' button (checkbox + dropdown arrow)
    # wraps an unnamed checkbox. Distinct actions; both badges stay.
    from ui.element_finder import collapse_near_identical_containers

    select = _cm((939, 618, 137, 70), n=1, name="Select")
    checkbox = _cm(
        (939, 618, 69, 70), n=2, name="", control_type_id=UIA_CHECKBOX
    )
    survivors = collapse_near_identical_containers([select, checkbox])
    assert survivors == [select, checkbox]


def test_collapse_drops_same_name_cell_around_control():
    # Live geometry: 'Not starred' grid cell (104x70) wrapping the
    # 'Not starred' button (69x70) -- one visual star, one badge.
    from ui.element_finder import collapse_near_identical_containers

    cell = _cm(
        (1041, 1109, 104, 70), n=1, name="Not starred",
        control_type_id=UIA_LISTITEM,
    )
    button = _cm((1041, 1109, 69, 70), n=2, name="Not starred")
    survivors = collapse_near_identical_containers([cell, button])
    assert survivors == [button]


def test_collapse_same_name_rule_is_case_and_whitespace_insensitive():
    from ui.element_finder import collapse_near_identical_containers

    cell = _cm(
        (1041, 1109, 104, 70), n=1, name="Not Starred ",
        control_type_id=UIA_LISTITEM,
    )
    button = _cm((1041, 1109, 69, 70), n=2, name="not starred")
    survivors = collapse_near_identical_containers([cell, button])
    assert survivors == [button]


def test_collapse_keeps_same_name_non_overlapping_siblings():
    # Two 'Delete' buttons on adjacent rows: same name, zero overlap --
    # distinct real controls, both keep their badges.
    from ui.element_finder import collapse_near_identical_containers

    first = _cm((0, 0, 50, 20), n=1, name="Delete")
    second = _cm((0, 30, 50, 20), n=2, name="Delete")
    survivors = collapse_near_identical_containers([first, second])
    assert survivors == [first, second]


def test_collapse_same_name_chain_keeps_innermost():
    # Live shape: the mail row (named with its full row text) wraps a grid
    # cell with the SAME name, which wraps the checkbox with the SAME name.
    # The chain collapses to the innermost control in one pass.
    from ui.element_finder import collapse_near_identical_containers

    row_text = "me , WheelHouse STT scan"
    row = _cm(
        (884, 1096, 2691, 96), n=1, name=row_text,
        control_type_id=UIA_LISTITEM,
    )
    cell = _cm(
        (895, 1109, 147, 70), n=2, name=row_text,
        control_type_id=UIA_LISTITEM,
    )
    checkbox = _cm(
        (939, 1109, 69, 70), n=3, name=row_text,
        control_type_id=UIA_CHECKBOX,
    )
    survivors = collapse_near_identical_containers([row, cell, checkbox])
    assert survivors == [checkbox]


def test_collapse_new_rules_respect_window_boundary():
    # A wrapper in one walked window never pairs with a control painted
    # over it from another walked window (popup over page).
    from ui.element_finder import collapse_near_identical_containers

    wrapper = _cm(
        (12, 843, 819, 83), n=1, name="", control_type_id=UIA_GROUP,
        source_window_hwnd=111,
    )
    popup_item = _cm(
        (230, 854, 160, 61), n=2, name="Paste", source_window_hwnd=222
    )
    survivors = collapse_near_identical_containers([wrapper, popup_item])
    assert survivors == [wrapper, popup_item]


# ---------------------------------------------------------------------------
# Round 3: passive-cell collapse for native tables/lists (wh-overlay-nested-dupes).
#
# The round-1 area rule and the round-2 wrapper rules both KEEP the inner
# element and drop the outer. They do nothing for a Windows Details-view row:
# the row is a clickable ListItem that fully contains four smaller,
# differently-named, NON-clickable cells (Name/Date/Type/Size -- Edit controls
# with no Invoke), so each row still gets five numbers.
# collapse_passive_cells_in_action_containers drops the number on a passive
# cell -- a text cell, label, or icon with no click action of its own -- when
# it sits fully inside a clearly larger clickable container. It keeps genuine
# nested targets (a small close/filter button with its own Invoke) and leaves
# the same-size-wrapper shape to collapse_near_identical_containers.
# ---------------------------------------------------------------------------

def test_cell_collapse_drops_details_view_row_cells():
    from ui.element_finder import collapse_passive_cells_in_action_containers

    row = _cm((891, 490, 2382, 66), n=1, name="report.pdf",
              control_type_id=UIA_LISTITEM, invoke_supported=True)
    name_cell = _cm((1003, 490, 1172, 66), n=2, name="Name",
                    control_type_id=UIA_EDIT, invoke_supported=False)
    date_cell = _cm((2175, 490, 432, 66), n=3, name="Date modified",
                    control_type_id=UIA_EDIT, invoke_supported=False)
    type_cell = _cm((2607, 490, 360, 66), n=4, name="Type",
                    control_type_id=UIA_EDIT, invoke_supported=False)
    size_cell = _cm((2967, 490, 318, 66), n=5, name="Size",
                    control_type_id=UIA_EDIT, invoke_supported=False)
    survivors = collapse_passive_cells_in_action_containers(
        [row, name_cell, date_cell, type_cell, size_cell]
    )
    assert survivors == [row]


def test_cell_collapse_drops_dataitem_cell():
    from ui.element_finder import collapse_passive_cells_in_action_containers

    # A data-grid variant: the cells are DataItem, not Edit. Same shape, same
    # drop.
    row = _cm((0, 0, 1000, 30), n=1, name="row 1",
              control_type_id=UIA_LISTITEM, invoke_supported=True)
    cell = _cm((0, 0, 300, 30), n=2, name="col A",
               control_type_id=UIA_DATAITEM, invoke_supported=False)
    survivors = collapse_passive_cells_in_action_containers([row, cell])
    assert survivors == [row]


def test_cell_collapse_keeps_clickable_inner_button():
    from ui.element_finder import collapse_passive_cells_in_action_containers

    # A column header's Filter dropdown / a tab's Close button: contained, but
    # it has its own Invoke -> a distinct action -> keep both.
    header = _cm((849, 390, 1326, 82), n=1, name="Name", invoke_supported=True)
    filter_btn = _cm((2127, 390, 45, 82), n=2, name="Filter dropdown",
                     control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_passive_cells_in_action_containers([header, filter_btn])
    assert survivors == [header, filter_btn]


def test_cell_collapse_keeps_cell_when_container_is_not_a_row_type():
    from ui.element_finder import collapse_passive_cells_in_action_containers

    # reviewer_1 (deepseek) finding: an unrecognized Chromium app walks as a
    # native window, and a <div role="button"> wrapping an <input> exposes as a
    # Button (Invoke) containing an Edit (no Invoke). The Button is a real click
    # target but it is NOT a list/grid ROW, so the Edit is a standalone input,
    # not a column cell -- it must keep its number. Only a genuine row container
    # (ListItem / DataItem) collapses its cells; a Button never does.
    button = _cm((0, 0, 200, 100), n=1, name="Search",
                 control_type_id=UIA_BUTTON, invoke_supported=True)
    edit = _cm((10, 10, 120, 50), n=2, name="query",
               control_type_id=UIA_EDIT, invoke_supported=False)
    survivors = collapse_passive_cells_in_action_containers([button, edit])
    assert survivors == [button, edit]


def test_cell_collapse_keeps_same_size_inner():
    from ui.element_finder import collapse_passive_cells_in_action_containers

    # Address-bar shape: a passive Edit that FILLS its clickable container
    # (>=90% area). collapse_near_identical_containers owns that pair and keeps
    # the inner element; this pass must NOT also act, or one pair is counted
    # twice.
    group = _cm((603, 125, 2159, 99), n=1, name="",
                control_type_id=UIA_GROUP, invoke_supported=True)
    edit = _cm((603, 125, 2159, 99), n=2, name="Address Bar",
               control_type_id=UIA_EDIT, invoke_supported=False)
    survivors = collapse_passive_cells_in_action_containers([group, edit])
    assert survivors == [group, edit]


def test_cell_collapse_keeps_cell_when_container_not_clickable():
    from ui.element_finder import collapse_passive_cells_in_action_containers

    # A passive Edit inside a container with NO click action of its own is a
    # standalone field, not a cell of a clickable item -> keep it.
    box = _cm((0, 0, 400, 200), n=1, name="panel",
              control_type_id=UIA_LISTITEM, invoke_supported=False)
    field = _cm((10, 10, 100, 30), n=2, name="Search",
                control_type_id=UIA_EDIT, invoke_supported=False)
    survivors = collapse_passive_cells_in_action_containers([box, field])
    assert survivors == [box, field]


def test_cell_collapse_keeps_tree_items_stacked_below_parent():
    from ui.element_finder import collapse_passive_cells_in_action_containers

    # Folder tree: a child TreeItem is drawn BELOW its parent, not inside it.
    # It is a real, separate target -> keep both. (TreeItem is not a passive
    # cell type either, so this is safe twice over.)
    parent = _cm((129, 1266, 137, 72), n=1, name="This PC",
                 control_type_id=UIA_TREEITEM, invoke_supported=False)
    child = _cm((153, 1338, 341, 72), n=2, name="Local Disk (C:)",
                control_type_id=UIA_TREEITEM, invoke_supported=False)
    survivors = collapse_passive_cells_in_action_containers([parent, child])
    assert survivors == [parent, child]


def test_cell_collapse_respects_window_boundary():
    from ui.element_finder import collapse_passive_cells_in_action_containers

    # A cell painted from a different walked window never pairs with a row.
    row = _cm((0, 0, 200, 20), n=1, name="row",
              control_type_id=UIA_LISTITEM, invoke_supported=True,
              source_window_hwnd=0)
    cell = _cm((0, 0, 80, 20), n=2, name="cell",
               control_type_id=UIA_EDIT, invoke_supported=False,
               source_window_hwnd=999)
    survivors = collapse_passive_cells_in_action_containers([row, cell])
    assert survivors == [row, cell]


# ---------------------------------------------------------------------------
# Unnamed invoke-only duplicate collapse (wh-overlay-browser-dupes).
#
# x.com live evidence (2026-07-05, Brave): Chromium exposes each tweet
# engagement action TWICE -- a named Button ("2 Replies. Reply") and, AFTER it
# in tree order, an unnamed 136x136 Group carrying only InvokePattern (the
# circular hover region around the icon). The two rectangles only PARTIALLY
# overlap (the button sticks out past the circle's right edge), so none of
# the near-identical rules fire: no full containment (rule 1), the unnamed
# element is the LATER one (rule 2 drops earlier only), names differ
# (rule 3). Result: two badges per button. This pass drops the unnamed
# invoke-only duplicate in EITHER tree order, keeping the named control.
# Real overlap shares of the smaller rectangle, from the live walk: reply
# 0.78, like 0.67, views 0.52, bookmark/share/avatar 1.0; genuinely adjacent
# controls score near 0.
# ---------------------------------------------------------------------------

def test_unnamed_dup_drops_trailing_unnamed_circle_partially_over_named_button():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # Real x.com reply pair: named button first, unnamed invoke Group second,
    # partial overlap (intersection 8190 = 0.78 of the button's 10452).
    button = _cm((912, 1252, 134, 78), n=1, name="2 Replies. Reply",
                 control_type_id=UIA_BUTTON, invoke_supported=True)
    circle = _cm((881, 1223, 136, 136), n=2, name="",
                 control_type_id=UIA_GROUP, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [button, circle]
    )
    assert survivors == [button]


def test_unnamed_dup_drops_leading_unnamed_circle_too():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # Same pair, reversed tree order: the drop must not depend on which side
    # comes first (the near-identical pass is earlier-only; this one is not).
    circle = _cm((881, 1223, 136, 136), n=1, name="",
                 control_type_id=UIA_GROUP, invoke_supported=True)
    button = _cm((912, 1252, 134, 78), n=2, name="2 Replies. Reply",
                 control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [circle, button]
    )
    assert survivors == [button]


def test_unnamed_dup_drops_circle_fully_containing_small_named_button():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # Real x.com "Grok actions" pair: the unnamed hover circle fully CONTAINS
    # the small named button (share of smaller = 1.0). Rule 2 of the
    # near-identical pass misses it because the unnamed element is LATER.
    button = _cm((2742, 810, 76, 78), n=1, name="Grok actions",
                 control_type_id=UIA_BUTTON, invoke_supported=True)
    circle = _cm((2711, 781, 138, 136), n=2, name="",
                 control_type_id=UIA_GROUP, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [button, circle]
    )
    assert survivors == [button]


def test_unnamed_dup_keeps_standalone_unnamed_clickable():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # An unnamed invoke-only Group that overlaps NO named control is a real
    # standalone clickable (an icon-only div) -- it keeps its badge.
    lone = _cm((100, 100, 60, 60), n=1, name="",
               control_type_id=UIA_GROUP, invoke_supported=True)
    far_button = _cm((500, 500, 120, 40), n=2, name="Post",
                     control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [lone, far_button]
    )
    assert survivors == [lone, far_button]


def test_unnamed_dup_keeps_unnamed_interactive_typed_control():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # Pinned constraint (Gmail): an unnamed CheckBox inside the named 'Select'
    # button is a real, distinct action. Interactive control types are NEVER
    # drop candidates -- only invoke-only Text/Group/Image scaffolding is.
    select = _cm((0, 0, 100, 40), n=1, name="Select",
                 control_type_id=UIA_BUTTON, invoke_supported=True)
    checkbox = _cm((10, 8, 24, 24), n=2, name="",
                   control_type_id=UIA_CHECKBOX, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [select, checkbox]
    )
    assert survivors == [select, checkbox]


def test_unnamed_dup_keeps_named_group_over_named_button():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # A NAMED Group is never a drop candidate here, whatever it overlaps --
    # named containers stay the near-identical pass's business.
    group = _cm((0, 0, 200, 80), n=1, name="Toolbar",
                control_type_id=UIA_GROUP, invoke_supported=True)
    button = _cm((10, 10, 80, 60), n=2, name="Save",
                 control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [group, button]
    )
    assert survivors == [group, button]


def test_unnamed_dup_requires_named_anchor():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # The overlapped control must be NAMED: two unnamed clickables stacked
    # give the voice user nothing to prefer, so both keep badges (the
    # near-identical pass handles true full-coverage stacks).
    circle = _cm((881, 1223, 136, 136), n=1, name="",
                 control_type_id=UIA_GROUP, invoke_supported=True)
    unnamed_button = _cm((912, 1252, 134, 78), n=2, name="  ",
                         control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [circle, unnamed_button]
    )
    assert survivors == [circle, unnamed_button]


def test_unnamed_dup_never_crosses_window_boundary():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # A popup element painted OVER a page control shares pixels but not a
    # walked window -- never pairs (same contract as the other passes).
    button = _cm((912, 1252, 134, 78), n=1, name="2 Replies. Reply",
                 control_type_id=UIA_BUTTON, invoke_supported=True,
                 source_window_hwnd=0)
    circle = _cm((881, 1223, 136, 136), n=2, name="",
                 control_type_id=UIA_GROUP, invoke_supported=True,
                 source_window_hwnd=999)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [button, circle]
    )
    assert survivors == [button, circle]


def test_unnamed_dup_overlap_threshold_pins_at_forty_percent_of_smaller():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # Both rects 100x100. Anchor at x=61 -> intersection 39% of the smaller:
    # keep. Anchor at x=55 -> 45%: drop. Pins the 0.40 threshold, chosen well
    # under the weakest real duplicate (views pair, 0.52) and far above
    # adjacent-control slop (near 0).
    below = _cm((0, 0, 100, 100), n=1, name="",
                control_type_id=UIA_GROUP, invoke_supported=True)
    anchor_below = _cm((61, 0, 100, 100), n=2, name="Like",
                       control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [below, anchor_below]
    )
    assert survivors == [below, anchor_below]

    above = _cm((0, 0, 100, 100), n=1, name="",
                control_type_id=UIA_GROUP, invoke_supported=True)
    anchor_above = _cm((55, 0, 100, 100), n=2, name="Like",
                       control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [above, anchor_above]
    )
    assert survivors == [anchor_above]


def test_unnamed_dup_ignores_non_invoke_candidates_and_zero_area():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # A no-Invoke unnamed element is not this pass's business (the clickable
    # filter already handles it), and degenerate rectangles never participate
    # on either side of a pair.
    passive = _cm((912, 1252, 134, 78), n=1, name="",
                  control_type_id=UIA_GROUP, invoke_supported=False)
    button = _cm((912, 1252, 134, 78), n=2, name="Reply",
                 control_type_id=UIA_BUTTON, invoke_supported=True)
    zero = _cm((912, 1252, 0, 0), n=3, name="",
               control_type_id=UIA_GROUP, invoke_supported=True)
    zero_anchor = _cm((881, 1223, 136, 0), n=4, name="Like",
                      control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [passive, button, zero, zero_anchor]
    )
    assert survivors == [passive, button, zero, zero_anchor]


def test_overlay_walk_browser_drops_unnamed_invoke_circle_over_named_button():
    # End-to-end (wh-overlay-browser-dupes): on a browser walk the x.com
    # engagement pair -- named Button plus trailing unnamed invoke-only Group
    # hover circle, partial overlap -- must yield ONE badge, on the button,
    # and survivors renumber contiguously.
    finder = make_multi_finder()
    top = FakeArrayTopLevel([
        el("2 Replies. Reply", control_type=UIA_BUTTON, role="button",
           rect=FakeRect(912, 1252, 1046, 1330), invoke_supported=True),
        el("", control_type=UIA_GROUP, role="group",
           rect=FakeRect(881, 1223, 1017, 1359), invoke_supported=True),
        el("Bookmark", control_type=UIA_BUTTON, role="button",
           rect=FakeRect(2729, 1252, 2803, 1330), invoke_supported=True),
        el("", control_type=UIA_GROUP, role="group",
           rect=FakeRect(2698, 1223, 2834, 1359), invoke_supported=True),
    ])
    result = _overlay_walk(finder, top, process_name="chrome.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    items = result.summary.items
    assert [i.name for i in items] == ["2 Replies. Reply", "Bookmark"]
    assert [i.display_number for i in items] == [1, 2]
    assert result.snapshot is not None
    assert [m.name for m in result.snapshot.matches] == [
        "2 Replies. Reply", "Bookmark"
    ]


def test_overlay_walk_native_does_not_run_unnamed_dup_pass():
    # Gate guard: the pass is BROWSER-only. On a native walk an unnamed
    # invoke-capable Group never reaches the collapse at all (the walker's
    # interactive-type filter drops Groups when query_has_role=True), so the
    # named button is the only badge either way -- this pins that a native
    # walk's badge set is unchanged by the new pass.
    finder = make_multi_finder()
    top = FakeArrayTopLevel([
        el("Save", control_type=UIA_BUTTON, role="button",
           rect=FakeRect(100, 100, 200, 140), invoke_supported=True),
        el("", control_type=UIA_GROUP, role="group",
           rect=FakeRect(90, 90, 210, 150), invoke_supported=True),
    ])
    result = _overlay_walk(finder, top, process_name="notepad.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    assert [i.name for i in result.summary.items] == ["Save"]


def test_overlay_walk_browser_still_keeps_edit_inside_clickable_wrapper():
    # Constraint guard: the new unnamed-duplicate pass must not disturb the
    # pinned browser shape -- a web text input (Edit, interactive type) and
    # its named clickable wrapper BOTH keep badges.
    finder = make_multi_finder()
    top = FakeArrayTopLevel([
        el("Search box", control_type=UIA_GROUP, role="group",
           rect=FakeRect(0, 0, 200, 100), invoke_supported=True),
        el("query", control_type=UIA_EDIT, role="edit",
           rect=FakeRect(10, 10, 130, 60), invoke_supported=False),
    ])
    result = _overlay_walk(finder, top, process_name="chrome.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    assert [i.name for i in result.summary.items] == ["Search box", "query"]


def test_unnamed_dup_keeps_icon_inside_named_edit():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # reviewer_0 finding 1: a named Edit never anchors a drop. A web search
    # box often contains an unnamed clear-X / show-password glyph with no
    # aria-label; clicking the Edit focuses the field, it does NOT perform
    # the glyph's action, so the glyph must keep its badge. Sized inside the
    # area-ratio band on purpose so ONLY the Edit exclusion protects it.
    search = _cm((0, 0, 60, 40), n=1, name="Search",
                 control_type_id=UIA_EDIT, invoke_supported=False)
    glyph = _cm((30, 8, 24, 24), n=2, name="",
                control_type_id=UIA_GROUP, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [search, glyph]
    )
    assert survivors == [search, glyph]


def test_unnamed_dup_keeps_large_card_containing_small_named_button():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # reviewer_0 finding 2: share-of-smaller alone has no size-disparity
    # bound. A large unnamed clickable card (delegated click: open the item)
    # containing a small named button of a DIFFERENT action (Buy) is two
    # real targets -- overlap share of the smaller is 1.0 but the areas
    # differ 100x, far past the ratio bound, so both keep badges.
    card = _cm((0, 0, 600, 400), n=1, name="",
               control_type_id=UIA_GROUP, invoke_supported=True)
    buy = _cm((500, 350, 80, 30), n=2, name="Buy",
              control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [card, buy]
    )
    assert survivors == [card, buy]


def test_unnamed_dup_area_ratio_bound_pins_at_six():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # Pins the 6.0 area-ratio bound. Candidate 100x100 (10000). A named
    # button 41x41 (1681) fully inside is ratio 5.95 -> still a duplicate:
    # drop. A named button 40x40 (1600) is ratio 6.25 -> past the bound:
    # keep both. The bound sits far above the strongest real duplicate
    # (bookmark circle/button, ratio 3.2) and far below in-control icons
    # (clear-X in a search box, 20x; a tab's close glyph, 33x).
    circle_a = _cm((0, 0, 100, 100), n=1, name="",
                   control_type_id=UIA_GROUP, invoke_supported=True)
    inside_a = _cm((30, 30, 41, 41), n=2, name="Like",
                   control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [circle_a, inside_a]
    )
    assert survivors == [inside_a]

    circle_b = _cm((0, 0, 100, 100), n=1, name="",
                   control_type_id=UIA_GROUP, invoke_supported=True)
    inside_b = _cm((30, 30, 40, 40), n=2, name="Like",
                   control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [circle_b, inside_b]
    )
    assert survivors == [circle_b, inside_b]


def test_unnamed_dup_named_noninteractive_element_never_anchors():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # reviewer_0 finding 3 (guard cover): the overlapped control must be an
    # interactive TYPE. A named non-interactive Group (x.com's named
    # timeline / region wrappers) stacked with an unnamed invoke Group must
    # not cause a drop -- deleting the anchor type guard would break this.
    named_region = _cm((0, 0, 120, 120), n=1, name="Timeline: Home",
                       control_type_id=UIA_GROUP, invoke_supported=True)
    scaffold = _cm((10, 10, 100, 100), n=2, name="",
                   control_type_id=UIA_GROUP, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [named_region, scaffold]
    )
    assert survivors == [named_region, scaffold]


def test_unnamed_dup_zero_area_anchor_reached_by_real_candidate():
    from ui.element_finder import (
        collapse_unnamed_invoke_duplicates_of_named_controls,
    )

    # reviewer_0 finding 3 (branch cover): a REAL candidate must actually
    # reach the anchor loop and skip a zero-area named control there (the
    # earlier zero-area test never entered the loop).
    candidate = _cm((0, 0, 100, 100), n=1, name="",
                    control_type_id=UIA_GROUP, invoke_supported=True)
    flat_anchor = _cm((0, 0, 100, 0), n=2, name="Like",
                      control_type_id=UIA_BUTTON, invoke_supported=True)
    survivors = collapse_unnamed_invoke_duplicates_of_named_controls(
        [candidate, flat_anchor]
    )
    assert survivors == [candidate, flat_anchor]


def test_overlay_walk_browser_wrapper_still_collapses_when_inner_is_unnamed_dup():
    # reviewer_1 (deepseek) finding: pass-composition regression. When the
    # unnamed-duplicate pass ran FIRST, it could remove the only element
    # that justified the near-identical pass's unnamed-wrapper drop, and
    # the wrapper's badge came back. Geometry (the finding's, with G1
    # grown to 200x1000 so its 6.67x area ratio vs B keeps the
    # unnamed-duplicate pass off G1 itself -- the finding's original
    # 200x250 G1 was 1.67x and got dropped as a duplicate of B, hiding the
    # composition problem): G1 unnamed wrapper; G2 (0,0,200,200) unnamed
    # invoke-only, fully inside G1 (rule-2 trigger); B (120,0,200,150)
    # named Button, 0.40 inside G1 (below rule 2's 0.50) and overlapping
    # G2 at 0.40 of the smaller with area ratio 1.33. Running the
    # near-identical pass FIRST drops G1 via G2; the unnamed-duplicate
    # pass then drops G2 via B. One badge, on the named button -- strictly
    # better than both the regression ([G1, B]) and the old shipped
    # behavior ([G2, B]).
    finder = make_multi_finder()
    top = FakeArrayTopLevel([
        el("", control_type=UIA_GROUP, role="group",
           rect=FakeRect(0, 0, 200, 1000), invoke_supported=True),
        el("", control_type=UIA_GROUP, role="group",
           rect=FakeRect(0, 0, 200, 200), invoke_supported=True),
        el("Send", control_type=UIA_BUTTON, role="button",
           rect=FakeRect(120, 0, 320, 150), invoke_supported=True),
    ])
    result = _overlay_walk(finder, top, process_name="chrome.exe")

    assert result.outcome == "ok"
    assert result.summary is not None
    assert [i.name for i in result.summary.items] == ["Send"]
    assert [i.display_number for i in result.summary.items] == [1]


def test_snapshot_ids_unique_across_finder_instances():
    """Reviewer_0 finding .1.3 (wh-pin-snapshot-contract-break-detection.1.3):
    snapshot ids were walk-{n} from a per-instance counter, so an Input
    process crash/restart re-minted an id the Logic-side pin bookkeeping
    still held, silently defeating the pin contract AND its new break
    detection. Ids must carry a per-instance salt so two finder instances
    (two Input process lifetimes) can never collide.
    """
    kwargs = dict(
        dpi_resolver=fixed_dpi_resolver,
        monitor_resolver=zero_monitor_resolver,
    )
    finder_a = ElementFinder(**kwargs)
    finder_b = ElementFinder(**kwargs)
    assert finder_a._snapshot_salt != finder_b._snapshot_salt
