"""GUI-side badge lease and Logic-restart reset for the numbered overlay
(bead wh-overlay-slow-uia-stale-badges.9).

The GUI must never show badges longer than Logic believes they are on
screen. Logic's clear can be lost (queue Full, Logic crash), so the GUI
holds a single-shot lease timer per painted overlay:

* an accepted paint arms the lease at ``_OVERLAY_LEASE_DEFAULT_MS``;
* Logic's keepalive tick renews it with an ``overlay_lease_renew`` dict
  (side-channel, no shared/ schema -- the walk-cue precedent);
* an accepted clear cancels it;
* expiry drives ``OverlayPaintWindowManager.expire_lease`` and reports
  ``state="expired"`` back to Logic;
* a ``reset_overlay`` action (a restarted Logic announcing itself) resets
  the manager's generation gate, tears down leftovers, and cancels the
  lease.

Fixture style mirrors ``tests/test_gui_grid_overlay.py``: GuiManager with
FloatingButton / WorkingDialog / pystray / QTimer patched, handlers driven
directly, collaborators replaced with MagicMocks.
"""

from __future__ import annotations

from queue import Empty
from unittest.mock import MagicMock, patch

import pytest

# Keep GuiManager construction free of real QDialogs in this file
# (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")

# gui.py's import path (``shared.X``), matching test_gui_grid_overlay.py.
from shared.clear_overlay import ClearOverlayEvent
from shared.paint_overlay import PaintOverlayEvent
from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem


def _summary(snapshot_id: str = "snap-1") -> WalkSnapshotSummary:
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id,
        items=[
            WalkSnapshotSummaryItem(
                item_id="item-1",
                display_number=1,
                name="control 1",
                role="Button",
                bounds=(10, 20, 110, 60),
                monitor_id=0,
            )
        ],
        created_at_monotonic=123.0,
    )


def _paint_payload(session: int = 3, gen: int = 2) -> dict:
    return PaintOverlayEvent(
        overlay_session_id=session, paint_generation=gen, summary=_summary()
    ).to_dict()


@pytest.fixture
def manager(qapp):
    with patch("gui.FloatingButton"), \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        from gui import GuiManager
        shutdown = MagicMock()
        shutdown.is_set.return_value = False
        mgr = GuiManager(shutdown, MagicMock(), MagicMock())
        # Every QTimer(self) in __init__ returned the SAME patched mock;
        # give the lease timer and the teardown retry timer their own so
        # start/stop assertions are not polluted by the other timers.
        mgr._overlay_lease_timer = MagicMock()
        mgr._overlay_teardown_retry_timer = MagicMock()
        return mgr


class TestLeaseArm:
    def test_accepted_paint_arms_lease_at_default(self, manager):
        import gui as gui_mod

        manager._overlay_manager = MagicMock()
        manager._overlay_manager.paint.return_value = {
            "action": "overlay_state_changed", "state": "painted",
        }

        manager._handle_paint_overlay(_paint_payload(session=3, gen=2))

        assert manager._overlay_lease_pair == (3, 2)
        manager._overlay_lease_timer.start.assert_called_once_with(
            gui_mod._OVERLAY_LEASE_DEFAULT_MS
        )

    def test_stale_paint_leaves_lease_untouched(self, manager):
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.paint.return_value = None  # stale-gated
        manager._overlay_lease_pair = (5, 4)

        manager._handle_paint_overlay(_paint_payload(session=5, gen=3))

        assert manager._overlay_lease_pair == (5, 4)
        manager._overlay_lease_timer.start.assert_not_called()

    def test_failed_paint_still_arms(self, manager):
        # A failed paint can leave windows on screen (partial multi-monitor
        # failure), so the lease must cover it too.
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.paint.return_value = {
            "action": "overlay_state_changed", "state": "failed",
        }

        manager._handle_paint_overlay(_paint_payload(session=3, gen=2))

        assert manager._overlay_lease_pair == (3, 2)
        manager._overlay_lease_timer.start.assert_called_once()


