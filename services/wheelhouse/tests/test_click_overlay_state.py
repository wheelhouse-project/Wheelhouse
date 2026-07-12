"""Exhaustive tests for the overlay toggle state machine (wh-gxj4kx).

Covers every cell of the v4 "### Allowed inbound events per state" table
(``docs/plans/2026-05-28-voice-element-clicking-phase-1-5-design-v4.md``),
both extra events (``auto_open`` from ``closed`` and its rejection
elsewhere; ``focused_hwnd_destroyed`` from ``paused`` and its rejection
elsewhere), every timeout path, stale-generation rejection on all three
generation-bearing events, generation monotonicity, the
``pending_ambiguous_notice`` and ``auto_hide_in_flight`` lifecycles, the
hide-numbers-immediate-close path with the cleared-ack-is-bookkeeping
NO_OP, and the exact ordered effects for the key transitions.

Effects are asserted as DATA (the returned ``Effect`` tuples), not via
mocks: the state machine is pure and returns its side effects as a value.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.click_overlay_state import (
    ApplyResult,
    BuildReason,
    ClickOverlayStateMachine,
    EffectKind,
    OverlayEvent,
    OverlayEventKind,
    OverlayOutcome,
    OverlayState,
    PaintAckState,
    _NO_TIMEOUT,
)
from services.wheelhouse.shared.click_notice import ClickNoticeEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_notice(spoken: str = "edit") -> ClickNoticeEvent:
    """A minimal valid ClickNoticeEvent for the pending-notice tests."""
    return ClickNoticeEvent(
        outcome="ambiguous",
        reason=None,
        matched_name=None,
        matched_names=("edit", "edit"),
        spoken_name=spoken,
        app_friendly_name="App",
        snapshot_id="snap-1",
        trace_id="trace-1",
    )


def effect_kinds(result: ApplyResult) -> list[EffectKind]:
    return [e.kind for e in result.effects]


def drive_to_painted(m: ClickOverlayStateMachine, snapshot_id: str = "snap") -> None:
    """Take a fresh machine from closed -> painted via the normal flow."""
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.state is OverlayState.WALK_IN_FLIGHT
    sess, gen = m.overlay_session_id, m.paint_generation
    m.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE,
            overlay_session_id=sess,
            paint_generation=gen,
            snapshot_id=snapshot_id,
        )
    )
    assert m.state is OverlayState.PAINT_IN_FLIGHT
    m.apply(
        OverlayEvent(
            OverlayEventKind.PAINT_ACK,
            overlay_session_id=sess,
            paint_generation=gen,
            paint_state=PaintAckState.PAINTED,
        )
    )
    assert m.state is OverlayState.PAINTED


def gen_event(
    m: ClickOverlayStateMachine, kind: OverlayEventKind, **kw
) -> OverlayEvent:
    """A generation-bearing event stamped with the machine's active pair."""
    return OverlayEvent(
        kind,
        overlay_session_id=m.overlay_session_id,
        paint_generation=m.paint_generation,
        **kw,
    )


# ---------------------------------------------------------------------------
# Construction & timeout table
# ---------------------------------------------------------------------------

def test_initial_state_is_closed():
    m = ClickOverlayStateMachine()
    assert m.state is OverlayState.CLOSED
    assert m.overlay_session_id == 0
    assert m.paint_generation == 0
    assert m.pinned_snapshot_id is None
    assert m.pending_ambiguous_notice is None
    assert m.auto_hide_in_flight is False
    assert m.reason == ""


def test_timeout_table_defaults():
    m = ClickOverlayStateMachine()
    assert m.timeout_ms(OverlayState.WALK_IN_FLIGHT) == 2500.0
    assert m.timeout_ms(OverlayState.PAINT_IN_FLIGHT) == 1000.0
    assert m.timeout_ms(OverlayState.REFRESH_IN_FLIGHT) == 2500.0
    for s in (
        OverlayState.CLOSED,
        OverlayState.PAINTED,
        OverlayState.PAUSED,
        OverlayState.ERROR,
    ):
        assert m.timeout_ms(s) == _NO_TIMEOUT


def test_timeout_table_uses_constructor_values():
    m = ClickOverlayStateMachine(walk_deadline_ms=999, paint_deadline_ms=42)
    assert m.timeout_ms(OverlayState.WALK_IN_FLIGHT) == 999.0
    assert m.timeout_ms(OverlayState.REFRESH_IN_FLIGHT) == 999.0
    assert m.timeout_ms(OverlayState.PAINT_IN_FLIGHT) == 42.0


# ---------------------------------------------------------------------------
# closed row
# ---------------------------------------------------------------------------

def test_closed_show_numbers_starts_walk():
    m = ClickOverlayStateMachine()
    r = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert m.overlay_session_id == 1
    assert m.paint_generation == 0
    assert effect_kinds(r) == [EffectKind.DISPATCH_BUILD, EffectKind.ARM_TIMER]
    build, arm = r.effects
    assert build.build_reason is BuildReason.SHOW_NUMBERS
    assert build.overlay_session_id == 1
    assert build.paint_generation == 0
    assert arm.timer_state is OverlayState.WALK_IN_FLIGHT
    assert arm.duration_ms == 2500.0


def test_closed_auto_open_stores_notice_and_walks():
    m = ClickOverlayStateMachine()
    notice = make_notice()
    r = m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=notice))
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert m.pending_ambiguous_notice is notice
    assert effect_kinds(r) == [EffectKind.DISPATCH_BUILD, EffectKind.ARM_TIMER]
    assert r.effects[0].build_reason is BuildReason.AUTO_OPEN


def test_closed_auto_open_threads_reuse_snapshot_id_onto_build_effect():
    # wh-n29v.96.1 (FINDING 1): AUTO_OPEN reuses an EXISTING click snapshot via
    # show_numbered_overlay, but it fires from CLOSED where the machine pin is
    # None. The reuse snapshot id must be carried on the AUTO_OPEN event and
    # stamped onto the DISPATCH_BUILD effect so the integration can thread it to
    # the Input request, instead of reading the (None) live machine pin.
    m = ClickOverlayStateMachine()
    notice = make_notice()
    r = m.apply(
        OverlayEvent(
            OverlayEventKind.AUTO_OPEN, notice=notice, snapshot_id="reuse-snap",
        )
    )
    build = r.effects[0]
    assert build.kind is EffectKind.DISPATCH_BUILD
    assert build.build_reason is BuildReason.AUTO_OPEN
    assert build.snapshot_id == "reuse-snap"
    # The machine pin is still None (auto_open does not pin until the build
    # returns), so the reuse id is genuinely only on the effect.
    assert m.pinned_snapshot_id is None


def test_closed_hide_numbers_is_noop():
    m = ClickOverlayStateMachine()
    r = m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.CLOSED
    assert r.effects == ()


def test_closed_click_n_routes_by_name_noop():
    m = ClickOverlayStateMachine()
    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_N))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.CLOSED


@pytest.mark.parametrize(
    "kind",
    [
        OverlayEventKind.MIC_PAUSE,
        OverlayEventKind.MIC_RESUME,
        OverlayEventKind.FOCUS_CHANGE,
    ],
)
def test_closed_record_only_events_are_noop(kind):
    m = ClickOverlayStateMachine()
    r = m.apply(OverlayEvent(kind))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.CLOSED


@pytest.mark.parametrize(
    "kind",
    [
        OverlayEventKind.FOCUSED_HWND_DESTROYED,
    ],
)
def test_closed_invalid_events(kind):
    m = ClickOverlayStateMachine()
    # FOCUSED_HWND_DESTROYED in closed is the remaining genuine protocol
    # violation (the transient destroy hook is live only while paused). The
    # late-completion kinds (BUILD_RESPONSE / TIMEOUT / CLICK_COMPLETE) are NO
    # LONGER invalid here -- they are teardown NO_OPs after a hide closed the
    # machine without bumping the generation (wh-n29v.19.1, covered by
    # test_late_same_generation_event_after_hide_is_teardown_noop).
    r = m.apply(OverlayEvent(kind))
    assert r.outcome is OverlayOutcome.INVALID_TRANSITION
    assert m.state is OverlayState.ERROR
    assert m.reason.startswith("invalid_transition_from_closed_via_")


