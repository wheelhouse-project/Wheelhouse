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


def test_painted_expired_ack_tears_down_to_closed():
    # wh-overlay-slow-uia-stale-badges.9: an EXPIRED ack is the GUI's report
    # that its badge lease ran out and it removed the badges itself. In
    # PAINTED at the current pair that means the user no longer sees the
    # numbers Logic believes are on screen, so the machine must not keep a
    # phantom overlay: tear down to closed exactly like hide_numbers.
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    r = m.apply(
        gen_event(
            m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.EXPIRED,
        )
    )
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.CLOSED
    assert m.pinned_snapshot_id is None
    kinds = effect_kinds(r)
    # The defensive clear echo covers a partial GUI teardown (a DestroyWindow
    # failure kept a window alive); the unpin releases the snapshot.
    assert EffectKind.DISPATCH_CLEAR in kinds
    assert EffectKind.UNPIN_SNAPSHOT in kinds


def test_painted_expired_ack_stale_pair_rejected():
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    r = m.apply(
        OverlayEvent(
            OverlayEventKind.PAINT_ACK,
            overlay_session_id=m.overlay_session_id,
            paint_generation=m.paint_generation + 7,
            paint_state=PaintAckState.EXPIRED,
        )
    )
    assert r.outcome is OverlayOutcome.STALE_GENERATION
    assert m.state is OverlayState.PAINTED


def test_expired_ack_is_bookkeeping_outside_painted():
    # In closed / paused / post_click_settling the badges are already gone
    # (or were never presented), so a lease-expiry report is bookkeeping
    # only: the state's own clear was lost and the GUI cleaned up itself.
    # closed:
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    r = m.apply(
        gen_event(
            m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.EXPIRED,
        )
    )
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.CLOSED
    # paused:
    m2 = ClickOverlayStateMachine()
    drive_to_painted(m2)
    m2.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m2.state is OverlayState.PAUSED
    r2 = m2.apply(
        gen_event(
            m2, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.EXPIRED,
        )
    )
    assert r2.outcome is OverlayOutcome.NO_OP
    assert m2.state is OverlayState.PAUSED
    # post_click_settling:
    m3 = ClickOverlayStateMachine(settle_after_click=True)
    drive_to_painted(m3)
    m3.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    assert m3.state is OverlayState.POST_CLICK_SETTLING
    r3 = m3.apply(
        gen_event(
            m3, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.EXPIRED,
        )
    )
    assert r3.outcome is OverlayOutcome.NO_OP
    assert m3.state is OverlayState.POST_CLICK_SETTLING


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


def test_painted_late_build_response_is_a_stale_no_op():
    # wh-overlay-slow-uia-stale-badges.19. The refresh fall-back returns the
    # machine to painted at the SAME pair, so a build response the machine
    # itself dispatched can land here. That is a late completion of the
    # machine's own work, not a protocol violation, and the closed / error
    # handlers already consume such a completion as a NO_OP (wh-n29v.19.1,
    # wh-n29v.70.3). _invalid entered error with NO clear, so the badges stayed
    # on screen until the GUI badge lease removed them while "click N" answered
    # numbers_not_showing. Consume it, keep the visible pin, keep painted.
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapA")
    m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.PAINTED
    r = m.apply(
        gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB")
    )
    assert r.outcome is OverlayOutcome.NO_OP
    assert r.effects == ()
    assert m.state is OverlayState.PAINTED
    assert m.pinned_snapshot_id == "snapA"
    assert m.reason == ""


# ---------------------------------------------------------------------------
# refresh_in_flight row
# ---------------------------------------------------------------------------

def to_refresh(m: ClickOverlayStateMachine):
    # FOCUS_CHANGE, not CLICK_COMPLETE. Since
    # wh-overlay-slow-uia-stale-badges.1, where CLICK_COMPLETE goes depends on
    # settle_after_click: refresh_in_flight with the flag off (the shipped
    # default, which these tests use), post_click_settling with it on. This
    # helper serves the whole refresh_in_flight block, so it drives the state
    # with an event that refreshes either way. Flipping the default when
    # child .2 lands then leaves this block alone.
    drive_to_painted(m, snapshot_id="snapA")
    m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
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


