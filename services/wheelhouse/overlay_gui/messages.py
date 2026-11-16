"""OverlayGuiMessages: the overlay and grid window messages (area G2).

wh-full-refactor.7.2 (stage B of place 4) moved this code out of GuiManager
in gui.py without a change in behavior. The stage A boundary tests
(tests/test_overlay_gui_requirements_g2.py, docs/design/overlay-gui-requirements.md,
rows OG-n) pin that behavior. GuiManager creates one instance as
self.overlay_messages; its queue pump calls the seven handle_ methods and its
two timers call the two timer methods through it. This class reads the
GuiManager members it does not own through self._host (see host.py). It runs
in the GUI process.
"""

from __future__ import annotations

import logging
from queue import Full
from typing import TYPE_CHECKING, Any, Optional

from gui import (
    _OVERLAY_LEASE_DEFAULT_MS,
    _OVERLAY_TEARDOWN_RETRY_MS,
    _QT_TIMER_MAX_INTERVAL_MS,
)

if TYPE_CHECKING:
    from overlay_gui.host import OverlayGuiHost

# The logger of gui.py, by name. The GUI process imports gui.py as module
# "gui" (launcher.py: from gui import gui_process_target), so the moved code
# logs under the same logger name as before the move.
logger = logging.getLogger("gui")


