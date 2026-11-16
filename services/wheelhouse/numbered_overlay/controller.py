"""NumberedOverlayController: the numbered overlay and voice clicking.

wh-overlay-refactor-pilot.2 moved this code out of LogicController in
services/wheelhouse/main.py without a change in behavior. The Phase A boundary
tests (tests/test_overlay_requirements_*.py, docs/design/overlay-requirements.md)
pin that behavior. LogicController creates one controller as self.overlay and
calls in through the public methods; this controller reads the LogicController
members it does not own through self._host (see host.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from services.wheelhouse.click_snapshot_summary_cache import (
    ClickSnapshotSummaryCache,
)
from services.wheelhouse.numbered_overlay.helpers import (
    OverlayFocusHookManager,
    _OVERLAY_CLEAR_ACK_DEADLINE_MS,
    _OVERLAY_CLICK_SHOW_COUNT_KEEP,
    _OVERLAY_LEASE_FLOOR_MS,
    _OVERLAY_LEASE_INTERVAL_TICKS,
    _OVERLAY_NON_WINDOW_CHANGE_CLASSES,
    _OVERLAY_READ_SENTENCE_WAIT_MAX_S,
    _OVERLAY_RENUMBER_GRACE_SECONDS,
    _OVERLAY_RESTORE_ACK_DEADLINE_MS,
    _OVERLAY_RESTORE_LOCK_WAIT_REQUESTS,
    _OVERLAY_RESTORE_SCHEDULE_BUDGET_MAX_MS,
    _OVERLAY_REWALK_REFUSAL_REASONS,
    _click_response_notice_kwargs,
    _forward_click_response_notice,
    _handle_late_click_response,
    _overlay_current_foreground_hwnd,
    _overlay_focus_logger,
    _overlay_keepalive_states,
    _overlay_window_facts,
    make_click_overlay_state_machine,
)
from services.wheelhouse.ui.browser_dom_corrections import (
    effective_browser_processes,
)

if TYPE_CHECKING:
    from services.wheelhouse.click_first_use_hint import FirstUseHintTracker
    from services.wheelhouse.click_overlay_state import OverlayState
    from services.wheelhouse.numbered_overlay.host import OverlayHost
    from services.wheelhouse.overlay_focus_hooks import (
        FocusChangeDebouncer,
        ForegroundIdentity,
    )

logger = logging.getLogger(__name__)

# wh-overlay-refresh-after-mouse: the numbers are read again this long after
# the last mouse click or wheel step, so a series of clicks or wheel steps
# gives one refresh. Half a second covers the gap between the steps of one
# wheel turn and the time most applications take to redraw after a click,
# and it is twice the default overlay_focus_debounce_ms, so the window-change
# rules (RW-5, RW-12) normally settle a click into another window first. The
# same time is the window after a WheelHouse number click in which a
# program-sent click is taken as that number click's own (RW-39).
_OVERLAY_POINTER_SETTLE_S = 0.5


class NumberedOverlayController:
    """The numbered overlay and voice clicking for one Logic process."""

    def __init__(
        self, host: "OverlayHost", *, clock: Callable[[], float]
    ) -> None:
        """Set up the overlay state.

        ``host`` is the LogicController. ``clock`` returns seconds; the area
        reads time only through it (main.py passes ``time.monotonic``).
        """

        self._host = host
        self._clock = clock
        self.click_snapshot_summary_cache = ClickSnapshotSummaryCache(
            ttl_seconds=float(self._host.click_config.snapshot_ttl_seconds),
        )
        # wh-overlay-slow-uia-stale-badges.20.4: the trace_id of the LAST
        # click notice forwarded to the GUI (set inside
        # forward_click_notice). The late-answer correction consults it so
        # a correction never replaces a newer click's notice.
        self._last_click_notice_trace_id: Optional[str] = None
        # wh-overlay-slow-uia-stale-badges.20.10: the trace_id of the last
        # click-notice ATTEMPT (recorded at forward_click_notice entry,
        # successful or not). The late-answer correction forwards when the
        # timed-out click matches EITHER id, so a timeout notice dropped by
        # a full GUI queue does not suppress the click's only feedback.
        self._last_click_notice_attempt_trace_id: Optional[str] = None
        # wh-n29v.17: Logic-side toggle state machine for the Phase 1.5
        # numbered overlay. ``make_click_overlay_state_machine`` derives
        # walk_deadline_ms and settle_deadline_ms from ``[click]
        # screen_read_timeout_ms`` (wh-overlay-slow-uia-stale-badges.3);
        # paint_deadline_ms keeps its class default (1000ms). The factory
        # also reads overlay_settle_after_click
        # (wh-overlay-slow-uia-stale-badges.1), which ships off. This
        # machine is the single routing source of truth for "show numbers" /
        # "hide numbers" and numeric "click N" -- see forward_click_element
        # and handle_overlay_command. The effect-PERFORMING integration (real
        # IPC dispatch of build/paint/clear, pin/unpin, the 200ms hold timer,
        # the per-state timeout timers, the real click_snapshot_item dispatch)
        # is owned by the not-yet-shipped integration bead wh-h9a8v2; this
        # slice hands every collected effect / decision to a documented stub
        # seam (_perform_overlay_effects / _dispatch_snapshot_item_click /
        # _hold_click_n).
        self.click_overlay_state = make_click_overlay_state_machine(
            self._host.click_config
        )
        # wh-n29v.95: effect-performing overlay integration state.
        #
        # ``_overlay_effect_lock`` serializes whole effect batches so the async
        # effect dispatch (build/paint/clear/pin/unpin/notice/timer) ships in
        # machine-return order even when ``_perform_overlay_effects`` is invoked
        # concurrently from multiple in-flight asyncio tasks (the
        # overlay_state_changed acks, the build-response feed, the timer feed).
        # ``machine.apply`` commits the state transition synchronously BEFORE
        # the performer runs, so machine state stays consistent; this lock
        # guarantees dispatch ORDERING -- a not-yet-completed clear from one ack
        # is never reordered against a paint/clear from a later ack
        # (wh-n29v.70.2). asyncio.Lock is FIFO, so batches dispatch in the order
        # they were scheduled on this single Logic loop.
        #
        # ``_overlay_timer`` is the single live per-state timeout handle (exactly
        # one timer is armed at a time -- the machine emits CANCEL_TIMER before a
        # new ARM_TIMER where needed); ``_overlay_timer_pair`` is the
        # (overlay_session_id, paint_generation) the timer was armed for, fed
        # back as the TIMEOUT event's pair on fire so the machine's generation
        # gate drops a timer for a superseded generation.
        # ``_overlay_hold_timer`` is the single live 200ms "click N" hold handle.
        self._overlay_effect_lock = asyncio.Lock()
        self._overlay_timer: "asyncio.TimerHandle | None" = None
        self._overlay_timer_pair: "tuple[int, int] | None" = None
        # The OverlayState the live per-state timer guards (WALK_IN_FLIGHT /
        # PAINT_IN_FLIGHT / REFRESH_IN_FLIGHT / POST_CLICK_SETTLING), or None
        # when no timer is armed.
        # Tracked so a regression test can confirm a build-response feed does not
        # leave a stale WALK timer armed after the machine has moved to
        # paint_in_flight (wh-n29v.96.4).
        self._overlay_armed_timer_state: "OverlayState | None" = None
        # wh-overlay-timer-cancel-race: a monotonically increasing id, one per
        # arm. The callback captures the value ITS arm was given, so a callback
        # whose timer has since been replaced can tell that it is no longer the
        # live arm. Neither the pair nor the armed state can show that: a
        # replacement arm can carry both of them unchanged.
        self._overlay_timer_arm_id: int = 0
        self._overlay_hold_timer: "asyncio.TimerHandle | None" = None
        # wh-overlay-slow-uia-stale-badges.8: what the armed hold timer would
        # click, as ((overlay_session_id, paint_generation), spoken_text), or
        # None when no hold is armed. Recorded so a badge click that succeeds
        # at the same pair can cancel the held click instead of letting it
        # fire against the list the success just invalidated. Lives NEXT TO
        # _overlay_hold_timer, not inside the FSM: the hold is integration
        # transport state (a timer), and the machine stays pure.
        self._overlay_hold_armed: (
            "tuple[tuple[int, int], str] | None"
        ) = None
        # wh-overlay-slow-uia-stale-badges.8: identity of the one badge click
        # currently awaiting its Input reply, as (snapshot_id, item_id,
        # trace_id), or None. While set, _dispatch_snapshot_item_click refuses
        # a second click (reason overlay_click_in_flight) instead of racing it
        # against the same painted list. Integration transport state, like
        # MouseGridController._grid_session_transition -- NOT an FSM state, because the machine
        # models what the user sees (the badges stay painted while the click
        # is in flight) and routing must keep resolving numbers to give the
        # refusal a spoken-number context.
        self._overlay_click_in_flight: (
            "tuple[str, str, str] | None"
        ) = None
        # wh-overlay-slow-uia-stale-badges.8: the (overlay_session_id,
        # paint_generation) pair of the list the user can SEE, i.e. the last
        # PAINTED pair. This is what a resolved badge click carries to Input.
        # It differs from the machine's current pair during REFRESH_IN_FLIGHT:
        # the machine has already bumped the generation for the refresh while
        # the user still clicks the visible prior list -- carrying the current
        # pair would consume the NEW generation and wrongly refuse the new
        # list's first click. Maintained by
        # _reconcile_overlay_visible_painted_pair from machine state.
        self._overlay_visible_painted_pair: "tuple[int, int] | None" = None
        # wh-overlay-slow-uia-stale-badges.16.1: the pinned snapshot id the
        # recorded pair belongs to. A FAILED refresh falls back to PAINTED
        # with a bumped generation but the SAME still-visible list (the
        # fall-back restores the prior pin, not the prior generation);
        # recording the bumped pair there would re-admit the consumed list
        # under a new generation and defeat Input's executed-click watermark.
        # The reconcile compares the machine's pin against this memo to tell
        # a restored list (keep the pair) from a fresh paint (record).
        self._overlay_visible_painted_pin: "str | None" = None
        # wh-n29v.96.2: periodic keepalive timer for a quiescent overlay whose
        # snapshot is still retained. The keepalive states are steady NO_TIMEOUT
        # states with no recurring pin/paint, so without a periodic re-put a
        # retained snapshot idle past the TTL would age out and 'click N' would
        # misreport 'no badge N'. The timer re-puts the machine's currently-
        # pinned summary every ``_overlay_keepalive_interval_s`` and reschedules
        # itself; it is armed on entry to a keepalive state and cancelled on
        # leaving. ``_overlay_keepalive_states`` holds the list and states the
        # contract -- a retained pin, NOT necessarily visible badges
        # (wh-overlay-slow-uia-stale-badges.13.6).
        self._overlay_keepalive_timer: "asyncio.TimerHandle | None" = None
        self._overlay_keepalive_interval_s: float = max(
            float(self._host.click_config.snapshot_ttl_seconds) / 2.0, 1.0
        )
        # wh-overlay-slow-uia-stale-badges.9: the clear-ack watchdog. The
        # (overlay_session_id, paint_generation) pair of the newest
        # dispatched clear that no ack has confirmed yet, its deadline
        # timer, and the last pair a clear fault was already reported for
        # (repeat faults at the same pair downgrade to WARNING so one
        # broken pair cannot storm error popups). Integration transport
        # state, not FSM state: the machine's r2.4 immediate-close on hide
        # is deliberate; the watchdog only audits the IPC delivery.
        self._overlay_pending_clear: "tuple[int, int] | None" = None
        self._overlay_pending_clear_timer: "asyncio.TimerHandle | None" = None
        self._overlay_clear_fault_reported_pair: "tuple[int, int] | None" = None
        # wh-overlay-slow-uia-stale-badges.21.3: the restore-paint delivery
        # audit. The paused MIC_RESUME restore commits PAINTED synchronously
        # and emits one paint with no ack-expecting state behind it, so a lost
        # paint would leave Logic resolving "click N" against a list the user
        # cannot see. These hold the pair of that paint while it is
        # unconfirmed, plus its deadline timer. Integration transport state,
        # like the clear watchdog above.
        self._overlay_pending_restore_paint: "tuple[int, int] | None" = None
        self._overlay_pending_restore_paint_timer: (
            "asyncio.TimerHandle | None"
        ) = None
        # wh-overlay-slow-uia-stale-badges.21.8: the DELIVERY ENTITLEMENT of
        # that audited restore paint -- the pair of the PAINTED state it was
        # scheduled in. The ordinary paint fence reads only the pair and the
        # pin, and the exits that supersede a restore at the SAME pair and the
        # SAME pin (a second mic pause, the post-click settling entry) leave
        # both unchanged, so the fence alone cannot refuse the superseded
        # paint. This slot is recorded when the restore is scheduled and
        # retired the moment the machine leaves that PAINTED state.
        self._overlay_restore_paint_entitlement: "tuple[int, int] | None" = None
        # wh-n29v.121: proactive refresh for a painted overlay over a browser
        # window. Dynamic Chromium pages shift layout while the user reads, so
        # cached badge positions go stale with no focus change, menu event, or
        # click to trigger the reactive re-walks. Each keepalive tick checks:
        # PAINTED + tracked window in the effective browser-process set + last
        # PAINTED entry older than the trust window -> feed one FOCUS_CHANGE
        # (a REFRESH in PAINTED). 0 disables. The cadence is quantized to the
        # keepalive tick, so the real window is this value rounded up to the
        # next tick (documented on the config key).
        self._overlay_browser_refresh_seconds: float = float(
            self._host.click_config.overlay_browser_refresh_seconds
        )
        # Lowercased + de-duplicated by the helper -- the same effective list
        # ElementFinder uses for DOM corrections, so "browser" means the same
        # thing on both sides of the IPC boundary.
        self._overlay_browser_process_set: frozenset[str] = frozenset(
            effective_browser_processes(
                list(self._host.click_config.browser_processes),
                list(self._host.click_config.browser_processes_extend),
            )
        )
        # Monotonic timestamp of the LATEST transition into PAINTED (stamped
        # in _apply_overlay_event), so the refresh measures age from the most
        # recent paint, not the first. None until the first paint.
        self._overlay_last_paint_monotonic: "float | None" = None
        # wh-overlay-fixqueue-review.1: multiplier on the proactive-refresh
        # trust window. A FAILED proactive refresh restores the prior
        # snapshot by re-entering PAINTED (re-stamping the paint age), so
        # without this a window whose walk consistently failed would be
        # re-walked every window forever, stalling the Input process's
        # serial command loop each time. Doubles on each failed proactive
        # refresh (capped in _apply_overlay_event), resets on a successful
        # one and on session close.
        self._overlay_browser_refresh_backoff: int = 1
        # True only while _maybe_overlay_browser_refresh is inside its
        # apply, so the REFRESH_IN_FLIGHT entry edge can attribute the
        # refresh to the proactive trigger (vs the focus/menu hooks).
        self._overlay_in_proactive_apply: bool = False
        self._overlay_refresh_started_proactive: bool = False
        self._overlay_refresh_entry_pin: "str | None" = None
        # wh-overlay-fixqueue-review.2: (prior_snapshot_id, swap_monotonic)
        # recorded when a repaint swaps in a new snapshot; consumed by the
        # renumber guard in forward_click_element so a "click N" spoken
        # against the pre-swap badges is not silently resolved against the
        # renumbered overlay. wh-overlay-slow-uia-stale-badges.4 widened the
        # arming from the proactive browser refresh alone to every repaint
        # that changes the pin, on every application.
        #
        # wh-overlay-slow-uia-stale-badges.4.2.1: a LIST, oldest first, with
        # one entry per swap still inside its own grace window. Repaints can
        # chain inside one window (a menu opens, then closes), and the list
        # the user read is any one of them, so a single slot has to guess
        # which -- and either guess is wrong for somebody. Empty means no
        # guard is armed.
        self._overlay_repaint_swaps: "list[tuple[str | None, float]]" = []
        # wh-overlay-slow-uia-stale-badges.4: the snapshot that was the
        # session's list when the repaint now under way STARTED. Set on the
        # way into REFRESH_IN_FLIGHT and POST_CLICK_SETTLING; the PAINTED edge
        # in _apply_overlay_event compares the newly painted pin against it to
        # tell a swap (arm the guard) from a restore of the same list (arm
        # nothing), then clears it. None while no repaint is in flight.
        self._overlay_repaint_entry_pin: "str | None" = None
        # wh-n29v.21: Logic-side Win32 focus hooks for the numbered overlay.
        # The FOREGROUND hook feeds debounced FOCUS_CHANGE events into the
        # machine; the TRANSIENT DESTROY hook (registered only while paused,
        # scoped to the tracked window's pid/tid) feeds FOCUSED_HWND_DESTROYED.
        # The pure decision logic (debounce window, generation supersession,
        # full-identity comparison, raw-event -> OverlayEvent mapping) lives in
        # overlay_focus_hooks.py; the raw SetWinEventHook seam is
        # OverlayFocusHookManager above. The hook thread is started in main()
        # only when voice clicking is enabled, and stopped in shutdown(). The
        # debounce interval comes from the VALIDATED overlay config value
        # (self.click_config.overlay_focus_debounce_ms, range [0, 5000], default
        # 250) so the Logic and Input processes derive the SAME debounce from
        # the same raw [click] block (wh-n29v.66 Phase 1.5 parity invariant).
        self._overlay_focus_debouncer = self._build_overlay_focus_debouncer()
        # wh-numbers-stay-setting: True once a FOREGROUND event was coalesced
        # by the debounce, whatever window it named, while numbers are on the
        # screen and overlay_keep_numbers is off
        # (``_overlay_numbers_can_be_dismissed``). The settle refire (or a
        # menu / Pattern Manager event that fires at once) then runs
        # ``_foreground_change_dismisses_numbers`` against the window that is
        # in front at that moment and sends ``dismisses_numbers`` from the
        # result; clearing the flag happens there, and cancelling the refire
        # clears it too.
        self._overlay_settle_refire_foreground: bool = False
        # Pending trailing settle re-fire timer (wh-overlay-nested-dupes.1.1):
        # a coalesced focus/menu event arms this one-shot loop timer for the
        # debounce-window remainder so the FINAL event of a burst always
        # produces one re-walk. None when no settle is pending.
        self._overlay_settle_handle = None
        # wh-overlay-refresh-after-mouse: the mouse refresh. The pynput hook
        # thread reads ``_overlay_pointer_input_wanted`` (True while the
        # machine is in a state where a click or wheel step can lead to a
        # refresh, kept by ``_reconcile_overlay_pointer_refresh``) and posts
        # each event to ``_on_overlay_pointer_input``. The quiet-time timer
        # ``_overlay_pointer_refresh_handle`` is separate from the focus
        # debounce. ``_overlay_number_click_quiet_handle`` is pending for
        # _OVERLAY_POINTER_SETTLE_S after a WheelHouse number click ends.
        self._overlay_pointer_input_wanted: bool = False
        self._overlay_pointer_refresh_handle = None
        self._overlay_number_click_quiet_handle = None
        # The hook manager is created lazily in main() (it spawns a thread and
        # creates a Win32 window); None until started, and on a non-Windows or
        # disabled run it stays None.
        self._overlay_focus_hooks: "OverlayFocusHookManager | None" = None
        # Identity of the foreground window the overlay was built for. The
        # resume-time full-identity check and the transient destroy hook both
        # read this. It is NOT set by this slice (wh-n29v.21): the
        # effect-performing overlay integration (wh-h9a8v2) must capture it via
        # ``_capture_overlay_foreground_identity()`` at the moment the overlay
        # becomes visible/paused (when it pins the snapshot) and clear it to
        # ``None`` on entry to ``closed``. Until that integration lands the
        # field stays ``None``, so the destroy hook stays unregistered and the
        # resume check returns ``False`` (re-walk) -- the safe defaults. This
        # cross-slice dependency is tracked as an acceptance criterion on
        # wh-h9a8v2.
        self._overlay_tracked_identity: "ForegroundIdentity | None" = None
        # wh-overlay-rewalk-after-filter.1.3: the window an IN-FLIGHT build is
        # being made for. ``_overlay_tracked_identity`` above names the latest
        # PIN, and the pin happens only after the build response comes back, so
        # during a build that field is stale (a cross-window refresh) or None
        # (the first session after a start) and cannot say which window a
        # Pattern Manager tree change belongs to. Sampled twice: when a batch
        # holding a build is handed over (``_perform_overlay_effects``, before
        # the task is scheduled, wh-overlay-rewalk-after-filter.1.4) and again
        # when the build runs (``_overlay_dispatch_build``). Three paths end
        # it: the pin consumes it (``_overlay_send_pin``), entry to closed
        # clears it (``_reconcile_overlay_tracked_identity``), and the next
        # dispatch replaces it.
        self._overlay_pending_build_identity: "ForegroundIdentity | None" = None
        # wh-overlay-rewalk-after-filter: the newest Pattern Manager
        # tree-change event already applied -- its window handle and its
        # sequence number. The dialog's counter is monotonic per dialog
        # instance, so an event at or below the stored sequence FOR THE SAME
        # HANDLE arrived out of order behind a newer one and is dropped. Both
        # start at 0: no window tracked, no sequence seen.
        self._pm_tree_change_last_hwnd: int = 0
        self._pm_tree_change_last_sequence: int = 0
        # wh-overlay-snapshot-keepalive (trigger B): the foreground-window
        # identity each pinned snapshot was built for, keyed by snapshot_id.
        # Unlike the single ``_overlay_tracked_identity`` (which always names the
        # LATEST pin), this remembers per snapshot, so during a focus-change
        # refresh -- where the new window's snapshot is already pinned but the
        # PRIOR (still-visible) snapshot belongs to the window that lost focus --
        # routing can tell the visible snapshot's window from the current
        # foreground and HOLD a "click N" instead of dispatching a click Input
        # would reject. Populated at the pin point, dropped at unpin, and cleared
        # wholesale on entry to ``closed``.
        self._overlay_snapshot_window_identity: "dict[str, ForegroundIdentity]" = {}
        # wh-overlay-slow-uia-stale-badges.2.2.1: the window a SETTLE reply
        # said it was READ from, held as (snapshot_id, identity) between the
        # commit fence that verified it and the PIN_SNAPSHOT effect that
        # brands the snapshot with it. One slot, because one build is in
        # flight at a time (the same reason ``_overlay_build_abort`` is one
        # slot). Consumed by the pin it names, and dropped on entry to closed.
        self._overlay_settle_read_identity: (
            "tuple[str, ForegroundIdentity] | None"
        ) = None
        # Whether the transient destroy hook is currently registered, so the
        # paused-enter/leave reconciliation does not double-register or
        # double-unregister.
        self._overlay_destroy_hook_active: bool = False
        # wh-n29v.101.1: a successful 'click N' whose ok reply arrives while the
        # overlay is REFRESH_IN_FLIGHT cannot refresh immediately -- the machine
        # returns HELD for CLICK_COMPLETE in that state, and the pair captured at
        # click-dispatch already names the in-flight refresh generation. Record
        # that pair here so the post-click refresh is REPLAYED once the same
        # generation settles back into PAINTED, instead of being dropped (which
        # would leave the in-flight generation painting a pre-click snapshot with
        # no later refresh, so the next 'click N' resolves against a stale
        # snapshot -- the exact wrong-target risk this slice exists to prevent).
        # ``_reconcile_overlay_pending_postclick_refresh`` consumes it on entry to
        # PAINTED at the matching pair and clears it on supersede (the live pair
        # moved past the recorded one) or session end (entry to closed), so a
        # stale pending refresh can never fire on a newer overlay. ``None`` means
        # no post-click refresh is pending.
        self._overlay_pending_postclick_refresh: "tuple[int, int] | None" = None
        # wh-overlay-slow-uia-stale-badges.15: the SIBLING of the field above,
        # for the other interleave. A FOCUS_CHANGE from any of the four sources
        # can apply ``_refresh`` -- which BUMPS the generation -- between a badge
        # click's dispatch and its Input reply. The reply then fails gate 1 of
        # ``_feed_click_complete_refresh`` (mismatched pair) and, with
        # ``settle_after_click`` ON, the post-click clear never happens; if that
        # refresh later FAILS, ``_refresh_fall_back`` puts the PRE-CLICK badges
        # back on screen over a screen the click already changed.
        #
        # This field records ``(overlay_session_id, dispatch ordinal,
        # pre-click snapshot id)`` so the clear is DEFERRED rather than
        # dropped.
        # ``_reconcile_overlay_pending_postclick_clear`` resolves it the moment
        # the overlay next reaches PAINTED in the SAME session, by snapshot
        # identity (David's ruling via boss 2026-08-27, "Option C"):
        #
        #   pinned_snapshot_id == the recorded pre-click id
        #       -> the refresh fell back and the stale pre-click badges are on
        #          screen -> replay CLICK_COMPLETE so the clear happens.
        #   pinned_snapshot_id != the recorded pre-click id
        #       -> a genuinely new snapshot is painted -> DROP; clearing good,
        #          current badges would be strictly worse than doing nothing.
        #
        # The DECISION is keyed on the SESSION, not on a generation, unlike
        # the field above. Chained same-session supersedes keep restoring the
        # pre-click id into ``pinned_snapshot_id`` (``_refresh_supersede``
        # and ``_refresh_fall_back`` both do), so a generation key would drop
        # the deferred clear on the second refresh and re-open the hole. A
        # NEW overlay session, ``closed`` and ``error`` all drop it.
        #
        # The dispatch ordinal is carried for ONE purpose, the OVERWRITE
        # rule (wh-overlay-slow-uia-stale-badges.23.1). Two clicks can have
        # replies outstanding at once: ``_overlay_click_in_flight`` is
        # cleared in ``_send_snapshot_item_click``'s finally, which runs on a
        # TIMEOUT too, so a second click is allowed while the first click's
        # late-response callback is still armed (``_LATE_RESPONSE_GRACE_S``,
        # 15 s). The late path feeds the FIRST click's dispatch pair and its
        # dispatch-time snapshot id, so an OLDER defer can land after a newer
        # one. Pins only advance on a successful paint, so the pin a later
        # fall-back restores belongs to the NEWER click; letting the older
        # record win would make the reconcile read 'a fresh snapshot is
        # painted' and discard the clear the newer click is owed. The
        # reconcile never reads this element.
        #
        # It is the ORDINAL and not ``paint_generation``
        # (wh-overlay-slow-uia-stale-badges.23.3): the generation does not
        # order two clicks. A click is routable while the machine is
        # REFRESH_IN_FLIGHT (``route_click_n`` resolves it against the
        # still-visible list) and takes the machine's CURRENT pair, and a
        # refresh that SUCCEEDS paints its new snapshot without bumping the
        # generation, so the next click against that new list carries the
        # same one. Two outstanding clicks can therefore share a generation
        # while owing clears against DIFFERENT pre-click snapshots, and a
        # generation comparison is false on equality.
        #
        # crewcut: the snapshot-identity test proves which LEG the machine took,
        # not that the painted list is settled. The intervening refresh's walk
        # can overlap the change the click caused, so a snapshot that is new can
        # still be a mid-change read, and this field keeps it. Removing that
        # residue is wh-overlay-slow-uia-stale-badges.2, which runs the settle
        # detector (ui/settle_detector.py) over the post-click read.
        self._overlay_pending_postclick_clear: (
            "tuple[int, int, str] | None"
        ) = None
        # wh-overlay-slow-uia-stale-badges.23.6: the LAST paint actually handed
        # to the display process, as (overlay_session_id, paint_generation,
        # snapshot_id). Written in ``_overlay_dispatch_paint`` only when
        # ``_put_overlay_gui_action`` accepted the event, so it names a list the
        # display process was really given. The deferred post-click clear needs
        # it because ``pinned_snapshot_id`` is Logic's ROLLBACK INTENT, not a
        # record of the screen: ``_refresh_fall_back`` and ``_refresh_supersede``
        # both restore the prior pin after ``_refresh_build_ok`` already emitted
        # the new snapshot's paint, and the wh-overlay-slow-uia-stale-badges.19
        # commit fence can only drop a paint that has not been enqueued yet.
        #
        # wh-overlay-slow-uia-stale-badges.23.8: RETIRED by
        # ``_overlay_note_display_teardown`` when the display confirms it tore
        # the badges down, because an empty screen holds no list that a clear
        # could erase. Without that, an owed clear stayed blocked for ever and
        # a mic-resume could re-paint the pre-click list with nothing left to
        # remove it.
        #
        # crewcut: between an enqueue and its acknowledgement the value is a
        # guess, because the display's generation gate can refuse the event.
        # During that window the deferred post-click clear fails closed: it
        # holds the record dormant rather than clearing. Removing the limit
        # needs the display to acknowledge a REFUSED paint, which today it
        # drops silently. (An earlier comment here said the display never
        # reports which snapshot it painted. That was wrong --
        # ``OverlayStateChangedEvent.snapshot_id`` carries it on every
        # ``painted`` ack -- but a painted ack could only restate what
        # ``_overlay_dispatch_paint`` already recorded at the same pair, so
        # nothing reads it here.)
        self._overlay_last_enqueued_paint: (
            "tuple[int, int, str] | None"
        ) = None
        # wh-overlay-slow-uia-stale-badges.23.3: a monotonic count of badge
        # clicks SENT, allocated in ``_send_snapshot_item_click`` -- the one
        # place every badge click passes through, and the owner of the
        # late-response callback that replays an older click's reply. It is
        # the ordering key for the slot above, and nothing else reads it.
        self._overlay_click_dispatch_ordinal: int = 0
        # wh-numbers-stay-setting (decision D5): how many "show numbers" voice
        # commands the machine has applied, and the count each outstanding
        # badge click saw when it was sent (keyed by dispatch ordinal, newest
        # ``_OVERLAY_CLICK_SHOW_COUNT_KEEP`` kept). A success reply whose click
        # was sent BEFORE a later "show numbers" must not remove numbers the
        # user asked for after it.
        self._overlay_show_numbers_applied: int = 0
        self._overlay_click_show_counts: "dict[int, int]" = {}
        # wh-numbers-stay-setting (acceptance item 4): the keep_numbers value
        # each outstanding badge click was sent under, keyed and pruned like
        # the counts above. A click on the settings check box saves a new
        # value, and that save can reach Logic before the click's own reply;
        # the click still follows the value it was sent under.
        self._overlay_click_keep_numbers: "dict[int, bool]" = {}
        # wh-n29v.111: the auto-open item_id_filter, stashed by
        # forward_click_element when it feeds an AUTO_OPEN OverlayEvent and read
        # back by _overlay_dispatch_build when the AUTO_OPEN DISPATCH_BUILD effect
        # runs. The DISPATCH_BUILD Effect carries only snapshot_id (the reuse
        # target), so the finalist filter -- sourced from
        # ClickElementResponse.ambiguous_item_ids -- rides here in the integration
        # layer, NOT on a state-machine Effect. Keyed by the auto-open
        # (overlay_session_id, paint_generation) so a superseding build (a fresh
        # start_overlay_walk) cannot pick up a stale filter; the machine holds at
        # most one auto-open in flight, so a single slot suffices. ``None`` means
        # no auto-open filter is pending.
        self._overlay_auto_open_filter: (
            "tuple[tuple[int, int], list[str]] | None"
        ) = None
        # wh-r3xy1: first-use discovery hint for the screen-reader-flag
        # opt-in. The first voice click into a Chromium-family window while
        # `[click] enable_screen_reader_flag` is false surfaces a one-shot
        # info notice through the existing GUI state queue (no new IPC
        # channel). The tracker owns the dismiss-or-three-subsequent-clicks
        # suppression and the durable record file (data/
        # click_first_use_hint_shown.toml). Initialised lazily on the first
        # click so a test or a no-click session never touches disk; see
        # _first_use_hint_tracker().
        self._first_use_hint: "FirstUseHintTracker | None" = None

    async def forward_click_element(
        self, query, trace_id: str, *, defer_failure_notice: bool = False,
    ) -> Optional[dict]:
        """Logic-side awaiter for a voice 'click <target>' (wh-tab7j).

        Called by ``ActionFunctions.click_element`` after it parses the
        spoken target into an ``ElementQuery`` and generates ``trace_id``.
        This method owns the cross-process round trip and every degrade
        path the bead names:

          1. Config gate. If voice clicking is disabled (operator opt-out
             or a [click] validation failure), short-circuit to a synthetic
             ``execution_failed:disabled_by_config`` notice (shown once per
             session) -- no IPC, no walk.
          2. Forward ``click_element`` to the Input process via
             ``app.send_request`` with ``timeout_s`` derived from the
             configured ``[click] response_timeout_ms`` (NOT
             WheelHouseApp.response_timeout_s). The ElementQuery and
             trace_id ride in the params dict.
          3. On ``asyncio.TimeoutError`` (no reply within the configured
             window) emit a synthetic ``execution_failed:timeout``.
          4. Parse the reply with ``ClickElementResponse.from_dict`` inside
             a try/except (ValueError, KeyError, TypeError). On failure log
             the truncated raw payload + the exception and emit a synthetic
             ``execution_failed:malformed_response``. No unhandled exception
             escapes the asyncio task.
          5. On a valid reply: populate the snapshot-summary cache (so a
             Phase 1.5 numbered-overlay click can resolve), and forward a
             ClickNoticeEvent to the GUI for every non-ok outcome.

        The notice WORDING is owned by wh-g4oma; this method only populates
        and forwards the ClickNoticeEvent payload (carrying trace_id).

        ``defer_failure_notice`` (wh-safety-word-free-commands): "click
        <name>" runs without the safety word, so a click that did nothing
        must let the caller type the words instead of showing a notice. When
        True, exactly three outcomes are returned instead of shown, as the
        keyword arguments ``forward_click_notice`` would have received:
        ``not_found`` on the by-name path, ``disabled_by_config``, and
        ``execution_failed:walk_deadline_exceeded`` (nothing was clicked in
        any of the three). Every other path behaves as with the default and
        returns None. A timeout is NOT one of them: the click may still run
        in the Input process, so it must never be typed, and its notice is
        shown here as always (the late-reply correction assumes that).
        """
        from utils.trace_context import set_trace

        # The contextvar is per-task; set it so send_request stamps the
        # trace_id on the IPC envelope and Logic log lines correlate.
        set_trace(trace_id)
        spoken = getattr(query, "name", "") or ""

        if not self._host.click_config.enabled:
            if defer_failure_notice:
                # The caller decides whether the notice is shown (the
                # safety word) and sets the once-per-session flag then.
                logger.info(
                    "click_element: voice clicking disabled by config "
                    "(invalid_key=%s); returning the notice to the caller "
                    "(trace_id=%s)",
                    self._host.click_config.invalid_key, trace_id,
                )
                return dict(
                    outcome="execution_failed",
                    reason="disabled_by_config",
                    matched_name=None,
                    matched_names=(),
                    spoken_name=spoken,
                    snapshot_id=None,
                    trace_id=trace_id,
                )
            if self._host.take_click_disabled_notice():
                logger.info(
                    "click_element: voice clicking disabled by config "
                    "(invalid_key=%s); showing one-shot notice (trace_id=%s)",
                    self._host.click_config.invalid_key, trace_id,
                )
                self.forward_click_notice(
                    outcome="execution_failed",
                    reason="disabled_by_config",
                    matched_name=None,
                    matched_names=(),
                    spoken_name=spoken,
                    snapshot_id=None,
                    trace_id=trace_id,
                )
            else:
                logger.debug(
                    "click_element: disabled by config; notice already "
                    "shown this session (trace_id=%s)", trace_id,
                )
            return

        # wh-n29v.17: numbered-overlay "click N" routing. Decide whether this
        # utterance is a numeric overlay pick or a by-name click, based on the
        # CURRENT overlay state machine state. The number-extraction rule:
        # parse_number_word runs on query.name ONLY when query.role is None.
        # A spoken role keyword ("click seven button" -> name="seven",
        # role="Button") is a by-name query, not click-N, so a query carrying
        # a role is never treated as a number. Using query.name (not
        # raw_utterance) also avoids trailing-STT-punctuation misses
        # ("click 7." -> name="7"). The non-fall-through cases (snapshot-item,
        # notice, held) return BEFORE the first-use hint and send_request;
        # only BY_NAME falls through to the existing flow below.
        from services.wheelhouse.speech.number_word_parser import (
            parse_number_word,
        )
        from services.wheelhouse.speech.overlay_click_router import (
            OVERLAY_NUMBERS_CHANGED,
            RoutingDecision,
            RoutingKind,
            route_click_n,
        )
        # wh-n29v.111: the ambiguous-outcome auto-open gate below reads
        # OverlayState.CLOSED to decide whether the overlay machine is idle.
        from services.wheelhouse.click_overlay_state import OverlayState

        role = getattr(query, "role", None)
        # wh-overlay-count-homophones (David's decision 2026-09-03): aliases
        # accepts the STT homophones "to", "too" and "for" as 2 and 4, the way
        # the command counts at actions.py:358 and the cursor moves at
        # navigation/parser.py:193 already do. The engine really does return
        # those words for the spoken "two" and "four", so without this a
        # spoken "four" became a by-name search that found nothing. The
        # accepted cost is David's: a bare "to", "too" or "for" spoken while
        # badges or the grid show now clicks 2 or 4 instead of typing. The
        # role guard above is unchanged, so "click for button" is still a
        # by-name query for a control named "for". This one value feeds BOTH
        # the grid claim below and the overlay router, which is why the grid
        # changes with the overlay.
        parsed_number = (
            parse_number_word(spoken, aliases=True) if role is None else None
        )

        # wh-grid-speech-routing: the mouse-grid claim. While the grid is
        # open, a bare 1..9 number (word or digit, no role) is consumed by
        # the grid -- refine, then act with the query's gesture -- and NEVER
        # falls through to the numbered-overlay router, to by-name, or to
        # dictation. The grid claim occupies the same precedence slot as the
        # overlay's route_click_n and is checked FIRST: mutual exclusion
        # keeps at most one of the two open, and if both somehow are, the
        # visible grid is what the user is looking at. The None check stays
        # for a host that has no grid machine.
        from services.wheelhouse.grid_overlay_state import GRID_CELL_COUNT

        grid_machine = self._host.grid_overlay_state
        if (
            grid_machine is not None
            and grid_machine.is_open
            and role is None
            and parsed_number is not None
            and 1 <= parsed_number <= GRID_CELL_COUNT
        ):
            logger.info(
                "click_element: grid open; number %s claimed by the grid "
                "(trace_id=%s)", parsed_number, trace_id,
            )
            await self._host.handle_grid_command(
                "click", trace_id, number=parsed_number,
                gesture=getattr(query, "gesture", None), spoken=spoken,
            )
            return

        # The overlay state machine is created in __init__; read it
        # defensively so a controller assembled without it (a partially-built
        # test fixture, an early-startup path) degrades to the existing
        # by-name flow rather than raising. With no machine the routing cannot
        # be overlay-aware, so by-name is the correct, behaviour-preserving
        # fall-back.
        machine = getattr(self, "click_overlay_state", None)
        # Capture the routing-relevant machine field into a local so the
        # decision-handling branches below do not reach back into ``machine``
        # (which is Optional through the defensive getattr); the non-BY_NAME
        # branches only run when a real machine produced the decision. The
        # snapshot a notice references comes from the resolver
        # (decision.snapshot_id), not from the machine pin, so the pin is not
        # captured here (wh-n29v.19.3).
        overlay_state_label = "no_machine"
        if machine is None:
            decision = RoutingDecision(RoutingKind.BY_NAME)
        else:
            overlay_state_label = machine.state.value
            decision = route_click_n(
                state=machine.state,
                parsed_number=parsed_number,
                cache=self.click_snapshot_summary_cache,
                pinned_snapshot_id=machine.pinned_snapshot_id,
                prior_pinned_snapshot_id=machine.prior_pinned_snapshot_id,
                prior_pin_deferred=machine.prior_pin_deferred,
                visible_window_is_foreground=(
                    self._overlay_refresh_visible_window_is_foreground(machine)
                    if machine.state is OverlayState.REFRESH_IN_FLIGHT
                    else None
                ),
            )

        if decision.kind is RoutingKind.SNAPSHOT_ITEM:
            # wh-overlay-fixqueue-review.2, widened by
            # wh-overlay-slow-uia-stale-badges.4: a "click N" inside the grace
            # window after ANY repaint that replaced the badge list -- a
            # focus- or menu-triggered refresh, the post-click settle re-read,
            # a re-said "show numbers", or the browser timer's proactive tick
            # -- may have been spoken against the pre-swap badges. When badge
            # N changed identity across ANY swap the user could have been
            # reading across (wh-overlay-slow-uia-stale-badges.4.2.1: several
            # repaints can chain inside one grace window), show the "numbers
            # just changed" notice instead of silently clicking the
            # renumbered control.
            if not self._overlay_renumber_click_safe(
                parsed_number, decision.snapshot_id,
            ):
                logger.info(
                    "click_element: overlay pick N=%s blocked -- badge "
                    "identity changed across a repaint swap "
                    "(state=%s, trace_id=%s)",
                    parsed_number, overlay_state_label, trace_id,
                )
                self.forward_click_notice(
                    outcome="execution_failed",
                    reason=OVERLAY_NUMBERS_CHANGED,
                    matched_name=None,
                    matched_names=(),
                    spoken_name=spoken,
                    snapshot_id=decision.snapshot_id,
                    trace_id=trace_id,
                )
                return
            logger.info(
                "click_element: overlay pick N=%s -> snapshot_item "
                "snapshot=%r item=%r (state=%s, trace_id=%s)",
                parsed_number, decision.snapshot_id, decision.item_id,
                overlay_state_label, trace_id,
            )
            self._dispatch_snapshot_item_click(
                snapshot_id=decision.snapshot_id,
                item_id=decision.item_id,
                trace_id=trace_id,
                gesture=getattr(query, "gesture", None),
            )
            return
        if decision.kind is RoutingKind.NOTICE:
            logger.info(
                "click_element: overlay pick N=%s -> notice reason=%r "
                "(state=%s, trace_id=%s)",
                parsed_number, decision.reason, overlay_state_label, trace_id,
            )
            # wh-n29v.18.1 / wh-n29v.19.3: the resolver is the single source of
            # truth for the snapshot a notice references. It sets
            # decision.snapshot_id to the VISIBLE snapshot the number was
            # resolved against on a resolvable-but-miss notice (in
            # refresh_in_flight with a deferred prior, that is the VISIBLE prior
            # snapshot, NOT the current not-yet-painted pin), and leaves it None
            # for the ERROR-state numbers_not_showing reject and the no-pin
            # case. Forward it verbatim. Do NOT fall back to
            # overlay_pinned_snapshot_id: in ERROR the machine deliberately
            # keeps pins populated for recovery unpinning, so the fallback would
            # attach a stale pin -- a snapshot the user never saw -- to a notice
            # whose whole meaning is that numbers are not showing (wh-n29v.19.3).
            self.forward_click_notice(
                outcome="execution_failed",
                reason=decision.reason,
                matched_name=None,
                matched_names=(),
                spoken_name=spoken,
                snapshot_id=decision.snapshot_id,
                trace_id=trace_id,
            )
            return
        if decision.kind is RoutingKind.HELD:
            logger.info(
                "click_element: overlay pick N=%s -> HELD (state=%s, "
                "trace_id=%s)", parsed_number, overlay_state_label, trace_id,
            )
            self._hold_click_n(
                number=parsed_number, spoken=spoken, trace_id=trace_id,
                gesture=getattr(query, "gesture", None),
            )
            return
        # RoutingKind.BY_NAME falls through to the existing send_request flow.

        # wh-r3xy1: first-use discovery hint. A click command is now being
        # attempted into the foreground window. If that window belongs to a
        # Chromium-family process and the screen-reader-flag opt-in is off,
        # surface the one-shot hint pointing the user at the config knob. This
        # is a read-only side observation around the click round trip -- it
        # does not alter the click execution path. The blocking foreground
        # resolution is offloaded off the asyncio loop (wh-9f3t.60.4).
        await self._maybe_show_first_use_hint(trace_id)

        timeout_ms = self._host.click_config.response_timeout_ms
        timeout_s = max(timeout_ms / 1000.0, 0.1)

        # wh-overlay-slow-uia-stale-badges.6 (Option A): if the reply arrives
        # AFTER the timeout below, app.py's grace-limited registry hands it to
        # this callback instead of discarding it, and a refusal replaces the
        # already-shown timeout toast text with the true reason.
        def _on_late_response(late_raw) -> None:
            _handle_late_click_response(
                self, late_raw,
                action="click_element",
                spoken_name=spoken,
                fallback_snapshot_id=None,
                trace_id=trace_id,
            )

        # wh-overlay-slow-uia-stale-badges.7: the by-name click runs the
        # same Input-side UIA walk as an overlay build, so it marks the
        # round trip for the dictation gate too. Counted out in the
        # finally; a timeout here does not cancel Input's running COM
        # walk, but the gate's own maximum wait bounds the refusal.
        self._host.mark_screen_read_started()
        try:
            raw = await self._host.app.send_request(
                "click_element",
                params={"query": query, "trace_id": trace_id},
                timeout_s=timeout_s,
                on_late_response=_on_late_response,
            )
        except asyncio.TimeoutError:
            # wh-mouse-grid.1.30: the request may still be queued in Input
            # and can execute later -- through the gesture or coordinate
            # seam it presses real mouse buttons. Leave the pointer channel
            # suspect so the next session-visibility change (grid open /
            # next_screen, overlay show, ambiguous auto-open) probes the
            # channel before painting.
            self._host.mark_grid_mouse_channel_suspect()
            # WARNING, not ERROR: send_request already logged this fault at
            # ERROR, and every ERROR record raises its own Windows
            # notification popup -- one fault must pop at most one
            # (wh-duplicate-error-popup).
            logger.warning(
                "click_element: no reply within %dms; emitting "
                "execution_failed:timeout (trace_id=%s)",
                timeout_ms, trace_id,
            )
            self.forward_click_notice(
                outcome="execution_failed",
                reason="timeout",
                matched_name=None,
                matched_names=(),
                spoken_name=spoken,
                snapshot_id=None,
                trace_id=trace_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 -- never crash the task
            # wh-mouse-grid.1.30: a failed send may still have placed (part
            # of) the request where Input can read it; treat the channel as
            # suspect like the timeout above -- the fence's probe is cheap
            # and only runs at the next session change.
            self._host.mark_grid_mouse_channel_suspect()
            # A non-timeout send failure (pickle error, shared-memory write
            # failure, queue full) is NOT a timeout; tagging it timeout would
            # mislead the user-facing notice and any consumer that branches on
            # reason (wh-9f3t.56.2). Use a distinct reason; wh-g4oma owns its
            # wording. (asyncio.CancelledError is BaseException on 3.12, so it
            # is not caught here and propagates as cancellation should.)
            # WARNING, not ERROR: send_request already logged the fault at
            # ERROR with traceback; a second ERROR would pop a second
            # notification (wh-duplicate-error-popup).
            logger.warning(
                "click_element: send_request failed (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            self.forward_click_notice(
                outcome="execution_failed",
                reason="send_request_failed",
                matched_name=None,
                matched_names=(),
                spoken_name=spoken,
                snapshot_id=None,
                trace_id=trace_id,
            )
            return
        finally:
            self._host.mark_screen_read_finished()

        from shared.click_element import (
            ClickElementResponse,
            ClickElementResponseSchemaError,
        )

        try:
            response = ClickElementResponse.from_dict(raw)
        except (ClickElementResponseSchemaError, ValueError, KeyError, TypeError) as exc:
            # Log a STRUCTURAL summary only -- never raw response values
            # (wh-9f3t.55.2). A malformed ClickElementResponse can carry
            # matched_names and snapshot_summary item names lifted from the
            # foreground UI, and a skewed Input process could place arbitrary
            # local content in any field; the main log must stay free of
            # on-screen text. We emit the payload's shape -- its type, the
            # top-level field names, each value's type, and the length of any
            # sized value -- which keeps the error diagnosable without
            # exposing values.
            if isinstance(raw, dict):
                _fields = []
                for _k in sorted(map(str, raw.keys())):
                    _v = raw.get(_k)
                    try:
                        _fields.append(f"{_k}:{type(_v).__name__}[{len(_v)}]")  # type: ignore[arg-type]
                    except TypeError:
                        _fields.append(f"{_k}:{type(_v).__name__}")
                payload_shape = "dict{" + ", ".join(_fields) + "}"
            else:
                try:
                    payload_shape = f"{type(raw).__name__}[{len(raw)}]"  # type: ignore[arg-type]
                except TypeError:
                    payload_shape = type(raw).__name__
            logger.error(
                "click_element: malformed response (trace_id=%s): %s; "
                "payload_shape=%s", trace_id, exc, payload_shape,
            )
            self.forward_click_notice(
                outcome="execution_failed",
                reason="malformed_response",
                matched_name=None,
                matched_names=(),
                spoken_name=spoken,
                snapshot_id=None,
                trace_id=trace_id,
            )
            return

        # Populate the snapshot-summary cache for the Phase 1.5 overlay
        # round trip. Keyed by snapshot_id; safe to skip when no walk
        # produced a summary (e.g. a synthetic Input-side failure).
        if response.snapshot_id and response.snapshot_summary is not None:
            try:
                self.click_snapshot_summary_cache.put(
                    response.snapshot_id, response.snapshot_summary,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "click_element: failed to populate snapshot cache "
                    "(trace_id=%s): %s", trace_id, exc,
                )

        if response.outcome == "ok":
            logger.info(
                "click_element: ok matched=%r (trace_id=%s)",
                response.matched_name, trace_id,
            )
            return

        # wh-n29v.111: auto-open the numbered overlay on an ambiguous match,
        # restricted to the ambiguous finalists, instead of the plain notice.
        # The decision tree (v4 design "## Auto-open on ambiguous match") gates
        # on overlay_enabled_effective AND overlay_auto_open_on_ambiguous AND a
        # usable (snapshot_id, ambiguous_item_ids) AND the state machine sitting
        # in CLOSED (the machine admits auto_open only from closed; an overlay
        # already mid-session must not be hijacked by a stray ambiguous click).
        # The DECISION is evaluated INLINE -- not behind a helper method -- so a
        # MagicMock(spec=LogicController) test fixture that binds only
        # forward_click_element cannot auto-mock the gate into an always-truthy
        # short-circuit; the non-auto-open case must still reach the plain notice
        # below for any controller shape. The apply/stash work delegates to
        # ``_perform_auto_open_ambiguous`` only AFTER the gate elects to open.
        if (
            response.outcome == "ambiguous"
            and self._host.click_config.enabled
            and self._host.click_config.overlay_enabled_effective
            and self._host.click_config.overlay_auto_open_on_ambiguous
            and response.snapshot_id
            and response.ambiguous_item_ids
            and getattr(
                getattr(self, "click_overlay_state", None), "state", None,
            ) is OverlayState.CLOSED
            # wh-mouse-grid.1.29: the auto-open is the fourth way a new
            # overlay session becomes visible; a stale grid pointer
            # action must not act under it. False falls through to the
            # plain ambiguous notice below, the gate's existing degrade.
            and await self._host.grid_pointer_channel_quiescent(
                trace_id=trace_id,
            )
            and self._perform_auto_open_ambiguous(response, spoken, trace_id)
        ):
            return

        # Non-ok outcome -> forward a ClickNoticeEvent to the GUI through the
        # shared response-to-notice mapping (also used by the late-answer
        # callback, wh-overlay-slow-uia-stale-badges.6, so the two cannot
        # drift). The response.trace_id echoes the one we generated; the
        # helper prefers it so a log surface can correlate even if it
        # diverged.
        if defer_failure_notice and (
            response.outcome == "not_found"
            or (
                response.outcome == "execution_failed"
                and response.reason == "walk_deadline_exceeded"
            )
        ):
            # Nothing was clicked: hand the notice arguments to the caller,
            # which shows them only when the safety word was spoken.
            return _click_response_notice_kwargs(
                response,
                spoken_name=spoken,
                fallback_snapshot_id=None,
                trace_id=trace_id,
            )
        _forward_click_response_notice(
            self,
            response,
            spoken_name=spoken,
            fallback_snapshot_id=None,
            trace_id=trace_id,
        )
        return None

    def _perform_auto_open_ambiguous(self, response, spoken: str,
                                     trace_id: str) -> bool:
        """Apply the AUTO_OPEN OverlayEvent for an ambiguous click (wh-n29v.111).

        Reached only after ``forward_click_element``'s inline gate has confirmed
        overlay-on, auto-open-on, a usable ``(snapshot_id, ambiguous_item_ids)``,
        and the machine in ``CLOSED``. Returns ``True`` when the auto-open
        actually entered the machine (the caller suppresses today's notice),
        ``False`` when it did not (the caller falls back to the plain notice).
        It:

          1. builds the suppressed ``ClickNoticeEvent`` (the one today's path
             would have shown) so the machine can fire it on a later failure;
          2. applies an ``AUTO_OPEN`` ``OverlayEvent`` carrying that notice and
             the reuse ``snapshot_id`` -- the machine allocates a fresh
             ``(overlay_session_id, paint_generation)`` and emits the
             ``DISPATCH_BUILD(AUTO_OPEN)`` + ``ARM_TIMER`` effects;
          3. STASHES the ``item_id_filter`` (the response's
             ``ambiguous_item_ids`` as a list) keyed by the freshly-allocated
             pair, so ``_overlay_dispatch_build`` reads it back when the
             AUTO_OPEN build runs (the DISPATCH_BUILD effect carries only the
             reuse snapshot_id; the filter rides in the integration layer, not on
             the Effect).

        The state transition and the effect hand-off happen synchronously here
        (``_apply_overlay_event`` only SCHEDULES the async IPC dispatch as a
        background task), so the stash is in place before the dispatch task runs.
        Never raises: any unexpected failure logs and returns ``False`` so the
        plain notice path still fires (the user must not lose feedback).
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )
        from shared.click_notice import ClickNoticeEvent

        machine = self.click_overlay_state
        snapshot_id = response.snapshot_id
        item_ids = response.ambiguous_item_ids
        try:
            suppressed = ClickNoticeEvent(
                outcome=response.outcome,
                reason=response.reason,
                matched_name=response.matched_name,
                matched_names=tuple(response.matched_names),
                spoken_name=spoken,
                app_friendly_name="",
                snapshot_id=snapshot_id,
                trace_id=response.trace_id or trace_id,
            )
        except Exception as exc:  # noqa: BLE001 -- degrade to the plain notice
            logger.warning(
                "click_element: failed to build suppressed auto-open notice; "
                "plain notice (trace_id=%s): %s", trace_id, exc,
            )
            return False

        # Apply AUTO_OPEN (closed -> walk_in_flight). The machine allocates the
        # fresh (overlay_session_id, paint_generation) during apply; read it back
        # to key the filter stash to the exact build the effect carries.
        #
        # wh-n29v.112.1: the apply, the post-apply guard, and the stash write are
        # wrapped so the docstring's "Never raises -> plain notice still fires"
        # contract holds for this region too (the notice-build above is already
        # guarded). ``_apply_overlay_event`` commits the closed -> walk_in_flight
        # transition SYNCHRONOUSLY before its (unguarded) reconcile helpers run,
        # so a raise from a reconcile helper -- or from the stash write -- after
        # that point would otherwise leave the machine wedged in walk_in_flight
        # (every later ambiguous click then fails the CLOSED gate and the overlay
        # feature is dead until restart) AND skip the caller's plain-notice
        # fallback (silent loss of feedback). On any failure we reset the machine
        # to CLOSED (the sanctioned integration recovery, mirroring the other
        # overlay recovery paths), clear the stash, and return False so the caller
        # fires the plain notice exactly once.
        try:
            # wh-grid-speech-routing: mutual exclusion. The ambiguous
            # auto-open is the second way the numbered overlay opens; close
            # the mouse grid before the overlay machine moves, mirroring the
            # handle_overlay_command 'show' branch.
            self._close_grid_for_numbered_overlay(trace_id)
            self._apply_overlay_event(
                OverlayEvent(
                    kind=OverlayEventKind.AUTO_OPEN,
                    notice=suppressed,
                    snapshot_id=snapshot_id,
                ),
                source=f"auto_open ambiguous snapshot={snapshot_id}",
            )
            if machine.state is not OverlayState.WALK_IN_FLIGHT:
                # The machine did not enter the auto-open path (a defensive guard
                # against a future machine change). Fall back to the plain notice
                # so the user still gets feedback; clear any stale stash.
                self._overlay_auto_open_filter = None
                logger.warning(
                    "click_element: AUTO_OPEN did not enter walk_in_flight "
                    "(state=%s); plain notice (trace_id=%s)",
                    machine.state.value, trace_id,
                )
                return False
            pair = (machine.overlay_session_id, machine.paint_generation)
            self._overlay_auto_open_filter = (pair, list(item_ids))
            logger.info(
                "click_element: auto-open dispatched, suppressing notice "
                "(gen=%s, filter=%d id(s), trace_id=%s)",
                pair, len(item_ids), trace_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001 -- degrade to the plain notice
            logger.warning(
                "click_element: auto-open apply/stash failed; resetting the "
                "overlay machine and falling back to the plain notice "
                "(trace_id=%s): %s", trace_id, exc,
            )
            self._overlay_auto_open_filter = None
            try:
                effects = machine.reset_to_closed()
                if effects:
                    self._perform_overlay_effects(effects, trace_id=trace_id)
            except Exception as reset_exc:  # noqa: BLE001 -- best-effort un-wedge
                logger.error(
                    "click_element: failed to reset the overlay machine after an "
                    "auto-open failure; machine may remain in %s (trace_id=%s): "
                    "%s", getattr(machine, "state", None), trace_id, reset_exc,
                )
            return False

    # ------------------------------------------------------------------
    # wh-n29v.17: numbered-overlay routing stub seams + show/hide handler.
    #
    # These are the documented STUB SEAMS the routing layer hands its
    # decisions / effects to. The effect-PERFORMING integration -- the real
    # cross-process IPC dispatch of build / paint / clear, pin / unpin, the
    # 200ms "click N" hold timer, the per-state timeout timers, and the real
    # click_snapshot_item dispatch -- is owned by the integration bead
    # wh-h9a8v2. Until that bead lands, each seam LOGS what it would do so a
    # routing test can assert the seam was reached, and the integration bead
    # replaces the body without touching the routing decision layer.
    # ------------------------------------------------------------------
    def _dispatch_snapshot_item_click(
        self, *, snapshot_id: Optional[str], item_id: Optional[str],
        trace_id: str, gesture=None,
    ) -> None:
        """Dispatch a numbered-overlay item click to the Input process (wh-n29v.95).

        The routing layer (voice ``click N``) or the hold timer resolved a
        numbered badge to (snapshot_id, item_id). This schedules the real
        ``click_snapshot_item`` IPC round trip as a background task so the
        synchronous caller (``forward_click_element`` after a routing decision)
        does not block on the cross-process send. ``_send_snapshot_item_click``
        owns the await, the ClickElementResponse parse, and the click-notice on
        any non-ok outcome (no notice on ok).

        POST-CLICK REFRESH GATE: the machine's CURRENT
        (overlay_session_id, paint_generation) is captured HERE, at dispatch
        time, and threaded to ``_send_snapshot_item_click`` so the success
        branch can feed CLICK_COMPLETE against the overlay the click was
        actually dispatched against. Capturing the pair at dispatch (rather
        than re-reading the machine when the ok response arrives) is what makes
        the gate correct: if the overlay is superseded (same session, newer
        generation) or torn down (new session) between dispatch and the ok
        reply, the captured pair no longer matches the live machine and the
        refresh is suppressed -- mirrors the ``_hold_click_n`` /
        ``_resolve_held_click_n`` armed-pair pattern (wh-n29v.96.3).
        """
        if not snapshot_id or not item_id:
            # wh-n29v.97.2: an unusable (snapshot_id, item_id) is an invariant
            # violation (the resolver only emits SNAPSHOT_ITEM with both set),
            # but a silent drop leaves a hands-free user with no feedback that
            # the accepted "click N" did nothing. Honour this slice's
            # never-a-silent-drop rule: surface an execution_failed notice with
            # a distinct reason and do NOT schedule an Input round trip.
            logger.warning(
                "overlay: snapshot-item click missing identity "
                "(snapshot=%r item=%r, trace_id=%s); surfacing "
                "execution_failed:invalid_snapshot_item",
                snapshot_id, item_id, trace_id,
            )
            self.forward_click_notice(
                outcome="execution_failed", reason="invalid_snapshot_item",
                matched_name=None, matched_names=(), spoken_name="",
                snapshot_id=snapshot_id or None, trace_id=trace_id,
            )
            return
        if self._host.grid_session_in_transition():
            # wh-mouse-grid.1.32: a grid open / next_screen is between
            # its quiescence fence and its paint. A badge click admitted
            # now would send after the fence already passed and could
            # press real mouse buttons under the freshly painted grid.
            # Refuse rather than hold: the user's newest intent is the
            # grid, and the click targets an overlay the transition is
            # tearing down.
            logger.info(
                "overlay: snapshot-item click refused, grid session "
                "transition in progress (snapshot=%r item=%r, "
                "trace_id=%s)", snapshot_id, item_id, trace_id,
            )
            self.forward_click_notice(
                outcome="execution_failed",
                reason="grid_transition_pending",
                matched_name=None, matched_names=(), spoken_name="",
                snapshot_id=snapshot_id, trace_id=trace_id,
            )
            return
        in_flight = getattr(self, "_overlay_click_in_flight", None)
        if in_flight is not None:
            # wh-overlay-slow-uia-stale-badges.8: a previous badge click has
            # not yet received its Input reply. Input runs commands strictly
            # in order, so this second click would execute AFTER the first
            # against the same painted list -- the first click's press has
            # already changed the screen, and number N now names the wrong
            # control. Refuse loudly (never a silent drop) rather than hold:
            # the settle/refresh that follows the first click's reply rebuilds
            # the list, and the user must re-read the new numbers anyway.
            logger.info(
                "overlay: snapshot-item click refused, previous click still "
                "in flight (in_flight=%r, snapshot=%r item=%r, trace_id=%s)",
                in_flight, snapshot_id, item_id, trace_id,
            )
            self.forward_click_notice(
                outcome="execution_failed",
                reason="overlay_click_in_flight",
                matched_name=None, matched_names=(), spoken_name="",
                snapshot_id=snapshot_id, trace_id=trace_id,
            )
            return
        machine = getattr(self, "click_overlay_state", None)
        overlay_dispatch_pair = (
            (machine.overlay_session_id, machine.paint_generation)
            if machine is not None
            else None
        )
        # wh-overlay-slow-uia-stale-badges.8: the pair this click RESOLVED
        # against -- the last-painted (visible) pair, not the machine's
        # current pair (they differ during REFRESH_IN_FLIGHT; see the attr
        # comment in __init__). Rides to Input inside the command payload so
        # Input can refuse a click from a generation an earlier click already
        # consumed.
        overlay_resolved_pair = getattr(
            self, "_overlay_visible_painted_pair", None
        )
        self._overlay_click_in_flight = (snapshot_id, item_id, trace_id)
        try:
            task = self._host.create_task_with_error_handling(
                self._send_snapshot_item_click(
                    snapshot_id=snapshot_id, item_id=item_id,
                    trace_id=trace_id,
                    overlay_dispatch_pair=overlay_dispatch_pair,
                    overlay_resolved_pair=overlay_resolved_pair,
                    gesture=gesture,
                ),
                "ClickSnapshotItem",
            )
        except BaseException:
            # If task creation itself fails, the send coroutine never runs
            # and its finally-clear never executes; clear here so the guard
            # cannot wedge shut.
            self._overlay_click_in_flight = None
            raise
        # wh-mouse-grid.1.30: a badge click is pointer-capable (a
        # non-default gesture or the coordinate fallback presses real
        # mouse buttons), so a later session-visibility change must wait
        # for it exactly like a grid pointer action.
        self._host.track_pending_pointer_task(task)

    async def _send_snapshot_item_click(
        self, *, snapshot_id: str, item_id: str, trace_id: str,
        overlay_dispatch_pair: "Optional[tuple[int, int]]" = None,
        overlay_resolved_pair: "Optional[tuple[int, int]]" = None,
        gesture=None,
    ) -> None:
        """Send ``click_snapshot_item`` to Input and surface the notice (wh-n29v.95).

        Mirrors the ``click_element`` awaiter's degrade paths exactly: a
        timeout, a non-timeout send failure, and a malformed reply each emit a
        synthetic ``execution_failed`` notice with a distinct reason tag rather
        than crashing the task. On a valid reply, every non-ok outcome forwards
        a ClickNoticeEvent; ``outcome="ok"`` shows NO notice. Never raises.

        POST-CLICK REFRESH: on an ``ok`` outcome this feeds an
        OverlayEvent(CLICK_COMPLETE) so a painted overlay stops showing the
        now-stale pre-click UI. Where that lands depends on the machine's
        ``settle_after_click`` flag, which comes from ``[click]
        overlay_settle_after_click`` (wh-overlay-slow-uia-stale-badges.1):
        off, the shipped default, refreshes (painted -> refresh_in_flight,
        generation bumped) and the old badges stay up for the whole re-walk;
        on, the badges clear at once and the machine enters
        post_click_settling. ``overlay_dispatch_pair`` is the
        (overlay_session_id, paint_generation) captured at click-dispatch time;
        the feed is gated on it (see ``_feed_click_complete_refresh``).

        wh-overlay-slow-uia-stale-badges.8: ``overlay_resolved_pair`` is the
        last-painted (visible-list) pair the number was resolved against;
        when present it rides in the command payload as ``overlay_session_id``
        / ``paint_generation`` so Input can refuse a click from a list an
        earlier click already consumed. An ``ok`` outcome also cancels a held
        'click N' armed at the dispatch pair (the only not-yet-sent click
        Logic queues). The in-flight marker set by the dispatcher is cleared
        in the ``finally`` below on EVERY exit -- ok, refusal, timeout, send
        failure, malformed reply, or cancellation -- because a wedged marker
        would refuse every later badge click for the session.
        """
        # wh-overlay-slow-uia-stale-badges.23.3: allocate this click's
        # dispatch ordinal HERE, in the one place every badge click passes
        # through, so the on-time reply and the late-response callback
        # created below both carry the same value. The dispatcher's
        # in-flight guard is cleared in this coroutine's finally, so a
        # second click cannot start before this line has run: the ordinals
        # increase in true dispatch order.
        self._overlay_click_dispatch_ordinal = (
            getattr(self, "_overlay_click_dispatch_ordinal", 0) + 1
        )
        show_counts = getattr(self, "_overlay_click_show_counts", None)
        if show_counts is None:
            show_counts = self._overlay_click_show_counts = {}
        show_counts[self._overlay_click_dispatch_ordinal] = getattr(
            self, "_overlay_show_numbers_applied", 0,
        )
        click_keeps = getattr(self, "_overlay_click_keep_numbers", None)
        if click_keeps is None:
            click_keeps = self._overlay_click_keep_numbers = {}
        machine = getattr(self, "click_overlay_state", None)
        if machine is not None:
            click_keeps[self._overlay_click_dispatch_ordinal] = (
                machine.keep_numbers
            )
        for record in (show_counts, click_keeps):
            for stale in [
                key for key in record
                if key <= self._overlay_click_dispatch_ordinal
                - _OVERLAY_CLICK_SHOW_COUNT_KEEP
            ]:
                del record[stale]
        try:
            await self._send_snapshot_item_click_inner(
                snapshot_id=snapshot_id, item_id=item_id, trace_id=trace_id,
                overlay_dispatch_pair=overlay_dispatch_pair,
                overlay_resolved_pair=overlay_resolved_pair,
                overlay_dispatch_ordinal=(
                    self._overlay_click_dispatch_ordinal
                ),
                gesture=gesture,
            )
        finally:
            self._overlay_click_in_flight = None
            # wh-overlay-refresh-after-mouse: the number click's own clicks
            # start no second refresh (RW-39).
            self._overlay_number_click_ended()

    async def _send_snapshot_item_click_inner(
        self, *, snapshot_id: str, item_id: str, trace_id: str,
        overlay_dispatch_pair: "Optional[tuple[int, int]]" = None,
        overlay_resolved_pair: "Optional[tuple[int, int]]" = None,
        overlay_dispatch_ordinal: int = 0,
        gesture=None,
    ) -> None:
        """Body of ``_send_snapshot_item_click``; see its docstring."""
        from utils.trace_context import set_trace

        set_trace(trace_id)
        timeout_ms = self._host.click_config.response_timeout_ms
        timeout_s = max(timeout_ms / 1000.0, 0.1)
        # wh-grid-speech-routing / wh-click-gesture-param: a non-default
        # gesture ("right click 3" / "double click 3") rides as the optional
        # ``gesture`` payload field the Input-side handler already parses via
        # ``_parse_gesture``. The default Invoke gesture is OMITTED so every
        # pre-existing payload shape is byte-identical.
        from services.wheelhouse.ui.element_types import DEFAULT_GESTURE

        params = {
            "snapshot_id": snapshot_id,
            "item_id": item_id,
            "trace_id": trace_id,
        }
        if overlay_resolved_pair is not None:
            # wh-overlay-slow-uia-stale-badges.8: the visible-list pair this
            # click was resolved against. OMITTED when unknown so every
            # pre-slice payload shape stays byte-identical and Input skips
            # its stale-generation check.
            params["overlay_session_id"] = overlay_resolved_pair[0]
            params["paint_generation"] = overlay_resolved_pair[1]
        if gesture is not None and gesture != DEFAULT_GESTURE:
            params["gesture"] = str(
                getattr(gesture, "value", gesture)
            )

        # wh-overlay-slow-uia-stale-badges.6 (Option A): a reply arriving
        # after the timeout corrects the already-shown timeout toast with the
        # true refusal (the incident: a 7810 ms pre-click verification ended
        # in a bounds_invalid refusal that arrived ~5 s after the awaiter
        # gave up and was discarded). The dispatch-time snapshot_id is the
        # notice fallback, exactly as on the on-time non-ok path.
        def _on_late_response(late_raw) -> None:
            _handle_late_click_response(
                self, late_raw,
                action="click_snapshot_item",
                spoken_name="",
                fallback_snapshot_id=snapshot_id,
                trace_id=trace_id,
                # wh-overlay-slow-uia-stale-badges.20.3: a late re-walk
                # refusal feeds the same post-click refresh the on-time tail
                # feeds, gated on this dispatch-time pair.
                overlay_dispatch_pair=overlay_dispatch_pair,
                overlay_dispatch_ordinal=overlay_dispatch_ordinal,
            )

        try:
            raw = await self._host.app.send_request(
                "click_snapshot_item",
                params=params,
                timeout_s=timeout_s,
                on_late_response=_on_late_response,
            )
        except asyncio.TimeoutError:
            # wh-mouse-grid.1.30: the badge click may still be queued in
            # Input and can execute later (gesture or coordinate seam);
            # leave the pointer channel suspect so the next session change
            # probes before painting.
            self._host.mark_grid_mouse_channel_suspect()
            # WARNING, not ERROR: send_request already logged this fault at
            # ERROR; a second ERROR would pop a second notification
            # (wh-duplicate-error-popup).
            logger.warning(
                "click_snapshot_item: no reply within %dms; "
                "execution_failed:timeout (trace_id=%s)", timeout_ms, trace_id,
            )
            self.forward_click_notice(
                outcome="execution_failed", reason="timeout",
                matched_name=None, matched_names=(), spoken_name="",
                snapshot_id=snapshot_id, trace_id=trace_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 -- never crash the task
            # wh-mouse-grid.1.30: a failed send may still have placed the
            # request where Input can read it; treat the channel as suspect
            # like the timeout above.
            self._host.mark_grid_mouse_channel_suspect()
            # CancelledError is BaseException on 3.12, so it is not caught here.
            # WARNING, not ERROR: send_request already logged the fault at
            # ERROR with traceback (wh-duplicate-error-popup).
            logger.warning(
                "click_snapshot_item: send_request failed (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            self.forward_click_notice(
                outcome="execution_failed", reason="send_request_failed",
                matched_name=None, matched_names=(), spoken_name="",
                snapshot_id=snapshot_id, trace_id=trace_id,
            )
            return

        from shared.click_element import (
            ClickElementResponse,
            ClickElementResponseSchemaError,
        )

        try:
            response = ClickElementResponse.from_dict(raw)
        except (
            ClickElementResponseSchemaError, ValueError, KeyError, TypeError,
        ) as exc:
            logger.error(
                "click_snapshot_item: malformed response (trace_id=%s): %s",
                trace_id, exc,
            )
            self.forward_click_notice(
                outcome="execution_failed", reason="malformed_response",
                matched_name=None, matched_names=(), spoken_name="",
                snapshot_id=snapshot_id, trace_id=trace_id,
            )
            return

        if response.outcome == "ok":
            logger.info(
                "click_snapshot_item: ok matched=%r (trace_id=%s)",
                response.matched_name, trace_id,
            )
            # wh-overlay-slow-uia-stale-badges.8: the success consumed the
            # list; a held 'click N' armed at the same pair must not fire
            # against it. Cancel BEFORE the CLICK_COMPLETE feed so the hold
            # cannot race the machine transition.
            self._cancel_held_click_after_success(
                overlay_dispatch_pair=overlay_dispatch_pair,
                trace_id=trace_id,
            )
            self._feed_click_complete_refresh(
                overlay_dispatch_pair=overlay_dispatch_pair,
                trace_id=trace_id,
                click_succeeded=True,
                preclick_snapshot_id=snapshot_id,
                overlay_dispatch_ordinal=overlay_dispatch_ordinal,
            )
            return

        # Shared response-to-notice mapping (also used by the late-answer
        # callback, wh-overlay-slow-uia-stale-badges.6).
        _forward_click_response_notice(
            self,
            response,
            spoken_name="",
            fallback_snapshot_id=snapshot_id,
            trace_id=trace_id,
        )

        # wh-overlay-stale-click-refresh: a click refused because the badge no
        # longer points at a clickable control in the same place (the saved
        # position is unreadable/zero-area, drifted, scrolled off-screen, or its
        # owning popup closed) means the layout changed after the overlay was
        # painted. Re-walk and repaint so the next 'click N' resolves against
        # fresh positions; otherwise the stale snapshot stays pinned and every
        # retry fails the same way. The exact reason set lives in
        # _OVERLAY_REWALK_REFUSAL_REASONS -- a transport failure, a disabled
        # control, or a found-but-unresponsive control is excluded because a
        # re-walk would not change the outcome. Reuses the success path's
        # generation/session + PAINTED gating, so a superseded / torn-down /
        # paused overlay is a safe no-op.
        if response.reason in _OVERLAY_REWALK_REFUSAL_REASONS:
            self._feed_click_complete_refresh(
                overlay_dispatch_pair=overlay_dispatch_pair,
                trace_id=response.trace_id or trace_id,
                click_succeeded=False,
                trigger=f"click refused ({response.reason})",
                preclick_snapshot_id=snapshot_id,
                overlay_dispatch_ordinal=overlay_dispatch_ordinal,
            )

    def _cancel_held_click_after_success(
        self, *, overlay_dispatch_pair: "Optional[tuple[int, int]]",
        trace_id: str,
    ) -> None:
        """Cancel a held 'click N' the ok click just invalidated (wh-overlay-slow-uia-stale-badges.8).

        Part 3 of the concurrent-click fix: when a badge click succeeds, every
        not-yet-sent click from the same generation must be cancelled. With
        the in-flight guard refusing immediate seconds, the only not-yet-sent
        click Logic ever queues is the 200ms hold timer. Cancel it when its
        armed pair equals the successful click's dispatch pair -- both pairs
        are the machine's current pair at their capture instants, so equality
        means the hold targets exactly the list the success consumed. A hold
        armed at a DIFFERENT pair belongs to a superseding overlay and is
        left to its own generation gate in ``_resolve_held_click_n``. Never a
        silent drop: the cancel surfaces a numbers_updating notice carrying
        the held spoken text.
        """
        timer = getattr(self, "_overlay_hold_timer", None)
        armed = getattr(self, "_overlay_hold_armed", None)
        if timer is None or armed is None:
            return
        if overlay_dispatch_pair is None:
            return
        armed_pair, spoken = armed
        if armed_pair != overlay_dispatch_pair:
            return
        timer.cancel()
        self._overlay_hold_timer = None
        self._overlay_hold_armed = None
        logger.info(
            "overlay: held click %r cancelled -- a badge click at the same "
            "pair %s succeeded, so the held number names a control on a "
            "consumed list (trace_id=%s)",
            spoken, overlay_dispatch_pair, trace_id,
        )
        self.forward_click_notice(
            outcome="execution_failed", reason="numbers_updating",
            matched_name=None, matched_names=(), spoken_name=spoken,
            snapshot_id=None, trace_id=trace_id,
        )

    def _feed_click_complete_refresh(
        self, *, overlay_dispatch_pair: "Optional[tuple[int, int]]",
        trace_id: str,
        click_succeeded: bool,
        trigger: str = "click ok",
        preclick_snapshot_id: "Optional[str]" = None,
        overlay_dispatch_ordinal: int = 0,
    ) -> None:
        """Feed CLICK_COMPLETE after an overlay item click completes (this slice).

        ``click_succeeded`` (wh-numbers-stay-setting) is True when the click
        landed and False for a refusal because the saved position is stale.
        With ``[click] overlay_keep_numbers`` off, a successful click removes
        the numbers: this function applies the same ``HIDE_NUMBERS`` event the
        "hide numbers" voice command applies, BEFORE the generation and state
        gates below, whenever the click was dispatched in the overlay session
        that is on the machine now. A refresh that started between the click
        and its reply (a menu closing, the browser timer, an owned dialog)
        bumps the generation and used to drop the success, so the numbers came
        back as new numbers. A stale-position refusal still refreshes them.

        Called on a successful 'click N', and on a 'click N' refused for a stale
        or unreadable saved position (bounds_invalid / bounds_stale,
        wh-overlay-stale-click-refresh). Both mean the on-screen layout the
        overlay was pinned to has changed, so a fresh walk + repaint is wanted.
        ``trigger`` only labels the log lines (the success path passes the
        default).

        The state machine already handled ``painted`` + CLICK_COMPLETE;
        nothing on the Logic side fed it, so after a successful 'click N' the
        overlay stayed pinned to the now-stale pre-click UI and a following
        'click N' resolved against a stale snapshot. The machine's destination
        is flag-dependent since wh-overlay-slow-uia-stale-badges.1: with
        ``settle_after_click`` off (the shipped default) it is ``_refresh``
        (painted -> refresh_in_flight, generation bumped); with it on the
        badges clear and the machine enters post_click_settling. This feeds the
        event through the SAME
        ``_apply_overlay_event`` -> ``_perform_overlay_effects`` path the rest of
        the feature uses, preserving the single-``_overlay_effect_lock`` FIFO
        batch ordering.

        GENERATION/SESSION GATE: CLICK_COMPLETE is NOT a generation-bearing kind
        (the machine ignores its (overlay_session_id, paint_generation) and does
        not run the pre-table stale-generation check on it), so the gate MUST
        live here. Two conditions must both hold before feeding:

          1. The machine's CURRENT (overlay_session_id, paint_generation) still
             equals ``overlay_dispatch_pair`` -- the pair captured when the click
             was dispatched. A supersede (same session, newer generation) or a
             new session that replaced the overlay between dispatch and the ok
             reply makes the pair mismatch, so a stale click cannot drive a
             refresh on the newer overlay (mirrors ``_resolve_held_click_n``).
          2. The machine is in ``PAINTED``. A painted overlay paused via mic
             (painted -> paused) does NOT bump the generation, so the pair still
             matches; feeding CLICK_COMPLETE there would hit ``_on_paused``'s
             ``_invalid`` path and drive the machine to ``error``. Requiring
             PAINTED keeps a late/closed CLICK_COMPLETE a Logic-side no-op
             rather than an error, preserving wh-n29v.95 part 6. PAUSED is not
             a no-op: it DEFERS through the pending machinery (see the branch
             below, wh-overlay-slow-uia-stale-badges.21.2).

        Feeding inline here is safe for the lock ordering: this runs from the
        ``_send_snapshot_item_click`` awaiter (its own background task), NOT from
        inside an in-flight ``_dispatch_overlay_effects`` batch, so it does not
        re-enter the held ``_overlay_effect_lock``. ``_apply_overlay_event``
        commits the transition synchronously and SCHEDULES the refresh effects as
        a fresh, separately-locked batch, so FIFO batch ordering is preserved
        without a ``call_soon`` hop (unlike ``_overlay_dispatch_build._feed``,
        which defers precisely because it runs WHILE holding the lock).
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )

        machine = getattr(self, "click_overlay_state", None)
        if machine is None or overlay_dispatch_pair is None:
            return
        current_pair = (machine.overlay_session_id, machine.paint_generation)
        click_show_count = getattr(
            self, "_overlay_click_show_counts", {},
        ).get(overlay_dispatch_ordinal)
        shown_again = (
            click_show_count is not None
            and click_show_count
            != getattr(self, "_overlay_show_numbers_applied", 0)
        )
        # The value this click was sent under (``_overlay_click_keep_numbers``);
        # the current value only when no record exists for the click.
        click_keep_numbers = getattr(
            self, "_overlay_click_keep_numbers", {},
        ).get(overlay_dispatch_ordinal, machine.keep_numbers)
        if (
            click_succeeded
            and not click_keep_numbers
            and overlay_dispatch_pair[0] == current_pair[0]
            and not shown_again
        ):
            # wh-numbers-stay-setting, item 2a: a successful click removes the
            # numbers, whatever generation or state the machine is in now. The
            # event is the one ``handle_overlay_command`` applies for "hide
            # numbers" (and ``_apply_lost_restore_paint_hide`` applies through
            # this same ``_apply_overlay_event`` path), so the clear, the
            # unpin, the session end and every post-apply reconciler are the
            # same. Only the SESSION is compared: a refresh in the same
            # session does not change what the click did. A different session
            # means the overlay the click was dispatched against is gone, and
            # the gate below keeps today's drop. This also wins over
            # ``settle_after_click`` (decision D1) and leaves no pending
            # post-click record behind to be replaced and dropped. A "show
            # numbers" applied AFTER the click was sent (decision D5, item 2e)
            # cancels this early hide: the user asked for numbers after the
            # click, and a late reply must not remove them. That reply takes
            # today's path below.
            logger.info(
                "overlay: %s succeeded at gen=%s (machine now gen=%s, "
                "state=%s); overlay_keep_numbers is off, so the numbers are "
                "removed (trace_id=%s)",
                trigger, overlay_dispatch_pair, current_pair,
                machine.state.value, trace_id,
            )
            self._apply_overlay_event(
                OverlayEvent(kind=OverlayEventKind.HIDE_NUMBERS),
                source=f"click_complete ({trigger}) gen={current_pair}",
            )
            return
        if current_pair != overlay_dispatch_pair:
            # wh-overlay-slow-uia-stale-badges.15, the interleave this branch
            # used to drop on the floor. A FOCUS_CHANGE from any of the four
            # sources -- the foreground hook, the menu-popup hook, the settle
            # refire, or the browser proactive refresh -- applies _refresh,
            # which bumps the generation, so a click dispatched from PAINTED
            # arrives here with a mismatched pair. Dropping it left the
            # post-click clear undone, and a refresh that then FAILED put the
            # PRE-CLICK badges back on screen (_refresh_fall_back is
            # non-destructive by design) over a screen the click had already
            # changed.
            #
            # A same-session generation BUMP is deferred, not dropped: record
            # the pre-click snapshot id and let
            # _reconcile_overlay_pending_postclick_clear decide by snapshot
            # identity when this overlay next reaches PAINTED. Every other
            # mismatch -- a different overlay session, or a pair that is not a
            # forward bump -- keeps the original drop, because the overlay the
            # click was dispatched against is genuinely gone.
            #
            # Only armed when settle_after_click is ON. With the flag OFF the
            # drop costs nothing for a click that is refused or for a machine
            # with overlay_keep_numbers ON: CLICK_COMPLETE would have started
            # the very refresh the FOCUS_CHANGE already started. It does NOT
            # cost nothing for a SUCCESSFUL click with overlay_keep_numbers
            # OFF; that case never reaches this branch in the same session,
            # because the early hide above removes the numbers first.
            self._maybe_defer_postclick_clear(
                machine=machine,
                overlay_dispatch_pair=overlay_dispatch_pair,
                current_pair=current_pair,
                preclick_snapshot_id=preclick_snapshot_id,
                overlay_dispatch_ordinal=overlay_dispatch_ordinal,
                trigger=trigger,
                trace_id=trace_id,
            )
            logger.info(
                "overlay: %s dispatched at gen=%s but machine is now "
                "gen=%s (state=%s); not refreshing a superseded/torn-down "
                "overlay from here (a same-session bump may have deferred "
                "the post-click clear above) (trace_id=%s)",
                trigger, overlay_dispatch_pair, current_pair,
                machine.state.value, trace_id,
            )
            return
        if machine.state is OverlayState.REFRESH_IN_FLIGHT:
            # wh-n29v.101.1: the click resolved against the still-visible
            # pre-click overlay while a refresh for THIS generation is in flight.
            # CLICK_COMPLETE returns HELD in refresh_in_flight (a machine no-op),
            # so feeding it now would silently drop the post-click refresh: the
            # in-flight generation would then paint a pre-click snapshot with no
            # later CLICK_COMPLETE, and the next 'click N' would resolve against
            # it. Instead record a pending post-click refresh keyed on the
            # captured pair; ``_reconcile_overlay_pending_postclick_refresh``
            # replays CLICK_COMPLETE once this generation settles into PAINTED
            # (or drops it if the overlay is superseded / torn down first).
            self._record_pending_postclick_refresh(current_pair)
            logger.info(
                "overlay: %s at matching gen=%s but machine is "
                "refresh_in_flight; deferring post-click refresh until that "
                "generation settles into painted (trace_id=%s)",
                trigger, current_pair, trace_id,
            )
            # wh-overlay-slow-uia-stale-badges.23.4: the record above is a
            # PAIR, and a same-session supersede drops it -- leaving nothing
            # that remembers a clear was owed. Arm the deferred clear beside
            # it, because it is the only record carrying the pre-click
            # snapshot id. The two never both fire: a consumed pending refresh
            # drives the machine to post_click_settling (or paints a new
            # snapshot), and both are DROP rules in
            # ``_reconcile_overlay_pending_postclick_clear``.
            self._maybe_defer_postclick_clear(
                machine=machine,
                overlay_dispatch_pair=overlay_dispatch_pair,
                current_pair=current_pair,
                preclick_snapshot_id=preclick_snapshot_id,
                overlay_dispatch_ordinal=overlay_dispatch_ordinal,
                trigger=trigger,
                trace_id=trace_id,
            )
            return
        if machine.state is OverlayState.PAUSED:
            # wh-overlay-slow-uia-stale-badges.21.2: the mic pause landed
            # between the click dispatch and its reply, so the reply arrives at
            # the matching pair (painted -> paused does not bump) while the
            # badges are hidden. Dropping the refresh here contradicts the rule
            # the pending machinery states: a deferred post-click refresh waits
            # through the paused state and is consumed when that generation
            # next reaches painted. Without the deferral the resume restore
            # repaints the PRE-CLICK list with no fresh walk behind it, and the
            # next 'click N' resolves against a list Input already consumed.
            # Record it exactly like the refresh_in_flight branch above: the
            # paused -> painted re-key in ``_apply_overlay_event`` carries the
            # key onto the restored generation, and
            # ``_reconcile_overlay_pending_postclick_refresh`` consumes it
            # there. Every other exit from paused (a stale-snapshot re-walk, a
            # hide, a destroyed window) bumps the pair or closes the session,
            # so the reconciler drops the key rather than leaking it.
            self._record_pending_postclick_refresh(current_pair)
            logger.info(
                "overlay: %s at matching gen=%s but machine is paused; "
                "deferring post-click refresh until that generation reaches "
                "painted (trace_id=%s)",
                trigger, current_pair, trace_id,
            )
            return
        if machine.state is not OverlayState.PAINTED:
            logger.info(
                "overlay: %s at matching gen=%s but machine state=%s "
                "(not painted); post-click refresh is a no-op (trace_id=%s)",
                trigger, current_pair, machine.state.value, trace_id,
            )
            return
        self._apply_overlay_event(
            OverlayEvent(kind=OverlayEventKind.CLICK_COMPLETE),
            source=f"click_complete ({trigger}) gen={current_pair}",
        )

    def _record_pending_postclick_refresh(
        self, pair: "tuple[int, int]",
    ) -> None:
        """Record a deferred post-click refresh keyed on ``pair``."""
        self._overlay_pending_postclick_refresh = pair

    def _reconcile_overlay_pending_postclick_refresh(self) -> None:
        """Consume or clear a deferred post-click refresh (wh-n29v.101.1).

        Called after EVERY overlay-machine apply (both ``_apply_overlay_event``
        and ``handle_overlay_command``), mirroring the sibling
        ``_reconcile_overlay_tracked_identity`` /
        ``_reconcile_overlay_keepalive_timer`` post-apply reconciles. Reads the
        machine's CURRENT state -- not a prev/next edge -- so it is idempotent
        and safe to call after any apply.

        A pending post-click refresh is the pair captured when a click ok arrived
        while the overlay was REFRESH_IN_FLIGHT (see
        ``_feed_click_complete_refresh``). It is resolved as follows:

          * CURRENT pair still equals the recorded pair AND the machine has
            settled into PAINTED -> the in-flight generation the click was
            dispatched against is now visible and is still a PRE-CLICK snapshot
            (the refresh fall-back paths return to PAINTED at the SAME generation,
            whether the refresh built a new snapshot or kept the prior one).
            Consume the pending refresh by replaying CLICK_COMPLETE so a FRESH
            post-click walk runs.
          * CURRENT pair still equals the recorded pair AND the machine is still
            settling (REFRESH_IN_FLIGHT, or PAUSED via a mid-flight mic-pause) ->
            keep waiting; that generation has not reached PAINTED yet. A later
            mic-resume back to PAINTED at the same pair consumes it then.
          * Anything else -- a supersede bumped the generation, a new session
            replaced the overlay, the session ended (closed), or the machine
            failed closed (error) -> the overlay the click was dispatched against
            is gone, so DROP the pending refresh; it must never fire on a
            different overlay.

        The consume is DEFERRED via ``loop.call_soon`` rather than applied
        inline: this runs from the tail of an ``_apply_overlay_event`` that just
        committed a paint-ack, and replaying CLICK_COMPLETE re-enters
        ``_apply_overlay_event``. Deferring keeps the re-entry off the current
        synchronous stack and preserves FIFO effect-batch ordering on the single
        Logic loop (the same call_soon idiom the build-response feed uses,
        wh-n29v.96.4). The deferred callback re-validates before applying, so an
        event arriving between scheduling and firing cannot drive a stale
        refresh. The pending pair is left SET at schedule time, NOT cleared here:
        the callback owns the consume / keep / drop decision against the live
        machine. Clearing at schedule time would lose the refresh if the machine
        moved to a keep-waiting state (a mic-pause -> PAUSED) between the schedule
        and the callback firing -- the callback would then see a non-PAINTED
        state with the pending already gone and could not keep it
        (wh-n29v.102.1). A redundant re-schedule is harmless because the callback
        re-validates and applies CLICK_COMPLETE at most once per generation
        (applying it moves the machine out of PAINTED at that pair, so a second
        callback no-ops).
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        pending = getattr(self, "_overlay_pending_postclick_refresh", None)
        if machine is None or pending is None:
            return
        current_pair = (machine.overlay_session_id, machine.paint_generation)
        if current_pair == pending:
            if machine.state is OverlayState.PAINTED:
                # Schedule the consume but leave the pending SET; the callback
                # clears it on a successful apply. Clearing here would lose the
                # refresh on a schedule/fire PAUSE interleave (wh-n29v.102.1).
                self._host.loop.call_soon(
                    lambda: self._apply_pending_postclick_click_complete(
                        current_pair
                    )
                )
                return
            if machine.state in (
                OverlayState.REFRESH_IN_FLIGHT, OverlayState.PAUSED,
            ):
                # Same overlay, still settling -> keep the pending refresh; it
                # consumes when this generation next reaches PAINTED.
                return
        # Superseded / new session / closed / error: the overlay the click was
        # dispatched against is gone. Drop the pending refresh so it cannot fire
        # on a different overlay.
        self._overlay_pending_postclick_refresh = None
        logger.info(
            "overlay: dropping deferred post-click refresh for gen=%s; machine "
            "moved on (now gen=%s, state=%s)",
            pending, current_pair, machine.state.value,
        )

    def _apply_pending_postclick_click_complete(
        self, pending_pair: "tuple[int, int]",
    ) -> None:
        """Deferred replay of a pending post-click CLICK_COMPLETE (wh-n29v.101.1).

        Scheduled by ``_reconcile_overlay_pending_postclick_refresh`` when the
        recorded generation reached PAINTED. The pending pair is NOT cleared at
        schedule time, so this callback owns the consume / keep / drop decision
        against the LIVE machine (wh-n29v.102.1). An event may have been processed
        between the call_soon scheduling and this firing:

          * A newer pending refresh replaced ours (the field no longer equals
            ``pending_pair``) -> do nothing; the newer pending owns the field and
            its own callback will handle it.
          * Still PAINTED at ``pending_pair`` -> consume: clear the pending and
            replay CLICK_COMPLETE. ``_apply_overlay_event`` mints its own trace id
            for the resulting refresh, exactly as the inline PAINTED feed in
            ``_feed_click_complete_refresh`` does.
          * Still at ``pending_pair`` but in a keep-waiting state
            (REFRESH_IN_FLIGHT or PAUSED -- e.g. a mic-pause landed between
            schedule and fire) -> leave the pending SET; the next apply that
            reaches PAINTED at this pair reschedules and consumes it. This is what
            stops a paused interlude from losing the refresh.
          * Otherwise (the generation was superseded, a new session replaced the
            overlay, the session ended, or the machine failed closed) -> drop the
            pending; the overlay the click was dispatched against is gone.
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if getattr(self, "_overlay_pending_postclick_refresh", None) != pending_pair:
            # A newer pending (or a clear) replaced ours after this callback was
            # scheduled; it is not ours to act on.
            return
        current_pair = (machine.overlay_session_id, machine.paint_generation)
        if current_pair == pending_pair:
            if machine.state is OverlayState.PAINTED:
                self._overlay_pending_postclick_refresh = None
                self._apply_overlay_event(
                    OverlayEvent(kind=OverlayEventKind.CLICK_COMPLETE),
                    source=f"click_complete_deferred gen={current_pair}",
                )
                return
            if machine.state in (
                OverlayState.REFRESH_IN_FLIGHT, OverlayState.PAUSED,
            ):
                # Still settling at the same overlay -> keep the pending; the
                # next reach-PAINTED reschedules it.
                logger.info(
                    "overlay: deferred post-click refresh for gen=%s still "
                    "settling (state=%s); keeping it pending",
                    pending_pair, machine.state.value,
                )
                return
        # Superseded / new session / closed / error -> drop.
        self._overlay_pending_postclick_refresh = None
        logger.info(
            "overlay: deferred post-click refresh for gen=%s dropped; machine "
            "is now gen=%s state=%s",
            pending_pair, current_pair, machine.state.value,
        )

    def _maybe_defer_postclick_clear(
        self,
        *,
        machine,
        overlay_dispatch_pair: "tuple[int, int]",
        current_pair: "tuple[int, int]",
        preclick_snapshot_id: "Optional[str]",
        overlay_dispatch_ordinal: int,
        trigger: str,
        trace_id: str,
    ) -> None:
        """Arm a deferred post-click clear for a same-session generation bump.

        Called from the gate-1 mismatch branch of
        ``_feed_click_complete_refresh``, and (since
        wh-overlay-slow-uia-stale-badges.23.4) from its matching-gate
        REFRESH_IN_FLIGHT branch, which records a pair-only pending refresh
        that a later same-session supersede drops. (The matching-gate PAUSED
        branch needs no arm: ``_apply_overlay_event`` re-keys the pending
        refresh onto the restored generation on the paused -> painted leg, and
        the stale-snapshot resume unpins the pre-click snapshot and supersedes
        it with a fresh walk.) Arms ONLY when all four hold:

          * ``settle_after_click`` is ON. With it OFF the drop costs nothing
            for a refused click or with ``overlay_keep_numbers`` ON
            (CLICK_COMPLETE would have started the same refresh the
            FOCUS_CHANGE already started). A SUCCESSFUL click with
            ``overlay_keep_numbers`` OFF never reaches this function in the
            same session: ``_feed_click_complete_refresh`` removes the numbers
            first (wh-numbers-stay-setting, item 2a).
          * The caller supplied the PRE-CLICK snapshot id. Without it there is
            no evidence to tell the two legs apart, so the old drop stands.
          * The mismatch is same-SESSION. A different session means the overlay
            the click was dispatched against is gone.
          * The machine has NOT fallen BEHIND the dispatch generation. A
            dispatch pair ahead of the machine is not an interleave this bead
            covers, and replaying into it could clear an overlay the click
            never resolved against. Equality IS admitted
            (wh-overlay-slow-uia-stale-badges.23.4): the two matching-gate
            branches call this with the machine still at the dispatch pair,
            and on the mismatch branch equality cannot occur at all -- a
            same-session pair that is not equal has an unequal generation.

        After arming, the reconcile runs inline: the intervening refresh may
        have ALREADY reached PAINTED before this reply arrived, in which case
        no further apply would run to trigger the post-apply reconcile.
        """

        if not getattr(machine, "settle_after_click", False):
            return
        if preclick_snapshot_id is None:
            return
        if overlay_dispatch_pair[0] != current_pair[0]:
            return
        if current_pair[1] < overlay_dispatch_pair[1]:
            return
        armed = getattr(self, "_overlay_pending_postclick_clear", None)
        if (
            armed is not None
            and armed[0] == current_pair[0]
            and armed[1] > overlay_dispatch_ordinal
        ):
            # wh-overlay-slow-uia-stale-badges.23.1: a NEWER click already
            # armed this slot and this reply belongs to an older dispatch
            # (the late-response window is the only way both are
            # outstanding). The pin a fall-back restores is the newer
            # click's, so the newer record is the one that can still be
            # decided; keep it.
            #
            # Compared by dispatch ORDINAL, not by generation
            # (wh-overlay-slow-uia-stale-badges.23.3): two outstanding
            # clicks can share a generation, and a generation comparison
            # is false on equality, which let the older reply win.
            logger.info(
                "overlay: %s dispatched at gen=%s (ordinal=%s) arrived "
                "after a NEWER click's deferred clear (armed at "
                "ordinal=%s, pre-click snapshot=%s) was already armed; "
                "keeping the newer record (trace_id=%s)",
                trigger, overlay_dispatch_pair, overlay_dispatch_ordinal,
                armed[1], armed[2], trace_id,
            )
            return
        self._overlay_pending_postclick_clear = (
            current_pair[0], overlay_dispatch_ordinal, preclick_snapshot_id,
        )
        logger.info(
            "overlay: %s dispatched at gen=%s arrived after a same-session "
            "refresh bumped the machine to gen=%s (state=%s); deferring the "
            "post-click clear against pre-click snapshot=%s (trace_id=%s)",
            trigger, overlay_dispatch_pair, current_pair,
            machine.state.value, preclick_snapshot_id, trace_id,
        )
        self._reconcile_overlay_pending_postclick_clear()

    def _reconcile_overlay_pending_postclick_clear(self) -> None:
        """Consume, keep, or drop a deferred post-click clear.

        Called after EVERY overlay-machine apply, beside
        ``_reconcile_overlay_pending_postclick_refresh``. Reads the machine's
        CURRENT state, so it is idempotent and safe to call after any apply.

          * Different overlay session -> DROP. The overlay the click was
            dispatched against is gone.
          * ``closed`` / ``error`` -> DROP. Nothing is on screen to clear.
          * ``post_click_settling`` -> DROP. A clear already happened; the
            stale badges this pending exists to remove are already gone.
          * ``painted`` and ``pinned_snapshot_id`` equals the recorded
            pre-click id -> CONSUME. The intervening refresh fell back, so the
            badges on screen are the stale pre-click ones.
          * ``painted`` and a DIFFERENT pin -> DROP. A genuinely new snapshot
            is painted; clearing it would be strictly worse than doing
            nothing.
          * ``painted``, the pin matches, but the display holds a DIFFERENT
            list -> KEEP (wh-overlay-slow-uia-stale-badges.23.6, changed from
            DROP to KEEP by .23.8). The pin is Logic's rollback intent, not the
            screen, and that disagreement ends when the display confirms a
            clear. The whole decision lives in
            ``_postclick_clear_verdict``.
          * Anything else (``walk_in_flight``, ``paint_in_flight``,
            ``refresh_in_flight``, ``paused``) -> KEEP. That overlay has not
            reached a decidable state yet. ``paused`` matters: the auto-hide
            fall-back leg lands there, and a later mic-resume restores the
            pinned snapshot -- if that pin is the pre-click one, the stale
            badges come back and the clear is still needed.

        The consume is DEFERRED via ``loop.call_soon`` for the same reason the
        sibling reconcile defers: this can run from the tail of an
        ``_apply_overlay_event``, and feeding CLICK_COMPLETE re-enters it. The
        pending is left SET at schedule time; the callback owns the decision
        against the live machine.
        """

        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        pending = getattr(self, "_overlay_pending_postclick_clear", None)
        if machine is None or pending is None:
            return
        pending_session, _dispatch_ordinal, preclick_snapshot_id = (
            pending
        )
        if machine.overlay_session_id != pending_session:
            self._drop_pending_postclick_clear(pending, machine, "new session")
            return
        state = machine.state
        if state in (
            OverlayState.CLOSED,
            OverlayState.ERROR,
            OverlayState.POST_CLICK_SETTLING,
        ):
            self._drop_pending_postclick_clear(pending, machine, state.value)
            return
        if state is not OverlayState.PAINTED:
            return
        verdict = self._postclick_clear_verdict(
            machine, preclick_snapshot_id,
        )
        if verdict is not None:
            action, reason = verdict
            if action == "drop":
                self._drop_pending_postclick_clear(pending, machine, reason)
            else:
                self._keep_pending_postclick_clear(pending, machine, reason)
            return
        self._host.loop.call_soon(
            lambda: self._apply_pending_postclick_clear(pending)
        )

    def _drop_pending_postclick_clear(
        self, pending: "tuple[int, int, str]", machine, reason: str,
    ) -> None:
        """Clear the pending record and log why, in one place."""

        self._overlay_pending_postclick_clear = None
        logger.info(
            "overlay: dropping deferred post-click clear for session=%s "
            "pre-click snapshot=%s (%s); machine is now session=%s gen=%s "
            "state=%s pinned=%s",
            pending[0], pending[2], reason, machine.overlay_session_id,
            machine.paint_generation, machine.state.value,
            machine.pinned_snapshot_id,
        )

    def _overlay_note_display_teardown(
        self, sid: int, gen: int, state: str,
    ) -> None:
        """Retire ``_overlay_last_enqueued_paint`` once the screen is empty.

        wh-overlay-slow-uia-stale-badges.23.8. The record says which list Logic
        handed to the display, and the deferred post-click clear reads it to
        avoid erasing a list the user can see. A ``cleared`` or ``expired``
        acknowledgement says the display tore every badge window down --
        ``OverlayPaintTeardown.clear`` and ``.expire_lease``
        (``overlay_paint/teardown.py``) both emit that ack only after
        ``destroy_all`` succeeded, and park it otherwise. Once the
        screen is empty no list exists for a clear to erase, so the record has
        nothing left to protect and must not keep an owed clear dormant for
        ever.

        Only the SAME session, and only at or after the recorded generation: an
        older teardown ack cannot describe a newer list.

        ``painted`` and ``failed`` acks are deliberately ignored. A ``painted``
        ack could only rewrite the record with what it already holds, since
        ``_overlay_dispatch_paint`` records every paint it hands over at the
        same pair the ack echoes back. A ``failed`` ack destroys the failed
        monitors' windows but says nothing about the others, so the safe
        reading is that the recorded list may still be visible.
        """

        if state not in ("cleared", "expired"):
            return
        enqueued = getattr(self, "_overlay_last_enqueued_paint", None)
        if (
            enqueued is not None
            and enqueued[0] == sid
            and gen >= enqueued[1]
        ):
            self._overlay_last_enqueued_paint = None

    def _postclick_clear_verdict(
        self, machine, preclick_snapshot_id: str,
    ) -> "tuple[str, str] | None":
        """Decide a deferred post-click clear: consume, drop, or keep dormant.

        ONE definition of the decision, called from both consumers
        (``_reconcile_overlay_pending_postclick_clear`` and the live recheck in
        ``_apply_pending_postclick_clear``). Both had the same test inline and
        both were wrong the same way, so a single function is what keeps them
        from drifting apart again.

        Returns ``None`` to CONSUME, or ``(action, reason)`` where action is
        ``"drop"`` or ``"keep"``. Both non-consume answers mean "clearing right
        now would erase a list the user can see, which is strictly worse than
        leaving stale badges up". They differ in whether the reason can expire:

        * ``"drop"`` -- the machine's pin is a DIFFERENT snapshot. A genuinely
          fresh list is painted (the original Option C rule). The click's own
          list is gone from the machine's bookkeeping, so nothing later brings
          the owed clear back into meaning.
        * ``"keep"`` -- the pin matches, but the display holds a DIFFERENT list
          (wh-overlay-slow-uia-stale-badges.23.6). The pin alone cannot see
          this: ``_refresh_build_ok`` pins the new snapshot and emits its paint
          while STAYING in refresh_in_flight, and both ``_refresh_fall_back``
          (deadline, failed build) and ``_refresh_supersede`` (a second focus
          change) then RESTORE the prior pin without sending any clear.

        The ``"keep"`` answer must NOT discard the record
        (wh-overlay-slow-uia-stale-badges.23.8). That evidence expires: once
        the display confirms a clear, ``_overlay_note_display_teardown``
        forgets the other list, and a mic-resume re-paints the machine's pin --
        the pre-click list, whose click already succeeded. Discarding the
        record left those stale badges with nothing owed to remove them. The
        record stays armed and every later reconcile decides again; the
        session, closed, error and post_click_settling rules still drop it.
        """

        if machine.pinned_snapshot_id != preclick_snapshot_id:
            return ("drop", "a fresh snapshot is painted")
        enqueued = getattr(self, "_overlay_last_enqueued_paint", None)
        if (
            enqueued is not None
            and enqueued[0] == machine.overlay_session_id
            and enqueued[2] != preclick_snapshot_id
        ):
            return (
                "keep",
                f"the display process holds a later list ({enqueued[2]}) "
                f"at gen={enqueued[1]}",
            )
        return None

    def _keep_pending_postclick_clear(
        self, pending: "tuple[int, int, str]", machine, reason: str,
    ) -> None:
        """Log why a deferred post-click clear waits, in one place."""

        logger.info(
            "overlay: holding the deferred post-click clear for session=%s "
            "pre-click snapshot=%s (%s); machine is now session=%s gen=%s "
            "state=%s pinned=%s",
            pending[0], pending[2], reason, machine.overlay_session_id,
            machine.paint_generation, machine.state.value,
            machine.pinned_snapshot_id,
        )

    def _apply_pending_postclick_clear(
        self, pending: "tuple[int, int, str]",
    ) -> None:
        """Deferred replay of CLICK_COMPLETE for a pending post-click clear.

        Scheduled by ``_reconcile_overlay_pending_postclick_clear`` once the
        overlay reached PAINTED still holding the PRE-CLICK pin. Re-validates
        against the LIVE machine first: an event may have been processed
        between the ``call_soon`` scheduling and this firing, and the same
        three outcomes apply as in the reconcile -- consume, keep, or drop.
        Re-validating here is what stops a stale callback from clearing an
        overlay that has since been refreshed or replaced.
        """

        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if getattr(self, "_overlay_pending_postclick_clear", None) != pending:
            # A newer pending (or a drop) replaced ours after this callback was
            # scheduled; it is not ours to act on.
            return
        pending_session, _dispatch_ordinal, preclick_snapshot_id = (
            pending
        )
        if machine.overlay_session_id != pending_session:
            self._drop_pending_postclick_clear(pending, machine, "new session")
            return
        state = machine.state
        if state in (
            OverlayState.CLOSED,
            OverlayState.ERROR,
            OverlayState.POST_CLICK_SETTLING,
        ):
            self._drop_pending_postclick_clear(pending, machine, state.value)
            return
        if state is not OverlayState.PAINTED:
            # Still settling at the same overlay -> keep the pending; the next
            # reach-PAINTED reschedules it.
            logger.info(
                "overlay: deferred post-click clear for session=%s still "
                "settling (state=%s); keeping it pending",
                pending_session, state.value,
            )
            return
        verdict = self._postclick_clear_verdict(
            machine, preclick_snapshot_id,
        )
        if verdict is not None:
            action, reason = verdict
            if action == "drop":
                self._drop_pending_postclick_clear(pending, machine, reason)
            else:
                self._keep_pending_postclick_clear(pending, machine, reason)
            return
        self._overlay_pending_postclick_clear = None
        logger.info(
            "overlay: replaying the post-click clear for session=%s; the "
            "intervening refresh fell back to the pre-click snapshot=%s",
            pending_session, preclick_snapshot_id,
        )
        self._apply_overlay_event(
            OverlayEvent(kind=OverlayEventKind.CLICK_COMPLETE),
            source=f"click_complete_deferred_clear session={pending_session}",
        )

    def _hold_click_n(
        self, *, number: Optional[int], spoken: str, trace_id: str,
        gesture=None,
    ) -> None:
        """Hold a 'click N' that arrived during a transition, up to 200ms (wh-n29v.95).

        The v4 "queue or drop" rule: a numeric ``click N`` that lands while the
        machine is in a transitional state (walk_in_flight / paint_in_flight /
        paused) cannot resolve yet. Arm a single 200ms hold timer; when it
        fires, RE-READ the machine and re-route through the same resolver
        ``forward_click_element`` used. If the overlay has reached a resolvable
        state, dispatch the click; otherwise emit the "numbers did not paint
        yet" notice. NEVER a silent drop (criterion 1, wh-n29v.18.2). Only one
        hold is live at a time; a second held click cancels and re-arms it.

        wh-n29v.96.3: stamp the machine's CURRENT (overlay_session_id,
        paint_generation) onto the held click so ``_resolve_held_click_n`` can
        reject it if a supersede or new session reached a resolvable state during
        the hold -- otherwise the held N would resolve against a snapshot the
        user never saw and dispatch a real click against the WRONG control.
        """
        existing = getattr(self, "_overlay_hold_timer", None)
        if existing is not None:
            existing.cancel()
        machine = self.click_overlay_state
        armed_pair = (machine.overlay_session_id, machine.paint_generation)
        delay_s = 0.2
        self._overlay_hold_timer = self._host.loop.call_later(
            delay_s,
            lambda: self._resolve_held_click_n(
                number=number, spoken=spoken, trace_id=trace_id,
                armed_pair=armed_pair, gesture=gesture,
            ),
        )
        # wh-overlay-slow-uia-stale-badges.8: mirror the armed pair (and the
        # spoken text for the cancel notice) where a concurrent badge-click
        # success can see it -- call_later gives no way to read the lambda's
        # captured pair back out of the handle.
        self._overlay_hold_armed = (armed_pair, spoken)

    def _resolve_held_click_n(
        self, *, number: Optional[int], spoken: str, trace_id: str,
        armed_pair: "tuple[int, int]", gesture=None,
    ) -> None:
        """Hold-timer callback: re-read the machine and resolve-or-notice (wh-n29v.95).

        Runs on the Logic loop. Re-routes the held number through
        ``route_click_n`` against the CURRENT machine state. A SNAPSHOT_ITEM
        decision dispatches the click; any other decision (still HELD, NOTICE,
        or BY_NAME) means the overlay never reached a resolvable painted state
        within the hold window, so it surfaces the "numbers did not paint yet"
        notice rather than dropping the click silently.

        wh-n29v.96.3 generation gate: before dispatching SNAPSHOT_ITEM, require
        the machine's CURRENT (overlay_session_id, paint_generation) to still
        equal ``armed_pair`` -- the pair captured when the hold was armed. A
        supersede (same session, new generation) or a new session that reached a
        resolvable state within the 200ms hold would otherwise make the held N
        resolve against a DIFFERENT snapshot the user never saw. On a mismatch,
        surface the numbers_not_showing notice instead of clicking the wrong
        control.
        """
        self._overlay_hold_timer = None
        # wh-overlay-slow-uia-stale-badges.8: the hold is no longer armed, so
        # drop its cancel-visible mirror too.
        self._overlay_hold_armed = None
        from services.wheelhouse.speech.overlay_click_router import (
            RoutingKind,
            route_click_n,
        )
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = self.click_overlay_state
        current_pair = (machine.overlay_session_id, machine.paint_generation)
        if current_pair != armed_pair:
            # The overlay was superseded / a new session started during the hold.
            # Resolving N now would target a snapshot the user never saw, so do
            # NOT dispatch a click -- surface the numbers-not-showing notice.
            logger.info(
                "overlay: held click N=%s armed at gen=%s but machine is now "
                "gen=%s (state=%s); rejecting to avoid a wrong-overlay click "
                "(trace_id=%s)",
                number, armed_pair, current_pair, machine.state.value, trace_id,
            )
            self.forward_click_notice(
                outcome="execution_failed", reason="numbers_not_showing",
                matched_name=None, matched_names=(), spoken_name=spoken,
                snapshot_id=None, trace_id=trace_id,
            )
            return
        decision = route_click_n(
            state=machine.state,
            parsed_number=number,
            cache=self.click_snapshot_summary_cache,
            pinned_snapshot_id=machine.pinned_snapshot_id,
            prior_pinned_snapshot_id=machine.prior_pinned_snapshot_id,
            prior_pin_deferred=machine.prior_pin_deferred,
            visible_window_is_foreground=(
                self._overlay_refresh_visible_window_is_foreground(machine)
                if machine.state is OverlayState.REFRESH_IN_FLIGHT
                else None
            ),
        )
        if decision.kind is RoutingKind.SNAPSHOT_ITEM:
            logger.info(
                "overlay: held click N=%s resolved after hold -> "
                "snapshot_item snapshot=%r item=%r (state=%s, trace_id=%s)",
                number, decision.snapshot_id, decision.item_id,
                machine.state.value, trace_id,
            )
            self._dispatch_snapshot_item_click(
                snapshot_id=decision.snapshot_id,
                item_id=decision.item_id,
                trace_id=trace_id,
                gesture=gesture,
            )
            return
        if decision.kind is RoutingKind.NOTICE:
            logger.info(
                "overlay: held click N=%s -> notice reason=%r after hold "
                "(state=%s, trace_id=%s)",
                number, decision.reason, machine.state.value, trace_id,
            )
            self.forward_click_notice(
                outcome="execution_failed", reason=decision.reason,
                matched_name=None, matched_names=(), spoken_name=spoken,
                snapshot_id=decision.snapshot_id, trace_id=trace_id,
            )
            return
        # Still HELD (overlay never painted within the hold), or BY_NAME (the
        # overlay tore down to closed during the hold). Either way the numbers
        # are not showing for this pick: surface the notice, never a silent drop
        # (criterion 1, wh-n29v.18.2).
        logger.info(
            "overlay: held click N=%s did not become resolvable within the "
            "hold (decision=%s, state=%s); firing numbers-not-showing notice "
            "(trace_id=%s)",
            number, decision.kind.value, machine.state.value, trace_id,
        )
        self.forward_click_notice(
            outcome="execution_failed", reason="numbers_not_showing",
            matched_name=None, matched_names=(), spoken_name=spoken,
            snapshot_id=None, trace_id=trace_id,
        )

    def _close_grid_for_numbered_overlay(self, trace_id: str) -> None:
        """Close the mouse grid because the numbered overlay is opening.

        Mutual exclusion (wh-grid-speech-routing): called from the
        handle_overlay_command 'show' branch and the ambiguous auto-open
        path BEFORE the overlay machine transitions. Applies
        ``NUMBERED_OVERLAY_OPENED`` (whose only effect is CLEAR_GRID) and
        sends the clear synchronously. Never raises; a closed grid is a
        no-op.
        """
        grid = self._host.grid_overlay_state
        if grid is None or not grid.is_open:
            return
        from services.wheelhouse.grid_overlay_state import (
            GridEffectKind,
            GridEvent,
            GridEventKind,
        )

        try:
            result = grid.apply(
                GridEvent(GridEventKind.NUMBERED_OVERLAY_OPENED)
            )
        except Exception as exc:  # noqa: BLE001 -- never block the overlay
            logger.warning(
                "grid: close-for-numbered-overlay failed (trace_id=%s): %s",
                trace_id, exc,
            )
            return
        logger.info(
            "grid: closed for numbered overlay (outcome=%s, trace_id=%s)",
            result.outcome.value, trace_id,
        )
        for effect in result.effects:
            if effect.kind is GridEffectKind.CLEAR_GRID:
                from shared.clear_grid import ClearGridEvent

                self._host.put_grid_gui_event(
                    ClearGridEvent().to_dict(), trace_id,
                )

    async def handle_overlay_command(
        self, command: str, trace_id: str,
    ) -> "Optional[asyncio.Task]":
        """Apply a 'show numbers' / 'hide numbers' voice command (wh-n29v.17).

        Delegated to by ``ActionFunctions.show_overlay_command`` /
        ``hide_overlay_command``. Gated on BOTH ``click_config.enabled`` AND
        ``click_config.overlay_enabled_effective``: the overlay runs only when
        voice clicking is on AND the overlay itself is effectively enabled. A
        bad overlay key disables only the overlay (``overlay_enabled_effective``
        False) while by-name click stays operative; a valid ``enabled=false``
        master opt-out leaves ``overlay_enabled_effective`` True, so the
        ``enabled`` term is still required. This matches the Input-side overlay
        walk gate
        (``OverlaySnapshotActions._get_overlay_walk_finder``), which
        requires the ``enabled``-gated finder AND ``overlay_enabled_effective``,
        so the Logic and Input processes agree on whether the overlay is on.
        When the overlay is not active the commands are a no-op (no machine
        transition, no effects). Applies
        ``SHOW_NUMBERS`` or ``HIDE_NUMBERS`` to ``self.click_overlay_state`` and
        hands the returned ordered effects to the ``_perform_overlay_effects``
        stub seam. The state TRANSITION happens here (the machine is pure and
        single-thread-owned by this Logic loop); PERFORMING the effects is the
        integration bead's job.
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )

        if not (
            self._host.click_config.enabled
            and self._host.click_config.overlay_enabled_effective
        ):
            logger.debug(
                "overlay: %r ignored, numbered overlay not active "
                "(enabled=%s, overlay_invalid_key=%s, trace_id=%s)",
                command, self._host.click_config.enabled,
                self._host.click_config.overlay_invalid_key, trace_id,
            )
            return

        if command == "show":
            # wh-mouse-grid.1.29: same fence as the grid-open path -- a
            # stale grid click/move/drag must not act under the freshly
            # painted numbered overlay. An unconfirmed channel withholds
            # the show.
            if not await self._host.grid_pointer_channel_quiescent(
                trace_id=trace_id,
            ):
                logger.warning(
                    "overlay: show withheld, grid pointer channel "
                    "unconfirmed after an earlier timeout (trace_id=%s)",
                    trace_id,
                )
                self.forward_click_notice(
                    outcome="execution_failed",
                    reason="grid_pointer_pending",
                    matched_name=None,
                    matched_names=(),
                    spoken_name="",
                    snapshot_id=None,
                    trace_id=trace_id,
                )
                return None
            kind = OverlayEventKind.SHOW_NUMBERS
            # wh-grid-speech-routing: mutual exclusion. Opening the numbered
            # overlay closes the mouse grid BEFORE the overlay machine moves,
            # so a bare number spoken next resolves against exactly one
            # consumer. Never raises; a closed grid is a no-op.
            self._close_grid_for_numbered_overlay(trace_id)
        elif command == "hide":
            kind = OverlayEventKind.HIDE_NUMBERS
        else:
            logger.warning(
                "overlay: unknown command %r ignored (trace_id=%s)",
                command, trace_id,
            )
            return

        prev_state = self.click_overlay_state.state
        result = self.click_overlay_state.apply(OverlayEvent(kind))
        if command == "show":
            # wh-numbers-stay-setting (decision D5): see
            # ``_overlay_click_show_counts``.
            self._overlay_show_numbers_applied = (
                getattr(self, "_overlay_show_numbers_applied", 0) + 1
            )
        logger.info(
            "overlay: %s -> state=%s outcome=%s (%d effect(s), trace_id=%s)",
            command, self.click_overlay_state.state.value,
            result.outcome.value, len(result.effects), trace_id,
        )
        # wh-mouse-grid.1.20, wh-overlay-slow-uia-stale-badges.2.2.4 and .24:
        # wake a parked build this command just made stale, whatever the
        # command was.
        #
        # A build dispatched for the session a 'hide numbers' just tore down,
        # the settle build a 'show numbers' spoken in the settling gap
        # superseded, and the walk or refresh build a re-said 'show numbers'
        # superseded (.24: WALK_IN_FLIGHT and REFRESH_IN_FLIGHT SHOW_NUMBERS)
        # are all in the same position: parked on an Input round trip holding
        # ``_overlay_effect_lock`` for up to the build's own awaited window
        # (screen_read_timeout_ms for a walk build, response_timeout_ms for
        # AUTO_OPEN). Waking it is what lets the batch this command just
        # scheduled -- the fresh walk, or the hide's clear and the grid paint
        # awaiting it on the CLOSE_NUMBERED_OVERLAY path -- ship promptly
        # instead of waiting out a dead request's deadline.
        #
        # One unconditional call replaces the two guarded calls this had. The
        # helper decides for itself whether the parked send is still
        # consumable (state CLOSED, or a current pair that no longer matches
        # the parked one), so a command the machine ignored wakes nothing.
        self._abort_inflight_overlay_build()
        effects_task: Optional[asyncio.Task] = None
        if result.effects:
            effects_task = self._perform_overlay_effects(
                result.effects, trace_id=trace_id,
            )
        # wh-overlay-voice-command-reconcilers: this direct apply path must
        # release a paused window's hook before a later session can register
        # its own, just as _apply_overlay_event does.
        self._reconcile_overlay_destroy_hook(
            self.click_overlay_state.state is OverlayState.PAUSED
        )
        if (
            self.click_overlay_state.state is OverlayState.CLOSED
            and prev_state is not OverlayState.CLOSED
        ):
            self._overlay_focus_debouncer.reset()
        # wh-n29v.95 part 5: clear the tracked identity when the command (e.g.
        # hide) drove the machine to closed. hide_numbers does NOT go through
        # _apply_overlay_event, so reconcile here too.
        self._reconcile_overlay_tracked_identity()
        # wh-overlay-slow-uia-stale-badges.8: the same show/hide commands move
        # the machine in and out of PAINTED/CLOSED, so the visible-list pair
        # must be reconciled on this apply path too.
        self._reconcile_overlay_visible_painted_pair()
        # wh-n29v.96.2: arm/cancel the periodic retained-snapshot keepalive to
        # match whether the resulting state still retains the pin.
        self._reconcile_overlay_keepalive_timer()
        # wh-n29v.101.1: clear any deferred post-click refresh when a show/hide
        # command supersedes or tears down the overlay it was recorded against.
        self._reconcile_overlay_pending_postclick_refresh()
        # wh-overlay-slow-uia-stale-badges.15: its sibling, for the
        # deferred post-click CLEAR.
        self._reconcile_overlay_pending_postclick_clear()
        # wh-n29v.114.1: hide_numbers can drive the machine to closed while an
        # auto-open item_id_filter is still stashed; this path bypasses
        # _apply_overlay_event, so clear the stash here too (shared helper).
        self._reconcile_overlay_auto_open_filter()
        # wh-overlay-nested-dupes.1.4: hide_numbers can drive the machine to
        # closed while a settle re-fire from a coalesced foreground/menu event
        # is still pending; cancel it here too, or an immediate 'show numbers'
        # lets the stale timer restart the fresh session's build.
        self._reconcile_overlay_settle_refire()
        # wh-overlay-refresh-after-mouse: "show numbers" opens the mouse
        # refresh and "hide numbers" closes it; this path bypasses
        # _apply_overlay_event.
        self._reconcile_overlay_pointer_refresh()
        # wh-overlay-slow-uia-stale-badges.4.1.1: a re-said "show numbers"
        # over painted badges starts a refresh from THIS path, which never
        # reaches _apply_overlay_event, so the renumber guard's entry pin is
        # recorded here too. Runs before the reset below so a hide that ends
        # the session still clears it.
        self._reconcile_overlay_repaint_entry_pin(prev_state)
        # wh-overlay-fixqueue-review.1/.2: hide_numbers ends the session, so
        # the proactive-refresh back-off and renumber guard reset here too.
        self._reconcile_overlay_browser_refresh_reset()
        # wh-overlay-slow-uia-stale-badges.21.8: 'hide numbers' and a re-said
        # 'show numbers' both move the machine out of a PAINTED state that may
        # have scheduled an audited restore paint, and this path bypasses
        # _apply_overlay_event, so retire the entitlement and its audit here too.
        self._reconcile_overlay_restore_paint_entitlement()
        # wh-mouse-grid.1.17: the grid's CLOSE_NUMBERED_OVERLAY path must not
        # queue paint_grid before this command's clear batch has shipped; hand
        # it the scheduled dispatch task so it can await batch completion.
        return effects_task

    async def handle_overlay_state_changed(self, command) -> None:
        """Consume the GUI's ``overlay_state_changed`` paint-ack (wh-n29v.67).

        After the GUI applies / fails / clears a numbered-overlay paint, it
        emits ``overlay_state_changed { state, overlay_session_id,
        paint_generation, monitor_ids, snapshot_id }`` on the
        commands_to_logic_queue. This is the Logic-side consumer that feeds the
        paint-acknowledgement into the held ``ClickOverlayStateMachine`` instead
        of letting it fall through as an unknown GUI action.

          1. Validate the payload via :func:`safe_parse`. A malformed payload
             (``OverlayStateChangedEventSchemaError`` -> ``ValueError``) is
             logged and dropped, per wh-uf54, so a version-skewed sender cannot
             crash the GUI command listener.
          2. Early-return when the numbered overlay is not active -- gated on
             BOTH ``click_config.enabled`` AND
             ``click_config.overlay_enabled_effective``, matching the focus-hook
             handlers (``_on_overlay_foreground_change`` /
             ``_on_overlay_focused_hwnd_destroyed``) and the
             ``handle_overlay_command`` / Input-side overlay-walk gate so the
             Logic and Input processes agree on whether the overlay is on.
          3. Map the wire ``state`` string to the machine's ``PaintAckState``
             (the schema's closed set and ``PaintAckState`` have identical
             members, so a parsed event always maps cleanly).
          4. Build a ``PAINT_ACK`` ``OverlayEvent`` carrying the wire's
             ``overlay_session_id`` / ``paint_generation`` / ``snapshot_id`` and
             apply it THROUGH ``_apply_overlay_event`` so the effect hand-off
             (``_perform_overlay_effects``), the session-end debouncer reset, and
             the transient-destroy-hook reconciliation are reused, not
             duplicated.

        The stale-generation drop is the machine's OWN concern: ``apply`` rejects
        a ``PAINT_ACK`` whose ``(overlay_session_id, paint_generation)`` does not
        equal the active pair as ``STALE_GENERATION`` (no state change, no
        effects) BEFORE the transition table. This handler copies the wire pair
        onto the event and does NOT re-implement a generation check.
        """
        from services.wheelhouse.shared.ipc_schema_validation import safe_parse
        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            PaintAckState,
        )

        ev = safe_parse(
            OverlayStateChangedEvent.from_dict,
            command,
            log_label="overlay_state_changed",
        )
        if ev is None:
            return  # already logged

        if not (
            self._host.click_config.enabled
            and self._host.click_config.overlay_enabled_effective
        ):
            logger.debug(
                "overlay: overlay_state_changed %r ignored, numbered overlay "
                "not active (enabled=%s, overlay_invalid_key=%s)",
                ev.state, self._host.click_config.enabled,
                self._host.click_config.overlay_invalid_key,
            )
            return

        # Degrade, do not die. This handler runs as a
        # create_task_with_error_handling background task whose done-callback
        # (_handle_task_completion) calls request_shutdown() on ANY uncaught
        # exception. A paint-ack must never be able to restart the whole Logic
        # process, so wrap the post-parse body in the same degrade-don't-die
        # posture _handle_snapshot_item_clicked uses (wh-9f3t.69.3): an
        # unexpected error here logs and drops the ack rather than escalating to
        # a process-wide shutdown. safe_parse above already handles the
        # malformed-payload (ValueError) path; this guard covers the two
        # remaining unprotected operations -- the PaintAckState mapping (a future
        # schema/enum drift would surface as a ValueError, since the two enums
        # live in separate files) and the _apply_overlay_event hand-off, whose
        # effects seam becomes real cross-process IPC in the integration slice
        # (wh-uf54 / wh-n29v.68.1).
        try:
            # wh-overlay-slow-uia-stale-badges.9 / .18.1: feed every ack to
            # the clear-ack watchdog. An ack at a STRICTLY greater pair
            # proves the FIFO GUI queue consumed the pending clear
            # (dispatch is pair-monotonic); an EQUAL-pair ack resolves only
            # when its state is cleared/expired (the badges at that pair
            # are gone). The state string is the schema's closed set, so it
            # is passed as-is.
            self._overlay_note_clear_ack(
                ev.overlay_session_id, ev.paint_generation, ev.state,
            )
            # wh-overlay-slow-uia-stale-badges.21.3: and to the restore-paint
            # delivery audit, which resolves on a painted ack at the audited
            # pair and recovers on a failed one.
            self._overlay_note_restore_paint_ack(
                ev.overlay_session_id, ev.paint_generation, ev.state,
            )
            # wh-overlay-slow-uia-stale-badges.23.8: and to the record of
            # the list the display holds, which the deferred post-click clear
            # reads. A confirmed teardown empties the screen and retires it.
            self._overlay_note_display_teardown(
                ev.overlay_session_id, ev.paint_generation, ev.state,
            )

            if ev.state == "expired":
                # The GUI's badge lease ran out and it tore the badges down
                # itself. While PAINTED in the same session this is the
                # real fault when the ack pair is the machine's pair OR the
                # recorded VISIBLE pair (wh-overlay-slow-uia-stale-badges
                # .18.2: a failed refresh leaves the machine one generation
                # ahead of the badges the user saw, and the GUI's lease --
                # and its expiry report -- carry the visible pair). The
                # user lost badges Logic still believed visible. Everywhere
                # else it is bookkeeping -- the lease cleaned up after a
                # clear whose loss the deadline watchdog already reported.
                from services.wheelhouse.click_overlay_state import (
                    OverlayState,
                )

                machine = getattr(self, "click_overlay_state", None)
                ack_pair = (ev.overlay_session_id, ev.paint_generation)
                machine_pair = (
                    (machine.overlay_session_id, machine.paint_generation)
                    if machine is not None else None
                )
                visible_pair = getattr(
                    self, "_overlay_visible_painted_pair", None,
                )
                if (
                    machine is not None
                    and machine.state is OverlayState.PAINTED
                    and ev.overlay_session_id == machine.overlay_session_id
                    and ack_pair in (machine_pair, visible_pair)
                ):
                    if ack_pair == machine_pair:
                        logger.error(
                            "overlay: the display process expired the badge "
                            "lease and removed the numbered badges while "
                            "Logic still had them painted at generation "
                            "(%s,%s). Logic closes the overlay session to "
                            "match; say 'show numbers' to paint again.",
                            ev.overlay_session_id, ev.paint_generation,
                        )
                        # wh-overlay-slow-uia-stale-badges.18.3: this ERROR
                        # is the report for the incident. Mark the machine
                        # pair as reported BEFORE the normal apply below
                        # closes the machine and dispatches the defensive
                        # echo clear at this same pair, so the echo's own
                        # deadline (the GUI is gone and never acks it)
                        # downgrades to WARNING instead of a second popup.
                        self._overlay_clear_fault_reported_pair = machine_pair
                    else:
                        logger.error(
                            "overlay: the display process expired the badge "
                            "lease and removed the numbered badges at "
                            "generation (%s,%s) after a failed refresh left "
                            "Logic's pair out of step at (%s,%s). Logic "
                            "closes the overlay session to match; say "
                            "'show numbers' to paint again.",
                            ev.overlay_session_id, ev.paint_generation,
                            machine.overlay_session_id,
                            machine.paint_generation,
                        )
                        # Same double-report guard as above: the synthetic
                        # hide dispatches the machine's clear at the
                        # MACHINE pair, so mark that pair before the hide.
                        self._overlay_clear_fault_reported_pair = machine_pair
                        # The normal apply below is stale-gated (the ack
                        # carries the visible pair, not the machine pair),
                        # so it cannot close the machine. Close it with a
                        # synthetic hide through the standard path: the
                        # machine's own hide cell dispatches the clear and
                        # the unpin. The stale-gated expired ack that still
                        # applies afterwards lands in CLOSED -- harmless
                        # bookkeeping.
                        self._apply_overlay_event(
                            OverlayEvent(kind=OverlayEventKind.HIDE_NUMBERS),
                            source="overlay-lease-expired-reconcile",
                        )
                else:
                    logger.warning(
                        "overlay: badge lease expiry reported for "
                        "generation (%s,%s); bookkeeping only (the display "
                        "process cleaned up badges Logic already considered "
                        "gone).",
                        ev.overlay_session_id, ev.paint_generation,
                    )

            # The schema's closed _ALLOWED_STATE set
            # ('painted'/'failed'/'cleared'/'expired') mirrors PaintAckState
            # member-for-member, so a parsed event always maps to a valid
            # PaintAckState. PaintAckState(ev.state) does the lookup by value.
            paint_state = PaintAckState(ev.state)

            # ev.monitor_ids is intentionally NOT forwarded onto the
            # OverlayEvent (wh-n29v.70.1). It reports which monitors the GUI
            # actually painted, but the machine targets monitors from its own
            # dispatch (the paint/clear effects it emits), not from the ack, and
            # clear is idempotent -- clearing a monitor with no overlay is a safe
            # no-op -- so the machine never needs the ack's monitor set to route
            # effects. monitor_ids stays available here at the Logic boundary for
            # diagnostic logging if the integration bead (wh-overlay-click-
            # integration) later needs per-monitor reconciliation; it would add
            # the field to OverlayEvent/Effect at that point. Carrying an unused
            # field through the machine now would be its own contract hazard.
            self._apply_overlay_event(
                OverlayEvent(
                    kind=OverlayEventKind.PAINT_ACK,
                    overlay_session_id=ev.overlay_session_id,
                    paint_generation=ev.paint_generation,
                    snapshot_id=ev.snapshot_id,
                    paint_state=paint_state,
                ),
                source=(
                    f"overlay_state_changed state={ev.state} "
                    f"gen=({ev.overlay_session_id},{ev.paint_generation})"
                ),
            )
        except Exception:
            logger.exception(
                "overlay: overlay_state_changed handler dropped an "
                "unexpected error (state=%r, gen=(%s,%s)); degrading instead "
                "of escalating to a Logic shutdown.",
                ev.state, ev.overlay_session_id, ev.paint_generation,
            )
            return

    def _abort_inflight_overlay_build(self) -> None:
        """Wake a parked build the machine can no longer consume.

        (wh-mouse-grid.1.20.) ``_overlay_dispatch_build`` parks this event on
        the controller while it awaits Input; a caller sets it after
        committing a machine transition, so the stale build abandons its
        wait (feeding ``build_ok=False``, which the machine's closed-state /
        stale-generation gates drop) and releases ``_overlay_effect_lock``
        within milliseconds instead of after the build's own awaited window
        (up to screen_read_timeout_ms for a walk build, response_timeout_ms
        for AUTO_OPEN). No event parked (no build awaiting) is a no-op.

        wh-overlay-slow-uia-stale-badges.2.2.4: the wake is PAIR-AWARE. Every
        caller runs immediately after ``machine.apply`` with no await between,
        so the parked build is always the pre-transition one -- but the
        transition does not always supersede it (a rejected stale event leaves
        the pair alone). Waking a build the machine still wants would throw
        away a live read, so this refuses unless the machine has actually left
        that build behind: the state is CLOSED (a hide keeps the pair) or the
        current pair no longer matches the parked one. A parked send with no
        recorded pair falls through to the unconditional wake, which is what
        this helper did before the pair existed.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        abort = getattr(self, "_overlay_build_abort", None)
        if abort is None:
            return
        parked = getattr(self, "_overlay_build_abort_pair", None)
        machine = getattr(self, "click_overlay_state", None)
        if parked is not None and machine is not None:
            still_consumable = (
                machine.state is not OverlayState.CLOSED
                and (
                    machine.overlay_session_id, machine.paint_generation,
                ) == parked
            )
            if still_consumable:
                return
        abort.set()

    async def _wait_for_open_sentence_end(
        self, *, sid: int, gen: int, action: str, trace_id: str,
    ) -> None:
        """Hold a screen read until the open dictation sentence ends.

        (wh-overlay-slow-uia-stale-badges.7.) A read requested while the
        user is mid-sentence must not jump the queue: the read blocks
        Input's ONE command loop, so dictation words spoken after it
        would be refused by the read gate and lost. Waits, bounded by
        ``_overlay_read_sentence_wait_max_s`` (default
        ``_OVERLAY_READ_SENTENCE_WAIT_MAX_S``), on the speech
        processor's ``sentence_closed_event``. Also parks the build
        abort event so a hide committed during the wait wakes it at
        once; the caller's pre-send staleness check then skips the send.
        No processor reachable, a non-Event attribute (test fixtures),
        or no open sentence all return immediately.
        """
        event = self._host.speech_sentence_closed_event()
        if not isinstance(event, asyncio.Event) or event.is_set():
            return
        # wh-overlay-slow-uia-stale-badges.7.1.4: this batch may have
        # sat QUEUED behind _overlay_effect_lock while a hide or
        # supersede committed. The committer's single
        # _abort_inflight_overlay_build call ran BEFORE this build
        # parked its abort, so that wake is lost for good -- nothing
        # would ever set the abort parked below, and the wait would
        # sleep out its full bound holding the lock while the hide's
        # clear batch queues behind it. Same predicate as the
        # caller's pre-send staleness check; no await sits between
        # here and the park, so a commit cannot slip through the gap
        # (it would find the abort parked and wake it).
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is not None and (
            machine.state is OverlayState.CLOSED
            or (machine.overlay_session_id, machine.paint_generation)
            != (sid, gen)
        ):
            logger.info(
                "overlay: %s already stale before the sentence wait "
                "(machine state=%s gen=(%s,%s), build gen=(%s,%s)); "
                "skipping the wait (trace_id=%s)",
                action, machine.state.value, machine.overlay_session_id,
                machine.paint_generation, sid, gen, trace_id,
            )
            return
        bound = getattr(
            self, "_overlay_read_sentence_wait_max_s",
            _OVERLAY_READ_SENTENCE_WAIT_MAX_S,
        )
        logger.info(
            "overlay: %s waiting up to %.1fs for the open dictation "
            "sentence to end (gen=(%s,%s), trace_id=%s)",
            action, bound, sid, gen, trace_id,
        )
        abort = asyncio.Event()
        self._overlay_build_abort = abort
        self._overlay_build_abort_pair = (sid, gen)
        sentence_wait = self._host.loop.create_task(event.wait())
        abort_wait = self._host.loop.create_task(abort.wait())
        try:
            await asyncio.wait(
                {sentence_wait, abort_wait},
                timeout=bound,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self._overlay_build_abort = None
            self._overlay_build_abort_pair = None
            sentence_wait.cancel()
            abort_wait.cancel()

    def _perform_overlay_effects(
        self, effects, *, trace_id: str,
    ) -> "Optional[asyncio.Task]":
        """Perform the overlay effects the pure machine returned (wh-n29v.95).

        ``ClickOverlayStateMachine.apply`` returns the side effects as data
        (DISPATCH_BUILD / PAINT / CLEAR, PIN / UNPIN_SNAPSHOT, FIRE_NOTICE,
        ARM / CANCEL_TIMER). This is the synchronous entry the machine-feed
        paths call (``handle_overlay_command``, ``_apply_overlay_event``); it
        SCHEDULES the real cross-process dispatch as a background task rather
        than blocking the caller on the awaitable IPC. Returns that task
        (``None`` when there were no effects) so a caller that must order its
        own work after the batch -- the grid's CLOSE_NUMBERED_OVERLAY path
        (wh-mouse-grid.1.17) -- can await batch completion.

        CONCURRENCY CONTRACT (wh-n29v.70.2): the real dispatch
        (``_dispatch_overlay_effects``) is async and acquires
        ``_overlay_effect_lock`` for the WHOLE batch, so effects ship in
        machine-return order even when multiple in-flight tasks (paint-acks,
        the build-response feed, the timer feed) call this concurrently.
        ``machine.apply`` already committed the state transition synchronously
        BEFORE this runs, so machine state is consistent; the lock guarantees
        a not-yet-completed clear from one batch is not reordered against a
        paint/clear from a later batch. asyncio.Lock is FIFO, so batches
        dispatch in the order they were scheduled on this single Logic loop.
        """
        # wh-pin-snapshot-contract-break-detection: the pure machine records
        # an over-pin contract break (a pin outside the legitimate
        # deferred-refresh two-pin window) instead of logging; surface it
        # here, the funnel every apply site's effects flow through. Checked
        # before the empty-return: a break implies a PIN effect was emitted,
        # so effects are non-empty whenever one is pending, but the ordering
        # keeps that coupling explicit rather than incidental.
        machine = getattr(self, "click_overlay_state", None)
        if machine is not None:
            break_msg = machine.consume_pin_contract_break()
            if break_msg:
                logger.warning(
                    "overlay: PIN CONTRACT BREAK -- %s (trace_id=%s). "
                    "A lost unpin or racing double-pin; the Input store may "
                    "hold a leaked pinned snapshot until TTL.",
                    break_msg, trace_id,
                )
        if not effects:
            return None
        # wh-overlay-slow-uia-stale-badges.21.5: the audit for a restore paint
        # starts HERE, not at its GUI enqueue. The machine already committed
        # PAINTED; the task below still has to win ``_overlay_effect_lock``
        # from whatever batch holds it.
        self._overlay_arm_scheduled_restore_paint_audit(
            effects, trace_id=trace_id,
        )
        # wh-overlay-rewalk-after-filter.1.4: record the build's window HERE,
        # synchronously, not only in the scheduled task. The machine is
        # already in flight when this runs, and the task below starts only
        # after every callback already ready on the Logic loop. A Pattern
        # Manager tree change among those callbacks would otherwise find no
        # build target (None on a first session) and be dropped; for AUTO_OPEN,
        # which repaints the snapshot the click walked before the change, that
        # paints stale badges. ``_overlay_dispatch_build`` samples again when
        # the build actually runs, so a walk that waited behind another batch
        # is still judged against the window it reads.
        from services.wheelhouse.click_overlay_state import EffectKind

        if any(e.kind is EffectKind.DISPATCH_BUILD for e in effects):
            self._overlay_pending_build_identity = (
                self._capture_overlay_foreground_identity()
            )
        return self._host.create_task_with_error_handling(
            self._dispatch_overlay_effects(tuple(effects), trace_id=trace_id),
            "OverlayEffects",
        )

    async def _dispatch_overlay_effects(self, effects, *, trace_id: str) -> None:
        """Async, ordering-safe dispatch of one overlay effect batch (wh-n29v.95).

        Acquires ``_overlay_effect_lock`` for the whole batch so the per-effect
        cross-process sends ship in machine-return order under concurrency. A
        DISPATCH_BUILD awaits the Input build response, populates the summary
        cache, and feeds BUILD_RESPONSE back through ``_apply_overlay_event`` so
        the machine advances. Each effect's IO is wrapped in degrade-don't-die:
        an unexpected failure on one effect logs and the batch continues, so one
        bad send cannot abort the rest of an ordered batch.
        """
        from services.wheelhouse.click_overlay_state import EffectKind

        lock = getattr(self, "_overlay_effect_lock", None)
        if lock is None:
            lock = self._overlay_effect_lock = asyncio.Lock()
        async with lock:
            for effect in effects:
                try:
                    if effect.kind is EffectKind.DISPATCH_BUILD:
                        await self._overlay_dispatch_build(effect, trace_id)
                    elif effect.kind is EffectKind.DISPATCH_PAINT:
                        await self._overlay_dispatch_paint(effect, trace_id)
                    elif effect.kind is EffectKind.DISPATCH_CLEAR:
                        await self._overlay_dispatch_clear_one(effect, trace_id)
                    elif effect.kind is EffectKind.PIN_SNAPSHOT:
                        await self._overlay_send_pin(effect, trace_id)
                    elif effect.kind is EffectKind.UNPIN_SNAPSHOT:
                        await self._overlay_send_unpin(effect, trace_id)
                    elif effect.kind is EffectKind.FIRE_NOTICE:
                        self._overlay_fire_notice(effect, trace_id)
                    elif effect.kind is EffectKind.ARM_TIMER:
                        self._overlay_arm_timer(effect, trace_id)
                    elif effect.kind is EffectKind.CANCEL_TIMER:
                        self._overlay_cancel_timer()
                except Exception:  # noqa: BLE001 -- degrade, do not abort batch
                    logger.exception(
                        "overlay: effect %s gen=(%s,%s) failed; continuing the "
                        "batch (trace_id=%s)",
                        effect.kind.value, effect.overlay_session_id,
                        effect.paint_generation, trace_id,
                    )

    def _take_auto_open_filter(
        self, sid: int, gen: int,
    ) -> "list[str] | None":
        """Pop the stashed auto-open item_id_filter for ``(sid, gen)`` (wh-n29v.111).

        ``forward_click_element`` stashes the ambiguous-finalist filter keyed by
        the auto-open ``(overlay_session_id, paint_generation)`` because the
        DISPATCH_BUILD Effect carries only the reuse snapshot_id. This reads it
        back when the AUTO_OPEN build dispatches, consuming it (single-slot, one
        auto-open in flight). Returns the filter list when the stashed pair
        matches ``(sid, gen)``, else ``None`` (no stash, or a stale/mismatched
        pair) so the build degrades to an unfiltered re-paint rather than
        applying a filter from a different overlay. Clears the slot only when it
        is the one being consumed, so a concurrent unrelated stash (which the
        single-auto-open-in-flight contract forbids in practice) is not dropped.
        """
        # Read defensively: a controller assembled via object.__new__ in a test
        # fixture may not have run __init__, so the slot may be absent. Treat an
        # absent slot the same as an empty one (no filter -> unfiltered re-paint).
        stash = getattr(self, "_overlay_auto_open_filter", None)
        if stash is None:
            return None
        pair, item_ids = stash
        if pair != (sid, gen):
            return None
        self._overlay_auto_open_filter = None
        return item_ids

    async def _overlay_dispatch_build(self, effect, trace_id: str) -> None:
        """Dispatch a build (start_overlay_walk / show_numbered_overlay) (wh-n29v.95).

        AUTO_OPEN reuses the existing click snapshot via ``show_numbered_overlay``;
        every other BuildReason is a fresh walk via ``start_overlay_walk``. The
        response echoes the (overlay_session_id, paint_generation) pair; on a
        usable response the summary cache is populated and a BUILD_RESPONSE
        OverlayEvent (build_ok, snapshot_id, echoed pair) is fed back through
        ``_apply_overlay_event`` so the machine advances. A timeout / send
        failure / malformed reply feeds a build_ok=False BUILD_RESPONSE so the
        machine takes its in-flight-failure path (fire pending/standalone
        notice -> closed) instead of stalling in walk_in_flight forever.
        """
        from utils.trace_context import set_trace
        from services.wheelhouse.click_overlay_state import (
            BuildReason,
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )

        set_trace(trace_id)
        sid = effect.overlay_session_id
        gen = effect.paint_generation
        # wh-overlay-rewalk-after-filter.1.3: record the window this build is
        # for, BEFORE the send, so a Pattern Manager tree change arriving while
        # the walk runs is judged against the window being read rather than
        # against the previous pin (or against nothing at all on a first
        # session). AUTO_OPEN takes the same sample: it repaints a snapshot
        # without a fresh walk, but the machine can still supersede it.
        self._overlay_pending_build_identity = (
            self._capture_overlay_foreground_identity()
        )
        # wh-overlay-slow-uia-stale-badges.3: the awaited window depends on
        # what the build actually asks the Input process to do. AUTO_OPEN
        # sends show_numbered_overlay, a lookup in the snapshot store with no
        # walk behind it, so it keeps the click reply limit. Every other build
        # reason sends start_overlay_walk, which IS a screen read of the whole
        # window, so it gets the read's own limit -- otherwise a read that
        # answers correctly after the reply limit is discarded, which is the
        # defect this bead removes.
        if effect.build_reason is BuildReason.AUTO_OPEN:
            timeout_ms = self._host.click_config.response_timeout_ms
        else:
            timeout_ms = self._host.click_config.screen_read_timeout_ms
        timeout_s = max(timeout_ms / 1000.0, 0.1)
        # wh-overlay-slow-uia-stale-badges.3.1.1: the GUI walking-cue bound,
        # chosen here beside the awaited window it is built from so the cue
        # emission below names one value. Only a non-AUTO_OPEN build holds for
        # the open dictation sentence before its send (``walks_input`` below),
        # so only it carries that wait.
        cue_bound_ms = timeout_ms + self._host.click_config.response_timeout_ms
        if effect.build_reason is not BuildReason.AUTO_OPEN:
            cue_bound_ms += int(_OVERLAY_READ_SENTENCE_WAIT_MAX_S * 1000)

        if effect.build_reason is BuildReason.AUTO_OPEN:
            action = "show_numbered_overlay"
            # wh-n29v.96.1: the reuse snapshot id travels ON the effect. AUTO_OPEN
            # fires from CLOSED before the machine pins anything, so reading
            # self.click_overlay_state.pinned_snapshot_id here would send "" and
            # lose the reuse target. Read effect.snapshot_id instead.
            #
            # wh-n29v.111: the auto-open item_id_filter (the ambiguous finalists)
            # is NOT on the Effect -- it was stashed by forward_click_element
            # keyed by this build's (overlay_session_id, paint_generation). Pop it
            # for THIS pair so the Input handler restricts the painted set to the
            # finalists and renumbers them 1..K. A pair mismatch (a superseding
            # build that re-used AUTO_OPEN, which the design never does, or a
            # stale stash) yields ``None`` -> an unfiltered re-paint, the safe
            # degrade. The stash is single-slot (one auto-open in flight) and is
            # consumed exactly once here.
            item_id_filter = self._take_auto_open_filter(sid, gen)
            params: dict = {
                "snapshot_id": effect.snapshot_id or "",
                "item_id_filter": item_id_filter,
                "overlay_session_id": sid,
                "paint_generation": gen,
                "trace_id": trace_id,
            }
            parser = self._parse_show_numbered_overlay_response
        else:
            action = "start_overlay_walk"
            params = {
                "scope": "focused_window",
                "overlay_session_id": sid,
                "paint_generation": gen,
                "trace_id": trace_id,
            }
            if effect.build_reason is BuildReason.SETTLE:
                # wh-overlay-slow-uia-stale-badges.2: the post-click re-read.
                # ``settle`` makes the Input handler read the window through
                # the shared settle detector instead of walking once, and
                # ``compare_snapshot_id`` is what it compares the settled read
                # against. Both come off the EFFECT for the same reason
                # AUTO_OPEN's reuse id does (wh-n29v.96.1): this performer is
                # async, so the machine pin can have moved by the time it runs,
                # and comparing against the wrong snapshot would report
                # "changed" for a screen that did not change.
                params["settle"] = True
                params["compare_snapshot_id"] = effect.snapshot_id or ""
            parser = self._parse_start_overlay_walk_response

        # wh-n29v.117: emit the floating-button "walking" progress cue. This
        # method is the single funnel every walk-start passes through, so
        # active:True here covers fresh/refresh walks (start_overlay_walk) and
        # auto-open (show_numbered_overlay). It is a plain-dict side-channel
        # notification on the existing GUI state queue -- NOT a new EffectKind
        # and NOT a shared/ schema -- so the overlay state-machine contract is
        # untouched and the GUI consumer is defensive. The terminating
        # active:False rides the _feed closure below on the build-FAILURE paths
        # only (timeout, send failure, malformed, generation mismatch); on the
        # build-SUCCESS path the GUI clears the cue when paint_overlay arrives,
        # and the GUI fallback timer is the last-resort safety net (wh-n29v.119.2).
        #
        # wh-n29v.118 / wh-n29v.120.1: carry the GUI fallback bound as
        # walk_timeout_ms. On the SUCCESS path the cue must survive until
        # paint_overlay is enqueued, and paint is enqueued only AFTER BOTH this
        # build's own send_request AND the PIN_SNAPSHOT ack await in
        # _overlay_send_pin (bounded by response_timeout_ms) complete -- the
        # PIN_SNAPSHOT effect runs before DISPATCH_PAINT in the same FIFO-locked
        # batch. So the fallback must outlast build + pin; a fallback sized for
        # the build alone would clear the dot during a slow or hung pin before
        # the numbers paint. Carry the build's own awaited window
        # (screen_read_timeout_ms for a walk, response_timeout_ms for
        # AUTO_OPEN) plus response_timeout_ms for the pin ack
        # (wh-overlay-slow-uia-stale-badges.3; this was 2 * response_timeout_ms
        # while both legs shared one limit). The GUI adds its own buffer on top
        # and clamps to the Qt timer range (wh-n29v.119.1) so the sum cannot
        # overflow even on a response_timeout_ms that never passed the
        # validator's 100-to-10000 bound.
        #
        # wh-overlay-slow-uia-stale-badges.3.1.1: this cue is emitted BEFORE
        # ``_wait_for_open_sentence_end`` below, so on a walk requested
        # mid-sentence the whole wait sits inside the fallback's window before
        # the send even starts. ``cue_bound_ms`` therefore carries
        # _OVERLAY_READ_SENTENCE_WAIT_MAX_S as a third term for every build but
        # AUTO_OPEN, which does not wait; without it the GUI fallback cleared
        # the dot during a legitimate walk whenever the wait exceeded about a
        # second.
        self._put_overlay_walk_cue(
            True,
            trace_id=trace_id,
            walk_timeout_ms=cue_bound_ms,
        )

        # wh-overlay-slow-uia-stale-badges.2.2.1. The window the reply says it
        # was READ from, filled in from the parsed payload just before the
        # commit feed. A one-slot list because ``_feed`` is a closure defined
        # before the reply exists, and every failure path calls it with no
        # reply at all.
        reported_window: list = []
        # Only a SETTLE build spans a period in which the foreground is
        # ALLOWED to move: ``post_click_settling`` ignores FOCUS_CHANGE on
        # purpose, because a click that opens a dialog is the common case for
        # "click N". Every other build lives in a state whose FOCUS_CHANGE
        # cell already supersedes or refreshes, so re-checking them here would
        # duplicate the machine's own rule.
        is_settle = effect.build_reason is BuildReason.SETTLE

        def _machine_still_awaits_this_build() -> bool:
            """True while the machine can still consume THIS build's reply.

            The commit fence for wh-overlay-slow-uia-stale-badges.19. Three
            states have a BUILD_RESPONSE cell that does work, and the machine
            stays in the state that dispatched the build until the reply
            arrives, so any OTHER state at this pair means the machine has
            moved on without this reply.

            ``post_click_settling`` is the third
            (wh-overlay-slow-uia-stale-badges.2.1.1, deepseek round 1). It was
            added after this fence was written: the settle build is dispatched
            from that state by ``_enter_post_click_settling``, and
            ``_on_post_click_settling`` consumes the reply -- repainting the
            held pin when the screen did not change, or pinning and painting
            the new snapshot when it did. Omitting it dropped EVERY settle
            reply at the identical pair, so the badges never came back and the
            state closed on its own ``settle_deadline_ms`` timer. No
            machine-level test can see that: the fence lives here, not in the
            machine.
            """
            machine = getattr(self, "click_overlay_state", None)
            if machine is None:
                return False
            return machine.state in (
                OverlayState.WALK_IN_FLIGHT, OverlayState.REFRESH_IN_FLIGHT,
                OverlayState.POST_CLICK_SETTLING,
            ) and (
                machine.overlay_session_id, machine.paint_generation
            ) == (sid, gen)

        def _feed(*, build_ok: bool, snapshot_id) -> None:
            # wh-n29v.117 / wh-n29v.119.2: clear the walking cue immediately
            # ONLY on the build-FAILURE paths (timeout, send failure, malformed
            # reply, generation mismatch). Those paths enqueue no paint_overlay,
            # so nothing else would clear the cue before the GUI fallback timer.
            # On the build-SUCCESS path do NOT clear here: the success
            # transition schedules PIN_SNAPSHOT then DISPATCH_PAINT, and
            # _overlay_send_pin AWAITS the pin ack (up to response_timeout_ms)
            # before paint_overlay is enqueued. Clearing the cue here would drop
            # the dot during that window, so the user would see neither the cue
            # nor the numbers -- defeating the latency-budget affordance exactly
            # when latency is worst. The GUI clears the cue as a backstop when
            # paint_overlay (or clear_overlay) arrives. Every success path that
            # enqueues NO paint clears the cue at its own drop point instead
            # (``_overlay_dispatch_paint``: the commit fence and the summary
            # cache miss, wh-overlay-slow-uia-stale-badges.21.4), so the GUI
            # fallback timer is a last-resort safety net, not the cover for a
            # known success-without-paint case.
            if not build_ok:
                self._put_overlay_walk_cue(False, trace_id=trace_id)
            # wh-overlay-slow-uia-stale-badges.19 (post-await commit fence).
            # The PRE-send check above cannot cover this: it runs before the
            # Input round trip, and the refresh fall-back
            # (``_refresh_fall_back``) commits PAINTED at the SAME pair the
            # build was dispatched at, so a reply that arrives after it passes
            # every earlier gate. ``_on_painted`` has no BUILD_RESPONSE cell,
            # so the feed reached ``_invalid``: the machine entered ERROR with
            # NO clear, so the badges stayed on screen until the GUI badge
            # lease expired and the GUI tore them down itself, and "click N"
            # answered numbers_not_showing while the user looked at numbers.
            # Re-check the machine HERE, at the commit point, and treat a reply
            # the machine can no longer use as a harmless stale completion:
            # drop it and clear the walking cue, because no paint will follow.
            # This also covers CLOSED (already a machine-side NO_OP) and a
            # superseded pair (already dropped by the generation gate), so the
            # fence never changes a case those two paths already handled.
            if not _machine_still_awaits_this_build():
                current = self.click_overlay_state
                logger.info(
                    "overlay: %s reply for gen=(%s,%s) arrived after the "
                    "machine moved on (state=%s gen=(%s,%s)); dropping it as "
                    "a stale completion (trace_id=%s)",
                    action, sid, gen, current.state.value,
                    current.overlay_session_id, current.paint_generation,
                    trace_id,
                )
                self._put_overlay_walk_cue(False, trace_id=trace_id)
                return
            # wh-overlay-slow-uia-stale-badges.2.2.1 (the SAME commit fence,
            # asking the other half of the question). The check above asks
            # whether the MACHINE can still use this reply; this one asks
            # whether the SCREEN can. A settle read is answered against the
            # window Input captured before it started, and the machine
            # deliberately does not cancel on a focus change while it settles,
            # so the numbers can describe a window the user has already left.
            # Painting them would put window A's badges over window B, and the
            # numbers would then resolve against a list Input itself rejects.
            # Verify here rather than at paint: this is the point before the
            # machine PINS the snapshot, so a rejected reply never becomes the
            # session's pinned list.
            if build_ok and is_settle:
                reported = reported_window[0] if reported_window else None
                if not self._overlay_settle_read_is_current(
                    reported, trace_id=trace_id,
                ):
                    # Fail closed: feed the build-FAILURE leg, which
                    # ``_on_post_click_settling`` answers by unpinning and
                    # closing -- the same outcome its TIMEOUT arm already
                    # documents as acceptable ("the badges are already gone,
                    # so the only cost is the spoken command"). No paint, and
                    # no pin left behind for a window that is not in front.
                    #
                    # crewcut: two limits are accepted here, both removable
                    # later. (1) This gives up instead of rebuilding for the
                    # NEW window. The pre-flag path did better: a click ran
                    # CLICK_COMPLETE into refresh_in_flight, whose FOCUS_CHANGE
                    # cell calls _refresh_supersede and walks the new window,
                    # so the user kept numbers across a dialog opening.
                    # Restoring that parity means feeding a supersede-and-
                    # rebuild from here instead of a build failure, which needs
                    # a machine-side entry point post_click_settling does not
                    # have yet. (2) A residual window remains between this
                    # check and the paint -- the reply still has to cross to
                    # the GUI process and be drawn, and the foreground can move
                    # in that gap. No check can close it; this one closes the
                    # LARGE window (the settle read itself, up to
                    # settle_maximum_ms) and leaves the message-transit tail.
                    self._put_overlay_walk_cue(False, trace_id=trace_id)
                    build_ok = False
                    snapshot_id = None
                else:
                    # Carry the READ's window to the pin. _overlay_send_pin
                    # must not re-sample the foreground for this snapshot: a
                    # fresh sample records whichever window is in front when
                    # the pin goes out, which defeats the two consumers of the
                    # recorded identity (overlay_snapshot_is_valid_on_resume
                    # and _overlay_refresh_visible_window_is_foreground) by the
                    # same race this fence closes.
                    self._overlay_settle_read_identity = (
                        (snapshot_id, reported)
                        if snapshot_id and reported is not None else None
                    )
            # wh-n29v.96.4: DEFER the build-response transition via call_soon so
            # the in-flight build batch fully drains its remaining effects --
            # crucially the walk batch's trailing ARM_TIMER(WALK) -- and RELEASES
            # ``_overlay_effect_lock`` BEFORE the build-response transition
            # (-> paint_in_flight) schedules its own effects. Feeding inline here
            # would commit the paint transition while the WALK timer arm is still
            # pending in the same batch, briefly arming a stale WALK-duration
            # timer at the still-current generation. The call_soon hop preserves
            # FIFO ordering on the single Logic loop without holding the lock
            # across the transition.
            event = OverlayEvent(
                kind=OverlayEventKind.BUILD_RESPONSE,
                overlay_session_id=sid,
                paint_generation=gen,
                snapshot_id=snapshot_id,
                build_ok=build_ok,
            )
            source = f"build_response action={action} gen=({sid},{gen})"
            self._host.loop.call_soon(
                lambda: self._apply_overlay_event(event, source=source)
            )

        # wh-overlay-slow-uia-stale-badges.7.1.9: only start_overlay_walk
        # makes Input run a UIA walk. The AUTO_OPEN build sends
        # show_numbered_overlay, a snapshot lookup plus repaint of badges
        # Input already holds, so it must neither wait for the open
        # sentence nor set the marker that refuses dictation.
        walks_input = action == "start_overlay_walk"

        # wh-overlay-slow-uia-stale-badges.7: a read requested mid-sentence
        # waits for the sentence to end before it goes to Input. Runs
        # BEFORE the staleness check below so a hide committed during the
        # wait is seen by that check and skips the send.
        if walks_input:
            await self._wait_for_open_sentence_end(
                sid=sid, gen=gen, action=action, trace_id=trace_id,
            )

        # wh-mouse-grid.1.20 (pre-send staleness): this batch may have sat
        # queued behind _overlay_effect_lock while a hide tore the session
        # down (state -> CLOSED, pair kept) or a supersede advanced the
        # generation. Sending would hold the lock for up to the build's own
        # awaited window (screen_read_timeout_ms for a walk build,
        # response_timeout_ms for AUTO_OPEN) with nothing the machine can use.
        machine = self.click_overlay_state
        if machine.state is OverlayState.CLOSED or (
            machine.overlay_session_id, machine.paint_generation
        ) != (sid, gen):
            logger.info(
                "overlay: %s superseded before send (machine state=%s "
                "gen=(%s,%s), build gen=(%s,%s)); skipping the send "
                "(trace_id=%s)",
                action, machine.state.value, machine.overlay_session_id,
                machine.paint_generation, sid, gen, trace_id,
            )
            _feed(build_ok=False, snapshot_id=None)
            return

        # wh-mouse-grid.1.20 (abort race): park an abort event so a hide
        # committed while this send is in flight can wake us; a stale build
        # must not make the hide's clear -- or a grid paint awaiting it --
        # wait out this request's deadline.
        abort = asyncio.Event()
        self._overlay_build_abort = abort
        # wh-overlay-slow-uia-stale-badges.2.2.4: record WHICH pair this parked
        # send belongs to, beside the event itself. The abort helper compares
        # it against the machine's current pair so it can wake a superseded
        # build without ever waking a fresh one.
        self._overlay_build_abort_pair = (sid, gen)
        # wh-overlay-slow-uia-stale-badges.7: mark the Input round trip so
        # the speech processor's dictation gate refuses words while the
        # read runs. Counted out in the finally: a Logic-side timeout or
        # abandon does not cancel Input's running COM walk, but the
        # gate's own maximum wait bounds the refusal either way.
        send_task = self._host.loop.create_task(
            self._host.app.send_request(action, params=params, timeout_s=timeout_s)
        )
        abort_wait = self._host.loop.create_task(abort.wait())
        if walks_input:
            self._host.mark_screen_read_started()
        try:
            await asyncio.wait(
                {send_task, abort_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if walks_input:
                self._host.mark_screen_read_finished()
            self._overlay_build_abort = None
            self._overlay_build_abort_pair = None
            abort_wait.cancel()
        if not send_task.done():
            # Superseded mid-await. Abandon the wait: the late response (if
            # Input ever answers) is dropped by the response demuxer, and an
            # unread Input-side snapshot expires by TTL -- the same residue
            # the timeout path already leaves.
            send_task.cancel()
            try:
                await send_task
            except BaseException:  # noqa: BLE001 -- absorbing the cancel
                pass
            logger.info(
                "overlay: %s abandoned, session hidden while awaiting Input "
                "(gen=(%s,%s), trace_id=%s)", action, sid, gen, trace_id,
            )
            _feed(build_ok=False, snapshot_id=None)
            return
        try:
            raw = send_task.result()
        except asyncio.TimeoutError:
            # WARNING, not ERROR: send_request already logged this fault at
            # ERROR, and every ERROR record raises its own Windows
            # notification popup -- one fault must pop at most one. A single
            # slow start_overlay_walk popped two (wh-duplicate-error-popup).
            logger.warning(
                "overlay: %s no reply within %dms; feeding build_ok=False "
                "(gen=(%s,%s), trace_id=%s)",
                action, timeout_ms, sid, gen, trace_id,
            )
            _feed(build_ok=False, snapshot_id=None)
            return
        except Exception as exc:  # noqa: BLE001 -- CancelledError propagates
            # WARNING, not ERROR: send_request already logged the fault at
            # ERROR with traceback (wh-duplicate-error-popup).
            logger.warning(
                "overlay: %s send_request failed (gen=(%s,%s), trace_id=%s): "
                "%s", action, sid, gen, trace_id, exc, exc_info=True,
            )
            _feed(build_ok=False, snapshot_id=None)
            return

        parsed = parser(raw, trace_id=trace_id)
        if parsed is None:
            # Malformed payload already logged by the parser; treat as a build
            # failure so the machine recovers rather than stalling.
            _feed(build_ok=False, snapshot_id=None)
            return

        (
            build_ok, snapshot_id, summary, echoed_sid, echoed_gen,
            read_window,
        ) = parsed
        # Hand the reply's read-window identity to the commit fence
        # (wh-overlay-slow-uia-stale-badges.2.2.1). Set before ANY _feed call
        # that can carry build_ok=True; the earlier failure feeds above run
        # with the slot empty, which the fence reads as "no window reported".
        reported_window.append(read_window)
        # wh-n29v.97.3: the response echoes (overlay_session_id,
        # paint_generation) "for the generation/supersession check" (v4 design
        # line 186: "Logic compares the echoed generation against the current
        # one ... drops the response and does NOT paint"). A response whose
        # echoed pair disagrees with the request pair is not trustworthy -- an
        # Input-side generation bug or a skewed/misrouted payload -- so treat it
        # as a build failure (recover non-destructively; do NOT paint or cache
        # it) instead of restamping it with the request pair and letting it
        # through the machine's stale-generation gate. The request pair stays
        # authoritative for the BUILD_RESPONSE feed; the echo is the cross-check.
        if (echoed_sid, echoed_gen) != (sid, gen):
            logger.error(
                "overlay: %s response echoed generation (%s,%s) != request "
                "(%s,%s); treating as build failure (trace_id=%s)",
                action, echoed_sid, echoed_gen, sid, gen, trace_id,
            )
            _feed(build_ok=False, snapshot_id=None)
            return

        if build_ok and snapshot_id and summary is not None:
            try:
                self.click_snapshot_summary_cache.put(snapshot_id, summary)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "overlay: failed to populate snapshot cache for %r "
                    "(trace_id=%s): %s", snapshot_id, trace_id, exc,
                )
        _feed(build_ok=build_ok, snapshot_id=snapshot_id if build_ok else None)

    def _parse_start_overlay_walk_response(self, raw, *, trace_id: str):
        """Parse a StartOverlayWalkResponse; return (build_ok, snapshot_id, summary)
        or None on a malformed payload (wh-uf54 degrade)."""
        from services.wheelhouse.shared.start_overlay_walk import (
            StartOverlayWalkResponse,
            StartOverlayWalkResponseSchemaError,
        )

        try:
            resp = StartOverlayWalkResponse.from_dict(raw)
        except (
            StartOverlayWalkResponseSchemaError, ValueError, KeyError, TypeError,
        ) as exc:
            logger.error(
                "overlay: malformed start_overlay_walk response "
                "(trace_id=%s): %s", trace_id, exc,
            )
            return None
        from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity

        return (
            resp.outcome == "ok", resp.snapshot_id, resp.snapshot_summary,
            resp.overlay_session_id, resp.paint_generation,
            # wh-overlay-slow-uia-stale-badges.2.2.1: the window Input read.
            # Mapped onto ForegroundIdentity here, at the process boundary, so
            # the fence can hand it straight to ``identity_matches`` and the
            # shared/ schema stays plain data.
            ForegroundIdentity(
                hwnd=resp.foreground_window,
                pid=resp.foreground_pid,
                process_name=resp.foreground_process_name,
                window_creation_time=resp.foreground_window_creation_time,
            ),
        )

    def _parse_show_numbered_overlay_response(self, raw, *, trace_id: str):
        """Parse a ShowNumberedOverlayResponse; return (build_ok, snapshot_id,
        summary) or None on a malformed payload (wh-uf54 degrade)."""
        from services.wheelhouse.shared.show_numbered_overlay import (
            ShowNumberedOverlayResponse,
            ShowNumberedOverlayResponseSchemaError,
        )

        try:
            resp = ShowNumberedOverlayResponse.from_dict(raw)
        except (
            ShowNumberedOverlayResponseSchemaError, ValueError, KeyError,
            TypeError,
        ) as exc:
            logger.error(
                "overlay: malformed show_numbered_overlay response "
                "(trace_id=%s): %s", trace_id, exc,
            )
            return None
        return (
            resp.outcome == "ok", resp.snapshot_id, resp.snapshot_summary,
            resp.overlay_session_id, resp.paint_generation,
            # No read-window identity: this schema does not carry one, and it
            # does not need to. show_numbered_overlay is only ever the
            # AUTO_OPEN build, which re-paints a snapshot Input already holds
            # and is never a SETTLE build, so the fence below never asks.
            None,
        )

    async def _overlay_dispatch_paint(self, effect, trace_id: str) -> None:
        """Put a paint_overlay action (flattened summary) on the GUI queue (wh-n29v.95).

        Looks up the summary for ``effect.snapshot_id`` in the Logic cache to
        build the PaintOverlayEvent. PART 4 keepalive: re-put the summary on
        access so the actively-painted snapshot does not age out of the resolver
        cache while it is on screen (criterion 4). A cache miss (the summary was
        evicted) logs and skips the paint -- there is nothing to draw.

        wh-overlay-slow-uia-stale-badges.19: a commit fence runs first, because
        this send can be postponed long past the moment the machine chose it.
        """
        from services.wheelhouse.shared.paint_overlay import PaintOverlayEvent

        # wh-overlay-slow-uia-stale-badges.21.4: BOTH early returns below
        # enqueue no paint, so both clear the walking cue themselves. The GUI
        # clears the cue when paint_overlay (or clear_overlay) arrives; a drop
        # that sends neither would leave the moving dot on screen until the GUI
        # fallback timer expires (the build's own awaited window plus
        # response_timeout_ms, plus the sentence-wait bound for a walk build;
        # see ``_overlay_dispatch_build``'s ``cue_bound_ms``), claiming a walk
        # is in flight after the walk ended. This is
        # the same rule the build fence in ``_overlay_dispatch_build._feed``
        # follows. Ordering is safe: the drop runs inside the abandoned batch
        # while ``_overlay_effect_lock`` is held (see
        # ``_dispatch_overlay_effects``), and any newer walk's cue-True ships in
        # a later batch, so a False written here can only PRECEDE the final
        # state, never overwrite it.
        if not self._overlay_paint_is_still_wanted(effect, trace_id=trace_id):
            # No audit on this leg (wh-overlay-slow-uia-stale-badges.21.3): the
            # fence fires only when the machine ALREADY abandoned this paint,
            # so there is no belief left to correct. The schedule-time arm
            # (.21.5) covered the wait that just ended here, so retire it --
            # the pair filter keeps a NEWER restore's audit untouched.
            self._overlay_cancel_restore_paint_audit(
                only_pair=(effect.overlay_session_id, effect.paint_generation),
            )
            self._put_overlay_walk_cue(False, trace_id=trace_id)
            return
        # wh-overlay-slow-uia-stale-badges.21.8: an AUDITED restore paint needs
        # a delivery entitlement as well as the fence above. The fence reads
        # only the pair and the pin, and the exits that supersede a restore at
        # the SAME pair with the SAME pin leave both untouched, so the fence
        # cannot see that this paint was abandoned. Same drop handling as the
        # fence leg: no audit is reported (the machine abandoned the paint, so
        # there is no belief left to correct), and the walking cue is cleared
        # here because no paint and no clear will do it.
        if getattr(effect, "audit_delivery", False) and not (
            self._overlay_restore_paint_is_entitled(effect, trace_id=trace_id)
        ):
            self._overlay_cancel_restore_paint_audit(
                only_pair=(effect.overlay_session_id, effect.paint_generation),
            )
            self._put_overlay_walk_cue(False, trace_id=trace_id)
            return
        snapshot_id = effect.snapshot_id
        summary = self._overlay_keepalive_summary(snapshot_id)
        if summary is None:
            logger.warning(
                "overlay: paint requested for snapshot %r but no summary is "
                "cached; skipping paint (gen=(%s,%s), trace_id=%s)",
                snapshot_id, effect.overlay_session_id,
                effect.paint_generation, trace_id,
            )
            self._put_overlay_walk_cue(False, trace_id=trace_id)
            self._overlay_audit_restore_paint(
                effect, enqueued=False, trace_id=trace_id,
                loss="the snapshot summary is no longer cached",
            )
            return
        event = PaintOverlayEvent(
            overlay_session_id=effect.overlay_session_id,
            paint_generation=effect.paint_generation,
            summary=summary,
        )
        enqueued = self._put_overlay_gui_action(
            event.to_dict(), trace_id=trace_id,
        )
        if enqueued:
            # wh-overlay-slow-uia-stale-badges.23.6: from here the display
            # process owns this list; no later Logic rollback can recall it.
            # The deferred post-click clear reads this to tell "the pre-click
            # badges are still on screen" from "the pin was restored after a
            # newer list had already been sent".
            self._overlay_last_enqueued_paint = (
                effect.overlay_session_id, effect.paint_generation,
                snapshot_id,
            )
        self._overlay_audit_restore_paint(
            effect, enqueued=enqueued, trace_id=trace_id,
            loss="the display process queue rejected it",
        )

    def _overlay_paint_is_still_wanted(self, effect, *, trace_id: str) -> bool:
        """Commit fence for a paint (wh-overlay-slow-uia-stale-badges.19).

        A DISPATCH_PAINT can sit behind an awaited PIN_SNAPSHOT in the same
        FIFO-locked batch for up to ``response_timeout_ms``, which is LONGER
        than the refresh deadline the machine armed. When the refresh timer
        fires in that window, ``_refresh_fall_back`` has already committed
        PAINTED and RESTORED the prior pin, and the fall-back does not cancel
        the batch. Presenting the paint then puts the NEW list on screen while
        Logic resolves "click N" against the RESTORED prior snapshot -- the
        wrong-control defect family. The GUI generation gate cannot catch it
        either: the fall-back does not bump the pair, so the paint still looks
        current from the display side.

        The machine's own bookkeeping is the fence. Every DISPATCH_PAINT the
        machine emits is for the snapshot it has just made its pinned
        (intended-visible) one at the current pair -- ``_walk_build_ok``,
        ``_refresh_build_ok``, ``_resolve_in_flight_to_paused`` and the paused
        MIC_RESUME restore all assign ``pinned_snapshot_id`` in the same
        transition that emits the paint. So a pair mismatch or a pin mismatch
        at dispatch time means the machine abandoned this paint while it
        waited. Returns True when the paint is still the machine's intent.

        Read defensively: a controller assembled via ``object.__new__`` in a
        test fixture may have no machine. No machine means nothing can vouch
        for the paint, so refuse it rather than present an unvouched list.
        """
        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            logger.warning(
                "overlay: paint for gen=(%s,%s) has no state machine to "
                "vouch for it; dropping it (trace_id=%s)",
                effect.overlay_session_id, effect.paint_generation, trace_id,
            )
            return False
        pair = (machine.overlay_session_id, machine.paint_generation)
        if pair != (effect.overlay_session_id, effect.paint_generation) or (
            machine.pinned_snapshot_id != effect.snapshot_id
        ):
            logger.info(
                "overlay: paint for snapshot %r gen=(%s,%s) was abandoned "
                "while it waited (machine state=%s gen=(%s,%s) pin=%r); "
                "dropping it instead of presenting a generation the machine "
                "rolled back (trace_id=%s)",
                effect.snapshot_id, effect.overlay_session_id,
                effect.paint_generation, machine.state.value, pair[0], pair[1],
                machine.pinned_snapshot_id, trace_id,
            )
            return False
        return True

    def _overlay_restore_paint_is_entitled(self, effect, *, trace_id: str) -> bool:
        """Delivery entitlement for an AUDITED restore paint (wh-overlay-slow-uia-stale-badges.21.8).

        The pair-and-pin fence above is deliberately blind to the machine's
        state, because every ordinary paint is dispatched from an in-flight
        state and would fail a state test. That blindness is what lets a
        superseded restore paint through: ``_on_painted`` MIC_PAUSE returns the
        machine to PAUSED, and CLICK_COMPLETE enters POST_CLICK_SETTLING, both
        at the SAME pair and with the SAME pin the restore was scheduled with.
        The queued paint then repaints badges the user should not see -- while
        the microphone is paused, or into the settling gap -- and the
        superseding state's own clear only follows in a later batch.

        So the audited restore carries a permission of its own:
        ``_overlay_restore_paint_entitlement``, recorded when the restore is
        scheduled and retired by
        ``_reconcile_overlay_restore_paint_entitlement`` as soon as the machine
        leaves the PAINTED state it was scheduled in. Requiring the entitlement
        is fail-closed: a restore paint that reaches this dispatch by some path
        that never recorded one is refused rather than presented.
        """
        pair = (effect.overlay_session_id, effect.paint_generation)
        if getattr(self, "_overlay_restore_paint_entitlement", None) == pair:
            return True
        logger.info(
            "overlay: restore paint for snapshot %r gen=(%s,%s) lost its "
            "delivery entitlement while it waited; the machine left the "
            "painted state that scheduled it, so dropping it instead of "
            "repainting badges it has since taken off the screen "
            "(trace_id=%s)",
            effect.snapshot_id, pair[0], pair[1], trace_id,
        )
        return False

    async def _overlay_dispatch_clear_one(self, effect, trace_id: str) -> None:
        """Put a clear_overlay action on the GUI queue (wh-n29v.95).

        wh-overlay-slow-uia-stale-badges.9: the clear is the one overlay
        message whose loss leaves STALE STATE ON SCREEN while Logic
        believes it is gone, so every dispatched clear is handed to the
        ack watchdog: an enqueue failure is reported at once, an enqueued
        clear gets a deadline that fires an ERROR if no ack confirms it.
        """
        from services.wheelhouse.shared.clear_overlay import ClearOverlayEvent

        event = ClearOverlayEvent(
            overlay_session_id=effect.overlay_session_id,
            paint_generation=effect.paint_generation,
        )
        enqueued = self._put_overlay_gui_action(event.to_dict(), trace_id=trace_id)
        self._overlay_track_pending_clear(
            effect.overlay_session_id,
            effect.paint_generation,
            enqueued=enqueued,
            trace_id=trace_id,
        )

    def _put_overlay_gui_action(self, gui_msg: dict, *, trace_id: str) -> bool:
        """Put a paint/clear action on the GUI state queue, degrade on failure.

        Returns True when the message was enqueued, False when it was
        dropped (no queue, or the put raised) -- the clear-ack watchdog
        needs to know whether a clear actually left Logic
        (wh-overlay-slow-uia-stale-badges.9).
        """
        if not self._host.state_manager or not hasattr(
            self._host.state_manager, "state_to_gui_queue",
        ):
            logger.warning(
                "overlay: %s dropped, no state_manager queue (trace_id=%s)",
                gui_msg.get("action"), trace_id,
            )
            return False
        try:
            self._host.state_manager.state_to_gui_queue.put_nowait(gui_msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "overlay: %s dropped, GUI queue put failed (trace_id=%s): %s",
                gui_msg.get("action"), trace_id, exc,
            )
            return False
        return True

    def _overlay_track_pending_clear(
        self, sid: int, gen: int, *, enqueued: bool, trace_id: str,
    ) -> None:
        """Watch one dispatched clear until an ack confirms delivery."""
        existing = getattr(self, "_overlay_pending_clear_timer", None)
        if existing is not None:
            existing.cancel()
        self._overlay_pending_clear_timer = None
        if not enqueued:
            # The clear never left Logic; there is nothing to wait for.
            self._overlay_pending_clear = None
            self._report_overlay_clear_fault(
                sid, gen,
                "overlay: the clear for generation (%s,%s) could not be "
                "handed to the display process (the GUI queue rejected "
                "it). The numbered badges may still be on the screen; the "
                "display process removes them automatically when its "
                "badge lease expires. (trace_id=%s)",
                trace_id,
            )
            return
        self._overlay_pending_clear = (sid, gen)
        loop = self._host.loop
        if loop is None:
            return
        self._overlay_pending_clear_timer = loop.call_later(
            _OVERLAY_CLEAR_ACK_DEADLINE_MS / 1000.0,
            lambda: self._fire_overlay_clear_ack_deadline(sid, gen, trace_id),
        )

    def _overlay_note_clear_ack(self, sid: int, gen: int, state: str) -> None:
        """Resolve the pending clear when this ack proves it is moot.

        Two sound resolution rules (wh-overlay-slow-uia-stale-badges.18.1):

        * STRICTLY greater pair, any state: dispatch is pair-monotonic
          under the FIFO effect lock, so this ack comes from a message
          enqueued AFTER the pending clear, and the FIFO GUI queue
          consumed the clear first (a stale-gated consume is a correct
          no-op, not a loss).
        * EQUAL pair, state "cleared" or "expired": both prove the badges
          at that pair are GONE (whichever same-pair clear or lease
          expiry removed them), which is the guarantee the watchdog
          audits.

        An EQUAL-pair "painted" or "failed" ack must NOT resolve: the GUI
        can send it before it consumes the pending clear (a slow walk's
        paint at the same pair acks first), so it proves nothing about
        the clear.

        Accepted residual (finding member 8): when two clears ship at the
        same pair, the FIRST clear's cleared ack resolves the SECOND
        clear's slot before the second clear is consumed. The badges at
        that pair are provably gone either way, which is the guarantee
        the watchdog audits, so resolving the slot is acceptable.
        """
        pending = getattr(self, "_overlay_pending_clear", None)
        if pending is None:
            return
        if (sid, gen) == pending:
            if state not in ("cleared", "expired"):
                return
        elif (sid, gen) < pending:
            return
        self._overlay_pending_clear = None
        timer = getattr(self, "_overlay_pending_clear_timer", None)
        if timer is not None:
            timer.cancel()
        self._overlay_pending_clear_timer = None

    def _fire_overlay_clear_ack_deadline(
        self, sid: int, gen: int, trace_id: str,
    ) -> None:
        """Deadline callback: the dispatched clear was never confirmed.

        Race-safe shutdown backstop (wh-overlay-slow-uia-stale-badges
        .18.9): shutdown() cancels the timer, but this callback can
        already be queued on the loop when the shutdown event is set.
        With shutdown in progress the pending slot state is still
        cleared, and nothing is reported -- an intentional exit is not
        an overlay fault. ``shutdown_event`` is the same signal the run
        loop and main()'s shutdown watcher key on.
        """
        self._overlay_pending_clear_timer = None
        pending = getattr(self, "_overlay_pending_clear", None)
        if pending != (sid, gen):
            return  # resolved or superseded since arming; nothing to report
        self._overlay_pending_clear = None
        shutdown_event = self._host.shutdown_event
        if shutdown_event is not None and shutdown_event.is_set():
            return  # intentional exit; report nothing
        self._report_overlay_clear_fault(
            sid, gen,
            "overlay: the numbered badges may still be on the screen. "
            "Logic sent a clear for generation (%s,%s) but the display "
            "process did not confirm it within "
            f"{_OVERLAY_CLEAR_ACK_DEADLINE_MS}ms. The display process "
            "removes leftover badges automatically when its badge lease "
            "expires; say 'show numbers' to paint again. (trace_id=%s)",
            trace_id,
        )

    def _overlay_arm_scheduled_restore_paint_audit(
        self, effects, *, trace_id: str,
    ) -> None:
        """Audit an audit-marked restore paint from the moment it is SCHEDULED.

        (wh-overlay-slow-uia-stale-badges.21.5.) ``_apply_overlay_event``
        commits PAINTED synchronously and hands the effects to
        ``_perform_overlay_effects``, which only schedules the dispatch task.
        That task then waits for ``_overlay_effect_lock``, which an older batch
        holds across every awaited pin and unpin -- up to
        ``response_timeout_ms`` each. Arming only at enqueue time left that
        whole wait with the machine PAINTED, no paint on the display queue and
        no deadline behind it, which is the wrong-control window the audit
        exists to close.

        The budget is deliberately LARGER here than the display-round-trip
        budget, and the enqueue re-arm resets the clock, so an ordinary slow
        pin does not turn a healthy restore into a closed overlay and an error
        popup. See ``_overlay_restore_paint_schedule_budget_ms``.

        The same call records the paint's DELIVERY ENTITLEMENT
        (wh-overlay-slow-uia-stale-badges.21.8). Both are written here, at the
        one moment the machine has just committed the restore, so the audit and
        the permission to deliver it share a single arming site.
        """
        from services.wheelhouse.click_overlay_state import EffectKind

        for effect in effects:
            if effect.kind is not EffectKind.DISPATCH_PAINT:
                continue
            if not getattr(effect, "audit_delivery", False):
                continue
            self._overlay_start_restore_paint_audit(
                effect.overlay_session_id,
                effect.paint_generation,
                budget_ms=self._overlay_restore_paint_schedule_budget_ms(),
                trace_id=trace_id,
            )
            self._overlay_restore_paint_entitlement = (
                effect.overlay_session_id, effect.paint_generation,
            )
            return

    def _overlay_restore_paint_schedule_budget_ms(self) -> int:
        """The schedule-time audit budget: lock wait plus the display leg.

        The lock wait is bounded by the awaited requests a preceding batch can
        hold -- a PIN and an UNPIN, each capped at ``response_timeout_ms`` --
        so a healthy but slow machine finishes that batch inside the allowance
        and the enqueue re-arm then restarts the clock at the display budget.
        Reading the CONFIGURED bound rather than a fixed number is what keeps
        the audit quiet on a slow hardware tier: an operator who raises
        ``response_timeout_ms`` raises this allowance with it.

        The result is capped at ``_OVERLAY_RESTORE_SCHEDULE_BUDGET_MAX_MS``,
        because that configured bound has no upper limit of its own and the
        caller converts this number to a float delay
        (wh-overlay-slow-uia-stale-badges.21.10). The cap sits about 24.8 days
        out, so it bounds only values no operator writes.
        """
        timeout_ms = _OVERLAY_RESTORE_ACK_DEADLINE_MS
        config = self._host.click_config
        response_timeout_ms = getattr(config, "response_timeout_ms", None)
        try:
            lock_wait_ms = (
                int(response_timeout_ms) * _OVERLAY_RESTORE_LOCK_WAIT_REQUESTS
            )
        except (TypeError, ValueError, OverflowError):
            # OverflowError covers a non-finite float reaching this read; a
            # validated ClickConfig cannot hold one, but this reads the
            # attribute defensively rather than the validated type.
            lock_wait_ms = 0
        return min(
            timeout_ms + max(lock_wait_ms, 0),
            _OVERLAY_RESTORE_SCHEDULE_BUDGET_MAX_MS,
        )

    def _overlay_start_restore_paint_audit(
        self, sid: int, gen: int, *, budget_ms: int, trace_id: str,
    ) -> None:
        """Record ``(sid, gen)`` as the audited restore paint and arm its deadline."""
        self._overlay_cancel_restore_paint_audit()
        self._overlay_pending_restore_paint = (sid, gen)
        loop = self._host.loop
        if loop is None:
            return
        self._overlay_pending_restore_paint_timer = loop.call_later(
            budget_ms / 1000.0,
            lambda: self._fire_overlay_restore_paint_deadline(
                sid, gen, trace_id,
            ),
        )

    def _overlay_audit_restore_paint(
        self, effect, *, enqueued: bool, trace_id: str, loss: str,
    ) -> None:
        """Re-arm (or immediately fail) the audit for a restore paint.

        The audit already exists by the time this runs: the schedule-time arm
        started it before the dispatch task was created. This is the second
        arming, the one that gives the DISPLAY leg its own full budget
        measured from the enqueue rather than from the machine's commit.
        """
        if not getattr(effect, "audit_delivery", False):
            return
        sid = effect.overlay_session_id
        gen = effect.paint_generation
        if not enqueued:
            # A synchronous loss: nothing to wait for, recover now. Drop the
            # schedule-time deadline first, or it would fire later and report
            # the same incident a second time.
            self._overlay_cancel_restore_paint_audit(only_pair=(sid, gen))
            logger.error(
                "overlay: the restored numbered badges were never handed to "
                "the display process for generation (%s,%s) -- %s. Logic "
                "closes the overlay session so a spoken number cannot resolve "
                "against badges that are not on the screen; say 'apply "
                "numbers' to paint again. (trace_id=%s)",
                sid, gen, loss, trace_id,
            )
            self._recover_lost_restore_paint(sid, gen)
            return
        self._overlay_start_restore_paint_audit(
            sid, gen, budget_ms=_OVERLAY_RESTORE_ACK_DEADLINE_MS,
            trace_id=trace_id,
        )

    def _overlay_note_restore_paint_ack(
        self, sid: int, gen: int, state: str,
    ) -> None:
        """Resolve or fail the restore audit from a display ack.

        Only an ack at the AUDITED pair says anything about this paint:

        * ``painted`` -- the badges are on the screen. Resolve the audit.
        * ``failed`` -- the display tore its windows down and reported the
          failure. The badges are NOT on the screen, and the machine's PAINTED
          paint-ack cell treats a failed ack as bookkeeping (PAINTED has no
          path to error, r2.12), so this audit is what recovers.
        * ``cleared`` / ``expired`` -- these belong to a teardown, not to this
          paint. Leave the audit alone; the deadline callback re-validates
          against the live machine and drops a moot audit on its own.

        An ack at any other pair is not about the audited paint.
        """
        pending = getattr(self, "_overlay_pending_restore_paint", None)
        if pending is None or (sid, gen) != pending:
            return
        if state == "painted":
            self._overlay_cancel_restore_paint_audit()
            return
        if state == "failed":
            self._overlay_cancel_restore_paint_audit()
            logger.error(
                "overlay: the display process could not draw the restored "
                "numbered badges for generation (%s,%s). Logic closes the "
                "overlay session so a spoken number cannot resolve against "
                "badges that are not on the screen; say 'show numbers' to "
                "paint again.",
                sid, gen,
            )
            self._recover_lost_restore_paint(sid, gen)

    def _fire_overlay_restore_paint_deadline(
        self, sid: int, gen: int, trace_id: str,
    ) -> None:
        """Deadline callback: the restore paint was never confirmed.

        Race-safe shutdown backstop, like the clear watchdog's deadline
        (wh-overlay-slow-uia-stale-badges.18.9): shutdown() cancels the timer,
        but this callback can already be queued on the loop when the shutdown
        event is set. During shutdown the slot is cleared and nothing is
        reported -- an intentional exit is not an overlay fault.
        """
        self._overlay_pending_restore_paint_timer = None
        pending = getattr(self, "_overlay_pending_restore_paint", None)
        if pending != (sid, gen):
            return  # resolved or superseded since arming
        self._overlay_pending_restore_paint = None
        shutdown_event = self._host.shutdown_event
        if shutdown_event is not None and shutdown_event.is_set():
            return
        logger.error(
            "overlay: the display process did not confirm the restored "
            "numbered badges for generation (%s,%s) within "
            f"{_OVERLAY_RESTORE_ACK_DEADLINE_MS}ms. Logic closes the overlay "
            "session so a spoken number cannot resolve against badges that "
            "are not on the screen; say 'show numbers' to paint again. "
            "(trace_id=%s)",
            sid, gen, trace_id,
        )
        self._recover_lost_restore_paint(sid, gen)

    def _overlay_machine_is_painted_at(self, pair: "tuple[int, int]") -> bool:
        """Is the machine PAINTED at ``pair`` right now (wh-overlay-slow-uia-stale-badges.21.8)?

        Read defensively: a controller assembled via ``object.__new__`` in a
        test fixture may have no machine, and no machine can vouch for a
        restore paint, so answer False.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return False
        return machine.state is OverlayState.PAINTED and (
            machine.overlay_session_id, machine.paint_generation,
        ) == pair

    def _reconcile_overlay_restore_paint_entitlement(self) -> None:
        """Retire an audited restore that left its PAINTED state (wh-overlay-slow-uia-stale-badges.21.8).

        Called after EVERY overlay-machine apply (``_apply_overlay_event`` and
        ``handle_overlay_command``), like the sibling ``_reconcile_overlay_*``
        helpers, and reading the machine's CURRENT state rather than a
        prev/next edge, so it is idempotent and safe after any apply.

        The restore paint is scheduled while the machine is PAINTED at one
        pair. Several exits leave that state WITHOUT bumping the generation and
        WITHOUT dropping the pin -- the second MIC_PAUSE of ``_on_painted`` and
        the CLICK_COMPLETE entry to ``post_click_settling`` -- so the ordinary
        pair-and-pin fence still reads the queued paint as current and delivers
        badges the machine no longer wants. The other exits (HIDE_NUMBERS,
        SHOW_NUMBERS, FOCUS_CHANGE, an expired paint-ack) do move the pair or
        the pin, so the fence drops the paint, but they leave the schedule-time
        audit armed while the paint waits for ``_overlay_effect_lock``; its
        deadline then reports a user-visible lost restore for an overlay that
        is already off the screen. Retiring BOTH here covers the whole class in
        one place, so an exit added later is covered by construction.

        Retiring the audit early loses no protection: its recovery
        (``_apply_lost_restore_paint_hide``) re-validates that the machine is
        still PAINTED at the audited pair and does nothing when it is not, so
        every report this suppresses would have been followed by a no-op.
        """
        entitled = getattr(self, "_overlay_restore_paint_entitlement", None)
        if entitled is not None and not self._overlay_machine_is_painted_at(
            entitled,
        ):
            self._overlay_restore_paint_entitlement = None
        pending = getattr(self, "_overlay_pending_restore_paint", None)
        if pending is not None and not self._overlay_machine_is_painted_at(
            pending,
        ):
            self._overlay_cancel_restore_paint_audit(only_pair=pending)

    def _overlay_cancel_restore_paint_audit(
        self, *, only_pair: "tuple[int, int] | None" = None,
    ) -> None:
        """Drop the pending restore-paint audit and its deadline.

        ``only_pair`` makes the drop conditional: the audit is cancelled only
        when it is the one for that pair. Every caller that cancels because a
        SPECIFIC paint ended passes it, so a newer restore's audit -- armed
        while the older paint was still queued -- survives the older paint's
        outcome (wh-overlay-slow-uia-stale-badges.21.5).
        """
        if only_pair is not None:
            pending = getattr(self, "_overlay_pending_restore_paint", None)
            if pending != only_pair:
                return
        timer = getattr(self, "_overlay_pending_restore_paint_timer", None)
        if timer is not None:
            timer.cancel()
        self._overlay_pending_restore_paint_timer = None
        self._overlay_pending_restore_paint = None

    def _recover_lost_restore_paint(self, sid: int, gen: int) -> None:
        """Close the overlay session a lost restore paint left believed-visible.

        Applies a synthetic HIDE_NUMBERS through the standard
        ``_apply_overlay_event`` path -- the idiom the lease-expiry reconcile
        already uses -- so the machine's own hide cell dispatches the clear and
        the unpin and every post-apply reconciler runs. Deferred via
        ``loop.call_soon`` because the synchronous callers run inside a
        dispatching effect batch that holds ``_overlay_effect_lock``; the hop
        keeps the re-entry off that stack and preserves FIFO batch ordering.

        Re-validated at fire time: the machine must still be PAINTED at the
        audited pair. Anything else means the machine already moved on (a hide,
        a refresh, a new session), so there is no false belief left to correct.

        The clear-fault dedupe marker is NOT written here
        (wh-overlay-slow-uia-stale-badges.21.6). It belongs to the synthetic
        clear, and this half does not know yet whether that clear will ever
        exist; the deferred half writes it once its re-validation passes.
        """
        loop = self._host.loop
        if loop is None:
            return
        loop.call_soon(lambda: self._apply_lost_restore_paint_hide(sid, gen))

    def _apply_lost_restore_paint_hide(self, sid: int, gen: int) -> None:
        """Deferred half of ``_recover_lost_restore_paint`` (re-validates).

        Writes the clear-fault dedupe marker only on the leg that actually
        dispatches the synthetic hide (wh-overlay-slow-uia-stale-badges.21.6).
        The marker says "a fault at this pair was already reported to the
        user", and the hide's own clear is what it deduplicates. Written
        before the re-validation, it survived the no-op leg and then downgraded
        a LATER genuine clear fault at the same pair -- a real lost clear, from
        a real hide or mic pause -- to a WARNING the user never sees.

        Clearing it in the no-op leg instead would be wrong: this method cannot
        tell whether the value it would erase is its own premature write or a
        marker a different, genuine clear fault set in the meantime.
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if machine.state is not OverlayState.PAINTED or (
            machine.overlay_session_id, machine.paint_generation
        ) != (sid, gen):
            logger.info(
                "overlay: the lost restore paint for generation (%s,%s) needs "
                "no recovery; the machine already moved on (state=%s "
                "gen=(%s,%s))",
                sid, gen, machine.state.value, machine.overlay_session_id,
                machine.paint_generation,
            )
            return
        # The clear this hide dispatches carries the same pair the audit just
        # reported on. Mark that pair as already reported so the clear
        # watchdog's own deadline downgrades to a WARNING instead of popping a
        # second error at the user for one incident (the .18.3 idiom).
        self._overlay_clear_fault_reported_pair = (sid, gen)
        self._apply_overlay_event(
            OverlayEvent(kind=OverlayEventKind.HIDE_NUMBERS),
            source=f"restore-paint-lost gen=({sid},{gen})",
        )

    def _report_overlay_clear_fault(
        self, sid: int, gen: int, message: str, trace_id: str,
    ) -> None:
        """Report a clear fault: ERROR once per pair, WARNING on repeats.

        An ERROR record raises a user-visible popup via the error
        notifier (utils/error_notifier.py), independent of the GUI queue
        -- exactly what a lost GUI message needs. The per-pair downgrade
        keeps one broken pair (for example a pause-clear and a hide-clear
        against the same hung queue) from storming popups.
        """
        reported = getattr(self, "_overlay_clear_fault_reported_pair", None)
        if reported == (sid, gen):
            logger.warning(message, sid, gen, trace_id)
            return
        self._overlay_clear_fault_reported_pair = (sid, gen)
        logger.error(message, sid, gen, trace_id)

    def _put_overlay_walk_cue(
        self, active: bool, *, trace_id: str, walk_timeout_ms=None,
    ) -> None:
        """Put an overlay_walk_cue action on the GUI state queue (wh-n29v.117).

        A plain-dict side-channel notification (no shared/ schema) that drives
        the floating-button "walking" progress cue while a numbered-overlay
        walk is in flight. Reuses the same defensive enqueue path as the
        paint/clear effects so it rides the same channel and ordering, and
        degrades quietly (the cue is cosmetic) if the queue is unavailable.

        wh-n29v.118: on the active:True emit, ``walk_timeout_ms`` carries the
        cue bound the caller computes (``_overlay_dispatch_build``'s
        ``cue_bound_ms`` -- the build's own awaited window plus
        response_timeout_ms, plus the sentence-wait bound for a walk build)
        so the GUI fallback timer outlasts the real walk. The active:False
        emit omits it (the GUI ignores it on the clear path).
        """
        msg: dict = {
            "action": "overlay_walk_cue",
            "active": bool(active),
            "trace_id": trace_id,
        }
        if walk_timeout_ms is not None:
            msg["walk_timeout_ms"] = walk_timeout_ms
        self._put_overlay_gui_action(msg, trace_id=trace_id)

    def send_overlay_startup_reset(self) -> None:
        """Announce a fresh Logic to the GUI (wh-overlay-slow-uia-stale-badges.9).

        Sent once at startup, unconditionally (the previous Logic may have
        had the overlay enabled even if this one does not). The GUI
        responds by destroying any badge windows a dead Logic left behind,
        resetting its generation gate (this Logic numbers its
        (overlay_session_id, paint_generation) pairs from zero, which the
        old high-water mark would gate forever), and cancelling its badge
        lease. Side-channel dict like the walk cue (no shared/ schema).
        In the shipped launcher a Logic death tears down the GUI too, and
        every restart iteration creates fresh queues, so no queued message
        crosses a restart there. In dev runs where the GUI outlives Logic
        this reset clears leftover windows, the gate, and the lease, but
        it does NOT fence stale in-flight messages from the old Logic:
        the message schemas carry no Logic-incarnation identity, and both
        incarnations number their pairs from (1, 0). Full safety in that
        mode needs an incarnation epoch -- filed as
        wh-overlay-incarnation-epoch, deliberately not built for a
        dev-only topology.
        """
        self._put_overlay_gui_action(
            {"action": "reset_overlay"}, trace_id="logic-startup",
        )

    def _overlay_keepalive_summary(self, snapshot_id):
        """Resolve ``snapshot_id`` and re-put it to keep it alive past TTL (wh-n29v.95).

        PART 4 / criterion 4: a snapshot the overlay still RETAINS must not age
        out of the resolver cache. The machine keeps a snapshot pinned only
        while it still needs it, so re-putting on every pin/paint access and on
        every keepalive tick resets the TTL window for as long as it is needed.
        A SNAPSHOT_EXPIRED on a retained snapshot would otherwise misreport
        'no badge N' (in PAINTED) or leave the follow-on read nothing to compare
        against (in POST_CLICK_SETTLING). "Retained", not "visible": PAUSED and
        POST_CLICK_SETTLING both hold a pin with nothing on screen -- see
        ``_overlay_keepalive_states`` for the contract
        (wh-overlay-slow-uia-stale-badges.13.6). Returns the summary, or None
        when the snapshot is genuinely gone (never stored, or evicted under
        max_entries pressure).
        """
        if not snapshot_id:
            return None
        result = self.click_snapshot_summary_cache.resolve(snapshot_id)
        from services.wheelhouse.click_snapshot_summary_cache import CacheStatus

        if result.status is not CacheStatus.HIT or result.summary is None:
            return None
        # Re-put resets the TTL window (keepalive): the entry is still needed.
        try:
            self.click_snapshot_summary_cache.put(snapshot_id, result.summary)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "overlay: snapshot keepalive re-put failed for %r: %s",
                snapshot_id, exc,
            )
        return result.summary

    async def _overlay_send_pin(self, effect, trace_id: str) -> None:
        """Send pin_snapshot to Input and assign the tracked identity (wh-n29v.95).

        PART 5 / criterion 5: the pin point is when the overlay becomes
        visible/paused, so capture and store ``_overlay_tracked_identity`` here
        so the destroy-while-paused hook can register and the resume identity
        check works. The pin keepalive also re-puts the summary so the
        actively-pinned snapshot survives past TTL (criterion 4). Paint
        CORRECTNESS does not depend on this ack; it is consumed for bookkeeping
        only. Note (wh-n29v.120.1), however, that the ack await DOES delay paint
        dispatch in wall-clock time: PIN_SNAPSHOT runs before DISPATCH_PAINT in
        the same FIFO-locked effect batch, so a slow/hung pin ack postpones
        paint_overlay by up to response_timeout_ms. The walking-cue fallback
        timer (wh-n29v.117) is sized to account for this (it carries the
        build's own awaited window plus response_timeout_ms, plus the
        sentence-wait bound for a walk build, as walk_timeout_ms).
        """
        # wh-overlay-slow-uia-stale-badges.2.2.1: a SETTLE build already knows
        # which window its snapshot was read from, and the commit fence has
        # already confirmed that window against the foreground. Use THAT
        # identity rather than sampling again here. Sampling records whichever
        # window is in front at the pin instant, so a snapshot read from
        # window A gets branded with window B whenever the foreground moved --
        # and the two consumers of the recorded identity
        # (``overlay_snapshot_is_valid_on_resume`` and
        # ``_overlay_refresh_visible_window_is_foreground``) then compare B
        # against B and pass, which is the same race the fence closes. The
        # slot is consumed here, so a later pin of a different snapshot falls
        # back to a fresh sample as before.
        carried = None
        slot = getattr(self, "_overlay_settle_read_identity", None)
        if (
            slot is not None
            and effect.snapshot_id
            and slot[0] == effect.snapshot_id
        ):
            carried = slot[1]
            self._overlay_settle_read_identity = None
        # Capture the foreground identity the overlay is being built for.
        try:
            self._overlay_tracked_identity = (
                carried if carried is not None
                else self._capture_overlay_foreground_identity()
            )
            # Remember it per snapshot too (trigger B): the prior, still-visible
            # snapshot during a focus-change refresh needs its OWN recorded
            # window, not the latest pin's. Skip a None identity (undeterminable
            # -> routing falls back to its resolve-against-visible default).
            if effect.snapshot_id and self._overlay_tracked_identity is not None:
                self._overlay_snapshot_window_identity[effect.snapshot_id] = (
                    self._overlay_tracked_identity
                )
            # wh-overlay-rewalk-after-filter.1.3: the pin is the moment the
            # tracked identity becomes truthful for the visible badges, so the
            # build-time target has no further use. Leaving it set would make
            # the tree-change callback prefer it over the pin.
            self._overlay_pending_build_identity = None
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "overlay: failed to capture tracked identity at pin "
                "(trace_id=%s): %s", trace_id, exc,
            )
        # Keepalive the pinned (retained) snapshot.
        self._overlay_keepalive_summary(effect.snapshot_id)
        await self._overlay_send_pin_request(
            "pin_snapshot",
            {
                "overlay_session_id": effect.overlay_session_id,
                "snapshot_id": effect.snapshot_id or "",
                "paint_generation": effect.paint_generation,
            },
            trace_id=trace_id,
        )

    async def _overlay_send_unpin(self, effect, trace_id: str) -> None:
        """Send unpin_snapshot to Input (clear-by-identity) (wh-n29v.95)."""
        # Drop the per-snapshot window identity for the unpinned snapshot
        # (trigger B bookkeeping); the deferred prior unpin fires when a refresh
        # completes, so the prior's entry stays until then -- exactly while it is
        # the visible snapshot.
        if effect.snapshot_id:
            self._overlay_snapshot_window_identity.pop(effect.snapshot_id, None)
        await self._overlay_send_pin_request(
            "unpin_snapshot",
            {
                "overlay_session_id": effect.overlay_session_id,
                "snapshot_id": effect.snapshot_id or "",
            },
            trace_id=trace_id,
        )

    async def _overlay_send_refresh(
        self, snapshot_id: str, overlay_session_id: int, trace_id: str,
    ) -> None:
        """Send refresh_overlay_snapshot to Input to slide the store TTL.

        The Input-store counterpart to ``_overlay_keepalive_summary`` (which
        slides only the Logic resolver cache). The numbered overlay's snapshot
        lives in TWO independent 30s-TTL stores -- the Logic resolver cache and
        the Input-process ElementFinder store, both keyed off
        ``[click] snapshot_ttl_seconds``. Before this, the 15s keepalive re-put
        only the Logic cache; the Input copy aged out while Logic kept resolving
        and dispatching "click N", so the click failed with snapshot_expired on a
        still-visible overlay (wh-overlay-snapshot-keepalive trigger A). This
        sends the Input side a refresh so both stores slide together.

        Best-effort, like pin/unpin: a lost ack does not corrupt state (the
        store ages out normally from its last refresh if this never lands).
        """
        if not snapshot_id:
            return
        await self._overlay_send_pin_request(
            "refresh_overlay_snapshot",
            {
                "overlay_session_id": overlay_session_id,
                "snapshot_id": snapshot_id,
            },
            trace_id=trace_id,
        )

    async def _overlay_send_pin_request(
        self, action: str, params: dict, *, trace_id: str,
    ) -> None:
        """Send a pin/unpin request and consume the PinSnapshotResponse ack.

        Best-effort bookkeeping: Logic does not block the paint on this ack. A
        timeout / send failure / malformed reply logs and is dropped (the pin is
        defence-in-depth over LRU + TTL, so a lost ack does not corrupt state).
        """
        from utils.trace_context import set_trace

        set_trace(trace_id)
        timeout_ms = self._host.click_config.response_timeout_ms
        timeout_s = max(timeout_ms / 1000.0, 0.1)
        try:
            raw = await self._host.app.send_request(
                action, params=params, timeout_s=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "overlay: %s no reply within %dms (trace_id=%s)",
                action, timeout_ms, trace_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 -- CancelledError propagates
            logger.warning(
                "overlay: %s send_request failed (trace_id=%s): %s",
                action, trace_id, exc,
            )
            return
        from services.wheelhouse.shared.pin_snapshot import (
            PinSnapshotResponse,
            PinSnapshotResponseSchemaError,
        )

        try:
            resp = PinSnapshotResponse.from_dict(raw)
        except (
            PinSnapshotResponseSchemaError, ValueError, KeyError, TypeError,
        ) as exc:
            logger.warning(
                "overlay: malformed %s response (trace_id=%s): %s",
                action, trace_id, exc,
            )
            return
        if resp.status != "ok" or (action == "pin_snapshot" and not resp.pinned):
            logger.debug(
                "overlay: %s ack status=%s reason=%s pinned=%s "
                "(snapshot=%s, trace_id=%s)",
                action, resp.status, resp.reason, resp.pinned,
                resp.snapshot_id, trace_id,
            )

    def _overlay_fire_notice(self, effect, trace_id: str) -> None:
        """Fire a FIRE_NOTICE effect (wh-n29v.95).

        A FIRE_NOTICE carrying a ClickNoticeEvent forwards that suppressed
        notice (the auto-open ambiguous-click notice). A FIRE_NOTICE whose
        ``notice`` is None is the marker for the generic standalone
        "numbers couldn't be drawn" notice for a failed standalone walk
        (wh-n29v.16.1): build an execution_failed ClickNoticeEvent here.
        """
        notice = effect.notice
        if notice is not None:
            self.forward_click_notice(
                outcome=notice.outcome,
                reason=notice.reason,
                matched_name=notice.matched_name,
                matched_names=notice.matched_names,
                spoken_name=notice.spoken_name,
                snapshot_id=notice.snapshot_id,
                trace_id=notice.trace_id or trace_id,
            )
            return
        # Generic standalone walk-failure notice.
        self.forward_click_notice(
            outcome="execution_failed",
            reason="numbers_not_drawn",
            matched_name=None,
            matched_names=(),
            spoken_name="",
            snapshot_id=None,
            trace_id=trace_id,
        )

    def _overlay_arm_timer(self, effect, trace_id: str) -> None:
        """Arm the single per-state timeout timer (wh-n29v.95).

        Cancels any existing timer first (exactly one timer is live at a time;
        the machine emits CANCEL_TIMER before a new ARM where needed, but cancel
        here too as a belt-and-suspenders). Schedules a ``loop.call_later`` for
        ``effect.duration_ms`` that, on fire, feeds a TIMEOUT OverlayEvent at the
        ARMED (overlay_session_id, paint_generation) through
        ``_apply_overlay_event``. The machine's pre-table generation gate drops
        the TIMEOUT if the generation has since advanced, so a stale armed timer
        cannot abort a newer walk.
        """
        existing = getattr(self, "_overlay_timer", None)
        if existing is not None:
            existing.cancel()
        sid = effect.overlay_session_id
        gen = effect.paint_generation
        self._overlay_timer_pair = (sid, gen)
        armed_state = effect.timer_state
        self._overlay_armed_timer_state = armed_state
        # wh-overlay-timer-cancel-race: the callback carries the state it was
        # armed for and the id of this arm, so it can check both on fire.
        arm_id = getattr(self, "_overlay_timer_arm_id", 0) + 1
        self._overlay_timer_arm_id = arm_id
        delay_s = max(effect.duration_ms / 1000.0, 0.0)
        self._overlay_timer = self._host.loop.call_later(
            delay_s,
            lambda: self._fire_overlay_timeout(
                sid, gen, trace_id, arm_id=arm_id, armed_state=armed_state,
            ),
        )

    def _overlay_cancel_timer(self) -> None:
        """Cancel the current per-state timeout timer, if any (wh-n29v.95)."""
        existing = getattr(self, "_overlay_timer", None)
        if existing is not None:
            existing.cancel()
        self._overlay_timer = None
        self._overlay_timer_pair = None
        self._overlay_armed_timer_state = None

    def _fire_overlay_timeout(
        self,
        sid: int,
        gen: int,
        trace_id: str,
        *,
        arm_id: int,
        armed_state: "OverlayState | None",
    ) -> None:
        """Timer callback: feed a TIMEOUT for the arm this callback belongs to.

        Feeds the TIMEOUT OverlayEvent at the armed pair (wh-n29v.95), behind
        two checks that answer two different questions
        (wh-overlay-timer-cancel-race). The pair answers neither of them.

        ``arm_id`` decides whether this callback still owns the bookkeeping. A
        callback whose timer has been replaced must clear nothing: the three
        attributes below now describe the LIVE arm, and clearing them would
        leave that timer with no handle to cancel. A replacement arm can carry
        the same pair AND the same state, so only the per-arm id separates
        them.

        ``armed_state`` decides whether a TIMEOUT is fed at all. Four
        transitions keep the (overlay_session_id, paint_generation) pair the
        timer was armed for -- WALK_IN_FLIGHT to PAINT_IN_FLIGHT,
        WALK_IN_FLIGHT to PAUSED, PAINT_IN_FLIGHT to PAINTED, and a refresh
        resolving to PAINTED -- so the machine's generation gate cannot reject
        the event. Each of them emits CANCEL_TIMER, but that effect is
        dispatched as a task and cannot run until a later loop turn, while a
        callback already in the ready queue runs in this one. What HAS changed
        by then is the machine's own state, so the armed state is compared
        against the MACHINE, not against the timer bookkeeping (which nothing
        but an arm or a cancel ever rewrites, and neither has run). A timer
        armed with no state cannot prove the machine is still where it was, and
        that one comparison already refuses it: ``None`` is no machine state,
        so it can never match one. A separate ``armed_state is None`` branch
        was written first and removed -- the mutation gate showed it changed
        nothing, which is what a dead branch does.

        When the id is current but the state has moved on, the timer really did
        expire and its bookkeeping is spent: clear it, and feed nothing.
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
        )

        if arm_id != getattr(self, "_overlay_timer_arm_id", 0):
            return
        self._overlay_timer = None
        self._overlay_timer_pair = None
        self._overlay_armed_timer_state = None
        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if machine.state is not armed_state:
            return
        self._apply_overlay_event(
            OverlayEvent(
                kind=OverlayEventKind.TIMEOUT,
                overlay_session_id=sid,
                paint_generation=gen,
            ),
            source=f"timeout gen=({sid},{gen})",
        )

    # ------------------------------------------------------------------
    # wh-n29v.21: Logic-side focus-hook wiring (foreground + transient destroy
    # + resume full-identity check). Started in main(), stopped in shutdown().
    # ------------------------------------------------------------------
    def start_overlay_focus_hooks(self) -> None:
        """Start the overlay focus-hook thread when the overlay is active.

        Mirrors window_mover's start: a background daemon thread owns the
        message-only window, the FOREGROUND ``SetWinEventHook``, and the
        transient destroy hook. The callbacks marshal onto this Logic loop via
        ``call_soon_threadsafe``. Best-effort: a failure to start logs and
        leaves overlay focus-following disabled rather than aborting startup.
        Gated on BOTH ``click_config.enabled`` AND
        ``click_config.overlay_enabled_effective``: when voice clicking is off
        (master opt-out or a bad Phase 1 key) OR the overlay alone is off (a bad
        overlay key, or an ``overlay_enabled=false`` opt-out) the hook is not
        started (no IPC, no walk, no focus following). A valid ``enabled=false``
        leaves ``overlay_enabled_effective`` True, so the ``enabled`` term is
        required. This matches the Input-side overlay walk gate
        (``OverlaySnapshotActions._get_overlay_walk_finder``) so the Logic and
        Input processes agree on whether the overlay is on; by-name click
        stays gated on ``enabled`` alone.
        """
        if not (
            self._host.click_config.enabled
            and self._host.click_config.overlay_enabled_effective
        ):
            logger.info(
                "overlay: focus hooks not started (numbered overlay not "
                "active; enabled=%s, overlay_invalid_key=%s).",
                self._host.click_config.enabled,
                self._host.click_config.overlay_invalid_key,
            )
            return
        if self._overlay_focus_hooks is not None:
            return
        try:
            manager = OverlayFocusHookManager(
                loop=self._host.loop,
                on_foreground=self._on_overlay_foreground_change,
                on_destroy=self._on_overlay_focused_hwnd_destroyed,
                on_menu_popup=self._on_overlay_menu_popup_change,
            )
            if manager.start():
                self._overlay_focus_hooks = manager
            else:
                logger.warning(
                    "overlay: focus-hook manager failed to start; overlay "
                    "focus following is disabled this session.",
                )
        except Exception as exc:  # noqa: BLE001 -- never abort startup
            logger.error(
                "overlay: error starting focus hooks: %s", exc, exc_info=True,
            )

    def stop_overlay_focus_hooks(self) -> None:
        """Stop the focus-hook thread and unhook all hooks (no leaked hooks)."""
        manager = self._overlay_focus_hooks
        if manager is None:
            return
        try:
            manager.stop()
        except Exception as exc:  # noqa: BLE001 -- never block shutdown
            logger.debug("overlay: error stopping focus hooks: %s", exc)
        self._overlay_focus_hooks = None
        self._overlay_destroy_hook_active = False
        self._cancel_overlay_settle_refire()

    def _build_overlay_focus_debouncer(self) -> "FocusChangeDebouncer":
        """Build the focus-change debouncer from the VALIDATED overlay config.

        The debounce interval is ``self.click_config.overlay_focus_debounce_ms``
        -- the value the ``ClickConfig`` validator already range-checked against
        ``[0, 5000]`` (an out-of-range raw value degrades to the 250 default and
        disables the overlay). Reading it through the validated config rather
        than re-reading the raw ``[click]`` block guarantees the Logic process
        and the Input process derive the SAME debounce from the same raw block,
        so they can never disagree on the validated config (wh-n29v.66). This is
        a small method so the wiring is testable via the
        ``MagicMock(spec=LogicController)`` bound-method pattern without driving
        the heavy full ``__init__``.
        """
        from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer

        return FocusChangeDebouncer(
            debounce_ms=self._host.click_config.overlay_focus_debounce_ms
        )

    def _on_overlay_foreground_change(self, hwnd: int) -> None:
        """Loop-marshalled FOREGROUND callback: debounce, then feed FOCUS_CHANGE.

        Runs on the Logic asyncio loop (the hook thread already marshalled
        here). Applies the pure debounce; a coalesced event returns without
        touching the machine. On a real change it maps the raw event to a
        ``FOCUS_CHANGE`` ``OverlayEvent`` and applies it via
        ``_apply_overlay_event`` (which gates its trace-id mint and INFO log on
        the outcome -- a closed-state NO_OP, the all-day common case, logs only
        DEBUG). The debounce only COALESCES rapid foreground callbacks
        (tooltip-as-foreground artifacts, Alt-Tab steps); it does NOT enforce
        generation supersession -- that is a wire-level guarantee of the
        ``(overlay_session_id, paint_generation)`` gate inside the state machine
        (design v4 "Supersession generation"), not a producer-side check here.
        """
        if not (
            self._host.click_config.enabled
            and self._host.click_config.overlay_enabled_effective
        ):
            return
        from services.wheelhouse.overlay_focus_hooks import map_foreground_event

        now_ms = self._clock() * 1000.0
        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):
            _overlay_focus_logger.debug(
                "overlay: foreground change to hwnd=%s coalesced by debounce.",
                hwnd,
            )
            # The refire replays ONE event. It must remember that a
            # foreground event was among the coalesced ones, including when
            # the refire is already armed (the arm call below then returns
            # early). Whether it counts as a change of window is decided at
            # the refire, against the window that is in front then: a burst
            # that ends back on the numbered window is not a change. Only
            # while numbers are on the screen and the key is off: an event
            # coalesced during the first walk (item 11) or while the overlay
            # is closed is not a change of window, and no timer is armed to
            # clear a flag set then.
            if self._overlay_numbers_can_be_dismissed():
                self._overlay_settle_refire_foreground = True
            self._arm_overlay_settle_refire("foreground change")
            return
        self._cancel_overlay_settle_refire()
        self._apply_overlay_event(
            map_foreground_event(
                hwnd=hwnd,
                dismisses_numbers=self._foreground_change_dismisses_numbers(
                    hwnd
                ),
            ),
            source=f"foreground change hwnd={hwnd}",
        )

    def _overlay_numbers_can_be_dismissed(self) -> bool:
        """True when a change of window would remove numbers that are showing.

        wh-numbers-stay-setting: ``overlay_keep_numbers`` is off AND the
        machine is in ``painted`` or ``refresh_in_flight``. In every other
        state (the first walk and paint, closed, paused, ...) a foreground
        change keeps today's behaviour (item 11).
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None or getattr(machine, "keep_numbers", True):
            return False
        return machine.state in (
            OverlayState.PAINTED, OverlayState.REFRESH_IN_FLIGHT,
        )

    def _consume_pending_foreground_dismissal(self) -> bool:
        """Settle a coalesced foreground event: does the window in front count?

        wh-numbers-stay-setting, item 2b. Returns False and changes nothing
        when no foreground event was coalesced (``_overlay_settle_refire_
        foreground`` is clear). Otherwise clears the flag and runs
        ``_foreground_change_dismisses_numbers`` against the window that is in
        front NOW (``_overlay_current_foreground_hwnd``), so a burst that ends
        back on the numbered window keeps the refresh and a burst that ends on
        another application removes the numbers. An unreadable foreground
        window keeps the refresh. When no numbers are on the screen
        (``_overlay_numbers_can_be_dismissed`` is False), it clears the flag
        and returns False without reading the window. Never raises.
        """
        if not getattr(self, "_overlay_settle_refire_foreground", False):
            return False
        self._overlay_settle_refire_foreground = False
        if not self._overlay_numbers_can_be_dismissed():
            return False
        hwnd = _overlay_current_foreground_hwnd()
        if not hwnd:
            return False
        return self._foreground_change_dismisses_numbers(hwnd)

    def _foreground_change_dismisses_numbers(self, hwnd: int) -> bool:
        """True when a foreground change is the user moving to another window.

        wh-numbers-stay-setting, items 2b, 10 and 11. With ``[click]
        overlay_keep_numbers`` off, a change of window removes the numbers. The
        answer is False (the change keeps today's refresh) when:

          * the machine is not showing numbers (``painted`` or
            ``refresh_in_flight``). During the first walk and paint after "show
            numbers" the app reads the new window instead (item 11);
          * ``keep_numbers`` is on (the machine ignores the flag then, so the
            Win32 lookup is skipped);
          * the window cannot be read (a refresh is always safe);
          * the new foreground window is a tooltip (``tooltips_class32``);
          * it is the numbered window itself: the tracked window handle, or a
            window inside the same top-level window (``GA_ROOT``, compared
            against ``_overlay_tracked_identity``). A dialog that the numbered
            window OWNS (an Open dialog from Ctrl+O) is a top-level window of
            its own, so it is a change of window (ruling R-B, option 1);
          * it belongs to a WheelHouse process (``_wheelhouse_process_ids``).
            This check comes last because it scans processes; a return to the
            numbered window never pays for it.

        Never raises.
        """
        if not self._overlay_numbers_can_be_dismissed():
            return False
        try:
            facts = _overlay_window_facts(hwnd)
            if facts is None:
                return False
            if facts.class_name.lower() in _OVERLAY_NON_WINDOW_CHANGE_CLASSES:
                return False
            tracked = getattr(self, "_overlay_tracked_identity", None)
            if tracked is not None:
                if int(hwnd) == int(tracked.hwnd):
                    return False
                numbered_facts = _overlay_window_facts(tracked.hwnd)
                numbered_root = (
                    numbered_facts.root if numbered_facts is not None
                    else int(tracked.hwnd)
                )
                if facts.root == numbered_root:
                    return False
            if facts.pid in self._host.wheelhouse_process_ids():
                return False
            return True
        except Exception as exc:  # noqa: BLE001 -- never break focus following
            _overlay_focus_logger.debug(
                "overlay: window-change check for hwnd=%s failed (%s); "
                "keeping the refresh.", hwnd, exc,
            )
            return False

    def _on_overlay_menu_popup_change(self, event_id: int) -> None:
        """Loop-marshalled MENUPOPUP callback: map, debounce, feed FOCUS_CHANGE.

        wh-overlay-menu-close-stale: a menu closing (or opening) over the
        focused window changes what is clickable WITHOUT a foreground change,
        so the overlay's badge set must be rebuilt exactly as a focus change
        rebuilds it -- ``map_menu_popup_event`` documents why reusing
        ``FOCUS_CHANGE`` is the deliberate design. Mapping runs BEFORE the
        debounce so an id the mapper rejects cannot burn the debounce window
        (a real menu-close right after would be coalesced and the stale
        badges would survive). The debouncer is SHARED with the foreground
        callback on purpose: both event sources mean "re-walk against current
        reality", so one walk serves a focus change and a menu event landing
        together. Unlike the foreground callback this does NOT touch the
        tracked identity -- the foreground window is unchanged.
        """
        if not (
            self._host.click_config.enabled
            and self._host.click_config.overlay_enabled_effective
        ):
            return
        from services.wheelhouse.overlay_focus_hooks import (
            EVENT_SYSTEM_MENUPOPUPEND,
            map_menu_popup_event,
        )

        event = map_menu_popup_event(event_id=event_id)
        if event is None:
            return
        now_ms = self._clock() * 1000.0
        verb = "closed" if event_id == EVENT_SYSTEM_MENUPOPUPEND else "opened"
        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):
            _overlay_focus_logger.debug(
                "overlay: menu pop-up event %#06x coalesced by debounce.",
                event_id,
            )
            self._arm_overlay_settle_refire(f"menu popup {verb}")
            return
        # A foreground event coalesced earlier is still owed its decision; the
        # cancel below would drop it, so settle it into this event first.
        if self._consume_pending_foreground_dismissal():
            event = dataclasses.replace(event, dismisses_numbers=True)
        self._cancel_overlay_settle_refire()
        self._apply_overlay_event(event, source=f"menu popup {verb}")

    def on_pattern_manager_tree_change(self, hwnd: int, sequence: int) -> None:
        """Loop-marshalled Pattern Manager tree change: decide, debounce, refresh.

        wh-overlay-rewalk-after-filter. Typing in the Pattern Manager's filter
        box hides rows, so badges painted from an earlier walk float over rows
        that are no longer there. The window announces NOTHING a Logic-side
        hook could see (measured on this bead: zero UIA StructureChanged events
        and zero WinEvents 0x8000..0x8004 for a filter change, against a probe
        control of 150 of each over a page that adds and removes a control
        every 200 ms), so the GUI process -- which knows its own model changed
        -- sends the event, and this callback turns it into the same re-walk a
        menu pop-up produces.

        ``decide_tree_change`` runs BEFORE the debounce, for the same reason
        ``map_menu_popup_event`` does: an event this window's overlay has no
        use for must not burn the shared debounce window, or a real change
        landing right after would be coalesced and the stale badges would
        survive. Each drop writes exactly one log line naming its reason
        (contract C2). An accepted event records its ``(hwnd, sequence)``
        FIRST, before the debounce decides: the event WAS applied to the newest
        state either by this call's walk or by the settle timer's, so a later
        duplicate of the same sequence is stale whichever way the debounce
        went.

        Like the menu pop-up callback, this does NOT touch
        ``_overlay_tracked_identity`` -- the foreground window does not change
        when a dialog re-filters its own tree. It does READ a window handle,
        which the menu pop-up callback does not, and that is why it needs two
        sources (wh-overlay-rewalk-after-filter.1.3). While a build is in
        flight the pinned identity is stale or absent, because the pin runs
        only after the build response comes back, so the handle to judge
        against is ``_overlay_pending_build_identity`` -- the window the
        in-flight build was dispatched for. In every other accepted state the
        badges on screen ARE the pinned ones, so the pinned identity is the
        right source; reading the pending target there would judge against a
        window whose badges never reached the screen, which is what a refresh
        that falls back to its prior pin (``_refresh_fall_back`` in
        click_overlay_state.py) leaves behind.
        """
        if not (
            self._host.click_config.enabled
            and self._host.click_config.overlay_enabled_effective
        ):
            return
        from services.wheelhouse.overlay_focus_hooks import decide_tree_change

        from services.wheelhouse.click_overlay_state import OverlayState

        state = self.click_overlay_state.state
        pending = self._overlay_pending_build_identity
        if pending is not None and state in (
            OverlayState.WALK_IN_FLIGHT,
            OverlayState.PAINT_IN_FLIGHT,
            OverlayState.REFRESH_IN_FLIGHT,
        ):
            tracked = pending
        else:
            tracked = self._overlay_tracked_identity
        decision = decide_tree_change(
            state=state,
            tracked_hwnd=tracked.hwnd if tracked is not None else 0,
            event_hwnd=hwnd,
            event_sequence=sequence,
            last_hwnd=self._pm_tree_change_last_hwnd,
            last_sequence=self._pm_tree_change_last_sequence,
        )
        if decision.event is None:
            _overlay_focus_logger.debug(
                "overlay: pattern manager tree change hwnd=%s seq=%s dropped "
                "(%s).",
                hwnd,
                sequence,
                decision.drop_reason,
            )
            return

        self._pm_tree_change_last_hwnd = hwnd
        self._pm_tree_change_last_sequence = sequence

        now_ms = self._clock() * 1000.0
        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):
            _overlay_focus_logger.debug(
                "overlay: pattern manager tree change hwnd=%s seq=%s "
                "coalesced by debounce.",
                hwnd,
                sequence,
            )
            self._arm_overlay_settle_refire("pattern manager tree change")
            return
        event = decision.event
        # As in ``_on_overlay_menu_popup_change``: settle a coalesced
        # foreground event into this event before the cancel drops it.
        if self._consume_pending_foreground_dismissal():
            event = dataclasses.replace(event, dismisses_numbers=True)
        self._cancel_overlay_settle_refire()
        self._apply_overlay_event(
            event,
            source=f"pattern manager tree change hwnd={hwnd} seq={sequence}",
        )

    def _arm_overlay_settle_refire(self, source: str) -> None:
        """Arm ONE trailing settle timer for the debounce-window remainder.

        wh-overlay-nested-dupes.1.1: the drop-only debounce loses the FINAL
        event of a burst -- double-Escape out of a submenu (the second close is
        coalesced, dead parent-menu badges persist), open-then-instant-dismiss,
        and a menu action whose new dialog's foreground event lands inside the
        window the menu close just anchored. While painted, the debounced
        events are the ONLY thing that rebuilds the badge set, so a lost final
        event leaves stale badges indefinitely. The settle timer fires at the
        window edge and re-applies one FOCUS_CHANGE, so a burst still costs at
        most one extra walk while the final state is never lost. At most one
        settle is pending; a real (non-coalesced) fire cancels it.

        Runs only on the Logic asyncio loop (the hook callbacks marshal here,
        and ``loop.call_later`` callbacks run here), so no lock is needed.

        Never arms while the machine is CLOSED (wh-overlay-nested-dupes.1.5):
        with no overlay session there are no badges to clean up, so a settle
        serves no purpose -- and a timer armed during the CLOSED period could
        outlive it, surviving into a 'show numbers' / auto-open session that
        starts inside the remaining window and superseding that session's
        user-requested walk with a stale FOCUS_CHANGE. Together with the
        cancel-on-CLOSED reconciler this enforces the invariant "no settle is
        ever pending while CLOSED", so no stale settle can cross a session
        boundary. The gate also covers the timer body's re-arm path.
        """
        if self._overlay_settle_handle is not None:
            return
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None or machine.state is OverlayState.CLOSED:
            return
        delay_ms = self._overlay_focus_debouncer.remaining_ms(
            now_ms=self._clock() * 1000.0
        )
        self._overlay_settle_handle = self._host.loop.call_later(
            delay_ms / 1000.0, self._on_overlay_settle_refire, source,
        )

    def _on_overlay_settle_refire(self, source: str) -> None:
        """The settle timer body: apply the deferred FOCUS_CHANGE, or re-arm.

        If the debounce window is still hot (a real event fired after this
        timer was armed and advanced the anchor), do not touch the machine --
        re-arm for the new remainder so the guarantee ("the burst's final
        state gets one walk") still holds. Otherwise fire one FOCUS_CHANGE
        through the normal path; a closed/paused/error machine treats it as
        the usual record-only no-op.
        """
        self._overlay_settle_handle = None
        if not (
            self._host.click_config.enabled
            and self._host.click_config.overlay_enabled_effective
        ):
            return
        now_ms = self._clock() * 1000.0
        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):
            self._arm_overlay_settle_refire(source)
            return
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
        )

        # wh-numbers-stay-setting: the refire replays one event. When a
        # foreground event was coalesced, the window in front NOW decides
        # whether the burst was a change of window.
        dismisses = self._consume_pending_foreground_dismissal()
        self._apply_overlay_event(
            OverlayEvent(
                kind=OverlayEventKind.FOCUS_CHANGE,
                dismisses_numbers=dismisses,
            ),
            source=f"{source} (settled)",
        )

    def _cancel_overlay_settle_refire(self) -> None:
        """Cancel a pending settle timer (a real fire made it redundant)."""
        self._overlay_settle_refire_foreground = False
        handle = self._overlay_settle_handle
        if handle is not None:
            handle.cancel()
            self._overlay_settle_handle = None

    def _on_overlay_focused_hwnd_destroyed(self, destroyed_hwnd: int) -> None:
        """Loop-marshalled DESTROY callback: feed FOCUSED_HWND_DESTROYED if tracked.

        Runs on the Logic loop. The transient destroy hook is scoped to the
        tracked window's pid/tid, but a process emits ``EVENT_OBJECT_DESTROY``
        for many objects; ``map_destroy_event`` narrows to the EXACT tracked
        top-level HWND. A non-matching or untracked destroy is dropped. A match
        feeds ``FOCUSED_HWND_DESTROYED`` (drives ``paused -> closed``).
        """
        if not (
            self._host.click_config.enabled
            and self._host.click_config.overlay_enabled_effective
        ):
            return
        # wh-n29v.24.2: drop a destroy callback that arrives after the transient
        # hook was logically unregistered. The hook is unregistered
        # asynchronously (``unregister_destroy_hook`` POSTs to the hook thread,
        # which processes it on its ~20 ms PeekMessage poll), so a destroy
        # firing in the gap between leaving ``paused`` and the worker thread
        # processing the unregister would otherwise feed FOCUSED_HWND_DESTROYED
        # into a non-``paused`` machine -> INVALID_TRANSITION -> ERROR. The hook
        # is live only while ``paused``; ``_reconcile_overlay_destroy_hook``
        # clears ``_overlay_destroy_hook_active`` synchronously on this same loop
        # when leaving ``paused``, so a False flag here means we have already
        # requested the unregister and any destroy callback is a stale artifact
        # of that gap. Dropping it keeps the state machine's fail-closed contract
        # intact and does not block the loop.
        if not self._overlay_destroy_hook_active:
            return
        from services.wheelhouse.overlay_focus_hooks import map_destroy_event

        tracked = self._overlay_tracked_identity
        tracked_hwnd = tracked.hwnd if tracked is not None else 0
        event = map_destroy_event(
            destroyed_hwnd=destroyed_hwnd, tracked_hwnd=tracked_hwnd,
        )
        if event is None:
            return
        self._apply_overlay_event(
            event,
            source=(
                f"tracked window hwnd={destroyed_hwnd} destroyed while paused"
            ),
        )

    def _apply_overlay_event(self, event, *, source: str) -> None:
        """Apply an OverlayEvent, hand off effects, reconcile session-end state.

        Single Logic-loop entry point for the focus-hook-produced events
        (FOCUS_CHANGE, FOCUSED_HWND_DESTROYED). The machine is pure; this
        applies the event, hands any returned effects to the
        ``_perform_overlay_effects`` seam (the integration bead performs the
        real IPC), resets the focus debouncer when the session ends (entry to
        ``closed``), and reconciles the transient destroy hook against the
        RESULTING machine state -- registering it on entry to ``paused`` and
        unregistering it on leaving ``paused`` -- so the hook is live exactly
        while the overlay is paused (bounded cost, no leaked hooks).

        Logging is outcome-gated (wh-n29v.22.3): the overwhelmingly common case
        is a foreground change while the machine is ``closed`` (a NO_OP). That
        case logs a single DEBUG line and mints NO trace id, so ordinary all-day
        window switching neither spews INFO lines nor churns uuids. Only an
        event that actually changes state or carries effects mints a trace id
        and logs at INFO.
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayOutcome,
            OverlayState,
        )

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        prev_state = machine.state
        prev_pair = (machine.overlay_session_id, machine.paint_generation)
        event = self._overlay_mark_visible_window_left(machine, event)
        result = machine.apply(event)
        # wh-overlay-slow-uia-stale-badges.2.2.4 and .24. Wake a parked build
        # the transition just made stale, after EVERY apply on this path.
        #
        # .2.2.4 covered only transitions out of POST_CLICK_SETTLING, which is
        # the path the settling TIMEOUT arrives on (871a52a7 arms that timer
        # BEFORE the settle build, so the deadline can expire mid-send). .24
        # removes that restriction, because four more cells commit a pair that
        # makes the active build stale and leave it parked just the same:
        # WALK_IN_FLIGHT SHOW_NUMBERS and FOCUS_CHANGE through
        # ``_restart_walk``, and REFRESH_IN_FLIGHT SHOW_NUMBERS and
        # FOCUS_CHANGE through ``_refresh_supersede``. Leaving one parked
        # holds ``_overlay_effect_lock`` for the rest of the build's own
        # awaited window (screen_read_timeout_ms for a walk build; wh-overlay-
        # slow-uia-stale-badges.3), so the walk this very transition just
        # dispatched cannot start until a dead request times out.
        #
        # Calling it unconditionally is safe because the helper decides for
        # itself: it wakes a parked send only when the machine has actually
        # left it behind (state CLOSED, or a current pair that no longer
        # matches the parked one), so an apply that changes nothing -- or one
        # the machine rejected -- wakes nothing. It runs BEFORE the effects
        # below, so the pair it compares against is the one this apply
        # committed and the parked send is still the pre-transition one.
        self._abort_inflight_overlay_build()
        if result.outcome is OverlayOutcome.NO_OP and not result.effects:
            _overlay_focus_logger.debug(
                "overlay: %s -> %s no_op (state=%s)",
                source, event.kind.value, machine.state.value,
            )
        else:
            trace_id = self._mint_overlay_trace_id()
            logger.info(
                "overlay: %s -> applied %s state=%s outcome=%s "
                "(%d effect(s), trace_id=%s)",
                source, event.kind.value, machine.state.value,
                result.outcome.value, len(result.effects), trace_id,
            )
            if result.effects:
                self._perform_overlay_effects(result.effects, trace_id=trace_id)
        # wh-n29v.22.2 / wh-n29v.23.2: clear the debounce anchor only on the
        # actual TRANSITION into closed (a session ending), not whenever the
        # machine merely happens to already be closed. Resetting on every
        # closed-state foreground change would defeat the debounce for the
        # all-day idle path (each event would fire as a first event).
        if (
            machine.state is OverlayState.CLOSED
            and prev_state is not OverlayState.CLOSED
        ):
            self._overlay_focus_debouncer.reset()
        # wh-n29v.121: stamp the LATEST transition into PAINTED so the browser
        # proactive refresh measures age from the most recent paint. An edge,
        # not a level: re-stamping on every apply while already PAINTED would
        # never let the age reach the trust window.
        if (
            machine.state is OverlayState.PAINTED
            and prev_state is not OverlayState.PAINTED
        ):
            self._overlay_last_paint_monotonic = self._overlay_now_monotonic()
            # wh-overlay-slow-uia-stale-badges.4: arm the renumber guard on
            # EVERY transition that puts a DIFFERENT snapshot on screen -- the
            # browser timer's proactive refresh, a focus- or menu-triggered
            # refresh, the post-click settle re-read, and a re-said "show
            # numbers" alike. Badge numbers are positional (uia_walker.py
            # increments display_number in walk order), so one control added
            # or removed anywhere renumbers everything after it; after a
            # content change, badge N keeping its meaning is the unlikely
            # case. A "click N" the user began before the swap has to be
            # checked against the PRE-swap badge identities before it resolves
            # against the new list.
            #
            # The comparison is the pin now painted against the pin recorded
            # when the repaint STARTED (``_overlay_repaint_entry_pin``, set on
            # the way into REFRESH_IN_FLIGHT and POST_CLICK_SETTLING below),
            # and that is what tells a swap from a restore. A failed refresh
            # build, a failed refresh paint, a refresh timeout and a settle
            # re-read that found nothing changed all reach PAINTED holding
            # the pin they started with, and none of them arms the guard.
            # A paint with no recorded entry pin -- the first paint of a
            # session, and the mic-resume restore -- arms nothing either:
            # neither replaced a list the user had been reading.
            entry_pin = getattr(self, "_overlay_repaint_entry_pin", None)
            if entry_pin is not None and machine.pinned_snapshot_id != entry_pin:
                # wh-overlay-slow-uia-stale-badges.4.1.2 and .4.2.1: APPEND,
                # keeping every prior still inside its own grace window. Two
                # or more swaps can now land in one window, because every
                # repaint arms -- a dialog opens and its own focus change
                # fires a second refresh, a menu opens and closes. The list a
                # spoken number was read off is whichever one was on screen
                # when the user began speaking, so all of them are kept and
                # _overlay_renumber_click_safe checks badge N against each.
                # Keeping only one loses a real case either way: the newest
                # prior alone misses the user who began before the first swap
                # (.4.1.2), and the oldest alone misses the user who began
                # while an intermediate list was up, whose badge N can agree
                # between the first and last list while differing in the
                # middle (.4.2.1). Neither could happen before this bead: the
                # browser timer was the only arming trigger and its ticks are
                # _overlay_browser_refresh_seconds apart, far beyond the
                # grace window. The pruning bounds the list: an entry needs a
                # full walk and paint acknowledgement, so only a handful can
                # fit inside _OVERLAY_RENUMBER_GRACE_SECONDS.
                now = self._overlay_now_monotonic()
                armed = list(
                    getattr(self, "_overlay_repaint_swaps", None) or ()
                )
                self._overlay_repaint_swaps = [
                    entry for entry in armed
                    if now - entry[1] <= _OVERLAY_RENUMBER_GRACE_SECONDS
                ] + [(entry_pin, now)]
            # A restore leaves any armed guard ALONE rather than clearing it.
            # The badges it restored are the ones an earlier swap put on
            # screen, so the guard's recorded prior list is still the right
            # thing to compare a spoken number against, and clearing here
            # would drop that protection whenever a failed refresh landed
            # inside the grace window. The guard expires on its own
            # (_OVERLAY_RENUMBER_GRACE_SECONDS) and the session close resets
            # it.
            self._overlay_repaint_entry_pin = None
        # wh-overlay-fixqueue-review.1: attribute each refresh to its
        # trigger on the way IN, process the back-off on the way OUT.
        if (
            machine.state is OverlayState.REFRESH_IN_FLIGHT
            and prev_state is not OverlayState.REFRESH_IN_FLIGHT
        ):
            # The pin is still the prior snapshot here: the machine's
            # _refresh only bumps the generation; the pin changes later at
            # BUILD_RESPONSE. A supersede while already in flight keeps the
            # original attribution (no edge fires).
            self._overlay_refresh_started_proactive = bool(
                getattr(self, "_overlay_in_proactive_apply", False)
            )
            self._overlay_refresh_entry_pin = machine.pinned_snapshot_id
        # wh-overlay-slow-uia-stale-badges.4: record the list the user can
        # read RIGHT NOW at the start of every repaint that can replace it,
        # for the renumber guard armed on the PAINTED edge above. Shared with
        # handle_overlay_command, the other apply path (child .4.1.1).
        self._reconcile_overlay_repaint_entry_pin(prev_state)
        if (
            prev_state is OverlayState.REFRESH_IN_FLIGHT
            and machine.state is not OverlayState.REFRESH_IN_FLIGHT
        ):
            if getattr(self, "_overlay_refresh_started_proactive", False):
                if machine.state is OverlayState.PAINTED:
                    entry_pin = getattr(
                        self, "_overlay_refresh_entry_pin", None,
                    )
                    backoff = max(
                        int(getattr(
                            self, "_overlay_browser_refresh_backoff", 1,
                        )),
                        1,
                    )
                    if machine.pinned_snapshot_id != entry_pin:
                        # Genuine swap: fresh badges are on screen, so this
                        # window is worth re-walking at the normal cadence.
                        # (The renumber guard is armed by the PAINTED edge
                        # above, for this refresh and every other repaint --
                        # wh-overlay-slow-uia-stale-badges.4.)
                        self._overlay_browser_refresh_backoff = 1
                    else:
                        # Restore (failed build/paint/timeout): back off the
                        # next proactive attempt so a consistently failing
                        # walk is not re-run every window forever.
                        self._overlay_browser_refresh_backoff = min(
                            backoff * 2, 8,
                        )
                # Exits to CLOSED/PAUSED/ERROR change no back-off state;
                # the CLOSED edge above resets it for the next session.
            self._overlay_refresh_started_proactive = False
            self._overlay_refresh_entry_pin = None
        # wh-overlay-slow-uia-stale-badges.17: the paused -> painted restore
        # repaints the SAME pinned list at a NEW generation. The machine has to
        # bump there, because the mic-pause dispatched a clear at the old pair
        # and the GUI generation gate refuses a paint whose pair is <= the pair
        # of the last accepted clear. A deferred post-click refresh is keyed on
        # the pair it was captured at, and its documented rule -- keep it
        # waiting through PAUSED, consume it when that generation next reaches
        # PAINTED (wh-n29v.102.1 / .102.2) -- reads that key against the
        # machine's CURRENT pair. Re-key it onto the restored generation so the
        # resume still consumes it. Without this the reconciler below reads the
        # bump as a supersede and DROPS the refresh, which leaves the pre-click
        # list on screen with no fresh walk behind it. Deliberately narrow: it
        # fires only on the PAUSED -> PAINTED restore, so the stale-snapshot
        # resume (PAUSED -> WALK_IN_FLIGHT) still drops the pending, which is
        # correct -- that fresh walk supersedes the post-click refresh.
        if (
            prev_state is OverlayState.PAUSED
            and machine.state is OverlayState.PAINTED
            and getattr(self, "_overlay_pending_postclick_refresh", None)
            == prev_pair
        ):
            self._overlay_pending_postclick_refresh = (
                machine.overlay_session_id, machine.paint_generation,
            )
        self._reconcile_overlay_destroy_hook(
            machine.state is OverlayState.PAUSED
        )
        # wh-n29v.95 part 5: clear the tracked identity on entry to closed so
        # the destroy-while-paused hook and the resume identity check do not see
        # a stale identity from a torn-down session.
        self._reconcile_overlay_tracked_identity()
        # wh-overlay-slow-uia-stale-badges.8: record / keep / clear the pair of
        # the last-painted (visible) list that a resolved badge click carries
        # to Input.
        self._reconcile_overlay_visible_painted_pair()
        # wh-n29v.96.2: arm/cancel the periodic retained-snapshot keepalive to
        # match whether the resulting state still retains the pin (a
        # focus-change refresh, a paint-ack, a mic-pause, a successful click,
        # or a timeout can all move the machine in or out of that set here).
        self._reconcile_overlay_keepalive_timer()
        # wh-overlay-fixqueue-review.1/.2: reset the proactive-refresh back-off
        # and renumber guard when this event closed the session.
        self._reconcile_overlay_browser_refresh_reset()
        # wh-n29v.101.1: consume or clear a deferred post-click refresh now that
        # the machine state has settled (a paint-ack may have reached PAINTED, a
        # supersede may have bumped the generation, or this event may have closed
        # the session). Runs last so it observes the final post-apply state.
        self._reconcile_overlay_pending_postclick_refresh()
        # wh-overlay-slow-uia-stale-badges.15: its sibling, for the
        # deferred post-click CLEAR.
        self._reconcile_overlay_pending_postclick_clear()
        # wh-n29v.114.1: clear a stale auto-open item_id_filter stash on entry to
        # closed (shared with handle_overlay_command, which bypasses this method).
        self._reconcile_overlay_auto_open_filter()
        # wh-overlay-nested-dupes.1.4: a pending settle re-fire belongs to the
        # session that armed it; cancel it once the machine is closed (shared
        # with handle_overlay_command, which bypasses this method).
        self._reconcile_overlay_settle_refire()
        # wh-overlay-refresh-after-mouse: the hook thread's flag and the mouse
        # refresh timer follow the state (shared with handle_overlay_command).
        self._reconcile_overlay_pointer_refresh()
        # wh-overlay-slow-uia-stale-badges.21.8: retire an audited restore
        # paint's delivery entitlement (and its audit) once this event moved
        # the machine out of the PAINTED state that scheduled it.
        self._reconcile_overlay_restore_paint_entitlement()

    def _reconcile_overlay_settle_refire(self) -> None:
        """Cancel a pending settle re-fire when the machine is closed (wh-overlay-nested-dupes.1.4).

        A pending settle timer belongs to the overlay session that armed it.
        Both Logic-side paths that can drive the machine to CLOSED must cancel
        it: ``_apply_overlay_event`` (focus-hook events, paint-acks) and
        ``handle_overlay_command`` (the show/hide voice command, which applies
        HIDE_NUMBERS directly and bypasses ``_apply_overlay_event``). If the
        timer survived a hide, an immediate 'show numbers' would let the stale
        timer fire a FOCUS_CHANGE into the fresh session's in-flight build and
        restart it -- an unnecessary second walk and generation bump belonging
        to the previous session. Mirrors the sibling ``_reconcile_overlay_*``
        helpers: reads the machine's CURRENT state (not a prev/next edge), so
        it is idempotent and safe to call after every apply. Arming never
        happens inside an apply (only the hook callbacks and the timer body
        arm), so this can never cancel a timer armed by the same apply it
        follows.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is not None and machine.state is OverlayState.CLOSED:
            self._cancel_overlay_settle_refire()

    # -- wh-overlay-refresh-after-mouse: the mouse refresh -------------------

    def note_pointer_input(self, click: bool, injected: bool) -> None:
        """One mouse click or wheel step, from the pynput hook thread.

        MouseHandler calls this from ``on_click`` (press and release) and
        ``on_scroll``. It runs inside the WH_MOUSE_LL hook procedure, so the
        click or wheel step waits for it: it reads one bool and posts at most
        one call to the Logic loop, and it writes no log line. An exception
        (a closed loop at shutdown) goes back to MouseHandler, which drops
        it. Every decision is made on the Logic loop, in
        ``_on_overlay_pointer_input``.
        """
        if self._overlay_pointer_input_wanted:
            self._host.loop.call_soon_threadsafe(
                self._on_overlay_pointer_input, click, injected,
            )

    def _on_overlay_pointer_input(self, click: bool, injected: bool) -> None:
        """Logic loop: restart the quiet time for one click or wheel step.

        A click that a program sent (``injected``) while a WheelHouse number
        click is in flight, or within _OVERLAY_POINTER_SETTLE_S after it
        ended, is that number click's own click (a SendInput fallback), so it
        changes nothing: ``overlay_keep_numbers`` and
        ``overlay_settle_after_click`` already decide what follows a number
        click (RW-39). Wheel events and the user's own clicks always count:
        WheelHouse scroll commands send injected wheel events, and they
        refresh like the user's wheel (RW-37).
        """
        if not (
            self._host.click_config.enabled
            and self._host.click_config.overlay_enabled_effective
        ):
            return
        if click and injected and (
            getattr(self, "_overlay_click_in_flight", None) is not None
            or getattr(self, "_overlay_number_click_quiet_handle", None)
            is not None
        ):
            return
        self._arm_overlay_pointer_refresh()

    def _arm_overlay_pointer_refresh(self) -> None:
        """Cancel the pending quiet-time timer and arm it again.

        Arms only in the states where the numbers show or are being read
        (walk_in_flight, paint_in_flight, painted, refresh_in_flight); in
        every other state there is nothing to refresh (RW-38).
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None or machine.state not in (
            OverlayState.WALK_IN_FLIGHT, OverlayState.PAINT_IN_FLIGHT,
            OverlayState.PAINTED, OverlayState.REFRESH_IN_FLIGHT,
        ):
            return
        self._cancel_overlay_pointer_refresh()
        self._overlay_pointer_refresh_handle = self._host.loop.call_later(
            _OVERLAY_POINTER_SETTLE_S, self._on_overlay_pointer_settled,
        )

    def _cancel_overlay_pointer_refresh(self) -> None:
        """Cancel a pending quiet-time timer, if one is pending."""
        handle = getattr(self, "_overlay_pointer_refresh_handle", None)
        if handle is not None:
            handle.cancel()
            self._overlay_pointer_refresh_handle = None

    def _on_overlay_pointer_settled(self) -> None:
        """The quiet time passed with no click or wheel step: refresh once.

        Only while numbers show: painted, or refresh_in_flight when the
        refresh is not an automatic hide. During the first walk or paint the
        timer is armed again, so the refresh follows the paint. In every
        other state (closed, post_click_settling, paused, error) nothing
        happens (RW-38). The refresh is one FOCUS_CHANGE, the event the
        browser keepalive refresh feeds (``_maybe_overlay_browser_refresh``),
        so the machine's refresh and supersede rules apply unchanged. It does
        not set ``_overlay_in_proactive_apply``, so the browser back-off
        (RW-27) does not count it.
        """
        self._overlay_pointer_refresh_handle = None
        if not (
            self._host.click_config.enabled
            and self._host.click_config.overlay_enabled_effective
        ):
            return
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if machine.state in (
            OverlayState.WALK_IN_FLIGHT, OverlayState.PAINT_IN_FLIGHT,
        ):
            self._arm_overlay_pointer_refresh()
            return
        numbers_show = machine.state is OverlayState.PAINTED or (
            machine.state is OverlayState.REFRESH_IN_FLIGHT
            and not machine.auto_hide_in_flight
        )
        if not numbers_show:
            return
        logger.debug(
            "overlay: no mouse click or wheel step for %.0f ms; reading the "
            "numbers again",
            _OVERLAY_POINTER_SETTLE_S * 1000.0,
        )
        self._apply_overlay_event(
            OverlayEvent(kind=OverlayEventKind.FOCUS_CHANGE),
            source="mouse input settled",
        )

    def _overlay_number_click_ended(self) -> None:
        """A WheelHouse number click finished: its clicks are not user input.

        Called from the ``finally`` of ``_send_snapshot_item_click`` on every
        exit. Cancels a pending mouse refresh (the number click's own
        refresh, or its closing, follows from CLICK_COMPLETE) and opens a
        window of _OVERLAY_POINTER_SETTLE_S in which a program-sent click is
        ignored, because the hook can deliver the SendInput fallback's clicks
        after the Input reply has arrived (RW-39). Never raises.
        """
        try:
            self._cancel_overlay_pointer_refresh()
            quiet = getattr(self, "_overlay_number_click_quiet_handle", None)
            if quiet is not None:
                quiet.cancel()
            self._overlay_number_click_quiet_handle = self._host.loop.call_later(
                _OVERLAY_POINTER_SETTLE_S, self._end_overlay_number_click_quiet,
            )
        except Exception:  # noqa: BLE001 -- runs in a finally
            logger.debug(
                "overlay: number-click quiet time not armed", exc_info=True,
            )

    def _end_overlay_number_click_quiet(self) -> None:
        """The window after a number click ended; program clicks count again."""
        self._overlay_number_click_quiet_handle = None

    def _reconcile_overlay_pointer_refresh(self) -> None:
        """Keep the hook thread's flag and the mouse refresh timer in step.

        Runs after every apply on both apply paths (``_apply_overlay_event``
        and ``handle_overlay_command``), like the other ``_reconcile_overlay_*``
        helpers. The flag the hook thread reads is True only in the states
        where ``_arm_overlay_pointer_refresh`` arms, so a click while the
        overlay is closed posts nothing. A pending timer is cancelled once the
        machine is closed, so it cannot reach the next session.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        state = machine.state if machine is not None else OverlayState.CLOSED
        self._overlay_pointer_input_wanted = state in (
            OverlayState.WALK_IN_FLIGHT, OverlayState.PAINT_IN_FLIGHT,
            OverlayState.PAINTED, OverlayState.REFRESH_IN_FLIGHT,
        )
        if state is OverlayState.CLOSED:
            self._cancel_overlay_pointer_refresh()

    def _reconcile_overlay_auto_open_filter(self) -> None:
        """Clear the auto-open item_id_filter stash when the machine is closed (wh-n29v.114.1).

        The stash (``_overlay_auto_open_filter``) is set only while an auto-open
        is in flight (the machine left CLOSED via AUTO_OPEN, then
        ``_perform_auto_open_ambiguous`` recorded the finalist filter). When the
        machine is CLOSED there is no in-flight auto-open, so any remaining stash
        is stale and must be dropped before it can leak into a later session.
        Both Logic-side closed-entry paths call this: ``_apply_overlay_event``
        (focus-hook events, paint-acks) and ``handle_overlay_command`` (the
        show/hide voice command, which applies HIDE_NUMBERS directly and bypasses
        ``_apply_overlay_event``). Mirrors the other ``_reconcile_overlay_*``
        helpers -- reading the CURRENT state (not a prev/next edge) is idempotent
        and safe to call after every apply.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is not None and machine.state is OverlayState.CLOSED:
            self._overlay_auto_open_filter = None

    def _reconcile_overlay_tracked_identity(self) -> None:
        """Clear ``_overlay_tracked_identity`` when the machine is closed (wh-n29v.95).

        PART 5 / criterion 5: the tracked identity is assigned at the pin point
        (``_overlay_send_pin``) when the overlay becomes visible/paused and must
        be cleared to ``None`` on entry to closed, so the transient destroy hook
        does not register against a torn-down window and the resume identity
        check fails closed to re-walk. Reading the machine's CURRENT state (not a
        prev/next edge) is idempotent and safe to call after every apply.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if machine.state is OverlayState.CLOSED:
            self._overlay_tracked_identity = None
            # No snapshot is visible once closed; drop the whole per-snapshot
            # window-identity map so it cannot leak across overlay sessions
            # (trigger B bookkeeping).
            self._overlay_snapshot_window_identity.clear()
            # And the settle read's carried identity, which names a snapshot
            # of the session that just ended
            # (wh-overlay-slow-uia-stale-badges.2.2.1).
            self._overlay_settle_read_identity = None
            # And the window a build in flight was being made for: the session
            # it belonged to is over (wh-overlay-rewalk-after-filter.1.3).
            self._overlay_pending_build_identity = None

    def _reconcile_overlay_visible_painted_pair(self) -> None:
        """Track the pair of the list the user can SEE (wh-overlay-slow-uia-stale-badges.8).

        ``_overlay_visible_painted_pair`` is the (overlay_session_id,
        paint_generation) a resolved badge click carries to Input. It follows
        the LAST PAINT, not the machine's current pair:

        * PAINTED with the machine's pin MATCHING the memoized pin
          (wh-overlay-slow-uia-stale-badges.16.1) -- the SAME physical list
          is (still) on screen: a failed refresh fell back and restored the
          prior pin, or a paused-resume repainted the cached snapshot. KEEP
          the recorded pair. Recording the machine's pair here would carry
          the bumped generation for a list an executed click may already
          have consumed, beating Input's watermark and letting a click run
          against an already-changed screen. ``_refresh_build_ok`` pins the
          NEW snapshot id on a successful refresh build and the fall-back
          restores the prior id, so the pin id discriminates a fresh paint
          from a restored list.
        * PAINTED otherwise -- a genuinely fresh list; record the machine's
          pair and memoize the machine's pinned snapshot id with it.
        * REFRESH_IN_FLIGHT / POST_CLICK_SETTLING / PAUSED -- the machine's
          generation may already be bumped (refresh) or the badges are
          temporarily off screen, but no NEW list has been painted; KEEP the
          recorded pair (and its pin memo) so a click resolved against the
          still-visible prior list (the REFRESH_IN_FLIGHT routing case)
          carries the pair of what the user actually read. The memo's
          survival through PAUSED is what covers the auto-hide fall-back
          leg: ``_on_paused`` MIC_RESUME(snapshot_valid=True) goes DIRECTLY
          to PAINTED with the restored pin.
        * Every other state (closed / error / walk_in_flight /
          paint_in_flight) -- nothing the user read remains; clear both to
          None so a dispatch sends no pair and Input skips its stale check.

        Reads the machine's CURRENT state (not an edge), so it is idempotent
        and safe to call after every apply, mirroring the sibling
        ``_reconcile_overlay_*`` helpers.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if machine.state is OverlayState.PAINTED:
            recorded = getattr(self, "_overlay_visible_painted_pair", None)
            recorded_pin = getattr(self, "_overlay_visible_painted_pin", None)
            if (
                recorded is not None
                and machine.pinned_snapshot_id == recorded_pin
            ):
                # Same physical list as the recorded pair (fall-back restore
                # or paused-resume repaint): keep what the user actually read.
                return
            self._overlay_visible_painted_pair = (
                machine.overlay_session_id, machine.paint_generation,
            )
            self._overlay_visible_painted_pin = machine.pinned_snapshot_id
        elif machine.state in (
            OverlayState.REFRESH_IN_FLIGHT,
            OverlayState.POST_CLICK_SETTLING,
            OverlayState.PAUSED,
        ):
            pass  # the last-painted list is still what the user read
        else:
            self._overlay_visible_painted_pair = None
            self._overlay_visible_painted_pin = None

    def _reconcile_overlay_keepalive_timer(self) -> None:
        """Arm/cancel the periodic retained-snapshot keepalive (wh-n29v.96.2).

        FINDING 2 / criterion 4: the keepalive states are steady NO_TIMEOUT
        states with no recurring pin/paint, so the one-shot pin/paint keepalive
        cannot keep a quiescent overlay's summary alive. Arm a periodic timer
        whenever the machine holds a pinned snapshot in one of those states,
        and cancel it otherwise. ``_overlay_keepalive_states`` holds the list
        and states the contract: a RETAINED pin, which two of the three states
        hold with nothing on screen (wh-overlay-slow-uia-stale-badges.13.6).
        Reads the machine's CURRENT state (not an edge), so it is idempotent
        and safe to call after every apply. The timer re-puts the pinned
        summary and reschedules itself, so a snapshot retained past the TTL
        stays resolvable.
        """
        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        # wh-n29v.98.3: truthy guard, matching the sibling early return in
        # _overlay_keepalive_summary (`if not snapshot_id: return None`). An
        # empty-string pin is a schema-prevented invariant violation, but this
        # integration-layer guard should not rely on the build path's
        # validation alone -- a falsy pin must not arm a keepalive timer that
        # would fire every interval as a no-op.
        wants = (
            machine.state in _overlay_keepalive_states()
            and bool(machine.pinned_snapshot_id)
        )
        if wants:
            if getattr(self, "_overlay_keepalive_timer", None) is None:
                # wh-overlay-snapshot-keepalive (residual edge): the FIRST
                # tick fires immediately, not a full interval out. When a
                # FAILED refresh restores the prior snapshot, that snapshot's
                # ttl_anchor was last slid up to ~one interval before the
                # refresh began; a fresh full interval here could put the
                # next slide past the TTL, where refresh_snapshot_ttl fails
                # closed and a click on the still-visible restored overlay
                # misses. The immediate tick slides the restored snapshot
                # right away (a harmless extra re-put on a first paint) and
                # its body re-arms the periodic interval as usual.
                self._overlay_keepalive_timer = self._host.loop.call_soon(
                    self._fire_overlay_keepalive,
                )
        else:
            self._overlay_cancel_keepalive_timer()

    def _arm_overlay_keepalive_timer(self) -> None:
        """Schedule the next periodic keepalive re-put (wh-n29v.96.2)."""
        interval = getattr(self, "_overlay_keepalive_interval_s", 15.0)
        self._overlay_keepalive_timer = self._host.loop.call_later(
            interval, self._fire_overlay_keepalive,
        )

    def _overlay_cancel_keepalive_timer(self) -> None:
        """Cancel the periodic keepalive timer, if any (wh-n29v.96.2)."""
        existing = getattr(self, "_overlay_keepalive_timer", None)
        if existing is not None:
            existing.cancel()
        self._overlay_keepalive_timer = None

    def _fire_overlay_keepalive(self) -> None:
        """Keepalive-timer callback: re-put the pinned summary and reschedule.

        Re-puts the machine's currently-pinned (retained) snapshot summary to
        slide its TTL window, then reschedules itself while the machine is
        still in one of the keepalive states (see
        ``_overlay_keepalive_states`` -- the same list the reconciler reads,
        and the one place the retained-not-visible contract is stated).
        Best-effort: any error logs and does NOT escalate (this runs directly
        on the loop, not under a task done-callback). The timer is rescheduled
        ONLY while the pinned snapshot is still needed, so it cannot leak past
        teardown.
        """
        self._overlay_keepalive_timer = None
        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if machine.state not in _overlay_keepalive_states():
            return
        try:
            self._overlay_keepalive_summary(machine.pinned_snapshot_id)
        except Exception as exc:  # noqa: BLE001 -- never crash the loop
            logger.debug("overlay: periodic keepalive re-put failed: %s", exc)
        # Slide the Input-process store TTL too, not just the Logic cache: both
        # expire independently 30s after the walk, and "click N" dispatches
        # against the Input store. Fire-and-forget (Logic does not block on the
        # ack); the send swallows its own errors. wh-overlay-snapshot-keepalive.
        pinned = machine.pinned_snapshot_id
        if pinned:
            try:
                self._host.create_task_with_error_handling(
                    self._overlay_send_refresh(
                        pinned,
                        machine.overlay_session_id,
                        self._mint_overlay_trace_id(),
                    ),
                    "overlay_keepalive_refresh",
                )
            except Exception as exc:  # noqa: BLE001 -- never crash the loop
                logger.debug(
                    "overlay: periodic keepalive store-refresh schedule "
                    "failed: %s", exc,
                )
        # wh-overlay-slow-uia-stale-badges.9: renew the GUI badge lease
        # while badges are ON SCREEN -- PAINTED only, deliberately not
        # PAUSED / POST_CLICK_SETTLING (their entry cleared the badges, so
        # the GUI already cancelled its lease; renewing would re-arm a
        # lease over nothing). The grant covers several missed ticks so
        # scheduling jitter cannot expire a healthy overlay; if Logic
        # crashes or hangs, the renews stop and the GUI removes the badges
        # when the last grant runs out.
        from services.wheelhouse.click_overlay_state import OverlayState

        if machine.state is OverlayState.PAINTED:
            interval_s = getattr(self, "_overlay_keepalive_interval_s", 15.0)
            lease_ms = max(
                _OVERLAY_LEASE_FLOOR_MS,
                int(interval_s * 1000) * _OVERLAY_LEASE_INTERVAL_TICKS,
            )
            # wh-overlay-slow-uia-stale-badges.18.2: the GUI accepts a
            # renew only at the EXACT pair its lease was armed with -- the
            # pair of the LAST PRESENTED paint. After a failed refresh the
            # machine falls back to PAINTED with a bumped generation while
            # the old badges stay visible, so the machine pair no longer
            # matches the armed lease. Renew at the recorded visible pair
            # (kept by _reconcile_overlay_visible_painted_pair); fall back
            # to the machine pair when nothing is recorded -- the
            # fresh-paint paths keep the two equal.
            visible = getattr(self, "_overlay_visible_painted_pair", None)
            renew_sid, renew_gen = (
                visible if visible is not None
                else (machine.overlay_session_id, machine.paint_generation)
            )
            self._put_overlay_gui_action(
                {
                    "action": "overlay_lease_renew",
                    "overlay_session_id": renew_sid,
                    "paint_generation": renew_gen,
                    "lease_ms": lease_ms,
                },
                trace_id="overlay-lease-renew",
            )
        # Reschedule for the next interval while the pin is still retained.
        self._arm_overlay_keepalive_timer()
        # wh-n29v.121: LAST, so the re-arm above is already in place -- if this
        # feeds a refresh, the apply's keepalive reconciler cancels the timer
        # (state leaves PAINTED) and re-arms it when the refresh completes.
        self._maybe_overlay_browser_refresh(machine)

    def _overlay_now_monotonic(self) -> float:
        """The overlay clock, in seconds (the clock given to __init__)."""
        return self._clock()

    def _maybe_overlay_browser_refresh(self, machine) -> None:
        """Feed one refresh when a painted overlay over a browser goes stale.

        wh-n29v.121: dynamic Chromium/Brave pages shift layout while the user
        idles, so the first "click N" after an in-page shift is refused once
        before the reactive re-walk repairs the badges. Called from each
        keepalive tick; when the machine is PAINTED (deliberately NOT PAUSED --
        a paused overlay is invisible), the tracked window's process is in the
        effective browser-process set, and the latest PAINTED entry is older
        than ``overlay_browser_refresh_seconds``, feed one FOCUS_CHANGE -- the
        same event the focus/menu hooks reuse, which maps to a REFRESH in
        PAINTED -- so generation discipline, pin handoff, and repaint all run
        through the machine normally. A "click N" racing the refresh is HELD
        by the in-flight routing rule, and stale acks are already rejected.
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )

        window_s = getattr(self, "_overlay_browser_refresh_seconds", 0.0)
        if window_s <= 0:
            return
        if machine.state is not OverlayState.PAINTED:
            return
        tracked = getattr(self, "_overlay_tracked_identity", None)
        if tracked is None or not tracked.process_name:
            return
        browsers = getattr(self, "_overlay_browser_process_set", frozenset())
        if tracked.process_name.lower() not in browsers:
            return
        last_paint = getattr(self, "_overlay_last_paint_monotonic", None)
        if last_paint is None:
            return
        # wh-overlay-fixqueue-review.1: the back-off multiplier stretches the
        # trust window after failed proactive refreshes (a failed refresh
        # restores the prior snapshot by re-entering PAINTED, which re-stamps
        # last_paint -- so the age below measures from the failed attempt).
        backoff = max(
            int(getattr(self, "_overlay_browser_refresh_backoff", 1)), 1,
        )
        if self._overlay_now_monotonic() - last_paint < window_s * backoff:
            return
        # Mark the apply so the REFRESH_IN_FLIGHT entry edge in
        # _apply_overlay_event attributes this refresh to the proactive
        # trigger (back-off and renumber-guard bookkeeping key off it).
        self._overlay_in_proactive_apply = True
        try:
            self._apply_overlay_event(
                OverlayEvent(kind=OverlayEventKind.FOCUS_CHANGE),
                source="browser proactive refresh (keepalive tick)",
            )
        finally:
            self._overlay_in_proactive_apply = False

    def _reconcile_overlay_browser_refresh_reset(self) -> None:
        """Reset the proactive-refresh back-off and renumber guard while CLOSED.

        wh-overlay-fixqueue-review.1/.2: a fresh overlay session starts with
        a fresh proactive-refresh budget and no renumber guard. Level-triggered
        and idempotent (the reconciler idiom): it reads the CURRENT machine
        state, so it is correct from BOTH apply paths -- hide_numbers goes
        through handle_overlay_command, which bypasses _apply_overlay_event.

        wh-overlay-slow-uia-stale-badges.4: the repaint entry pin is cleared
        here too. A repaint that ends in CLOSED rather than PAINTED -- a
        settle re-read the window closed under, a refresh timeout, "hide
        numbers" said mid-refresh -- leaves the pin recorded and never
        consumed, and the FIRST paint of the next session would then compare
        against a list from the closed session and arm the guard over badges
        the user cannot have read.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None or machine.state is OverlayState.CLOSED:
            self._overlay_browser_refresh_backoff = 1
            self._overlay_repaint_swaps = []
            self._overlay_repaint_entry_pin = None

    def _reconcile_overlay_repaint_entry_pin(self, prev_state) -> None:
        """Record the visible list at the start of a repaint that can swap it.

        wh-overlay-slow-uia-stale-badges.4: the renumber guard armed on the
        PAINTED edge of ``_apply_overlay_event`` compares the newly painted
        pin against the pin recorded here, and that comparison is what tells
        a swap from a restore. Two states begin such a repaint:
        REFRESH_IN_FLIGHT (a focus change, a menu opening or closing, a
        re-said "show numbers", and the browser timer's proactive tick, which
        all keep the old badges up while the new list is built) and
        POST_CLICK_SETTLING (the post-click re-read, whose badges are already
        cleared but whose pin is still the list the user clicked in).

        wh-overlay-slow-uia-stale-badges.4.1.1: this lives in a helper
        because the overlay has TWO apply paths and both can start a repaint.
        ``handle_overlay_command`` applies SHOW_NUMBERS to the machine itself
        and never calls ``_apply_overlay_event``, so a re-said "show numbers"
        over painted badges (PAINTED -> ``_refresh`` -> REFRESH_IN_FLIGHT)
        recorded nothing and its swap armed nothing -- the one member of the
        class that the original edge-only version missed. Same shape as
        ``_reconcile_overlay_tracked_identity`` and the other reconcilers
        that path already calls for the same reason.

        The pin read here is the PRIOR snapshot: the machine's ``_refresh``
        only bumps the generation, and the pin changes later at
        BUILD_RESPONSE. A supersede while already in flight is a
        self-transition, so no edge fires and the original entry pin is kept.

        Kept separate from ``_overlay_refresh_entry_pin`` on purpose, even
        though the two hold the same value during a refresh: that one is
        proactive-refresh back-off bookkeeping and is cleared on the way out
        of REFRESH_IN_FLIGHT, while this one has to survive
        POST_CLICK_SETTLING -> PAINT_IN_FLIGHT -> PAINTED and is cleared when
        the paint lands or the session closes.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if (
            machine.state
            in (OverlayState.REFRESH_IN_FLIGHT, OverlayState.POST_CLICK_SETTLING)
            and prev_state is not machine.state
        ):
            self._overlay_repaint_entry_pin = machine.pinned_snapshot_id

    def _overlay_renumber_click_safe(
        self, parsed_number, current_snapshot_id,
    ) -> bool:
        """Check a "click N" against the renumber guard; True means proceed.

        wh-overlay-fixqueue-review.2: for ``_OVERLAY_RENUMBER_GRACE_SECONDS``
        after a repaint swapped the badge list, a "click N" may have been
        spoken against the PRE-swap badges. Resolving it against the
        renumbered overlay would click the wrong control with fresh bounds --
        the stale-position refusal that protected this case before the overlay
        repainted itself never fires. Compare badge N's identity across the
        swap (the prior summary is still in the Logic cache); block only when
        it changed. One block per swap: the notice tells the user to
        re-check, so their next utterance is informed and the guard is
        consumed either way once it decides or expires.

        wh-overlay-slow-uia-stale-badges.4: the arming (the PAINTED edge in
        ``_apply_overlay_event``) covers every repaint that replaces the
        pinned list, on every application. It was the browser timer's
        proactive refresh alone until then, so this check had nothing to
        compare after a focus-triggered refresh or a post-click settle
        re-read and those numbers resolved against a list the user never saw.

        wh-overlay-slow-uia-stale-badges.4.2.1: every prior still inside ITS
        OWN grace window is checked, not just one of them. Widening the
        arming made chains of repaints inside a single window ordinary -- a
        menu opens and closes again, a dialog appears and its own focus
        change fires a second refresh -- and the user read exactly one list
        of that chain. A single slot has to guess which, and both guesses
        lose a real case: keeping the newest prior misses the user who began
        speaking before the first swap, and keeping the oldest misses the
        user who began speaking while an intermediate list was up (badge N
        can agree between the first and last list while differing in the
        middle, as when a menu opens and closes). Each entry also expires on
        its own timestamp: measuring the whole chain from the oldest entry
        would drop the protection for the swap that just happened.
        """
        swaps = list(getattr(self, "_overlay_repaint_swaps", None) or ())
        now = self._overlay_now_monotonic()
        live = [
            entry for entry in swaps
            if now - entry[1] <= _OVERLAY_RENUMBER_GRACE_SECONDS
        ]
        if len(live) != len(swaps):
            self._overlay_repaint_swaps = live
        if not live:
            return True
        from services.wheelhouse.speech.overlay_click_router import (
            renumber_click_is_safe,
        )

        cache = self.click_snapshot_summary_cache
        current = (
            cache.resolve(current_snapshot_id).summary
            if current_snapshot_id else None
        )
        for prior_id, _swap_t in live:
            prior = cache.resolve(prior_id).summary if prior_id else None
            if not renumber_click_is_safe(prior, current, parsed_number):
                # One block per chain, matching the single-swap behaviour
                # above it: the notice tells the user to re-check, so their
                # next utterance is informed and nothing in the chain should
                # refuse it a second time.
                self._overlay_repaint_swaps = []
                return False
        return True

    def _reconcile_overlay_destroy_hook(self, should_be_active: bool) -> None:
        """Register/unregister the transient destroy hook to match paused-ness.

        The destroy hook is registered only while the overlay is ``paused`` and
        is scoped to the tracked window's pid/tid (so the OS does not wake the
        Logic loop for every foreign-process child destroy). Idempotent: it
        only acts on a state edge, so repeated calls in the same paused-ness do
        nothing. Skips registration when no tracked identity is known (no
        pid/tid to scope the hook).
        """
        manager = self._overlay_focus_hooks
        if manager is None:
            return
        if should_be_active and not self._overlay_destroy_hook_active:
            tracked = self._overlay_tracked_identity
            if tracked is None or tracked.hwnd <= 0:
                logger.debug(
                    "overlay: destroy hook not registered (no tracked window "
                    "identity to scope it).",
                )
                return
            pid, tid = self._overlay_window_pid_tid(tracked.hwnd)
            if pid <= 0 or tid <= 0:
                logger.debug(
                    "overlay: destroy hook not registered (could not resolve "
                    "pid/tid for tracked hwnd=%s).", tracked.hwnd,
                )
                return
            # wh-n29v.23.3: only record the hook as active when the register
            # request was ACCEPTED (posted to the hook thread). If the post was
            # rejected (manager not alive, window gone, PostMessage failed), the
            # flag stays False so the next paused reconcile retries instead of
            # believing a dead hook is live.
            if manager.register_destroy_hook(pid=pid, tid=tid):
                self._overlay_destroy_hook_active = True
                logger.info(
                    "overlay: transient destroy hook register requested for "
                    "tracked hwnd=%s (pid=%s tid=%s).", tracked.hwnd, pid, tid,
                )
            else:
                logger.debug(
                    "overlay: destroy hook register request not accepted; will "
                    "retry on the next paused reconcile.",
                )
        elif not should_be_active and self._overlay_destroy_hook_active:
            manager.unregister_destroy_hook()
            self._overlay_destroy_hook_active = False
            logger.info("overlay: transient destroy hook unregistered.")

    def overlay_snapshot_is_valid_on_resume(self) -> bool:
        """Resume-time FULL foreground-identity check (r2.8).

        Returns True iff a tracked identity exists AND the CURRENT foreground
        identity matches it on ALL of HWND + PID + process name + window
        creation time -- the same full-identity rule
        ``ElementFinder.get_snapshot`` enforces, NOT ``IsWindow`` alone. This
        is the value the mic-resume wiring feeds as ``OverlayEvent.snapshot_valid``
        to drive the ``paused`` machine to restore (match) vs re-walk
        (mismatch). It catches the HWND-reuse trap (a recycled HWND on a
        different process) that ``IsWindow`` would miss. With no tracked
        identity there is nothing to restore, so it returns False (re-walk).
        """
        from services.wheelhouse.overlay_focus_hooks import identity_matches

        tracked = self._overlay_tracked_identity
        if tracked is None:
            return False
        current = self._capture_overlay_foreground_identity()
        if current is None:
            return False
        return identity_matches(tracked, current)

    def _overlay_settle_read_is_current(self, reported, *, trace_id: str) -> bool:
        """Whether a settle reply's window is STILL the foreground (r2.8).

        ``reported`` is the ``ForegroundIdentity`` the walk reply says it was
        read from (``None`` when the reply carried no window, which every
        pre-fix Input build and every malformed-but-parsed payload does).
        Returns True only when a window was reported, the current foreground
        can be sampled, and ALL FOUR identity fields match it.

        The comparison is ``overlay_focus_hooks.identity_matches``, the same
        full-identity rule ``ElementFinder.get_snapshot`` and the resume check
        use, NOT an ``IsWindow``-style HWND test: Windows reuses HWND values,
        so a recycled handle landing on a different process would pass an
        HWND-only check while naming a completely different window.

        Fails closed on both unknowns -- no reported window, or no sampleable
        foreground -- because the only cost of a false negative here is that
        the user says "show numbers" again, while a false positive paints one
        window's numbers over another.
        """
        from services.wheelhouse.overlay_focus_hooks import identity_matches

        if reported is None:
            logger.info(
                "overlay: the settle reply named no window it was read from; "
                "refusing to paint it (trace_id=%s)", trace_id,
            )
            return False
        current = self._capture_overlay_foreground_identity()
        if current is None:
            logger.info(
                "overlay: the current foreground could not be sampled to "
                "check the settle reply; refusing to paint it (trace_id=%s)",
                trace_id,
            )
            return False
        if identity_matches(reported, current):
            return True
        logger.info(
            "overlay: the settle reply was read from hwnd=%s pid=%s (%s) but "
            "the foreground is now hwnd=%s pid=%s (%s); refusing to paint it "
            "(trace_id=%s)",
            reported.hwnd, reported.pid, reported.process_name,
            current.hwnd, current.pid, current.process_name, trace_id,
        )
        return False

    def _overlay_refresh_visible_window_is_foreground(self, machine):
        """Whether a REFRESH_IN_FLIGHT overlay's VISIBLE snapshot is foreground.

        Trigger B (wh-overlay-snapshot-keepalive). Returns:
          * ``True``  -- the visible snapshot's window IS the current foreground
            (a same-window content refresh) -> route normally.
          * ``False`` -- the visible snapshot belongs to a window that is no
            longer foreground (a focus-change refresh) -> the caller passes this
            to ``route_click_n`` so a "click N" HOLDS instead of dispatching a
            click Input would reject on a foreground-identity mismatch.
          * ``None``  -- undeterminable (no recorded identity for the visible
            snapshot, or the current foreground cannot be sampled) -> the router
            keeps its resolve-against-visible default.

        The VISIBLE snapshot is the prior pin when a refresh build already pinned
        a new not-yet-painted snapshot (deferred unpin), else the current pin --
        the same rule ``route_click_n`` uses, so they agree on which snapshot the
        relationship is computed for.
        """
        from services.wheelhouse.overlay_focus_hooks import identity_matches

        visible_id = (
            machine.prior_pinned_snapshot_id if machine.prior_pin_deferred
            else machine.pinned_snapshot_id
        )
        if not visible_id:
            return None
        recorded = self._overlay_snapshot_window_identity.get(visible_id)
        if recorded is None:
            return None
        current = self._capture_overlay_foreground_identity()
        if current is None:
            return None
        return identity_matches(recorded, current)

    def _overlay_mark_visible_window_left(self, machine, event):
        """Mark a failed refresh whose visible numbers belong to a left window.

        wh-overlay-failed-focus-read-keeps-numbers. A failed build response or
        a timeout in REFRESH_IN_FLIGHT makes the machine fall back to the
        numbers already on screen. After a focus change those numbers belong
        to a window that is no longer in front and may be closed: on
        2026-09-24 at 18:10:29 a desktop read got no reply, and Brave's
        numbers stayed on the desktop until "hide numbers". This returns the
        event with ``visible_window_left=True`` when
        ``_overlay_refresh_visible_window_is_foreground`` answers False, so
        the machine clears and closes instead. A same-window refresh
        (keepalive, browser refresh, menu, Pattern Manager tree change)
        answers True and keeps the fall-back. An undeterminable answer (None)
        also keeps the fall-back, which is the behaviour before this bead.

        The answer is taken when the failure is applied, not when the read
        started. A later focus change supersedes the refresh and makes the
        earlier failure stale, so the foreground at the failure is the window
        the failed read was for.
        """
        import dataclasses

        from services.wheelhouse.click_overlay_state import (
            OverlayEventKind,
            OverlayState,
        )

        if machine.state is not OverlayState.REFRESH_IN_FLIGHT:
            return event
        failed = event.kind is OverlayEventKind.TIMEOUT or (
            event.kind is OverlayEventKind.BUILD_RESPONSE and not event.build_ok
        )
        if not failed:
            return event
        if (event.overlay_session_id, event.paint_generation) != (
            machine.overlay_session_id, machine.paint_generation
        ):
            # A stale failure: the machine's generation gate rejects it.
            return event
        if self._overlay_refresh_visible_window_is_foreground(machine) is not False:
            return event
        logger.info(
            "overlay: refresh %s at gen=(%s,%s) while the visible numbers "
            "belong to a window no longer in front; clearing them",
            event.kind.value, event.overlay_session_id, event.paint_generation,
        )
        return dataclasses.replace(event, visible_window_left=True)

    def _mint_overlay_trace_id(self) -> str:
        """Mint a click-scoped trace id (mirrors the click_element id shape)."""
        import uuid

        return f"click-{uuid.uuid4().hex[:12]}"

    def _capture_overlay_foreground_identity(self):
        """Sample the CURRENT foreground identity (HWND+PID+name+creation time).

        Thin Win32 seam (lazy win32 imports). Returns a ``ForegroundIdentity``,
        or ``None`` when the foreground window cannot be resolved (so the
        resume check fails closed to re-walk). Read failures degrade to safe
        sentinels per field, mirroring the Input-side ``_capture_click_foreground``
        rule (a real window is required before trusting the pid, so
        ``GetWindowThreadProcessId(0)`` cannot misreport WheelHouse's own pid).
        """
        from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity

        try:
            import win32gui
            import win32process
        except Exception:  # noqa: BLE001 -- non-Windows / missing pywin32
            return None
        hwnd = 0
        pid = 0
        process_name = ""
        creation_time = 0
        try:
            hwnd = int(win32gui.GetForegroundWindow())
        except Exception:  # noqa: BLE001
            hwnd = 0
        if not hwnd:
            return None
        try:
            _thread, raw_pid = win32process.GetWindowThreadProcessId(hwnd)
            pid = int(raw_pid) if raw_pid else 0
        except Exception:  # noqa: BLE001
            pid = 0
        if pid:
            try:
                import psutil

                proc = psutil.Process(pid)
                process_name = proc.name()
                creation_time = int(proc.create_time() * 1000)
            except Exception:  # noqa: BLE001
                process_name = ""
                creation_time = 0
        return ForegroundIdentity(
            hwnd=hwnd,
            pid=pid,
            process_name=process_name,
            window_creation_time=creation_time,
        )

    def _overlay_window_pid_tid(self, hwnd: int) -> tuple[int, int]:
        """Resolve (pid, tid) for ``hwnd`` to scope the transient destroy hook.

        Returns ``(0, 0)`` on any failure so the caller skips registration
        rather than registering a system-wide destroy hook.
        """
        try:
            import win32process

            tid, pid = win32process.GetWindowThreadProcessId(int(hwnd))
            return (int(pid) if pid else 0, int(tid) if tid else 0)
        except Exception:  # noqa: BLE001
            return (0, 0)

    async def handle_snapshot_item_clicked(self, command) -> None:
        """Resolve a Phase 1.5 numbered-overlay click (wh-jfavj / wh-g4oma).

        The GUI emits ``snapshot_item_clicked { snapshot_id, display_number }``
        on the commands_to_logic_queue when the user clicks a numbered overlay
        item. This handler:

          1. Validates the payload via :func:`safe_parse`. A malformed payload
             (``SnapshotItemClickedSchemaError`` -> ``ValueError``) is logged
             and dropped, per wh-uf54, so a version-skewed sender cannot crash
             the GUI command listener.
          2. Generates a click-scoped ``trace_id`` (mirrors
             ``ActionFunctions.click_element`` / ``forward_click_element``) so
             the resolve + notice log lines correlate.
          3. Resolves (snapshot_id, display_number) via
             ``resolve_display_number`` against the retained
             ``click_snapshot_summary_cache`` and maps the outcome:

               * ``SNAPSHOT_EXPIRED`` -> an
                 ``execution_failed:snapshot_expired`` notice ("the numbered
                 overlay has expired -- say the click command again"; wording
                 owned by wh-g4oma, which needs no spoken_name for this tag).
               * ``NOT_FOUND`` -> COLLAPSED into the identical snapshot_expired
                 notice. The resolver's docstring permits the caller to
                 collapse NOT_FOUND into the snapshot_expired surface: a
                 NOT_FOUND means the user spoke a number not in the live
                 overlay (stale / out-of-range), whose user remedy is identical
                 to expiry (say the command again for fresh numbers), and no
                 separate wording exists -- so reusing snapshot_expired is
                 correct and avoids a silent no-feedback path.
               * ``FOUND`` -> the handler sends the click: it calls
                 ``_dispatch_snapshot_item_click`` with the resolved item_id
                 (wh-n29v.95), which sends ``click_snapshot_item`` to the Input
                 process. This handler forwards no notice on FOUND itself.
                 ``_dispatch_snapshot_item_click`` forwards an
                 ``execution_failed`` notice and sends nothing in three
                 cases: a missing snapshot_id or item_id
                 (``invalid_snapshot_item``), a grid open or next screen in
                 progress (``grid_transition_pending``), and a previous
                 badge click still in flight (``overlay_click_in_flight``).
                 Otherwise the send path forwards a notice when the click
                 fails.
        """
        import uuid

        # Import via the full services.wheelhouse.* package path -- the same
        # path main.py's module-level imports and the cache init in __init__
        # (self.click_snapshot_summary_cache) use. resolve_display_number
        # compares the cache's CacheStatus.HIT by identity (`is`), and Python
        # loads this file as a DISTINCT module object under the bare name
        # `click_snapshot_summary_cache` versus the package name. A bare import
        # here would give a different CacheStatus enum object than the
        # package-path cache instance carries, so a real cache HIT would fail
        # the identity check and misresolve as snapshot_expired (wh-9f3t.70.1).
        from services.wheelhouse.shared.ipc_schema_validation import safe_parse
        from services.wheelhouse.shared.snapshot_item_clicked import (
            SnapshotItemClickedEvent,
        )
        from services.wheelhouse.click_snapshot_summary_cache import (
            ResolveOutcome,
            resolve_display_number,
        )

        ev = safe_parse(
            SnapshotItemClickedEvent.from_dict,
            command,
            log_label="snapshot_item_clicked",
        )
        if ev is None:
            return  # already logged

        # Mint a click-scoped trace_id (the GUI->Logic event carries none),
        # mirroring ActionFunctions.click_element's id shape so the resolve
        # and notice log lines correlate with the rest of the click flow.
        trace_id = f"click-{uuid.uuid4().hex[:12]}"

        # Degrade, do not die. This handler runs as a
        # create_task_with_error_handling background task whose done-callback
        # calls request_shutdown() on any uncaught exception. An advisory
        # click notice must never be able to restart the whole Logic process,
        # so wrap the resolve + forward in the same degrade-don't-die posture
        # forward_click_element uses for its notice surface (wh-9f3t.69.3): an
        # unexpected error here logs and drops the click silently rather than
        # escalating to a process-wide shutdown. safe_parse above already
        # handles the malformed-payload (ValueError) path.
        try:
            result = resolve_display_number(
                self.click_snapshot_summary_cache,
                ev.snapshot_id,
                ev.display_number,
            )

            if result.outcome is ResolveOutcome.FOUND:
                # wh-n29v.95: dispatch the real overlay-item click. The resolver
                # mapped the display number to an item_id in the live retained
                # snapshot; forward it to the Input process via
                # ``click_snapshot_item`` (the GUI-to-Logic round-trip step 6
                # dispatch). The send + ClickElementResponse parse + non-ok
                # click-notice are owned by ``_send_snapshot_item_click`` (the
                # SAME path the voice ``click N`` route uses). No notice on ok.
                logger.info(
                    "snapshot_item_clicked: resolved display_number=%d to "
                    "item_id=%r in snapshot=%r; dispatching click_snapshot_item "
                    "(trace_id=%s)",
                    ev.display_number, result.item_id, ev.snapshot_id,
                    trace_id,
                )
                self._dispatch_snapshot_item_click(
                    snapshot_id=ev.snapshot_id,
                    item_id=result.item_id,
                    trace_id=trace_id,
                )
                return

            # SNAPSHOT_EXPIRED and NOT_FOUND both surface as the shipped
            # snapshot_expired notice. The NOT_FOUND collapse is intentional
            # (see the docstring): same user remedy, no distinct wording.
            if result.outcome is ResolveOutcome.NOT_FOUND:
                logger.info(
                    "snapshot_item_clicked: display_number=%d not in live "
                    "snapshot=%r; collapsing NOT_FOUND into the "
                    "snapshot_expired notice (trace_id=%s)",
                    ev.display_number, ev.snapshot_id, trace_id,
                )
            else:
                logger.info(
                    "snapshot_item_clicked: snapshot=%r expired/missing; "
                    "emitting snapshot_expired notice (trace_id=%s)",
                    ev.snapshot_id, trace_id,
                )

            self.forward_click_notice(
                outcome="execution_failed",
                reason="snapshot_expired",
                matched_name=None,
                matched_names=(),
                # The event carries only snapshot_id + display_number; the
                # snapshot_expired wording does not embed spoken_name.
                spoken_name="",
                snapshot_id=ev.snapshot_id,
                trace_id=trace_id,
            )
        except Exception:
            # Drop the click silently rather than restart the Logic process.
            logger.exception(
                "snapshot_item_clicked: unexpected error resolving / "
                "forwarding the notice for snapshot=%r display_number=%d; "
                "dropping the click (trace_id=%s)",
                ev.snapshot_id, ev.display_number, trace_id,
            )

    def forward_click_notice(
        self,
        *,
        outcome: str,
        reason: Optional[str],
        matched_name: Optional[str],
        matched_names: tuple,
        spoken_name: str,
        snapshot_id: Optional[str],
        trace_id: str,
        late_correction: bool = False,
    ) -> None:
        """Forward a ClickNoticeEvent to the GUI for a non-ok click outcome.

        Builds the wh-lstwt ClickNoticeEvent payload and puts a
        ``show_click_notice`` action on the GUI state queue. The notice
        WORDING (wh-g4oma) and the GUI render path consume this; this
        method only populates the schema-valid payload, including
        trace_id. app_friendly_name is left empty here (the
        ClickElementResponse does not carry the process identity); the
        wording slice resolves any display name it needs.

        After a successful put this records the notice's trace_id in
        ``self._last_click_notice_trace_id``
        (wh-overlay-slow-uia-stale-badges.20.4): every click notice goes
        through this method, so the late-answer correction can tell whether
        the toast still shows the timed-out click's message. At method
        ENTRY -- before any early return -- it also records the trace_id in
        ``self._last_click_notice_attempt_trace_id``
        (wh-overlay-slow-uia-stale-badges.20.10), so the correction can
        tell "a newer notice displaced A's" from "A's own notice never
        left Logic" (e.g. the GUI queue was full).

        ``late_correction`` (wh-overlay-slow-uia-stale-badges.20.11) marks
        the one caller that is NOT a primary notice: the late-answer
        correction in ``_handle_late_click_response``. A correction belongs
        to a click that already spoke, so it must never claim the
        newest-ATTEMPT marker -- claiming it would move the marker backwards
        and suppress the true answer of a newer click whose own notice put
        failed. A correction still records the SUCCESSFUL-put marker,
        because the corrected wording is what the toast now shows. This
        method is the only writer of either marker.
        """
        from shared.click_notice import ClickNoticeEvent

        # Record EVERY primary attempt first (wh-overlay-slow-uia-stale-
        # badges.20.10): a failed put must still leave a trace, or the
        # late-answer gate would wrongly suppress the only feedback for this
        # click. A late correction is excluded (.20.11): it is not a newer
        # click, so it must not overwrite a newer click's failed attempt.
        if not late_correction:
            self._last_click_notice_attempt_trace_id = trace_id

        try:
            event = ClickNoticeEvent(
                outcome=outcome,
                reason=reason,
                matched_name=matched_name,
                matched_names=tuple(matched_names),
                spoken_name=spoken_name,
                app_friendly_name="",
                snapshot_id=snapshot_id,
                trace_id=trace_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "click_element: failed to build ClickNoticeEvent "
                "(trace_id=%s): %s", trace_id, exc,
            )
            return

        if not self._host.state_manager or not hasattr(
            self._host.state_manager, "state_to_gui_queue",
        ):
            logger.warning(
                "click_element notice dropped, no state_manager queue "
                "(trace_id=%s)", trace_id,
            )
            return

        gui_msg = {"action": "show_click_notice", **event.to_dict()}
        try:
            self._host.state_manager.state_to_gui_queue.put_nowait(gui_msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "click_element notice dropped, GUI queue put failed "
                "(trace_id=%s): %s", trace_id, exc,
            )
            return
        # Record ONLY after a successful put: a dropped notice never reached
        # the GUI, so it must not gate the late-answer correction
        # (wh-overlay-slow-uia-stale-badges.20.4).
        self._last_click_notice_trace_id = trace_id
        # wh-n29v.122: the success path used to write ZERO log lines (both
        # logs above are failure-only), so a live session could not tell
        # "notice never sent" from "sent and missed while the toast
        # auto-dismissed". One INFO line after the successful put; the GUI
        # logs its own line after rendering, sharing this trace_id.
        logger.info(
            "click notice forwarded to GUI: outcome=%s reason=%s matched=%r "
            "(trace_id=%s)", outcome, reason, matched_name, trace_id,
        )

    def _resolve_first_use_hint_path(self) -> "Path":
        """Return the path to the first-use-hint record file (wh-r3xy1).

        Tests override the location by setting ``self._first_use_hint_path``
        on the controller before the first click. Production resolves to the
        same default the writer uses.
        """
        override = getattr(self, "_first_use_hint_path", None)
        if override is not None:
            return override
        from services.wheelhouse.click_first_use_hint import default_hint_path

        return default_hint_path()

    def _first_use_hint_tracker(self) -> "FirstUseHintTracker | None":
        """Lazily build (and memoise) the first-use hint tracker (wh-r3xy1).

        Built on first use so a session that never issues a click -- or a test
        that does not exercise the hint -- never reads the record file. Returns
        None if construction fails for any reason; a hint problem must never
        break the click flow.
        """
        if self._first_use_hint is not None:
            return self._first_use_hint
        try:
            from services.wheelhouse.click_first_use_hint import (
                FirstUseHintTracker,
            )

            # Match the predicate's resolved browser-process view: the
            # ClickConfig starter list plus any user extension. is_chromium_
            # family lower-cases internally so casing in config is fine.
            browser_names = (
                tuple(self._host.click_config.browser_processes)
                + tuple(self._host.click_config.browser_processes_extend)
            )
            self._first_use_hint = FirstUseHintTracker(
                self._resolve_first_use_hint_path(),
                browser_process_names=browser_names,
            )
        except Exception as exc:  # noqa: BLE001 -- never break the click flow
            logger.warning(
                "click_first_use_hint: failed to build tracker: %s", exc,
            )
            return None
        return self._first_use_hint

    def _resolve_foreground_process_name(self) -> str:
        """Best-effort exe basename of the current foreground window (wh-r3xy1).

        The ``click_element`` round trip does not carry the target process
        name back to Logic, and the spoken ``ElementQuery`` does not carry it
        either, so the hint resolves the foreground process directly. This is
        a read-only observation; it does not touch the click execution path.
        Returns ``""`` on any failure (no foreground, access denied, missing
        Win32 dependency) so the caller simply skips the hint.
        """
        try:
            import psutil
            import win32gui
            import win32process

            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return ""
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if not pid:
                return ""
            return psutil.Process(pid).name()
        except Exception as exc:  # noqa: BLE001 -- never break the click flow
            logger.debug(
                "click_first_use_hint: could not resolve foreground "
                "process: %s", exc,
            )
            return ""

    async def _maybe_show_first_use_hint(self, trace_id: str) -> None:
        """Surface the screen-reader-flag first-use hint when eligible (wh-r3xy1).

        Resolves the foreground process, consults the suppression tracker, and
        on a positive verdict pushes a ``click_first_use_hint`` action onto the
        existing GUI state queue. Never raises; any failure logs and returns so
        the click flow is unaffected.

        Latency (wh-9f3t.60.4): the foreground resolution does three blocking
        Win32 / psutil calls, so it is offloaded to a worker thread via
        ``asyncio.to_thread`` and never runs on the Logic asyncio loop. The
        ``recorded_shown`` short-circuit means neither the offload nor the
        tracker work runs once the hint has been recorded. The cheap tracker
        bookkeeping (and the rare, at-most-once ``os.fsync`` inside
        ``_record_shown`` it may trigger) stays on the loop; the fsync fires at
        most once per machine for the whole feature lifetime, so the ordering
        simplicity of keeping it inline outweighs offloading it.

        Delivery-gated display (wh-9f3t.61.2): the decision is split from the
        state mutation. ``tracker.evaluate`` is a pure verdict; on a SHOW
        verdict the GUI action is forwarded FIRST and the display is committed
        (``commit_displayed``) ONLY if the enqueue succeeded. If the GUI queue
        is absent or the put fails, the tracker is left unmutated so the next
        eligible click retries the display -- a hint the user never saw is
        never recorded as shown. A COUNT verdict advances the persistence
        counter via ``note_counted``.
        """
        try:
            from services.wheelhouse.click_first_use_hint import HintDecision

            tracker = self._first_use_hint_tracker()
            if tracker is None:
                return
            if tracker.recorded_shown:
                return
            process_name = await asyncio.to_thread(
                self._resolve_foreground_process_name
            )
            if not process_name:
                return
            decision = tracker.evaluate(
                process_name,
                flag_enabled=self._host.click_config.enable_screen_reader_flag,
            )
            if decision is HintDecision.SHOW:
                # Forward FIRST; commit the one-shot display only on a
                # successful enqueue so a failed delivery retries next click.
                if self._forward_first_use_hint(trace_id):
                    tracker.commit_displayed()
            elif decision is HintDecision.COUNT:
                tracker.note_counted()
        except Exception as exc:  # noqa: BLE001 -- never break the click flow
            logger.warning(
                "click_first_use_hint: hint hook failed (trace_id=%s): %s",
                trace_id, exc,
            )

    def _forward_first_use_hint(self, trace_id: str) -> bool:
        """Push the ``click_first_use_hint`` action to the GUI (wh-r3xy1).

        Rides the existing GUI state queue (the same queue carrying
        ``show_click_notice`` / ``show_rejection_toast``); no new IPC channel.
        The GUI renders ``HINT_TEXT`` via the existing info-notice path.

        Returns True iff the action was actually enqueued. The caller commits
        the tracker's one-shot display only on True so a missing-queue or
        failed-put does not burn the display (wh-9f3t.61.2).
        """
        from services.wheelhouse.click_first_use_hint import HINT_TEXT

        if not self._host.state_manager or not hasattr(
            self._host.state_manager, "state_to_gui_queue",
        ):
            logger.warning(
                "click_first_use_hint dropped, no state_manager queue "
                "(trace_id=%s)", trace_id,
            )
            return False

        gui_msg = {
            "action": "click_first_use_hint",
            "message": HINT_TEXT,
            "trace_id": trace_id,
        }
        try:
            self._host.state_manager.state_to_gui_queue.put_nowait(gui_msg)
            logger.info(
                "click_first_use_hint: surfaced first-use hint "
                "(trace_id=%s)", trace_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "click_first_use_hint dropped, GUI queue put failed "
                "(trace_id=%s): %s", trace_id, exc,
            )
            return False

    def apply_saved_overlay_keep_numbers(self, keys, saved) -> None:
        """Hand a saved ``click.overlay_keep_numbers`` to the running machine.

        Does nothing unless the save succeeded and ``keys`` names the setting.
        Reads the value back through ``ClickConfig.from_raw`` (the same reader
        the factory uses at start), so a value that reader refuses keeps its
        default. ``self.click_config`` is NOT replaced: every other [click]
        key keeps its restart semantics. Never raises.
        """
        if not saved or "click.overlay_keep_numbers" not in keys:
            return
        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        try:
            from services.wheelhouse.ui.click_config import ClickConfig

            machine.keep_numbers = ClickConfig.from_raw(
                self._host.config_service.get("click", {})
            ).overlay_keep_numbers
        except Exception as exc:  # noqa: BLE001 -- the save already succeeded
            logger.warning(
                "overlay: could not apply the saved overlay_keep_numbers "
                "value live: %s", exc,
            )
            return
        logger.info(
            "overlay: overlay_keep_numbers is now %s (applied live)",
            machine.keep_numbers,
        )

    def shutdown(self) -> None:
        """Release the overlay resources at Logic shutdown.

        LogicController.shutdown calls this first. It stops the focus hooks,
        cancels every overlay timer, drops the pending clear and restore-paint
        state, and empties the snapshot-summary cache. It never raises.
        """

        # wh-n29v.21: stop the overlay focus-hook thread and unhook both the
        # foreground hook and any transient destroy hook so no Win32 hook is
        # leaked across shutdown. getattr guard for the bypassed-__init__ case;
        # stop_overlay_focus_hooks is itself a no-op when no manager exists.
        if getattr(self, "_overlay_focus_hooks", None) is not None:
            self.stop_overlay_focus_hooks()

        # wh-n29v.96.2 / wh-n29v.95: cancel the per-state timeout timer, the
        # 200ms hold timer, and the periodic keepalive timer so no overlay timer
        # outlives shutdown. wh-overlay-slow-uia-stale-badges.18.9: also the
        # clear-ack watchdog deadline, and its pending slot beside it -- the
        # slow awaits below run on the still-live loop, so an armed 5000 ms
        # deadline could fire during an intentional exit and report a false
        # user-visible clear fault. getattr guards for the bypassed-__init__
        # case.
        for _timer_attr in (
            "_overlay_timer", "_overlay_hold_timer", "_overlay_keepalive_timer",
            "_overlay_pending_clear_timer",
            # wh-overlay-slow-uia-stale-badges.21.3: the restore-paint audit
            # deadline, for the same reason as the clear deadline beside it.
            "_overlay_pending_restore_paint_timer",
            # wh-overlay-refresh-after-mouse.1.1: the mouse-refresh quiet
            # time and the quiet window after a number click. The click on
            # the tray Exit item arms the first one, and it would start a
            # refresh walk during the awaits below.
            "_overlay_pointer_refresh_handle",
            "_overlay_number_click_quiet_handle",
        ):
            _t = getattr(self, _timer_attr, None)
            if _t is not None:
                try:
                    _t.cancel()
                except Exception:  # noqa: BLE001 -- never block shutdown
                    pass
                setattr(self, _timer_attr, None)
        # wh-overlay-refresh-after-mouse.1.1: the pynput hook delivers clicks
        # until stop_listeners runs, later in shutdown_services; the hook
        # entry posts nothing from here on.
        self._overlay_pointer_input_wanted = False
        if getattr(self, "_overlay_pending_clear", None) is not None:
            self._overlay_pending_clear = None
        if getattr(self, "_overlay_pending_restore_paint", None) is not None:
            self._overlay_pending_restore_paint = None
        # wh-overlay-slow-uia-stale-badges.21.8: and the delivery entitlement
        # beside it, so a batch still queued behind the effect lock cannot
        # present badges during an intentional exit.
        if getattr(self, "_overlay_restore_paint_entitlement", None) is not None:
            self._overlay_restore_paint_entitlement = None

        # wh-tab7j: drop every retained WalkSnapshotSummary (eviction
        # trigger 3). getattr guard for the bypassed-__init__ case.
        cache = getattr(self, "click_snapshot_summary_cache", None)
        if cache is not None:
            try:
                cache.clear()
            except Exception:  # noqa: BLE001 -- never block shutdown
                pass