def test_refresh_paint_ack_failed_auto_hide_restores_prior_to_paused():
    # wh-overlay-slow-uia-stale-badges.18.7: this test pinned the old
    # FAILED -> PAINTED fall-back for every failed refresh paint-ack; that
    # cell is now destructive (see the closes_and_clears test below) and
    # ONLY the auto-hide leg keeps the Finding 1 fall-back -- the mic-pause
    # already cleared the screen, PAUSED stops the keepalive renew, and the
    # restored prior pin is what mic-resume repaints.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.auto_hide_in_flight is True
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.FAILED))
    assert m.state is OverlayState.PAUSED
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.auto_hide_in_flight is False
    # pinned restored to the prior snapshot
    assert m.pinned_snapshot_id == "snapA"
    assert m.prior_pinned_snapshot_id is None
    assert m._prior_pin_deferred is False
    # the new failed snapshot is unpinned; the prior is NOT unpinned
    unpins = [e for e in r.effects if e.kind is EffectKind.UNPIN_SNAPSHOT]
    assert len(unpins) == 1
    assert unpins[0].snapshot_id == "snapB"
    # NO clear from this transition: the mic-pause already dispatched one.
    assert EffectKind.DISPATCH_CLEAR not in effect_kinds(r)


def test_refresh_paint_ack_failed_no_auto_hide_closes_and_clears():
    # wh-overlay-slow-uia-stale-badges.18.7: a failed refresh paint is
    # DESTRUCTIVE -- both production failed emitters in
    # OverlayPaintWindowManager.paint() invalidate the display before they
    # emit (the exception branch destroys every window; the any_failed
    # branch destroyed each failed monitor's window while other monitors
    # already show the new generation). Falling back to PAINTED would let
    # the keepalive renew the GUI's lease forever over a destroyed or
    # mixed display. Route to _error_to_closed, the same contract as
    # PAINT_IN_FLIGHT x FAILED: clear, unpin BOTH snapshots, close.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    sess, gen = m.overlay_session_id, m.paint_generation
    r = m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.FAILED))
    assert r.outcome is OverlayOutcome.ACCEPTED
    assert m.state is OverlayState.CLOSED
    clears = [e for e in r.effects if e.kind is EffectKind.DISPATCH_CLEAR]
    assert len(clears) == 1
    assert clears[0].overlay_session_id == sess
    assert clears[0].paint_generation == gen
    # BOTH the new pinned snapshot and the deferred prior are unpinned.
    unpinned = {
        e.snapshot_id for e in r.effects if e.kind is EffectKind.UNPIN_SNAPSHOT
    }
    assert unpinned == {"snapA", "snapB"}
    # v4 line 279: a paint-phase failure fires NO standalone notice.
    assert EffectKind.FIRE_NOTICE not in effect_kinds(r)
    assert m.pinned_snapshot_id is None
    assert m.prior_pinned_snapshot_id is None
    assert m._prior_pin_deferred is False


def test_refresh_stale_pair_failed_ack_rejected():
    # wh-overlay-slow-uia-stale-badges.18.7: the destructive close applies
    # only to a LIVE-pair failed ack. A failed ack at a stale pair (here
    # the prior generation's) is rejected by the apply() generation gate
    # with no state change and no effects.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    stale = OverlayEvent(
        OverlayEventKind.PAINT_ACK,
        overlay_session_id=m.overlay_session_id,
        paint_generation=m.paint_generation - 1,
        paint_state=PaintAckState.FAILED,
    )
    r = m.apply(stale)
    assert r.outcome is OverlayOutcome.STALE_GENERATION
    assert r.effects == ()
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    assert m.pinned_snapshot_id == "snapB"
    assert m._prior_pin_deferred is True


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
    # Finding 1 end-to-end, auto-hide leg. Updated for
    # wh-overlay-slow-uia-stale-badges.18.7: the mic-pause now has to land
    # BEFORE the failed ack (a failed paint without auto-hide closes the
    # session, so there is nothing left to pause). After the auto-hide
    # fall-back the prior snapshot is the pinned one, so mic-resume-valid
    # restores the PRIOR (correct) snapshot, not the failed one.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.FAILED))
    assert m.pinned_snapshot_id == "snapA"
    assert m.state is OverlayState.PAUSED
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True))
    assert m.state is OverlayState.PAINTED
    paint = [e for e in r.effects if e.kind is EffectKind.DISPATCH_PAINT][0]
    assert paint.snapshot_id == "snapA"  # the correct restorable snapshot


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


