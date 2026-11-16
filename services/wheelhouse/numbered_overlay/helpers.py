"""Module-level code of the numbered overlay (wh-overlay-refactor-pilot.2).

Moved without change from services/wheelhouse/main.py: the focus-hook logger,
the Win32 event constants, the click-response helpers, the state machine
factory, the window-facts helpers, the keepalive states, the overlay timing
constants, and the Win32 focus-hook manager. NumberedOverlayController in
controller.py uses them.
"""

from __future__ import annotations

import logging
import os
import time
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

# Dedicated logger for the overlay focus-hook no-op / debounce-coalesced lines.
# These fire on every foreground change, menu pop-up, and no-op state-machine
# event -- one idle desktop can emit hundreds per minute -- and carry no
# diagnostic value unless you are actively debugging the overlay focus hooks.
# Pinning this child logger to INFO gates its own DEBUG records even when the
# root logger runs at DEBUG (LOG_LEVEL=DEBUG), so the noise stays out of the
# file. setup_logging() only sets the root level and a fixed list of
# third-party loggers, so this import-time level survives. Flip it to DEBUG
# here to get the lines back when debugging the overlay. wh-log-noise-debug-demote.
_overlay_focus_logger = logging.getLogger(f"{__name__}.overlay_focus")
_overlay_focus_logger.setLevel(logging.INFO)

# Win32 WinEvent constants for the overlay focus hooks (wh-n29v.21). Mirrors
# features/window_mover.py's values; defined here so the hook manager below has
# no hard dependency on importing that module.
_EVENT_SYSTEM_FOREGROUND = 0x0003
_EVENT_OBJECT_DESTROY = 0x8003
_OBJID_WINDOW = 0  # only top-level-window destroys (OBJID_WINDOW) are of interest

# wh-overlay-stale-click-refresh: the pre-click verification reasons that mean
# the numbered badge no longer points at a clickable control IN THE SAME PLACE,
# so a fresh walk + repaint of the overlay is the right response to a refused
# "click N". ClickExecutor._verify (ui/click_executor.py) returns these:
#   bounds_invalid        -- the live position is unreadable or zero-area now
#   bounds_stale          -- the control drifted past the per-dimension tolerance
#   target_moved_offscreen-- the control's centre scrolled off-screen (this check
#                            runs BEFORE the bounds_stale drift check, so a fully
#                            scrolled-away control reports THIS, not bounds_stale)
#   popup_closed          -- the owning popup the badge pointed at is gone
#   taskbar_closed        -- the taskbar shell window the badge pointed at is
#                            gone or its HWND was recycled for another window
# Excluded on purpose: "disabled", an Invoke/COM failure, and "item_not_found"
# are NOT position staleness -- a re-walk finds the same control in the same
# place (or the same missing item), so it would not change the outcome and would
# only churn the overlay. "verification_timeout" is also excluded on purpose
# (wh-overlay-slow-uia-stale-badges.6): it means the application answered its
# COM reads too slowly, and a hung provider must not be answered with MORE UIA
# load (a re-walk hits the same slow provider).
_OVERLAY_REWALK_REFUSAL_REASONS = frozenset(
    {
        "bounds_invalid",
        "bounds_stale",
        "target_moved_offscreen",
        "popup_closed",
        "taskbar_closed",
        # wh-overlay-slow-uia-stale-badges.8: Input refused the click because
        # an earlier click already executed against the same (or a newer)
        # overlay generation -- the screen the number came from is gone. A
        # re-walk is the recovery that brings usable numbers back without
        # another spoken command. Accepted tradeoff: if the machine is
        # REFRESH_IN_FLIGHT when this refusal lands, the CLICK_COMPLETE feed
        # records a pending post-click refresh and one extra walk can run.
        "stale_overlay_generation",
    }
)

# wh-overlay-fixqueue-review.2: how long after a repaint swap a
# "click N" is checked against the pre-swap badge identities. Sized to cover
# the read-then-speak-then-STT round trip of an utterance begun against the
# old badges; a click blocked by the check shows the "numbers just changed"
# notice instead. Module-level (not a class attribute) so the real value is
# read even on MagicMock(spec=LogicController) test controllers.
_OVERLAY_RENUMBER_GRACE_SECONDS = 3.0

# wh-overlay-slow-uia-stale-badges.7: how long a screen read requested
# mid-sentence waits for the open dictation sentence to end before the
# read is sent to Input anyway. The settle path arms its timer BEFORE
# its build, so ``make_click_overlay_state_machine`` adds this bound to
# the settle deadline and ``_overlay_dispatch_build`` adds it to the
# walking cue it emits before the same wait
# (wh-overlay-slow-uia-stale-badges.3.1.1); a change to this value
# therefore widens both automatically.
# The other build paths arm their timer after the build completes, so
# they do not race this wait. Module-level (not a class attribute) so
# the real value is read even on MagicMock(spec=LogicController) test
# controllers; tests override per-instance via
# ``_overlay_read_sentence_wait_max_s``.
_OVERLAY_READ_SENTENCE_WAIT_MAX_S = 5.0


def _click_response_notice_kwargs(
    response,
    *,
    spoken_name: str,
    fallback_snapshot_id=None,
    trace_id: str,
    late_correction: bool = False,
) -> dict:
    """The arguments ``forward_click_notice`` takes for a non-ok response.

    The one place the response-to-notice mapping lives. Both
    ``_forward_click_response_notice`` (shows the notice) and
    ``forward_click_element`` with ``defer_failure_notice=True`` (hands the
    same arguments to the caller without showing anything,
    wh-safety-word-free-commands) build their call from it, so the notice a
    deferred failure shows later is identical to the one shown today.
    """
    return dict(
        outcome=response.outcome,
        reason=response.reason,
        matched_name=response.matched_name,
        matched_names=response.matched_names,
        spoken_name=spoken_name,
        snapshot_id=response.snapshot_id or fallback_snapshot_id,
        trace_id=response.trace_id or trace_id,
        late_correction=late_correction,
    )