def test_closed_paint_ack_is_bookkeeping_noop():
    # A late paint-ack from a torn-down session lands in closed and is
    # consumed as bookkeeping (r2.4), not an error or a resurrection.
    m = ClickOverlayStateMachine()
    r = m.apply(OverlayEvent(OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.CLEARED))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.CLOSED


# ---------------------------------------------------------------------------
# walk_in_flight row
# ---------------------------------------------------------------------------

def test_walk_build_response_to_paint_in_flight():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snap"))
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.PAINT_IN_FLIGHT
    assert m.pinned_snapshot_id == "snap"
    assert effect_kinds(r) == [
        EffectKind.CANCEL_TIMER,
        EffectKind.PIN_SNAPSHOT,
        EffectKind.DISPATCH_PAINT,
        EffectKind.ARM_TIMER,
    ]
    pin, paint, arm = r.effects[1], r.effects[2], r.effects[3]
    assert pin.snapshot_id == "snap"
    assert paint.snapshot_id == "snap"
    assert paint.immediate_clear is False
    assert arm.timer_state is OverlayState.PAINT_IN_FLIGHT
    assert arm.duration_ms == 1000.0


def test_walk_build_response_not_ok_fires_pending_and_closes():
    m = ClickOverlayStateMachine()
    notice = make_notice()
    m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=notice))
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, build_ok=False))
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.CLOSED
    assert m.pending_ambiguous_notice is None
    kinds = effect_kinds(r)
    assert EffectKind.FIRE_NOTICE in kinds
    fired = [e for e in r.effects if e.kind is EffectKind.FIRE_NOTICE]
    assert fired[0].notice is notice


def test_walk_show_numbers_restarts_bumps_gen():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.paint_generation == 0
    r = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert m.paint_generation == 1
    assert effect_kinds(r) == [
        EffectKind.CANCEL_TIMER,
        EffectKind.DISPATCH_BUILD,
        EffectKind.ARM_TIMER,
    ]
    # session id unchanged (a restart, not a new session)
    assert m.overlay_session_id == 1


def test_walk_focus_change_supersedes():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    r = m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert m.paint_generation == 1
    assert r.effects[1].build_reason is BuildReason.SUPERSEDE


def test_walk_restart_unpins_old_when_pinned():
    # Get a pinned snapshot first (walk -> paint_in_flight pins), then back
    # to walk via a restart to confirm the unpin fires.
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snap"))
    assert m.pinned_snapshot_id == "snap"
    r = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))  # paint_in_flight restart
    assert m.state is OverlayState.WALK_IN_FLIGHT
    kinds = effect_kinds(r)
    assert EffectKind.UNPIN_SNAPSHOT in kinds
    assert m.pinned_snapshot_id is None
    unpin = [e for e in r.effects if e.kind is EffectKind.UNPIN_SNAPSHOT][0]
    assert unpin.snapshot_id == "snap"


def test_walk_hide_numbers_to_closed():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    r = m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    assert EffectKind.DISPATCH_CLEAR in effect_kinds(r)


def test_walk_mic_pause_sets_flag_stays():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert m.auto_hide_in_flight is True
    assert r.effects == ()


def test_walk_mic_resume_noop():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.WALK_IN_FLIGHT


def test_walk_click_n_held():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_N))
    assert r.outcome is OverlayOutcome.HELD
    assert m.state is OverlayState.WALK_IN_FLIGHT


def test_walk_timeout_to_closed():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.CLOSED
    assert effect_kinds(r)[0] is EffectKind.CANCEL_TIMER


def test_walk_timeout_fires_pending_notice():
    m = ClickOverlayStateMachine()
    notice = make_notice()
    m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=notice))
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.CLOSED
    fired = [e for e in r.effects if e.kind is EffectKind.FIRE_NOTICE]
    assert fired and fired[0].notice is notice
    assert m.pending_ambiguous_notice is None


def test_walk_timeout_standalone_fires_generic_notice():
    # wh-n29v.16.1: a standalone "show numbers" whose walk times out must signal
    # the generic "numbers couldn't be drawn" notice (v4 line 278). The pure
    # machine has no ClickNoticeEvent for it, so it emits FIRE_NOTICE with
    # notice=None and the integration constructs the text.
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))  # standalone: no pending notice
    assert m.pending_ambiguous_notice is None
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.CLOSED
    fires = [e for e in r.effects if e.kind is EffectKind.FIRE_NOTICE]
    assert len(fires) == 1
    assert fires[0].notice is None  # the generic standalone-failure marker


def test_walk_build_failed_standalone_fires_generic_notice():
    # wh-n29v.16.1: a standalone walk that returns build_ok=False also signals
    # the generic standalone-failure notice.
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, build_ok=False))
    assert m.state is OverlayState.CLOSED
    fires = [e for e in r.effects if e.kind is EffectKind.FIRE_NOTICE]
    assert len(fires) == 1
    assert fires[0].notice is None


def test_auto_open_walk_timeout_fires_pending_not_standalone():
    # wh-n29v.16.1: with an auto-open in flight, the walk-timeout fires the
    # SPECIFIC pending ambiguous-click notice -- exactly one notice, and it is
    # the pending one, not the generic (notice=None) marker.
    m = ClickOverlayStateMachine()
    notice = make_notice()
    m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=notice))
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.CLOSED
    fires = [e for e in r.effects if e.kind is EffectKind.FIRE_NOTICE]
    assert len(fires) == 1
    assert fires[0].notice is notice


@pytest.mark.parametrize(
    "kind",
    [
        OverlayEventKind.PAINT_ACK,
        OverlayEventKind.CLICK_COMPLETE,
        OverlayEventKind.AUTO_OPEN,
        OverlayEventKind.FOCUSED_HWND_DESTROYED,
    ],
)
def test_walk_invalid_events(kind):
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    ev = gen_event(m, kind) if kind is OverlayEventKind.PAINT_ACK else OverlayEvent(kind)
    r = m.apply(ev)
    assert r.outcome is OverlayOutcome.INVALID_TRANSITION
    assert m.state is OverlayState.ERROR


# ---------------------------------------------------------------------------
# paint_in_flight row
# ---------------------------------------------------------------------------

def to_paint_in_flight(m: ClickOverlayStateMachine, snapshot_id="snap"):
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id=snapshot_id))
    assert m.state is OverlayState.PAINT_IN_FLIGHT


def test_paint_ack_painted_to_painted():
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    assert m.state is OverlayState.PAINTED
    assert effect_kinds(r) == [EffectKind.CANCEL_TIMER]


def test_paint_ack_failed_fires_pending_to_closed():
    m = ClickOverlayStateMachine()
    notice = make_notice()
    m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=notice))
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snap"))
    assert m.state is OverlayState.PAINT_IN_FLIGHT
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.FAILED))
    assert m.state is OverlayState.CLOSED
    fired = [e for e in r.effects if e.kind is EffectKind.FIRE_NOTICE]
    assert fired and fired[0].notice is notice
    assert EffectKind.UNPIN_SNAPSHOT in effect_kinds(r)


def test_paint_ack_cleared_is_bookkeeping_noop():
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.CLEARED))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.PAINT_IN_FLIGHT


def test_paint_show_numbers_restart_unpins():
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert m.paint_generation == 1
    assert EffectKind.UNPIN_SNAPSHOT in effect_kinds(r)