def test_paused_mic_resume_valid_repaints_at_a_new_generation():
    # wh-overlay-slow-uia-stale-badges.17. The mic-pause dispatches a clear at
    # the current pair. The GUI generation gate
    # (``overlay_paint_window.GenerationGate.accept_paint``) refuses a paint
    # whose pair is <= the pair of the last accepted clear, so a restore paint
    # at the SAME pair never reaches the screen while the machine sits in
    # PAINTED and routes "click N" to badges. The restore paint must carry a
    # strictly newer pair, exactly like the stale branch below it.
    m = ClickOverlayStateMachine()
    to_paused(m)
    pause_clear = m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert pause_clear.outcome is OverlayOutcome.NO_OP  # already paused
    # The clear that hid the badges was dispatched on the painted -> paused
    # edge inside ``to_paused``; its pair is the machine's pair while paused.
    cleared_pair = (m.overlay_session_id, m.paint_generation)
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True))
    assert m.state is OverlayState.PAINTED
    paint = [e for e in r.effects if e.kind is EffectKind.DISPATCH_PAINT][0]
    assert paint.snapshot_id == "snapP"
    assert paint.overlay_session_id == cleared_pair[0]
    assert (paint.overlay_session_id, paint.paint_generation) > cleared_pair
    assert m.paint_generation == cleared_pair[1] + 1


def test_paused_restore_paint_asks_for_a_delivery_audit():
    # wh-overlay-slow-uia-stale-badges.21.3. The restore commits PAINTED
    # synchronously, so it is the ONE paint the machine emits with no
    # ack-expecting state and no deadline behind it. A paint the integration
    # cannot deliver (a full display queue, an evicted summary) would leave
    # Logic believing badges are up over an empty screen. The effect must carry
    # the marker that tells the integration to audit this paint's delivery.
    m = ClickOverlayStateMachine()
    to_paused(m)
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True))
    paint = [e for e in r.effects if e.kind is EffectKind.DISPATCH_PAINT][0]
    assert paint.audit_delivery is True


def test_deadline_guarded_paints_ask_for_no_delivery_audit():
    # Every OTHER paint lands in an ack-expecting state whose armed deadline
    # already recovers a lost paint, so auditing them would double-report the
    # same loss. Cover both: the walk paint and the refresh paint.
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    walk = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapA"))
    walk_paint = [e for e in walk.effects if e.kind is EffectKind.DISPATCH_PAINT][0]
    assert walk_paint.audit_delivery is False
    m.apply(gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED))
    m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    ref = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB"))
    ref_paint = [e for e in ref.effects if e.kind is EffectKind.DISPATCH_PAINT][0]
    assert ref_paint.audit_delivery is False


def test_paused_entry_clear_and_resume_paint_pairs_differ():
    # wh-overlay-slow-uia-stale-badges.17, asserted on the two effects
    # themselves rather than on the machine's fields: the clear the
    # painted -> paused edge emits and the paint the resume emits must not
    # carry the same pair.
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapP")
    pause = m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    clear = [e for e in pause.effects if e.kind is EffectKind.DISPATCH_CLEAR][0]
    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True))
    paint = [e for e in r.effects if e.kind is EffectKind.DISPATCH_PAINT][0]
    assert (paint.overlay_session_id, paint.paint_generation) > (
        clear.overlay_session_id, clear.paint_generation
    )


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


def test_paused_late_build_response_is_a_stale_no_op():
    # wh-overlay-slow-uia-stale-badges.22, the mirror of the PAINTED cell
    # fixed in .19. PAUSED and PAINTED share one entry path: the refresh
    # fall-back (``_refresh_fall_back``) lands in PAUSED instead of PAINTED
    # whenever ``auto_hide_in_flight`` is set, and it returns at the SAME
    # pair without bumping the generation. So a build response for the
    # refresh THIS machine dispatched passes the pre-table generation gate
    # and reaches the paused row, exactly as it reached the painted row.
    # Only the painted side was fixed; paused still fell through to
    # ``_invalid``, which enters ERROR and sends NO clear -- the same
    # stale-badge failure .19 removed. Consume it, keep the restored pin,
    # stay paused.
    #
    # This is a state-table correction, not a live defect: the .19 sender
    # fence in main.py (``_machine_still_awaits_this_build``) admits only
    # WALK_IN_FLIGHT, REFRESH_IN_FLIGHT and POST_CLICK_SETTLING, so the
    # shipped sender drops a PAUSED reply before it reaches the machine.
    # The table should be right on its own rather than right because one
    # caller happens to guard it.
    m = ClickOverlayStateMachine()
    drive_to_painted(m, snapshot_id="snapA")
    m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    # The mic pause arms the auto-hide leg; the machine stays in flight.
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    # The refresh times out: the fall-back takes the auto-hide leg to paused.
    m.apply(gen_event(m, OverlayEventKind.TIMEOUT))
    assert m.state is OverlayState.PAUSED

    r = m.apply(
        gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapB")
    )
    assert r.outcome is OverlayOutcome.NO_OP
    assert r.effects == ()
    assert m.state is OverlayState.PAUSED
    assert m.pinned_snapshot_id == "snapA"
    assert m.reason == ""


