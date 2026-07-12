"""Tests for the GUI-side overlay_walk_cue rendering (wh-n29v.117).

While a numbered-overlay walk is in flight (overlay states
``walk_in_flight`` or ``refresh_in_flight``), Logic emits a plain-dict
``{'action': 'overlay_walk_cue', 'active': True}`` message onto the
Logic-to-GUI state queue so the floating button can show a small
"walking" progress cue ("we heard you, working"). When the overlay is
painted, or the walk fails/times out, Logic emits ``active': False``.

The cue rides the 100ms ``state_from_logic_queue`` dispatcher
(``GuiManager._check_queues_and_events``) -- NOT the 10ms shared-memory
activity fast path -- and is routed to ``FloatingButton.set_walk_cue``.

This file mirrors ``test_gui_click_notice.py``: a new queue action ->
a new handler -> a button call. The ``TestQueueDispatch`` class guards
the exact bug this slice risks -- a handler that exists (or a button
method that exists) but whose dispatch branch is missing, which would
silently drop the cue.

Defence in depth on the clear path: besides the primary ``active: False``
message, ``set_walk_cue(True)`` arms a single-shot timeout QTimer
(~3000ms) that self-clears the cue even when no clear message arrives
(a fresh-walk TIMEOUT sends no clear_overlay to the GUI).
"""

from __future__ import annotations

from queue import Empty
from unittest.mock import MagicMock, patch

import pytest

# Keep GuiManager construction free of real QDialogs in this file
# (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")


@pytest.fixture
def manager(qapp):
    with patch("gui.FloatingButton"), \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        from gui import GuiManager
        cmds_q = MagicMock()
        state_q = MagicMock()
        shutdown = MagicMock()
        shutdown.is_set.return_value = False
        mgr = GuiManager(shutdown, cmds_q, state_q)
        return mgr


@pytest.fixture
def button(qapp):
    """A real FloatingButton for rendering/timeout assertions."""
    from gui import FloatingButton
    return FloatingButton()


class TestQueueDispatch:
    """The dispatcher in ``_check_queues_and_events`` is the only
    production entry point for the cue. A missing ``elif`` branch or a
    typo in the action string would silently drop every walk cue while
    still passing direct handler tests; this is the guard.
    """

    def test_dispatch_routes_active_true_to_set_walk_cue(self, manager):
        payload = {
            "action": "overlay_walk_cue", "active": True, "walk_timeout_ms": 3000,
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]

        manager._check_queues_and_events()

        manager.button.set_walk_cue.assert_called_once_with(
            True, walk_timeout_ms=3000,
        )

    def test_dispatch_threads_walk_timeout_ms_through(self, manager):
        """wh-n29v.118: the effective walk timeout carried by Logic must be
        passed through to set_walk_cue so the GUI fallback outlasts it.
        """
        payload = {
            "action": "overlay_walk_cue", "active": True, "walk_timeout_ms": 9000,
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]

        manager._check_queues_and_events()

        manager.button.set_walk_cue.assert_called_once_with(
            True, walk_timeout_ms=9000,
        )

    def test_dispatch_absent_walk_timeout_passes_none(self, manager):
        """A payload with no walk_timeout_ms (older sender) passes None so
        set_walk_cue falls back to its default.
        """
        payload = {"action": "overlay_walk_cue", "active": True}
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]

        manager._check_queues_and_events()

        manager.button.set_walk_cue.assert_called_once_with(
            True, walk_timeout_ms=None,
        )

    def test_dispatch_routes_active_false_to_set_walk_cue(self, manager):
        payload = {"action": "overlay_walk_cue", "active": False}
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]

        manager._check_queues_and_events()

        manager.button.set_walk_cue.assert_called_once_with(
            False, walk_timeout_ms=None,
        )

    def test_dispatch_missing_active_key_defaults_false(self, manager):
        payload = {"action": "overlay_walk_cue"}
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]

        manager._check_queues_and_events()

        manager.button.set_walk_cue.assert_called_once_with(
            False, walk_timeout_ms=None,
        )

    def test_dispatch_does_not_invoke_other_handlers(self, manager):
        payload = {"action": "overlay_walk_cue", "active": True}
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]

        with patch.object(manager, "_handle_paint_overlay") as mock_paint, \
             patch.object(manager, "_handle_clear_overlay") as mock_clear:
            manager._check_queues_and_events()

        mock_paint.assert_not_called()
        mock_clear.assert_not_called()


