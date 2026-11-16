"""OverlayPaintTeardown: clear, lease expiry and teardown (area P4).

wh-full-refactor.7.2 (stage B of place 4) moved this code out of
OverlayPaintWindowManager in overlay_paint_window.py without a change in
behavior. The stage A boundary tests (tests/test_overlay_gui_requirements_p4.py,
docs/design/overlay-gui-requirements.md, rows OT-n) pin that behavior.
OverlayPaintWindowManager creates one instance as self._teardown and keeps a
delegator for each public method; its paint and the grid painter call
destroy_all through it. This class reads the manager members it does not own
through self._host (see host.py). It runs in the GUI process.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from overlay_paint_window import GenerationGate
from shared.overlay_state_changed import OverlayStateChangedEvent

if TYPE_CHECKING:
    from overlay_paint.host import OverlayTeardownHost

# The logger of overlay_paint_window.py, by name, so the moved code logs
# under the same logger name as before the move.
logger = logging.getLogger("overlay_paint_window")


class OverlayPaintTeardown:
    """The teardown of one OverlayPaintWindowManager's windows.

    It owns the teardown debt flag (_teardown_pending) and the parked ack
    (_deferred_teardown_ack). The generation gate, the live windows and the
    debt cohort (host.pending_destroy) stay on the manager, because paint uses
    them too.
    """

    def __init__(self, host: OverlayTeardownHost) -> None:
        """``host`` is the OverlayPaintWindowManager that owns this object."""
        self._host = host
        # Teardown-completion tracking (wh-overlay-slow-uia-stale-badges.18.4).
        # _teardown_pending is True only while a TEARDOWN (clear /
        # expire_lease / reset) left a window whose DestroyWindow failed.
        # A container check cannot stand in for it: a non-empty
        # host.windows is the NORMAL painted state. Any destroy_all sweep
        # that ends with
        # both containers empty clears the flag. _deferred_teardown_ack
        # parks the cleared/expired event an incomplete teardown withheld;
        # retry_teardown releases it once a sweep ends clean.
        self._teardown_pending = False
        self._deferred_teardown_ack: Optional[dict[str, Any]] = None

    def clear(
        self,
        overlay_session_id: int,
        paint_generation: int,
    ) -> Optional[dict[str, Any]]:
        """Tear down ALL overlay windows and report ``state="cleared"``.

        Advances the generation high-water mark (when the clear's pair is
        ``>=`` the mark) so a late stale paint at the prior generation
        cannot present afterwards.

        A clear whose ``(overlay_session_id, paint_generation)`` pair is
        STRICTLY OLDER than the mark is STALE -- a newer paint or newer
        clear already advanced the mark, so honoring it would destroy a
        NEWER overlay that must stay on screen. A stale clear is a no-op:
        it returns ``None`` and tears down nothing (wh-n29v.55.1). The GUI
        caller drops a ``None`` result.

        An ACCEPTED clear whose destroy sweep left a survivor (a
        DestroyWindow failed) also returns ``None``: the ack is parked in
        ``_deferred_teardown_ack`` and ``teardown_pending`` turns True, so
        the GUI retry timer finishes the job (see ``retry_teardown``,
        wh-overlay-slow-uia-stale-badges.18.4).
        """
        if not self._host.gate.accept_clear(overlay_session_id, paint_generation):
            logger.debug(
                "overlay_paint_window: stale clear (%s, %s) ignored",
                overlay_session_id,
                paint_generation,
            )
            return None
        ack = OverlayStateChangedEvent(
            state="cleared",
            overlay_session_id=overlay_session_id,
            paint_generation=paint_generation,
            monitor_ids=(),
            snapshot_id=None,
        ).to_dict()
        if not self.destroy_all():
            # wh-overlay-slow-uia-stale-badges.18.4: a window survived its
            # DestroyWindow, so badges may still be on screen. A "cleared"
            # ack now would resolve Logic's clear-ack watchdog and cancel
            # the GUI badge lease while the survivor stays visible. The
            # retry drivers are Logic's clear-ack watchdog, the GUI badge
            # lease, and the GUI teardown retry timer
            # (GuiManager.overlay_teardown_retry_timer, which calls
            # retry_teardown). Park the ack instead. Logic's 5000 ms
            # watchdog then fires a truthful "badges may still be on the
            # screen" ERROR, and the deferred ack arrives after a later
            # successful retry as bookkeeping. That is the designed
            # behavior, not a bug. A newer teardown replaces an older
            # parked ack only when the newer teardown also leaves a
            # survivor. A newer teardown that ends clean returns its own
            # ack and leaves the older parked ack in place.
            self._teardown_pending = True
            self._deferred_teardown_ack = ack
            logger.warning(
                "overlay_paint_window: clear (%s, %s) left surviving badge "
                "windows; ack deferred until a retry destroys them",
                overlay_session_id,
                paint_generation,
            )
            return None
        return ack

    def expire_lease(
        self,
        overlay_session_id: int,
        paint_generation: int,
    ) -> Optional[dict[str, Any]]:
        """Tear down ALL overlay windows and report ``state="expired"``.

        The GUI-side badge-lease timer calls this when Logic stopped
        renewing the lease (wh-overlay-slow-uia-stale-badges.9): the badges
        must not outlive a Logic that no longer believes they are on
        screen. Gate semantics are identical to ``clear`` -- the pair
        advances the high-water mark and records the clear-block, so a
        late paint at the expired pair cannot re-present dead badges. A
        STALE expiry (the armed pair lost a race with a newer paint or
        clear) returns ``None`` and tears down nothing: a newer overlay
        owns the screen and its own lease.

        An ACCEPTED expiry whose destroy sweep left a survivor also
        returns ``None`` and parks the ack, exactly like ``clear``
        (wh-overlay-slow-uia-stale-badges.18.4).
        """
        if not self._host.gate.accept_clear(overlay_session_id, paint_generation):
            logger.debug(
                "overlay_paint_window: stale lease expiry (%s, %s) ignored",
                overlay_session_id,
                paint_generation,
            )
            return None
        ack = OverlayStateChangedEvent(
            state="expired",
            overlay_session_id=overlay_session_id,
            paint_generation=paint_generation,
            monitor_ids=(),
            snapshot_id=None,
        ).to_dict()
        if not self.destroy_all():
            # See clear(): no success claim while a window survived. The
            # GUI retry timer re-runs the destroy and releases this ack.
            self._teardown_pending = True
            self._deferred_teardown_ack = ack
            logger.warning(
                "overlay_paint_window: lease expiry (%s, %s) left surviving "
                "badge windows; ack deferred until a retry destroys them",
                overlay_session_id,
                paint_generation,
            )
            return None
        return ack

    def reset(self) -> None:
        """Destroy every overlay window and start a FRESH generation gate.

        The ``reset_overlay`` startup action calls this when a new Logic
        process announces itself (wh-overlay-slow-uia-stale-badges.9). A
        restarted Logic numbers its (overlay_session_id, paint_generation)
        pairs from zero again, so the old high-water mark would gate every
        new paint forever; and any badge windows a dead Logic left behind
        must not stay on screen. Emits nothing -- there is no live pair to
        report, and the new Logic starts from ``closed``.

        Drops any parked cleared/expired ack: the Logic process that
        asked for it is gone, so the ack must not surface later. A
        survivor still sets ``teardown_pending`` so the GUI retry keeps
        running (wh-overlay-slow-uia-stale-badges.18.4).
        """
        self._host.gate = GenerationGate()
        if not self.destroy_all():
            self._teardown_pending = True
        self._deferred_teardown_ack = None

    def retry_teardown(self) -> Optional[dict[str, Any]]:
        """Retry destroying the teardown debt cohort; release the parked ack.

        The GUI's 2000 ms retry timer drives this after an incomplete
        clear / expire_lease / reset left ``teardown_pending`` True
        (wh-overlay-slow-uia-stale-badges.18.4). It never touches the
        generation gate -- the original teardown already advanced it.

        COHORT-OWNED (wh-overlay-slow-uia-stale-badges.18.10): a
        teardown moves its survivors into the ``host.pending_destroy`` debt
        cohort (see ``destroy_all``), so the debt branch sweeps ONLY
        that cohort via ``_destroy_pending``. It never sweeps
        ``host.windows``, so a retry structurally cannot destroy a window
        that a later accepted paint owns. The paint path prunes the
        cohort on every paint but does not pay the debt; the next retry
        then finds the cohort empty, pays the debt, and releases the
        parked ack.

        Without debt (wh-overlay-slow-uia-stale-badges.18.8) this
        method sweeps nothing: a later clean clear / expire_lease /
        reset paid the debt before the timer fired, and a fresh paint
        may own the screen. It only releases a still-parked ack (the
        accepted late-bookkeeping residual) or does nothing.

        Returns the deferred cleared/expired ack exactly once, when no
        teardown debt remains (the cohort sweep just ended clean, or a
        later teardown already paid the debt) while an ack is parked;
        otherwise ``None``.
        """
        if self._teardown_pending and not self._destroy_pending():
            return None
        if self._deferred_teardown_ack is not None:
            ack = self._deferred_teardown_ack
            self._deferred_teardown_ack = None
            return ack
        return None

    @property
    def teardown_pending(self) -> bool:
        """True while a teardown left a window its DestroyWindow failed on.

        The survivor lives in the ``host.pending_destroy`` debt cohort,
        not in ``host.windows`` (wh-overlay-slow-uia-stale-badges.18.10). The
        GUI checks this after every clear / expire_lease / reset and
        after every retry, and keeps its retry timer armed while it is
        True (wh-overlay-slow-uia-stale-badges.18.4).
        """
        return self._teardown_pending

    @property
    def has_deferred_teardown_ack(self) -> bool:
        """True while a deferred cleared/expired ack is still parked.

        The GUI reads this beside ``teardown_pending`` to decide
        whether an armed retry timer is still needed
        (wh-overlay-slow-uia-stale-badges.18.8): with no teardown debt
        and no parked ack the timer has nothing left to do and is
        stopped; while an ack is parked the timer stays armed so
        ``retry_teardown`` can release it as late bookkeeping.
        """
        return self._deferred_teardown_ack is not None

    def clear_all(self) -> None:
        """Destroy every overlay window without emitting an event.

        Used on GUI teardown / fixture cleanup.
        """
        self.destroy_all()

    def destroy_all(self) -> bool:
        """Destroy every overlay window; move failures into the debt cohort.

        A window whose ``destroy()`` returned False is still on screen, so
        it is kept for a later teardown retry. This sweeps two sources:

        * ``host.pending_destroy`` -- the teardown debt cohort:
          rebuild-path orphans and survivors of earlier sweeps. Retried
          first; still-failing ones stay.
        * ``host.windows`` -- the live per-monitor windows. A survivor
          MOVES into ``host.pending_destroy``
          (wh-overlay-slow-uia-stale-badges.18.10), so ``host.windows``
          is always empty after this method returns. A later paint then
          owns ``host.windows`` alone, and ``retry_teardown`` sweeps only
          the cohort.

        Returns True when the cohort ended empty (a clean sweep) and
        False when a survivor remains. A clean sweep also pays off any
        recorded teardown debt (``_teardown_pending`` goes False). Only
        the TEARDOWN callers (clear / expire_lease / reset) set the flag
        on a False return; the paint except-path and ``clear_all`` do
        not, because they own no ack and the teardowns re-check the
        containers themselves (wh-overlay-slow-uia-stale-badges.18.4).
        """
        # Retry cohort members first; keep the ones that still fail.
        self._host.pending_destroy = [
            window for window in self._host.pending_destroy if not window.destroy()
        ]
        for window in self._host.windows.values():
            if not window.destroy():
                self._host.pending_destroy.append(window)
        self._host.windows = {}
        clean = not self._host.pending_destroy
        if clean:
            self._teardown_pending = False
        return clean

    def _destroy_pending(self) -> bool:
        """Sweep ONLY the ``host.pending_destroy`` teardown debt cohort.

        The same keep-the-failures filter as ``destroy_all``, restricted
        to the cohort. It never reads or writes ``host.windows``, so it
        cannot touch a window that a later paint owns
        (wh-overlay-slow-uia-stale-badges.18.10). Returns True when the
        cohort ended empty; a clean sweep also pays off any recorded
        teardown debt (``_teardown_pending`` goes False).
        """
        self._host.pending_destroy = [
            window for window in self._host.pending_destroy if not window.destroy()
        ]
        clean = not self._host.pending_destroy
        if clean:
            self._teardown_pending = False
        return clean