@pytest.mark.parametrize(
    "kind",
    [
        # BUILD_RESPONSE left this list in wh-overlay-slow-uia-stale-badges.22:
        # the paused row now consumes a late build reply as a NO_OP, mirroring
        # the painted cell from .19. See
        # test_paused_late_build_response_is_a_stale_no_op above.
        OverlayEventKind.CLICK_COMPLETE,
        OverlayEventKind.AUTO_OPEN,
    ],
)
def test_paused_invalid_events(kind):
    m = ClickOverlayStateMachine()
    to_paused(m)
    r = m.apply(OverlayEvent(kind))
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
    m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))  # refresh, bump to 1
    assert m.paint_generation == 1
    # New session resets generation to 0
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    assert m.paint_generation == 0


def test_generation_bumped_on_each_refresh():
    m = ClickOverlayStateMachine()
    drive_to_painted(m)
    assert m.paint_generation == 0
    m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
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
    # pin + clear -> nothing is ever presented
    # (wh-overlay-slow-uia-stale-badges.21.1).
    assert EffectKind.PIN_SNAPSHOT in kinds
    assert EffectKind.DISPATCH_CLEAR in kinds


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


def test_refresh_mic_pause_then_build_ok_resolves_to_paused():
    # wh-overlay-slow-uia-stale-badges.21.1 leg 2. The mic-pause dispatched the
    # clear at THIS pair, so the display gate refuses a paint at the same pair
    # and answers nothing. The build-ok must therefore resolve to PAUSED itself
    # instead of waiting for a paint-ack that the gate makes impossible; a later
    # paint-ack at the same pair is paused bookkeeping.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.auto_hide_in_flight is True
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapR"))
    assert m.state is OverlayState.PAUSED
    assert m.auto_hide_in_flight is False
    assert EffectKind.DISPATCH_CLEAR in effect_kinds(r)
    late = m.apply(
        gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED)
    )
    assert late.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.PAUSED


def test_refresh_build_then_mic_pause_still_resolves_on_the_paint_ack():
    # The OTHER refresh auto-hide ordering, which the build-ok change must not
    # disturb: the build succeeded and the paint was already dispatched BEFORE
    # the mic paused, so the GUI can still present it and ack it. That ack is
    # what drives the machine to PAUSED (``_paint_ack_to_paused``).
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapR"))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert m.auto_hide_in_flight is True
    r = m.apply(
        gen_event(m, OverlayEventKind.PAINT_ACK, paint_state=PaintAckState.PAINTED)
    )
    assert m.state is OverlayState.PAUSED
    assert m.auto_hide_in_flight is False
    assert m.pinned_snapshot_id == "snapR"
    assert EffectKind.DISPATCH_CLEAR in effect_kinds(r)


def test_refresh_build_ok_with_auto_hide_commits_the_new_pin_at_paused():
    # wh-overlay-slow-uia-stale-badges.21.1 leg 2, on the pin bookkeeping. The
    # old path parked in REFRESH_IN_FLIGHT until the walk deadline, and the
    # timeout fall-back then unpinned the NEW snapshot and restored the PRIOR
    # one -- so the resume repainted the pre-refresh list. Resolving here
    # commits the refresh instead: the new snapshot is the sole pin.
    m = ClickOverlayStateMachine()
    to_refresh(m)
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapR2"))
    assert m.state is OverlayState.PAUSED
    assert m.pinned_snapshot_id == "snapR2"
    assert m.prior_pinned_snapshot_id is None
    assert m.prior_pin_deferred is False
    kinds = effect_kinds(r)
    assert EffectKind.DISPATCH_PAINT not in kinds
    assert kinds == [
        EffectKind.PIN_SNAPSHOT,
        EffectKind.CANCEL_TIMER,
        EffectKind.UNPIN_SNAPSHOT,
        EffectKind.DISPATCH_CLEAR,
    ]
    unpin = [e for e in r.effects if e.kind is EffectKind.UNPIN_SNAPSHOT][0]
    assert unpin.snapshot_id == "snapA"  # the prior, now-superseded snapshot


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
# wh-overlay-slow-uia-stale-badges.21.1: an auto-hide resolution emits NO
# paint. The old code emitted a paint flagged ``immediate_clear`` that no
# reader honoured, so the walk leg composited one visible frame (and armed a
# badge lease the same batch then cancelled), and the refresh leg dispatched a
# paint the display gate always refused.
# ---------------------------------------------------------------------------