def test_paint_focus_change_restart():
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert r.effects[-2].build_reason is BuildReason.SUPERSEDE


def test_paint_hide_to_closed():
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    assert EffectKind.DISPATCH_CLEAR in effect_kinds(r)
    assert EffectKind.UNPIN_SNAPSHOT in effect_kinds(r)


def test_paint_mic_pause_hides_sets_flag_stays():
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.state is OverlayState.PAINT_IN_FLIGHT
    assert m.auto_hide_in_flight is True
    assert effect_kinds(r) == [EffectKind.DISPATCH_CLEAR]


def test_paint_click_n_held():
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_N))
    assert r.outcome is OverlayOutcome.HELD


def test_paint_timeout_to_closed():
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.CLOSED
    assert EffectKind.UNPIN_SNAPSHOT in effect_kinds(r)


def test_paint_timeout_to_closed_clears_orphaned_overlay():
    # wh-n29v.15.1: a paint_in_flight timeout tears down to closed. A paint was
    # already dispatched (a snapshot is pinned), so the machine emits a
    # DISPATCH_CLEAR -- if the GUI rendered that paint just before the timeout,
    # the badges would otherwise orphan on screen with no way to dismiss them.
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.CLOSED
    kinds = effect_kinds(r)
    assert kinds[0] is EffectKind.CANCEL_TIMER
    assert EffectKind.DISPATCH_CLEAR in kinds
    # the clear precedes the unpin (clear the GUI window, then release the pin)
    assert kinds.index(EffectKind.DISPATCH_CLEAR) < kinds.index(
        EffectKind.UNPIN_SNAPSHOT
    )


def test_paint_ack_failed_clears_orphaned_overlay():
    # wh-n29v.15.1: a failed paint-ack also tears down to closed with a pin set;
    # a "failed" ack can still mean a partial paint, so emit the clear.
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.FAILED))
    assert m.state is OverlayState.CLOSED
    assert EffectKind.DISPATCH_CLEAR in effect_kinds(r)


def test_paint_timeout_standalone_emits_no_notice():
    # wh-n29v.16.1 boundary: per v4 line 279 the paint-phase failure fires only
    # the pending (auto-open) notice. A STANDALONE paint timeout therefore emits
    # NO FIRE_NOTICE -- the generic standalone notice is walk-phase only.
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)  # standalone: no pending notice
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.CLOSED
    assert EffectKind.FIRE_NOTICE not in effect_kinds(r)


def test_walk_timeout_to_closed_emits_no_clear():
    # wh-n29v.15.1 boundary: a walk_in_flight timeout has nothing pinned (no
    # paint was ever dispatched), so NO spurious DISPATCH_CLEAR is emitted.
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.CLOSED
    assert EffectKind.DISPATCH_CLEAR not in effect_kinds(r)


def test_paint_mic_resume_noop():
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME))
    assert r.outcome is OverlayOutcome.NO_OP


@pytest.mark.parametrize(
    "kind",
    [
        OverlayEventKind.CLICK_COMPLETE,
        OverlayEventKind.AUTO_OPEN,
        OverlayEventKind.FOCUSED_HWND_DESTROYED,
    ],
)
def test_paint_invalid_events(kind):
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(OverlayEvent(kind))
    assert r.outcome is OverlayOutcome.INVALID_TRANSITION
    assert m.state is OverlayState.ERROR


def test_paint_build_response_invalid():
    # build_response in paint_in_flight is invalid (would deadlock if wired
    # as the paint trigger). Must carry current gen to reach the table.
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="x"))
    assert r.outcome is OverlayOutcome.INVALID_TRANSITION
    assert m.state is OverlayState.ERROR


# ---------------------------------------------------------------------------
# painted row
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kind",
    [
        OverlayEventKind.SHOW_NUMBERS,
        OverlayEventKind.FOCUS_CHANGE,
        OverlayEventKind.CLICK_COMPLETE,
    ],
)
def test_painted_refresh_triggers(kind):
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    gen_before = m.paint_generation
    r = m.apply(OverlayEvent(kind))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    assert m.paint_generation == gen_before + 1
    assert effect_kinds(r) == [
        EffectKind.CANCEL_TIMER,
        EffectKind.DISPATCH_BUILD,
        EffectKind.ARM_TIMER,
    ]
    assert r.effects[1].build_reason is BuildReason.REFRESH
    # The prior snapshot is NOT unpinned yet (still visible).
    assert m.pinned_snapshot_id == "snap"


def test_painted_hide_to_closed_unpins():
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    r = m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    assert effect_kinds(r) == [
        EffectKind.CANCEL_TIMER,
        EffectKind.DISPATCH_CLEAR,
        EffectKind.UNPIN_SNAPSHOT,
    ]
    assert m.pinned_snapshot_id is None


def test_painted_click_n_noop():
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_N))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.PAINTED


def test_painted_mic_pause_to_paused():
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.state is OverlayState.PAUSED
    assert effect_kinds(r) == [EffectKind.DISPATCH_CLEAR]
    # snapshot stays pinned for fast resume
    assert m.pinned_snapshot_id == "snap"


def test_painted_mic_resume_noop():
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME))
    assert r.outcome is OverlayOutcome.NO_OP


def test_painted_paint_ack_noop_no_path_to_error():
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.PAINTED


@pytest.mark.parametrize(
    "kind",
    [
        OverlayEventKind.AUTO_OPEN,
        OverlayEventKind.FOCUSED_HWND_DESTROYED,
    ],
)
def test_painted_invalid_events(kind):
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    r = m.apply(OverlayEvent(kind))
    assert r.outcome is OverlayOutcome.INVALID_TRANSITION
    assert m.state is OverlayState.ERROR


def test_painted_build_response_invalid():
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="x"))
    assert r.outcome is OverlayOutcome.INVALID_TRANSITION
    assert m.state is OverlayState.ERROR


# ---------------------------------------------------------------------------
# refresh_in_flight row
# ---------------------------------------------------------------------------

def to_refresh(m: ClickOverlayStateMachine):
    drive_to_painted(m, snapshot_id="snapA")
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT


def test_refresh_build_ok_pins_new_defers_prior_unpin():
    # Finding 1: at build-ok time the NEW snapshot is pinned but the prior
    # (still-visible) snapshot's unpin is DEFERRED until the paint succeeds.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.REFRESH_IN_FLIGHT  # paint-ack drives the move
    assert m.pinned_snapshot_id == "snapB"
    assert m.prior_pinned_snapshot_id == "snapA"
    assert m._prior_pin_deferred is True
    kinds = effect_kinds(r)
    # No UNPIN at build-ok time -- it is deferred to the successful paint-ack.
    assert kinds == [EffectKind.PIN_SNAPSHOT, EffectKind.DISPATCH_PAINT]
    assert r.effects[0].snapshot_id == "snapB"
    assert r.effects[1].immediate_clear is False


def test_prior_pin_deferred_property_reflects_internal_field():
    # wh-n29v.20.1 (reviewer_2): the integration layer (forward_click_element)
    # needs the deferred-prior-unpin flag to ask route_click_n which snapshot
    # is visible during refresh. Expose it as a public read-only property so
    # the call site does not reach into the private backing field; the property
    # must mirror the field exactly.
    m = ClickOverlayStateMachine()
    assert m.prior_pin_deferred is False
    assert m.prior_pin_deferred == m._prior_pin_deferred
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    assert m._prior_pin_deferred is True
    assert m.prior_pin_deferred is True
    assert m.prior_pin_deferred == m._prior_pin_deferred