class TestLeaseCancel:
    def test_accepted_clear_cancels_lease(self, manager):
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.clear.return_value = {
            "action": "overlay_state_changed", "state": "cleared",
        }
        manager._overlay_lease_pair = (5, 4)

        manager._handle_clear_overlay(
            ClearOverlayEvent(overlay_session_id=5, paint_generation=5).to_dict()
        )

        assert manager._overlay_lease_pair is None
        manager._overlay_lease_timer.stop.assert_called_once()

    def test_stale_clear_leaves_lease_running(self, manager):
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.clear.return_value = None  # stale-gated
        manager._overlay_lease_pair = (5, 4)

        manager._handle_clear_overlay(
            ClearOverlayEvent(overlay_session_id=5, paint_generation=3).to_dict()
        )

        assert manager._overlay_lease_pair == (5, 4)
        manager._overlay_lease_timer.stop.assert_not_called()


class TestLeaseRenew:
    def _renew(self, session=5, gen=4, lease_ms=60000):
        return {
            "action": "overlay_lease_renew",
            "overlay_session_id": session,
            "paint_generation": gen,
            "lease_ms": lease_ms,
        }

    def test_renew_at_active_pair_rearms(self, manager):
        manager._overlay_lease_pair = (5, 4)

        manager._handle_overlay_lease_renew(self._renew(5, 4, 60000))

        manager._overlay_lease_timer.start.assert_called_once_with(60000)

    def test_renew_at_same_session_older_generation_rearms(self, manager):
        # wh-overlay-slow-uia-stale-badges.18.5: the lease audits that
        # Logic is ALIVE and believes this session's badges are visible.
        # A renew from an OLDER view (a late or failed refresh paint
        # armed the lease at a newer pair while Logic fell back to its
        # prior visible pair) still proves liveness and must re-arm --
        # the exact-pair rule let the 60 s lease kill visible badges.
        manager._overlay_lease_pair = (5, 4)

        manager._handle_overlay_lease_renew(self._renew(5, 3, 60000))

        manager._overlay_lease_timer.start.assert_called_once_with(60000)
        # The lease identity stays at the ARMED pair: a later expiry must
        # tear down under the pair the generation gate painted.
        assert manager._overlay_lease_pair == (5, 4)

    def test_renew_at_same_session_newer_generation_is_ignored(self, manager):
        # A renew NEWER than the armed lease names a pair the GUI never
        # presented; the visible-pair renew (.18.2) covers that direction.
        manager._overlay_lease_pair = (5, 4)

        manager._handle_overlay_lease_renew(self._renew(5, 5, 60000))

        manager._overlay_lease_timer.start.assert_not_called()

    def test_renew_for_other_session_is_ignored_both_directions(self, manager):
        # A different session proves nothing about THIS session's badges,
        # whether its generation sits below or above the armed one.
        manager._overlay_lease_pair = (5, 4)

        manager._handle_overlay_lease_renew(self._renew(4, 2, 60000))
        manager._handle_overlay_lease_renew(self._renew(4, 9, 60000))

        manager._overlay_lease_timer.start.assert_not_called()

    def test_renew_with_no_active_lease_is_ignored(self, manager):
        manager._overlay_lease_pair = None

        manager._handle_overlay_lease_renew(self._renew(5, 4, 60000))

        manager._overlay_lease_timer.start.assert_not_called()

    def test_renew_clamps_to_qt_max(self, manager):
        import gui as gui_mod

        manager._overlay_lease_pair = (5, 4)

        manager._handle_overlay_lease_renew(self._renew(5, 4, 2 ** 40))

        manager._overlay_lease_timer.start.assert_called_once_with(
            gui_mod._QT_TIMER_MAX_INTERVAL_MS
        )

    def test_renew_with_bad_lease_ms_uses_default(self, manager):
        import gui as gui_mod

        manager._overlay_lease_pair = (5, 4)

        manager._handle_overlay_lease_renew(self._renew(5, 4, "soon"))

        manager._overlay_lease_timer.start.assert_called_once_with(
            gui_mod._OVERLAY_LEASE_DEFAULT_MS
        )

    def test_renew_with_bool_pair_field_is_ignored(self, manager):
        # bool is an int subclass; a True/False pair field must not match.
        manager._overlay_lease_pair = (1, 0)

        manager._handle_overlay_lease_renew(self._renew(True, False, 60000))

        manager._overlay_lease_timer.start.assert_not_called()


