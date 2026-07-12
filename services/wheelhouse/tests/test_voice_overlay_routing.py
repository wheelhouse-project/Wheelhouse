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
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from services.wheelhouse.click_overlay_state import (
    ClickOverlayStateMachine,
    EffectKind,
    OverlayEventKind,
    OverlayOutcome,
    OverlayState,
)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def test_closed_plus_integer_routes_by_name():
    cache = _cache_with(_summary("snap", 1, 2, 3))
    decision = route_click_n(
        state=OverlayState.CLOSED,
        parsed_number=7,
        cache=cache,
        pinned_snapshot_id=None,
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
    )
    assert decision.kind is RoutingKind.BY_NAME


# ---------------------------------------------------------------------------
# Pure resolver: PAINTED + N.
# ---------------------------------------------------------------------------


def test_painted_found_routes_snapshot_item():
    cache = _cache_with(_summary("snap-cur", 1, 2, 3))
    decision = route_click_n(
        state=OverlayState.PAINTED,
        parsed_number=2,
        cache=cache,
        pinned_snapshot_id="snap-cur",
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
    )
    assert decision.kind is RoutingKind.SNAPSHOT_ITEM
    assert decision.snapshot_id == "snap-cur"
    assert decision.item_id == "snap-cur-item-2"


def test_painted_not_found_routes_notice_never_by_name():
    cache = _cache_with(_summary("snap-cur", 1, 2, 3))
    decision = route_click_n(
        state=OverlayState.PAINTED,
        parsed_number=9,  # no badge 9
        cache=cache,
        pinned_snapshot_id="snap-cur",
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
    )
    assert decision.kind is RoutingKind.NOTICE
    assert decision.reason == "no_badge_numbered"
    assert decision.kind is not RoutingKind.BY_NAME
    # The miss notice carries the snapshot the number was resolved against.
    # In painted the pinned snapshot IS the visible one (wh-n29v.18.1).
    assert decision.snapshot_id == "snap-cur"


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


def test_refresh_without_deferred_resolves_against_pinned():
    # _prior_pin_deferred False: the prior build has not returned, so the
    # still-visible snapshot is pinned_snapshot_id.
    cache = _cache_with(
        _summary("snap-visible", 1, 2, 3),
        _summary("snap-new", 5, 6),  # not yet painted -- must NOT be used
    )
    decision = route_click_n(
        state=OverlayState.REFRESH_IN_FLIGHT,
        parsed_number=2,
        cache=cache,
        pinned_snapshot_id="snap-visible",
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
    )
    assert decision.kind is RoutingKind.SNAPSHOT_ITEM
    assert decision.snapshot_id == "snap-visible"
    assert decision.item_id == "snap-visible-item-2"


def test_refresh_with_deferred_resolves_against_prior_not_new():
    # _prior_pin_deferred True: a refresh build already pinned a NEW
    # not-yet-painted snapshot (pinned_snapshot_id) and deferred the prior's
    # unpin. The still-VISIBLE snapshot is prior_pinned_snapshot_id; resolve
    # against THAT, never the new one.
    cache = _cache_with(
        _summary("snap-new", 5, 6),       # pinned but NOT yet painted
        _summary("snap-prior", 1, 2, 3),  # still on screen
    )
    decision = route_click_n(
        state=OverlayState.REFRESH_IN_FLIGHT,
        parsed_number=2,
        cache=cache,
        pinned_snapshot_id="snap-new",
        prior_pinned_snapshot_id="snap-prior",
        prior_pin_deferred=True,
    )
    assert decision.kind is RoutingKind.SNAPSHOT_ITEM
    assert decision.snapshot_id == "snap-prior"
    assert decision.item_id == "snap-prior-item-2"