def _forward_click_response_notice(
    controller,
    response,
    *,
    spoken_name: str,
    fallback_snapshot_id=None,
    trace_id: str,
    late_correction: bool = False,
) -> None:
    """Forward the notice for a non-ok ClickElementResponse (the ONE mapping).

    The single response-to-notice mapping for a parsed non-ok
    ``ClickElementResponse``, shared by the on-time awaiter paths and the
    late-answer callbacks (wh-overlay-slow-uia-stale-badges.6) so the two can
    never drift. ``fallback_snapshot_id`` is used when the response carries no
    snapshot id (the badge path passes its dispatch-time snapshot_id; the
    by-name path has no fallback). A module-level function, not a method, so a
    ``MagicMock(spec=LogicController)`` test controller cannot auto-mock it
    into a no-op between the awaiter and ``forward_click_notice``.

    ``late_correction`` (wh-overlay-slow-uia-stale-badges.20.11) is passed
    straight through: only ``_handle_late_click_response`` sets it, and it
    keeps a correction for an OLDER click from claiming the newest-attempt
    marker.
    """
    controller.forward_click_notice(
        **_click_response_notice_kwargs(
            response,
            spoken_name=spoken_name,
            fallback_snapshot_id=fallback_snapshot_id,
            trace_id=trace_id,
            late_correction=late_correction,
        )
    )