class OverlayGuiMessages:
    """The numbered-overlay and mouse-grid messages of one GuiManager.

    It owns the badge lease pair (_overlay_lease_pair). The two timers, the
    two paint window managers and the queue to Logic stay on GuiManager.
    """

    def __init__(self, host: OverlayGuiHost) -> None:
        """``host`` is the GuiManager that owns this object."""
        self._host = host
        # The (overlay_session_id, paint_generation) pair the lease covers,
        # or None when no lease is active.
        self._overlay_lease_pair: tuple[int, int] | None = None

    def handle_paint_overlay(self, message: dict) -> None:
        """Drive the overlay manager for a paint_overlay action (wh-n29v.53).

        Parses the inbound dict via ``PaintOverlayEvent.from_dict`` (a
        malformed payload is logged and dropped, never raised, so a
        version-skewed sender cannot crash the GUI loop -- wh-uf54), calls
        the overlay manager, and forwards the returned
        ``overlay_state_changed`` dict back to Logic on
        ``commands_to_logic_queue``. A stale-gated paint returns ``None``
        and emits nothing.
        """
        if self._host.overlay_manager is None:
            return
        try:
            from shared.ipc_schema_validation import safe_parse
            from shared.paint_overlay import PaintOverlayEvent

            event = safe_parse(
                PaintOverlayEvent.from_dict, message, log_label="paint_overlay",
            )
            if event is None:
                return  # already logged
            result = self._host.overlay_manager.paint(
                event.summary,
                overlay_session_id=event.overlay_session_id,
                paint_generation=event.paint_generation,
            )
            if result is not None:
                # wh-overlay-slow-uia-stale-badges.9: the paint presented
                # (or failed partway, which can also leave windows), so
                # badges may be on screen -- arm the lease. A stale-gated
                # paint (None) presented nothing; any active lease belongs
                # to the overlay already on screen and must keep running.
                self._arm_overlay_lease(
                    (event.overlay_session_id, event.paint_generation),
                    _OVERLAY_LEASE_DEFAULT_MS,
                )
            self._emit_overlay_state_changed(result)
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "paint_overlay handling failed: %s", exc, exc_info=True,
            )

    def handle_clear_overlay(self, message: dict) -> None:
        """Drive the overlay manager for a clear_overlay action (wh-n29v.53).

        Parses the inbound dict via ``ClearOverlayEvent.from_dict`` (a
        malformed payload is logged and dropped, never raised -- wh-uf54),
        tears down all overlay windows, and forwards the returned
        ``overlay_state_changed`` dict (``state="cleared"``) back to Logic.
        """
        if self._host.overlay_manager is None:
            return
        try:
            from shared.ipc_schema_validation import safe_parse
            from shared.clear_overlay import ClearOverlayEvent

            event = safe_parse(
                ClearOverlayEvent.from_dict, message, log_label="clear_overlay",
            )
            if event is None:
                return  # already logged
            result = self._host.overlay_manager.clear(
                overlay_session_id=event.overlay_session_id,
                paint_generation=event.paint_generation,
            )
            if result is not None:
                # wh-overlay-slow-uia-stale-badges.9: the badges are gone,
                # so nothing is left to lease. A STALE clear (None) tore
                # down nothing -- the newer overlay's lease keeps running.
                # An INCOMPLETE clear (also None, teardown_pending True)
                # left a survivor: the lease keeps covering it while the
                # retry timer finishes the teardown (.18.4).
                self._cancel_overlay_lease()
            self._arm_teardown_retry_if_pending()
            self._emit_overlay_state_changed(result)
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "clear_overlay handling failed: %s", exc, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Numbered-overlay badge lease (wh-overlay-slow-uia-stale-badges.9)
    # ------------------------------------------------------------------

    def _arm_overlay_lease(self, pair: tuple[int, int], lease_ms: int) -> None:
        """Start (or restart) the badge lease for ``pair``.

        The interval is clamped to Qt's signed-32-bit ceiling
        (``_QT_TIMER_MAX_INTERVAL_MS``) so ``QTimer.start`` can never raise
        ``OverflowError`` on an oversized renew.
        """
        self._overlay_lease_pair = pair
        self._host.overlay_lease_timer.start(
            min(int(lease_ms), _QT_TIMER_MAX_INTERVAL_MS)
        )

    def _cancel_overlay_lease(self) -> None:
        """Stop the badge lease; no badges remain to watch."""
        self._overlay_lease_pair = None
        self._host.overlay_lease_timer.stop()

    def handle_overlay_lease_renew(self, message: dict) -> None:
        """Re-arm the badge lease for a Logic keepalive renew.

        Side-channel dict (no shared/ schema -- the walk-cue precedent),
        so validation is local and defensive (bool excluded -- it is an
        int subclass). The lease audits that Logic is ALIVE and believes
        this session's badges are visible -- not that Logic's view of the
        generation matches the GUI's. So a renew is accepted when its
        session id equals the armed lease pair's session id AND its
        (sid, gen) pair is <= the armed pair
        (wh-overlay-slow-uia-stale-badges.18.5): a renew from an OLDER
        view of the same session (a late or failed refresh paint armed
        the lease at a newer pair while Logic fell back to its prior
        visible pair) still proves liveness, and the exact-pair rule made
        the 60 s lease kill visible badges in that divergence. A renew
        for a pair NEWER than the armed lease stays rejected -- the GUI
        never presented that pair; Logic's visible-pair renew (.18.2)
        covers that direction. A different session id is rejected in both
        generation directions. The re-arm keeps the ARMED pair, so a
        later expiry tears down under the pair the generation gate
        painted. A malformed ``lease_ms`` falls back to the default
        rather than dropping the renew: a healthy Logic is talking, so
        keep the lease alive.
        """
        try:
            if self._overlay_lease_pair is None:
                return
            sid = message.get("overlay_session_id")
            gen = message.get("paint_generation")
            if isinstance(sid, bool) or not isinstance(sid, int):
                return
            if isinstance(gen, bool) or not isinstance(gen, int):
                return
            armed_sid, armed_gen = self._overlay_lease_pair
            if sid != armed_sid or (sid, gen) > (armed_sid, armed_gen):
                return
            lease_ms = message.get("lease_ms")
            if isinstance(lease_ms, bool) or not isinstance(lease_ms, int) \
                    or lease_ms <= 0:
                lease_ms = _OVERLAY_LEASE_DEFAULT_MS
            self._arm_overlay_lease((armed_sid, armed_gen), lease_ms)
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "overlay_lease_renew handling failed: %s", exc, exc_info=True,
            )

    def on_overlay_lease_expired(self) -> None:
        """The badge lease ran out: Logic stopped renewing.

        Either Logic crashed / hung, or its clear_overlay never arrived
        (a Full queue drops it with only a warning). Tear the badges down
        so they cannot outlive Logic's intent, and report
        ``state="expired"`` so a live Logic can reconcile (its machine
        closes from ``painted``; everywhere else the ack is bookkeeping).
        ``expire_lease`` returns None when the armed pair lost a race with
        a newer paint/clear -- then the newer overlay owns the screen and
        nothing is reported.
        """
        try:
            pair = self._overlay_lease_pair
            self._overlay_lease_pair = None
            if pair is None or self._host.overlay_manager is None:
                return
            logger.warning(
                "numbered-overlay badge lease expired for pair %s; "
                "tearing the badges down GUI-side", pair,
            )
            result = self._host.overlay_manager.expire_lease(
                overlay_session_id=pair[0], paint_generation=pair[1],
            )
            self._arm_teardown_retry_if_pending()
            self._emit_overlay_state_changed(result)
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "overlay lease expiry handling failed: %s", exc, exc_info=True,
            )

    def handle_reset_overlay(self, message: dict) -> None:
        """A (re)started Logic announced itself: start the overlay fresh.

        Destroys any badge windows a previous Logic left behind and resets
        the manager's generation gate (a restarted Logic numbers its pairs
        from zero, which the old high-water mark would gate forever). Also
        cancels the lease -- there is no Logic intent left to watch. No
        event is emitted; the new Logic starts from ``closed``.
        """
        try:
            self._cancel_overlay_lease()
            if self._host.overlay_manager is None:
                return
            self._host.overlay_manager.reset()
            # reset() dropped any deferred ack (the old Logic is gone),
            # but a survivor its destroy attempt failed on still needs
            # the retry (.18.4).
            self._arm_teardown_retry_if_pending()
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "reset_overlay handling failed: %s", exc, exc_info=True,
            )

    def _arm_teardown_retry_if_pending(self) -> None:
        """Arm or stop the teardown retry timer to match the manager.

        Called after every clear / expire_lease / reset drive into the
        overlay manager (wh-overlay-slow-uia-stale-badges.18.4): when a
        DestroyWindow failed, a badge window may still be on screen, and
        the retry timer is the mechanism that finishes the teardown.

        The stop branch (wh-overlay-slow-uia-stale-badges.18.8): a clean
        teardown pays the debt an EARLIER incomplete teardown armed the
        timer for. Left armed, that stale single-shot fire would arrive
        after a fresh paint took the screen (``retry_teardown`` itself
        no longer sweeps without debt; this stop is the timer-side half
        of the same fix). The timer stays armed while a deferred ack is
        still parked, so a fire can release it as late bookkeeping --
        the accepted residual.
        """
        if self._host.overlay_manager is None:
            return
        if self._host.overlay_manager.teardown_pending:
            self._host.overlay_teardown_retry_timer.start(
                _OVERLAY_TEARDOWN_RETRY_MS
            )
        elif not self._host.overlay_manager.has_deferred_teardown_ack:
            self._host.overlay_teardown_retry_timer.stop()

    def on_overlay_teardown_retry(self) -> None:
        """Retry destroying badge windows an incomplete teardown left.

        Drives ``retry_teardown`` (wh-overlay-slow-uia-stale-badges.18.4):
        a clean sweep releases the deferred cleared/expired ack, which is
        forwarded to Logic as late bookkeeping (its clear-ack watchdog
        already fired its truthful ERROR at 5000 ms -- designed behavior,
        not a bug); a still-failing sweep re-arms the timer. Unbounded
        2 s retries are fine -- a repeated no-op is cheap.

        A released CLEARED ack whose pair exactly matches the armed lease
        also cancels the lease (.18.6): the clean sweep is the same
        badges-are-gone proof that makes a direct clean clear cancel it,
        and a stranded lease would later fire expire_lease over an empty
        screen and report a spurious expired ack. Exact-match only: an
        old parked cleared ack must not cancel a newer overlay's lease,
        and a deferred EXPIRED ack's own expiry already dropped its
        lease.
        """
        try:
            if self._host.overlay_manager is None:
                return
            ack = self._host.overlay_manager.retry_teardown()
            if ack is not None:
                if ack.get("state") == "cleared" and (
                    ack.get("overlay_session_id"),
                    ack.get("paint_generation"),
                ) == self._overlay_lease_pair:
                    self._cancel_overlay_lease()
                self._emit_overlay_state_changed(ack)
            if self._host.overlay_manager.teardown_pending:
                self._host.overlay_teardown_retry_timer.start(
                    _OVERLAY_TEARDOWN_RETRY_MS
                )
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "overlay teardown retry failed: %s", exc, exc_info=True,
            )

    def handle_paint_grid(self, message: dict) -> None:
        """Drive the grid manager for a paint_grid action (wh-grid-paint-mode).

        Parses the inbound dict via ``PaintGridEvent.from_dict`` (a malformed
        payload is logged and dropped, never raised, so a version-skewed
        sender cannot crash the GUI loop -- wh-uf54) and hands the event
        straight to the manager. Nothing is sent back to Logic: the grid has
        no in-flight build phase and no generation pair to acknowledge.
        """
        if self._host.grid_overlay is None:
            return
        try:
            from shared.ipc_schema_validation import safe_parse
            from shared.paint_grid import PaintGridEvent

            event = safe_parse(
                PaintGridEvent.from_dict, message, log_label="paint_grid",
            )
            if event is None:
                return  # already logged
            self._host.grid_overlay.paint_grid(event)
        except Exception as exc:  # noqa: BLE001 - the grid is non-critical
            logger.warning(
                "paint_grid handling failed: %s", exc, exc_info=True,
            )

    def handle_paint_grid_pin(self, message: dict) -> None:
        """Drive the grid manager for a paint_grid_pin action
        (wh-grid-paint-mode).

        Same defensive parse as ``handle_paint_grid``. The pin does not
        replace the grid -- the manager repaints both -- so no clear is sent
        first.
        """
        if self._host.grid_overlay is None:
            return
        try:
            from shared.ipc_schema_validation import safe_parse
            from shared.paint_grid_pin import PaintGridPinEvent

            event = safe_parse(
                PaintGridPinEvent.from_dict, message,
                log_label="paint_grid_pin",
            )
            if event is None:
                return  # already logged
            self._host.grid_overlay.paint_grid_pin(event)
        except Exception as exc:  # noqa: BLE001 - the grid is non-critical
            logger.warning(
                "paint_grid_pin handling failed: %s", exc, exc_info=True,
            )

    def handle_clear_grid(self, message: dict) -> None:
        """Drive the grid manager for a clear_grid action
        (wh-grid-paint-mode).

        ``ClearGridEvent`` carries no fields, but it is still parsed so a
        payload addressed to a different action can never tear the grid down.
        One clear removes the grid AND the pin.
        """
        if self._host.grid_overlay is None:
            return
        try:
            from shared.ipc_schema_validation import safe_parse
            from shared.clear_grid import ClearGridEvent

            event = safe_parse(
                ClearGridEvent.from_dict, message, log_label="clear_grid",
            )
            if event is None:
                return  # already logged
            self._host.grid_overlay.clear_grid()
        except Exception as exc:  # noqa: BLE001 - the grid is non-critical
            logger.warning(
                "clear_grid handling failed: %s", exc, exc_info=True,
            )

    def _emit_overlay_state_changed(
        self, result: Optional[dict[str, Any]],
    ) -> None:
        """Forward an overlay_state_changed dict back to Logic (wh-n29v.53).

        ``result`` is the manager's ``overlay_state_changed`` wire dict, or
        ``None`` for a stale-gated paint (nothing to report). A Full queue
        is logged and dropped rather than raised.
        """
        if not result:
            return
        try:
            self._host.commands_to_logic_queue.put_nowait(result)
        except Full:
            logger.warning(
                "overlay_state_changed: commands_to_logic_queue Full; "
                "dropping the overlay state report",
            )
