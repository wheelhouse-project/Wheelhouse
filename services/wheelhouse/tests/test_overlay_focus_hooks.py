"""Unit tests for the Logic-side overlay focus-hook decision logic (wh-n29v.21).

These tests cover the PURE, Win32-free decision logic in
``services/wheelhouse/overlay_focus_hooks.py``:

  1. The debounce window (collapses rapid foreground changes within
     ``overlay_focus_debounce_ms``, default 250).
  2. The paint_generation supersession drop (a debounced callback whose
     ``(overlay_session_id, paint_generation)`` no longer matches the
     machine's current pair is dropped).
  3. The resume full-foreground-identity comparison (HWND + PID + process
     name + window creation time -- NOT IsWindow alone).
  4. The raw foreground / destroy event -> ``OverlayEvent`` mapping.

The raw ``SetWinEventHook`` registration + message pump is a thin seam in
``main.py``; it is NOT exercised here (no faked OS event loop). The pure
logic takes injected inputs so it is testable without any live Win32 call.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.click_overlay_state import (
    OverlayEventKind,
)
from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity, FocusChangeDebouncer, identity_matches, map_destroy_event, map_foreground_event


# ---------------------------------------------------------------------------
# Debounce window.
# ---------------------------------------------------------------------------


def test_event_after_window_fires_again():
    deb = FocusChangeDebouncer(debounce_ms=250)
    assert deb.should_fire(now_ms=1000.0) is True
    # 250 ms later: at the window edge -> fires (>= boundary is allowed).
    assert deb.should_fire(now_ms=1250.0) is True


def test_dropped_event_does_not_advance_the_window():
    deb = FocusChangeDebouncer(debounce_ms=250)
    assert deb.should_fire(now_ms=1000.0) is True
    # A dropped event at 1100 must NOT reset the window to 1100; the next
    # gate is still measured from the 1000 fire, so 1250 fires.
    assert deb.should_fire(now_ms=1100.0) is False
    assert deb.should_fire(now_ms=1250.0) is True


def test_zero_debounce_fires_every_time():
    deb = FocusChangeDebouncer(debounce_ms=0)
    assert deb.should_fire(now_ms=1000.0) is True
    assert deb.should_fire(now_ms=1000.0) is True
    assert deb.should_fire(now_ms=1000.1) is True


def test_reset_clears_the_window():
    deb = FocusChangeDebouncer(debounce_ms=250)
    assert deb.should_fire(now_ms=1000.0) is True
    deb.reset()
    # After reset the next event is treated as a first event again.
    assert deb.should_fire(now_ms=1001.0) is True


# ---------------------------------------------------------------------------
# Phase 1.5 parity: the Logic process builds its focus-change debouncer from
# the VALIDATED overlay config value (ClickConfig.overlay_focus_debounce_ms),
# not from an independent raw read of the [click] block, so Logic and Input
# can never disagree on the validated config (wh-n29v.66). The former raw
# reader (read_focus_debounce_ms) was retired with this fix: it had no
# remaining production caller and was a second, divergent reader of the same
# key. The validating ClickConfig reader (ui/click_config.py, wh-n29v.29)
# owns the [0, 5000] range, default 250, and the 0 = no-debounce rule, and is
# covered by tests/test_click_config.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Resume full-foreground-identity comparison.
# ---------------------------------------------------------------------------


def _ident(hwnd=111, pid=222, name="app.exe", created=999) -> ForegroundIdentity:
    return ForegroundIdentity(
        hwnd=hwnd, pid=pid, process_name=name, window_creation_time=created
    )


def test_identity_matches_when_all_fields_equal():
    assert identity_matches(_ident(), _ident()) is True


def test_identity_mismatch_on_hwnd():
    assert identity_matches(_ident(), _ident(hwnd=112)) is False


def test_identity_mismatch_on_pid():
    assert identity_matches(_ident(), _ident(pid=223)) is False


def test_identity_mismatch_on_process_name():
    assert identity_matches(_ident(), _ident(name="other.exe")) is False


def test_identity_mismatch_on_creation_time():
    # The HWND-reuse trap: same HWND + PID, different creation time -> mismatch.
    assert identity_matches(_ident(), _ident(created=1000)) is False


def test_identity_reused_hwnd_on_different_process_is_mismatch():
    # A recycled HWND landing on a different process must NOT match (the r2.8
    # case IsWindow alone would miss).
    captured = _ident(hwnd=500, pid=10, name="brave.exe", created=100)
    current = _ident(hwnd=500, pid=99, name="explorer.exe", created=200)
    assert identity_matches(captured, current) is False


# ---------------------------------------------------------------------------
# Raw-event -> OverlayEvent mapping.
# ---------------------------------------------------------------------------


def test_map_foreground_event_does_not_dismiss_the_numbers_unless_told_to():
    """wh-numbers-stay-setting: the flag defaults to False and carries True."""
    assert map_foreground_event(hwnd=4242).dismisses_numbers is False
    assert map_foreground_event(
        hwnd=4242, dismisses_numbers=True,
    ).dismisses_numbers is True


def test_map_destroy_event_matches_tracked_hwnd():
    ev = map_destroy_event(destroyed_hwnd=777, tracked_hwnd=777)
    assert ev is not None
    assert ev.kind is OverlayEventKind.FOCUSED_HWND_DESTROYED


def test_map_destroy_event_ignores_other_hwnd():
    # The destroy hook is filtered to the tracked window's pid/tid, but a
    # different HWND in that process must NOT drive the paused->closed edge.
    assert map_destroy_event(destroyed_hwnd=778, tracked_hwnd=777) is None


def test_map_destroy_event_ignores_zero_tracked_hwnd():
    # No tracked window -> never produce a destroy event.
    assert map_destroy_event(destroyed_hwnd=777, tracked_hwnd=0) is None


# ---------------------------------------------------------------------------
# Menu pop-up open/close re-walk (wh-overlay-menu-close-stale).
#
# Closing a menu does not change the foreground window, so the FOREGROUND hook
# never fires and badges painted for the (now gone) menu items stay floating
# over page controls that have their own badges. The fix registers the
# EVENT_SYSTEM_MENUPOPUPSTART / MENUPOPUPEND range on the same hook thread and
# maps both to the existing FOCUS_CHANGE event: the machine's per-state
# focus-change handling (supersede-refresh when visible, restart while
# building, no-op when closed / paused / error) is exactly the desired menu
# behaviour, so no new event kind or state-machine change is needed.
# ---------------------------------------------------------------------------


def test_map_menu_popup_event_returns_none_for_other_event_ids():
    from services.wheelhouse.overlay_focus_hooks import map_menu_popup_event

    # Foreground (0x0003), MENUSTART (0x0004, the menu-BAR event), and
    # OBJECT_DESTROY (0x8003) must not map -- only the pop-up pair does.
    for event_id in (0x0003, 0x0004, 0x0005, 0x8003, 0):
        assert map_menu_popup_event(event_id=event_id) is None


# ---------------------------------------------------------------------------
# Trailing settle re-fire (reviewer_0 finding wh-overlay-nested-dupes.1.1):
# the drop-only debounce loses the FINAL event of a burst (double-Escape,
# open-then-instant-dismiss, dialog-after-menu-close), leaving dead badges.
# A coalesced event now arms a one-shot timer for the debounce-window
# remainder so the final state always gets exactly one re-walk.
# ---------------------------------------------------------------------------


def test_remaining_ms_zero_before_any_fire():
    from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer

    d = FocusChangeDebouncer(debounce_ms=250)
    assert d.remaining_ms(now_ms=1_000.0) == 0.0


def test_remaining_ms_counts_down_from_a_fire_and_clears():
    from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer

    d = FocusChangeDebouncer(debounce_ms=250)
    assert d.should_fire(now_ms=1_000.0)
    assert d.remaining_ms(now_ms=1_000.0) == 250.0
    assert d.remaining_ms(now_ms=1_100.0) == 150.0
    assert d.remaining_ms(now_ms=1_250.0) == 0.0
    assert d.remaining_ms(now_ms=2_000.0) == 0.0


def test_remaining_ms_zero_when_debounce_disabled():
    from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer

    d = FocusChangeDebouncer(debounce_ms=0)
    assert d.should_fire(now_ms=1_000.0)
    assert d.remaining_ms(now_ms=1_000.0) == 0.0


# ---------------------------------------------------------------------------
# wh-overlay-rewalk-after-filter: the Pattern Manager tells Logic its tree
# changed, because the window announces nothing a hook can see (measured: 0 UIA
# StructureChanged events and 0 WinEvents 0x8000..0x8004 for a filter change).
# ``decide_tree_change`` is the pure half: it decides accept or drop, and the
# Logic callback ``_on_pattern_manager_tree_change`` carries out the decision
# through the SAME debouncer and the SAME FOCUS_CHANGE effect the menu pop-up
# path uses.
# ---------------------------------------------------------------------------


def _decide(**kwargs):
    from services.wheelhouse.click_overlay_state import OverlayState
    from services.wheelhouse.overlay_focus_hooks import decide_tree_change

    args = {
        "state": OverlayState.PAINTED,
        "tracked_hwnd": 4242,
        "event_hwnd": 4242,
        "event_sequence": 1,
        "last_hwnd": 0,
        "last_sequence": 0,
    }
    args.update(kwargs)
    return decide_tree_change(**args)


# wh-overlay-rewalk-after-filter.1.1, contract C2 as amended 2026-09-22.
#
# The accepted set is the states whose FOCUS_CHANGE handling rebuilds the
# badge set; the dropped set is the states whose FOCUS_CHANGE handling is a
# no-operation. Every one of the eight OverlayState members is named here, so
# adding a state without deciding which side it belongs on fails this test
# rather than falling silently into the drop.
_TREE_CHANGE_BY_STATE = {
    "PAINTED": None,
    "REFRESH_IN_FLIGHT": None,
    "WALK_IN_FLIGHT": None,
    "PAINT_IN_FLIGHT": None,
    "CLOSED": "overlay_state_closed",
    "POST_CLICK_SETTLING": "overlay_state_post_click_settling",
    "PAUSED": "overlay_state_paused",
    "ERROR": "overlay_state_error",
}


def test_the_state_table_covers_every_overlay_state():
    """The table above must not drift from the enum it describes."""
    from services.wheelhouse.click_overlay_state import OverlayState

    assert set(_TREE_CHANGE_BY_STATE) == {s.name for s in OverlayState}


@pytest.mark.parametrize(
    "state_name,expected_drop", sorted(_TREE_CHANGE_BY_STATE.items())
)
def test_decide_tree_change_decides_every_overlay_state(
    state_name, expected_drop
):
    from services.wheelhouse.click_overlay_state import (
        OverlayEventKind,
        OverlayState,
    )

    decision = _decide(state=getattr(OverlayState, state_name))
    assert decision.drop_reason == expected_drop
    if expected_drop is None:
        assert decision.event is not None
        assert decision.event.kind is OverlayEventKind.FOCUS_CHANGE
        assert decision.accepted_sequence == 1
    else:
        assert decision.event is None
        assert decision.accepted_sequence is None


def test_no_drop_reason_claims_the_overlay_is_closed_unless_it_is():
    """A log line must never say "closed" about an open session.

    The retired constant said ``overlay_closed`` for six of the eight states,
    four of which hold an open overlay session.
    """
    from services.wheelhouse.click_overlay_state import OverlayState

    for state_name, expected_drop in _TREE_CHANGE_BY_STATE.items():
        if expected_drop is None or state_name == "CLOSED":
            continue
        decision = _decide(state=getattr(OverlayState, state_name))
        assert decision.drop_reason is not None
        assert "closed" not in decision.drop_reason


def _machine_in(state):
    """A real state machine driven to ``state`` through its own events.

    Only the two in-flight build states are needed here, and each is reached
    the way the running system reaches it, so the test cannot pass against a
    machine that no longer behaves that way.
    """
    from services.wheelhouse.click_overlay_state import (
        ClickOverlayStateMachine,
        OverlayEvent,
        OverlayEventKind,
        OverlayState,
    )

    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.state is OverlayState.WALK_IN_FLIGHT
    if state is OverlayState.PAINT_IN_FLIGHT:
        m.apply(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE,
                overlay_session_id=m.overlay_session_id,
                paint_generation=m.paint_generation,
                snapshot_id="pre-filter",
            )
        )
        assert m.state is OverlayState.PAINT_IN_FLIGHT
    return m


@pytest.mark.parametrize("state_name", ["WALK_IN_FLIGHT", "PAINT_IN_FLIGHT"])
def test_a_tree_change_in_flight_supersedes_the_build_and_paints_post_filter(
    state_name,
):
    """The end-to-end proof for the two in-flight states (contract C2 amended).

    A filter change that lands while a build is still in flight must not be
    dropped: the in-flight build walked the PRE-filter tree, so painting it
    would put badges on rows the user just hid. The machine's own
    ``FOCUS_CHANGE`` arm already restarts such a build with
    ``BuildReason.SUPERSEDE``; this test drives one real event through
    ``decide_tree_change`` into a real machine and follows the restarted build
    all the way to the paint, so it measures the outcome the user sees -- the
    painted snapshot is the POST-filter one -- and not only the decision.
    """
    from services.wheelhouse.click_overlay_state import (
        BuildReason,
        EffectKind,
        OverlayEvent,
        OverlayEventKind,
        OverlayState,
        PaintAckState,
    )

    m = _machine_in(getattr(OverlayState, state_name))
    generation_before = m.paint_generation

    decision = _decide(state=m.state)
    assert decision.drop_reason is None
    assert decision.event is not None

    result = m.apply(decision.event)

    builds = [
        e for e in result.effects if e.kind is EffectKind.DISPATCH_BUILD
    ]
    assert len(builds) == 1
    assert builds[0].build_reason is BuildReason.SUPERSEDE
    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert m.paint_generation > generation_before

    # The restarted build walks the tree as it is NOW, so its snapshot is the
    # post-filter one. Follow it through to the paint and confirm that is what
    # reaches the screen.
    m.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE,
            overlay_session_id=m.overlay_session_id,
            paint_generation=m.paint_generation,
            snapshot_id="post-filter",
        )
    )
    assert m.state is OverlayState.PAINT_IN_FLIGHT
    assert m.pinned_snapshot_id == "post-filter"
    m.apply(
        OverlayEvent(
            OverlayEventKind.PAINT_ACK,
            overlay_session_id=m.overlay_session_id,
            paint_generation=m.paint_generation,
            paint_state=PaintAckState.PAINTED,
        )
    )
    assert m.state is OverlayState.PAINTED
    assert m.pinned_snapshot_id == "post-filter"


def test_decide_tree_change_drops_other_window_on_a_different_handle():
    from services.wheelhouse.overlay_focus_hooks import (
        TREE_CHANGE_DROP_OTHER_WINDOW,
    )

    decision = _decide(tracked_hwnd=1111, event_hwnd=2222)
    assert decision.drop_reason == TREE_CHANGE_DROP_OTHER_WINDOW
    assert decision.event is None


@pytest.mark.parametrize(
    "tracked,event_hwnd", [(0, 4242), (4242, 0), (0, 0)]
)
def test_decide_tree_change_drops_other_window_on_a_zero_handle(
    tracked, event_hwnd
):
    from services.wheelhouse.overlay_focus_hooks import (
        TREE_CHANGE_DROP_OTHER_WINDOW,
    )

    decision = _decide(tracked_hwnd=tracked, event_hwnd=event_hwnd)
    assert decision.drop_reason == TREE_CHANGE_DROP_OTHER_WINDOW


def test_decide_tree_change_checks_state_before_the_window():
    # Order matters for the log line: a closed overlay over some other window
    # reports the state, not other_window.
    from services.wheelhouse.click_overlay_state import OverlayState
    from services.wheelhouse.overlay_focus_hooks import (
        TREE_CHANGE_DROP_STATE_CLOSED,
    )

    decision = _decide(
        state=OverlayState.CLOSED, tracked_hwnd=1111, event_hwnd=2222
    )
    assert decision.drop_reason == TREE_CHANGE_DROP_STATE_CLOSED


def test_decide_tree_change_checks_the_window_before_the_sequence():
    from services.wheelhouse.overlay_focus_hooks import (
        TREE_CHANGE_DROP_OTHER_WINDOW,
    )

    decision = _decide(
        tracked_hwnd=1111,
        event_hwnd=2222,
        event_sequence=1,
        last_hwnd=2222,
        last_sequence=9,
    )
    assert decision.drop_reason == TREE_CHANGE_DROP_OTHER_WINDOW


def test_tree_change_decision_is_frozen():
    decision = _decide()
    with pytest.raises(Exception):
        decision.drop_reason = "nope"  # type: ignore[misc]


def test_decide_tree_change_is_exported():
    from services.wheelhouse import overlay_focus_hooks

    for name in (
        "TREE_CHANGE_ACCEPTED_STATES",
        "TREE_CHANGE_DROP_BY_STATE",
        "TREE_CHANGE_DROP_OTHER_WINDOW",
        "TREE_CHANGE_DROP_STALE_SEQUENCE",
        "TREE_CHANGE_DROP_STATE_CLOSED",
        "TREE_CHANGE_DROP_STATE_ERROR",
        "TREE_CHANGE_DROP_STATE_PAUSED",
        "TREE_CHANGE_DROP_STATE_POST_CLICK_SETTLING",
        "TreeChangeDecision",
        "decide_tree_change",
    ):
        assert name in overlay_focus_hooks.__all__

    # The retired name must not come back: it said "closed" about three states
    # holding an open overlay session (wh-overlay-rewalk-after-filter.1.1).
    assert not hasattr(overlay_focus_hooks, "TREE_CHANGE_DROP_OVERLAY_CLOSED")


# ---------------------------------------------------------------------------
# Logic callback: wh-overlay-rewalk-after-filter.1.3, the window an IN-FLIGHT
# build is for.
#
# The pinned identity is assigned only at the pin point, which runs after the
# build response comes back. While a build is in flight it therefore names the
# PREVIOUS window, or nothing at all on the first session after a start. The
# tree-change gate compared that identity against the event's window, so the
# event the amended contract C2 accepts in ``walk_in_flight`` and
# ``paint_in_flight`` was dropped as ``other_window`` and the pre-change build
# painted badges over the filtered-away rows -- the very failure that bead
# exists to remove.
#
# The callback now judges an in-flight tree change against the window the
# in-flight build was dispatched for, and against the pinned identity
# otherwise, so a refresh that falls back to the prior pin
# (``_refresh_fall_back`` in click_overlay_state.py) cannot leave the callback
# judging against a window whose badges are not the ones on screen.
#
# Both tests drive the real controller through the overlay boundary harness:
# speech and focus events in, IPC frames out. A tree change that the callback
# accepts restarts the build (BuildReason.SUPERSEDE), which reaches Input as
# one new ``start_overlay_walk`` at the next paint generation of the same
# overlay session. A dropped tree change sends nothing.
# ---------------------------------------------------------------------------


def _tree_change(hwnd: int, sequence: int) -> dict:
    """A pattern_manager_tree_changed message, through the production schema."""
    from services.wheelhouse.shared.pattern_manager_tree_changed import (
        PatternManagerTreeChangedEvent,
    )

    return PatternManagerTreeChangedEvent(hwnd=hwnd, sequence=sequence).to_dict()


def _ambiguous_reply(frame) -> dict:
    """An ambiguous by-name reply whose finalists the auto-open can show."""
    from shared.click_element import ClickElementResponse
    from tests.overlay_boundary_harness import item_id, summary

    return ClickElementResponse(
        status="ok", outcome="ambiguous", reason=None,
        matched_names=("save", "save as"), snapshot_id="snap-click",
        snapshot_summary=summary("snap-click", 1, 2, 3), matched_name=None,
        trace_id=frame.params.get("trace_id", ""),
        ambiguous_item_ids=(item_id("snap-click", 1), item_id("snap-click", 2)),
    ).to_dict()


async def test_tree_change_accepted_during_a_first_session_auto_open(
    tmp_path, monkeypatch,
):
    # The ambiguous-click path enters the same in-flight state from closed,
    # with no pinned identity yet (the first overlay after a start).
    import tests.overlay_boundary_harness as harness
    from tests.overlay_boundary_harness import (
        NUMBERED_WINDOW,
        overlay_boundary,
        write_config,
    )

    # Answer the by-name click by hand, so the reply can be ambiguous.
    monkeypatch.setattr(harness, "_AUTO_CLICKED", frozenset())
    config = write_config(tmp_path, overlay_focus_debounce_ms=0)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        b.say_soon("click save")
        click = await b.next_frame("click_element")
        b.reply(click, _ambiguous_reply(click))
        # The auto-open build for the numbered window is never answered, so
        # the machine stays in walk_in_flight.
        build = await b.next_frame("show_numbered_overlay")
        from services.wheelhouse.click_overlay_state import OverlayState

        assert b.controller.overlay.click_overlay_state.state is (
            OverlayState.WALK_IN_FLIGHT
        )
        mark = b.mark()

        b.send_gui(_tree_change(NUMBERED_WINDOW.hwnd, 1))
        await b.settle_gui()

        frames, gui = await b.quiet(mark, 0.3)
        assert [f.action for f in frames] == ["start_overlay_walk"]
        assert frames[0].params["overlay_session_id"] == (
            build.params["overlay_session_id"]
        )
        assert frames[0].params["paint_generation"] == (
            build.params["paint_generation"] + 1
        )
        # Nothing was painted yet, so the restart clears nothing.
        assert gui == []


async def test_tree_change_in_a_cross_window_refresh_follows_the_build(
    tmp_path,
):
    # Badges are painted over the numbered window; the user moves to another
    # window and the focus change dispatches a refresh build for it. Until
    # that build's pin lands, the pinned identity still names the numbered
    # window. Keep-numbers is on, so the focus change refreshes instead of
    # dismissing the numbers.
    from tests.overlay_boundary_harness import (
        NUMBERED_WINDOW,
        OTHER_WINDOW,
        overlay_boundary,
        settle,
        write_config,
    )

    config = write_config(
        tmp_path, overlay_focus_debounce_ms=0, overlay_keep_numbers=True,
    )
    async with overlay_boundary(
        tmp_path, config_path=config, windows=(NUMBERED_WINDOW, OTHER_WINDOW),
    ) as b:
        await b.show_numbers_painted("snap-1")
        b.desktop.bring_to_front(OTHER_WINDOW)
        b.hooks.foreground(OTHER_WINDOW.hwnd)
        await settle()
        # The refresh build for the other window is never answered, so the
        # machine stays in refresh_in_flight.
        refresh = await b.next_frame("start_overlay_walk", timeout_s=2.0)
        from services.wheelhouse.click_overlay_state import OverlayState

        assert b.controller.overlay.click_overlay_state.state is (
            OverlayState.REFRESH_IN_FLIGHT
        )
        mark = b.mark()

        b.send_gui(_tree_change(OTHER_WINDOW.hwnd, 1))
        await b.settle_gui()

        frames, gui = await b.quiet(mark, 0.3)
        assert [f.action for f in frames] == ["start_overlay_walk"]
        assert frames[0].params["overlay_session_id"] == (
            refresh.params["overlay_session_id"]
        )
        assert frames[0].params["paint_generation"] == (
            refresh.params["paint_generation"] + 1
        )
        # A supersede of a refresh keeps the visible old badges.
        assert "clear_overlay" not in [
            getattr(m, "action", None) or m["action"] for m in gui
        ]