def test_refresh_with_deferred_miss_against_prior_routes_notice():
    cache = _cache_with(
        _summary("snap-new", 2),
        _summary("snap-prior", 1),  # only badge 1 visible
    )
    decision = route_click_n(
        state=OverlayState.REFRESH_IN_FLIGHT,
        parsed_number=2,  # badge 2 only exists in the NEW snapshot
        cache=cache,
        pinned_snapshot_id="snap-new",
        prior_pinned_snapshot_id="snap-prior",
        prior_pin_deferred=True,
    )
    assert decision.kind is RoutingKind.NOTICE
    assert decision.reason == "no_badge_numbered"
    # wh-n29v.18.1: the miss notice must reference the VISIBLE snapshot the
    # number was resolved against (the prior, still-on-screen one), NOT the
    # current pin (the new not-yet-painted snapshot the user never saw).
    assert decision.snapshot_id == "snap-prior"
    assert decision.snapshot_id != "snap-new"


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


def test_refresh_window_mismatch_holds_instead_of_resolving():
    # The number WOULD resolve against the visible snapshot, but its window is
    # no longer foreground -> HELD, not a doomed SNAPSHOT_ITEM dispatch.
    cache = _cache_with(_summary("snap-visible", 1, 2, 3))
    decision = route_click_n(
        state=OverlayState.REFRESH_IN_FLIGHT,
        parsed_number=2,
        cache=cache,
        pinned_snapshot_id="snap-visible",
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
        visible_window_is_foreground=False,
    )
    assert decision.kind is RoutingKind.HELD


def test_refresh_window_match_resolves_normally():
    # Same-window content refresh: the visible snapshot's window IS foreground,
    # so resolve normally.
    cache = _cache_with(_summary("snap-visible", 1, 2, 3))
    decision = route_click_n(
        state=OverlayState.REFRESH_IN_FLIGHT,
        parsed_number=2,
        cache=cache,
        pinned_snapshot_id="snap-visible",
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
        visible_window_is_foreground=True,
    )
    assert decision.kind is RoutingKind.SNAPSHOT_ITEM
    assert decision.snapshot_id == "snap-visible"
    assert decision.item_id == "snap-visible-item-2"


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