def _handle_late_click_response(
    controller,
    raw,
    *,
    action: str,
    spoken_name: str,
    fallback_snapshot_id=None,
    trace_id: str,
    overlay_dispatch_pair=None,
    overlay_dispatch_ordinal: int = 0,
) -> None:
    """Handle a click answer that arrived after the awaiter gave up (Option A).

    wh-overlay-slow-uia-stale-badges.6: the awaiter already showed the
    "Wheelhouse timed out while clicking." notice. When the true answer
    arrives within app.py's grace window, this callback corrects the message
    and finishes the click's Logic-side bookkeeping
    (wh-overlay-slow-uia-stale-badges.20.3), in this order
    (wh-overlay-slow-uia-stale-badges.20.9):

      1. a parsed answer carrying snapshot_id + snapshot_summary populates
         ``click_snapshot_summary_cache`` first, exactly like the on-time
         by-name put, so the badge resolution can read a late summary;
      2. an OK answer runs the on-time success bookkeeping when the caller
         supplied ``overlay_dispatch_pair``
         (wh-overlay-slow-uia-stale-badges.20.8): cancel a held 'click N'
         armed at that pair and feed the same CLICK_COMPLETE refresh, with
         the same arguments, under the feed's own generation/PAINTED gates
         (a superseded pair is a safe no-op). The notice behaviour is
         unchanged: INFO log only, never a success notice (decided
         residual: no success-notice mechanism exists, and the user saw
         the click happen). A by-name late ok (no pair) stays log-only;
      3. a NON-OK answer whose reason is in
         ``_OVERLAY_REWALK_REFUSAL_REASONS`` feeds the badge path's
         post-click re-walk (only the badge caller supplies
         ``overlay_dispatch_pair``) INDEPENDENT of the notice gate below
         (wh-overlay-slow-uia-stale-badges.20.9): the refusal is evidence
         the painted badges are stale even when a newer notice suppresses
         the corrective TOAST, so the recovery must not be dropped with it;
      4. the trace-id staleness gate below decides whether the corrective
         notice may still be shown;
      5. a gated NON-OK answer forwards the true refusal through the SAME
         response-to-notice mapping the on-time path uses
         (``_forward_click_response_notice``); the click-notice toast is a
         singleton whose show_notice replaces the visible text, so the
         corrected refusal replaces the timeout wording in place.

    A malformed answer logs and forwards nothing -- the timeout notice
    already on screen is more accurate than a malformed_response notice.

    STALENESS GATE (wh-overlay-slow-uia-stale-badges.20.4, .20.10): the
    correction is forwarded only while the click-notice toast can still be
    showing THIS click's feedback. ``forward_click_notice`` records two
    ids: ``_last_click_notice_attempt_trace_id`` at method entry (EVERY
    attempt, successful or not) and ``_last_click_notice_trace_id`` after a
    successful GUI-queue put. The correction is forwarded when ``trace_id``
    equals EITHER id and suppressed otherwise, with an INFO log naming all
    three ids. The three sequences (wh-overlay-slow-uia-stale-badges.20.10):

      * A's timeout notice put FAILED (queue full) and nothing newer was
        attempted -> last attempted == A -> FORWARD: the late refusal is
        the first feedback for the newest click, not a stale overwrite.
      * A's timeout notice succeeded, then a newer B attempt FAILED ->
        last successful == A -> FORWARD: the toast still shows A's timeout
        wording, so correcting it stays accurate.
      * A's attempt failed, then a newer B2 succeeded -> neither id is A
        -> SUPPRESS: B2's newer notice is on the toast and must not be
        overwritten.

    Forwarding a correction does NOT advance the last-ATTEMPT id
    (wh-overlay-slow-uia-stale-badges.20.11): it passes late_correction=True,
    so an older click's correction cannot displace a newer click whose own
    notice put failed and whose true answer is still to come.

    Accepted residuals, decided: the correction may re-show the toast after
    its 8 s auto-dismissal (Logic cannot observe GUI-side dismissal and the
    text is truthful), and two in-flight corrections replace each other in
    arrival order.

    DELIBERATE SKIP, decided (wh-overlay-slow-uia-stale-badges.20.3): a late
    AMBIGUOUS answer does NOT auto-open the numbered overlay -- an overlay
    appearing seconds after the command, unprompted, is worse for the user
    than the corrected notice alone. Only the ambiguous notice is forwarded.

    Runs synchronously on Logic's event loop (the demuxer schedules it via
    call_soon), so it may touch the GUI state queue exactly like the on-time
    path. Never raises: app.py guards the callback, but a raise here would
    still land in the error log, so the malformed parse is caught locally.
    """
    from shared.click_element import (
        ClickElementResponse,
        ClickElementResponseSchemaError,
    )

    logger.info(
        "%s: the reply arrived after the %sms limit; correcting the timeout "
        "notice if the click was refused (trace_id=%s)",
        action, controller._host.click_config.response_timeout_ms, trace_id,
    )
    try:
        response = ClickElementResponse.from_dict(raw)
    except (
        ClickElementResponseSchemaError, ValueError, KeyError, TypeError,
    ) as exc:
        logger.warning(
            "%s: late reply was malformed; the timeout notice stands "
            "(trace_id=%s): %s", action, trace_id, exc,
        )
        return
    # Populate the snapshot-summary cache exactly like the on-time by-name
    # put (wh-overlay-slow-uia-stale-badges.20.3): keyed by snapshot_id,
    # skipped when no walk produced a summary.
    if response.snapshot_id and response.snapshot_summary is not None:
        try:
            controller.click_snapshot_summary_cache.put(
                response.snapshot_id, response.snapshot_summary,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s: failed to populate snapshot cache from the late reply "
                "(trace_id=%s): %s", action, trace_id, exc,
            )
    if response.outcome == "ok":
        logger.info(
            "%s: late reply reports the click landed (matched=%r); no "
            "corrective notice (trace_id=%s)",
            action, response.matched_name, trace_id,
        )
        if overlay_dispatch_pair is not None:
            # wh-overlay-slow-uia-stale-badges.20.8: mirror the on-time ok
            # branch's bookkeeping with the SAME arguments -- cancel a held
            # 'click N' armed at the dispatch pair, then feed the
            # CLICK_COMPLETE refresh so the overlay stops painting the
            # pre-click snapshot. The feed's generation/PAINTED gates make
            # a superseded pair a safe no-op. A by-name late ok has no
            # pair and stays log-only.
            controller._cancel_held_click_after_success(
                overlay_dispatch_pair=overlay_dispatch_pair,
                trace_id=trace_id,
            )
            controller._feed_click_complete_refresh(
                overlay_dispatch_pair=overlay_dispatch_pair,
                trace_id=trace_id,
                click_succeeded=True,
                preclick_snapshot_id=fallback_snapshot_id,
                overlay_dispatch_ordinal=overlay_dispatch_ordinal,
            )
        return
    # Badge path (wh-overlay-slow-uia-stale-badges.20.3): mirror the on-time
    # tail's re-walk for the refusal reasons that mean the painted layout is
    # stale. The by-name caller passes no dispatch pair, so it never feeds
    # (matching its on-time path); a badge caller whose pair is None would
    # be a no-op inside the feed anyway. DELIBERATELY BEFORE the staleness
    # gate (wh-overlay-slow-uia-stale-badges.20.9): a newer notice only
    # suppresses the corrective toast, not the stale-badge recovery this
    # refusal proves is needed; the feed's own gates protect a superseded
    # overlay.
    if (
        overlay_dispatch_pair is not None
        and response.reason in _OVERLAY_REWALK_REFUSAL_REASONS
    ):
        controller._feed_click_complete_refresh(
            overlay_dispatch_pair=overlay_dispatch_pair,
            trace_id=response.trace_id or trace_id,
            click_succeeded=False,
            trigger=f"click refused ({response.reason})",
            preclick_snapshot_id=fallback_snapshot_id,
            overlay_dispatch_ordinal=overlay_dispatch_ordinal,
        )
    last_attempted = getattr(
        controller, "_last_click_notice_attempt_trace_id", None,
    )
    last_forwarded = getattr(controller, "_last_click_notice_trace_id", None)
    if trace_id != last_attempted and trace_id != last_forwarded:
        # wh-overlay-slow-uia-stale-badges.20.4/.20.10: a newer notice
        # (attempted or shown) replaced this click's feedback; the stale
        # correction must not overwrite it. Matching the last ATTEMPT as
        # well covers the click whose own timeout notice never left Logic
        # (queue full): its late refusal is first feedback, not overwrite.
        logger.info(
            "%s: suppressing the late correction for trace_id=%s -- the "
            "last forwarded click notice belongs to trace_id=%s and the "
            "last attempted to trace_id=%s",
            action, trace_id, last_forwarded, last_attempted,
        )
        return
    logger.info(
        "%s: late reply is a refusal (outcome=%s reason=%r); forwarding the "
        "corrected notice (trace_id=%s)",
        action, response.outcome, response.reason, trace_id,
    )
    _forward_click_response_notice(
        controller,
        response,
        spoken_name=spoken_name,
        fallback_snapshot_id=fallback_snapshot_id,
        trace_id=trace_id,
        late_correction=True,
    )