def test_refresh_paint_ack_painted_ships_deferred_prior_unpin():
    # Finding 1: the successful refresh paint-ack ships UNPIN(prior).
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    assert m.state is OverlayState.PAINTED
    assert m.pinned_snapshot_id == "snapB"
    assert m.prior_pinned_snapshot_id is None
    assert m._prior_pin_deferred is False
    assert effect_kinds(r) == [EffectKind.CANCEL_TIMER, EffectKind.UNPIN_SNAPSHOT]
    unpin = [e for e in r.effects if e.kind is EffectKind.UNPIN_SNAPSHOT][0]
    assert unpin.snapshot_id == "snapA"  # the prior visible snapshot


def test_refresh_paint_ack_failed_restores_prior_unpins_new():
    # Finding 1: a failed refresh paint-ack restores the prior visible
    # snapshot as pinned and unpins the new FAILED one.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.FAILED))
    assert m.state is OverlayState.PAINTED  # keep the prior usable overlay
    assert r.outcome is OverlayOutcome.ACCEPTED
    # pinned restored to the prior visible snapshot
    assert m.pinned_snapshot_id == "snapA"
    assert m.prior_pinned_snapshot_id is None
    assert m._prior_pin_deferred is False
    # the new failed snapshot is unpinned; the prior is NOT unpinned
    unpins = [e for e in r.effects if e.kind is EffectKind.UNPIN_SNAPSHOT]
    assert len(unpins) == 1
    assert unpins[0].snapshot_id == "snapB"


def test_refresh_build_failed_non_destructive_no_pin_churn():
    # Finding 1: a failed build-response never installed a new snapshot, so
    # the visible pin is untouched and no unpin is emitted.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, build_ok=False))
    assert m.state is OverlayState.PAINTED
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.pinned_snapshot_id == "snapA"
    assert m._prior_pin_deferred is False
    assert EffectKind.UNPIN_SNAPSHOT not in effect_kinds(r)


def test_refresh_timeout_after_build_ok_restores_prior_unpins_new():
    # Finding 1: a refresh timeout AFTER a successful build (deferred prior
    # present) restores the prior and unpins the new never-painted snapshot.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    assert m._prior_pin_deferred is True
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.PAINTED
    assert m.pinned_snapshot_id == "snapA"
    assert m.prior_pinned_snapshot_id is None
    assert m._prior_pin_deferred is False
    unpins = [e for e in r.effects if e.kind is EffectKind.UNPIN_SNAPSHOT]
    assert len(unpins) == 1
    assert unpins[0].snapshot_id == "snapB"


def test_refresh_timeout_non_destructive_to_painted():
    # Timeout while the build is still outstanding (no deferred prior): the
    # visible pin is untouched.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.PAINTED
    assert m.pinned_snapshot_id == "snapA"
    assert m._prior_pin_deferred is False
    assert effect_kinds(r) == [EffectKind.CANCEL_TIMER]


def test_refresh_timeout_with_auto_hide_to_paused():
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.auto_hide_in_flight is True
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.PAUSED
    assert m.auto_hide_in_flight is False


def test_refresh_show_numbers_supersede():
    m = ClickOverlayStateMachine()
    to_refresh(m)
    gen_before = m.paint_generation
    r = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    assert m.paint_generation == gen_before + 1
    # Does not unpin (prior snapshot still visible until new paint installs).
    assert EffectKind.UNPIN_SNAPSHOT not in effect_kinds(r)
    assert r.effects[1].build_reason is BuildReason.SUPERSEDE


def test_refresh_focus_change_supersede():
    m = ClickOverlayStateMachine()
    to_refresh(m)
    r = m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    assert r.effects[1].build_reason is BuildReason.SUPERSEDE


def test_refresh_supersede_after_build_ok_abandons_new_restores_prior():
    # Finding 1 (rapid supersede): build-ok defers the prior; a supersede
    # before the paint-ack abandons the new snapshot (unpin it) and restores
    # the truly-visible prior as the sole pin, so the next build-ok defers
    # cleanly.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    assert m.pinned_snapshot_id == "snapB"
    assert m.prior_pinned_snapshot_id == "snapA"
    r = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    assert m.pinned_snapshot_id == "snapA"  # restored to the visible prior
    assert m.prior_pinned_snapshot_id is None
    assert m._prior_pin_deferred is False
    unpins = [e for e in r.effects if e.kind is EffectKind.UNPIN_SNAPSHOT]
    assert len(unpins) == 1
    assert unpins[0].snapshot_id == "snapB"  # the abandoned new snapshot


def test_refresh_duplicate_build_response_is_noop_preserves_prior_pin():
    # wh-n29v.15.2: a SECOND build_response for the SAME refresh generation
    # (a duplicate or replayed Input response) must NOT re-run _refresh_build_ok
    # and clobber the deferred prior pin. The pre-table generation gate cannot
    # catch it because the (session, generation) pair is still current; the
    # _prior_pin_deferred flag is the "build already consumed this generation"
    # signal that drops the duplicate.
    m = ClickOverlayStateMachine()
    to_refresh(m)  # painted snapA -> refresh_in_flight; prior visible = snapA
    # First build_ok: pin snapB, defer the prior (snapA) unpin.
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    assert m.pinned_snapshot_id == "snapB"
    assert m.prior_pinned_snapshot_id == "snapA"
    assert m._prior_pin_deferred is True
    # Duplicate build_response at the SAME (session, generation) -> ignored.
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapC"))
    assert r.outcome is OverlayOutcome.NO_OP
    assert r.effects == ()
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    assert m.pinned_snapshot_id == "snapB"          # snapC ignored
    assert m.prior_pinned_snapshot_id == "snapA"    # prior NOT clobbered to snapB
    assert m._prior_pin_deferred is True
    # The successful paint-ack unpins the TRUE prior (snapA), leaving snapB as
    # the sole pinned snapshot -- the invariant holds despite the duplicate.
    r2 = m.apply(
        gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED)
    )
    assert m.state is OverlayState.PAINTED
    unpins = [e for e in r2.effects if e.kind is EffectKind.UNPIN_SNAPSHOT]
    assert len(unpins) == 1
    assert unpins[0].snapshot_id == "snapA"
    assert m.pinned_snapshot_id == "snapB"
    assert m.prior_pinned_snapshot_id is None
    assert m._prior_pin_deferred is False


def test_refresh_duplicate_failed_build_response_also_noop():
    # wh-n29v.15.2: once a refresh generation's build has succeeded, even a
    # duplicate build_response reporting build_ok=False must be ignored -- it
    # must not tear the in-flight new paint down via the failed-build path.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, build_ok=False))
    assert r.outcome is OverlayOutcome.NO_OP
    assert r.effects == ()
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    assert m.pinned_snapshot_id == "snapB"
    assert m.prior_pinned_snapshot_id == "snapA"
    assert m._prior_pin_deferred is True


def test_refresh_paint_fail_then_mic_resume_restores_correct_snapshot():
    # Finding 1 end-to-end: after a failed refresh paint, the prior snapshot
    # is the pinned one, so a later mic-pause + mic-resume-valid restores the
    # PRIOR (correct) snapshot, not the failed one.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.FAILED))
    assert m.pinned_snapshot_id == "snapA"
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.state is OverlayState.PAUSED
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True))
    assert m.state is OverlayState.PAINTED
    paint = [e for e in r.effects if e.kind is EffectKind.DISPATCH_PAINT][0]
    assert paint.snapshot_id == "snapA"  # the correct visible snapshot


def test_refresh_hide_after_build_ok_unpins_both_snapshots():
    # Finding 1: hiding while a refresh deferred the prior leaves BOTH the
    # new and the prior pinned in the store -- both must be unpinned.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    r = m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    unpinned = {
        e.snapshot_id for e in r.effects if e.kind is EffectKind.UNPIN_SNAPSHOT
    }
    assert unpinned == {"snapA", "snapB"}
    assert m.pinned_snapshot_id is None
    assert m.prior_pinned_snapshot_id is None