def test_walk_auto_hide_resolution_ships_a_clear_and_no_paint():
    # Walk path: mic-pause mid-walk, then the build-response resolves straight
    # to paused. Only the clear ships, so nothing is ever composited.
    m = ClickOverlayStateMachine()
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    r = m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, snapshot_id="snapW"))
    assert m.state is OverlayState.PAUSED
    kinds = effect_kinds(r)
    assert EffectKind.DISPATCH_PAINT not in kinds
    assert kinds == [
        EffectKind.CANCEL_TIMER,
        EffectKind.PIN_SNAPSHOT,
        EffectKind.DISPATCH_CLEAR,
    ]
    clear = r.effects[kinds.index(EffectKind.DISPATCH_CLEAR)]
    assert (clear.overlay_session_id, clear.paint_generation) == (
        m.overlay_session_id, m.paint_generation
    )


def test_effect_carries_no_never_presented_paint_flag():
    # The ``immediate_clear`` field was data that no reader ever read: the
    # integration ignored it and PaintOverlayEvent had no such field. The
    # machine now emits no never-visible paint at all, so the field must be
    # gone rather than left as a contract a future reader would trust.
    import dataclasses

    from services.wheelhouse.click_overlay_state import Effect

    assert "immediate_clear" not in {
        f.name for f in dataclasses.fields(Effect)
    }


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


# ---------------------------------------------------------------------------
# Post-click settling state (wh-overlay-slow-uia-stale-badges.1)
# ---------------------------------------------------------------------------


def settling_machine() -> ClickOverlayStateMachine:
    """A machine with the settle-after-click behaviour turned ON.

    The behaviour ships OFF. Child .1 only opens the gap after a click;
    child .2 is the half that closes it again by reading the screen and
    repainting. Child .1 alone would leave the user with no numbers, which
    is the option David rejected. Every test below therefore asks for the
    flag; the shipped default is covered by
    test_click_complete_still_refreshes_when_the_flag_is_off.
    """
    return ClickOverlayStateMachine(settle_after_click=True)


def test_click_complete_still_refreshes_when_the_flag_is_off():
    """The shipped default keeps the pre-bead behaviour, badge defect and all.

    Asserts the whole effect contract, not just the state
    (wh-overlay-slow-uia-stale-badges.13.4). CLICK_COMPLETE is the only
    painted trigger whose destination depends on the flag, so it no longer
    rides the test_painted_refresh_triggers parametrize. Without the effects
    here, a regression that reached refresh_in_flight but dispatched no build
    or armed no timer would pass, and that regression IS the shipped
    stale-badge failure: the old badges stay up and nothing replaces them.
    """
    m = ClickOverlayStateMachine()
    assert m.settle_after_click is False
    drive_to_painted(m)
    gen_before = m.paint_generation

    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))

    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    assert m.paint_generation == gen_before + 1
    assert effect_kinds(r) == [
        EffectKind.CANCEL_TIMER,
        EffectKind.DISPATCH_BUILD,
        EffectKind.ARM_TIMER,
    ]
    assert r.effects[1].build_reason is BuildReason.REFRESH
    assert r.effects[2].timer_state is OverlayState.REFRESH_IN_FLIGHT
    # The prior snapshot stays pinned: it is still visible during a refresh.
    assert m.pinned_snapshot_id == "snap"


def test_painted_click_complete_enters_settling_and_clears_badges():
    """A successful badge click clears the badges and enters settling.

    Before this bead CLICK_COMPLETE called _refresh, which left the OLD
    badges on screen for the whole re-walk and left them there permanently
    when the re-walk failed. That is the stale-badge defect.
    """
    m = settling_machine()
    drive_to_painted(m)
    sess_before = m.overlay_session_id
    gen_before = m.paint_generation

    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))

    assert m.state is OverlayState.POST_CLICK_SETTLING
    # ARM_TIMER precedes DISPATCH_BUILD
    # (wh-overlay-slow-uia-stale-badges.2.2.3): the performer awaits the build
    # inside the batch, so a timer armed after it would start counting only
    # once the Input round trip ended.
    assert effect_kinds(r) == [
        EffectKind.CANCEL_TIMER,
        EffectKind.DISPATCH_CLEAR,
        EffectKind.ARM_TIMER,
        EffectKind.DISPATCH_BUILD,
    ]
    # The session bookkeeping SURVIVES: child .2 re-reads and repaints into
    # this same session, and its no-change comparison needs the old snapshot
    # still pinned in the Input store.
    assert m.overlay_session_id == sess_before
    assert m.pinned_snapshot_id == "snap"
    assert r.effects[2].timer_state is OverlayState.POST_CLICK_SETTLING
    # The generation DOES move now. Child .1 shipped without the bump and
    # this test asserted its absence; child .1's own docstring reserved the
    # bump for "the build that child .2 dispatches", and child .2 spends it
    # here. test_settling_entry_dispatches_the_re_read_itself owns the full
    # contract, including which effect carries which generation.
    assert m.paint_generation == gen_before + 1