class TestSetWalkCue:
    def test_set_walk_cue_true_sets_flag(self, button):
        assert button._walk_active is False
        button.set_walk_cue(True)
        assert button._walk_active is True

    def test_set_walk_cue_false_clears_flag(self, button):
        button.set_walk_cue(True)
        button.set_walk_cue(False)
        assert button._walk_active is False

    def test_set_walk_cue_true_starts_timeout_timer(self, button):
        button.set_walk_cue(True)
        assert button._walk_timeout_timer.isActive()

    def test_set_walk_cue_false_stops_timeout_timer(self, button):
        button.set_walk_cue(True)
        button.set_walk_cue(False)
        assert not button._walk_timeout_timer.isActive()

    def test_timeout_slot_clears_cue(self, button):
        button.set_walk_cue(True)
        assert button._walk_active is True
        # Fire the timeout slot directly (no real wait).
        button._on_walk_timeout()
        assert button._walk_active is False

    def test_walk_timeout_ms_arms_timer_at_value_plus_buffer(self, button):
        """wh-n29v.118: the GUI fallback must outlast the ACTUAL Logic walk
        bound (response_timeout_ms), not a hardcoded 3000ms. When Logic
        carries the effective timeout in the payload, the timer is armed at
        that value plus the named safety buffer.
        """
        from gui import _WALK_CUE_FALLBACK_BUFFER_MS

        button.set_walk_cue(True, walk_timeout_ms=8000)
        assert button._walk_timeout_timer.interval() == (
            8000 + _WALK_CUE_FALLBACK_BUFFER_MS
        )

    def test_no_walk_timeout_uses_default(self, button):
        """No walk_timeout_ms (older sender or absent field) falls back to
        the sane default (3000) plus the buffer.
        """
        from gui import _WALK_CUE_DEFAULT_WALK_MS, _WALK_CUE_FALLBACK_BUFFER_MS

        button.set_walk_cue(True)
        assert button._walk_timeout_timer.interval() == (
            _WALK_CUE_DEFAULT_WALK_MS + _WALK_CUE_FALLBACK_BUFFER_MS
        )

    def test_non_int_walk_timeout_falls_back_to_default(self, button):
        """A non-int walk_timeout_ms (version skew / corruption) must not
        crash or arm a nonsense interval; it degrades to the default.
        """
        from gui import _WALK_CUE_DEFAULT_WALK_MS, _WALK_CUE_FALLBACK_BUFFER_MS

        button.set_walk_cue(True, walk_timeout_ms="oops")
        assert button._walk_timeout_timer.interval() == (
            _WALK_CUE_DEFAULT_WALK_MS + _WALK_CUE_FALLBACK_BUFFER_MS
        )

    def test_bool_walk_timeout_falls_back_to_default(self, button):
        """bool is a subclass of int; True/False must NOT be accepted as a
        timeout value -- they degrade to the default.
        """
        from gui import _WALK_CUE_DEFAULT_WALK_MS, _WALK_CUE_FALLBACK_BUFFER_MS

        button.set_walk_cue(True, walk_timeout_ms=True)
        assert button._walk_timeout_timer.interval() == (
            _WALK_CUE_DEFAULT_WALK_MS + _WALK_CUE_FALLBACK_BUFFER_MS
        )


class TestWalkCueTimerBounds:
    """wh-n29v.119.1: QTimer.start takes a signed 32-bit int, and
    response_timeout_ms (carried as walk_timeout_ms) has NO upper bound. The
    fallback interval must be clamped to the Qt timer range, and arming the
    timer must fail closed so a start failure never leaves the cue stuck on
    with no timer to clear it.
    """

    def test_huge_walk_timeout_clamps_interval_and_keeps_cue_active(self, button):
        from gui import _QT_TIMER_MAX_INTERVAL_MS

        # 3e9 ms exceeds the signed 32-bit QTimer limit. Without clamping,
        # QTimer.start raises OverflowError AFTER the cue is marked active and
        # BEFORE the fallback is armed, stranding the dot on screen.
        button.set_walk_cue(True, walk_timeout_ms=3_000_000_000)
        assert button._walk_active is True
        assert button._walk_timeout_timer.isActive()
        assert button._walk_timeout_timer.interval() == _QT_TIMER_MAX_INTERVAL_MS

    def test_timer_start_failure_fails_closed(self, button):
        # Any failure arming the fallback timer must leave the cue inactive,
        # not stuck on with no timer to clear it.
        with patch.object(
            button._walk_timeout_timer, "start", side_effect=OverflowError
        ):
            button.set_walk_cue(True, walk_timeout_ms=3000)
        assert button._walk_active is False
        assert not button._walk_timeout_timer.isActive()


class TestRendering:
    def test_paint_event_runs_with_cue_active(self, button):
        """The composable cue glyph must paint on top of the base
        ellipse without raising in any base color state.
        """
        from PySide6.QtGui import QPaintEvent
        from PySide6.QtCore import QRect

        button.set_walk_cue(True)
        # Exercise paint across the base color branches that can overlap
        # with a walk (idle/enabled, hearing, confirmed).
        for enabled, indeterminate, activity in (
            (True, False, "idle"),
            (True, False, "hearing"),
            (True, False, "confirmed"),
            (False, False, "idle"),
            (True, True, "idle"),
        ):
            button._is_enabled = enabled
            button._is_indeterminate = indeterminate
            button._activity_state = activity
            event = QPaintEvent(QRect(0, 0, button.width(), button.height()))
            # Must not raise.
            button.paintEvent(event)

    def test_paint_event_runs_with_cue_inactive(self, button):
        from PySide6.QtGui import QPaintEvent
        from PySide6.QtCore import QRect

        assert button._walk_active is False
        event = QPaintEvent(QRect(0, 0, button.width(), button.height()))
        button.paintEvent(event)