def test_refresh_hide_to_closed():
    m = ClickOverlayStateMachine()
    to_refresh(m)
    r = m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    assert EffectKind.DISPATCH_CLEAR in effect_kinds(r)


def test_refresh_mic_pause_hides_sets_flag():
    m = ClickOverlayStateMachine()
    to_refresh(m)
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    assert m.auto_hide_in_flight is True
    assert effect_kinds(r) == [EffectKind.DISPATCH_CLEAR]


def test_refresh_click_n_resolves_previous_noop():
    m = ClickOverlayStateMachine()
    to_refresh(m)
    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_N))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.REFRESH_IN_FLIGHT


def test_refresh_click_complete_held():
    m = ClickOverlayStateMachine()
    to_refresh(m)
    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    assert r.outcome is OverlayOutcome.HELD


def test_refresh_mic_resume_noop():
    m = ClickOverlayStateMachine()
    to_refresh(m)
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME))
    assert r.outcome is OverlayOutcome.NO_OP


@pytest.mark.parametrize(
    "kind",
    [OverlayEventKind.AUTO_OPEN, OverlayEventKind.FOCUSED_HWND_DESTROYED],
)
def test_refresh_invalid_events(kind):
    m = ClickOverlayStateMachine()
    to_refresh(m)
    r = m.apply(OverlayEvent(kind))
    assert r.outcome is OverlayOutcome.INVALID_TRANSITION
    assert m.state is OverlayState.ERROR


# ---------------------------------------------------------------------------
# paused row
# ---------------------------------------------------------------------------

def to_paused(m: ClickOverlayStateMachine):
    drive_to_painted(m, snapshot_id="snapP")
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.state is OverlayState.PAUSED


def test_paused_mic_resume_valid_restores():
    m = ClickOverlayStateMachine()
    to_paused(m)
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True))
    assert m.state is OverlayState.PAINTED
    assert effect_kinds(r) == [EffectKind.DISPATCH_PAINT]
    assert r.effects[0].snapshot_id == "snapP"


def test_paused_mic_resume_stale_rewalks():
    m = ClickOverlayStateMachine()
    to_paused(m)
    gen_before = m.paint_generation
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=False))
    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert m.paint_generation == gen_before + 1
    assert m.pinned_snapshot_id is None
    kinds = effect_kinds(r)
    assert EffectKind.UNPIN_SNAPSHOT in kinds
    assert EffectKind.DISPATCH_BUILD in kinds
    build = [e for e in r.effects if e.kind is EffectKind.DISPATCH_BUILD][0]
    assert build.build_reason is BuildReason.RESUME_REWALK


def test_paused_show_numbers_restart():
    m = ClickOverlayStateMachine()
    to_paused(m)
    r = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert EffectKind.UNPIN_SNAPSHOT in effect_kinds(r)
    build = [e for e in r.effects if e.kind is EffectKind.DISPATCH_BUILD][0]
    assert build.build_reason is BuildReason.SHOW_NUMBERS


def test_paused_hide_to_closed():
    m = ClickOverlayStateMachine()
    to_paused(m)
    r = m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    assert EffectKind.DISPATCH_CLEAR in effect_kinds(r)
    assert EffectKind.UNPIN_SNAPSHOT in effect_kinds(r)


def test_paused_focused_hwnd_destroyed_to_closed():
    m = ClickOverlayStateMachine()
    to_paused(m)
    r = m.apply(OverlayEvent(OverlayEventKind.FOCUSED_HWND_DESTROYED))
    assert m.state is OverlayState.CLOSED
    assert EffectKind.DISPATCH_CLEAR in effect_kinds(r)
    assert EffectKind.UNPIN_SNAPSHOT in effect_kinds(r)
    assert m.pinned_snapshot_id is None


def test_paused_mic_pause_noop():
    m = ClickOverlayStateMachine()
    to_paused(m)
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.PAUSED


def test_paused_focus_change_noop():
    m = ClickOverlayStateMachine()
    to_paused(m)
    r = m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.PAUSED


def test_paused_click_n_held():
    m = ClickOverlayStateMachine()
    to_paused(m)
    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_N))
    assert r.outcome is OverlayOutcome.HELD


def test_paused_paint_ack_noop():
    # A generation-matching paint-ack in paused is the acknowledgement of the
    # hide that drove the machine here: entry to paused dispatches a clear (and
    # the walk-in-flight->paused resolve a paint+immediate-clear), so the GUI
    # emits painted / cleared at the same generation. It is bookkeeping, not a
    # state driver and not an error (mirrors the closed handler; wh-n29v.69.1).
    # The paint_state is irrelevant here -- the branch consumes any
    # generation-matching paint-ack as a NO_OP.
    m = ClickOverlayStateMachine()
    to_paused(m)
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.PAUSED


@pytest.mark.parametrize(
    "kind",
    [
        OverlayEventKind.BUILD_RESPONSE,
        OverlayEventKind.CLICK_COMPLETE,
        OverlayEventKind.AUTO_OPEN,
    ],
)
def test_paused_invalid_events(kind):
    m = ClickOverlayStateMachine()
    to_paused(m)
    if kind is OverlayEventKind.BUILD_RESPONSE:
        ev = gen_event(m, kind)
    else:
        ev = OverlayEvent(kind)
    r = m.apply(ev)
    assert r.outcome is OverlayOutcome.INVALID_TRANSITION
    assert m.state is OverlayState.ERROR


# ---------------------------------------------------------------------------
# error row
# ---------------------------------------------------------------------------

def to_error(m: ClickOverlayStateMachine):
    # Force an invalid transition from closed to land in error.
    # FOCUSED_HWND_DESTROYED in closed is the remaining genuine protocol
    # violation (the transient destroy hook is live only while paused), so it
    # still fails closed to error. CLICK_COMPLETE / BUILD_RESPONSE / TIMEOUT in
    # closed are now teardown NO_OPs (wh-n29v.19.1) and no longer reach error.
    m.apply(OverlayEvent(OverlayEventKind.FOCUSED_HWND_DESTROYED))
    assert m.state is OverlayState.ERROR


def test_error_show_numbers_fresh_walk():
    m = ClickOverlayStateMachine()
    to_error(m)
    r = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert m.reason == ""  # cleared on the fresh-walk path
    assert effect_kinds(r) == [EffectKind.DISPATCH_BUILD, EffectKind.ARM_TIMER]


def test_error_hide_to_closed():
    m = ClickOverlayStateMachine()
    to_error(m)
    r = m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.CLOSED
    assert m.reason == ""


@pytest.mark.parametrize(
    "kind", [OverlayEventKind.MIC_PAUSE, OverlayEventKind.MIC_RESUME]
)
def test_error_mic_events_close(kind):
    m = ClickOverlayStateMachine()
    to_error(m)
    r = m.apply(OverlayEvent(kind))
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.CLOSED


def test_error_click_n_rejected_noop():
    m = ClickOverlayStateMachine()
    to_error(m)
    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_N))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.ERROR


def test_error_focus_change_noop():
    m = ClickOverlayStateMachine()
    to_error(m)
    r = m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.ERROR


@pytest.mark.parametrize(
    "kind",
    [
        OverlayEventKind.CLICK_COMPLETE,
        OverlayEventKind.AUTO_OPEN,
        OverlayEventKind.FOCUSED_HWND_DESTROYED,
    ],
)
def test_error_invalid_events(kind):
    # The genuinely-invalid events in error are the NON-generation-bearing ones:
    # external triggers / integration acks that the machine did not dispatch and
    # cannot be a late completion of in-flight work. They keep failing closed.
    m = ClickOverlayStateMachine()
    to_error(m)
    r = m.apply(OverlayEvent(kind))
    assert r.outcome is OverlayOutcome.INVALID_TRANSITION
    assert m.state is OverlayState.ERROR