def test_settling_entry_dispatches_the_re_read_itself():
    """Acceptance criterion 1: entry starts the re-read with no spoken command.

    The clear keeps the generation the GUI painted, because that is the pair
    the GUI matches it against. The build that follows carries the bumped
    generation, so a response for the pre-click generation cannot be mistaken
    for this read's answer. ``_enter_post_click_settling`` was written for
    this: its docstring already reserved the bump for "the build that child
    .2 dispatches".
    """
    m = settling_machine()
    drive_to_painted(m)
    sess_before = m.overlay_session_id
    gen_before = m.paint_generation

    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))

    assert effect_kinds(r) == [
        EffectKind.CANCEL_TIMER,
        EffectKind.DISPATCH_CLEAR,
        EffectKind.ARM_TIMER,
        EffectKind.DISPATCH_BUILD,
    ]
    clear, arm, build = r.effects[1], r.effects[2], r.effects[3]
    assert clear.paint_generation == gen_before
    assert build.paint_generation == gen_before + 1
    # The timer is armed on the BUMPED generation
    # (wh-overlay-slow-uia-stale-badges.2.2.3). The timer callback feeds its
    # TIMEOUT at the armed pair, and the machine's generation gate drops a
    # TIMEOUT at a superseded pair, so an ARM_TIMER moved ahead of the bump
    # would produce a timer that can never fire into this state.
    assert arm.paint_generation == gen_before + 1
    assert m.paint_generation == gen_before + 1
    assert build.build_reason is BuildReason.SETTLE
    assert build.overlay_session_id == sess_before
    # The pre-click snapshot stays pinned: criterion 3's comparison needs it.
    assert m.pinned_snapshot_id == "snap"
    assert m.state is OverlayState.POST_CLICK_SETTLING


def test_the_settle_build_carries_the_pre_click_pin_as_the_compare_id():
    """Acceptance criterion 3: the Input side is told WHAT to compare against.

    The comparison target rides the DISPATCH_BUILD effect rather than being
    read from the live machine pin when the performer runs, for the reason
    already documented for AUTO_OPEN at ``_dispatch_build``: the performer is
    async and the pin can move before it looks. The pin is unchanged at this
    point -- entry clears the badges and bumps the generation, neither of
    which unpins -- so the id on the effect is the pre-click snapshot.
    """
    m = settling_machine()
    drive_to_painted(m)

    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))

    build = r.effects[3]
    assert build.build_reason is BuildReason.SETTLE
    assert build.snapshot_id == "snap"
    assert build.snapshot_id == m.pinned_snapshot_id


def test_a_refresh_build_carries_no_compare_id():
    """Only SETTLE compares. A refresh walk answers with whatever it reads.

    Without this, a refresh could inherit the settle path's unchanged answer
    and restore numbers the user asked to have rebuilt. This one was green
    before the compare id existed -- ``snapshot_id`` was already None on every
    walk -- so it is a "stays untouched" guard, not evidence of new behaviour.
    """
    m = settling_machine()
    drive_to_painted(m)

    # "show numbers" over an already-painted overlay is a REFRESH, not a
    # fresh SHOW_NUMBERS build: the machine is in painted, not closed.
    r = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))

    builds = [e for e in r.effects if e.kind is EffectKind.DISPATCH_BUILD]
    assert len(builds) == 1
    assert builds[0].build_reason is BuildReason.REFRESH
    assert builds[0].snapshot_id is None


@pytest.mark.parametrize(
    "kind",
    [OverlayEventKind.SHOW_NUMBERS, OverlayEventKind.FOCUS_CHANGE],
)
def test_painted_show_numbers_and_focus_change_still_refresh(kind):
    """Only CLICK_COMPLETE changes. The other two triggers still refresh."""
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(kind))
    assert m.state is OverlayState.REFRESH_IN_FLIGHT


def test_settling_state_has_a_maximum():
    """The settling state cannot last forever."""
    m = settling_machine()
    assert m.timeout_ms(OverlayState.POST_CLICK_SETTLING) != _NO_TIMEOUT
    assert m.timeout_ms(OverlayState.POST_CLICK_SETTLING) > 0


def test_settling_timeout_falls_back_to_closed_and_unpins():
    """Nothing ended the settling state, so the machine gives up cleanly."""
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))

    r = m.apply(gen_event(m, OverlayEventKind.TIMEOUT))

    assert m.state is OverlayState.CLOSED
    assert m.pinned_snapshot_id is None
    assert EffectKind.UNPIN_SNAPSHOT in effect_kinds(r)


