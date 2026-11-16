"""Logic-side routing tests for the numbered overlay (wh-n29v.17).

Two layers under test:

  1. The PURE resolver ``speech/overlay_click_router.py:route_click_n`` --
     given the current overlay state, the parsed integer, and the
     machine's pin bookkeeping, it returns a ``RoutingDecision``
     (BY_NAME / SNAPSHOT_ITEM / NOTICE / HELD) WITHOUT performing any
     effect. Every routing rule from the v4 design doc "### Click N
     routing (state-machine-driven)" section is exercised here as a pure
     data assertion (state x integer; non-integer -> by-name; the refresh
     visible-snapshot subtlety with and without ``_prior_pin_deferred``;
     NOT_FOUND and SNAPSHOT_EXPIRED both -> notice, never by-name;
     closed+integer -> by-name).

  2. The Logic wiring -- ``LogicController.forward_click_element`` consults
     the resolver after the config gate and routes SNAPSHOT_ITEM / NOTICE /
     HELD to documented stub seams, with BY_NAME preserving the existing
     send_request flow; and ``LogicController.handle_overlay_command`` which
     applies SHOW_NUMBERS / HIDE_NUMBERS to the held state machine and hands
     the returned effects to a stub seam. Stub seams and app.send_request
     are mocked; transitions and effects are asserted as DATA.

Effects/decisions are asserted as values, not via behavioural mocks of the
pure machine (it is pure and returns its effects as data).
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional
from unittest.mock import MagicMock

import pytest

from services.wheelhouse.click_overlay_state import ClickOverlayStateMachine, OverlayState
from services.wheelhouse.click_snapshot_summary_cache import (
    ClickSnapshotSummaryCache,
)
from services.wheelhouse.speech.overlay_click_router import (
    RoutingDecision,
    RoutingKind,
    route_click_n,
)
from services.wheelhouse.ui.element_types import (
    ElementQuery,
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)
from services.wheelhouse.numbered_overlay.controller import (
    NumberedOverlayController,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_foreground_window(monkeypatch):
    """The real overlay's first-use hint reads the foreground window.

    With no foreground window the hint returns before it reads or writes
    its record file, so these tests never show a hint and never touch
    services/wheelhouse/data/. A test that installs a FakeDesktop patches
    the same Win32 call again inside its own scope.
    """
    import win32gui

    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 0)


def _bind_overlay(c):
    """Build the real NumberedOverlayController as ``c.overlay``.

    wh-overlay-refactor-pilot.2 moved the numbered overlay out of
    LogicController. The public constructor takes the (mocked)
    LogicController as its host, so ``c.click_config`` must be set first.
    """
    c.overlay = NumberedOverlayController(c, clock=time.monotonic)
    return c.overlay


def _summary(snapshot_id: str, *display_numbers: int) -> WalkSnapshotSummary:
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"{snapshot_id}-item-{n}",
            display_number=n,
            name=f"control {n}",
            role="Button",
            bounds=(0, 0, 10, 10),
            monitor_id=0,
        )
        for n in display_numbers
    ]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id, items=items, created_at_monotonic=1.0,
    )


def _cache_with(*summaries: WalkSnapshotSummary) -> ClickSnapshotSummaryCache:
    cache = ClickSnapshotSummaryCache()
    for s in summaries:
        cache.put(s.snapshot_id, s)
    return cache


# ---------------------------------------------------------------------------
# Pure resolver: non-integer always falls through to by-name.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        OverlayState.CLOSED,
        OverlayState.WALK_IN_FLIGHT,
        OverlayState.PAINT_IN_FLIGHT,
        OverlayState.PAINTED,
        OverlayState.REFRESH_IN_FLIGHT,
        OverlayState.POST_CLICK_SETTLING,
        OverlayState.PAUSED,
        OverlayState.ERROR,
    ],
)
def test_non_integer_routes_by_name_in_every_state(state):
    cache = _cache_with(_summary("snap", 1, 2, 3))
    decision = route_click_n(
        state=state,
        parsed_number=None,
        cache=cache,
        pinned_snapshot_id="snap",
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
    )
    assert decision.kind is RoutingKind.BY_NAME


# ---------------------------------------------------------------------------
# Pure resolver: closed + integer -> by-name.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pure resolver: PAINTED + N.
# ---------------------------------------------------------------------------


def test_painted_snapshot_expired_routes_notice_never_by_name():
    # The pinned snapshot id is not present in the cache (TTL elapsed /
    # evicted) -> SNAPSHOT_EXPIRED -> notice, never by-name.
    cache = _cache_with(_summary("some-other-snap", 1, 2))
    decision = route_click_n(
        state=OverlayState.PAINTED,
        parsed_number=1,
        cache=cache,
        pinned_snapshot_id="snap-gone",
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
    )
    assert decision.kind is RoutingKind.NOTICE
    assert decision.reason == "no_badge_numbered"


def test_painted_with_no_pinned_snapshot_routes_notice():
    # painted but pinned_snapshot_id is None (defensive): must NOT fall
    # through to by-name; the user said a number while numbers are on.
    cache = _cache_with(_summary("snap-cur", 1))
    decision = route_click_n(
        state=OverlayState.PAINTED,
        parsed_number=1,
        cache=cache,
        pinned_snapshot_id=None,
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
    )
    assert decision.kind is RoutingKind.NOTICE


# ---------------------------------------------------------------------------
# Pure resolver: REFRESH_IN_FLIGHT + N -- the visible-snapshot subtlety.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pure resolver: REFRESH_IN_FLIGHT + N -- focus-change window mismatch (Fix B).
# wh-overlay-snapshot-keepalive: when the refresh was caused by focus moving to
# a DIFFERENT window, the still-visible snapshot belongs to the window that is
# no longer foreground. Resolving N against it would dispatch a click Input
# rejects on a foreground-identity mismatch (reported as snapshot_expired).
# ``visible_window_is_foreground=False`` HOLDS the click so it re-resolves
# against the freshly-built list once the new window's overlay paints. ``None``
# (undeterminable) and ``True`` (same-window content refresh) preserve the prior
# resolve-against-visible behaviour.
# ---------------------------------------------------------------------------


def test_refresh_window_relationship_unknown_preserves_default():
    # Undeterminable (None, the default) -> unchanged resolve-against-visible.
    cache = _cache_with(_summary("snap-visible", 1, 2, 3))
    decision = route_click_n(
        state=OverlayState.REFRESH_IN_FLIGHT,
        parsed_number=2,
        cache=cache,
        pinned_snapshot_id="snap-visible",
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
        visible_window_is_foreground=None,
    )
    assert decision.kind is RoutingKind.SNAPSHOT_ITEM
    assert decision.snapshot_id == "snap-visible"


def test_refresh_window_mismatch_holds_with_deferred_prior():
    # The deferred-prior sub-window: the new W-prime snapshot is pinned but not
    # painted; the VISIBLE prior belongs to the window that lost focus. Still
    # HELD, not resolved against the prior.
    cache = _cache_with(
        _summary("snap-new", 5, 6),
        _summary("snap-prior", 1, 2, 3),
    )
    decision = route_click_n(
        state=OverlayState.REFRESH_IN_FLIGHT,
        parsed_number=2,
        cache=cache,
        pinned_snapshot_id="snap-new",
        prior_pinned_snapshot_id="snap-prior",
        prior_pin_deferred=True,
        visible_window_is_foreground=False,
    )
    assert decision.kind is RoutingKind.HELD


def test_painted_window_mismatch_flag_is_ignored_outside_refresh():
    # The flag gates ONLY refresh_in_flight. In steady PAINTED a mismatch flag
    # (which the integration never sets there) must not suppress a normal click.
    cache = _cache_with(_summary("snap-cur", 1, 2, 3))
    decision = route_click_n(
        state=OverlayState.PAINTED,
        parsed_number=2,
        cache=cache,
        pinned_snapshot_id="snap-cur",
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
        visible_window_is_foreground=False,
    )
    assert decision.kind is RoutingKind.SNAPSHOT_ITEM
    assert decision.snapshot_id == "snap-cur"


# ---------------------------------------------------------------------------
# Pure resolver: HELD states.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        OverlayState.WALK_IN_FLIGHT,
        OverlayState.PAINT_IN_FLIGHT,
        OverlayState.PAUSED,
    ],
)
def test_held_states_produce_held_decision(state):
    cache = _cache_with(_summary("snap", 1, 2))
    decision = route_click_n(
        state=state,
        parsed_number=1,
        cache=cache,
        pinned_snapshot_id="snap",
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
    )
    assert decision.kind is RoutingKind.HELD


# ---------------------------------------------------------------------------
# Pure resolver: ERROR + N -> reject notice.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pure resolver: POST_CLICK_SETTLING + N -> "numbers are updating" notice.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Logic wiring: forward_click_element routes by the resolver decision.
# ---------------------------------------------------------------------------


def _query(name: str = "cancel", role: Optional[str] = "Button") -> ElementQuery:
    return ElementQuery(name, role, None, None, name)


def _make_controller(*, state: OverlayState = OverlayState.CLOSED,
                     pinned_snapshot_id=None,
                     prior_pinned_snapshot_id=None,
                     prior_pin_deferred=False,
                     cache_summaries=()):
    """Build a MagicMock(spec=LogicController) wired for click-N routing.

    forward_click_element / handle_overlay_command are bound from the real
    LogicController; the overlay is the real NumberedOverlayController built
    through its public constructor. The state machine, cache, config, and
    queues are real or simple fakes. app.send_request is captured.
    """
    from main import LogicController
    from ui.click_config import ClickConfig

    c = MagicMock(spec=LogicController)

    for name in (
        "forward_click_element",
        "handle_overlay_command",
        "take_click_disabled_notice",
        "grid_session_in_transition",
    ):
        setattr(c, name, getattr(LogicController, name).__get__(c))

    c.click_config = ClickConfig.from_raw(
        {"enabled": True, "response_timeout_ms": 3000}
    )
    _bind_overlay(c)
    c.overlay.click_snapshot_summary_cache = _cache_with(*cache_summaries)
    c._click_disabled_notice_shown = False
    c.grid_overlay_state = None

    machine = ClickOverlayStateMachine()
    machine.state = state
    machine.pinned_snapshot_id = pinned_snapshot_id
    machine.prior_pinned_snapshot_id = prior_pinned_snapshot_id
    machine._prior_pin_deferred = prior_pin_deferred
    c.overlay.click_overlay_state = machine

    c.state_manager = MagicMock()
    c.state_manager.state_to_gui_queue = MagicMock()

    captured = {}

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        captured["action"] = action
        captured["params"] = params
        captured["timeout_s"] = timeout_s
        # Reply with a minimal not_found so the by-name path completes.
        from shared.click_element import ClickElementResponse
        return ClickElementResponse(
            status="ok", outcome="not_found", reason=None,
            matched_names=(), snapshot_id=None, snapshot_summary=None,
            matched_name=None, trace_id="trace",
        ).to_dict()

    c.app = MagicMock()
    c.app.send_request = _send_request
    c._captured = captured
    return c


def test_painted_integer_miss_emits_notice_no_send_request():
    c = _make_controller(
        state=OverlayState.PAINTED, pinned_snapshot_id="snap",
        cache_summaries=(_summary("snap", 1),),
    )
    c.overlay.forward_click_notice = MagicMock()
    asyncio.run(
        c.forward_click_element(_query(name="9", role=None), "tr")
    )
    assert "action" not in c._captured
    c.overlay.forward_click_notice.assert_called_once()
    _, kwargs = c.overlay.forward_click_notice.call_args
    assert kwargs.get("outcome") == "execution_failed"
    assert kwargs.get("reason") == "no_badge_numbered"


async def test_click_number_for_drops_the_filler_and_clicks_badge_four(
    tmp_path,
):
    """"click number for" clicks badge 4.

    click_parser drops a leading "number" filler only when what follows
    parses as a number, so without the alias the filler stayed and the
    query name was "number for" -- a by-name search for a control nobody
    has. Drives the real parser.
    """
    from services.wheelhouse.speech.click_parser import ClickCommandParser
    from tests.overlay_boundary_harness import item_id, overlay_boundary

    query = ClickCommandParser().parse("number for")
    assert query is not None
    assert query.name == "for"
    assert query.role is None

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap", numbers=(1, 2, 3, 4))
        mark = b.mark()
        await b.say("click number for")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap", 4)
        await b.flush()
        # No by-name click went out: the one click is the badge click.
        clicks = b.frames_since(
            mark, actions={"click_element", "click_snapshot_item"},
        )
        assert [f.action for f in clicks] == ["click_snapshot_item"]
        # Nothing else went to the Input process either, so a by-name
        # click or a new walk fails here. The only other frames allowed:
        # end_utterance, which the speech processor sends at the end of
        # every utterance, and unpin_snapshot, the badge click's own
        # unpin of the snapshot whose badges it cleared.
        sent = [f.action for f in b.frames[mark[0]:]]
        assert set(sent) <= {
            "end_utterance", "click_snapshot_item", "unpin_snapshot",
        }, sent


def test_a_real_name_beginning_with_the_filler_still_searches_by_name():
    """"click number pad" keeps both words: "pad" is not a number.

    The filler drop must stay guarded; the alias widens what counts as a
    number, it must not make the guard unconditional.
    """
    from services.wheelhouse.speech.click_parser import ClickCommandParser

    query = ClickCommandParser().parse("number pad")
    assert query is not None
    assert query.name == "number pad"


def test_a_homophone_with_a_role_keyword_stays_a_by_name_click():
    """"click for button" still searches for a control named "for".

    The role check is what separates a badge number from a by-name
    query; the alias change must not reach past it.
    """
    c = _make_controller(
        state=OverlayState.PAINTED, pinned_snapshot_id="snap",
        cache_summaries=(_summary("snap", 1, 2, 3, 4),),
    )
    asyncio.run(
        c.forward_click_element(_query(name="for", role="Button"), "tr")
    )
    assert c._captured.get("action") == "click_element"


def test_error_state_notice_does_not_carry_stale_pinned_snapshot():
    # wh-n29v.19.3 (reviewer_1): the ERROR-state numbers_not_showing notice
    # must NOT carry the pinned snapshot id. The state machine deliberately
    # leaves pins populated through ERROR so the recovery path can unpin them
    # (see ClickOverlayStateMachine._invalid), but that pin is bookkeeping --
    # numbers are not showing, so there is no snapshot the user saw. The
    # resolver returns NOTICE with snapshot_id=None for ERROR; forward that,
    # never the stale pin.
    c = _make_controller(
        state=OverlayState.ERROR, pinned_snapshot_id="snap-stale",
    )
    c.overlay.forward_click_notice = MagicMock()
    asyncio.run(
        c.forward_click_element(_query(name="1", role=None), "tr")
    )
    c.overlay.forward_click_notice.assert_called_once()
    _, kwargs = c.overlay.forward_click_notice.call_args
    assert kwargs.get("reason") == "numbers_not_showing"
    assert kwargs.get("snapshot_id") is None


# ---------------------------------------------------------------------------
# Logic wiring: handle_overlay_command applies the events + hands off effects.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Overlay-only disabled gate (wh-n29v.66.1.1, codex finding).
#
# A bad overlay key (here overlay_focus_debounce_ms=5001, outside the
# validated [0, 5000] range) disables ONLY the numbered overlay: ClickConfig
# records overlay_focus_debounce_ms=250 AND overlay_enabled_effective=False
# while enabled STAYS True (by-name click keeps working). The Input process
# already gates the overlay walk on overlay_enabled_effective
# (OverlaySnapshotActions._get_overlay_walk_finder in
# ui/overlay_snapshot_actions.py), so the Logic-side
# overlay-only entry points and the focus-hook thread must gate on the SAME
# overlay_enabled_effective, not on the coarser enabled flag. Otherwise Logic
# would start the Win32 focus-hook thread and accept show/hide commands for an
# overlay the validated config says is off, while Input refuses to walk -- the
# two processes disagreeing on whether the overlay is on, which is exactly the
# parity the click_config docstring (overlay_enabled_effective) promises.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Master opt-out gate (wh-n29v.66.1.1 fix correctness).
#
# A valid enabled=false flows through the normal validation path, so
# overlay_enabled (default True) and overlay_invalid_key () leave
# overlay_enabled_effective TRUE. overlay_enabled_effective therefore does NOT
# imply enabled: the Logic overlay gates must require BOTH enabled AND
# overlay_enabled_effective, matching the Input-side gate (which requires the
# enabled-gated finder AND overlay_enabled_effective). These tests prove the
# master opt-out still blocks the focus-hook thread and its callbacks; the
# existing test_handle_overlay_command_gated_on_disabled_config covers
# show/hide.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RoutingDecision is a frozen value type.
# ---------------------------------------------------------------------------


def test_routing_decision_is_frozen():
    d = RoutingDecision(kind=RoutingKind.BY_NAME)
    with pytest.raises(Exception):
        d.kind = RoutingKind.HELD  # type: ignore[misc]


# ---------------------------------------------------------------------------
# wh-overlay-fixqueue-review.2: renumber guard after a proactive refresh.
#
# A timer-driven (proactive) refresh can renumber badges between the user
# reading badge N and their "click N" transcript arriving. Resolving N
# against the NEW snapshot then clicks a different control with fresh bounds,
# so the stale-position refusal that used to protect this case never fires.
# The guard: for a short grace window after a proactive swap, a "click N"
# whose badge N changed identity (name differs between the prior and current
# snapshots) gets a "numbers just changed" notice instead of a click. One
# block per swap -- the notice tells the user to re-check, so their next
# utterance is informed and the guard is consumed.
# ---------------------------------------------------------------------------

from services.wheelhouse.speech.overlay_click_router import renumber_click_is_safe


def _named_summary(snapshot_id: str, names_by_number: dict) -> WalkSnapshotSummary:
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"{snapshot_id}-item-{n}",
            display_number=n,
            name=name,
            role="Button",
            bounds=(0, 0, 10, 10),
            monitor_id=0,
        )
        for n, name in names_by_number.items()
    ]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id, items=items, created_at_monotonic=1.0,
    )


def test_renumber_safe_when_names_match_case_insensitively():
    prior = _named_summary("old", {1: "Submit "})
    current = _named_summary("new", {1: "submit"})
    assert renumber_click_is_safe(prior, current, 1) is True


def test_renumber_safe_when_prior_summary_unavailable():
    # The prior snapshot aged out of the cache: nothing to compare against,
    # so do not block (the guard is best-effort, never a hard gate).
    current = _named_summary("new", {1: "Delete"})
    assert renumber_click_is_safe(None, current, 1) is True


def test_renumber_safe_when_current_summary_unavailable():
    prior = _named_summary("old", {1: "Submit"})
    assert renumber_click_is_safe(prior, None, 1) is True


# ---------------------------------------------------------------------------
# wh-overlay-fixqueue-review.3 (codex): badge identity is name + role +
# bounds, not name alone. Same-named controls are common on browser pages
# (a "Delete" button per row); a row insert/remove during the grace window
# can move badge N from one "Delete" to another, and a name-only check
# waves the wrong click through. Equal bounds on an unchanged page keep the
# seamless-click case working (a re-walk of an unmoved control reproduces
# identical physical-pixel bounds); any visible move blocks once.
# ---------------------------------------------------------------------------


def _item_summary(snapshot_id: str, items_spec: dict) -> WalkSnapshotSummary:
    """items_spec: display_number -> (name, role, bounds)."""
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"{snapshot_id}-item-{n}",
            display_number=n,
            name=name,
            role=role,
            bounds=bounds,
            monitor_id=0,
        )
        for n, (name, role, bounds) in items_spec.items()
    ]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id, items=items, created_at_monotonic=1.0,
    )


def test_renumber_unsafe_when_same_name_but_bounds_moved():
    prior = _item_summary("old", {2: ("Delete", "Button", (0, 0, 10, 10))})
    current = _item_summary("new", {2: ("Delete", "Button", (0, 40, 10, 50))})
    assert renumber_click_is_safe(prior, current, 2) is False


def test_renumber_unsafe_when_same_name_but_role_changed():
    prior = _item_summary("old", {2: ("Delete", "Button", (0, 0, 10, 10))})
    current = _item_summary("new", {2: ("Delete", "Hyperlink", (0, 0, 10, 10))})
    assert renumber_click_is_safe(prior, current, 2) is False


def test_renumber_safe_when_name_role_and_bounds_all_match():
    prior = _item_summary("old", {2: ("Delete", "Button", (0, 0, 10, 10))})
    current = _item_summary("new", {2: ("delete ", "Button", (0, 0, 10, 10))})
    assert renumber_click_is_safe(prior, current, 2) is True
