"""ElementFinder owned-popup orchestration + multi-WalkResult keepalive tests.

wh-n29v.45 -- the classic Win32 #32768 owned-popup walker extension, the
ElementFinder side (scope items 2, 3, 5 in the dispatch):

* ``_StoredSnapshot`` carries ``walk_results: list[WalkResult]`` (was singular);
  all subtree keepalives are pinned together and eviction isolation still holds.
* ``find`` orchestrates the primary + popup walks with the split deadline
  (primary uses the full budget; the popup walk is done only when the primary
  finished within its 0.7 share, else skipped) and the documented merge order
  (focused-window matches first in reading order, popup matches appended in
  their own order, focused badge numbers unchanged).

Driven with fakes -- no live COM, no real display.
"""

import gc
import weakref
from dataclasses import replace
from typing import Any
from unittest.mock import patch

from ui import uia_walker
from ui.browser_dom_corrections import apply_dom_corrections
from ui.element_finder import ElementFinder, ForegroundContext
from ui.element_types import ElementQuery
from ui.uia_walker import (
    CLASSIC_POPUP_CLASS_NAME,
    UIA_BUTTON,
    UIA_GROUP,
    UIA_MENU,
    UIA_MENUITEM,
    UIA_TEXT,
    WalkResult,
    walk_owned_popups,
    walk_window,
)

from tests.test_uia_walker import (
    FakeAutomation,
    FakeCachedElement,
    FakeElementArray,
    FakeRect,
    FakeTopLevel,
)
from tests.test_element_finder import (
    fixed_dpi_resolver,
    make_multi_finder,
    zero_monitor_resolver,
)
from tests.test_element_finder import FakeArrayTopLevel as _MultiTopLevel
from tests.test_element_finder import el as _interactive_el
from tests.test_element_finder import fg_top as _fg_top


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


FOCUSED_HWND = 1000
POPUP_HWND = 2001


def fg(*, top_level=None, process_name="notepad.exe"):
    return ForegroundContext(
        foreground_window=FOCUSED_HWND,
        foreground_pid=4321,
        foreground_process_name=process_name,
        foreground_window_creation_time=99,
        cursor_at_walk=(500, 500),
        cursor_monitor_id=0,
        top_level=top_level,
    )


def make_finder(primary_top_level, *, popup_walk_fn=None, clock=None, **overrides):
    """Build a finder whose primary walk drives the real walk_window over a fake
    tree and whose popup walk is the injected ``popup_walk_fn``."""

    captured: dict[str, Any] = {"walk_kwargs": [], "popup_calls": []}

    def _primary_walk(top_level, **kwargs):
        captured["walk_kwargs"].append(kwargs)
        kwargs.pop("automation", None)
        return walk_window(primary_top_level, automation=FakeAutomation(None),
                           **kwargs)

    def _recording_popup_walk(focused_hwnd, **kwargs):
        captured["popup_calls"].append((focused_hwnd, kwargs))
        if popup_walk_fn is None:
            return []
        return popup_walk_fn(focused_hwnd, **kwargs)

    kwargs: dict[str, Any] = {
        "walk_fn": _primary_walk,
        "popup_walk_fn": _recording_popup_walk,
        "dpi_resolver": fixed_dpi_resolver,
        "monitor_resolver": zero_monitor_resolver,
        "window_enumerator": lambda: [],
    }
    if clock is not None:
        kwargs["clock"] = clock
    kwargs.update(overrides)
    return ElementFinder(**kwargs), captured