def test_settling_hide_numbers_closes():
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    m.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert m.state is OverlayState.CLOSED
    assert m.pinned_snapshot_id is None


def test_settling_click_n_makes_no_state_change():
    """The refusal is the router's job; the machine only holds the state."""
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    r = m.apply(OverlayEvent(OverlayEventKind.CLICK_N))
    assert r.outcome is OverlayOutcome.NO_OP
    assert m.state is OverlayState.POST_CLICK_SETTLING


def test_repeated_focus_change_while_settling_starts_no_second_read():
    """Acceptance criterion 5: no read per keystroke.

    Typing into a control the click focused raises FOCUS_CHANGE repeatedly.
    The settle path is entered only by a successful badge click, and once in
    it the machine must not treat each of those as a new reason to read: a
    read costs about 450 ms, so one per keystroke would make the machine
    read continuously and never reach the answer. The entry dispatched
    exactly one build; nothing after it adds another.
    """
    m = settling_machine()
    drive_to_painted(m)
    entry = m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    builds_at_entry = [
        e for e in entry.effects if e.kind is EffectKind.DISPATCH_BUILD
    ]
    assert len(builds_at_entry) == 1

    later_builds = []
    for _ in range(8):
        r = m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
        assert r.outcome is OverlayOutcome.NO_OP
        later_builds += [
            e for e in r.effects if e.kind is EffectKind.DISPATCH_BUILD
        ]

    assert later_builds == []
    assert m.state is OverlayState.POST_CLICK_SETTLING


def test_the_settling_bound_is_the_configured_settle_deadline():
    """Acceptance criterion 6, the machine's half of the double bound.

    The state's maximum has to come from ``settle_deadline_ms`` rather than
    from a constant written into the timer, or tuning the config would leave
    the real bound where it was. The value is not asserted -- David owns it --
    only that changing it changes the bound.
    """
    m = ClickOverlayStateMachine(
        settle_after_click=True, settle_deadline_ms=4321,
    )
    assert m.timeout_ms(OverlayState.POST_CLICK_SETTLING) == 4321.0
    assert settling_machine().timeout_ms(
        OverlayState.POST_CLICK_SETTLING
    ) == float(ClickOverlayStateMachine.settle_deadline_ms)


def test_settling_focus_change_keeps_the_state():
    """A click that opens a dialog changes the foreground window.

    Cancelling here would make the common case behave like the rejected
    Option A. The machine stays; the integration (child .2) decides which
    window to read and revalidates identity before it paints.
    """
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    m.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert m.state is OverlayState.POST_CLICK_SETTLING


def _settle_build(m, snapshot_id: str, *, build_ok: bool = True):
    """The settle build's answer, stamped with the pair the entry bumped to."""
    return OverlayEvent(
        OverlayEventKind.BUILD_RESPONSE,
        overlay_session_id=m.overlay_session_id,
        paint_generation=m.paint_generation,
        snapshot_id=snapshot_id,
        build_ok=build_ok,
    )


def test_settling_unchanged_read_repaints_the_same_snapshot():
    """Acceptance criterion 3: nothing changed, so the same numbers come back.

    The Input side owns the comparison -- it holds both match lists -- and
    says "unchanged" by answering with the SAME snapshot id it was already
    holding. The machine must then repaint that snapshot and keep the pin:
    re-pinning would be a second pin of a snapshot already pinned, and
    unpinning would destroy the list the badges are about to be drawn from.
    """
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    gen_settle = m.paint_generation

    r = m.apply(_settle_build(m, "snap"))

    assert m.state is OverlayState.PAINT_IN_FLIGHT
    assert effect_kinds(r) == [
        EffectKind.CANCEL_TIMER,
        EffectKind.DISPATCH_PAINT,
        EffectKind.ARM_TIMER,
    ]
    assert r.effects[1].snapshot_id == "snap"
    assert m.pinned_snapshot_id == "snap"
    # No second pin and no unpin: the pre-click snapshot never stopped being
    # the pinned one, and it is the one being painted.
    assert EffectKind.PIN_SNAPSHOT not in effect_kinds(r)
    assert EffectKind.UNPIN_SNAPSHOT not in effect_kinds(r)
    # The generation does not move again: this paint answers the settle
    # build, so it must carry the generation that build was stamped with.
    assert m.paint_generation == gen_settle
    assert r.effects[1].paint_generation == gen_settle