class TestLeaseExpirySlot:
    def test_expiry_drives_manager_and_reports_expired(self, manager):
        manager._overlay_manager = MagicMock()
        expired = {"action": "overlay_state_changed", "state": "expired"}
        manager._overlay_manager.expire_lease.return_value = expired
        manager._overlay_lease_pair = (5, 4)

        manager._on_overlay_lease_expired()

        manager._overlay_manager.expire_lease.assert_called_once_with(
            overlay_session_id=5, paint_generation=4
        )
        manager.commands_to_logic_queue.put_nowait.assert_called_once_with(
            expired
        )
        assert manager._overlay_lease_pair is None

    def test_expiry_with_no_lease_is_noop(self, manager):
        manager._overlay_manager = MagicMock()
        manager._overlay_lease_pair = None

        manager._on_overlay_lease_expired()

        manager._overlay_manager.expire_lease.assert_not_called()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_race_lost_expiry_reports_nothing(self, manager):
        # The manager judged the armed pair stale (a newer paint or clear
        # advanced the gate between arming and firing): nothing to report.
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.expire_lease.return_value = None
        manager._overlay_lease_pair = (5, 4)

        manager._on_overlay_lease_expired()

        manager.commands_to_logic_queue.put_nowait.assert_not_called()
        assert manager._overlay_lease_pair is None

    def test_manager_exception_does_not_escape(self, manager):
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.expire_lease.side_effect = RuntimeError("boom")
        manager._overlay_lease_pair = (5, 4)

        manager._on_overlay_lease_expired()


class TestResetOverlay:
    def test_reset_resets_manager_and_cancels_lease(self, manager):
        manager._overlay_manager = MagicMock()
        manager._overlay_lease_pair = (5, 4)

        manager._handle_reset_overlay({"action": "reset_overlay"})

        manager._overlay_manager.reset.assert_called_once_with()
        assert manager._overlay_lease_pair is None
        manager._overlay_lease_timer.stop.assert_called_once()

    def test_reset_with_no_manager_still_cancels_lease(self, manager):
        manager._overlay_manager = None
        manager._overlay_lease_pair = (5, 4)

        manager._handle_reset_overlay({"action": "reset_overlay"})

        assert manager._overlay_lease_pair is None

    def test_manager_exception_does_not_escape(self, manager):
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.reset.side_effect = RuntimeError("boom")

        manager._handle_reset_overlay({"action": "reset_overlay"})