def _popup_walkresult(*names, source_window_hwnd=POPUP_HWND):
    """A WalkResult standing in for an owned-popup subtree walk.

    Each match carries source_window_hwnd and a DISTINCT control_ref object so
    keepalive isolation is observable. Matches are eligible+scored so they
    survive the finder's merge (find() scores via the query hook, but the
    popup_walk_fn seam returns already-walked results; the finder re-uses them
    as the popup subtree's matches).
    """
    array = FakeElementArray([])
    # Each item gets its own vertically stacked rect (like a real menu):
    # wh-overlay-nested-dupes made overlay geometry meaningful -- two fakes
    # sharing one rect now read as one visual control and collapse to one
    # badge, which no real pair of distinct menu items can be.
    matches = [
        uia_walker.element_match_from_cached(
            FakeCachedElement(name=n, control_type=UIA_MENUITEM,
                              localized_control_type="menu item",
                              rect=FakeRect(10, 20 + 40 * (i - 1),
                                            110, 50 + 40 * (i - 1))),
            display_number=i,
            source_window_hwnd=source_window_hwnd,
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


# ---------------------------------------------------------------------------
# Merge order: focused subtree first, popups appended, focused numbers stable.
# ---------------------------------------------------------------------------


def test_find_merges_focused_then_popup_in_order():
    # Focused window has two controls that match the query "cancel"; the popup
    # contributes two more. The popup_walk_fn returns its matches verbatim, so
    # the merged snapshot proves the order: focused matches first (reading
    # order), popup matches appended in their own order.
    primary = FakeTopLevel(FakeElementArray([el("Cancel"), el("Cancel All")]))

    def popup_walk(focused_hwnd, **kwargs):
        assert focused_hwnd == FOCUSED_HWND
        return [_popup_walkresult("Cancel item", "Cancel entry")]

    finder, captured = make_finder(primary, popup_walk_fn=popup_walk)
    result = finder.find(
        ElementQuery("cancel", None, None, None, "cancel"), fg()
    )
    names = [m.name for m in result.snapshot.matches]
    # Focused-window matches FIRST (Cancel, Cancel All), then popup matches.
    assert names[:2] == ["Cancel", "Cancel All"]
    assert names[2:] == ["Cancel item", "Cancel entry"]


def test_find_appends_popup_without_renumbering_focused():
    primary = FakeTopLevel(FakeElementArray([el("Cancel"), el("Cancel All")]))

    def popup_walk(focused_hwnd, **kwargs):
        return [_popup_walkresult("Cancel item")]

    finder, _ = make_finder(primary, popup_walk_fn=popup_walk)
    result = finder.find(
        ElementQuery("cancel", None, None, None, "cancel"), fg()
    )
    matches = result.snapshot.matches
    focused = [m for m in matches if m.source_window_hwnd == 0]
    popup = [m for m in matches if m.source_window_hwnd == POPUP_HWND]
    # Focused badge numbers are 1..N, unchanged.
    assert [m.display_number for m in focused] == [1, 2]
    # Popup items are appended AFTER, continuing the sequence (no collision,
    # no renumber of the focused items).
    assert popup[0].display_number == 3
    assert popup[0].source_window_hwnd == POPUP_HWND


def test_find_no_popup_present_behaves_like_phase1():
    primary = FakeTopLevel(FakeElementArray([el("Cancel")]))
    finder, captured = make_finder(primary, popup_walk_fn=lambda h, **k: [])
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"), fg()
    )
    names = [m.name for m in result.snapshot.matches]
    assert names == ["Cancel"]
    # No popup match merged in; all matches are primary-sourced.
    assert all(m.source_window_hwnd == 0 for m in result.snapshot.matches)


def test_find_passes_shared_cache_request_to_popup_walk():
    """The popup walk must receive the SAME cache_request object the primary
    walk built (design line 392)."""
    primary = FakeTopLevel(FakeElementArray([el("Cancel")]))
    finder, captured = make_finder(primary, popup_walk_fn=lambda h, **k: [])
    finder.find(ElementQuery("cancel", "button", None, None, "cancel"), fg())
    assert captured["popup_calls"], "popup_walk_fn was never called"
    _focused, kwargs = captured["popup_calls"][-1]
    assert kwargs["cache_request"] is not None


# ---------------------------------------------------------------------------
# Browser wiring: the popup walk must receive the SAME browser DOM-correction
# hook as the primary walk (wh-n29v.47.1). For a Chromium-family foreground,
# query_has_role is False so Text/Group/Heading wrappers survive the walker's
# interactive filter; the hook MUST run to fold that scaffolding, else it
# reaches decide() as scoring candidates.
# ---------------------------------------------------------------------------


def _group_then_text_popup_walkresult(name, *, source_window_hwnd=POPUP_HWND):
    """A popup WalkResult holding a Group wrapper + its sole same-name Text child.

    Geometry/order satisfy apply_dom_corrections' Rule 1 (a GroupControl whose
    sole direct descendant is a same-name TextControl folds away, leaving the
    text): the Group is FIRST in input order (pre-order ancestor) and its rect
    contains the Text's rect. Both carry a winning score + is_eligible so a
    surviving wrapper WOULD reach the kept/scored set if the hook did not run.
    """
    array = FakeElementArray([])
    group = uia_walker.element_match_from_cached(
        FakeCachedElement(name=name, control_type=UIA_GROUP,
                          localized_control_type="group",
                          rect=FakeRect(0, 0, 200, 100)),
        display_number=1,
        source_window_hwnd=source_window_hwnd,
    )
    text = uia_walker.element_match_from_cached(
        FakeCachedElement(name=name, control_type=UIA_TEXT,
                          localized_control_type="text",
                          rect=FakeRect(10, 10, 50, 30)),
        display_number=2,
        source_window_hwnd=source_window_hwnd,
    )
    matches = [
        replace(group, score=0.6, is_eligible=True),
        replace(text, score=0.6, is_eligible=True),
    ]
    return WalkResult(
        matches=matches,
        _keepalive_automation=object(),
        _keepalive_cache_request=object(),
        _keepalive_element_array=array,
        _keepalive_top_level_element=object(),
        deadline_truncated=False,
    )


def _popup_walk_applying_hook(walkresult_factory, recorder=None):
    """A popup_walk_fn fake that applies the RECEIVED browser_correction_hook to
    the popup matches, mimicking what the real walk_window does inside
    walk_owned_popups. ``recorder`` (a list) captures the hook find() passed."""

    def _popup_walk(focused_hwnd, **kwargs):
        hook = kwargs.get("browser_correction_hook")
        if recorder is not None:
            recorder.append(hook)
        wr = walkresult_factory()
        if hook is not None:
            folded = hook(wr.matches)
            wr = replace(wr, matches=folded)
        return [wr]

    return _popup_walk


def test_popup_walk_receives_browser_hook_for_browser_foreground():
    """For a browser-process foreground the popup walk must receive
    apply_dom_corrections (NOT None) as browser_correction_hook."""
    primary = FakeTopLevel(FakeElementArray([el("Save")]))
    recorder: list = []
    finder, _ = make_finder(
        primary,
        popup_walk_fn=_popup_walk_applying_hook(
            lambda: _group_then_text_popup_walkresult("Reload"),
            recorder=recorder,
        ),
    )
    finder.find(
        ElementQuery("reload", None, None, None, "reload"),
        fg(process_name="chrome.exe"),
    )
    assert recorder == [apply_dom_corrections]


def test_popup_walk_browser_hook_folds_scaffolding_out_of_decided_set():
    """End-to-end: with a browser foreground the Group/Text scaffolding wrapper
    is folded by the hook, so the raw Group does NOT survive into the merged
    snapshot -- only the inner Text remains (Rule 1)."""
    primary = FakeTopLevel(FakeElementArray([el("Save")]))
    finder, _ = make_finder(
        primary,
        popup_walk_fn=_popup_walk_applying_hook(
            lambda: _group_then_text_popup_walkresult("Reload"),
        ),
    )
    result = finder.find(
        ElementQuery("reload", None, None, None, "reload"),
        fg(process_name="chrome.exe"),
    )
    popup_matches = [
        m for m in result.snapshot.matches if m.source_window_hwnd == POPUP_HWND
    ]
    # Rule 1 folded the Group away; only the inner Text wrapper survives.
    assert [m.control_type_id for m in popup_matches] == [UIA_TEXT]
    assert all(m.control_type_id != UIA_GROUP for m in popup_matches)


def test_popup_walk_no_hook_for_non_browser_foreground():
    """Counterpart: a NON-browser foreground gets browser_correction_hook=None,
    so no folding runs and the Group wrapper survives unchanged."""
    primary = FakeTopLevel(FakeElementArray([el("Save")]))
    recorder: list = []
    finder, _ = make_finder(
        primary,
        popup_walk_fn=_popup_walk_applying_hook(
            lambda: _group_then_text_popup_walkresult("Reload"),
            recorder=recorder,
        ),
    )
    result = finder.find(
        ElementQuery("reload", None, None, None, "reload"),
        fg(process_name="notepad.exe"),
    )
    assert recorder == [None]
    popup_matches = [
        m for m in result.snapshot.matches if m.source_window_hwnd == POPUP_HWND
    ]
    # No fold: both the Group wrapper and the Text child survive.
    assert UIA_GROUP in [m.control_type_id for m in popup_matches]


# ---------------------------------------------------------------------------
# Split deadline: primary uses full budget; popup is skipped when the primary
# overran its 0.7 share.
# ---------------------------------------------------------------------------


def test_find_skips_popup_when_primary_overran_its_share():
    """ACCEPTANCE: when the primary walk consumes more than its 0.7 share of
    walk_deadline_ms, the popup walk is SKIPPED and the primary results ship."""
    primary = FakeTopLevel(FakeElementArray([el("Cancel")]))

    # walk_deadline_ms=1000 -> budget 1.0s anchored at the first clock read
    # (walk_start=0.0 -> deadline 1.0). The 0.7 checkpoint is start+0.7=0.7.
    # The clock reads, in order: walk_start, the primary walk's pre-walk + per-
    # element checks, then _popup_share_allows. The share-check read is 0.85,
    # which is past the 0.7 checkpoint but still under the 1.0 deadline -- so the
    # primary walk completes normally yet the popup walk is skipped.
    times = iter([0.0,    # walk_start -> deadline 1.0
                  0.0,    # primary walk pre-walk deadline check
                  0.5,    # primary build loop element check
                  0.85])  # _popup_share_allows: 0.85 > 0.7 share -> skip popup

    def clock():
        return next(times, 0.85)

    finder, captured = make_finder(
        primary, popup_walk_fn=lambda h, **k: [_popup_walkresult("Copy")],
        clock=clock, walk_deadline_ms=1000,
    )
    result = finder.find(
        ElementQuery("cancel", "button", None, None, "cancel"), fg()
    )
    # Primary results shipped.
    assert [m.name for m in result.snapshot.matches] == ["Cancel"]
    # Popup walk was NOT performed (primary overran its 0.7 share).
    assert captured["popup_calls"] == []


def test_find_does_popup_when_primary_within_share():
    """Counterpart: primary finished within its 0.7 share -> popup walk runs."""
    primary = FakeTopLevel(FakeElementArray([el("Cancel")]))

    # Clock stays well under the 0.7 checkpoint (start=0.0, budget 1.0, 0.7
    # checkpoint at 0.7); every read returns 0.1.
    finder, captured = make_finder(
        primary, popup_walk_fn=lambda h, **k: [_popup_walkresult("Cancel copy")],
        clock=lambda: 0.1, walk_deadline_ms=1000,
    )
    result = finder.find(
        ElementQuery("cancel", None, None, None, "cancel"), fg()
    )
    assert captured["popup_calls"], "popup walk should have run within budget"
    names = [m.name for m in result.snapshot.matches]
    assert "Cancel copy" in names


def test_find_does_popup_when_no_deadline_configured():
    """No deadline -> no budget pressure -> the popup walk always runs."""
    primary = FakeTopLevel(FakeElementArray([el("Cancel")]))
    finder, captured = make_finder(
        primary, popup_walk_fn=lambda h, **k: [_popup_walkresult("Cancel copy")],
    )
    finder.find(ElementQuery("cancel", None, None, None, "cancel"), fg())
    assert captured["popup_calls"], "popup walk should run when no deadline set"


# ---------------------------------------------------------------------------
# walk_results list keepalive + per-snapshot isolation across primary + popup.
# ---------------------------------------------------------------------------


def test_stored_snapshot_holds_all_subtree_walkresults():
    primary = FakeTopLevel(FakeElementArray([el("Cancel")]))
    popup_result = _popup_walkresult("Cancel copy")

    finder, _ = make_finder(primary, popup_walk_fn=lambda h, **k: [popup_result])
    result = finder.find(
        ElementQuery("cancel", None, None, None, "cancel"), fg()
    )
    sid = result.snapshot.snapshot_id
    stored = finder._stored[sid]
    # The stored snapshot pins BOTH the primary and the popup WalkResult.
    assert isinstance(stored.walk_results, list)
    assert len(stored.walk_results) == 2
    assert popup_result in stored.walk_results
    # Every WalkResult keepalive is held together.
    assert all(wr.is_alive() for wr in stored.walk_results)


def test_popup_keepalive_survives_until_snapshot_evicted():
    primary = FakeTopLevel(FakeElementArray([el("Cancel")]))
    popup_result = _popup_walkresult("Cancel copy")
    ref_popup_array = weakref.ref(popup_result._keepalive_element_array)

    finder, _ = make_finder(
        primary, popup_walk_fn=lambda h, **k: [popup_result],  # noqa: F821 -- lambda runs before the later `del popup_result`
        snapshot_store_capacity=1,
    )
    result = finder.find(
        ElementQuery("cancel", None, None, None, "cancel"), fg()
    )
    del result, popup_result
    gc.collect()
    # While the snapshot is stored, the popup subtree keepalive survives.
    assert ref_popup_array() is not None


def _find_with_popup(finder, focused_name, popup_name, popup_hwnd):
    """Run one find() over a FRESH primary tree + a fresh popup WalkResult.

    The fresh trees are NOT retained by the caller, so only the finder's store
    (and the returned FindResult) pin the keepalives -- the prerequisite for
    observing per-snapshot, multi-WalkResult keepalive isolation on eviction.
    Returns (FindResult, primary-array weakref, popup-array weakref).
    """
    primary = FakeTopLevel(FakeElementArray([el(focused_name)]))
    popup = _popup_walkresult(popup_name, source_window_hwnd=popup_hwnd)
    ref_primary = weakref.ref(primary._element_array)
    ref_popup = weakref.ref(popup._keepalive_element_array)
    finder._walk_fn = lambda tl, **k: walk_window(
        primary, automation=FakeAutomation(None),
        **{kk: vv for kk, vv in k.items() if kk != "automation"})
    finder._popup_walk_fn = lambda h, **k: [popup]
    result = finder.find(
        ElementQuery(focused_name.lower(), None, None, None,
                     focused_name.lower()),
        fg(),
    )
    return result, ref_primary, ref_popup


def test_evicting_snapshot_releases_only_its_subtree_keepalives():
    """Per-snapshot keepalive isolation extends to multi-WalkResult snapshots:
    evicting snapshot A drops ALL of A's WalkResults (primary + popup) but
    leaves snapshot B's untouched."""
    finder, _ = make_finder(
        FakeTopLevel(FakeElementArray([el("First")])),
        snapshot_store_capacity=1,
    )

    res_a, ref_primary_a, ref_popup_a = _find_with_popup(
        finder, "First", "First copy", 2001
    )
    sid_a = res_a.snapshot.snapshot_id
    # Drop the FindResult AND the walk closures so ONLY the store pins A.
    del res_a
    finder._walk_fn = lambda tl, **k: None  # type: ignore[assignment]
    finder._popup_walk_fn = lambda h, **k: []
    gc.collect()
    assert ref_primary_a() is not None and ref_popup_a() is not None

    # Second find() at capacity 1 evicts snapshot A; its closures replace A's.
    res_b, _ref_primary_b, ref_popup_b = _find_with_popup(
        finder, "Second", "Second copy", 2002
    )
    gc.collect()

    assert finder.get_snapshot(sid_a) is None
    # A's BOTH keepalives released by eviction (no test fixture pins them now).
    assert ref_primary_a() is None
    assert ref_popup_a() is None
    # B's popup keepalive isolated, still held by the store + res_b.
    assert ref_popup_b() is not None
    del res_b


# ---------------------------------------------------------------------------
# Popup-owned SOLE clear winner: _walk_result_for_winner returns the POPUP
# WalkResult so FindResult keeps the winner's control_ref alive (wh-n29v.46.4).
# ---------------------------------------------------------------------------


def _scored_popup_walkresult(name, *, source_window_hwnd=POPUP_HWND, score=0.6):
    """A popup WalkResult whose sole match is an already-scored clear winner.

    Unlike :func:`_popup_walkresult`, this stamps a winning ``score`` and
    ``is_eligible=True`` on the match. The finder's popup merge re-decides over
    the merged set WITHOUT re-scoring popup matches (it reuses their existing
    score), so a raw score-0.0 popup match is dropped below min_confidence and
    can never win. Stamping the score here lets the popup item be the SOLE kept
    match and thus the clear winner -- the only way to reach the popup-owned
    branch of ``_walk_result_for_winner``.
    """
    array = FakeElementArray([])
    base = uia_walker.element_match_from_cached(
        FakeCachedElement(name=name, control_type=UIA_MENUITEM,
                          localized_control_type="menu item"),
        display_number=1,
        source_window_hwnd=source_window_hwnd,
    )
    match = replace(base, score=score, is_eligible=True)
    return WalkResult(
        matches=[match],
        _keepalive_automation=object(),
        _keepalive_cache_request=object(),
        _keepalive_element_array=array,
        _keepalive_top_level_element=object(),
        deadline_truncated=False,
    )


def test_popup_owned_sole_winner_keepalive_backed_by_popup_walkresult():
    """When the decided winner is a popup item (focused window has NO eligible
    match, exactly ONE popup match matches the query), the find() result is
    ``ok`` with a popup-owned winner, and FindResult._walk_result is the POPUP
    WalkResult -- NOT the primary -- so the winner's control_ref keepalive is
    backed by the walk that actually owns it (the popup-owned branch of
    _walk_result_for_winner)."""
    # Focused window has only "Save" -- the query "cancel" finds no eligible
    # match there, so the focused walk decides not_found and the merge re-decides
    # over the single scored popup item.
    primary = FakeTopLevel(FakeElementArray([el("Save")]))
    popup_result = _scored_popup_walkresult("Cancel")

    finder, _ = make_finder(
        primary, popup_walk_fn=lambda h, **k: [popup_result]
    )
    result = finder.find(
        ElementQuery("cancel", None, None, None, "cancel"), fg()
    )

    # (a) The popup item is the sole clear winner.
    assert result.outcome.outcome == "ok"
    # (b) The winner is popup-owned (carries the popup HWND, not 0).
    assert result.outcome.winner is not None
    assert result.outcome.winner.source_window_hwnd == POPUP_HWND
    # (c) FindResult._walk_result is the POPUP WalkResult (the one whose matches
    #     contain the winning control_ref) -- NOT the primary walk_results[0].
    stored = finder._stored[result.snapshot.snapshot_id]
    assert result._walk_result is popup_result
    assert result._walk_result is not stored.walk_results[0]
    assert any(
        m.control_ref is result.outcome.winner.control_ref
        for m in result._walk_result.matches
    )


def test_popup_owned_winner_keepalive_survives_store_drop():
    """Keepalive survival: after the finder's store drops the snapshot (a second
    find() at capacity 1 evicts it), the popup winner's WalkResult element-array
    keepalive is still reachable through the retained FindResult alone."""
    primary = FakeTopLevel(FakeElementArray([el("Save")]))
    popup_result = _scored_popup_walkresult("Cancel")
    ref_popup_array = weakref.ref(popup_result._keepalive_element_array)

    finder, _ = make_finder(
        primary, popup_walk_fn=lambda h, **k: [popup_result],  # noqa: F821 -- lambda runs before the later `del popup_result`
        snapshot_store_capacity=1,
    )
    result = finder.find(
        ElementQuery("cancel", None, None, None, "cancel"), fg()
    )
    assert result.outcome.outcome == "ok"
    assert result._walk_result is popup_result
    # Drop the local strong ref to the popup WalkResult and the walk closure so
    # ONLY the finder store and the FindResult can keep the array alive.
    del popup_result
    finder._popup_walk_fn = lambda h, **k: []

    # A second find() at capacity 1 evicts the first snapshot from the store, so
    # the store no longer pins the first snapshot's keepalives.
    finder._walk_fn = lambda tl, **k: walk_window(
        FakeTopLevel(FakeElementArray([el("Other")])),
        automation=FakeAutomation(None),
        **{kk: vv for kk, vv in k.items() if kk != "automation"})
    finder.find(ElementQuery("other", None, None, None, "other"), fg())
    gc.collect()

    # The store evicted the first snapshot, but `result` still holds the popup
    # WalkResult through FindResult._walk_result, so its element-array keepalive
    # is still reachable.
    assert ref_popup_array() is not None
    del result
    gc.collect()
    assert ref_popup_array() is None


# ---------------------------------------------------------------------------
# Production-wired composed chain (wh-n29v.72.1): the finder runs the REAL
# walk_owned_popups default (NO popup_walk_fn override) over a shared fake
# IUIAutomation root. This is the ONE finder-level test that composes the
# production wiring instead of testing each link against a fake at its own
# seam, so a regression like the finder mis-passing self._automation as None
# to the popup walk -- which would dead-end _make_default_control_type_of --
# would FAIL here, not only on a live desktop. Only the COM/win32 LEAVES are
# faked (the IUIAutomation root + the win32gui enumerator/owner/class/visible
# seams that walk_owned_popups binds as keyword defaults); no real display.
# ---------------------------------------------------------------------------


class _MenuPopupElement:
    """A fake top-level popup element resolved by ElementFromHandle.

    Serves the two reads the production chain makes on a popup HWND through the
    SHARED IUIAutomation root: (1) the UIA-Menu control-type probe
    (``CurrentControlType == UIA_MENU``) that ``_make_default_control_type_of``
    issues, and (2) the popup ``walk_window``'s ``FindAllBuildCache`` over the
    popup subtree (returning the popup's one named match).
    """

    def __init__(self, element_array):
        self.CurrentControlType = UIA_MENU
        self._element_array = element_array
        self.find_all_build_cache_calls = 0

    def FindAllBuildCache(self, _tree_scope, _condition, _cache_request):
        self.find_all_build_cache_calls += 1
        return self._element_array


class _SharedRootAutomation(FakeAutomation):
    """Fake IUIAutomation root that resolves a popup HWND to a Menu element.

    Extends the walker's FakeAutomation (so CreateCacheRequest /
    CreateTrueCondition work for the popup walk_window) and adds the
    ElementFromHandle the production control-type probe + popup walk both call.
    The SAME object is the finder's automation root, so this test proves the
    finder threaded a non-None root into both the control-type lookup and the
    popup walk (the production wiring).
    """

    def __init__(self, popup_element):
        super().__init__(None)
        self._popup_element = popup_element
        self.element_from_handle_calls: list[int] = []

    def ElementFromHandle(self, hwnd):
        self.element_from_handle_calls.append(hwnd)
        return self._popup_element


def test_find_real_popup_walk_default_over_shared_root_merges_menu_item():
    # Focused window has an unrelated control; the owned popup contributes the
    # "Copy" menu item the query targets. The popup is detected via the
    # UIA-Menu CONTROL-TYPE path (its class name is NOT the classic #32768), so
    # the test drives _make_default_control_type_of over the shared root -- the
    # exact path the production wiring turns on.
    primary = FakeTopLevel(FakeElementArray([el("Save")]))

    popup_element = _MenuPopupElement(
        FakeElementArray([
            el("Copy", control_type=UIA_MENUITEM, role="menu item"),
        ])
    )
    shared_root = _SharedRootAutomation(popup_element)

    def _primary_walk(top_level, **kwargs):
        # Drive the REAL walk_window over the fake focused tree, but use the
        # SHARED root for the cache_request so the popup walk inherits it.
        kwargs.pop("automation", None)
        return walk_window(primary, automation=shared_root, **kwargs)

    finder = ElementFinder(
        automation=shared_root,
        walk_fn=_primary_walk,
        # NO popup_walk_fn override -- the REAL walk_owned_popups default runs.
        dpi_resolver=fixed_dpi_resolver,
        monitor_resolver=zero_monitor_resolver,
        window_enumerator=lambda: [],
    )

    # Fake ONLY the win32 leaves walk_owned_popups binds as keyword defaults:
    # one visible owned popup HWND whose class name is not #32768 (forcing the
    # UIA-Menu control-type probe over the shared root).
    leaf_fakes = {
        "enumerator": lambda: [POPUP_HWND],
        "owner_fn": lambda hwnd: FOCUSED_HWND,
        "class_name_fn": lambda hwnd: "SomeOtherClass",
        "visible_fn": lambda hwnd: True,
    }
    with patch.dict(walk_owned_popups.__kwdefaults__, leaf_fakes):
        result = finder.find(
            ElementQuery("copy", None, None, None, "copy"), fg()
        )

    # The popup "Copy" menu item merged into the snapshot and carries the popup
    # HWND -- proof the shared-root control-type probe matched UIA_MENU and the
    # popup walk_window resolved + walked the popup subtree.
    popup_matches = [
        m for m in result.snapshot.matches if m.source_window_hwnd != 0
    ]
    assert [m.name for m in popup_matches] == ["Copy"]
    assert popup_matches[0].source_window_hwnd == POPUP_HWND
    # The shared root WAS consulted (control-type probe + popup top-level
    # resolution both go through ElementFromHandle on the SAME root).
    assert POPUP_HWND in shared_root.element_from_handle_calls


# ---------------------------------------------------------------------------
# overlay_walk owned-popup merge (wh-n29v.75): the numbered overlay folds in
# owned #32768 / UIA-Menu popup items exactly like find() does, so "show
# numbers" can badge a menu item that by-name "click <item>" can already
# target. There is NO spoken name, so no decide() runs: the popup matches are
# merged AFTER the focused matches and the combined set is renumbered 1..K
# contiguously. The stored snapshot pins the full WalkResult list.
# ---------------------------------------------------------------------------


def test_overlay_walk_merges_owned_popup_items_badged_contiguously():
    # Focused window has two interactive controls; an owned popup contributes
    # two menu items. overlay_walk must number ALL four 1..4, focused first
    # (reading order) then popup, and the popup items must be present in the
    # summary. Distinct side-by-side rects: same-rect fakes would read as one
    # visual control and collapse to one badge (wh-overlay-nested-dupes).
    focused = _MultiTopLevel([
        _interactive_el("Save", rect=FakeRect(10, 20, 110, 70)),
        _interactive_el("Open", rect=FakeRect(120, 20, 220, 70)),
    ])
    popup_result = _popup_walkresult("Copy item", "Paste item")

    finder = make_multi_finder(popup_walk_fn=lambda h, **k: [popup_result])
    result = finder.overlay_walk(_fg_top(focused))

    assert result.outcome == "ok"
    assert result.summary is not None
    items = result.summary.items
    # Four badges total, contiguous 1..4: focused (Save, Open) then popup.
    assert [i.display_number for i in items] == [1, 2, 3, 4]
    assert [i.item_id for i in items] == ["uia-1", "uia-2", "uia-3", "uia-4"]
    assert [i.name for i in items] == [
        "Save", "Open", "Copy item", "Paste item",
    ]
    # The popup items carry the popup HWND on the stored snapshot; the focused
    # ones carry 0 -- proof the merge folded a separate subtree in.
    matches = result.snapshot.matches
    focused_matches = [m for m in matches if m.source_window_hwnd == 0]
    popup_matches = [m for m in matches if m.source_window_hwnd == POPUP_HWND]
    assert [m.name for m in focused_matches] == ["Save", "Open"]
    assert [m.name for m in popup_matches] == ["Copy item", "Paste item"]
    # The stored snapshot.matches agree with the summary on BOTH keys.
    assert [m.display_number for m in matches] == [1, 2, 3, 4]
    assert [m.item_id for m in matches] == ["uia-1", "uia-2", "uia-3", "uia-4"]


def test_overlay_walk_stored_snapshot_pins_primary_and_popup_walkresults():
    # Keepalive contract parity with find(): the stored snapshot's walk_results
    # list holds BOTH the primary focused-window walk AND the owned-popup walk.
    focused = _MultiTopLevel([_interactive_el("Save")])
    popup_result = _popup_walkresult("Copy item")

    finder = make_multi_finder(popup_walk_fn=lambda h, **k: [popup_result])
    result = finder.overlay_walk(_fg_top(focused))

    sid = result.snapshot.snapshot_id
    stored = finder._stored[sid]
    assert isinstance(stored.walk_results, list)
    assert len(stored.walk_results) == 2
    # The PRIMARY walk leads; the popup WalkResult is appended after.
    assert stored.walk_results[0] is result._walk_results[0]
    assert popup_result in stored.walk_results
    assert stored.walk_results[1] is popup_result
    assert all(wr.is_alive() for wr in stored.walk_results)


def test_overlay_walk_no_popup_present_behaves_like_focused_only():
    # No popup -> the stored snapshot pins a single-element WalkResult list and
    # the summary numbers only the focused controls (no regression). Distinct
    # rects: same-rect fakes would collapse (wh-overlay-nested-dupes).
    focused = _MultiTopLevel([
        _interactive_el("Save", rect=FakeRect(10, 20, 110, 70)),
        _interactive_el("Open", rect=FakeRect(120, 20, 220, 70)),
    ])

    finder = make_multi_finder(popup_walk_fn=lambda h, **k: [])
    result = finder.overlay_walk(_fg_top(focused))

    assert result.outcome == "ok"
    assert [i.name for i in result.summary.items] == ["Save", "Open"]
    stored = finder._stored[result.snapshot.snapshot_id]
    assert len(stored.walk_results) == 1
    assert all(m.source_window_hwnd == 0 for m in result.snapshot.matches)


def test_overlay_walk_passes_shared_cache_request_and_clock_to_popup_walk():
    # The popup walk must run under the SAME automation root + shared
    # cache_request (the primary walk's _keepalive_cache_request) and the SAME
    # deadline as the primary -- no second root, no per-popup CacheRequest.
    focused = _MultiTopLevel([_interactive_el("Save")])
    captured: dict = {}

    def popup_walk(focused_hwnd, **kwargs):
        captured["focused_hwnd"] = focused_hwnd
        captured["kwargs"] = kwargs
        return []

    finder = make_multi_finder(popup_walk_fn=popup_walk)
    finder.overlay_walk(_fg_top(focused))

    assert captured["focused_hwnd"] == FOCUSED_HWND
    assert captured["kwargs"]["cache_request"] is not None
    # A non-browser foreground walks query_has_role=True (interactive filter on);
    # the popup walk mirrors that value (not is_browser == True).
    assert captured["kwargs"]["query_has_role"] is True
    assert captured["kwargs"]["browser_correction_hook"] is None


def test_overlay_walk_popup_receives_browser_hook_for_browser_foreground():
    # For a Chromium-family foreground the popup walk must receive
    # apply_dom_corrections (query_has_role=False so Text/Group/Heading survive
    # into the fold), mirroring find()'s browser wiring.
    focused = _MultiTopLevel([_interactive_el("Save")])
    captured: dict = {}

    def popup_walk(focused_hwnd, **kwargs):
        captured["kwargs"] = kwargs
        return []

    finder = make_multi_finder(popup_walk_fn=popup_walk)
    finder.overlay_walk(_fg_top(focused, process_name="chrome.exe"))

    assert captured["kwargs"]["query_has_role"] is False
    assert captured["kwargs"]["browser_correction_hook"] is apply_dom_corrections


def test_overlay_walk_skips_popup_when_primary_overran_its_share():
    # Gate parity with find(): when the primary walk consumes more than its 0.7
    # share of walk_deadline_ms, the popup walk is SKIPPED and the focused-only
    # overlay ships.
    focused = _MultiTopLevel([_interactive_el("Save")])

    # Same clock schedule as test_find_skips_popup_when_primary_overran_its_share:
    # walk_start=0.0 -> deadline 1.0, 0.7 checkpoint at 0.7; the share-check read
    # is 0.85 (> 0.7 but < 1.0) so the primary completes yet the popup is skipped.
    times = iter([0.0, 0.0, 0.5, 0.85])

    def clock():
        return next(times, 0.85)

    popup_calls: list = []

    def popup_walk(focused_hwnd, **kwargs):
        popup_calls.append(focused_hwnd)
        return [_popup_walkresult("Copy")]

    finder = make_multi_finder(
        popup_walk_fn=popup_walk, clock=clock, walk_deadline_ms=1000,
    )
    result = finder.overlay_walk(_fg_top(focused))

    assert [i.name for i in result.summary.items] == ["Save"]
    assert popup_calls == []


def test_overlay_walk_result_pins_primary_and_popup_walkresults():
    # Keepalive parity with find() on the RETURNED result (reviewer_0 finding
    # wh-n29v.76.1). FindResult._walk_result exists so a consumer that retains
    # the result across a LATER walk -- after self._stored has been overwritten
    # to the newer walk -- still keeps THIS result's snapshot COM proxies alive.
    # OverlayWalkResult has no single winner, so it must pin the FULL WalkResult
    # list (primary + popups), not just the primary, or a retained overlay
    # result's popup COM chains would dangle on a later snapshot-item Invoke.
    focused = _MultiTopLevel([_interactive_el("Save")])
    popup_result = _popup_walkresult("Copy item")

    finder = make_multi_finder(popup_walk_fn=lambda h, **k: [popup_result])
    result = finder.overlay_walk(_fg_top(focused))

    stored = finder._stored[result.snapshot.snapshot_id]
    # The returned result pins the SAME WalkResult objects the stored snapshot
    # does: primary first, popup appended -- the whole keepalive set.
    assert list(result._walk_results) == list(stored.walk_results)
    assert result._walk_results[0] is stored.walk_results[0]
    assert popup_result in result._walk_results
    assert all(wr.is_alive() for wr in result._walk_results)


def test_overlay_walk_no_popup_result_pins_single_primary_walkresult():
    # The no-popup returned result pins exactly the single primary WalkResult --
    # same keepalive set as Phase 1, expressed as the one-element list.
    focused = _MultiTopLevel([_interactive_el("Save")])

    finder = make_multi_finder(popup_walk_fn=lambda h, **k: [])
    result = finder.overlay_walk(_fg_top(focused))

    stored = finder._stored[result.snapshot.snapshot_id]
    assert len(result._walk_results) == 1
    assert result._walk_results[0] is stored.walk_results[0]


def test_overlay_walk_anchors_internal_deadline_on_one_clock_read():
    # Deadline-anchor parity with find() (reviewer_0 finding wh-n29v.76.2). When
    # overlay_walk derives the per-request deadline internally (deadline=None +
    # walk_deadline_ms), it must anchor the deadline on the SAME clock read as
    # walk_start -- ONE read, exactly like find() (lines 510-516) -- so the
    # popup-share budget is the full walk_deadline_ms, not a sliver less. A clock
    # that returns a DISTINCT value per call exposes any second top-of-method
    # read: a two-read derivation makes deadline - walk_start < walk_deadline_ms.
    focused = _MultiTopLevel([_interactive_el("Save")])
    ticks = iter([100.0, 100.1, 100.2, 100.3, 100.4, 100.5])

    finder = make_multi_finder(
        popup_walk_fn=lambda h, **k: [],
        clock=lambda: next(ticks, 200.0),
        walk_deadline_ms=1000,
    )

    captured: dict = {}
    real_gate = finder._popup_share_allows

    def spy(deadline, walk_start):
        captured["deadline"] = deadline
        captured["walk_start"] = walk_start
        return real_gate(deadline, walk_start)

    finder._popup_share_allows = spy  # type: ignore[method-assign]
    finder.overlay_walk(_fg_top(focused), deadline=None)

    # deadline anchored on the SAME read as walk_start: budget == walk_deadline_ms.
    assert captured.get("walk_start") is not None
    assert captured["deadline"] == captured["walk_start"] + 1.0