def make_click_overlay_state_machine(click_config):
    """Build the Logic-side overlay state machine from the validated config.

    A module-level factory rather than an inline constructor call, so the
    config-to-machine wiring can be tested without building a whole
    LogicController (wh-overlay-slow-uia-stale-badges.1).

    ``settle_after_click`` comes straight from the config. Two of the three
    timeout durations are DERIVED from ``[click] screen_read_timeout_ms``
    (wh-overlay-slow-uia-stale-badges.3), because both guard a state that is
    waiting on a screen read and neither may fire while that read is still
    inside its own bound:

    * ``walk_deadline_ms`` guards ``walk_in_flight`` / ``refresh_in_flight``.
      Today it is armed AFTER the build round trip, so it does not bound the
      read; deriving it anyway means a later change that arms it first cannot
      silently reintroduce the 2500 ms discard.
    * ``settle_deadline_ms`` guards ``post_click_settling``, and this one is
      load-bearing today: the settle state arms its timer BEFORE the read, so
      at the class default of 8000 a settle read answering at 9 s would be
      dropped by the state while the Logic awaiter still had a second left.
      It never goes below the class default, so raising the read limit widens
      it and lowering the read limit leaves it alone. It also adds
      ``_OVERLAY_READ_SENTENCE_WAIT_MAX_S`` in milliseconds
      (wh-overlay-slow-uia-stale-badges.3.1.1): the settle build waits that
      long for an open dictation sentence to end BEFORE it sends the read, and
      the read's own bound starts at the send, so the timer's window has to
      hold the wait as well as the read for the invariant above to be true on
      this path. ``walk_deadline_ms`` needs no such term -- its timers are
      armed after the round trip that contains the wait.

    Both add ``PRE_WALK_MARGIN_MS`` for the same reason the Input-side bound
    subtracts it: these timers start before the IPC send, the Input walk
    deadline starts at command dequeue. ``paint_deadline_ms`` keeps the class
    default -- a paint is a GUI round trip, not a read.
    """
    from services.wheelhouse.click_overlay_state import (
        ClickOverlayStateMachine,
    )
    from services.wheelhouse.ui.click_config import PRE_WALK_MARGIN_MS

    read_bound_ms = click_config.screen_read_timeout_ms + PRE_WALK_MARGIN_MS
    wait_ms = int(_OVERLAY_READ_SENTENCE_WAIT_MAX_S * 1000)
    return ClickOverlayStateMachine(
        settle_after_click=click_config.overlay_settle_after_click,
        # wh-numbers-stay-setting: False (the default) removes the numbers
        # after a successful click and after a change of window; True keeps
        # showing new numbers. The settings window changes it live through
        # ``LogicController._apply_saved_overlay_keep_numbers``.
        keep_numbers=click_config.overlay_keep_numbers,
        walk_deadline_ms=read_bound_ms,
        settle_deadline_ms=max(
            ClickOverlayStateMachine.settle_deadline_ms,
            read_bound_ms + wait_ms,
        ),
    )


class _OverlayWindowFacts(NamedTuple):
    """What the numbers-stay filter reads about one window.

    ``root`` is ``GetAncestor(hwnd, GA_ROOT)``: the top-level window that
    contains ``hwnd`` through its parent chain (``hwnd`` itself for a top-level
    window). It is NOT the owner: a dialog that the numbered window owns is a
    top-level window of its own, so its ``root`` is the dialog.
    """

    class_name: str
    pid: int
    root: int


def _overlay_window_facts(hwnd: int) -> "Optional[_OverlayWindowFacts]":
    """Read the class name, process id and top-level window of ``hwnd``.

    The Win32 seam of the wh-numbers-stay-setting window-change filter
    (``LogicController._foreground_change_dismisses_numbers``); tests patch it.
    Returns ``None`` when the window cannot be read (it is gone, or pywin32 is
    missing), and the caller then keeps today's refresh.
    """
    try:
        import win32con
        import win32gui
        import win32process
    except Exception:  # noqa: BLE001 -- non-Windows / missing pywin32
        return None
    try:
        handle = int(hwnd)
        if not handle or not win32gui.IsWindow(handle):
            return None
        class_name = str(win32gui.GetClassName(handle))
        _thread, pid = win32process.GetWindowThreadProcessId(handle)
        root = int(win32gui.GetAncestor(handle, win32con.GA_ROOT)) or handle
        return _OverlayWindowFacts(
            class_name=class_name, pid=int(pid or 0), root=root,
        )
    except Exception:  # noqa: BLE001 -- the window can vanish mid-read
        return None