def test_error_plus_integer_routes_reject_notice():
    cache = _cache_with(_summary("snap", 1))
    decision = route_click_n(
        state=OverlayState.ERROR,
        parsed_number=1,
        cache=cache,
        pinned_snapshot_id="snap",
        prior_pinned_snapshot_id=None,
        prior_pin_deferred=False,
    )
    assert decision.kind is RoutingKind.NOTICE
    assert decision.reason == "numbers_not_showing"


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

    forward_click_element / handle_overlay_command and the stub seams are
    bound from the real class; the state machine, cache, config, and queues
    are real or simple fakes. app.send_request is captured.
    """
    from main import LogicController
    from ui.click_config import ClickConfig

    c = MagicMock(spec=LogicController)
    for name in (
        "forward_click_element",
        "_forward_click_notice",
        "_dispatch_snapshot_item_click",
        "_hold_click_n",
        "handle_overlay_command",
        "_perform_overlay_effects",
    ):
        setattr(c, name, getattr(LogicController, name).__get__(c))

    c.click_config = ClickConfig.from_raw(
        {"enabled": True, "response_timeout_ms": 3000}
    )
    c.click_snapshot_summary_cache = _cache_with(*cache_summaries)
    c._click_disabled_notice_shown = False

    machine = ClickOverlayStateMachine()
    machine.state = state
    machine.pinned_snapshot_id = pinned_snapshot_id
    machine.prior_pinned_snapshot_id = prior_pinned_snapshot_id
    machine._prior_pin_deferred = prior_pin_deferred
    c.click_overlay_state = machine

    c.state_manager = MagicMock()
    c.state_manager.state_to_gui_queue = MagicMock()

    captured = {}

    async def _send_request(action, params=None, timeout_s=None):
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

    # First-use hint is irrelevant to routing; make it a no-op coroutine.
    async def _hint(_trace):
        return None

    c._maybe_show_first_use_hint = _hint
    return c


def test_closed_integer_goes_through_send_request_by_name():
    c = _make_controller(state=OverlayState.CLOSED)
    asyncio.run(c.forward_click_element(_query(name="7", role=None), "trace-c"))
    # By-name: the existing send_request flow ran.
    assert c._captured.get("action") == "click_element"


def test_non_integer_name_with_overlay_painted_goes_by_name():
    # role is set -> a spoken role keyword query -> by-name, NOT click-N,
    # even though the overlay is painted.
    c = _make_controller(
        state=OverlayState.PAINTED, pinned_snapshot_id="snap",
        cache_summaries=(_summary("snap", 1),),
    )
    asyncio.run(
        c.forward_click_element(_query(name="seven", role="Button"), "tr")
    )
    assert c._captured.get("action") == "click_element"


def test_painted_integer_dispatches_snapshot_item_stub_not_send_request():
    c = _make_controller(
        state=OverlayState.PAINTED, pinned_snapshot_id="snap",
        cache_summaries=(_summary("snap", 1, 2, 3),),
    )
    c._dispatch_snapshot_item_click = MagicMock()
    asyncio.run(
        c.forward_click_element(_query(name="2", role=None), "tr")
    )
    # No by-name IPC.
    assert "action" not in c._captured
    c._dispatch_snapshot_item_click.assert_called_once()
    _, kwargs = c._dispatch_snapshot_item_click.call_args
    assert kwargs.get("snapshot_id") == "snap"
    assert kwargs.get("item_id") == "snap-item-2"


def test_painted_integer_miss_emits_notice_no_send_request():
    c = _make_controller(
        state=OverlayState.PAINTED, pinned_snapshot_id="snap",
        cache_summaries=(_summary("snap", 1),),
    )
    c._forward_click_notice = MagicMock()
    asyncio.run(
        c.forward_click_element(_query(name="9", role=None), "tr")
    )
    assert "action" not in c._captured
    c._forward_click_notice.assert_called_once()
    _, kwargs = c._forward_click_notice.call_args
    assert kwargs.get("outcome") == "execution_failed"
    assert kwargs.get("reason") == "no_badge_numbered"


def test_held_state_integer_routes_hold_stub_no_send_request():
    c = _make_controller(
        state=OverlayState.WALK_IN_FLIGHT, pinned_snapshot_id=None,
    )
    c._hold_click_n = MagicMock()
    asyncio.run(
        c.forward_click_element(_query(name="3", role=None), "tr")
    )
    assert "action" not in c._captured
    c._hold_click_n.assert_called_once()


def test_error_state_integer_emits_reject_notice_no_send_request():
    c = _make_controller(state=OverlayState.ERROR)
    c._forward_click_notice = MagicMock()
    asyncio.run(
        c.forward_click_element(_query(name="1", role=None), "tr")
    )
    assert "action" not in c._captured
    c._forward_click_notice.assert_called_once()
    _, kwargs = c._forward_click_notice.call_args
    assert kwargs.get("reason") == "numbers_not_showing"


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
    c._forward_click_notice = MagicMock()
    asyncio.run(
        c.forward_click_element(_query(name="1", role=None), "tr")
    )
    c._forward_click_notice.assert_called_once()
    _, kwargs = c._forward_click_notice.call_args
    assert kwargs.get("reason") == "numbers_not_showing"
    assert kwargs.get("snapshot_id") is None


def test_disabled_config_short_circuits_before_overlay_routing():
    c = _make_controller(state=OverlayState.PAINTED, pinned_snapshot_id="snap",
                         cache_summaries=(_summary("snap", 1),))
    from ui.click_config import ClickConfig
    c.click_config = ClickConfig.from_raw({"enabled": False})
    c._dispatch_snapshot_item_click = MagicMock()
    asyncio.run(
        c.forward_click_element(_query(name="1", role=None), "tr")
    )
    # Config gate wins: no overlay dispatch, no IPC.
    c._dispatch_snapshot_item_click.assert_not_called()
    assert "action" not in c._captured


# ---------------------------------------------------------------------------
# Logic wiring: handle_overlay_command applies the events + hands off effects.
# ---------------------------------------------------------------------------


def test_handle_overlay_show_applies_show_numbers_and_dispatches_effects():
    c = _make_controller(state=OverlayState.CLOSED)
    c._perform_overlay_effects = MagicMock()
    asyncio.run(c.handle_overlay_command("show", "tr-show"))
    # The machine left closed for a walk.
    assert c.click_overlay_state.state is OverlayState.WALK_IN_FLIGHT
    c._perform_overlay_effects.assert_called_once()
    effects = c._perform_overlay_effects.call_args[0][0]
    kinds = [e.kind for e in effects]
    assert EffectKind.DISPATCH_BUILD in kinds
    assert EffectKind.ARM_TIMER in kinds


def test_handle_overlay_hide_applies_hide_numbers_and_dispatches_effects():
    # Drive a machine to painted first, then hide.
    c = _make_controller(state=OverlayState.CLOSED)
    m = c.click_overlay_state
    m.apply(__import__(
        "services.wheelhouse.click_overlay_state", fromlist=["OverlayEvent"]
    ).OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sess, gen = m.overlay_session_id, m.paint_generation
    from services.wheelhouse.click_overlay_state import OverlayEvent, PaintAckState
    m.apply(OverlayEvent(OverlayEventKind.BUILD_RESPONSE,
                         overlay_session_id=sess, paint_generation=gen,
                         snapshot_id="snap"))
    m.apply(OverlayEvent(OverlayEventKind.PAINT_ACK,
                         overlay_session_id=sess, paint_generation=gen,
                         paint_state=PaintAckState.PAINTED))
    assert m.state is OverlayState.PAINTED

    c._perform_overlay_effects = MagicMock()
    asyncio.run(c.handle_overlay_command("hide", "tr-hide"))
    assert m.state is OverlayState.CLOSED
    c._perform_overlay_effects.assert_called_once()
    effects = c._perform_overlay_effects.call_args[0][0]
    kinds = [e.kind for e in effects]
    assert EffectKind.DISPATCH_CLEAR in kinds


def test_handle_overlay_command_gated_on_disabled_config():
    c = _make_controller(state=OverlayState.CLOSED)
    from ui.click_config import ClickConfig
    c.click_config = ClickConfig.from_raw({"enabled": False})
    c._perform_overlay_effects = MagicMock()
    asyncio.run(c.handle_overlay_command("show", "tr"))
    # Disabled: the machine never left closed, no effects performed.
    assert c.click_overlay_state.state is OverlayState.CLOSED
    c._perform_overlay_effects.assert_not_called()


# ---------------------------------------------------------------------------
# Overlay-only disabled gate (wh-n29v.66.1.1, codex finding).
#
# A bad overlay key (here overlay_focus_debounce_ms=5001, outside the
# validated [0, 5000] range) disables ONLY the numbered overlay: ClickConfig
# records overlay_focus_debounce_ms=250 AND overlay_enabled_effective=False
# while enabled STAYS True (by-name click keeps working). The Input process
# already gates the overlay walk on overlay_enabled_effective
# (ui/ui_action_handler.py:_get_overlay_walk_finder), so the Logic-side
# overlay-only entry points and the focus-hook thread must gate on the SAME
# overlay_enabled_effective, not on the coarser enabled flag. Otherwise Logic
# would start the Win32 focus-hook thread and accept show/hide commands for an
# overlay the validated config says is off, while Input refuses to walk -- the
# two processes disagreeing on whether the overlay is on, which is exactly the
# parity the click_config docstring (overlay_enabled_effective) promises.
# ---------------------------------------------------------------------------


def _overlay_disabled_controller():
    """A spec-mock controller whose overlay is effectively off but by-name on.

    Raw overlay_focus_debounce_ms=5001 is out of the validated [0, 5000]
    range, so ClickConfig.from_raw records 250 AND overlay_enabled_effective
    False while enabled stays True (wh-n29v.66.1.1).
    """
    from main import LogicController
    from ui.click_config import ClickConfig

    c = MagicMock(spec=LogicController)
    cfg = ClickConfig.from_raw(
        {"enabled": True, "overlay_focus_debounce_ms": 5001}
    )
    assert cfg.enabled is True
    assert cfg.overlay_enabled_effective is False
    c.click_config = cfg
    # Needed so _start_overlay_focus_hooks reaches the OverlayFocusHookManager
    # construction pre-fix (the manager takes loop=self.loop); without it the
    # pre-fix path would AttributeError on self.loop and falsely look gated.
    c.loop = MagicMock()
    return c


def test_handle_overlay_command_gated_on_overlay_disabled():
    # Overlay-only disabled: show/hide is a no-op even though by-name click
    # (enabled) is still live, matching the Input-side overlay walk gate.
    c = _make_controller(state=OverlayState.CLOSED)
    from ui.click_config import ClickConfig

    c.click_config = ClickConfig.from_raw(
        {"enabled": True, "overlay_focus_debounce_ms": 5001}
    )
    assert c.click_config.enabled is True
    assert c.click_config.overlay_enabled_effective is False
    c._perform_overlay_effects = MagicMock()
    asyncio.run(c.handle_overlay_command("show", "tr"))
    assert c.click_overlay_state.state is OverlayState.CLOSED
    c._perform_overlay_effects.assert_not_called()


def test_start_overlay_focus_hooks_gated_on_overlay_disabled():
    # Overlay-only disabled: the Win32 focus-hook thread is never created.
    from main import LogicController

    c = _overlay_disabled_controller()
    c._start_overlay_focus_hooks = (
        LogicController._start_overlay_focus_hooks.__get__(c)
    )
    c._overlay_focus_hooks = None
    with patch("main.OverlayFocusHookManager") as mgr:
        c._start_overlay_focus_hooks()
    mgr.assert_not_called()
    assert c._overlay_focus_hooks is None


def test_on_overlay_foreground_change_gated_on_overlay_disabled():
    # Overlay-only disabled: a foreground change touches neither the debouncer
    # nor the state machine.
    from main import LogicController

    c = _overlay_disabled_controller()
    c._on_overlay_foreground_change = (
        LogicController._on_overlay_foreground_change.__get__(c)
    )
    c._overlay_focus_debouncer = MagicMock()
    c._apply_overlay_event = MagicMock()
    c._on_overlay_foreground_change(12345)
    c._overlay_focus_debouncer.should_fire.assert_not_called()
    c._apply_overlay_event.assert_not_called()


def test_on_overlay_focused_hwnd_destroyed_gated_on_overlay_disabled():
    # Overlay-only disabled: a destroy callback for the tracked window is
    # dropped before any state-machine event (would fire pre-fix because the
    # destroyed hwnd matches the tracked identity).
    from types import SimpleNamespace

    from main import LogicController

    c = _overlay_disabled_controller()
    c._on_overlay_focused_hwnd_destroyed = (
        LogicController._on_overlay_focused_hwnd_destroyed.__get__(c)
    )
    c._apply_overlay_event = MagicMock()
    c._overlay_destroy_hook_active = True
    c._overlay_tracked_identity = SimpleNamespace(hwnd=999)
    c._on_overlay_focused_hwnd_destroyed(999)
    c._apply_overlay_event.assert_not_called()


def test_start_overlay_focus_hooks_proceeds_when_overlay_enabled():
    # Positive control: a valid overlay config (overlay_enabled_effective True)
    # still starts the focus-hook thread.
    from main import LogicController
    from ui.click_config import ClickConfig

    c = MagicMock(spec=LogicController)
    c.click_config = ClickConfig.from_raw({"enabled": True})
    assert c.click_config.overlay_enabled_effective is True
    c._start_overlay_focus_hooks = (
        LogicController._start_overlay_focus_hooks.__get__(c)
    )
    c._overlay_focus_hooks = None
    c.loop = MagicMock()
    with patch("main.OverlayFocusHookManager") as mgr:
        mgr.return_value.start.return_value = True
        c._start_overlay_focus_hooks()
    mgr.assert_called_once()
    assert c._overlay_focus_hooks is mgr.return_value


def test_on_overlay_foreground_change_proceeds_when_overlay_enabled():
    # Positive control: a valid overlay config consults the debouncer on a
    # foreground change.
    from main import LogicController
    from ui.click_config import ClickConfig

    c = MagicMock(spec=LogicController)
    c.click_config = ClickConfig.from_raw({"enabled": True})
    assert c.click_config.overlay_enabled_effective is True
    c._on_overlay_foreground_change = (
        LogicController._on_overlay_foreground_change.__get__(c)
    )
    c._overlay_focus_debouncer = MagicMock()
    # Coalesce so the method returns after the debounce without driving the
    # real state machine.
    c._overlay_focus_debouncer.should_fire.return_value = False
    c._apply_overlay_event = MagicMock()
    c._on_overlay_foreground_change(123)
    c._overlay_focus_debouncer.should_fire.assert_called_once()
    c._apply_overlay_event.assert_not_called()


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


def _master_disabled_controller():
    """A spec-mock controller with voice clicking off (valid enabled=false).

    A valid enabled=false leaves overlay_enabled_effective True, so these
    tests confirm the enabled term (not overlay_enabled_effective) is what
    blocks the overlay work under the master opt-out.
    """
    from main import LogicController
    from ui.click_config import ClickConfig

    c = MagicMock(spec=LogicController)
    cfg = ClickConfig.from_raw({"enabled": False})
    assert cfg.enabled is False
    assert cfg.overlay_enabled_effective is True
    c.click_config = cfg
    c.loop = MagicMock()
    return c


def test_start_overlay_focus_hooks_gated_on_master_disabled():
    from main import LogicController

    c = _master_disabled_controller()
    c._start_overlay_focus_hooks = (
        LogicController._start_overlay_focus_hooks.__get__(c)
    )
    c._overlay_focus_hooks = None
    with patch("main.OverlayFocusHookManager") as mgr:
        c._start_overlay_focus_hooks()
    mgr.assert_not_called()
    assert c._overlay_focus_hooks is None


def test_on_overlay_foreground_change_gated_on_master_disabled():
    from main import LogicController

    c = _master_disabled_controller()
    c._on_overlay_foreground_change = (
        LogicController._on_overlay_foreground_change.__get__(c)
    )
    c._overlay_focus_debouncer = MagicMock()
    c._apply_overlay_event = MagicMock()
    c._on_overlay_foreground_change(12345)
    c._overlay_focus_debouncer.should_fire.assert_not_called()
    c._apply_overlay_event.assert_not_called()


def test_on_overlay_focused_hwnd_destroyed_gated_on_master_disabled():
    from types import SimpleNamespace

    from main import LogicController

    c = _master_disabled_controller()
    c._on_overlay_focused_hwnd_destroyed = (
        LogicController._on_overlay_focused_hwnd_destroyed.__get__(c)
    )
    c._apply_overlay_event = MagicMock()
    c._overlay_destroy_hook_active = True
    c._overlay_tracked_identity = SimpleNamespace(hwnd=999)
    c._on_overlay_focused_hwnd_destroyed(999)
    c._apply_overlay_event.assert_not_called()


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

from services.wheelhouse.speech.overlay_click_router import (  # noqa: E402
    OVERLAY_NUMBERS_CHANGED,
    renumber_click_is_safe,
)


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


def test_renumber_unsafe_when_badge_identity_changed():
    prior = _named_summary("old", {2: "Submit"})
    current = _named_summary("new", {2: "Delete"})
    assert renumber_click_is_safe(prior, current, 2) is False


def test_renumber_safe_when_prior_summary_unavailable():
    # The prior snapshot aged out of the cache: nothing to compare against,
    # so do not block (the guard is best-effort, never a hard gate).
    current = _named_summary("new", {1: "Delete"})
    assert renumber_click_is_safe(None, current, 1) is True


def test_renumber_safe_when_number_is_new():
    # N did not exist before the swap, so the user can only have read it on
    # the NEW overlay -- no mental-map divergence possible.
    prior = _named_summary("old", {1: "Submit"})
    current = _named_summary("new", {1: "Submit", 2: "Delete"})
    assert renumber_click_is_safe(prior, current, 2) is True


def test_renumber_safe_when_current_summary_unavailable():
    prior = _named_summary("old", {1: "Submit"})
    assert renumber_click_is_safe(prior, None, 1) is True


def _guarded_controller(*, cache_summaries, swap, now=100.0):
    """A routing controller with the REAL renumber-guard method bound."""
    from main import LogicController

    c = _make_controller(
        state=OverlayState.PAINTED, pinned_snapshot_id="snap-new",
        cache_summaries=cache_summaries,
    )
    c._overlay_renumber_click_safe = (
        LogicController._overlay_renumber_click_safe.__get__(c)
    )
    c._overlay_now_monotonic = lambda: now
    c._overlay_proactive_swap = swap
    c._dispatch_snapshot_item_click = MagicMock()
    c._forward_click_notice = MagicMock()
    return c


def test_click_n_within_grace_after_proactive_swap_blocked_when_renumbered():
    c = _guarded_controller(
        cache_summaries=(
            _named_summary("snap-old", {2: "Submit"}),
            _named_summary("snap-new", {2: "Delete"}),
        ),
        swap=("snap-old", 99.0),  # 1s ago -- inside the grace window
    )
    asyncio.run(c.forward_click_element(_query(name="2", role=None), "tr-rg"))
    c._dispatch_snapshot_item_click.assert_not_called()
    c._forward_click_notice.assert_called_once()
    _, kwargs = c._forward_click_notice.call_args
    assert kwargs.get("reason") == OVERLAY_NUMBERS_CHANGED
    assert kwargs.get("outcome") == "execution_failed"
    # One block per swap: the guard is consumed so the user's corrected
    # follow-up click is never blocked by the same swap.
    assert c._overlay_proactive_swap is None


def test_click_n_within_grace_proceeds_when_badge_name_unchanged():
    c = _guarded_controller(
        cache_summaries=(
            _named_summary("snap-old", {2: "Submit"}),
            _named_summary("snap-new", {2: "Submit"}),
        ),
        swap=("snap-old", 99.0),
    )
    asyncio.run(c.forward_click_element(_query(name="2", role=None), "tr-rg2"))
    c._dispatch_snapshot_item_click.assert_called_once()
    c._forward_click_notice.assert_not_called()


def test_click_n_after_grace_expiry_proceeds_and_clears_guard():
    c = _guarded_controller(
        cache_summaries=(
            _named_summary("snap-old", {2: "Submit"}),
            _named_summary("snap-new", {2: "Delete"}),
        ),
        swap=("snap-old", 90.0),  # 10s ago -- grace long expired
    )
    asyncio.run(c.forward_click_element(_query(name="2", role=None), "tr-rg3"))
    c._dispatch_snapshot_item_click.assert_called_once()
    c._forward_click_notice.assert_not_called()
    assert c._overlay_proactive_swap is None


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
