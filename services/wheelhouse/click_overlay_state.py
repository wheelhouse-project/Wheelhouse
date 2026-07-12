"""Logic-process toggle state machine for the numbered overlay (wh-gxj4kx).

Parent epic: ``wh-n29v`` (voice-element-clicking Phase 1.5). The
authoritative spec is the v4 design doc
``docs/plans/2026-05-28-voice-element-clicking-phase-1-5-design-v4.md``,
specifically the "## Toggle state machine" section (the state diagram,
the "### States" table, "### Per-state timeouts", "### Allowed inbound
events per state", the two extra-event notes, the in-flight-while-paused
annotation, and "### Cross-process producer/consumer") plus "### Mic-pause
corner cases".

This module is the single source of truth for the Phase 1.5
numbered-overlay lifecycle. It is modelled on the ``LogicMirror``
precedent in ``services/wheelhouse/shared/editor_lifecycle.py``: a small
finite state machine that

  * tracks the overlay state (``OverlayState``),
  * allocates and enforces the ``(overlay_session_id, paint_generation)``
    pair (generation discipline; see "Generation allocation" below),
  * rejects stale generation-bearing events and invalid ``(state,
    event)`` combinations, and
  * RETURNS, as plain data, the side effects the integration layer must
    perform (dispatch paint / clear, pin / unpin a snapshot, fire a
    notice, arm / cancel a timeout timer, dispatch a build request).

PURITY. The class performs NO input/output of any kind: no asyncio, no
real timers, no Win32, no IPC queues, no config reads, no logging that
matters to behaviour. ``apply(event)`` is a deterministic pure function
of the current state and the event; it returns ``(outcome, effects)``.
The actual focus-identity check, the 200 ms "click N" hold timer, the
real timeout timers, and all IPC are the INTEGRATION layer's job (a
separate slice). The class only decides state and returns effects.

The single imported domain type is ``ClickNoticeEvent`` (held by
``pending_ambiguous_notice`` and re-emitted via a ``fire_notice`` effect
on the auto-open failure paths). The three overlay wire dataclasses
(``PaintOverlayEvent``, ``ClearOverlayEvent``, ``OverlayStateChangedEvent``)
are constructed by the integration layer from the data this machine
returns; they are not constructed here. The Input-side build-response
schemas and the ``ClickConfig`` overlay keys are SEPARATE, not-yet-shipped
beads; this module therefore models build-response / paint-ack / timeout
inputs with its OWN lightweight event representation (``OverlayEvent``),
not those schemas.

Concurrency model
-----------------
The state machine is NOT thread-safe. It is owned by the Logic Process
and must be driven from a single thread of execution -- the asyncio
event loop that serialises the speech-pipeline handlers, the
EventBus mic-pause / mic-resume handlers, and the focus-change /
destroy ``SetWinEventHook`` callbacks (which marshal onto the loop via
``loop.call_soon_threadsafe`` before calling ``apply``). Any consumer
that needs to read the machine from another thread must marshal onto the
same loop. Adding a lock would be the wrong fix: the contract requires
single-writer ordering through the existing loop, not lock-based
serialisation. This mirrors the editor-lifecycle concurrency note.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

from services.wheelhouse.shared.click_notice import ClickNoticeEvent


class OverlayState(enum.Enum):
    """Lifecycle states for the numbered overlay (v4 "### States")."""

    CLOSED = "closed"
    WALK_IN_FLIGHT = "walk_in_flight"
    PAINT_IN_FLIGHT = "paint_in_flight"
    PAINTED = "painted"
    REFRESH_IN_FLIGHT = "refresh_in_flight"
    PAUSED = "paused"
    ERROR = "error"


class OverlayEventKind(enum.Enum):
    """Inbound event kinds for the overlay state machine.

    Twelve kinds total. The six "command / signal" kinds (``show_numbers``,
    ``hide_numbers``, ``click_n``, ``mic_pause``, ``mic_resume``,
    ``focus_change``) plus the two extra events (``auto_open``,
    ``focused_hwnd_destroyed``) carry no generation. The three
    generation-bearing kinds (``build_response``, ``paint_ack``,
    ``timeout``) are checked against the active
    ``(overlay_session_id, paint_generation)`` pair BEFORE the transition
    table is consulted. ``click_complete`` carries no generation (it is an
    Input-side click result; in ``painted`` it triggers a post-click
    refresh).
    """

    SHOW_NUMBERS = "show_numbers"
    HIDE_NUMBERS = "hide_numbers"
    CLICK_N = "click_n"
    MIC_PAUSE = "mic_pause"
    MIC_RESUME = "mic_resume"
    FOCUS_CHANGE = "focus_change"
    BUILD_RESPONSE = "build_response"
    PAINT_ACK = "paint_ack"
    CLICK_COMPLETE = "click_complete"
    AUTO_OPEN = "auto_open"
    FOCUSED_HWND_DESTROYED = "focused_hwnd_destroyed"
    TIMEOUT = "timeout"


# Generation-bearing event kinds: their (overlay_session_id,
# paint_generation) is matched against the active pair before the table.
_GENERATION_BEARING: frozenset[OverlayEventKind] = frozenset(
    {
        OverlayEventKind.BUILD_RESPONSE,
        OverlayEventKind.PAINT_ACK,
        OverlayEventKind.TIMEOUT,
    }
)


class PaintAckState(enum.Enum):
    """The ``state`` carried by a ``paint_ack`` event.

    Mirrors the closed ``_ALLOWED_STATE`` set on
    ``OverlayStateChangedEvent`` (``painted`` / ``failed`` / ``cleared``).
    A ``CLEARED`` ack is bookkeeping only -- it never drives a state
    change, because hide-numbers already transitioned the machine to
    ``closed`` at dispatch time (r2.4).
    """

    PAINTED = "painted"
    FAILED = "failed"
    CLEARED = "cleared"


@dataclass(frozen=True)
class OverlayEvent:
    """A single inbound event for the overlay state machine.

    Lightweight, machine-local event representation (NOT the IPC wire
    schema). Carries only the primitives the machine needs to decide a
    transition.

    Fields:
      kind: which event this is.
      overlay_session_id / paint_generation: the generation pair the
        event was produced for. Meaningful only for the generation-bearing
        kinds (``build_response`` / ``paint_ack`` / ``timeout``); ignored
        for every other kind. An event whose pair does not equal the
        machine's active pair is rejected as ``STALE_GENERATION`` before
        the transition table is consulted.
      snapshot_id: the snapshot a ``build_response`` produced (so the
        integration can pin it); ``None`` otherwise.
      build_ok: whether a ``build_response`` reports a usable summary.
        ``False`` routes the same way the design's failure paths do
        (treated like a ``failed`` paint-ack: error -> closed, fire any
        pending notice).
      paint_state: the ``PaintAckState`` carried by a ``paint_ack``;
        ``None`` for other kinds.
      snapshot_valid: for ``mic_resume`` from ``paused`` -- ``True`` when
        the cached snapshot still passes the full foreground-identity +
        TTL check (restore), ``False`` when it is stale / the HWND is gone
        (re-walk). Ignored for other kinds.
      notice: for ``auto_open`` -- the suppressed ``ClickNoticeEvent`` to
        stash in ``pending_ambiguous_notice``; ``None`` for other kinds.
      reason: optional human-readable reason carried into a terminal
        recovery state; empty for normal events.
    """

    kind: OverlayEventKind
    overlay_session_id: int = 0
    paint_generation: int = 0
    snapshot_id: Optional[str] = None
    build_ok: bool = True
    paint_state: Optional[PaintAckState] = None
    snapshot_valid: bool = False
    notice: Optional[ClickNoticeEvent] = None
    reason: str = ""


# Sentinel for "this state has no timeout", mirroring editor_lifecycle's
# _NO_TIMEOUT. Float infinity reads naturally as "no scheduled transition
# out of this state".
_NO_TIMEOUT = float("inf")


class EffectKind(enum.Enum):
    """The kinds of side effect the integration layer performs."""

    DISPATCH_BUILD = "dispatch_build"          # start_overlay_walk / show_numbered_overlay
    DISPATCH_PAINT = "dispatch_paint"          # paint_overlay (GUI)
    DISPATCH_CLEAR = "dispatch_clear"          # clear_overlay (GUI)
    PIN_SNAPSHOT = "pin_snapshot"              # pin a snapshot in the Input store
    UNPIN_SNAPSHOT = "unpin_snapshot"          # clear a snapshot pin
    FIRE_NOTICE = "fire_notice"                # surface a ClickNoticeEvent (notice=None -> generic standalone-failure notice)
    ARM_TIMER = "arm_timer"                    # start a per-state timeout timer
    CANCEL_TIMER = "cancel_timer"              # cancel the current timeout timer


# Why a build request is being dispatched (carried on DISPATCH_BUILD).
class BuildReason(enum.Enum):
    """The reason a build request is dispatched (DISPATCH_BUILD effect)."""

    SHOW_NUMBERS = "show_numbers"      # standalone "show numbers" -> start_overlay_walk
    AUTO_OPEN = "auto_open"            # ambiguous click -> show_numbered_overlay (reuse snapshot)
    REFRESH = "refresh"                # post-click / focus-change / re-said "show numbers"
    SUPERSEDE = "supersede"            # a newer trigger superseded an in-flight build
    RESUME_REWALK = "resume_rewalk"    # mic-resume with a stale snapshot -> fresh walk


@dataclass(frozen=True)
class Effect:
    """One side effect the integration layer must perform, as data.

    Not every field is meaningful for every ``kind``; the unused ones stay
    at their defaults. The class never performs the effect -- it only
    describes it. Effects are returned in the order the integration must
    apply them.

    Fields:
      kind: which effect.
      overlay_session_id / paint_generation: the generation this effect is
        stamped with (for build / paint / clear / pin / arm_timer).
      snapshot_id: the snapshot to pin / unpin (PIN_SNAPSHOT /
        UNPIN_SNAPSHOT). For DISPATCH_PAINT the integration pairs the paint
        with the snapshot it just pinned; carried here for symmetry.
      build_reason: why a build is dispatched (DISPATCH_BUILD only).
      notice: the ClickNoticeEvent to surface (FIRE_NOTICE only). A FIRE_NOTICE
        whose ``notice`` is ``None`` is the marker for the generic standalone
        "numbers couldn't be drawn" notice: the integration constructs the
        text, since the pure machine holds no ClickNoticeEvent for a standalone
        walk failure (wh-n29v.16.1).
      timer_state: the state whose timeout this ARM_TIMER guards.
      duration_ms: the timeout duration for ARM_TIMER.
      immediate_clear: set on a DISPATCH_PAINT to mean "the integration
        must NOT present this painted snapshot as a visible frame; it nets
        hidden" (the auto-hide-while-paused paint, v4 mic-pause case 2). The
        matching DISPATCH_CLEAR at the same generation ships on ONE of two
        paths, depending on which in-flight state resolved:
          * Walk path (``_resolve_in_flight_to_paused``): the DISPATCH_CLEAR
            is included INLINE in this same effect batch, right after the
            paint -- the build-response resolved straight to ``paused``.
          * Refresh path (``_refresh_build_ok`` while auto_hide): the paint
            is dispatched now but the DISPATCH_CLEAR ships on the SUBSEQUENT
            paint-ack at the same generation (``_paint_ack_to_paused``),
            because the refresh stays ``refresh_in_flight`` until the GUI
            acks the paint.
        In both cases the net visible result is "no overlay shown for this
        generation"; the flag does NOT promise that a clear immediately
        follows in the same batch. The integration must treat an
        ``immediate_clear`` paint as never-visible and rely on the matching
        same-generation clear (inline or on the next paint-ack) to confirm.
    """

    kind: EffectKind
    overlay_session_id: int = 0
    paint_generation: int = 0
    snapshot_id: Optional[str] = None
    build_reason: Optional[BuildReason] = None
    notice: Optional[ClickNoticeEvent] = None
    timer_state: Optional[OverlayState] = None
    duration_ms: float = 0.0
    immediate_clear: bool = False


class OverlayOutcome(enum.Enum):
    """Result of applying an event to the overlay state machine.

    ACCEPTED: the event drove a transition (state and/or generation
      change) and/or produced effects.
    NO_OP: a well-defined event that the design says does nothing in this
      state (e.g. ``mic_resume`` outside ``paused``, ``show_numbers`` in
      ``closed``-adjacent no-op cells, a ``cleared`` paint-ack that is pure
      bookkeeping). No state change, no effects.
    STALE_GENERATION: a generation-bearing event whose
      ``(overlay_session_id, paint_generation)`` does not equal the active
      pair. Rejected with NO state change, evaluated BEFORE the table.
    HELD: a "click N" that arrived during a transition (``walk_in_flight``
      / ``paint_in_flight`` / ``paused``). The integration applies the
      "queue or drop" hold (up to 200 ms) and re-reads the machine when
      the hold fires. No state change here.
    INVALID_TRANSITION: the ``(state, kind)`` pair is not admitted. The
      machine fails closed to ``error`` (with a synthetic ``reason``) where
      the design routes invalid combos to error; ``apply`` never raises on
      a well-typed call.
    """

    ACCEPTED = "accepted"
    NO_OP = "no_op"
    STALE_GENERATION = "stale_generation"
    HELD = "held"
    INVALID_TRANSITION = "invalid_transition"


@dataclass(frozen=True)
class ApplyResult:
    """The (outcome, ordered effects) pair returned by ``apply``."""

    outcome: OverlayOutcome
    effects: tuple[Effect, ...] = ()


@dataclass
class ClickOverlayStateMachine:
    """Logic-side overlay toggle state machine.

    See the module docstring for the contract. State fields:

      state: the current ``OverlayState``.
      overlay_session_id: monotonic; a fresh id is allocated each time the
        machine leaves ``closed`` (start walk / auto_open).
      paint_generation: monotonic per session, starts at 0; bumped on the
        initial paint and on every refresh / restart / supersede.
      pinned_snapshot_id: the snapshot currently pinned for this session
        (the "exactly one pinned snapshot per session" invariant), or
        ``None``.
      prior_pinned_snapshot_id / _prior_pin_deferred: bookkeeping for a
        refresh in flight. ``_refresh_build_ok`` pins the new snapshot and
        records the prior (still-visible) snapshot here, DEFERRING its
        unpin until the refresh paint succeeds. A refresh failure restores
        the prior snapshot and unpins the new one (Finding 1).
      pending_ambiguous_notice: the suppressed auto-open notice, or
        ``None``. Set on ``auto_open``; fired-and-cleared on the auto-open
        failure paths; cleared unconditionally on entry to ``closed``.
      auto_hide_in_flight: set when the mic pauses during an in-flight
        state; resolves the next build / paint to ``paused`` (kept hidden).
        Cleared on reaching ``paused`` and on entry to ``closed``.
      reason: synthetic reason recorded on entry to ``error``; cleared by
        ``_reset``.

    The constructor takes the two timeout durations as plain ints (the
    integration reads them from ``ClickConfig``; this class does NOT read
    config). ``walk_deadline_ms`` guards ``walk_in_flight`` and
    ``refresh_in_flight``; ``paint_deadline_ms`` guards
    ``paint_in_flight``.
    """

    walk_deadline_ms: int = 2500
    paint_deadline_ms: int = 1000

    state: OverlayState = OverlayState.CLOSED
    overlay_session_id: int = 0
    paint_generation: int = 0
    pinned_snapshot_id: Optional[str] = None
    # During a refresh, ``_refresh_build_ok`` pins the NEW snapshot but
    # DEFERS unpinning the prior (still-visible) one until the refresh's
    # paint succeeds. This field holds that prior id while the new paint is
    # outstanding, so a refresh FAILURE (failed paint-ack or timeout after a
    # successful build) can restore ``pinned_snapshot_id`` to the prior
    # snapshot and unpin only the new failed one -- preserving the
    # "pinned == visible" invariant (Finding 1). ``None`` means no refresh
    # is mid-flight with a deferred prior (a fresh refresh, a failed
    # build-response that never installed a new snapshot, or a non-refresh
    # state). A sentinel object distinguishes "no deferral" from "the prior
    # snapshot id is legitimately None".
    prior_pinned_snapshot_id: Optional[str] = None
    _prior_pin_deferred: bool = field(default=False, repr=False)
    pending_ambiguous_notice: Optional[ClickNoticeEvent] = None
    auto_hide_in_flight: bool = False
    reason: str = ""

    # The session id last handed out; the next session uses _next_session_id
    # + 1. Starts at 0 so the first allocated session is 1 (0 is reserved as
    # the "no session yet" sentinel while CLOSED).
    _next_session_id: int = field(default=0, repr=False)

    # ------------------------------------------------------------------
    # Pin contract self-audit (wh-pin-snapshot-contract-break-detection).
    # ------------------------------------------------------------------
    # The machine is the single producer of PIN/UNPIN effects, so it can
    # audit its own stream: ``_outstanding_pins`` mirrors every pin it has
    # emitted and not yet unpinned. The invariant is at most ONE outstanding
    # pin, EXCEPT exactly two during the legitimate deferred-refresh window
    # (``_refresh_build_ok`` pinned the new snapshot and deferred the
    # prior's unpin -- ``_prior_pin_deferred`` is the marker). A pin that
    # violates that (a lost unpin, a racing double-pin, or pins spanning
    # more than one refresh generation) records a description in
    # ``_pin_contract_break`` for the integration layer to consume and log.
    # A store-level pinned>1 warning was removed in 5a7abd66 because it
    # cried wolf on every normal refresh; THIS check sits where the
    # deferred-generation bookkeeping lives, so the legitimate two-pin
    # window is recognised. Deliberately NOT cleared in ``_enter_closed``:
    # the set self-maintains through emitted UNPIN effects, so a teardown
    # path that forgets an unpin leaves the stale id here and the next
    # session's first pin flags it -- exactly the leak this exists to catch.
    _outstanding_pins: set = field(default_factory=set, repr=False)
    _pin_contract_break: Optional[str] = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Timeout table (data; mirrors editor_lifecycle's STATE_TIMEOUTS_S).
    # ------------------------------------------------------------------
    def timeout_ms(self, state: OverlayState) -> float:
        """Return the per-state timeout in ms, or ``_NO_TIMEOUT``.

        Built from the constructor durations so a different config yields a
        different table without touching this method. ``closed`` /
        ``painted`` / ``paused`` / ``error`` have no timeout.
        """

        table: dict[OverlayState, float] = {
            OverlayState.CLOSED: _NO_TIMEOUT,
            OverlayState.WALK_IN_FLIGHT: float(self.walk_deadline_ms),
            OverlayState.PAINT_IN_FLIGHT: float(self.paint_deadline_ms),
            OverlayState.PAINTED: _NO_TIMEOUT,
            OverlayState.REFRESH_IN_FLIGHT: float(self.walk_deadline_ms),
            OverlayState.PAUSED: _NO_TIMEOUT,
            OverlayState.ERROR: _NO_TIMEOUT,
        }
        return table[state]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def prior_pin_deferred(self) -> bool:
        """Public read accessor for the deferred-prior-unpin flag (wh-n29v.20.1).

        The integration layer (``LogicController.forward_click_element``) needs
        this flag to ask ``route_click_n`` which snapshot is visible during
        ``refresh_in_flight`` -- the prior snapshot when a refresh build already
        pinned a NEW not-yet-painted snapshot and deferred the prior's unpin,
        else the current pin. The backing field carries a leading underscore
        for dataclass-repr hygiene (it is ``repr=False``), but the flag is part
        of this machine's documented read API for that resolver, alongside the
        public ``pinned_snapshot_id`` / ``prior_pinned_snapshot_id``. Exposing
        it as a property keeps callers off the private field, so a future
        rename or restructure of the backing field breaks HERE, co-located with
        the field, instead of silently at the call site.
        """

        return self._prior_pin_deferred

    def consume_pin_contract_break(self) -> Optional[str]:
        """Return and clear the pending pin contract-break description.

        wh-pin-snapshot-contract-break-detection: the machine is pure (no
        logging), so a detected over-pin records a description here and the
        integration layer (``LogicController._perform_overlay_effects``)
        consumes it after each apply and logs the warning. ``None`` means no
        break since the last consume. Consuming clears the slot so one break
        is surfaced exactly once.
        """

        msg = self._pin_contract_break
        self._pin_contract_break = None
        return msg

    def apply(self, event: OverlayEvent) -> ApplyResult:
        """Apply ``event`` and return ``(outcome, ordered effects)``.

        1. For a generation-bearing event, reject any event whose
           ``(overlay_session_id, paint_generation)`` does not equal the
           active pair as ``STALE_GENERATION`` with NO state change, BEFORE
           consulting the table.
        2. Dispatch to the per-state handler for ``(state, kind)``.
        3. A ``(state, kind)`` the design does not admit returns
           ``INVALID_TRANSITION`` (and fails closed to ``error`` where the
           design routes invalid combos to error). Never raises on a
           well-typed call.
        """

        if event.kind in _GENERATION_BEARING:
            if (
                event.overlay_session_id != self.overlay_session_id
                or event.paint_generation != self.paint_generation
            ):
                return ApplyResult(OverlayOutcome.STALE_GENERATION)

        handler = _DISPATCH.get(self.state)
        if handler is None:  # pragma: no cover - all states are in _DISPATCH
            return self._invalid(event)
        return handler(self, event)

    def reset_to_closed(self) -> tuple[Effect, ...]:
        """Reset the machine to ``closed`` and return the unpin effects.

        Mirrors ``LogicMirror.reset_to_closed``. Called by the integration
        recovery paths (after an ``error``, after the destroy-while-paused
        teardown, etc.) so the next session starts clean. Keeps the
        allocated ``_next_session_id`` so the next session gets a fresh,
        strictly-larger id. A freshly-constructed machine already starts in
        ``closed``, so startup does not need to call this.

        Returns, in order, a ``DISPATCH_CLEAR`` (only when a snapshot is
        pinned, i.e. an overlay may still be on screen -- wh-n29v.15.1)
        followed by the ``UNPIN_SNAPSHOT`` effect(s) for whatever was pinned at
        the moment of the reset (the current pin and any deferred refresh
        prior), so a snapshot orphaned by an error entered via the fail-closed
        ``_invalid`` path is not leaked until TTL (Finding 2) and any overlay
        the GUI painted before the error does not linger. Returns an empty
        tuple when nothing was pinned (an already-clean machine emits no
        spurious clear). The integration must dispatch the returned effects,
        exactly as it does for the effects returned by ``apply``.
        """

        effects: list[Effect] = []
        clear = self._clear_if_visible()
        if clear is not None:
            effects.append(clear)
        effects.extend(self._unpin_all_pinned())
        self._enter_closed()
        return tuple(effects)

    # ------------------------------------------------------------------
    # Internal: state entry helpers
    # ------------------------------------------------------------------
    def _enter_closed(self) -> None:
        """Set fields for the ``closed`` state.

        Clears ``pending_ambiguous_notice`` and ``auto_hide_in_flight``
        unconditionally (so neither leaks across sessions), drops the pin
        bookkeeping, and clears ``reason``. The ``overlay_session_id`` /
        ``paint_generation`` are left as-is; the next leave-from-closed
        allocates a fresh session id and resets the generation to 0.
        """

        self.state = OverlayState.CLOSED
        self.pinned_snapshot_id = None
        self.prior_pinned_snapshot_id = None
        self._prior_pin_deferred = False
        self.pending_ambiguous_notice = None
        self.auto_hide_in_flight = False
        self.reason = ""

    def _start_session(self) -> None:
        """Allocate a fresh session id and reset the generation to 0.

        Called on every leave-from-``closed`` that starts a build
        (``show_numbers`` / ``auto_open``). ``paint_generation`` starts at 0
        for the new session.
        """

        self._next_session_id += 1
        self.overlay_session_id = self._next_session_id
        self.paint_generation = 0

    def _bump_generation(self) -> None:
        """Bump ``paint_generation`` for a new walk/paint cycle in-session.

        Used by every refresh / restart / supersede that dispatches a new
        build or paint. The session id is unchanged.
        """

        self.paint_generation += 1

    # ------------------------------------------------------------------
    # Internal: effect builders
    # ------------------------------------------------------------------
    def _arm_timer(self, state: OverlayState) -> Effect:
        return Effect(
            kind=EffectKind.ARM_TIMER,
            overlay_session_id=self.overlay_session_id,
            paint_generation=self.paint_generation,
            timer_state=state,
            duration_ms=self.timeout_ms(state),
        )

    def _cancel_timer(self) -> Effect:
        return Effect(kind=EffectKind.CANCEL_TIMER)

    def _dispatch_build(
        self, reason: BuildReason, snapshot_id: Optional[str] = None
    ) -> Effect:
        # ``snapshot_id`` is meaningful only for ``BuildReason.AUTO_OPEN``, where
        # the integration re-paints an EXISTING click snapshot via
        # ``show_numbered_overlay``. AUTO_OPEN fires from CLOSED before the
        # machine has pinned anything, so the reuse id cannot be read from
        # ``self.pinned_snapshot_id`` at dispatch time -- it must travel ON the
        # effect (wh-n29v.96.1). Every other reason is a fresh walk
        # (``start_overlay_walk``) and leaves this ``None``.
        return Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=self.overlay_session_id,
            paint_generation=self.paint_generation,
            build_reason=reason,
            snapshot_id=snapshot_id,
        )

    def _dispatch_paint(
        self, snapshot_id: Optional[str], *, immediate_clear: bool = False
    ) -> Effect:
        return Effect(
            kind=EffectKind.DISPATCH_PAINT,
            overlay_session_id=self.overlay_session_id,
            paint_generation=self.paint_generation,
            snapshot_id=snapshot_id,
            immediate_clear=immediate_clear,
        )

    def _dispatch_clear(self) -> Effect:
        return Effect(
            kind=EffectKind.DISPATCH_CLEAR,
            overlay_session_id=self.overlay_session_id,
            paint_generation=self.paint_generation,
        )

    def _clear_if_visible(self) -> Optional[Effect]:
        """Return a DISPATCH_CLEAR when an overlay MAY be on screen, else None.

        A snapshot is pinned (or a refresh deferred a prior pin) exactly when a
        paint was dispatched for this session and not yet superseded, so the
        pin is the machine's proxy for "an overlay may be visible." On a
        teardown to ``closed`` from a NON-painted-preserving path (an in-flight
        timeout / failure, an ``error`` recovery, or ``reset_to_closed``) this
        emits the clear so a paint the GUI rendered just before Logic moved on
        cannot leave orphaned badges on screen (wh-n29v.15.1). Returns ``None``
        when nothing is pinned, so the fresh-walk failure paths (no paint was
        ever dispatched) emit no spurious clear and ``reset_to_closed`` keeps
        its empty-tuple contract for an already-clean machine. The refresh
        non-destructive fall-backs do NOT route here -- they keep the previous
        valid overlay visible by design, so clearing there would be wrong.
        """

        if self.pinned_snapshot_id is not None or self._prior_pin_deferred:
            return self._dispatch_clear()
        return None

    def _pin(self, snapshot_id: Optional[str]) -> Effect:
        if snapshot_id is not None:
            projected = self._outstanding_pins | {snapshot_id}
            legitimate = len(projected) <= 1 or (
                len(projected) == 2
                and self._prior_pin_deferred
                and projected == {self.prior_pinned_snapshot_id, snapshot_id}
            )
            if not legitimate:
                self._pin_contract_break = (
                    f"pin of {snapshot_id!r} while outstanding="
                    f"{sorted(self._outstanding_pins)!r} "
                    f"(prior_pin_deferred={self._prior_pin_deferred}, "
                    f"prior={self.prior_pinned_snapshot_id!r}, "
                    f"session={self.overlay_session_id}, "
                    f"generation={self.paint_generation})"
                )
                # Reconcile to the authoritative bookkeeping (reviewer_0
                # finding .1.1): without this, one leaked id re-flags a
                # warning on EVERY subsequent pin -- roughly every 15s over
                # a browser window via the wh-n29v.121 proactive refresh --
                # and the later messages stamp the wrong session/generation.
                # After this pin, a correct stream's outstanding set is
                # exactly {snapshot_id} plus the deferred prior (every
                # legitimate pin site either unpinned the old id first or
                # set the deferred-prior marker), so resetting to that keeps
                # detection armed for the NEXT genuine break while flagging
                # each leak once.
                self._outstanding_pins = {snapshot_id}
                if (
                    self._prior_pin_deferred
                    and self.prior_pinned_snapshot_id is not None
                ):
                    self._outstanding_pins.add(self.prior_pinned_snapshot_id)
            else:
                self._outstanding_pins.add(snapshot_id)
        return Effect(
            kind=EffectKind.PIN_SNAPSHOT,
            overlay_session_id=self.overlay_session_id,
            paint_generation=self.paint_generation,
            snapshot_id=snapshot_id,
        )

    def _unpin_current(self) -> Optional[Effect]:
        """Return an UNPIN_SNAPSHOT effect for the pinned snapshot, if any.

        Returns ``None`` when nothing is pinned, so callers can skip a
        no-op unpin. Does NOT clear ``pinned_snapshot_id`` itself -- the
        caller decides what to set it to next (typically a new pin or
        ``None``).
        """

        return self._unpin_id(self.pinned_snapshot_id)

    def _unpin_id(self, snapshot_id: Optional[str]) -> Optional[Effect]:
        """Return an UNPIN_SNAPSHOT effect for ``snapshot_id``, if not None.

        Returns ``None`` when ``snapshot_id`` is ``None`` so callers can
        skip a no-op unpin.
        """

        if snapshot_id is None:
            return None
        self._outstanding_pins.discard(snapshot_id)
        return Effect(
            kind=EffectKind.UNPIN_SNAPSHOT,
            overlay_session_id=self.overlay_session_id,
            snapshot_id=snapshot_id,
        )

    def _fire_notice(self, notice: ClickNoticeEvent) -> Effect:
        return Effect(kind=EffectKind.FIRE_NOTICE, notice=notice)

    def _fire_standalone_failure_notice(self) -> Effect:
        """Signal the generic standalone "numbers couldn't be drawn" notice.

        A standalone "show numbers" whose walk fails has NO source
        ``ClickNoticeEvent`` (unlike the auto-open path, which suppressed a real
        one and stashed it in ``pending_ambiguous_notice``). The v4 spec (line
        278) still requires user feedback for this case. This pure class does
        not fabricate ``ClickNoticeEvent`` text, so it returns a ``FIRE_NOTICE``
        effect with ``notice=None``: the integration layer recognises a
        ``FIRE_NOTICE`` whose ``notice`` is ``None`` as the request to build and
        show the generic standalone walk-failure notice (wh-n29v.16.1).
        """

        return Effect(kind=EffectKind.FIRE_NOTICE, notice=None)

    # ------------------------------------------------------------------
    # Internal: composite transitions
    # ------------------------------------------------------------------
    def _invalid(self, event: OverlayEvent) -> ApplyResult:
        """Fail closed to ``error`` for an unadmitted ``(state, kind)``.

        Sets a synthetic ``reason`` (``invalid_transition_from_<state>_via_
        <kind>``) and moves to ``error``. ``pinned_snapshot_id`` (and any
        deferred refresh prior) is deliberately left POPULATED through
        ``error`` so the recovery path -- ``reset_to_closed`` or an
        ``_on_error`` recovery branch -- can emit the matching UNPIN and not
        orphan the snapshot until TTL (Finding 2). The integration recovers
        before the next session. Never raises.

        Emits ``CANCEL_TIMER`` (wh-n29v.98.2): when ``_invalid`` is reached
        from an in-flight state (``walk_in_flight`` / ``paint_in_flight`` /
        ``refresh_in_flight``) the per-state timeout timer is still armed, and
        entering ``error`` does NOT bump the generation, so a later timer fire
        would pass the generation gate and be consumed as a wasted ``error``
        NO_OP. Cancelling it here closes that gap and keeps ``_invalid``
        consistent with every other transition away from an in-flight state.
        ``CANCEL_TIMER`` is a no-op at the integration when no timer is armed
        (``_invalid`` from ``closed`` / ``painted`` / ``paused`` / ``error``),
        so it is safe to emit unconditionally, matching ``_hide_to_closed``.
        """

        self.reason = (
            f"invalid_transition_from_{self.state.value}_via_{event.kind.value}"
        )
        self.state = OverlayState.ERROR
        return ApplyResult(
            OverlayOutcome.INVALID_TRANSITION, (self._cancel_timer(),)
        )

    def _hide_to_closed(self) -> ApplyResult:
        """Dispatch clear + unpin and transition straight to ``closed``.

        The hide-numbers immediate-close path (r2.4): dispatch
        ``clear_overlay``, unpin the pinned snapshot, and set ``closed``
        WITHOUT waiting for the ``cleared`` ack. A later ``cleared``
        paint-ack is bookkeeping only (NO_OP). Cancels the timer too, in
        case an in-flight state was hidden.
        """

        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]
        effects.extend(self._unpin_all_pinned())
        self._enter_closed()
        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))

    def _unpin_all_pinned(self) -> list[Effect]:
        """Return UNPIN effects for the current pin AND any deferred prior.

        On teardown from ``refresh_in_flight`` after a successful build, BOTH
        the new (``pinned_snapshot_id``) and the deferred prior
        (``prior_pinned_snapshot_id``) are pinned in the Input store. Unpin
        both so neither leaks until TTL (Finding 1 / Finding 2). In every
        other state only the current pin (if any) is returned.
        """

        effects: list[Effect] = []
        unpin = self._unpin_current()
        if unpin is not None:
            effects.append(unpin)
        if self._prior_pin_deferred:
            prior = self._unpin_id(self.prior_pinned_snapshot_id)
            if prior is not None:
                effects.append(prior)
        return effects

    def _error_to_closed(self, *, emit_standalone_notice: bool) -> ApplyResult:
        """Run an in-flight-failure ``error -> closed`` recovery.

        Fires exactly ONE failure notice when one is warranted:

          * If an auto-open is in flight (``pending_ambiguous_notice`` set),
            fire that suppressed ambiguous-click notice (the auto-open
            delayed-fallback, r2.9). This takes priority -- the user gets the
            specific notice, not the generic one.
          * Otherwise, if ``emit_standalone_notice`` is set (a standalone
            "show numbers" WALK failure -- build-failed or walk timeout), signal
            the generic "numbers couldn't be drawn" notice via
            ``_fire_standalone_failure_notice`` (a ``FIRE_NOTICE`` with
            ``notice=None``; v4 line 278, wh-n29v.16.1). The paint-phase failure
            callers pass ``emit_standalone_notice=False`` because the spec (line
            279) fires only the pending notice for a paint timeout, not the
            standalone one.

        Then clears any possibly-visible overlay (wh-n29v.15.1), unpins any
        pinned snapshot, and enters ``closed`` (which clears the pending notice
        and the auto-hide flag). The machine passes through ``error``
        conceptually; the design immediately recovers to ``closed`` so the
        resting state is ``closed``.
        """

        effects: list[Effect] = [self._cancel_timer()]
        clear = self._clear_if_visible()
        if clear is not None:
            # Defend against an orphaned overlay: a paint dispatched for this
            # generation that the GUI rendered just before the timeout / failure
            # would otherwise linger on screen after the machine reaches closed
            # (wh-n29v.15.1). Only fires when a snapshot is pinned, so the
            # walk_in_flight failure paths (nothing painted) stay clear-free.
            effects.append(clear)
        if self.pending_ambiguous_notice is not None:
            effects.append(self._fire_notice(self.pending_ambiguous_notice))
        elif emit_standalone_notice:
            effects.append(self._fire_standalone_failure_notice())
        effects.extend(self._unpin_all_pinned())
        self._enter_closed()
        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))

    def _restart_walk(self, reason: BuildReason) -> ApplyResult:
        """Restart/supersede: bump gen, unpin old, dispatch a new walk.

        Used by the ``walk_in_flight`` / ``paint_in_flight`` restart and
        supersede cells. Cancels the stale timer, unpins the
        previously-pinned snapshot before the new build (so a stale timer
        cannot abort a valid new walk and no pin leaks), bumps the
        generation, dispatches the new build, and arms a fresh
        ``walk_in_flight`` timer. Stays in / moves to ``walk_in_flight``.
        """

        effects: list[Effect] = [self._cancel_timer()]
        unpin = self._unpin_current()
        if unpin is not None:
            effects.append(unpin)
        self.pinned_snapshot_id = None
        self._bump_generation()
        self.state = OverlayState.WALK_IN_FLIGHT
        effects.append(self._dispatch_build(reason))
        effects.append(self._arm_timer(OverlayState.WALK_IN_FLIGHT))
        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))

    def _refresh(self, reason: BuildReason) -> ApplyResult:
        """Painted -> refresh_in_flight: bump gen, dispatch a new walk.

        The previous paint stays visible (and its snapshot stays pinned)
        until the new one replaces it, so this does NOT unpin here. Cancels
        any timer, bumps the generation, dispatches the build, and arms a
        ``refresh_in_flight`` timer.
        """

        effects: list[Effect] = [self._cancel_timer()]
        self._bump_generation()
        self.state = OverlayState.REFRESH_IN_FLIGHT
        effects.append(self._dispatch_build(reason))
        effects.append(self._arm_timer(OverlayState.REFRESH_IN_FLIGHT))
        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))

    def _refresh_supersede(self, reason: BuildReason) -> ApplyResult:
        """refresh_in_flight self-transition: bump gen, supersede walk.

        Cancels the stale timer, bumps the generation, dispatches the new
        build, arms a fresh timer. The previous VISIBLE snapshot stays
        pinned until the next paint installs its replacement. If a prior
        refresh build had already succeeded (a deferred prior is recorded
        and ``pinned_snapshot_id`` holds the not-yet-painted new snapshot),
        that new snapshot is now abandoned: unpin it and restore the
        deferred prior as the sole pinned (still-visible) snapshot, so the
        next build-ok defers cleanly against the truly-visible overlay
        (Finding 1 -- keeps "pinned == visible" across rapid supersede).
        """

        effects: list[Effect] = [self._cancel_timer()]
        if self._prior_pin_deferred:
            abandoned_new = self._unpin_id(self.pinned_snapshot_id)
            if abandoned_new is not None:
                effects.append(abandoned_new)
            self.pinned_snapshot_id = self.prior_pinned_snapshot_id
            self.prior_pinned_snapshot_id = None
            self._prior_pin_deferred = False
        self._bump_generation()
        # state stays REFRESH_IN_FLIGHT
        effects.append(self._dispatch_build(reason))
        effects.append(self._arm_timer(OverlayState.REFRESH_IN_FLIGHT))
        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))

    # ------------------------------------------------------------------
    # Internal: per-state handlers
    # ------------------------------------------------------------------
    def _on_closed(self, event: OverlayEvent) -> ApplyResult:
        kind = event.kind
        if kind is OverlayEventKind.SHOW_NUMBERS:
            # closed -> walk_in_flight (start a fresh walk).
            self._start_session()
            self.state = OverlayState.WALK_IN_FLIGHT
            return ApplyResult(
                OverlayOutcome.ACCEPTED,
                (
                    self._dispatch_build(BuildReason.SHOW_NUMBERS),
                    self._arm_timer(OverlayState.WALK_IN_FLIGHT),
                ),
            )
        if kind is OverlayEventKind.AUTO_OPEN:
            # closed -> walk_in_flight; stash the suppressed notice and
            # dispatch show_numbered_overlay (reuse the click snapshot). The
            # reuse snapshot id rides on the event (event.snapshot_id) and is
            # stamped onto the DISPATCH_BUILD effect: AUTO_OPEN fires from CLOSED
            # before the machine has pinned anything, so the integration cannot
            # recover the reuse target from the (None) live pin and must read it
            # from the effect (wh-n29v.96.1).
            self._start_session()
            self.state = OverlayState.WALK_IN_FLIGHT
            self.pending_ambiguous_notice = event.notice
            return ApplyResult(
                OverlayOutcome.ACCEPTED,
                (
                    self._dispatch_build(
                        BuildReason.AUTO_OPEN, snapshot_id=event.snapshot_id
                    ),
                    self._arm_timer(OverlayState.WALK_IN_FLIGHT),
                ),
            )
        if kind is OverlayEventKind.HIDE_NUMBERS:
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.CLICK_N:
            # By-name: routed to click_element by the integration. No state
            # change here.
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.MIC_PAUSE:
            # "record" -- no overlay to hide; nothing to do in the machine.
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.MIC_RESUME:
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.FOCUS_CHANGE:
            # record-only: there is no overlay to follow.
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind in (
            OverlayEventKind.PAINT_ACK,
            OverlayEventKind.BUILD_RESPONSE,
            OverlayEventKind.TIMEOUT,
            OverlayEventKind.CLICK_COMPLETE,
        ):
            # A build_response / paint_ack / timeout / click_complete landing in
            # closed is a LATE completion of work the machine itself dispatched
            # for a session the user already tore down. hide_numbers transitions
            # straight to closed WITHOUT bumping the generation (r2.4), so a late
            # generation-bearing event (build_response / paint_ack / timeout) at
            # the still-current pair PASSES the pre-table generation gate and
            # reaches here; click_complete carries no generation but is likewise a
            # late Input-side click result. None of these is a GUI protocol
            # violation -- each is the ack of a build / paint / timer / click the
            # machine started before hide-numbers closed it. Consume each as a
            # teardown NO_OP so a 'show -> hide -> late <event>' sequence does NOT
            # return INVALID_TRANSITION and does NOT strand the overlay path in
            # error (wh-n29v.19.1, criterion 3; same class as the closed
            # paint_ack NO_OP and the error-state generation-bearing NO_OP
            # wh-n29v.70.3). FOCUSED_HWND_DESTROYED is excluded below: it is not a
            # late ack of dispatched work and remains a genuine protocol
            # violation in closed.
            return ApplyResult(OverlayOutcome.NO_OP)
        # focused_hwnd_destroyed is a genuine protocol violation in closed (the
        # transient destroy hook is live only while paused).
        return self._invalid(event)

    def _on_walk_in_flight(self, event: OverlayEvent) -> ApplyResult:
        kind = event.kind
        if kind is OverlayEventKind.BUILD_RESPONSE:
            if not event.build_ok:
                # Build failed -> treat like the in-flight failure: fire the
                # pending notice (auto-open) or the generic standalone notice
                # (standalone "show numbers"), error -> closed (wh-n29v.16.1).
                return self._error_to_closed(emit_standalone_notice=True)
            if self.auto_hide_in_flight:
                # Mic paused mid-walk: pin, paint+immediate-clear (nets
                # hidden), clear flag, move to paused.
                return self._resolve_in_flight_to_paused(event.snapshot_id)
            # Normal: -> paint_in_flight, pin + dispatch paint.
            self.pinned_snapshot_id = event.snapshot_id
            self.state = OverlayState.PAINT_IN_FLIGHT
            return ApplyResult(
                OverlayOutcome.ACCEPTED,
                (
                    self._cancel_timer(),
                    self._pin(event.snapshot_id),
                    self._dispatch_paint(event.snapshot_id),
                    self._arm_timer(OverlayState.PAINT_IN_FLIGHT),
                ),
            )
        if kind is OverlayEventKind.SHOW_NUMBERS:
            return self._restart_walk(BuildReason.SHOW_NUMBERS)
        if kind is OverlayEventKind.FOCUS_CHANGE:
            return self._restart_walk(BuildReason.SUPERSEDE)
        if kind is OverlayEventKind.HIDE_NUMBERS:
            return self._hide_to_closed()
        if kind is OverlayEventKind.MIC_PAUSE:
            # Nothing painted yet; just arm the auto-hide flag. Stay
            # walk_in_flight.
            self.auto_hide_in_flight = True
            return ApplyResult(OverlayOutcome.ACCEPTED)
        if kind is OverlayEventKind.MIC_RESUME:
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.CLICK_N:
            return ApplyResult(OverlayOutcome.HELD)
        if kind is OverlayEventKind.TIMEOUT:
            # walk_in_flight timeout -> error -> closed: fire the pending
            # notice (auto-open) or the generic standalone notice (standalone
            # "show numbers"), unpin (wh-n29v.16.1, v4 line 278).
            return self._error_to_closed(emit_standalone_notice=True)
        # paint_ack / click_complete / auto_open / focused_hwnd_destroyed
        return self._invalid(event)

    def _on_paint_in_flight(self, event: OverlayEvent) -> ApplyResult:
        kind = event.kind
        if kind is OverlayEventKind.PAINT_ACK:
            if event.paint_state is PaintAckState.PAINTED:
                if self.auto_hide_in_flight:
                    return self._paint_ack_to_paused()
                # -> painted.
                self.state = OverlayState.PAINTED
                return ApplyResult(
                    OverlayOutcome.ACCEPTED, (self._cancel_timer(),)
                )
            if event.paint_state is PaintAckState.FAILED:
                # paint failed -> error -> closed; fire the pending notice only
                # (v4 line 279 fires no standalone notice for the paint phase).
                return self._error_to_closed(emit_standalone_notice=False)
            # CLEARED ack here is unexpected (no hide happened): bookkeeping.
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.SHOW_NUMBERS:
            return self._restart_walk(BuildReason.SHOW_NUMBERS)
        if kind is OverlayEventKind.FOCUS_CHANGE:
            return self._restart_walk(BuildReason.SUPERSEDE)
        if kind is OverlayEventKind.HIDE_NUMBERS:
            return self._hide_to_closed()
        if kind is OverlayEventKind.MIC_PAUSE:
            # Hide the (about-to-be-) overlay, set the flag, stay
            # paint_in_flight.
            self.auto_hide_in_flight = True
            return ApplyResult(
                OverlayOutcome.ACCEPTED, (self._dispatch_clear(),)
            )
        if kind is OverlayEventKind.MIC_RESUME:
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.CLICK_N:
            return ApplyResult(OverlayOutcome.HELD)
        if kind is OverlayEventKind.TIMEOUT:
            # paint timeout -> error -> closed; fire the pending notice only
            # (v4 line 279 fires no standalone notice for the paint phase).
            return self._error_to_closed(emit_standalone_notice=False)
        # build_response / click_complete / auto_open / focused_hwnd_destroyed
        return self._invalid(event)

    def _on_painted(self, event: OverlayEvent) -> ApplyResult:
        kind = event.kind
        if kind is OverlayEventKind.SHOW_NUMBERS:
            return self._refresh(BuildReason.REFRESH)
        if kind is OverlayEventKind.FOCUS_CHANGE:
            return self._refresh(BuildReason.REFRESH)
        if kind is OverlayEventKind.CLICK_COMPLETE:
            return self._refresh(BuildReason.REFRESH)
        if kind is OverlayEventKind.HIDE_NUMBERS:
            return self._hide_to_closed()
        if kind is OverlayEventKind.CLICK_N:
            # The integration dispatches click_snapshot_item; no state
            # change.
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.MIC_PAUSE:
            # painted -> paused directly. Hide the overlay; snapshot stays
            # pinned for fast resume.
            self.state = OverlayState.PAUSED
            self.auto_hide_in_flight = False
            return ApplyResult(
                OverlayOutcome.ACCEPTED, (self._dispatch_clear(),)
            )
        if kind is OverlayEventKind.MIC_RESUME:
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.PAINT_ACK:
            # A stale-but-current-gen painted ack in painted is a no-op
            # (the table marks paint-ack "invalid (stale)" -- a duplicate
            # current-gen painted ack changes nothing). A cleared ack is
            # bookkeeping. A failed ack here would be a protocol violation,
            # but painted has NO path to error (r2.12), so treat any
            # paint-ack as a NO_OP.
            return ApplyResult(OverlayOutcome.NO_OP)
        # build_response / timeout / auto_open / focused_hwnd_destroyed
        return self._invalid(event)

    def _on_refresh_in_flight(self, event: OverlayEvent) -> ApplyResult:
        kind = event.kind
        if kind is OverlayEventKind.BUILD_RESPONSE:
            if self._prior_pin_deferred:
                # A build_response already succeeded for THIS refresh
                # generation (``_refresh_build_ok`` pinned the new snapshot and
                # set the deferred-prior flag). A second build_response at the
                # same ``(overlay_session_id, paint_generation)`` is a duplicate
                # or replayed Input response: the pre-table generation gate
                # cannot catch it because the pair is still current, and
                # reprocessing it would overwrite ``prior_pinned_snapshot_id``
                # from the truly-visible prior snapshot to the first new
                # snapshot, orphaning the prior pin on the next paint-ack
                # (wh-n29v.15.2). Ignore it as a NO_OP -- the first build's
                # pin and paint stay in flight, so the deferred prior is
                # preserved and the next paint-ack unpins the correct snapshot.
                return ApplyResult(OverlayOutcome.NO_OP)
            if not event.build_ok:
                # A failed refresh build is non-destructive: keep the prior
                # valid overlay. Honour auto_hide_in_flight.
                return self._refresh_build_failed()
            # Refresh build-response: dispatch paint (+immediate clear if
            # auto_hide), pin new + unpin old, STAY refresh_in_flight (the
            # paint-ack drives the move to painted/paused).
            return self._refresh_build_ok(event.snapshot_id)
        if kind is OverlayEventKind.PAINT_ACK:
            if event.paint_state is PaintAckState.PAINTED:
                if self.auto_hide_in_flight:
                    return self._paint_ack_to_paused()
                # Refresh succeeded: the new snapshot is now visible. Ship
                # the deferred prior-snapshot unpin so the new one is the
                # sole pinned snapshot (Finding 1).
                effects: list[Effect] = [self._cancel_timer()]
                effects.extend(self._commit_refresh_prior_unpin())
                self.state = OverlayState.PAINTED
                return ApplyResult(
                    OverlayOutcome.ACCEPTED, tuple(effects)
                )
            if event.paint_state is PaintAckState.FAILED:
                # Non-destructive: keep the prior overlay (back to painted),
                # or to paused if auto_hide. Unpin only the failed new
                # snapshot; the prior pinned snapshot is restored.
                return self._refresh_paint_failed()
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.SHOW_NUMBERS:
            return self._refresh_supersede(BuildReason.SUPERSEDE)
        if kind is OverlayEventKind.FOCUS_CHANGE:
            return self._refresh_supersede(BuildReason.SUPERSEDE)
        if kind is OverlayEventKind.HIDE_NUMBERS:
            return self._hide_to_closed()
        if kind is OverlayEventKind.MIC_PAUSE:
            self.auto_hide_in_flight = True
            return ApplyResult(
                OverlayOutcome.ACCEPTED, (self._dispatch_clear(),)
            )
        if kind is OverlayEventKind.MIC_RESUME:
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.CLICK_N:
            # Resolve against the PREVIOUS still-valid summary; no state
            # change in the machine (the integration owns the resolve).
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.CLICK_COMPLETE:
            return ApplyResult(OverlayOutcome.HELD)
        if kind is OverlayEventKind.TIMEOUT:
            # Non-destructive timeout: keep prior badges -> painted, or
            # -> paused if auto_hide. Unpin only the failed new snapshot
            # (none is pinned mid-refresh under this machine's bookkeeping,
            # so nothing to unpin), prior pinned snapshot stays.
            return self._refresh_timeout()
        # auto_open / focused_hwnd_destroyed
        return self._invalid(event)

    def _on_paused(self, event: OverlayEvent) -> ApplyResult:
        kind = event.kind
        if kind is OverlayEventKind.MIC_RESUME:
            if event.snapshot_valid:
                # Restore: -> painted, re-emit a paint of the cached
                # snapshot.
                self.state = OverlayState.PAINTED
                return ApplyResult(
                    OverlayOutcome.ACCEPTED,
                    (self._dispatch_paint(self.pinned_snapshot_id),),
                )
            # Stale / HWND gone: unpin old, fresh walk -> walk_in_flight.
            effects: list[Effect] = []
            unpin = self._unpin_current()
            if unpin is not None:
                effects.append(unpin)
            self.pinned_snapshot_id = None
            self._bump_generation()
            self.state = OverlayState.WALK_IN_FLIGHT
            effects.append(self._dispatch_build(BuildReason.RESUME_REWALK))
            effects.append(self._arm_timer(OverlayState.WALK_IN_FLIGHT))
            return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))
        if kind is OverlayEventKind.SHOW_NUMBERS:
            # Post-resume restart -> walk_in_flight. Unpin the old snapshot
            # before the new build.
            effects2: list[Effect] = []
            unpin2 = self._unpin_current()
            if unpin2 is not None:
                effects2.append(unpin2)
            self.pinned_snapshot_id = None
            self._bump_generation()
            self.state = OverlayState.WALK_IN_FLIGHT
            effects2.append(self._dispatch_build(BuildReason.SHOW_NUMBERS))
            effects2.append(self._arm_timer(OverlayState.WALK_IN_FLIGHT))
            return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects2))
        if kind is OverlayEventKind.HIDE_NUMBERS:
            return self._hide_to_closed()
        if kind is OverlayEventKind.FOCUSED_HWND_DESTROYED:
            # paused -> closed: dispatch clear + unpin.
            return self._hide_to_closed()
        if kind is OverlayEventKind.MIC_PAUSE:
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.FOCUS_CHANGE:
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.CLICK_N:
            return ApplyResult(OverlayOutcome.HELD)
        if kind is OverlayEventKind.PAINT_ACK:
            # A generation-matching paint-ack landing in paused is the
            # acknowledgement of the hide that drove the machine here: entry to
            # paused dispatches a clear (_on_painted(MIC_PAUSE),
            # _paint_ack_to_paused) -- and the walk-in-flight->paused resolve
            # dispatches a paint+immediate-clear plus a clear
            # (_resolve_in_flight_to_paused) -- so the GUI emits painted /
            # cleared (or a failed immediate-clear paint) at the SAME
            # generation. It already passed the generation gate, so it belongs
            # to this paused session and is bookkeeping only: never a state
            # driver and never an error (mirrors the closed handler; wh-n29v.69.1).
            return ApplyResult(OverlayOutcome.NO_OP)
        # build_response / click_complete / auto_open are genuine protocol
        # violations in paused.
        return self._invalid(event)

    def _on_error(self, event: OverlayEvent) -> ApplyResult:
        kind = event.kind
        if kind is OverlayEventKind.SHOW_NUMBERS:
            # Fresh walk out of error -> walk_in_flight. Unpin whatever was
            # pinned at error-entry BEFORE the new build, so a snapshot
            # orphaned by an invalid-transition error is not leaked until TTL
            # (Finding 2).
            effects: list[Effect] = list(self._unpin_all_pinned())
            self._enter_closed()  # clears stale per-session state + pin
            self._start_session()
            self.state = OverlayState.WALK_IN_FLIGHT
            effects.append(self._dispatch_build(BuildReason.SHOW_NUMBERS))
            effects.append(self._arm_timer(OverlayState.WALK_IN_FLIGHT))
            return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))
        if kind in (
            OverlayEventKind.HIDE_NUMBERS,
            OverlayEventKind.MIC_PAUSE,
            OverlayEventKind.MIC_RESUME,
        ):
            # Recover to closed. Clear any overlay that may still be on screen
            # -- a paint dispatched before the error that the GUI rendered late
            # (wh-n29v.15.1) -- then unpin whatever was pinned at error-entry so
            # an orphaned snapshot is not leaked until TTL (Finding 2).
            effects2: list[Effect] = []
            clear = self._clear_if_visible()
            if clear is not None:
                effects2.append(clear)
            effects2.extend(self._unpin_all_pinned())
            self._enter_closed()
            return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects2))
        if kind is OverlayEventKind.CLICK_N:
            # Reject with a notice (the integration owns the notice text).
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind is OverlayEventKind.FOCUS_CHANGE:
            return ApplyResult(OverlayOutcome.NO_OP)
        if kind in _GENERATION_BEARING:
            # A build_response / paint_ack / timeout reaching error is, by the
            # gate above, generation-MATCHING: the generation is frozen on entry
            # to error (_invalid does not bump it), so any generation-bearing
            # event that gets here is a late completion of work the machine
            # dispatched at the still-current generation BEFORE it failed closed.
            # That is not a GUI protocol violation -- it is the ack of a
            # paint/build/timer the machine itself started. Consume it as
            # bookkeeping NO_OP exactly like the closed and paused handlers do,
            # so the diagnostic ``reason`` recording WHY the machine errored is
            # preserved instead of being overwritten by _invalid with a less
            # useful invalid_transition_from_error_via_<kind> string
            # (wh-n29v.70.3; same class as the paused paint-ack fix
            # wh-n29v.69.1). Recovery is driven only by the explicit
            # SHOW_NUMBERS / HIDE_NUMBERS / MIC_* branches above.
            return ApplyResult(OverlayOutcome.NO_OP)
        # click_complete / auto_open / focused_hwnd_destroyed are genuine
        # protocol violations in error: non-generation-bearing events the
        # machine did not dispatch and that cannot be a late in-flight ack.
        return self._invalid(event)

    # ------------------------------------------------------------------
    # Internal: auto-hide-while-paused resolution helpers
    # ------------------------------------------------------------------
    def _resolve_in_flight_to_paused(
        self, snapshot_id: Optional[str]
    ) -> ApplyResult:
        """walk_in_flight build-response while auto_hide -> paused, hidden.

        Pin the freshly built snapshot, paint + immediate clear at the same
        generation (nets invisible), clear ``auto_hide_in_flight``, move to
        ``paused``.
        """

        self.pinned_snapshot_id = snapshot_id
        self.auto_hide_in_flight = False
        self.state = OverlayState.PAUSED
        return ApplyResult(
            OverlayOutcome.ACCEPTED,
            (
                self._cancel_timer(),
                self._pin(snapshot_id),
                self._dispatch_paint(snapshot_id, immediate_clear=True),
                self._dispatch_clear(),
            ),
        )

    def _paint_ack_to_paused(self) -> ApplyResult:
        """paint_in_flight / refresh_in_flight painted ack while auto_hide.

        The snapshot was already pinned when the paint was dispatched (or
        in the refresh build-response). This is a refresh SUCCESS path too,
        so when a refresh deferred the prior-snapshot unpin
        (``_refresh_build_ok``), ship it now so the new snapshot is the sole
        pinned one (Finding 1). For the ``paint_in_flight`` caller nothing
        is deferred and the commit is a no-op. Keep the overlay hidden
        (dispatch clear), clear ``auto_hide_in_flight``, move to ``paused``.
        """

        effects: list[Effect] = [self._cancel_timer()]
        effects.extend(self._commit_refresh_prior_unpin())
        effects.append(self._dispatch_clear())
        self.auto_hide_in_flight = False
        self.state = OverlayState.PAUSED
        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))

    # ------------------------------------------------------------------
    # Internal: refresh build/paint resolution helpers
    # ------------------------------------------------------------------
    def _refresh_build_ok(self, snapshot_id: Optional[str]) -> ApplyResult:
        """refresh_in_flight build-response (ok): pin new, DEFER prior unpin.

        Pin the new snapshot and dispatch the paint (+immediate clear if
        auto_hide), but DEFER unpinning the prior (still-visible) snapshot
        until the refresh paint succeeds. The prior id is recorded in
        ``prior_pinned_snapshot_id`` / ``_prior_pin_deferred`` so that a
        refresh FAILURE (failed paint-ack or timeout) can restore the prior
        snapshot and unpin only the new failed one, keeping the visible
        overlay pinned (Finding 1). STAY in ``refresh_in_flight``; the
        paint-ack drives the move to painted / paused, and the prior unpin
        ships on that successful paint-ack. The timer keeps running for the
        paint leg under the ``walk_deadline_ms`` budget the design assigns
        refresh_in_flight.
        """

        # Record the prior (still-visible) snapshot for deferred unpin.
        # ``_refresh_supersede`` reconciles any earlier deferred prior before
        # the next build-response, and ``_on_refresh_in_flight`` drops a
        # duplicate build_response while a prior is already deferred
        # (wh-n29v.15.2), so this runs at most ONCE per refresh generation and
        # at most one prior is ever deferred when it does (the truly-visible
        # overlay).
        effects: list[Effect] = []
        self.prior_pinned_snapshot_id = self.pinned_snapshot_id
        self._prior_pin_deferred = True
        self.pinned_snapshot_id = snapshot_id
        effects.append(self._pin(snapshot_id))
        effects.append(
            self._dispatch_paint(
                snapshot_id, immediate_clear=self.auto_hide_in_flight
            )
        )
        # state stays REFRESH_IN_FLIGHT
        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))

    def _commit_refresh_prior_unpin(self) -> list[Effect]:
        """Ship the deferred prior-snapshot unpin on a refresh SUCCESS.

        Returns the UNPIN(prior) effect (if any) and clears the deferred
        bookkeeping. Called from both successful refresh paint-ack paths
        (refresh -> painted and refresh+auto_hide -> paused), so the new
        snapshot becomes the sole pinned one once it is actually visible.
        """

        effects: list[Effect] = []
        if self._prior_pin_deferred:
            unpin = self._unpin_id(self.prior_pinned_snapshot_id)
            if unpin is not None:
                effects.append(unpin)
        self.prior_pinned_snapshot_id = None
        self._prior_pin_deferred = False
        return effects

    def _refresh_build_failed(self) -> ApplyResult:
        """refresh_in_flight build-response (failed): non-destructive.

        Keep the prior valid overlay. The build never returned a new
        snapshot, so nothing new was pinned and there is no deferred prior
        to reconcile; the original ``pinned_snapshot_id`` (the visible
        overlay) stays untouched. If ``auto_hide_in_flight`` is set, go to
        ``paused`` (kept hidden); otherwise back to ``painted``.
        """

        return self._refresh_fall_back()

    def _refresh_paint_failed(self) -> ApplyResult:
        """refresh_in_flight paint-ack (failed): non-destructive (Finding 1).

        The build succeeded (``_refresh_build_ok`` pinned the new snapshot
        and deferred the prior unpin), but the paint failed. Restore the
        prior (still-visible) snapshot as the pinned one and unpin the new
        FAILED snapshot, so the visible overlay stays correctly pinned and a
        later mic-resume restores the right snapshot. Fall back to painted /
        paused. This is the inverse of the previous (buggy) behaviour that
        left the failed snapshot pinned and the visible one unpinned.
        """

        return self._refresh_fall_back()

    def _refresh_timeout(self) -> ApplyResult:
        """refresh_in_flight timeout: non-destructive fall-back (Finding 1).

        If the build had already succeeded (deferred prior present), restore
        the prior snapshot and unpin the new (never-painted) one, exactly
        like a failed paint-ack. If the build was still outstanding (no
        deferred prior), the visible overlay's pin is untouched.
        """

        return self._refresh_fall_back()

    def _refresh_fall_back(self) -> ApplyResult:
        """Shared non-destructive refresh fall-back to painted / paused.

        Every refresh failure (failed build-response, failed paint-ack, or
        the refresh timeout) is non-destructive by design -- the existing
        usable overlay is never torn down. When the build had already
        succeeded (``_prior_pin_deferred`` set), the NEW snapshot is the one
        currently in ``pinned_snapshot_id``; that new snapshot failed, so
        unpin it and RESTORE ``pinned_snapshot_id`` to the prior visible
        snapshot, preserving the "pinned == visible" invariant (Finding 1).
        When no build succeeded (no deferred prior), the pin is already the
        visible overlay and is left untouched. If ``auto_hide_in_flight`` is
        set, move to ``paused`` (kept hidden) and clear the flag; otherwise
        move back to ``painted``. Cancels the refresh timer.
        """

        effects: list[Effect] = [self._cancel_timer()]
        if self._prior_pin_deferred:
            # The new snapshot failed: unpin it, restore the prior visible
            # one as the sole pinned snapshot.
            failed_new = self._unpin_id(self.pinned_snapshot_id)
            if failed_new is not None:
                effects.append(failed_new)
            self.pinned_snapshot_id = self.prior_pinned_snapshot_id
            self.prior_pinned_snapshot_id = None
            self._prior_pin_deferred = False
        if self.auto_hide_in_flight:
            self.auto_hide_in_flight = False
            self.state = OverlayState.PAUSED
        else:
            self.state = OverlayState.PAINTED
        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))


# Dispatch table: state -> per-state handler. Built after the class so the
# methods exist. apply() looks up the current state here.
_DISPATCH = {
    OverlayState.CLOSED: ClickOverlayStateMachine._on_closed,
    OverlayState.WALK_IN_FLIGHT: ClickOverlayStateMachine._on_walk_in_flight,
    OverlayState.PAINT_IN_FLIGHT: ClickOverlayStateMachine._on_paint_in_flight,
    OverlayState.PAINTED: ClickOverlayStateMachine._on_painted,
    OverlayState.REFRESH_IN_FLIGHT: ClickOverlayStateMachine._on_refresh_in_flight,
    OverlayState.PAUSED: ClickOverlayStateMachine._on_paused,
    OverlayState.ERROR: ClickOverlayStateMachine._on_error,
}