@pytest.mark.parametrize(
    "kind,extra",
    [
        (OverlayEventKind.BUILD_RESPONSE, {"snapshot_id": "s"}),
        (OverlayEventKind.PAINT_ACK, {"paint_state": PaintAckState.PAINTED}),
        (OverlayEventKind.TIMEOUT, {}),
    ],
)
def test_error_generation_bearing_acks_noop_preserve_reason(kind, extra):
    # A generation-MATCHING build_response / paint_ack / timeout reaching error
    # is a late completion of work the machine dispatched at the still-current
    # generation BEFORE it failed closed to error (the generation is frozen on
    # entry to error -- _invalid does not bump it -- so the gate lets the ack
    # through). Like the closed and paused handlers, it is consumed as
    # bookkeeping NO_OP, never an error driver, and the diagnostic reason that
    # records WHY the machine errored is preserved rather than overwritten by
    # the late ack (wh-n29v.70.3; same class as the paused paint-ack fix
    # wh-n29v.69.1).
    m = ClickOverlayStateMachine()
    to_error(m)
    original_reason = m.reason
    assert original_reason  # the error-entry set a diagnostic reason
    r = m.apply(gen_event(m, kind, **extra))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.ERROR
    assert m.reason == original_reason  # not overwritten by a late ack
    assert r.effects == ()


# ---------------------------------------------------------------------------
# Generation discipline
# ---------------------------------------------------------------------------

def test_session_id_monotonic_across_sessions():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.overlay_session_id == 1
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.overlay_session_id == 2


def test_paint_generation_starts_at_zero_per_session():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.paint_generation == 0
    drive_rest = m
    drive_rest.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="s"))
    drive_rest.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))  # refresh, bump to 1
    assert m.paint_generation == 1
    # New session resets generation to 0
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.paint_generation == 0


def test_generation_bumped_on_each_refresh():
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    assert m.paint_generation == 0
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    assert m.paint_generation == 1
    # back to painted then refresh again
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="s2"))
    m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))  # refresh
    assert m.paint_generation == 2


@pytest.mark.parametrize(
    "kind,extra",
    [
        (OverlayEventKind.BUILD_RESPONSE, {"snapshot_id": "x"}),
        (OverlayEventKind.TIMEOUT, {}),
    ],
)
def test_stale_generation_rejected_in_walk(kind, extra):
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    # Stamp a wrong (older) generation.
    stale = OverlayEvent(
        kind,
        overlay_session_id=m.overlay_session_id,
        paint_generation=m.paint_generation - 1,
        **extra,
    )
    r = m.apply(stale)
    assert r.outcome is OverlayOutcome.STALE_GENERATION
    assert m.state is OverlayState.WALK_IN_FLIGHT  # unchanged
    assert r.effects == ()


def test_stale_session_id_rejected():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    stale = OverlayEvent(
        OverlayEventKind.BUILD_RESPONSE,
        overlay_session_id=m.overlay_session_id + 5,
        paint_generation=m.paint_generation,
        snapshot_id="x",
    )
    r = m.apply(stale)
    assert r.outcome is OverlayOutcome.STALE_GENERATION
    assert m.state is OverlayState.WALK_IN_FLIGHT


def test_stale_paint_ack_rejected_in_paint_in_flight():
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    stale = OverlayEvent(
        OverlayEventKind.PAINT_ACK,
        overlay_session_id=m.overlay_session_id,
        paint_generation=m.paint_generation + 1,
        paint_state=PaintAckState.PAINTED,
    )
    r = m.apply(stale)
    assert r.outcome is OverlayOutcome.STALE_GENERATION
    assert m.state is OverlayState.PAINT_IN_FLIGHT


def test_stale_painted_ack_does_not_move_after_newer_clear():
    # A stale painted ack must not move the machine to painted after a hide.
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m)
    sess, gen = m.overlay_session_id, m.paint_generation
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    # The old painted ack (sess/gen) is now stale -- closed active pair is
    # still sess/gen actually, since hide does not bump. But a NEW session
    # has not started. Confirm a paint_ack at the now-defunct pair is
    # invalid/no-op rather than reviving the overlay.
    late = OverlayEvent(
        OverlayEventKind.PAINT_ACK,
        overlay_session_id=sess,
        paint_generation=gen,
        paint_state=PaintAckState.PAINTED,
    )
    r = m.apply(late)
    # In closed, a late paint_ack is bookkeeping (NO_OP); it does not revive
    # the overlay nor fail closed to error.
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.CLOSED


# ---------------------------------------------------------------------------
# wh-n29v.95 part 6 / criterion 3 (wh-n29v.19.1): late same-generation
# completion events after hide-numbers must NOT error the machine.
#
# hide_numbers transitions straight to closed WITHOUT bumping the generation
# (r2.4), so a build_response / timeout / click_complete the machine itself
# dispatched at the still-current pair PASSES the pre-table generation gate and
# lands in _on_closed. Before the fix only PAINT_ACK was a teardown NO_OP there;
# the other three fell through to _invalid -> ERROR. The contract requires a
# 'show -> hide -> late <event>(session,gen)' sequence to NOT return
# INVALID_TRANSITION and to leave the machine in closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        OverlayEventKind.BUILD_RESPONSE,
        OverlayEventKind.TIMEOUT,
        OverlayEventKind.CLICK_COMPLETE,
    ],
)
def test_late_same_generation_event_after_hide_is_teardown_noop(kind):
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sess, gen = m.overlay_session_id, m.paint_generation
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED

    # CLICK_COMPLETE carries no generation; BUILD_RESPONSE / TIMEOUT do and the
    # closed active pair is still (sess, gen) because hide did not bump it, so a
    # late completion at that pair passes the generation gate and reaches the
    # _on_closed handler.
    late = OverlayEvent(
        kind,
        overlay_session_id=sess,
        paint_generation=gen,
    )
    r = m.apply(late)
    assert r.outcome is OverlayOutcome.NO_OP
    assert r.effects == ()
    assert m.state is OverlayState.CLOSED


# ---------------------------------------------------------------------------
# Mic-pause corner cases (in-flight -> paused resolution)
# ---------------------------------------------------------------------------

def test_walk_mic_pause_then_build_resolves_to_paused():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.auto_hide_in_flight is True
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapW"))
    assert m.state is OverlayState.PAUSED
    assert m.auto_hide_in_flight is False
    assert m.pinned_snapshot_id == "snapW"
    kinds = effect_kinds(r)
    # pin, paint(+immediate_clear), clear -> nets invisible
    assert EffectKind.PIN_SNAPSHOT in kinds
    assert EffectKind.DISPATCH_PAINT in kinds
    assert EffectKind.DISPATCH_CLEAR in kinds
    paint = [e for e in r.effects if e.kind is EffectKind.DISPATCH_PAINT][0]
    assert paint.immediate_clear is True


def test_paint_mic_pause_then_paint_ack_resolves_to_paused():
    m = ClickOverlayStateMachine()
    to_paint_in_flight(m, snapshot_id="snapPP")
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.auto_hide_in_flight is True
    assert m.state is OverlayState.PAINT_IN_FLIGHT
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    assert m.state is OverlayState.PAUSED
    assert m.auto_hide_in_flight is False
    assert m.pinned_snapshot_id == "snapPP"  # pinned at paint dispatch
    assert EffectKind.DISPATCH_CLEAR in effect_kinds(r)


def test_refresh_mic_pause_then_paint_ack_resolves_to_paused():
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.auto_hide_in_flight is True
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapR"))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    assert m.state is OverlayState.PAUSED
    assert m.auto_hide_in_flight is False
    assert EffectKind.DISPATCH_CLEAR in effect_kinds(r)