def test_settling_changed_read_paints_the_new_snapshot_and_unpins_the_old():
    """Acceptance criterion 4: the content changed, so new numbers appear.

    The pre-click snapshot must be unpinned in the same batch. Leaving it
    pinned would hold a second list in the Input store for the whole overlay
    session, and a later spoken number could still resolve against it.
    """
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))

    r = m.apply(_settle_build(m, "snap-after"))

    assert m.state is OverlayState.PAINT_IN_FLIGHT
    assert effect_kinds(r) == [
        EffectKind.CANCEL_TIMER,
        EffectKind.UNPIN_SNAPSHOT,
        EffectKind.PIN_SNAPSHOT,
        EffectKind.DISPATCH_PAINT,
        EffectKind.ARM_TIMER,
    ]
    # The unpin names the OLD snapshot and runs BEFORE the new pin, so the
    # store is never asked to hold two pinned lists for one session.
    assert r.effects[1].snapshot_id == "snap"
    assert r.effects[2].snapshot_id == "snap-after"
    assert r.effects[3].snapshot_id == "snap-after"
    assert m.pinned_snapshot_id == "snap-after"


def test_settling_build_failure_closes_without_stranding_the_pin():
    """The re-read could not be produced at all: give up cleanly.

    Mirrors the TIMEOUT arm of this state. The badges are already gone, so
    the only cost is that the user says "show numbers" again; the pin must
    not survive, or the next session starts holding a dead pre-click list.
    """
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))

    r = m.apply(_settle_build(m, "snap", build_ok=False))

    assert m.state is OverlayState.CLOSED
    assert m.pinned_snapshot_id is None
    assert EffectKind.UNPIN_SNAPSHOT in effect_kinds(r)


def test_settling_drops_a_build_response_for_the_pre_click_generation():
    """A late answer to the pre-click paint must not be read as the re-read.

    The entry bumps the generation exactly so this cannot happen. The gate
    lives in ``apply``; this test proves the settle build is inside it.
    """
    m = settling_machine()
    drive_to_painted(m)
    gen_before = m.paint_generation
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))

    r = m.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE,
            overlay_session_id=m.overlay_session_id,
            paint_generation=gen_before,
            snapshot_id="snap-stale",
            build_ok=True,
        )
    )

    assert r.outcome is OverlayOutcome.STALE_GENERATION
    assert m.state is OverlayState.POST_CLICK_SETTLING
    assert m.pinned_snapshot_id == "snap"


def test_settling_show_numbers_walks_fresh_and_drops_the_stale_pin():
    """"Apply numbers" during settling starts a fresh walk, not a refresh.

    wh-overlay-slow-uia-stale-badges.13.1. A refresh keeps the pin, because
    a refresh runs while the previous paint is still visible. In settling
    the previous paint is gone, so the pre-click pin must go with it.
    """
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))

    r = m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))

    assert m.state is OverlayState.WALK_IN_FLIGHT
    assert m.pinned_snapshot_id is None
    assert EffectKind.UNPIN_SNAPSHOT in effect_kinds(r)
    assert EffectKind.DISPATCH_BUILD in effect_kinds(r)


def test_settling_show_numbers_failure_cannot_bring_back_the_old_badges():
    """The consequence the fresh walk prevents.

    A refresh that fails runs _refresh_fall_back, which returns to painted
    and restores the pin. Out of settling that would leave the machine
    saying "badge 7 is on screen" with an empty screen and the PRE-CLICK
    list pinned, so the next spoken number would click the wrong control.
    """
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))

    m.apply(gen_event(m, OverlayEventKind.BUILD_RESPONSE, build_ok=False))

    assert m.state is OverlayState.CLOSED
    assert m.pinned_snapshot_id is None


def test_settling_mic_pause_closes_and_unpins():
    """The microphone pause closes; it does not park in paused.

    wh-overlay-slow-uia-stale-badges.13.1. Paused exists to hold badges
    that are on screen. Settling has none, so there is nothing to hold.
    """
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))

    r = m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))

    assert m.state is OverlayState.CLOSED
    assert m.pinned_snapshot_id is None
    assert EffectKind.UNPIN_SNAPSHOT in effect_kinds(r)


def test_settling_mic_pause_resume_does_not_repaint_the_old_badges():
    """The consequence the close prevents.

    A resume out of paused repaints pinned_snapshot_id. In settling that
    id is the PRE-CLICK snapshot, so the resume would put the stale
    numbers back on screen.
    """
    m = settling_machine()
    drive_to_painted(m)
    m.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))

    r = m.apply(OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True))

    assert EffectKind.DISPATCH_PAINT not in effect_kinds(r)
    assert m.state is OverlayState.CLOSED
