"""OverlaySnapshotActions: the overlay walk, the snapshots and click N.

wh-full-refactor.4.3 (stage B of place 2) moved this code out of
UIActionHandler in ui/ui_action_handler.py without a change in behavior. The
stage A boundary tests (tests/test_overlay_stack_requirements_u4.py,
docs/design/overlay-stack-requirements.md, rows IW-n) pin that behavior.
UIActionHandler creates one instance as self.overlay_snapshots and keeps the
six actions as delegators, because input_proc.py calls them by name. This
class reads the UIActionHandler members it does not own through self._host
(see overlay_snapshot_host.py). It runs in the Input process.
"""

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

from . import settle_detector
# Three module globals stay in ui_action_handler.py: _capture_click_foreground,
# _TRANSIENT_WALK_ERRORS and _parse_gesture. click_element also uses the first
# two; _parse_gesture has no other caller. They are read through the module
# object, so a patch of ui_action_handler.<name> (tests/e2e/app_adapter.py, the
# old handler tests) also reaches this code, as it did before the move.
# crewcut: this is a reference to the whole old module. A shared module for
# the three globals removes it (wh-full-refactor.5).
from . import ui_action_handler as _handler_module

if TYPE_CHECKING:
    from .overlay_snapshot_host import OverlaySnapshotHost

# The logger of ui_action_handler.py, by name: the moved code logs exactly the
# records it logged before the move, so log routing and the tests that filter
# on the logger name see no change.
logger = logging.getLogger(__name__.rpartition(".")[0] + ".ui_action_handler")