def test_refresh_build_ok_with_auto_hide_paints_immediate_clear():
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapR2"))
    paint = [e for e in r.effects if e.kind is EffectKind.DISPATCH_PAINT][0]
    assert paint.immediate_clear is True
    assert m.state is OverlayState.REFRESH_IN_FLIGHT  # paint-ack still drives the move


# ---------------------------------------------------------------------------
# pending_ambiguous_notice & auto_hide_in_flight lifecycle
# ---------------------------------------------------------------------------

def test_pending_notice_cleared_on_entry_to_closed():
    m = ClickOverlayStateMachine()
    notice = make_notice()
    m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=notice))
    assert m.pending_ambiguous_notice is notice
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    assert m.pending_ambiguous_notice is None


def test_pending_notice_does_not_leak_to_next_session():
    m = ClickOverlayStateMachine()
    notice = make_notice()
    m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=notice))
    # Successful paint then a new session -- the notice must be gone.
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="s"))
    m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    assert m.state is OverlayState.PAINTED
    # Auto-open's notice is still pending until closed; confirm it does not
    # fire on a later session-close timeout.
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.pending_ambiguous_notice is None
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    fired = [e for e in r.effects if e.kind is EffectKind.FIRE_NOTICE]
    # The first session's auto-open notice must NOT leak into this new
    # standalone session. The standalone walk timeout legitimately fires the
    # generic "numbers couldn't be drawn" notice (notice=None, wh-n29v.16.1),
    # but never the leaked auto-open notice object.
    assert notice not in [e.notice for e in fired]
    assert fired == [] or (len(fired) == 1 and fired[0].notice is None)


def test_auto_hide_cleared_on_entry_to_closed():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.auto_hide_in_flight is True
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    assert m.auto_hide_in_flight is False


def test_auto_hide_cleared_on_reaching_paused():
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="s"))
    assert m.state is OverlayState.PAUSED
    assert m.auto_hide_in_flight is False


# ---------------------------------------------------------------------------
# hide-numbers immediate-close + cleared-ack bookkeeping
# ---------------------------------------------------------------------------

def test_hide_numbers_does_not_wait_for_ack_then_cleared_is_noop():
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    sess, gen = m.overlay_session_id, m.paint_generation
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    # A later cleared ack (same gen) must not strand or change state.
    late = OverlayEvent(
        OverlayEventKind.PAINT_ACK,
        overlay_session_id=sess,
        paint_generation=gen,
        paint_state=PaintAckState.CLEARED,
    )
    r = m.apply(late)
    # closed consumes the late cleared ack as bookkeeping (r2.4) -- it does
    # NOT re-open the overlay. The key property is no resurrection.
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.CLOSED


def test_hide_numbers_clear_ack_race_does_not_strand_in_refresh():
    # User hides at gen 0; then a focus change would normally drive a
    # refresh. Because hide already moved to closed, a focus change in
    # closed is a record-only no-op, not a refresh.
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    r = m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert m.state is OverlayState.CLOSED
    assert r.outcome is OverlayOutcome.NO_OP


# ---------------------------------------------------------------------------
# reset_to_closed
# ---------------------------------------------------------------------------

def test_reset_to_closed_clears_everything_and_unpins():
    m = ClickOverlayStateMachine()
    notice = make_notice()
    m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=notice))
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    m.pinned_snapshot_id = "leftover"
    sess_before = m.overlay_session_id
    effects = m.reset_to_closed()
    # Finding 2: reset emits the UNPIN for whatever was pinned. wh-n29v.15.1:
    # because a snapshot is pinned (an overlay may be on screen), reset also
    # emits a DISPATCH_CLEAR first so a late-rendered paint cannot orphan.
    assert [e.kind for e in effects] == [
        EffectKind.DISPATCH_CLEAR,
        EffectKind.UNPIN_SNAPSHOT,
    ]
    unpin = [e for e in effects if e.kind is EffectKind.UNPIN_SNAPSHOT][0]
    assert unpin.snapshot_id == "leftover"
    assert m.state is OverlayState.CLOSED
    assert m.pending_ambiguous_notice is None
    assert m.auto_hide_in_flight is False
    assert m.pinned_snapshot_id is None
    assert m.reason == ""
    # session id retained; next session is strictly larger
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.overlay_session_id > sess_before


def test_reset_to_closed_no_pin_returns_empty():
    m = ClickOverlayStateMachine()
    assert m.reset_to_closed() == ()


def test_reset_to_closed_after_invalid_error_unpins_orphan():
    # Finding 2: an invalid transition in painted enters error WITHOUT
    # nulling the pin; reset_to_closed must emit the UNPIN so the snapshot
    # is not orphaned until TTL.
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapX")
    m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=make_notice()))
    assert m.state is OverlayState.ERROR
    assert m.pinned_snapshot_id == "snapX"  # pin preserved through error
    effects = m.reset_to_closed()
    # Finding 2 (UNPIN orphan) + wh-n29v.15.1 (CLEAR the possibly-visible
    # overlay): a pin survived into error, so reset clears then unpins.
    assert [e.kind for e in effects] == [
        EffectKind.DISPATCH_CLEAR,
        EffectKind.UNPIN_SNAPSHOT,
    ]
    unpin = [e for e in effects if e.kind is EffectKind.UNPIN_SNAPSHOT][0]
    assert unpin.snapshot_id == "snapX"
    assert m.state is OverlayState.CLOSED
    assert m.pinned_snapshot_id is None


def test_error_recovery_via_show_numbers_unpins_orphan():
    # Finding 2: recovering from an error (entered via _invalid while a
    # snapshot was pinned) through SHOW_NUMBERS emits the orphan UNPIN
    # before the fresh build.
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapX")
    m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=make_notice()))
    assert m.state is OverlayState.ERROR
    r = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.state is OverlayState.WALK_IN_FLIGHT
    kinds = effect_kinds(r)
    # UNPIN(orphan) ships before the new DISPATCH_BUILD.
    assert kinds == [
        EffectKind.UNPIN_SNAPSHOT,
        EffectKind.DISPATCH_BUILD,
        EffectKind.ARM_TIMER,
    ]
    assert r.effects[0].snapshot_id == "snapX"
    assert m.pinned_snapshot_id is None


def test_error_recovery_via_hide_numbers_unpins_orphan():
    # Finding 2: recovering from a pinned-at-entry error through HIDE_NUMBERS
    # emits the orphan UNPIN.
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapX")
    m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=make_notice()))
    assert m.state is OverlayState.ERROR
    r = m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    unpins = [e for e in r.effects if e.kind is EffectKind.UNPIN_SNAPSHOT]
    assert len(unpins) == 1
    assert unpins[0].snapshot_id == "snapX"
    assert m.pinned_snapshot_id is None
    # wh-n29v.15.1: a pin survived into error, so the recovery-to-closed also
    # clears any overlay the GUI may still be showing.
    assert EffectKind.DISPATCH_CLEAR in effect_kinds(r)


def test_invalid_transition_sets_reason_without_raising():
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    # auto_open from painted is invalid.
    r = m.apply(OverlayEvent(OverlayEventKind.AUTO_OPEN, notice=make_notice()))
    assert r.outcome is OverlayOutcome.INVALID_TRANSITION
    assert m.state is OverlayState.ERROR
    assert m.reason == "invalid_transition_from_painted_via_auto_open"


# ---------------------------------------------------------------------------
# Effect ordering for the canonical first-paint sequence
# ---------------------------------------------------------------------------

def test_first_paint_effect_ordering_end_to_end():
    m = ClickOverlayStateMachine()
    r1 = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert [e.kind for e in r1.effects] == [
        EffectKind.DISPATCH_BUILD,
        EffectKind.ARM_TIMER,
    ]
    r2 = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="s"))
    assert [e.kind for e in r2.effects] == [
        EffectKind.CANCEL_TIMER,
        EffectKind.PIN_SNAPSHOT,
        EffectKind.DISPATCH_PAINT,
        EffectKind.ARM_TIMER,
    ]
    r3 = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    assert [e.kind for e in r3.effects] == [EffectKind.CANCEL_TIMER]