class TestTeardownRetry:
    """wh-overlay-slow-uia-stale-badges.18.4 -- the GUI-side teardown retry.

    When a clear / lease expiry / reset leaves the manager with
    ``teardown_pending`` True (a DestroyWindow failed, so a badge window
    may still be on screen), the GUI arms a 2000 ms single-shot retry
    timer. Each fire drives ``retry_teardown``; a clean sweep releases
    the deferred cleared/expired ack onto the ack channel, and a
    still-pending sweep re-arms the timer.
    """

    def test_incomplete_clear_arms_retry_timer(self, manager):
        import gui as gui_mod

        manager._overlay_manager = MagicMock()
        manager._overlay_manager.clear.return_value = None  # ack deferred
        manager._overlay_manager.teardown_pending = True
        manager._overlay_lease_pair = (5, 4)

        manager._handle_clear_overlay(
            ClearOverlayEvent(overlay_session_id=5, paint_generation=5).to_dict()
        )

        manager._overlay_teardown_retry_timer.start.assert_called_once_with(
            gui_mod._OVERLAY_TEARDOWN_RETRY_MS
        )
        # No ack went to Logic (its clear-ack watchdog stays unresolved,
        # so its 5000 ms deadline fires a truthful ERROR), and the lease
        # keeps covering the survivors.
        manager.commands_to_logic_queue.put_nowait.assert_not_called()
        assert manager._overlay_lease_pair == (5, 4)
        manager._overlay_lease_timer.stop.assert_not_called()

    def test_retry_emits_deferred_ack_on_ack_channel(self, manager):
        manager._overlay_manager = MagicMock()
        ack = {"action": "overlay_state_changed", "state": "cleared"}
        manager._overlay_manager.retry_teardown.return_value = ack
        manager._overlay_manager.teardown_pending = False

        manager._on_overlay_teardown_retry()

        manager.commands_to_logic_queue.put_nowait.assert_called_once_with(ack)
        manager._overlay_teardown_retry_timer.start.assert_not_called()

    def test_still_pending_retry_rearms(self, manager):
        import gui as gui_mod

        manager._overlay_manager = MagicMock()
        manager._overlay_manager.retry_teardown.return_value = None
        manager._overlay_manager.teardown_pending = True

        manager._on_overlay_teardown_retry()

        manager.commands_to_logic_queue.put_nowait.assert_not_called()
        manager._overlay_teardown_retry_timer.start.assert_called_once_with(
            gui_mod._OVERLAY_TEARDOWN_RETRY_MS
        )

    def test_incomplete_expiry_arms_retry_timer(self, manager):
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.expire_lease.return_value = None
        manager._overlay_manager.teardown_pending = True
        manager._overlay_lease_pair = (5, 4)

        manager._on_overlay_lease_expired()

        manager._overlay_teardown_retry_timer.start.assert_called_once()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_reset_with_survivors_arms_retry_timer(self, manager):
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.teardown_pending = True

        manager._handle_reset_overlay({"action": "reset_overlay"})

        manager._overlay_manager.reset.assert_called_once_with()
        manager._overlay_teardown_retry_timer.start.assert_called_once()

    def test_clean_teardown_does_not_arm_retry_timer(self, manager):
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.clear.return_value = {
            "action": "overlay_state_changed", "state": "cleared",
        }
        manager._overlay_manager.teardown_pending = False

        manager._handle_clear_overlay(
            ClearOverlayEvent(overlay_session_id=5, paint_generation=5).to_dict()
        )

        manager._overlay_teardown_retry_timer.start.assert_not_called()

    def test_clean_teardown_with_no_parked_ack_stops_retry_timer(
        self, manager
    ):
        # wh-overlay-slow-uia-stale-badges.18.8: a clean teardown pays the
        # debt an EARLIER incomplete teardown armed the timer for. With no
        # parked ack left, the armed single-shot timer has nothing to do
        # and must be STOPPED -- left armed, its stale fire would arrive
        # after a fresh paint took the screen.
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.clear.return_value = {
            "action": "overlay_state_changed", "state": "cleared",
        }
        manager._overlay_manager.teardown_pending = False
        manager._overlay_manager.has_deferred_teardown_ack = False

        manager._handle_clear_overlay(
            ClearOverlayEvent(overlay_session_id=5, paint_generation=5).to_dict()
        )

        manager._overlay_teardown_retry_timer.stop.assert_called_once()
        manager._overlay_teardown_retry_timer.start.assert_not_called()

    def test_timer_stays_armed_while_ack_still_parked(self, manager):
        # The parked ack's late-bookkeeping release still needs a fire
        # (the accepted residual), so the timer is NOT stopped while the
        # manager reports a deferred ack.
        manager._overlay_manager = MagicMock()
        manager._overlay_manager.clear.return_value = {
            "action": "overlay_state_changed", "state": "cleared",
        }
        manager._overlay_manager.teardown_pending = False
        manager._overlay_manager.has_deferred_teardown_ack = True

        manager._handle_clear_overlay(
            ClearOverlayEvent(overlay_session_id=5, paint_generation=5).to_dict()
        )

        manager._overlay_teardown_retry_timer.stop.assert_not_called()
        manager._overlay_teardown_retry_timer.start.assert_not_called()

    def test_retry_with_no_manager_is_noop(self, manager):
        manager._overlay_manager = None

        manager._on_overlay_teardown_retry()  # must not raise

        manager._overlay_teardown_retry_timer.start.assert_not_called()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_retry_released_cleared_ack_cancels_matching_lease(self, manager):
        # wh-overlay-slow-uia-stale-badges.18.6: the clean retry proved
        # the badges at the armed pair are gone -- the same proof that
        # makes a direct clean clear cancel the lease. Without the
        # cancel, the stranded lease later fires expire_lease over an
        # empty screen and reports a spurious expired ack.
        manager._overlay_manager = MagicMock()
        ack = {
            "action": "overlay_state_changed", "state": "cleared",
            "overlay_session_id": 5, "paint_generation": 4,
        }
        manager._overlay_manager.retry_teardown.return_value = ack
        manager._overlay_manager.teardown_pending = False
        manager._overlay_lease_pair = (5, 4)

        manager._on_overlay_teardown_retry()

        assert manager._overlay_lease_pair is None
        manager._overlay_lease_timer.stop.assert_called_once()
        manager.commands_to_logic_queue.put_nowait.assert_called_once_with(ack)

    def test_retry_released_expired_ack_leaves_lease_untouched(self, manager):
        # A deferred EXPIRED ack comes from a lease expiry that already
        # dropped its own lease; any lease armed at retry time belongs
        # to something newer and must survive (.18.6 class table).
        manager._overlay_manager = MagicMock()
        ack = {
            "action": "overlay_state_changed", "state": "expired",
            "overlay_session_id": 5, "paint_generation": 4,
        }
        manager._overlay_manager.retry_teardown.return_value = ack
        manager._overlay_manager.teardown_pending = False
        manager._overlay_lease_pair = (5, 4)

        manager._on_overlay_teardown_retry()

        assert manager._overlay_lease_pair == (5, 4)
        manager._overlay_lease_timer.stop.assert_not_called()

    def test_retry_released_stale_cleared_ack_preserves_newer_lease(
        self, manager,
    ):
        # An old parked cleared ack must not cancel a NEWER overlay's
        # lease: a paint can land between the failed teardown and the
        # retry (the documented retry-vs-new-paint window), and the new
        # badges still need their lease (.18.6 class table).
        manager._overlay_manager = MagicMock()
        ack = {
            "action": "overlay_state_changed", "state": "cleared",
            "overlay_session_id": 5, "paint_generation": 4,
        }
        manager._overlay_manager.retry_teardown.return_value = ack
        manager._overlay_manager.teardown_pending = False
        manager._overlay_lease_pair = (5, 6)

        manager._on_overlay_teardown_retry()

        assert manager._overlay_lease_pair == (5, 6)
        manager._overlay_lease_timer.stop.assert_not_called()


class TestQueueDispatch:
    def test_overlay_lease_renew_routes_to_handler(self, manager):
        payload = {
            "action": "overlay_lease_renew",
            "overlay_session_id": 5,
            "paint_generation": 4,
            "lease_ms": 60000,
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]
        with patch.object(manager, "_handle_overlay_lease_renew") as handler:
            manager._check_queues_and_events()
        handler.assert_called_once_with(payload)

    def test_reset_overlay_routes_to_handler_and_clears_walk_cue(self, manager):
        payload = {"action": "reset_overlay"}
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]
        with patch.object(manager, "_handle_reset_overlay") as handler:
            manager._check_queues_and_events()
        handler.assert_called_once_with(payload)
        # A reset ends any in-flight walk from the dead Logic.
        manager.button.set_walk_cue.assert_called_with(False)