class OverlaySnapshotActions:
    """The Input side of the numbered overlay for one UIActionHandler."""

    def __init__(
        self, host: "OverlaySnapshotHost", *, clock: Callable[[], float]
    ) -> None:
        """``host`` is the UIActionHandler. ``clock`` returns seconds.

        The moved code reads time only through ``clock``
        (UIActionHandler passes ``time.monotonic``).
        """
        self._host = host
        self._clock = clock

        # wh-n29v.41 / wh-n29v.42.1: Input-side defence-in-depth watermark for
        # pin_snapshot stale-generation rejection. A SINGLE bounded
        # (overlay_session_id, latest_accepted_paint_generation) pair -- NOT a
        # per-session dict, which would add one permanent entry per overlay
        # session for the lifetime of this long-lived process. overlay_session_id
        # is monotonic and the overlay state machine is single, so only the
        # latest session can receive a legitimate pin; an older session id (or a
        # strictly-older generation within the latest session) is stale.
        self._latest_pin_watermark: Optional[tuple[int, int]] = None

        # wh-overlay-slow-uia-stale-badges.8 part 4: one EXECUTED click per
        # (overlay_session_id, paint_generation). The pair of the last click
        # that returned ok; a later click carrying a pair that is not STRICTLY
        # newer is refused (stale_overlay_generation) -- that list was
        # consumed by the click that changed the screen. A single bounded
        # pair, not a per-session dict, for the same reason as
        # _latest_pin_watermark above (wh-n29v.42.1): overlay_session_id is
        # monotonic and the overlay machine is single. Ordering alone cannot
        # give Input a "current" generation (the command loop is FIFO, so an
        # arriving pair is always >= any watermark it could keep from paints),
        # which is why the guard keys on EXECUTION, the one event that
        # invalidates a painted list.
        self._last_executed_click_pair: Optional[tuple[int, int]] = None

    def _settle_overlay_walk(
        self,
        finder,
        foreground,
        *,
        walk_deadline: Optional[float],
        compare_snapshot_id: str,
        trace_id: str,
    ):
        """Read the window until it settles, then compare it with the held list.

        Returns ``(walk, held_summary)``. ``held_summary`` is the stored
        summary of ``compare_snapshot_id`` when the settled read matches it --
        the caller reads "unchanged" from that being non-None -- and ``None``
        every other time, including every failure.

        The read callable performs a full ``overlay_walk`` each time rather
        than a bare tree walk, because the comparison has to be made against
        the SAME processed list the badges are drawn from: the browser DOM
        fold, the owned-popup fold, the taskbar walk and the contiguous
        renumber all run inside ``overlay_walk``, and a raw walk would differ
        from the held snapshot on every read.

        crewcut: two limits are accepted here, both removable later.
        (1) Every read this makes is bounded by the ONE dequeue-anchored
        screen-read deadline this request was given -- ``[click]
        screen_read_timeout_ms`` minus the 250 ms pre-walk margin, 9750 ms at
        the shipped default -- because all reads share that single deadline
        rather than each getting its own. On a slow accessibility provider a
        read can still exceed the bound and produce no list, which ends the
        settle early: the answer is then the last read that DID complete,
        unconfirmed by a matching pair. Removing this means giving each read
        its own budget, which needs a second configured number and a rule for
        how many reads the settle may spend.
        (2) Each read stores its own snapshot, so a settle leaves two or three
        extra UNPINNED entries in the finder's store (capacity 4, TTL 30 s).
        They cannot displace the held snapshot, which is pinned and so immune
        to LRU eviction, and the last insert protects itself. Removing this
        means splitting ``overlay_walk`` into a walk half and a store half,
        which is about 300 lines of shipped fold and renumber logic that three
        other paths share; not worth it for entries the store already reclaims.
        """

        class _SettleWalkFailed(Exception):
            """A read could not produce a list; abort the detector."""

            def __init__(self, walk):
                super().__init__("settle read failed")
                self.walk = walk

        last: list = []

        def _read():
            result = finder.overlay_walk(foreground, deadline=walk_deadline)
            if result.snapshot is None:
                # execution_failed: no list at all. The detector only catches
                # stale-window errors, so anything else propagates -- which is
                # exactly the abort this needs.
                raise _SettleWalkFailed(result)
            last.append(result)
            return result.snapshot.matches

        listener = settle_detector.StructureEventListener(
            foreground.foreground_window,
            now_ms=lambda: self._clock() * 1000.0,
        )
        available = False
        try:
            available = listener.start()
        except Exception:  # noqa: BLE001 - the listener is best-effort
            logger.warning(
                "start_overlay_walk: settle listener failed to start; "
                "degrading to plain two matching reads (trace_id=%s)",
                trace_id, exc_info=True,
            )
        settled = None
        try:
            settled = settle_detector.wait_for_settled_window(
                _read,
                events_between=(
                    listener.any_event_between if available else None
                ),
                now_ms=lambda: self._clock() * 1000.0,
            )
        except _SettleWalkFailed as failed:
            if not last:
                # Nothing completed, so there is no list to paint and nothing
                # to compare. Hand the failed result back and let the
                # handler's existing execution_failed branch answer.
                return failed.walk, None
            # A later read produced no list, but an earlier one did. Answer
            # with that one rather than with nothing: it is a post-click read,
            # so it is no worse than the single walk this path replaced, and
            # the comparison below still runs -- when it matches the held
            # snapshot the answer is the numbers already on screen, which is
            # the safest outcome available.
            logger.info(
                "start_overlay_walk: a settle read failed (%s) after %d "
                "completed reads; answering with the last completed read "
                "(trace_id=%s)",
                failed.walk.reason, len(last), trace_id,
            )
        finally:
            # stop() unconditionally, not only when start() reported
            # success: a start that registered some handlers and then raised
            # leaves live COM registrations, and stop() is a no-op when there
            # is nothing to release.
            try:
                listener.stop()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "start_overlay_walk: settle listener failed to stop "
                    "(trace_id=%s)", trace_id, exc_info=True,
                )

        walk = last[-1]
        if settled is not None:
            logger.info(
                "start_overlay_walk: settled=%s after %d reads "
                "(%d voided pairs, %.0f ms, trace_id=%s)",
                settled.settled, settled.read_count, settled.voided_pairs,
                settled.elapsed_ms, trace_id,
            )
        if not compare_snapshot_id or walk.snapshot is None:
            return walk, None
        # wh-overlay-slow-uia-stale-badges.2.2.2: ask for the held list AS THE
        # WINDOW THIS READ RAN AGAINST. ``signature_of`` compares control type
        # id, accessible name and bounding rectangle and carries no window
        # identity, so two windows with the same layout compare as "unchanged"
        # -- a dialog closed and reopened is the ordinary way there. Answering
        # with the held id would then paint the old window's badges over the
        # new one, and the machine's unchanged branch repaints WITHOUT
        # re-pinning, so nothing would rebind the list to the window on screen.
        # ``get_snapshot`` drops the entry and returns None on an identity
        # mismatch, which falls into the held-is-gone path just below and
        # answers with the fresh read; Logic then unpins, pins the new list and
        # paints it. The two other production consumers, show_numbered_overlay
        # and click_snapshot_item, already pass these four fields.
        held = finder.get_snapshot(
            compare_snapshot_id,
            current_foreground_window=foreground.foreground_window,
            current_foreground_pid=foreground.foreground_pid,
            current_foreground_process_name=(
                foreground.foreground_process_name
            ),
            current_foreground_window_creation_time=(
                foreground.foreground_window_creation_time
            ),
        )
        if held is None:
            # The held snapshot expired (TTL) or was unpinned and evicted
            # while the window settled. There is nothing to compare against,
            # so the fresh read is the answer.
            logger.info(
                "start_overlay_walk: the held snapshot %s is gone; "
                "answering with the fresh read (trace_id=%s)",
                compare_snapshot_id, trace_id,
            )
            return walk, None
        if settle_detector.signature_of(
            held.matches
        ) != settle_detector.signature_of(walk.snapshot.matches):
            return walk, None
        held_summary = finder.get_summary(compare_snapshot_id)
        if held_summary is None:
            # The lists match but the summary is gone, so the id cannot be
            # answered with. Fall back to the fresh read rather than send an
            # id with no list behind it.
            return walk, None
        return walk, held_summary

    def start_overlay_walk(
        self,
        scope: str = "focused_window",
        overlay_session_id: int = 0,
        paint_generation: int = 0,
        trace_id: str = "",
        request_id: Optional[str] = None,
        command_dequeue_monotonic: Optional[float] = None,
        settle: bool = False,
        compare_snapshot_id: str = "",
        **kwargs,
    ) -> None:
        """Walk the focused window from scratch for the numbered overlay (wh-n29v.37).

        ``start_overlay_walk`` is the standalone "show numbers" build request:
        Logic dispatches it when the overlay state machine needs a FRESH walk
        of the focused window (no prior ``click_element`` request to reuse).
        The Input process owns the walk:

          1. Gate on the validated overlay config: short-circuit (no walk) when
             ``overlay_enabled_effective`` is False -- the same two-sided gate
             the by-name click path uses (Logic also gates, but the Input side
             defends against a stale/racing request). Defence-in-depth.
          2. Snapshot the foreground identity + cursor.
          3. ``ElementFinder.overlay_walk(foreground)`` walks the focused window
             and numbers EVERY interactive control 1..K (no clear-winner rule).
          4. Map the walk outcome to the schema outcome (ok / no_targets /
             execution_failed) and emit exactly one ``StartOverlayWalkResponse``
             on the response queue, echoing ``overlay_session_id`` +
             ``paint_generation`` + ``trace_id`` so Logic can drop a superseded
             walk's response by generation.

        ``settle=True`` (wh-overlay-slow-uia-stale-badges.2) changes step 3
        only: instead of one walk, the window is read through
        ``ui.settle_detector.wait_for_settled_window`` until two consecutive
        reads agree, and the settled read is then compared against
        ``compare_snapshot_id`` -- the snapshot Logic pinned before the click.
        When the two comparison lists are equal the response carries THAT id
        and THAT summary, which is how Logic learns to restore the numbers it
        already had; when they differ the fresh snapshot is the answer, exactly
        as for a plain walk. Ids are never reused (``walk-<uuid4 hex>-<n>``
        with a per-run salt and a monotonic counter), so the held id in a
        response can only mean "unchanged".

        The handler is in ``_HANDLES_OWN_RESPONSE`` so the generic emitter does
        not clobber the walk outcome. It NEVER raises: any unexpected error is
        mapped to a ``status="error"`` / ``outcome="error"`` response so the
        one-response-per-request_id contract holds and the Logic awaiter does
        not fall through to its timeout path. The status/outcome split follows
        the documented r2.10 mapping: a feature failure (no_targets /
        execution_failed) rides transport ``status="ok"``; ``status="error"``
        is reserved for a handler-level crash where the outcome is unreliable.
        """
        from services.wheelhouse.shared.start_overlay_walk import (
            StartOverlayWalkResponse,
        )

        action_name = "start_overlay_walk"

        # wh-overlay-slow-uia-stale-badges.2.2.1: the reply names the window
        # this handler ACTUALLY READ, so Logic can refuse to paint a list over
        # a window it does not describe. Filled in from the ONE
        # ``ForegroundContext`` captured below -- the same context every settle
        # read walks against -- so it is the read's window, not whatever is in
        # front when the reply is parsed. It starts at the same per-field
        # sentinels ``_capture_click_foreground`` degrades to, which is what a
        # short-circuit emitted BEFORE the capture (automation unavailable,
        # overlay disabled) sends; a sentinel identity matches no real
        # foreground, so Logic fails closed on it.
        read_identity: dict = {
            "foreground_window": 0,
            "foreground_pid": 0,
            "foreground_process_name": "",
            "foreground_window_creation_time": 0,
        }

        def _emit(response: "StartOverlayWalkResponse") -> None:
            payload = response.to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self._host.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                # Last-resort: log and drop. Without a response the Logic-side
                # Future times out, which Logic already handles gracefully.
                logger.error(
                    "start_overlay_walk: failed to enqueue response "
                    "(trace_id=%s): %s",
                    trace_id, exc,
                )

        def _failed(outcome: str, reason: Optional[str], status: str = "ok",
                    snapshot_id: Optional[str] = None,
                    snapshot_summary=None) -> "StartOverlayWalkResponse":
            return StartOverlayWalkResponse(
                status=status,
                outcome=outcome,
                reason=reason,
                snapshot_id=snapshot_id,
                snapshot_summary=snapshot_summary,
                trace_id=trace_id,
                overlay_session_id=overlay_session_id,
                paint_generation=paint_generation,
                **read_identity,
            )

        try:
            finder = self._get_overlay_walk_finder()
            if finder is None:
                # finder is None either because the COM root could not be built
                # on this host (the _AUTOMATION_UNAVAILABLE sentinel, set by the
                # host's get_click_element_finder) or because the overlay is
                # genuinely disabled in config (operator opt-out, an overlay-key
                # validation failure, or the whole feature off). Emit the
                # matching tag so the notice is accurate (wh-n29v.74.1, deepseek
                # reviewer_2): clicking IS enabled in config on the
                # COM-unavailable path, so "disabled_by_config" would wrongly
                # point the user at config.toml. Logic also short-circuits
                # before sending; this defends the Input side against a stale or
                # racing request.
                if self._host.click_automation_unavailable():
                    logger.info(
                        "start_overlay_walk: IUIAutomation root unavailable on "
                        "this host; short-circuiting (trace_id=%s)", trace_id,
                    )
                    _emit(_failed("execution_failed", "automation_unavailable"))
                    return
                logger.info(
                    "start_overlay_walk: overlay disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                _emit(_failed("execution_failed", "disabled_by_config"))
                return

            # reviewer_0 finding 38.2: anchor the ONE per-request walk deadline
            # at the dequeue instant the input_proc command reader captured
            # (threaded in as command_dequeue_monotonic), exactly as
            # click_element does (wh-9f3t.73.1). Charging from that earliest
            # reader instant -- not this handler's entry -- folds the ~1s
            # pre-handler reader stall into the budget so the walk gives up
            # before the Logic walk_in_flight timeout rather than after it.
            # The fallback (a direct call with no anchor, e.g. a unit test) uses
            # this handler's entry instant. The bound is the SCREEN READ's own
            # one, ClickConfig.screen_read_walk_deadline_ms -- [click]
            # screen_read_timeout_ms minus the pre-walk margin, 9750 ms at the
            # shipped default -- NOT walk_deadline_ms, which bounds the by-name
            # click walk and is still validated strictly below the click reply
            # limit (wh-overlay-slow-uia-stale-badges.3). Reading it here is
            # what lets a read that answers correctly after 2500 ms be used
            # instead of discarded. It comes from the validated click_config
            # (set as a side effect of the finder build); None when no bound is
            # configured (defensive -- finder is non-None here). overlay_walk
            # already accepts deadline= and threads it into walk_window
            # unchanged.
            dequeue_monotonic = (
                command_dequeue_monotonic
                if command_dequeue_monotonic is not None
                else self._clock()
            )
            click_cfg = self._host.click_config
            walk_deadline_ms = (
                click_cfg.screen_read_walk_deadline_ms
                if click_cfg is not None
                else None
            )
            walk_deadline: Optional[float] = (
                dequeue_monotonic + (walk_deadline_ms / 1000.0)
                if walk_deadline_ms is not None
                else None
            )

            foreground = _handler_module._capture_click_foreground()
            # Record the window every read below runs against, so the reply
            # carries it (wh-overlay-slow-uia-stale-badges.2.2.1). This is the
            # ONE capture the settle loop reuses for every read, so it names
            # the window the answer describes even when the foreground moves
            # while the window settles.
            read_identity.update(
                foreground_window=foreground.foreground_window,
                foreground_pid=foreground.foreground_pid,
                foreground_process_name=foreground.foreground_process_name,
                foreground_window_creation_time=(
                    foreground.foreground_window_creation_time
                ),
            )
            logger.info(
                "start_overlay_walk: %s focused window in process=%s "
                "(session=%s gen=%s settle=%s trace_id=%s)",
                "settling" if settle else "walking",
                foreground.foreground_process_name,
                overlay_session_id, paint_generation, settle, trace_id,
            )
            unchanged = False
            held_summary = None
            if settle:
                walk, held_summary = self._settle_overlay_walk(
                    finder,
                    foreground,
                    walk_deadline=walk_deadline,
                    compare_snapshot_id=compare_snapshot_id,
                    trace_id=trace_id,
                )
                unchanged = held_summary is not None
            else:
                walk = finder.overlay_walk(foreground, deadline=walk_deadline)

            if walk.outcome == "execution_failed":
                logger.info(
                    "start_overlay_walk: execution_failed reason=%s "
                    "(trace_id=%s)", walk.reason, trace_id,
                )
                _emit(_failed(
                    "execution_failed",
                    walk.reason or "walk_failed",
                ))
                return

            snapshot_id = (
                walk.snapshot.snapshot_id if walk.snapshot is not None else None
            )
            summary = walk.summary
            if unchanged:
                # Nothing changed since the click. Answer with the HELD
                # snapshot and its OWN summary: a summary names the snapshot
                # it was projected from, and StartOverlayWalkResponse REFUSES
                # a response whose snapshot_summary.snapshot_id differs from
                # its top-level snapshot_id (shared/start_overlay_walk.py:266)
                # because Logic keys the retained summary by the top-level id.
                # The fresh walk's summary names the fresh snapshot, so that
                # pairing would be rejected at the process boundary and the
                # whole response lost. _settle_overlay_walk only reports
                # unchanged when it has the held summary in hand.
                snapshot_id = compare_snapshot_id
                summary = held_summary
            if walk.outcome == "no_targets":
                logger.info(
                    "start_overlay_walk: no_targets (trace_id=%s)", trace_id,
                )
                _emit(StartOverlayWalkResponse(
                    status="ok",
                    outcome="no_targets",
                    reason=None,
                    snapshot_id=snapshot_id,
                    snapshot_summary=summary,
                    trace_id=trace_id,
                    overlay_session_id=overlay_session_id,
                    paint_generation=paint_generation,
                    **read_identity,
                ))
                return

            # outcome == "ok".
            item_count = len(summary.items) if summary is not None else 0
            logger.info(
                "start_overlay_walk: ok with %d items (snapshot=%s "
                "unchanged=%s trace_id=%s)",
                item_count, snapshot_id, unchanged, trace_id,
            )
            _emit(StartOverlayWalkResponse(
                status="ok",
                outcome="ok",
                reason=None,
                snapshot_id=snapshot_id,
                snapshot_summary=summary,
                trace_id=trace_id,
                overlay_session_id=overlay_session_id,
                paint_generation=paint_generation,
                **read_identity,
            ))
        except _handler_module._TRANSIENT_WALK_ERRORS as exc:
            # Known transient class (rebuilding window, e.g. a theme switch):
            # a feature failure on the normal execution_failed path, logged at
            # WARNING so no error notification pops for a self-healing blip.
            logger.warning(
                "start_overlay_walk: transient UIA/COM walk error, likely a "
                "rebuilding window; mapping to execution_failed "
                "(trace_id=%s): %r",
                trace_id, exc,
            )
            _emit(_failed("execution_failed", "transient_com_error"))
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "start_overlay_walk: unexpected error (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            _emit(_failed("error", "unexpected_error", status="error"))

    def _get_overlay_walk_finder(self):
        """Return the ElementFinder for the numbered overlay, or None if off.

        The overlay gates on ``ClickConfig.overlay_enabled_effective`` (NOT just
        ``enabled``): a bad overlay key disables ONLY the overlay while by-name
        click stays operative, and a valid ``overlay_enabled=false`` is an
        operator opt-out. Reuses ``get_click_element_finder`` to build (and
        memoise) the same finder + validated ``click_config`` -- the overlay
        and by-name click share one ElementFinder over one ``[click]`` block --
        and then applies the overlay-specific gate on top.

        Returns the cached finder when the overlay is effectively enabled, else
        ``None`` so the handler short-circuits before any walk.
        """
        finder = self._host.get_click_element_finder()
        # get_click_element_finder set the host's click_config as a side
        # effect (even on its None return path, via the lazy-config guard).
        click_cfg = self._host.click_config
        if click_cfg is None or not click_cfg.overlay_enabled_effective:
            return None
        # When by-name click is disabled but the overlay somehow validated,
        # get_click_element_finder returns None; overlay_enabled_effective is
        # False in that case too (the disabled path sets overlay_enabled=False),
        # so the guard above already returned None. finder is non-None here.
        return finder

    def pin_snapshot(
        self,
        overlay_session_id: int = 0,
        snapshot_id: str = "",
        paint_generation: int = 0,
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Pin a stored snapshot so LRU eviction skips it (wh-n29v.41).

        Logic owns the active-overlay pin and dispatches ``pin_snapshot`` when
        it dispatches the ``paint_overlay`` that displays a snapshot. The Input
        process drives the multi-snapshot store's :meth:`ElementFinder.pin`
        (skips LRU eviction; the pin is still TTL-bounded). Logic does NOT block
        the paint on this ack, but the handler still emits exactly one
        ``PinSnapshotResponse`` so the Logic-side awaiting Future resolves.

        Stale rejection (the design point, r1c.2 / r1c.1). The store keys
        snapshots by ``snapshot_id`` ONLY and tracks no generation; Logic owns
        the authoritative generation comparison and drops a superseded WALK
        response before it would ever dispatch a pin. As Input-side
        defence-in-depth, this handler keeps a SINGLE bounded watermark
        ``(latest_session_id, latest_accepted_paint_generation)`` (not a
        per-session dict, which would grow one permanent entry per overlay
        session over the process lifetime -- wh-n29v.42.1). ``overlay_session_id``
        is monotonic (allocated when the overlay state machine leaves
        ``closed``) and the state machine is single, so a pin is rejected
        WITHOUT touching the store when EITHER its ``overlay_session_id`` is
        OLDER than the latest seen (a superseded session Logic has torn down)
        OR, within the latest session, its ``paint_generation`` is STRICTLY
        OLDER than the latest accepted one (a racing or duplicated out-of-order
        dispatch). Binding a stale pin could pin the wrong snapshot against a
        session/generation Logic has already advanced. Equal-or-newer
        generations within the latest session and any newer session are
        accepted (an equal generation is the legitimate re-dispatch of the same
        paint; r2.5 / line 366 of the v4 design).

        The watermark advances on ANY accepted (non-stale) dispatch BEFORE the
        store pin, NOT only on a successful pin (wh-n29v.42.2). A failed pin --
        the snapshot was already TTL-evicted -- does not make its generation
        stale, so it must still advance "latest seen"; otherwise a later,
        strictly-older generation for the same session would slip through the
        guard.

        The handler is in ``_HANDLES_OWN_RESPONSE`` so the generic emitter does
        not clobber the ack. It NEVER raises: any unexpected error is mapped to
        a ``status="error"`` response so the one-response-per-request_id
        contract holds and the Logic awaiter does not fall through to its
        timeout path.
        """
        from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

        action_name = "pin_snapshot"

        def _emit(
            *, status: str, reason: Optional[str], pinned: bool,
        ) -> None:
            # Coerce the echoed identity to schema-safe primitives so the
            # response always parses on the Logic side, even when this handler
            # is rejecting malformed input (wh-n29v.43.1). PinSnapshotResponse
            # .from_dict requires overlay_session_id to be a non-bool int and
            # snapshot_id to be a str; a malformed echo would otherwise raise
            # PinSnapshotResponseSchemaError on Logic instead of resolving the
            # awaiting Future cleanly.
            safe_session = (
                overlay_session_id
                if isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                else 0
            )
            safe_snapshot = snapshot_id if isinstance(snapshot_id, str) else ""
            payload = PinSnapshotResponse(
                status=status,
                reason=reason,
                overlay_session_id=safe_session,
                snapshot_id=safe_snapshot,
                pinned=pinned,
            ).to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self._host.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                # Last-resort: log and drop. Without a response the Logic-side
                # Future times out, which Logic already handles gracefully.
                logger.error(
                    "pin_snapshot: failed to enqueue response "
                    "(session=%s snapshot=%s): %s",
                    overlay_session_id, snapshot_id, exc,
                )

        try:
            # Validate IPC fields before touching the store or the watermark
            # (wh-n29v.43.1). Logic constructs these, but a malformed message
            # (wrong types from a Logic bug or corruption) must NOT reach the
            # single-pair watermark: a non-int written there makes every later
            # valid pin raise on the comparison (e.g. 5 < "bad") and return
            # status=error until the Input process restarts. overlay_session_id
            # and paint_generation must be non-bool ints (bool is an int
            # subclass; an overlay session / generation is a real count) and
            # snapshot_id must be a str. Reject without store or watermark
            # mutation, emitting exactly one response.
            if (
                not isinstance(overlay_session_id, int)
                or isinstance(overlay_session_id, bool)
                or not isinstance(paint_generation, int)
                or isinstance(paint_generation, bool)
                or not isinstance(snapshot_id, str)
            ):
                logger.error(
                    "pin_snapshot: invalid request fields; rejecting without "
                    "touching store or watermark "
                    "(session=%r snapshot=%r gen=%r)",
                    overlay_session_id, snapshot_id, paint_generation,
                )
                _emit(status="error", reason="invalid_request", pinned=False)
                return

            finder = self._get_overlay_walk_finder()
            if finder is None:
                # Overlay disabled by config (operator opt-out, an overlay-key
                # validation failure, or the whole feature disabled). Logic
                # gates before sending; defend the Input side too.
                logger.info(
                    "pin_snapshot: overlay disabled by config; "
                    "short-circuiting (session=%s snapshot=%s)",
                    overlay_session_id, snapshot_id,
                )
                _emit(status="ok", reason="disabled_by_config", pinned=False)
                return

            # Single-pair stale watermark (wh-n29v.42.1 / wh-n29v.42.2). Track
            # ONE (latest_session_id, latest_accepted_generation) pair, not a
            # per-session dict. overlay_session_id is monotonic and the overlay
            # state machine is single, so two staleness cases are rejected:
            #   * an OLDER overlay_session_id is from a superseded session that
            #     Logic has already torn down; and
            #   * within the latest session, a STRICTLY-OLDER paint_generation
            #     is a racing or duplicated out-of-order dispatch.
            # Equal-or-newer (same session) and any newer session are accepted.
            watermark = getattr(self, "_latest_pin_watermark", None)
            if watermark is not None:
                latest_session, latest_gen = watermark
                if overlay_session_id < latest_session:
                    logger.info(
                        "pin_snapshot: stale session rejected "
                        "(session=%s snapshot=%s < latest_session=%s)",
                        overlay_session_id, snapshot_id, latest_session,
                    )
                    _emit(status="ok", reason="stale_session", pinned=False)
                    return
                if (overlay_session_id == latest_session
                        and paint_generation < latest_gen):
                    logger.info(
                        "pin_snapshot: stale generation rejected "
                        "(session=%s snapshot=%s gen=%s < accepted=%s)",
                        overlay_session_id, snapshot_id, paint_generation,
                        latest_gen,
                    )
                    _emit(status="ok", reason="stale_generation", pinned=False)
                    return

            # Accepted (non-stale): advance the watermark BEFORE the store pin
            # so a FAILED pin (snapshot already TTL-evicted) still advances
            # "latest seen". The snapshot being absent does not make the
            # generation stale -- a later strictly-older generation must still
            # be rejected (wh-n29v.42.2). A newer session resets the generation
            # watermark to this dispatch's generation.
            if (watermark is not None
                    and overlay_session_id == watermark[0]):
                new_gen = max(watermark[1], paint_generation)
            else:
                new_gen = paint_generation
            self._latest_pin_watermark = (overlay_session_id, new_gen)

            pinned = finder.pin(snapshot_id)
            if pinned:
                logger.info(
                    "pin_snapshot: pinned (session=%s snapshot=%s gen=%s)",
                    overlay_session_id, snapshot_id, paint_generation,
                )
                _emit(status="ok", reason=None, pinned=True)
            else:
                logger.info(
                    "pin_snapshot: unknown snapshot (session=%s snapshot=%s)",
                    overlay_session_id, snapshot_id,
                )
                _emit(status="ok", reason="unknown_snapshot", pinned=False)
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "pin_snapshot: unexpected error (session=%s snapshot=%s): %s",
                overlay_session_id, snapshot_id, exc, exc_info=True,
            )
            _emit(status="error", reason="unexpected_error", pinned=False)

    def unpin_snapshot(
        self,
        overlay_session_id: int = 0,
        snapshot_id: str = "",
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Clear a stored snapshot's pin (wh-n29v.41).

        ``unpin_snapshot`` is clear-by-identity: it carries
        ``(overlay_session_id, snapshot_id)`` only and needs NO generation
        check. Clearing a pin only relaxes LRU immunity, so it is always safe
        to apply regardless of generation -- unlike ``pin_snapshot``, which
        could bind the wrong snapshot against a session Logic has advanced.
        The handler drives the multi-snapshot store's
        :meth:`ElementFinder.unpin` and emits exactly one ``PinSnapshotResponse``
        with ``pinned=False`` so the Logic-side awaiting Future resolves.

        The handler validates its IPC field types up front and is in
        ``_HANDLES_OWN_RESPONSE``. It is never-raise: a malformed message is
        rejected with ``status="error" reason="invalid_request"`` and any
        unexpected error maps to a ``status="error"`` response. Exactly one
        response is emitted per ``request_id`` (the single exception is a dead
        response queue, where the enqueue itself fails -- see the module
        docstring of ``shared/pin_snapshot.py``).
        """
        from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

        action_name = "unpin_snapshot"

        def _emit(*, status: str, reason: Optional[str]) -> None:
            # Coerce the echoed identity to schema-safe primitives so the
            # response always parses on the Logic side, even when this handler
            # is rejecting malformed input (wh-n29v.44.1, mirroring
            # pin_snapshot). PinSnapshotResponse.from_dict requires
            # overlay_session_id to be a non-bool int and snapshot_id to be a
            # str; a malformed echo would otherwise raise
            # PinSnapshotResponseSchemaError on Logic instead of resolving the
            # awaiting Future cleanly.
            safe_session = (
                overlay_session_id
                if isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                else 0
            )
            safe_snapshot = snapshot_id if isinstance(snapshot_id, str) else ""
            payload = PinSnapshotResponse(
                status=status,
                reason=reason,
                overlay_session_id=safe_session,
                snapshot_id=safe_snapshot,
                # An unpin's resulting pin state is always "not pinned".
                pinned=False,
            ).to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self._host.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "unpin_snapshot: failed to enqueue response "
                    "(session=%s snapshot=%s): %s",
                    overlay_session_id, snapshot_id, exc,
                )

        try:
            # Validate IPC fields before touching the store (wh-n29v.44.1).
            # unpin has no watermark to poison (unlike pin_snapshot), but a
            # malformed (overlay_session_id, snapshot_id) echo would make the
            # Logic-side from_dict raise instead of resolving the awaiting
            # Future. Mirror pin_snapshot: reject malformed input without
            # touching the store, emitting exactly one response.
            # overlay_session_id must be a non-bool int (bool is an int
            # subclass; an overlay session is a real count) and snapshot_id a
            # str. unpin carries no paint_generation, so only two fields.
            if (
                not isinstance(overlay_session_id, int)
                or isinstance(overlay_session_id, bool)
                or not isinstance(snapshot_id, str)
            ):
                logger.error(
                    "unpin_snapshot: invalid request fields; rejecting without "
                    "touching store (session=%r snapshot=%r)",
                    overlay_session_id, snapshot_id,
                )
                _emit(status="error", reason="invalid_request")
                return

            finder = self._get_overlay_walk_finder()
            if finder is None:
                logger.info(
                    "unpin_snapshot: overlay disabled by config; "
                    "short-circuiting (session=%s snapshot=%s)",
                    overlay_session_id, snapshot_id,
                )
                _emit(status="ok", reason="disabled_by_config")
                return

            present = finder.unpin(snapshot_id)
            logger.info(
                "unpin_snapshot: %s (session=%s snapshot=%s)",
                "cleared" if present else "unknown",
                overlay_session_id, snapshot_id,
            )
            _emit(
                status="ok",
                reason=None if present else "unknown_snapshot",
            )
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "unpin_snapshot: unexpected error (session=%s snapshot=%s): %s",
                overlay_session_id, snapshot_id, exc, exc_info=True,
            )
            _emit(status="error", reason="unexpected_error")

    def refresh_overlay_snapshot(
        self,
        overlay_session_id: int = 0,
        snapshot_id: str = "",
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Slide the Input store's TTL for the still-RETAINED pinned snapshot.

        The Input side of the Logic-side 15-second overlay keepalive
        (wh-overlay-snapshot-keepalive). Logic sends this every keepalive tick
        for the snapshot it still holds pinned; the handler slides that
        snapshot's TTL anchor via
        :meth:`ElementFinder.refresh_snapshot_ttl` so a numbered overlay left on
        screen past the TTL stays clickable. Without it the Logic resolver cache
        and the Input store expire independently and "click N" fails with
        ``snapshot_expired`` on a still-visible overlay.

        The badges are not always on screen when this arrives. Logic also sends
        it while its overlay is PAUSED or POST_CLICK_SETTLING, which clear the
        badges but keep the pin -- for a repaint on resume, or for the
        post-click comparison. See ``_overlay_keepalive_states`` in main.py
        (wh-overlay-slow-uia-stale-badges.13.6).

        Logic does NOT block on the ack, but the handler emits exactly one
        ``PinSnapshotResponse`` (reused as the small Schema-A ack) so the
        Logic-side awaiting Future resolves. The ``pinned`` field echoes whether
        the store found and refreshed the snapshot.

        Unlike ``pin_snapshot`` there is NO stale-generation watermark: a refresh
        carries no generation, and refreshing a superseded snapshot's TTL briefly
        is harmless (it is unpinned and aged out normally). The handler is in
        ``_HANDLES_OWN_RESPONSE`` so the generic emitter does not clobber the
        ack. It NEVER raises: any unexpected error maps to ``status="error"``.
        """
        from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

        action_name = "refresh_overlay_snapshot"

        def _emit(*, status: str, reason: Optional[str], pinned: bool) -> None:
            # Coerce the echoed identity to schema-safe primitives so the
            # response always parses on the Logic side (mirrors pin_snapshot).
            safe_session = (
                overlay_session_id
                if isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                else 0
            )
            safe_snapshot = snapshot_id if isinstance(snapshot_id, str) else ""
            payload = PinSnapshotResponse(
                status=status,
                reason=reason,
                overlay_session_id=safe_session,
                snapshot_id=safe_snapshot,
                pinned=pinned,
            ).to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self._host.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "refresh_overlay_snapshot: failed to enqueue response "
                    "(snapshot=%s): %s",
                    snapshot_id, exc,
                )

        try:
            if not isinstance(snapshot_id, str):
                logger.error(
                    "refresh_overlay_snapshot: invalid snapshot_id; rejecting "
                    "without touching the store (snapshot=%r)", snapshot_id,
                )
                _emit(status="error", reason="invalid_request", pinned=False)
                return

            finder = self._get_overlay_walk_finder()
            if finder is None:
                logger.info(
                    "refresh_overlay_snapshot: overlay disabled by config; "
                    "short-circuiting (snapshot=%s)", snapshot_id,
                )
                _emit(status="ok", reason="disabled_by_config", pinned=False)
                return

            refreshed = finder.refresh_snapshot_ttl(snapshot_id)
            _emit(
                status="ok",
                reason=None if refreshed else "unknown_snapshot",
                pinned=refreshed,
            )
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "refresh_overlay_snapshot: unexpected error (snapshot=%s): %s",
                snapshot_id, exc, exc_info=True,
            )
            _emit(status="error", reason="unexpected_error", pinned=False)

    def show_numbered_overlay(
        self,
        snapshot_id: str = "",
        item_id_filter: Optional[list[str]] = None,
        overlay_session_id: int = 0,
        paint_generation: int = 0,
        trace_id: str = "",
        request_id: Optional[str] = None,
        command_dequeue_monotonic: Optional[float] = None,
        **kwargs,
    ) -> None:
        """Re-paint an EXISTING walk snapshot for the numbered overlay (wh-n29v.83).

        Unlike the sibling ``start_overlay_walk`` (which walks the focused
        window FROM SCRATCH), ``show_numbered_overlay`` LOOKS UP a snapshot the
        multi-snapshot store already holds and re-paints it. Logic dispatches
        it to re-display a snapshot it retained -- e.g. an auto-open after an
        ambiguous by-name click, restricting the painted set to the ambiguous
        finalists via ``item_id_filter``. The Input process owns the lookup:

          1. Gate on the validated overlay config exactly as
             ``start_overlay_walk`` does, via ``_get_overlay_walk_finder()``:
             short-circuit (no lookup) to ``outcome="execution_failed"`` with
             reason ``automation_unavailable`` when the COM root is the
             ``_AUTOMATION_UNAVAILABLE`` sentinel, or ``disabled_by_config``
             when the overlay is genuinely off. Defence-in-depth -- Logic gates
             too, but the Input side defends against a stale/racing request.
          2. Snapshot the foreground identity + cursor.
          3. ``ElementFinder.get_snapshot(snapshot_id, ...)`` resolves the id
             with the captured foreground identity. A ``None`` return -- stale
             id, TTL-swept, LRU-evicted, or foreground-identity mismatch -- is
             the ``snapshot_expired`` signal.
          4. On a hit, ``ElementFinder._build_summary`` projects the snapshot to
             a display summary, then ``filter_and_renumber_summary`` keeps only
             ``item_id_filter`` items (when supplied) and renumbers the kept set
             1..K in reading order so the badges are contiguous from 1.
          5. An empty post-filter item set is ``no_targets`` (carrying an
             empty-items summary, never a populated one). A non-empty set is
             ``outcome="ok"`` with the rebuilt summary and the snapshot id; the
             summary names the SAME snapshot id (cross-field rule (c)).

        Every response echoes ``overlay_session_id`` + ``paint_generation`` +
        ``trace_id`` verbatim so Logic can drop a superseded paint by
        generation. The handler is in ``_HANDLES_OWN_RESPONSE`` so the generic
        emitter does not clobber the outcome. It NEVER raises: any unexpected
        error is mapped to ``status="error"`` / ``outcome="error"`` so the
        one-response-per-request_id contract holds and the Logic awaiter does
        not fall through to its timeout path. The status/outcome split follows
        the documented r2.10 mapping: a feature failure (snapshot_expired /
        no_targets / execution_failed) rides transport ``status="ok"``;
        ``status="error"`` is reserved for a handler-level crash where the
        outcome is unreliable. The real handler NEVER emits
        ``status="not_implemented"`` (the stub-only parse literal).

        Emits exactly one ShowNumberedOverlayResponse Schema A response on the
        response queue, augmented with ``request_id`` and ``action`` so the
        demuxer in ``app.py`` can resolve the awaiting Future.
        """
        from services.wheelhouse.shared.show_numbered_overlay import (
            ShowNumberedOverlayResponse,
        )
        from ui.element_finder import (
            ElementFinder,
            collapse_near_identical_containers,
        )

        action_name = "show_numbered_overlay"

        def _emit(response: "ShowNumberedOverlayResponse") -> None:
            payload = response.to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self._host.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                # Last-resort: log and drop. Without a response the Logic-side
                # Future times out, which Logic already handles gracefully.
                logger.error(
                    "show_numbered_overlay: failed to enqueue response "
                    "(trace_id=%s snapshot=%s): %s",
                    trace_id, snapshot_id, exc,
                )

        def _build(
            outcome: str,
            reason: Optional[str],
            *,
            status: str = "ok",
            snapshot_id_out: Optional[str] = None,
            snapshot_summary=None,
        ) -> "ShowNumberedOverlayResponse":
            # Coerce the echoed scalars to schema-safe primitives so EVERY
            # response this handler builds passes the Logic-side
            # ShowNumberedOverlayResponse.from_dict, even when the inbound Logic
            # message carried malformed echo fields (a Logic bug or corruption).
            # from_dict requires trace_id to be a str and overlay_session_id /
            # paint_generation to be non-bool ints; a response that fails it is
            # log-dropped, which would break the handler-owned one-response
            # contract (the awaiting Future never resolves with a usable
            # result). For a well-formed message these are no-ops. The early
            # invalid_request reject below stops a malformed request before any
            # lookup; this coercion is the belt-and-suspenders guarantee that
            # the error response itself -- and the never-raise error path -- are
            # always serialisable.
            safe_trace = trace_id if isinstance(trace_id, str) else ""
            safe_session = (
                overlay_session_id
                if isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                else 0
            )
            safe_generation = (
                paint_generation
                if isinstance(paint_generation, int)
                and not isinstance(paint_generation, bool)
                else 0
            )
            return ShowNumberedOverlayResponse(
                status=status,
                outcome=outcome,
                reason=reason,
                snapshot_id=snapshot_id_out,
                snapshot_summary=snapshot_summary,
                trace_id=safe_trace,
                overlay_session_id=safe_session,
                paint_generation=safe_generation,
            )

        try:
            # Reject a malformed Logic message before any lookup, mirroring the
            # pin_snapshot / unpin_snapshot IPC-field guard (wh-n29v.43.1).
            # trace_id must be a str, overlay_session_id / paint_generation
            # non-bool ints, snapshot_id a str, and item_id_filter None or a
            # list of str (a non-list filter would also break
            # filter_and_renumber_summary's set() membership build). _build
            # coerces the echoed scalars, so this invalid_request response still
            # passes from_dict; rejecting here means we never run a snapshot
            # lookup against a malformed snapshot_id / filter. status="error"
            # pairs with outcome="error" (cross-field rule (a)); the snapshot
            # fields stay None (rule (d)).
            if not (
                isinstance(trace_id, str)
                and isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                and isinstance(paint_generation, int)
                and not isinstance(paint_generation, bool)
                and isinstance(snapshot_id, str)
                and (
                    item_id_filter is None
                    or (
                        isinstance(item_id_filter, list)
                        and all(isinstance(x, str) for x in item_id_filter)
                    )
                )
            ):
                logger.error(
                    "show_numbered_overlay: invalid request fields; rejecting "
                    "without lookup (trace=%r session=%r gen=%r snapshot=%r "
                    "filter_type=%s)",
                    trace_id, overlay_session_id, paint_generation, snapshot_id,
                    type(item_id_filter).__name__,
                )
                _emit(_build("error", "invalid_request", status="error"))
                return

            finder = self._get_overlay_walk_finder()
            if finder is None:
                # finder is None either because the COM root could not be built
                # on this host (the _AUTOMATION_UNAVAILABLE sentinel set by the
                # host's get_click_element_finder) or because the overlay is
                # genuinely disabled in config. Emit the matching reason tag so
                # the notice is accurate (mirrors start_overlay_walk): clicking
                # IS enabled in config on the COM-unavailable path, so
                # "disabled_by_config" would wrongly point the user at
                # config.toml. Logic also short-circuits before sending; this
                # defends the Input side against a stale or racing request. Both
                # are execution_failed (in _NO_SNAPSHOT_OUTCOMES) so the snapshot
                # fields stay None per cross-field rule (d).
                if self._host.click_automation_unavailable():
                    logger.info(
                        "show_numbered_overlay: IUIAutomation root unavailable "
                        "on this host; short-circuiting (trace_id=%s)", trace_id,
                    )
                    _emit(_build("execution_failed", "automation_unavailable"))
                    return
                logger.info(
                    "show_numbered_overlay: overlay disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                _emit(_build("execution_failed", "disabled_by_config"))
                return

            foreground = _handler_module._capture_click_foreground()
            logger.info(
                "show_numbered_overlay: looking up snapshot=%s in process=%s "
                "(session=%s gen=%s filter=%s trace_id=%s)",
                snapshot_id, foreground.foreground_process_name,
                overlay_session_id, paint_generation,
                None if item_id_filter is None else len(item_id_filter),
                trace_id,
            )

            snapshot = finder.get_snapshot(
                snapshot_id,
                current_foreground_window=foreground.foreground_window,
                current_foreground_pid=foreground.foreground_pid,
                current_foreground_process_name=(
                    foreground.foreground_process_name
                ),
                current_foreground_window_creation_time=(
                    foreground.foreground_window_creation_time
                ),
            )
            if snapshot is None:
                # Stale id, TTL-swept, LRU-evicted, or foreground-identity
                # mismatch -- nothing to paint. snapshot_expired is in
                # _NO_SNAPSHOT_OUTCOMES so snapshot_id/summary MUST be None
                # (cross-field rule (d)).
                logger.info(
                    "show_numbered_overlay: snapshot_expired for snapshot=%s "
                    "(trace_id=%s)", snapshot_id, trace_id,
                )
                _emit(_build("snapshot_expired", "stale_snapshot_id"))
                return

            # Build the display summary, then filter to item_id_filter (when
            # supplied) and renumber the kept items 1..K in reading order so the
            # painted badges are contiguous from 1.
            #
            # wh-overlay-nested-dupes.1.2: this path re-paints a by-name find()
            # snapshot, which is stored UNCOLLAPSED (a spoken name may match a
            # container, so find() keeps both). The Brave wrapper+link pair --
            # identical name, identical rectangle -- is exactly the shape that
            # makes find() ambiguous, so without a collapse the auto-open would
            # paint badge 1 and badge 2 on the same pixels at the very moment
            # the user must read a number. Collapse the DISPLAY set (the
            # filter-selected matches, in the snapshot's pre-order) the same
            # way overlay_walk collapses its walk; the stored snapshot is
            # untouched and every surviving item_id still resolves for
            # click_snapshot_item.
            keep = set(item_id_filter) if item_id_filter is not None else None
            selected = [
                m for m in snapshot.matches
                if keep is None or m.item_id in keep
            ]
            survivors = collapse_near_identical_containers(selected)
            summary = ElementFinder._build_summary(snapshot)
            summary = ElementFinder.filter_and_renumber_summary(
                summary, [m.item_id for m in survivors]
            )

            if not summary.items:
                # Zero interactive controls, or the filter excluded everything.
                # no_targets is the successful-but-empty outcome; it carries the
                # empty-items summary (never a populated one).
                logger.info(
                    "show_numbered_overlay: no_targets for snapshot=%s "
                    "(filter=%s trace_id=%s)",
                    snapshot_id,
                    None if item_id_filter is None else len(item_id_filter),
                    trace_id,
                )
                _emit(_build(
                    "no_targets",
                    None,
                    snapshot_id_out=snapshot.snapshot_id,
                    snapshot_summary=summary,
                ))
                return

            # outcome == "ok": a non-empty painted set. The summary already
            # carries snapshot.snapshot_id (filter_and_renumber preserves it),
            # so summary.snapshot_id == the top-level snapshot_id we echo
            # (cross-field rule (c)).
            logger.info(
                "show_numbered_overlay: ok with %d items "
                "(snapshot=%s trace_id=%s)",
                len(summary.items), snapshot.snapshot_id, trace_id,
            )
            _emit(_build(
                "ok",
                None,
                snapshot_id_out=snapshot.snapshot_id,
                snapshot_summary=summary,
            ))
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "show_numbered_overlay: unexpected error "
                "(trace_id=%s snapshot=%s): %s",
                trace_id, snapshot_id, exc, exc_info=True,
            )
            _emit(_build("error", "unexpected_error", status="error"))

    def click_snapshot_item(
        self,
        snapshot_id: str = "",
        item_id: str = "",
        request_id: Optional[str] = None,
        trace_id: str = "",
        gesture: str = "",
        overlay_session_id: Any = None,
        paint_generation: Any = None,
        **kwargs,
    ) -> None:
        """Click a numbered-overlay item by item_id (wh-tab7j / wh-jfavj).

        Phase 1.5 of the voice-element-clicking feature (epic wh-l4h.1). When
        the user clicks a numbered overlay badge, Logic resolves the display
        number to an ``item_id`` from its retained ``WalkSnapshotSummary`` and
        forwards this request carrying ``snapshot_id`` + ``item_id``
        (+ ``trace_id`` + ``request_id``, and since wh-click-gesture-param an
        optional ``gesture`` for "right click N" / "double click N"; absent or
        unrecognised means today's Invoke behaviour). The Input process owns
        the click:

          1. Validate the request fields (both ``snapshot_id`` and ``item_id``
             must be non-empty strings) before any lookup.
          2. Get the finder that holds the pinned snapshot store via
             ``_get_overlay_walk_finder`` -- the SAME accessor
             ``show_numbered_overlay`` uses, so it hits the populated store and
             applies the overlay-enabled / automation-unavailable gates.
          3. Capture the current foreground identity.
          4. Look up the pinned snapshot via ``finder.get_snapshot`` with the
             foreground-identity check; ``None`` means stale / TTL-swept /
             LRU-evicted / foreground mismatch -> ``snapshot_expired``.
          5. Find the ``ElementMatch`` in ``snapshot.matches`` whose
             ``item_id`` matches; absence -> ``item_not_found``.
          6. Run ``ClickExecutor.click`` -- the full pre-click verification
             block (foreground identity, IsEnabled, BoundingRectangle, the
             bounds-tolerance check, the popup-still-visible probe) and
             InvokePattern with the DoDefaultAction press fallback. The handler
             does NOT re-implement verification and adds no second COM read.
          7. Emit EXACTLY ONE ``ClickElementResponse`` with the same
             status/outcome pairing and reason tags as ``click_element``:
             ``status="ok"`` only for ``outcome="ok"``, ``status="error"`` for
             ``execution_failed``.

        The handler is in ``_HANDLES_OWN_RESPONSE`` so the generic emitter does
        not clobber the executor's outcome. It NEVER raises: any unexpected
        error is mapped to an ``execution_failed`` response so the
        one-response-per-request_id contract holds and the Logic awaiter does
        not fall through to its timeout path. ``snapshot_summary`` is always
        ``None`` here -- this handler clicks a pinned snapshot, it does not
        re-walk or repaint.

        wh-overlay-slow-uia-stale-badges.8 part 4: the request MAY carry
        ``overlay_session_id`` + ``paint_generation``, the pair of the visible
        list the spoken number was resolved against. Between the field
        validation and the snapshot lookup the handler refuses the click
        (``stale_overlay_generation``) when that pair is not STRICTLY newer
        than ``_last_executed_click_pair`` -- an earlier click already
        executed against that list and changed the screen, so the number now
        names the wrong control. Only an ok click records the watermark (a
        refusal leaves the screen unchanged, so a retry must work). An absent
        pair (pre-slice payload shape) or a malformed pair skips the check --
        the primary guard is Logic's in-flight refusal; this one is
        defence-in-depth, so it degrades OPEN like the pin watermark.
        """
        from services.wheelhouse.shared.click_element import (
            ClickElementResponse,
        )
        from ui.element_types import ElementQuery

        action_name = "click_snapshot_item"

        # Coerce trace_id to a str so EVERY response this handler builds passes
        # the Logic-side ClickElementResponse.from_dict (trace_id must be a str)
        # even when the inbound Logic message carried a malformed echo field.
        safe_trace = trace_id if isinstance(trace_id, str) else ""

        def _emit(response: "ClickElementResponse") -> None:
            payload = response.to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self._host.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                # Last-resort: log and drop. Without a response the Logic-side
                # Future times out, which Logic already handles gracefully.
                logger.error(
                    "click_snapshot_item: failed to enqueue response "
                    "(trace_id=%s snapshot=%s item=%s): %s",
                    safe_trace, snapshot_id, item_id, exc,
                )

        def _failed(reason: str, matched_name: Optional[str] = None,
                    snapshot_id_out: Optional[str] = None) -> "ClickElementResponse":
            return ClickElementResponse(
                status="error",
                outcome="execution_failed",
                reason=reason,
                matched_names=(matched_name,) if matched_name else (),
                snapshot_id=snapshot_id_out,
                snapshot_summary=None,
                matched_name=matched_name,
                trace_id=safe_trace,
            )

        try:
            # Reject a malformed Logic message before any lookup, mirroring the
            # show_numbered_overlay IPC-field guard. Both ids must be non-empty
            # strings; the snapshot_id echoed on the invalid_request response
            # stays None so we never echo a non-str id.
            if not (
                isinstance(snapshot_id, str) and snapshot_id
                and isinstance(item_id, str) and item_id
            ):
                logger.error(
                    "click_snapshot_item: invalid request fields; rejecting "
                    "without lookup (snapshot_type=%s item_type=%s "
                    "trace_id=%s)",
                    type(snapshot_id).__name__, type(item_id).__name__,
                    safe_trace,
                )
                _emit(_failed("invalid_request"))
                return

            # wh-overlay-slow-uia-stale-badges.8 part 4: one executed click
            # per (overlay_session_id, paint_generation). Parse the optional
            # pair; a malformed pair degrades OPEN (log, skip the check, and
            # do NOT record it) because the primary guard lives on the Logic
            # side and a type glitch must not refuse a click the user
            # legitimately asked for. bool is excluded explicitly -- it is an
            # int subclass, not a session id or generation.
            resolved_pair: Optional[tuple[int, int]] = None
            if (
                isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                and isinstance(paint_generation, int)
                and not isinstance(paint_generation, bool)
            ):
                resolved_pair = (overlay_session_id, paint_generation)
            elif overlay_session_id is not None or paint_generation is not None:
                logger.warning(
                    "click_snapshot_item: malformed overlay pair "
                    "(session=%r generation=%r); skipping the stale check "
                    "(trace_id=%s)",
                    overlay_session_id, paint_generation, safe_trace,
                )
            last = getattr(self, "_last_executed_click_pair", None)
            if resolved_pair is not None and last is not None and (
                resolved_pair[0] < last[0]
                or (
                    resolved_pair[0] == last[0]
                    and resolved_pair[1] <= last[1]
                )
            ):
                # The list this number came from was consumed by an earlier
                # executed click (same pair) or belongs to an even older
                # paint. Refuse WITHOUT touching the snapshot or executor:
                # the screen has already changed under the badge.
                logger.info(
                    "click_snapshot_item: stale_overlay_generation -- "
                    "resolved pair %s is not newer than the last executed "
                    "click's pair %s (snapshot=%s item=%s trace_id=%s)",
                    resolved_pair, last, snapshot_id, item_id, safe_trace,
                )
                _emit(_failed(
                    "stale_overlay_generation", snapshot_id_out=snapshot_id,
                ))
                return

            finder = self._get_overlay_walk_finder()
            if finder is None:
                # finder is None either because the COM root could not be built
                # on this host (the _AUTOMATION_UNAVAILABLE sentinel set by the
                # host's get_click_element_finder) or because the feature is
                # genuinely disabled in config. Emit the matching reason tag so
                # the notice is accurate (mirrors click_element /
                # show_numbered_overlay).
                if self._host.click_automation_unavailable():
                    logger.info(
                        "click_snapshot_item: IUIAutomation root unavailable "
                        "on this host; short-circuiting (trace_id=%s)",
                        safe_trace,
                    )
                    _emit(_failed("automation_unavailable"))
                    return
                logger.info(
                    "click_snapshot_item: feature disabled by config; "
                    "short-circuiting (trace_id=%s)", safe_trace,
                )
                _emit(_failed("disabled_by_config"))
                return

            foreground = _handler_module._capture_click_foreground()
            # Name the exact miss cause BEFORE get_snapshot is called
            # (wh-overlay-snapshot-keepalive Fix 3). The bare snapshot_expired
            # conflated a TTL expiry (trigger A), a never-stored/evicted id, and
            # a foreground change (trigger B), so a live report could not tell
            # the two triggers apart. describe_snapshot_miss is the non-mutating
            # query that names which one fired -- but get_snapshot MUTATES on a
            # miss (it sweeps a TTL-expired entry and drops a foreground-mismatch
            # entry before returning None), so describe MUST run first or it
            # always sees an already-gone entry and reports not_found for the two
            # real triggers (wh-overlay-snapshot-keepalive.1.1). describe returns
            # None on the hit path, so this is a cheap non-mutating lookup on
            # every numbered-badge click. Best-effort: never let cause-naming
            # break the click path. The emitted Schema-A reason tag stays
            # snapshot_expired (routing/notice behaviour is unchanged); only the
            # log gains the cause.
            miss_cause = None
            describe = getattr(finder, "describe_snapshot_miss", None)
            if callable(describe):
                try:
                    miss_cause = describe(
                        snapshot_id,
                        current_foreground_window=(
                            foreground.foreground_window
                        ),
                        current_foreground_pid=foreground.foreground_pid,
                        current_foreground_process_name=(
                            foreground.foreground_process_name
                        ),
                        current_foreground_window_creation_time=(
                            foreground.foreground_window_creation_time
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 -- log only
                    logger.debug(
                        "click_snapshot_item: describe_snapshot_miss failed "
                        "for snapshot=%s: %s", snapshot_id, exc,
                    )
            snapshot = finder.get_snapshot(
                snapshot_id,
                current_foreground_window=foreground.foreground_window,
                current_foreground_pid=foreground.foreground_pid,
                current_foreground_process_name=(
                    foreground.foreground_process_name
                ),
                current_foreground_window_creation_time=(
                    foreground.foreground_window_creation_time
                ),
            )
            if snapshot is None:
                # Stale id, TTL-swept, LRU-evicted, or foreground-identity
                # mismatch -- nothing to click. miss_cause (computed above,
                # before get_snapshot dropped the entry) names which.
                logger.info(
                    "click_snapshot_item: snapshot_expired (cause=%s) for "
                    "snapshot=%s (trace_id=%s)",
                    miss_cause or "unknown", snapshot_id, safe_trace,
                )
                _emit(_failed("snapshot_expired", snapshot_id_out=snapshot_id))
                return

            # Find the requested item by its stable item_id in the pinned
            # snapshot's matches (a simple linear scan -- the snapshot holds at
            # most a few dozen interactive controls).
            match = next(
                (m for m in snapshot.matches if m.item_id == item_id), None
            )
            if match is None:
                logger.info(
                    "click_snapshot_item: item_not_found item=%s in "
                    "snapshot=%s (trace_id=%s)",
                    item_id, snapshot_id, safe_trace,
                )
                _emit(_failed("item_not_found", snapshot_id_out=snapshot_id))
                return

            from ui.click_executor import SnapshotForeground

            # The overlay-click path has no spoken query; build a minimal
            # ElementQuery from the match. It is consumed ONLY by the executor's
            # coordinate-eligibility check (_coord_eligible) and, since
            # wh-click-gesture-param, by the gesture branch: "right click 5" /
            # "double click 5" arrive as a gesture field on the request, and
            # this is where that value becomes something the executor reads.
            # An absent or unrecognised value degrades to the default Invoke
            # gesture rather than guessing a physical click.
            query = ElementQuery(
                name=match.name,
                role=match.role,
                ordinal=None,
                spatial=None,
                raw_utterance="",
                gesture=_handler_module._parse_gesture(gesture),
            )
            snap_fg = SnapshotForeground(
                window=foreground.foreground_window,
                pid=foreground.foreground_pid,
                process_name=foreground.foreground_process_name,
                window_creation_time=foreground.foreground_window_creation_time,
            )
            executor = self._host.get_click_executor()
            # wh-review-pattern-fixes.8: a badge click can move focus, the
            # caret, or the selection; invalidate before the dispatch (see
            # the hotkey_action block comment for the full rationale).
            self._host.buffer_manager.invalidate()
            # badge_pick=True: a numbered-badge pick prefers the guarded
            # coordinate click over DoDefaultAction when InvokePattern is
            # structurally unavailable (wh-electron-dda-noop).
            click_result = executor.click(match, snap_fg, query, badge_pick=True)
            if click_result.outcome == "ok":
                if resolved_pair is not None:
                    # wh-overlay-slow-uia-stale-badges.8 part 4: only an
                    # EXECUTED click consumes its pair -- a refusal leaves
                    # the screen unchanged, so a retry from the same list
                    # must stay allowed.
                    self._last_executed_click_pair = resolved_pair
                logger.info(
                    "click_snapshot_item: clicked %r via %s "
                    "(item=%s snapshot=%s trace_id=%s)",
                    click_result.matched_name, click_result.clicked_via,
                    item_id, snapshot_id, safe_trace,
                )
                _emit(ClickElementResponse(
                    status="ok",
                    outcome="ok",
                    reason=None,
                    matched_names=(
                        (click_result.matched_name,)
                        if click_result.matched_name else ()
                    ),
                    snapshot_id=snapshot_id,
                    snapshot_summary=None,
                    matched_name=click_result.matched_name,
                    trace_id=safe_trace,
                ))
                return

            logger.info(
                "click_snapshot_item: execution_failed reason=%s matched=%r "
                "(item=%s snapshot=%s trace_id=%s)",
                click_result.reason, click_result.matched_name,
                item_id, snapshot_id, safe_trace,
            )
            _emit(_failed(
                click_result.reason or "invoke_com_error",
                matched_name=click_result.matched_name,
                snapshot_id_out=snapshot_id,
            ))
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "click_snapshot_item: unexpected error "
                "(trace_id=%s snapshot=%s item=%s): %s",
                safe_trace, snapshot_id, item_id, exc, exc_info=True,
            )
            _emit(_failed("invoke_com_error"))