def _overlay_current_foreground_hwnd() -> int:
    """The window that is in front now (``GetForegroundWindow``), or 0.

    The seam the settle refire uses to judge a burst of foreground events by
    where the burst ended (wh-numbers-stay-setting, ``_consume_pending_foreground_dismissal``);
    tests patch it. 0 means the window cannot be read, and the caller then keeps
    today's refresh.
    """
    try:
        import win32gui

        return int(win32gui.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001 -- non-Windows / missing pywin32
        return 0


# How many badge clicks' "show numbers" counts ``LogicController`` remembers
# (wh-numbers-stay-setting). The value equals ``app._LATE_RESPONSE_CAPACITY``
# (32), the most click replies that can arrive late, so no count that a late
# reply needs is pruned before the reply arrives.
_OVERLAY_CLICK_SHOW_COUNT_KEEP = 32

# Window classes whose foreground changes are not the user moving to another
# application window (wh-numbers-stay-setting, item 10).
_OVERLAY_NON_WINDOW_CHANGE_CLASSES = frozenset({"tooltips_class32"})


def _overlay_keepalive_states():
    """The states whose pinned snapshot the keepalive must keep alive.

    THE KEEPALIVE CONTRACT, stated once here because five other comments
    used to state it for themselves and all of them said it wrongly
    (wh-overlay-slow-uia-stale-badges.13.6). The predicate is that a
    RETAINED pin still needs its TTL slid. It is NOT that badges are on
    screen. The three states differ in why they retain the pin:

      PAINTED               badges are on screen and the user can click a
                            number.
      PAUSED                badges are NOT on screen -- painted -> paused
                            dispatches a clear, and the router HOLDS a
                            spoken number here. The pin is retained so
                            MIC_RESUME can repaint the same snapshot
                            without a re-walk.
      POST_CLICK_SETTLING   badges are NOT on screen -- entry cleared them.
                            The pin is retained for the read that child .2
                            will compare against the list that was on
                            screen.

    So PAUSED was already a counterexample to "pinned means visible" before
    the settling state existed; the settling state is the second one.

    Every retained pin needs the slide, because a pin does NOT stop expiry
    in either store: the Input-side store expires PINNED snapshots on TTL
    (pin blocks LRU eviction only), and the Logic-side summary cache has no
    pin concept at all. POST_CLICK_SETTLING makes that urgent -- its window
    is settle_deadline_ms (8000 ms), longer than the shortest valid
    snapshot_ttl_seconds.

    ONE list, read by both keepalive seams -- the reconciler that arms the
    timer and the callback that fires it. Two separate literal tuples is
    what produced wh-overlay-slow-uia-stale-badges.13.5: a state added to
    one filter and not the other gives an armed timer whose callback
    returns at once, or a callback that can never run.
    """
    from services.wheelhouse.click_overlay_state import OverlayState

    return (
        OverlayState.PAINTED,
        OverlayState.PAUSED,
        OverlayState.POST_CLICK_SETTLING,
    )


# wh-overlay-slow-uia-stale-badges.9: the clear-ack watchdog and the GUI
# badge-lease renew. Class-level defaults, not config keys.
#
# The watchdog: every DISPATCH_CLEAR arms a deadline; if no ack at a pair
# >= the clear's pair arrives in time, Logic REPORTS the miss as an ERROR
# (the error notifier pops it to the user). 5000ms is generous against the
# GUI's 100ms queue poll plus paint-thread stalls, while still reporting
# well before the GUI-side lease (>= 60s) removes the badges itself.
_OVERLAY_CLEAR_ACK_DEADLINE_MS = 5000
# wh-overlay-slow-uia-stale-badges.21.3: the restore-paint delivery audit. The
# paused-resume restore is the one paint the machine commits synchronously, so
# no per-state deadline covers it; this bounds how long Logic may believe the
# restored badges are up before the display confirms them. Same budget and same
# reasoning as the clear deadline above -- generous against the display's 100ms
# queue poll and paint-thread stalls, and far shorter than the display-side
# badge lease.
_OVERLAY_RESTORE_ACK_DEADLINE_MS = 5000
# wh-overlay-slow-uia-stale-badges.21.5: the audit is armed twice. The machine
# commits PAINTED inside ``_apply_overlay_event``, which only SCHEDULES the
# effects, and an older batch holds ``_overlay_effect_lock`` across every
# awaited pin and unpin. So the FIRST arming happens at schedule time, and its
# budget must also cover that lock wait; the deadline above is re-armed when
# the paint actually reaches the display queue, so the display leg keeps its
# own unchanged budget. A paused-entry batch awaits at most a PIN and an UNPIN
# (``_refresh_build_ok`` auto-hide leg), each bounded by
# ``[click] response_timeout_ms``, hence the factor of two on that bound
# alone -- NOT the same bound the walking-cue fallback carries as
# ``walk_timeout_ms``, which now also carries the build's own awaited window
# (``screen_read_timeout_ms`` for a walk) and the sentence-wait bound; see
# ``_overlay_dispatch_build``.
_OVERLAY_RESTORE_LOCK_WAIT_REQUESTS = 2
# wh-overlay-slow-uia-stale-badges.21.10: and the doubled result needs a
# ceiling. When this ceiling was added, ``response_timeout_ms`` had no upper
# bound of its own: ui/click_config.py validated it with
# ``_is_int_at_least(100)``, and tomllib parses arbitrary-precision integers
# rather than enforcing TOML's 64-bit range, so a value of 10**308 reached a
# live, ENABLED ClickConfig. wh-click-response-timeout-unbounded has since
# bounded that key at 10000 ms, so the overflow below is no longer reachable
# through the config file. This ceiling stays as the second line of defence,
# because it protects the arithmetic rather than one key: it also covers a
# caller that builds a ClickConfig directly, without the validator. Doubling
# an unbounded value exceeds what a float can hold, and ``budget_ms / 1000.0``
# in ``_overlay_start_restore_paint_audit`` then raises OverflowError INSIDE
# the schedule-time arm -- before the dispatch task is created, with the
# machine already committed PAINTED. Logic would keep resolving "click N"
# against badges that never reached the screen. The ceiling is the 32-bit
# signed millisecond maximum, about 24.8 days: no timeout a real operator
# writes is anywhere near it, so the clamp cannot shorten a legitimate budget
# and make the audit close a working overlay early.
_OVERLAY_RESTORE_SCHEDULE_BUDGET_MAX_MS = 2_147_483_647
# The lease renew: each keepalive tick in PAINTED grants the GUI
# lease_ms = max(floor, ticks * keepalive interval). Four missed ticks (or
# the 60s floor, whichever is longer) is unambiguous "Logic stopped
# talking", not scheduling jitter. The GUI's own no-renew default
# (gui._OVERLAY_LEASE_DEFAULT_MS, 90s) exceeds the floor so the first
# renew always lands before the initial lease can fire.
_OVERLAY_LEASE_FLOOR_MS = 60000
_OVERLAY_LEASE_INTERVAL_TICKS = 4


class OverlayFocusHookManager:
    """Thin Win32 ``SetWinEventHook`` seam for the numbered overlay (wh-n29v.21).

    This is the DELIBERATELY-THIN, hard-to-unit-test seam that mirrors the
    registration shape in ``features/window_mover.py``: a message-only window
    on a dedicated daemon thread, a ``WinEventProcType`` callback, and a
    ``PeekMessage`` pump. All the testable DECISION logic (debounce,
    generation supersession, identity comparison, raw-event -> OverlayEvent
    mapping) lives in the pure module ``overlay_focus_hooks.py``; this class
    only registers the OS hooks and marshals each callback onto the owning
    asyncio loop via ``loop.call_soon_threadsafe`` before any state is touched.

    Three hooks:

      * A FOREGROUND hook (``EVENT_SYSTEM_FOREGROUND``,
        ``WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS``) registered for the
        whole lifetime of the manager. Its callback forwards the new HWND to
        ``on_foreground(hwnd)`` on the loop.
      * A MENU POP-UP hook (the ``EVENT_SYSTEM_MENUPOPUPSTART`` ..
        ``MENUPOPUPEND`` range, same flags) registered alongside the
        foreground hook when ``on_menu_popup`` is supplied
        (wh-overlay-menu-close-stale). Closing a menu does not change the
        foreground window, so without this hook badges painted for the (now
        gone) menu items stay floating over the page. Its callback forwards
        the raw event id to ``on_menu_popup(event_id)`` on the loop.
        Registration failure is NON-fatal (a warning; focus following still
        works) -- the menu re-walk is an improvement, not the manager's
        purpose.
      * A TRANSIENT DESTROY hook (``EVENT_OBJECT_DESTROY``,
        ``WINEVENT_OUTOFCONTEXT``) registered only while the overlay is
        ``paused``, scoped to the tracked window's process/thread id via the
        ``idProcess`` / ``idThread`` parameters of ``SetWinEventHook``. Its
        callback forwards ``(destroyed_hwnd, object_id)`` to
        ``on_destroy(destroyed_hwnd)`` on the loop. ``register_destroy_hook`` /
        ``unregister_destroy_hook`` are called from the Logic loop when the
        machine enters / leaves ``paused``.

    The thread is started by :meth:`start` and torn down by :meth:`stop`, which
    unhooks both hooks (no leaked hooks on shutdown). The callbacks NEVER touch
    the state machine directly -- they only schedule the loop callbacks -- so
    the machine stays single-thread-owned by the Logic loop.
    """

    def __init__(
        self, loop, on_foreground, on_destroy, on_menu_popup=None,
    ) -> None:
        self._loop = loop
        self._on_foreground = on_foreground
        self._on_destroy = on_destroy
        # wh-overlay-menu-close-stale: optional; when None the menu pop-up
        # hook is not registered (older construction sites keep working).
        self._on_menu_popup = on_menu_popup

        self._running = False
        self._thread = None
        self._message_only_hwnd = None
        self._window_class_name = f"OverlayFocusHookMsgWnd_{os.getpid()}"
        self._thread_ready = None  # threading.Event, created in start()

        self._foreground_proc_cb = None
        self._foreground_hook = None
        self._menu_popup_proc_cb = None
        self._menu_popup_hook = None
        # Transient destroy hook bookkeeping; mutated only from the hook thread
        # via the WM_USER request below or, during teardown, from stop().
        self._destroy_proc_cb = None
        self._destroy_hook = None
        self._pending_destroy_pid = 0
        self._pending_destroy_tid = 0
        # WM_APP requests posted from the Logic loop to the hook thread so the
        # transient destroy hook is registered/unregistered ON the hook thread
        # (SetWinEventHook hooks are owned by the thread that creates them).
        self._WM_REGISTER_DESTROY = 0x8000 + 1   # WM_APP + 1
        self._WM_UNREGISTER_DESTROY = 0x8000 + 2  # WM_APP + 2

    # -- lifecycle -----------------------------------------------------------
    def is_alive(self) -> bool:
        return bool(
            self._running
            and self._thread is not None
            and self._thread.is_alive()
        )

    def start(self) -> bool:
        """Start the hook thread; returns True once the message window is ready."""
        import threading

        if self._running:
            return True
        self._running = True
        self._thread_ready = threading.Event()
        self._thread = threading.Thread(
            target=self._message_loop, name="OverlayFocusHookThread", daemon=True,
        )
        self._thread.start()
        if not self._thread_ready.wait(timeout=3.0):
            logger.error(
                "OverlayFocusHookManager: timeout waiting for hook thread "
                "to become ready; overlay focus following disabled.",
            )
            self._running = False
            return False
        # wh-n29v.23.1: the readiness Event is signalled on BOTH success and
        # every startup-failure path (window-create error, foreground-hook
        # failure). On any failure the hook thread sets _running False (and runs
        # _teardown) BEFORE signalling ready, so a cleared _running here means
        # the message window or the foreground hook did not come up. Report that
        # as a failed start so the caller leaves focus following disabled rather
        # than storing a manager whose hook is dead.
        if not self._running:
            logger.warning(
                "OverlayFocusHookManager: hook thread reported a startup "
                "failure; overlay focus following disabled this session.",
            )
            return False
        logger.info("OverlayFocusHookManager: foreground hook thread ready.")
        return True

    def stop(self) -> None:
        """Stop the hook thread and unhook BOTH hooks (no leaked hooks)."""
        if not self._running:
            return
        self._running = False
        try:
            import win32con
            import win32gui

            if self._message_only_hwnd and win32gui.IsWindow(
                self._message_only_hwnd
            ):
                win32gui.PostMessage(
                    self._message_only_hwnd, win32con.WM_NULL, 0, 0,
                )
        except Exception as exc:  # noqa: BLE001 -- never block shutdown
            logger.debug(
                "OverlayFocusHookManager: failed to wake hook loop on stop: %s",
                exc,
            )
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=4.0)
            if thread.is_alive():
                logger.warning(
                    "OverlayFocusHookManager: hook thread did not stop within "
                    "the join timeout.",
                )
        self._thread = None

    # -- transient destroy hook (called from the Logic loop) ----------------
    def register_destroy_hook(self, *, pid: int, tid: int) -> bool:
        """Request registration of the transient destroy hook (while paused).

        Posts a request to the hook thread so the hook is created ON that
        thread (the thread that owns the message pump and the foreground hook).
        Scoped to ``pid`` / ``tid`` of the tracked foreground window. A
        ``pid``/``tid`` of 0 would register a SYSTEM-WIDE destroy hook, which
        is far too noisy; the caller must supply a real pair, and a 0 pair is
        rejected here as a no-op.

        Returns True only when the register request was ACCEPTED (the manager is
        alive, the message window exists, the pid/tid are real, and PostMessage
        succeeded). Returns False otherwise, so the caller does not record the
        hook as active when the request never reached the hook thread
        (wh-n29v.23.3). NOTE: a True here means the request was posted, not that
        the hook-thread ``SetWinEventHook(DESTROY)`` ultimately succeeded -- that
        call is asynchronous and best-effort. A later hook-thread failure
        degrades gracefully to the resume-time full-identity check, which design
        v4 makes the PRIMARY stale-overlay defence (the destroy hook is
        defence-in-depth).
        """
        if not self.is_alive() or not self._message_only_hwnd:
            return False
        if pid <= 0 or tid <= 0:
            logger.debug(
                "OverlayFocusHookManager: destroy-hook register skipped "
                "(pid=%s tid=%s); a real pid/tid is required to scope the hook.",
                pid, tid,
            )
            return False
        try:
            import win32api

            self._pending_destroy_pid = int(pid)
            self._pending_destroy_tid = int(tid)
            win32api.PostMessage(
                self._message_only_hwnd, self._WM_REGISTER_DESTROY, 0, 0,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "OverlayFocusHookManager: failed to post destroy-hook "
                "register request: %s", exc,
            )
            return False

    def unregister_destroy_hook(self) -> bool:
        """Request removal of the transient destroy hook (leaving paused).

        Returns True when the unregister request was posted, False otherwise.
        """
        if not self._message_only_hwnd:
            return False
        try:
            import win32api

            win32api.PostMessage(
                self._message_only_hwnd, self._WM_UNREGISTER_DESTROY, 0, 0,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "OverlayFocusHookManager: failed to post destroy-hook "
                "unregister request: %s", exc,
            )
            return False

    # -- hook-thread internals ----------------------------------------------
    def _message_loop(self) -> None:
        import ctypes
        from ctypes import wintypes

        import win32api
        import win32con
        import win32gui

        win_event_proc_type = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.HWND,
            wintypes.LONG,
            wintypes.LONG,
            wintypes.DWORD,
            wintypes.DWORD,
        )

        def _foreground_proc(
            _hook, _event, hwnd, _id_object, _id_child, _thread, _time,
        ) -> None:
            # OS worker thread: marshal onto the Logic loop and do nothing else.
            if not self._running or not hwnd:
                return
            try:
                self._loop.call_soon_threadsafe(self._on_foreground, int(hwnd))
            except Exception:  # noqa: BLE001 -- loop may be closing
                pass

        def _destroy_proc(
            _hook, _event, hwnd, id_object, _id_child, _thread, _time,
        ) -> None:
            # Only top-level window (OBJID_WINDOW) destroys matter; child-object
            # destroys in the same process are filtered here so the loop is not
            # woken for every transient child teardown.
            if not self._running or not hwnd or id_object != _OBJID_WINDOW:
                return
            try:
                self._loop.call_soon_threadsafe(self._on_destroy, int(hwnd))
            except Exception:  # noqa: BLE001
                pass

        def _menu_popup_proc(
            _hook, event, _hwnd, _id_object, _id_child, _thread, _time,
        ) -> None:
            # wh-overlay-menu-close-stale: OS worker thread -- marshal the raw
            # event id onto the Logic loop and do nothing else. The pure
            # mapper (map_menu_popup_event) and the machine's state gating run
            # on the loop; no idObject filter here because the registered
            # range carries only the two pop-up ids and the mapper drops
            # anything else anyway.
            if not self._running or self._on_menu_popup is None:
                return
            try:
                self._loop.call_soon_threadsafe(
                    self._on_menu_popup, int(event),
                )
            except Exception:  # noqa: BLE001 -- loop may be closing
                pass

        # Hold strong references so the ctypes callbacks are not GC'd while the
        # hooks are live.
        self._foreground_proc_cb = win_event_proc_type(_foreground_proc)
        self._destroy_proc_cb = win_event_proc_type(_destroy_proc)
        self._menu_popup_proc_cb = win_event_proc_type(_menu_popup_proc)

        # PyWNDCLASS attribute assignments and the str-classname args, the proven
        # features/window_mover.py pattern. The pywin32 stub models the PyWNDCLASS
        # attributes as read-only properties and over-narrows the className/atom
        # params, so pyright flags these five calls as reportAttributeAccessIssue
        # / reportArgumentType even though they are correct at runtime.
        # window_mover.py makes the identical calls and carries the SAME five
        # errors unsuppressed (verified: it is not pyright-clean on its WNDCLASS
        # block); we suppress here to hold main.py at zero NEW pyright errors.
        # Precise `pyright: ignore[<rule>]` (not blanket `type: ignore`) so an
        # unrelated future error on any of these lines still surfaces.
        wndclass = win32gui.WNDCLASS()
        wndclass.hInstance = win32api.GetModuleHandle(None)  # pyright: ignore[reportAttributeAccessIssue]
        wndclass.lpszClassName = self._window_class_name  # pyright: ignore[reportAttributeAccessIssue]

        def _wnd_proc(hwnd_param, msg, wparam, lparam):
            if msg == self._WM_REGISTER_DESTROY:
                self._do_register_destroy(ctypes, win32api)
                return 0
            if msg == self._WM_UNREGISTER_DESTROY:
                self._do_unregister_destroy(ctypes)
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
            return win32gui.DefWindowProc(hwnd_param, msg, wparam, lparam)

        wndclass.lpfnWndProc = _wnd_proc  # pyright: ignore[reportAttributeAccessIssue]
        try:
            try:
                win32gui.UnregisterClass(
                    wndclass.lpszClassName, wndclass.hInstance,  # pyright: ignore[reportArgumentType]
                )
            except Exception:  # noqa: BLE001
                pass
            class_atom = win32gui.RegisterClass(wndclass)
            self._message_only_hwnd = win32gui.CreateWindowEx(
                0, class_atom, "OverlayFocusHookMessageOnly", 0, 0, 0, 0, 0,  # pyright: ignore[reportArgumentType]
                win32con.HWND_MESSAGE, None, wndclass.hInstance, None,
            )
            if not self._message_only_hwnd:
                logger.error(
                    "OverlayFocusHookManager: failed to create message-only "
                    "window (error %s).", win32api.GetLastError(),
                )
                # wh-n29v.23.1: RegisterClass already succeeded, so _teardown
                # must run to UnregisterClass it (a bare return leaked the
                # WNDCLASS). _running False before signalling ready makes
                # start() report the failure.
                self._running = False
                self._teardown(ctypes, win32api, win32gui)
                if self._thread_ready is not None:
                    self._thread_ready.set()
                return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "OverlayFocusHookManager: error creating message window: %s",
                exc,
            )
            self._running = False
            self._teardown(ctypes, win32api, win32gui)
            if self._thread_ready is not None:
                self._thread_ready.set()
            return

        self._foreground_hook = ctypes.windll.user32.SetWinEventHook(
            _EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND, 0,
            self._foreground_proc_cb, 0, 0,
            win32con.WINEVENT_OUTOFCONTEXT | win32con.WINEVENT_SKIPOWNPROCESS,
        )
        if not self._foreground_hook:
            # wh-n29v.23.1: the foreground hook is the manager's whole purpose.
            # If it fails, tear down (unhook nothing, destroy the window,
            # unregister the class) and report a failed start instead of
            # entering the pump with a dead hook.
            logger.error(
                "OverlayFocusHookManager: SetWinEventHook(FOREGROUND) failed "
                "(error %s); overlay focus following disabled.",
                win32api.GetLastError(),
            )
            self._running = False
            self._teardown(ctypes, win32api, win32gui)
            if self._thread_ready is not None:
                self._thread_ready.set()
            return

        # wh-overlay-menu-close-stale: register the menu pop-up hook on the
        # exact two-id range (0x0006..0x0007; no other WinEvent id lies
        # between). SKIPOWNPROCESS excludes only THIS (Logic) process, which
        # owns no menus -- WheelHouse's tray and editor menus live in the GUI
        # process, so their events DO reach this hook and rely on the config
        # gate + shared debounce + closed-state no-op for containment
        # (wh-overlay-nested-dupes.1.3). Non-fatal on failure: the foreground
        # hook (the manager's purpose) is already up, so a dead menu hook only
        # degrades the menu-close re-walk back to the pre-fix behaviour.
        if self._on_menu_popup is not None:
            from services.wheelhouse.overlay_focus_hooks import (
                EVENT_SYSTEM_MENUPOPUPEND,
                EVENT_SYSTEM_MENUPOPUPSTART,
            )

            self._menu_popup_hook = ctypes.windll.user32.SetWinEventHook(
                EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUPOPUPEND, 0,
                self._menu_popup_proc_cb, 0, 0,
                win32con.WINEVENT_OUTOFCONTEXT
                | win32con.WINEVENT_SKIPOWNPROCESS,
            )
            if not self._menu_popup_hook:
                logger.warning(
                    "OverlayFocusHookManager: SetWinEventHook(MENUPOPUP) "
                    "failed (error %s); overlay menu-close refresh disabled.",
                    win32api.GetLastError(),
                )

        if self._thread_ready is not None:
            self._thread_ready.set()

        msg = wintypes.MSG()
        while self._running:
            if ctypes.windll.user32.PeekMessageW(
                ctypes.byref(msg), self._message_only_hwnd, 0, 0,
                win32con.PM_REMOVE,
            ):
                if msg.message == win32con.WM_QUIT:
                    break
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
            if not self._running:
                break
            time.sleep(0.02)

        self._teardown(ctypes, win32api, win32gui)

    def _do_register_destroy(self, ctypes, win32api) -> None:
        # Runs on the hook thread (posted via WM_APP). Replace any existing
        # transient hook with one scoped to the pending pid/tid.
        self._do_unregister_destroy(ctypes)
        pid = self._pending_destroy_pid
        tid = self._pending_destroy_tid
        if pid <= 0 or tid <= 0:
            return
        self._destroy_hook = ctypes.windll.user32.SetWinEventHook(
            _EVENT_OBJECT_DESTROY, _EVENT_OBJECT_DESTROY, 0,
            self._destroy_proc_cb, pid, tid, 0,  # WINEVENT_OUTOFCONTEXT == 0
        )
        if not self._destroy_hook:
            logger.warning(
                "OverlayFocusHookManager: SetWinEventHook(DESTROY) failed for "
                "pid=%s tid=%s (error %s).", pid, tid, win32api.GetLastError(),
            )

    def _do_unregister_destroy(self, ctypes) -> None:
        if self._destroy_hook:
            try:
                ctypes.windll.user32.UnhookWinEvent(self._destroy_hook)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "OverlayFocusHookManager: UnhookWinEvent(destroy) error: %s",
                    exc,
                )
            self._destroy_hook = None

    def _teardown(self, ctypes, win32api, win32gui) -> None:
        if self._foreground_hook:
            try:
                ctypes.windll.user32.UnhookWinEvent(self._foreground_hook)
            except Exception:  # noqa: BLE001
                pass
            self._foreground_hook = None
        if self._menu_popup_hook:
            try:
                ctypes.windll.user32.UnhookWinEvent(self._menu_popup_hook)
            except Exception:  # noqa: BLE001
                pass
            self._menu_popup_hook = None
        self._do_unregister_destroy(ctypes)
        if self._message_only_hwnd and win32gui.IsWindow(self._message_only_hwnd):
            try:
                win32gui.DestroyWindow(self._message_only_hwnd)
            except Exception:  # noqa: BLE001
                pass
        self._message_only_hwnd = None
        try:
            win32gui.UnregisterClass(
                self._window_class_name, win32api.GetModuleHandle(None),
            )
        except Exception:  # noqa: BLE001
            pass
        logger.debug("OverlayFocusHookManager: hook thread torn down.")