# ---------------------------------------------------------------------------
# Finding 3: immediate_clear has two clear-delivery paths (locked in)
# ---------------------------------------------------------------------------

def test_immediate_clear_walk_path_ships_clear_inline():
    # Walk path: mic-pause mid-walk, then build-response resolves straight to
    # paused. The DISPATCH_CLEAR ships INLINE in the same effect batch as the
    # immediate_clear paint.
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapW"))
    assert m.state is OverlayState.PAUSED
    kinds = effect_kinds(r)
    paint_idx = kinds.index(EffectKind.DISPATCH_PAINT)
    clear_idx = kinds.index(EffectKind.DISPATCH_CLEAR)
    paint = r.effects[paint_idx]
    assert paint.immediate_clear is True
    # the clear is present in THIS same batch, after the paint
    assert clear_idx > paint_idx
    assert paint.paint_generation == r.effects[clear_idx].paint_generation


def test_immediate_clear_refresh_path_defers_clear_to_paint_ack():
    # Refresh path: mic-pause mid-refresh, then build-ok dispatches the
    # immediate_clear paint but NO inline clear (stays refresh_in_flight).
    # The DISPATCH_CLEAR ships on the SUBSEQUENT paint-ack at the same
    # generation. Same flag, two delivery paths -- intentional.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    gen = m.paint_generation
    r_build = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapR2"))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    build_kinds = effect_kinds(r_build)
    paint = [e for e in r_build.effects if e.kind is EffectKind.DISPATCH_PAINT][0]
    assert paint.immediate_clear is True
    # NO inline clear on the refresh build-ok leg
    assert EffectKind.DISPATCH_CLEAR not in build_kinds
    # the clear arrives on the subsequent paint-ack at the same generation
    r_ack = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    assert m.state is OverlayState.PAUSED
    clears = [e for e in r_ack.effects if e.kind is EffectKind.DISPATCH_CLEAR]
    assert len(clears) == 1
    assert clears[0].paint_generation == gen


def test_invalid_from_in_flight_cancels_timer():
    """Finding wh-n29v.98.2: entering ERROR via _invalid from an in-flight state
    must cancel the armed per-state timeout timer (CANCEL_TIMER), so a stale
    timer does not fire later as a wasted NO_OP. Every other transition away
    from an in-flight state emits CANCEL_TIMER; _invalid must not be the one
    path that orphans it."""

    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))  # -> walk_in_flight (timer armed)
    assert m.state is OverlayState.WALK_IN_FLIGHT
    r = m.apply(OverlayEvent(OverlayEventKind.FOCUSED_HWND_DESTROYED))  # invalid in walk
    assert r.outcome is OverlayOutcome.INVALID_TRANSITION
    assert m.state is OverlayState.ERROR
    assert EffectKind.CANCEL_TIMER in effect_kinds(r)


# ---------------------------------------------------------------------------
# Pin contract-break detection (wh-pin-snapshot-contract-break-detection)
# ---------------------------------------------------------------------------
# The machine self-audits its own PIN/UNPIN effect stream. The invariant:
# at most ONE snapshot pin outstanding, EXCEPT exactly two during the
# legitimate deferred-refresh window (_refresh_build_ok pinned the new
# snapshot and deferred the prior's unpin). A naive store-level pinned>1
# warning was removed in 5a7abd66 because it cried wolf on every refresh;
# this detection lives where the deferred-generation bookkeeping exists.


def test_pin_contract_clean_through_normal_lifecycle():
    """show -> painted -> refresh -> painted never reports a break."""
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapA")
    assert m.consume_pin_contract_break() is None
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    # Two pins outstanding here -- the LEGITIMATE deferred-refresh window.
    assert m.consume_pin_contract_break() is None
    m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    assert m.consume_pin_contract_break() is None


def test_pin_contract_clean_through_hide():
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapA")
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.consume_pin_contract_break() is None
    # A fresh session after a clean teardown starts with no residue.
    drive_to_painted(m, snapshot_id="snapB")
    assert m.consume_pin_contract_break() is None


def test_pin_contract_break_second_pin_without_deferred_prior():
    """A pin landing on top of an outstanding pin with NO deferred prior is
    the lost-unpin / racing-double-pin shape and must be flagged."""
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapA")
    # Emulate a regression: some future path emits a second PIN effect
    # without the deferred-refresh bookkeeping.
    m._pin("snapB")
    msg = m.consume_pin_contract_break()
    assert msg is not None
    assert "snapB" in msg and "snapA" in msg
    # consume clears the pending break.
    assert m.consume_pin_contract_break() is None


def test_pin_contract_break_third_pin_spanning_generations():
    """Pins spanning more than one refresh generation must be flagged even
    though the deferred-prior flag is set for the CURRENT generation."""
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapA")
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    assert m.consume_pin_contract_break() is None  # legitimate two-pin window
    # Emulate a second build-ok pin arriving without the supersede
    # reconcile shipping snapA's deferred unpin first.
    m._pin("snapC")
    msg = m.consume_pin_contract_break()
    assert msg is not None
    assert "snapC" in msg


def test_pin_contract_repin_same_snapshot_is_not_a_break():
    """Re-pinning the already-outstanding snapshot (idempotent re-pin) is
    not an over-pin."""
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapA")
    m._pin("snapA")
    assert m.consume_pin_contract_break() is None


def test_pin_contract_unpin_all_clears_outstanding_in_refresh_teardown():
    """Teardown from the two-pin refresh window unpins both, leaving no
    residue to false-flag the next session."""
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapA")
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    drive_to_painted(m, snapshot_id="snapC")
    assert m.consume_pin_contract_break() is None

def test_pin_contract_break_flags_once_then_reconciles():
    """Reviewer_0 finding .1.1: one leaked id must flag ONE break, not a
    warning on every subsequent pin forever (over a browser window the
    proactive refresh pins every ~15s). At flag time the audit set
    reconciles to the machine's authoritative bookkeeping, so a correct
    stream after the break stays clean."""
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapA")
    # A real racing double-pin: the buggy path emits the PIN effect and
    # updates the machine's pinned id, but never unpinned snapA.
    m._pin("snapB")
    m.pinned_snapshot_id = "snapB"
    msg = m.consume_pin_contract_break()
    assert msg is not None
    # A legitimate refresh cycle after the break must NOT re-flag.
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapC"))
    assert m.consume_pin_contract_break() is None
    m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    assert m.consume_pin_contract_break() is None
    # And a follow-on hide -> fresh session is clean too.
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    drive_to_painted(m, snapshot_id="snapD")
    assert m.consume_pin_contract_break() is None


def test_pin_contract_leak_survives_session_teardown():
    """Reviewer_0 finding .1.2: the cross-session detection is the
    feature's headline case -- a teardown path that lost an unpin must be
    flagged by the NEXT session's first pin. This test is the enforcement
    of the deliberate no-clear-in-_enter_closed choice: adding
    _outstanding_pins.clear() to _enter_closed makes it fail."""
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapA")
    # Emulate a lost unpin: a pin whose UNPIN effect never shipped.
    m._outstanding_pins.add("stale-leak")
    # Clean teardown of the session (snapA unpins normally; the leaked id
    # has no unpin to ship, so it must survive _enter_closed).
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    # Next session's first pin flags the stale id.
    drive_to_painted(m, snapshot_id="snapB")
    msg = m.consume_pin_contract_break()
    assert msg is not None
    assert "stale-leak" in msg
    # One flag, then reconciled: the rest of the session is clean.
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    drive_to_painted(m, snapshot_id="snapC")
    assert m.consume_pin_contract_break() is None
