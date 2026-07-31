"""Tests for resizing the floating button by dragging its edge.

A left press on the floating button used to mean two things: hold in place to
start push-to-talk, or drag to move. Grabbing the outer ring now means a third
thing -- resize -- and it has to take that press cleanly away from the other
two.

The riskiest part is push-to-talk. A press in the ring must not emit
``press_started`` AT ALL, so the hold timer never starts and grabbing the edge
can never open the microphone. The pre-existing ``_is_dragging`` guard is not
enough on its own: it only takes effect once the pointer has moved, so a press
that pauses on the edge before dragging would already have started recording.

The arithmetic lives in ``floating_button_geometry`` and is tested separately.
These tests cover the wiring: which gesture a press becomes, what the drag
does, and what the pointer looks like.

See docs/superpowers/specs/2026-07-25-floating-button-drag-resize-design.md.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent

from floating_button_geometry import MAX_BUTTON_SIZE, MIN_BUTTON_SIZE

pytestmark = pytest.mark.usefixtures("qapp")


BUTTON_ORIGIN = QPoint(400, 300)
BUTTON_SIZE = 50

# Button-local points, for a 50 px button: the ring boundary sits 17 px from
# the centre at (25, 25).
CENTRE_POINT = QPointF(25, 25)
RING_POINT = QPointF(2, 25)


@pytest.fixture
def button(qapp):
    """A real FloatingButton at a known position, never shown on screen."""
    from gui import FloatingButton

    widget = FloatingButton(initial_size=BUTTON_SIZE)
    widget.move(BUTTON_ORIGIN)
    # The real button refuses gestures until the stored size and position
    # arrive from the Logic process. These tests are about what happens after
    # that, so the fixture does what the manager does on the first state.
    widget.set_ready_for_gestures(True)
    yield widget
    widget.deleteLater()


def _global_for(button, local: QPointF) -> QPointF:
    """Translate a button-local point into virtual-desktop coordinates."""
    return QPointF(button.pos().x() + local.x(), button.pos().y() + local.y())


def _press(button, local: QPointF) -> None:
    button.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            local,
            _global_for(button, local),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


def _move_to(button, global_point: QPointF, buttons=Qt.MouseButton.LeftButton) -> None:
    local = QPointF(
        global_point.x() - button.pos().x(), global_point.y() - button.pos().y()
    )
    button.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            local,
            global_point,
            Qt.MouseButton.NoButton,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        )
    )


def _release(button, global_point: QPointF) -> None:
    local = QPointF(
        global_point.x() - button.pos().x(), global_point.y() - button.pos().y()
    )
    button.mouseReleaseEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            local,
            global_point,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


class TestPushToTalkIsNeverStartedByAnEdgeGrab:
    """The failure this guards is worse than the paper cut being fixed."""

    def test_press_in_the_ring_does_not_emit_press_started(self, button):
        started = []
        button.press_started.connect(lambda: started.append(True))

        _press(button, RING_POINT)

        assert started == []

    def test_press_in_the_middle_still_emits_press_started(self, button):
        started = []
        button.press_started.connect(lambda: started.append(True))

        _press(button, CENTRE_POINT)

        assert started == [True]

    def test_ring_press_that_never_moves_still_emits_nothing_on_release(self, button):
        """A press-and-release on the edge is a zero-length resize, not a click."""
        started = []
        ended = []
        clicked = []
        button.press_started.connect(lambda: started.append(True))
        button.press_ended.connect(lambda: ended.append(True))
        button.left_clicked.connect(lambda: clicked.append(True))

        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))

        assert started == []
        assert ended == []
        assert clicked == []

    def test_press_in_the_ring_of_the_smallest_button_still_suppresses(self, button):
        """The ring narrows on a small button; the suppression must follow it."""
        button.set_size(MIN_BUTTON_SIZE)
        started = []
        button.press_started.connect(lambda: started.append(True))

        radius = MIN_BUTTON_SIZE / 2
        # Hard against the left edge, which is ring at every supported size.
        _press(button, QPointF(0, radius))

        assert started == []


class TestRingPressResizes:
    def test_dragging_the_ring_outward_grows_the_button(self, button):
        _press(button, RING_POINT)
        # Centre is at (425, 325); drag to 40 px left of it -> diameter 80.
        _move_to(button, QPointF(385, 325))

        assert button.width() == 80

    def test_dragging_the_ring_inward_shrinks_the_button(self, button):
        _press(button, RING_POINT)
        _move_to(button, QPointF(405, 325))

        assert button.width() == 40

    def test_resize_keeps_the_button_centred_on_where_it_started(self, button):
        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))

        assert button.pos().x() + button.width() // 2 == 425
        assert button.pos().y() + button.height() // 2 == 325

    def test_resize_stops_at_the_maximum(self, button):
        _press(button, RING_POINT)
        _move_to(button, QPointF(0, 325))

        assert button.width() == MAX_BUTTON_SIZE

    def test_resize_stops_at_the_minimum(self, button):
        _press(button, RING_POINT)
        _move_to(button, QPointF(425, 325))

        assert button.width() == MIN_BUTTON_SIZE

    def test_ring_press_does_not_move_the_button(self, button):
        """A resize drag must never turn into a move part-way through."""
        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))

        # The corner moves because the button grew around a fixed centre, but
        # the centre itself must not have travelled with the pointer.
        assert button.pos().x() + button.width() // 2 == 425

    def test_resize_takes_priority_over_a_move_in_flight(self, button):
        """One event must never both resize and move.

        The two gestures cannot currently overlap: a ring press returns before
        it records a move origin, so the move branch is unreachable during a
        resize. That makes the priority an accident of ordering rather than a
        stated rule, so this test sets both gestures' state by hand and pins
        the rule directly. A later change that lets both flags be live at once
        then cannot silently produce a drag that resizes and travels together.
        """
        _press(button, RING_POINT)
        button._initial_press_pos = QPoint(425, 325)
        button._is_dragging = True
        button._drag_position = QPoint(25, 25)

        _move_to(button, QPointF(385, 325))

        assert button.width() == 80
        assert button.pos().x() + button.width() // 2 == 425

    def test_resize_stops_when_the_button_is_released(self, button):
        """A resize flag left set would resize on every later mouse move."""
        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))
        _release(button, QPointF(385, 325))

        _move_to(button, QPointF(300, 325), buttons=Qt.MouseButton.NoButton)

        assert button.width() == 80

    def test_push_to_talk_works_again_after_a_resize(self, button):
        """The gestures share one press path; a resize must not poison it."""
        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))
        _release(button, QPointF(385, 325))

        started = []
        button.press_started.connect(lambda: started.append(True))
        # The button is 80 px now, so its centre is 40 px in.
        _press(button, QPointF(40, 40))

        assert started == [True]

    def test_ring_press_does_not_emit_drag_started(self, button):
        dragged = []
        button.drag_started.connect(lambda: dragged.append(True))

        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))

        assert dragged == []


class TestMiddlePressStillMoves:
    def test_drag_from_the_middle_moves_the_button(self, button):
        # The move gesture captures its grab offset on the event that crosses
        # the drag threshold, so a real drag is at least two events: one to
        # start dragging and one to travel.
        _press(button, CENTRE_POINT)
        _move_to(button, QPointF(435, 335))
        _move_to(button, QPointF(525, 425))

        # The pointer travelled (90, 90) after the drag began, so the button
        # travelled with it from (400, 300).
        assert button.pos() == QPoint(490, 390)

    def test_drag_from_the_middle_does_not_resize(self, button):
        _press(button, CENTRE_POINT)
        _move_to(button, QPointF(525, 425))

        assert button.width() == BUTTON_SIZE

    def test_drag_from_the_middle_emits_drag_started(self, button):
        dragged = []
        button.drag_started.connect(lambda: dragged.append(True))

        _press(button, CENTRE_POINT)
        _move_to(button, QPointF(525, 425))

        assert dragged == [True]


class TestInterruptedResizeLeavesNoStaleState:
    """A resize can end without a release ever reaching the button.

    The context menu is the clearest way in: right-clicking mid-drag runs
    ``menu.exec()``, which grabs the mouse, so the left-button release goes to
    the menu instead. Whatever the cause, the next gesture must start clean --
    otherwise a later press in the middle silently resizes around a stale
    centre, and the push-to-talk hold timer arms while that is happening.
    """

    def _abandon_a_resize(self, button):
        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))
        # No release: the mouse went somewhere else.

    def test_a_later_middle_press_moves_instead_of_resizing(self, button):
        self._abandon_a_resize(button)
        width_after_abandoned_resize = button.width()

        _press(button, QPointF(40, 40))
        _move_to(button, QPointF(415, 305))
        _move_to(button, QPointF(505, 395))

        assert button.width() == width_after_abandoned_resize

    def test_a_later_middle_press_still_arms_push_to_talk(self, button):
        self._abandon_a_resize(button)

        started = []
        button.press_started.connect(lambda: started.append(True))
        _press(button, QPointF(40, 40))

        assert started == [True]

    def test_showing_the_button_again_clears_an_abandoned_resize(self, button):
        self._abandon_a_resize(button)
        width_after_abandoned_resize = button.width()

        button.hide()
        button.show()
        _move_to(button, QPointF(300, 325))
        button.hide()

        assert button.width() == width_after_abandoned_resize


class TestHoldTimerRefusesToStartPushToTalkDuringAResize:
    """The last line of defence for the microphone.

    Suppressing ``press_started`` on a ring press is the primary guard. This is
    the backstop: even if some future path leaves a resize in flight, the hold
    timer itself refuses to open the microphone. The pre-existing guard covered
    only the move gesture.
    """

    def _hold_timer_fires_with(self, *, resizing: bool, dragging: bool = False):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from gui import GuiManager

        manager = SimpleNamespace(
            button=SimpleNamespace(_is_dragging=dragging, _is_resizing=resizing),
            _ptt_held=False,
            _start_ptt=MagicMock(),
        )
        GuiManager._on_hold_threshold(manager)
        return manager

    def test_a_resize_in_flight_blocks_push_to_talk(self):
        manager = self._hold_timer_fires_with(resizing=True)

        manager._start_ptt.assert_not_called()
        assert manager._ptt_held is False

    def test_a_move_in_flight_still_blocks_push_to_talk(self):
        manager = self._hold_timer_fires_with(resizing=False, dragging=True)

        manager._start_ptt.assert_not_called()

    def test_an_ordinary_hold_still_starts_push_to_talk(self):
        manager = self._hold_timer_fires_with(resizing=False)

        manager._start_ptt.assert_called_once()
        assert manager._ptt_held is True


class TestScreenIsRecordedWhenTheDragStarts:
    """Which screen bounds the correction is decided once, at the press.

    The button grows around a fixed centre, so its corner travels outward. Ask
    Qt which screen the button is on part-way through and the answer can change
    to a neighbouring monitor; correcting against that monitor's bounds then
    teleports the button onto it in a single frame.
    """

    class _FakeScreen:
        def __init__(self, x, y, w, h):
            self._rect = QRect(x, y, w, h)

        def availableGeometry(self):
            return self._rect

    def test_a_monitor_change_mid_drag_does_not_move_the_button_to_it(self, button):
        _press(button, RING_POINT)

        # Qt now reports a monitor to the right of the one the drag began on.
        button.screen = lambda: self._FakeScreen(1920, 0, 1920, 1080)
        _move_to(button, QPointF(385, 325))

        # Correcting against that monitor would clamp the button to x >= 1920.
        assert button.pos().x() == 385
        assert button.width() == 80

    def test_the_recorded_screen_still_corrects_an_overhang(self, button):
        # Centre the button at x=480 on a screen only 500 px wide, so growing
        # to the maximum genuinely pushes its right edge past the screen. At
        # the default position the largest possible button stops exactly at the
        # edge, and the assertion would hold whether or not the correction ran.
        button.move(QPoint(455, 300))
        button.screen = lambda: self._FakeScreen(0, 0, 500, 1080)
        _press(button, RING_POINT)

        button.screen = lambda: self._FakeScreen(1920, 0, 1920, 1080)
        _move_to(button, QPointF(300, 325))

        # Uncorrected the button would sit at x=405 and end at 555.
        assert button.width() == MAX_BUTTON_SIZE
        assert button.pos().x() == 500 - MAX_BUTTON_SIZE
        assert button.pos().x() + button.width() == 500


class TestTheHeldLeftButtonOwnsTheResize:
    """A resize lasts exactly as long as the left button is held.

    Nothing in the move handler used to check that the button was still down.
    That made an abandoned resize follow the bare pointer: after the release
    went somewhere else, the next ordinary hover carried on resizing, and the
    button grew on its own while the user was not pressing anything.
    """

    def test_a_hover_with_no_button_held_does_not_keep_resizing(self, button):
        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))
        size_after_the_drag = button.width()

        _move_to(button, QPointF(300, 325), buttons=Qt.MouseButton.NoButton)

        assert button.width() == size_after_the_drag

    def test_a_move_with_only_the_right_button_held_does_not_keep_resizing(self, button):
        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))
        size_after_the_drag = button.width()

        _move_to(button, QPointF(300, 325), buttons=Qt.MouseButton.RightButton)

        assert button.width() == size_after_the_drag

    def test_the_resize_is_over_once_the_button_comes_up_elsewhere(self, button):
        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))
        _move_to(button, QPointF(300, 325), buttons=Qt.MouseButton.NoButton)

        # A fresh press in the middle must now move the button, not resize it.
        size_after_the_drag = button.width()
        _press(button, QPointF(40, 40))
        _move_to(button, QPointF(button.pos().x() + 60, button.pos().y() + 60))
        _move_to(button, QPointF(button.pos().x() + 150, button.pos().y() + 150))

        assert button.width() == size_after_the_drag

    def _open_context_menu(self, button):
        from PySide6.QtGui import QContextMenuEvent

        button.contextMenuEvent(
            QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse,
                QPoint(10, 10),
                QPoint(410, 310),
            )
        )

    def test_opening_the_context_menu_ends_the_resize(self, button):
        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))
        size_after_the_drag = button.width()

        self._open_context_menu(button)
        # Still reporting the left button as held, because as far as this
        # widget knows it never came up -- the menu took the release. The
        # resize must already be over anyway.
        _move_to(button, QPointF(300, 325))

        assert button.width() == size_after_the_drag

    def test_opening_the_context_menu_saves_the_resize_it_ends(self, button):
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append((size, pos)))

        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))
        self._open_context_menu(button)

        assert saved == [(80, QPoint(385, 285))]

    def test_a_wheel_resize_ends_an_edge_resize_first(self, button):
        """Otherwise the wheel moves the button while the drag holds an old centre."""
        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))

        _ctrl_wheel(button, 1)
        size_after_the_wheel = button.width()
        # Left button still held: without the edge resize having been ended,
        # this move would snap the button back around the drag's old centre.
        _move_to(button, QPointF(300, 325))

        assert button.width() == size_after_the_wheel

    def test_a_wheel_resize_on_its_own_reports_no_edge_resize(self, button):
        """The finisher must do nothing when no edge drag is in progress."""
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append((size, pos)))

        _ctrl_wheel(button, 1)

        assert saved == []

    def test_showing_the_button_reports_no_edge_resize(self, button):
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append((size, pos)))

        button.show()
        button.hide()

        assert saved == []


class TestSaving:
    """Size and position are saved together, once, when the drag ends.

    They are one coupled setting, not two. The button grows around its centre,
    so its stored top-left corner moves during a resize; saving the size while
    losing the corner puts a differently sized button at an incompatible
    position on the next start. They therefore travel as a single value.
    """

    def test_release_reports_the_final_size_and_position_once(self, button):
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append((size, pos)))

        _press(button, RING_POINT)
        _move_to(button, QPointF(395, 325))
        _move_to(button, QPointF(385, 325))
        _release(button, QPointF(385, 325))

        assert saved == [(80, QPoint(385, 285))]

    def test_a_resize_in_progress_saves_nothing(self, button):
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append((size, pos)))

        _press(button, RING_POINT)
        _move_to(button, QPointF(395, 325))
        _move_to(button, QPointF(385, 325))

        assert saved == []

    def test_an_edge_tap_that_changed_nothing_saves_nothing(self, button):
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append((size, pos)))

        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))

        assert saved == []

    def test_a_resize_that_only_moved_the_button_is_still_saved(self, button):
        """The correction can move the button without changing its size.

        A button restored at a stale position that is partly off screen gets
        pulled back on screen by the correction. If the drag ends at the same
        diameter it started at, the size has not changed -- but the position
        has, and the corrected position is what the user now sees. Deciding
        whether to save by looking at the size alone leaves the old position
        stored, and the button returns to it after a restart.
        """
        button.move(QPoint(-10, 300))
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append((size, pos)))

        # Centre is at (15, 325). Drag to exactly 25 px away, so the diameter
        # comes back to the 50 it started at while the correction moves the
        # corner from -10 to 0.
        _press(button, QPointF(2, 25))
        _move_to(button, QPointF(-10, 325))
        _release(button, QPointF(-10, 325))

        assert button.width() == BUTTON_SIZE
        assert saved == [(BUTTON_SIZE, QPoint(0, 300))]

    def test_a_resize_that_only_changed_the_size_is_still_saved(self, button):
        """The corner can stay put while the diameter changes by one pixel.

        The corner is the centre minus half the diameter, rounded down, so
        growing from 50 to 51 leaves it exactly where it was. Deciding whether
        to save by looking at the position alone would call that "nothing
        changed" and lose the new size.
        """
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append((size, pos)))

        # 24 left and 9 up from the centre at (425, 325) is 25.63 px away, so
        # the diameter comes out at 51 while the corner stays at (400, 300).
        _press(button, RING_POINT)
        _move_to(button, QPointF(401, 316))
        _release(button, QPointF(401, 316))

        assert button.width() == 51
        assert button.pos() == BUTTON_ORIGIN
        assert saved == [(51, BUTTON_ORIGIN)]

    def test_moving_from_the_middle_still_saves_only_the_position(self, button):
        sizes = []
        positions = []
        button.size_changed.connect(sizes.append)
        button.moved.connect(positions.append)

        _press(button, CENTRE_POINT)
        _move_to(button, QPointF(435, 335))
        _move_to(button, QPointF(525, 425))
        _release(button, QPointF(525, 425))

        assert sizes == []
        assert positions == [QPoint(490, 390)]

    def test_a_resize_ended_by_a_lost_release_is_still_saved(self, button):
        """The size on screen and the size in settings must not diverge."""
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append((size, pos)))

        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))
        _move_to(button, QPointF(385, 325), buttons=Qt.MouseButton.NoButton)

        assert saved == [(80, QPoint(385, 285))]


class TestCursor:
    def test_hovering_the_ring_shows_a_resize_cursor(self, button):
        _move_to(button, _global_for(button, RING_POINT), buttons=Qt.MouseButton.NoButton)

        assert button.cursor().shape() == Qt.CursorShape.SizeFDiagCursor

    def test_hovering_the_middle_shows_the_default_cursor(self, button):
        _move_to(button, _global_for(button, RING_POINT), buttons=Qt.MouseButton.NoButton)
        _move_to(button, _global_for(button, CENTRE_POINT), buttons=Qt.MouseButton.NoButton)

        assert button.cursor().shape() == Qt.CursorShape.ArrowCursor

    def test_cursor_follows_the_narrowed_ring_on_a_small_button(self, button):
        button.set_size(30)
        # 12 px from the centre of a 30 px button: inside the 8 px ring a
        # bigger button would have, outside the 5 px ring this one has.
        _move_to(button, _global_for(button, QPointF(3, 15)), buttons=Qt.MouseButton.NoButton)
        assert button.cursor().shape() == Qt.CursorShape.SizeFDiagCursor

        _move_to(button, _global_for(button, QPointF(9, 15)), buttons=Qt.MouseButton.NoButton)
        assert button.cursor().shape() == Qt.CursorShape.ArrowCursor


class TestDoubleClick:
    def test_double_click_on_the_ring_does_not_fire(self, button):
        fired = []
        button.double_clicked.connect(lambda: fired.append(True))

        _press(button, RING_POINT)
        button.mouseDoubleClickEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonDblClick,
                RING_POINT,
                _global_for(button, RING_POINT),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

        assert fired == []

    def test_double_click_in_the_middle_still_fires(self, button):
        fired = []
        button.double_clicked.connect(lambda: fired.append(True))

        button.mouseDoubleClickEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonDblClick,
                CENTRE_POINT,
                _global_for(button, CENTRE_POINT),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

        assert fired == [True]


def _ctrl_wheel(button, notches: int) -> None:
    from PySide6.QtGui import QWheelEvent

    button.wheelEvent(
        QWheelEvent(
            QPointF(button.width() / 2, button.height() / 2),
            _global_for(button, CENTRE_POINT),
            QPoint(0, 0),
            QPoint(0, 120 * notches),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
    )


class TestWheelResizeUnchanged:
    def test_ctrl_wheel_still_resizes(self, button):
        sizes = []
        button.size_changed.connect(sizes.append)

        _ctrl_wheel(button, 1)

        assert button.width() == 60
        assert sizes == [60]


class TestBothGesturesShareTheSameLimits:
    """Ctrl + wheel and the edge drag resize the same button.

    If they disagree, one gesture reaches a size the other refuses, and the
    button can end up at a size its own limits say is impossible. Both of these
    drive the real wheel handler rather than comparing the constants against
    written-in numbers, because a comparison against numbers would still pass
    while the wheel handler used a completely different pair of limits.
    """

    def test_wheel_stops_at_the_same_minimum_as_the_drag(self, button):
        for _ in range(30):
            _ctrl_wheel(button, -1)

        assert button.width() == MIN_BUTTON_SIZE

    def test_wheel_stops_at_the_same_maximum_as_the_drag(self, button):
        for _ in range(30):
            _ctrl_wheel(button, 1)

        assert button.width() == MAX_BUTTON_SIZE

    def test_the_drag_reaches_the_same_minimum_the_wheel_does(self, button):
        _press(button, RING_POINT)
        _move_to(button, QPointF(425, 325))

        assert button.width() == MIN_BUTTON_SIZE

    def test_the_drag_reaches_the_same_maximum_the_wheel_does(self, button):
        _press(button, RING_POINT)
        _move_to(button, QPointF(0, 325))

        assert button.width() == MAX_BUTTON_SIZE


def _double_click(button, local: QPointF) -> None:
    """The second press of a double-click, which Qt sends to its own handler."""
    button.mouseDoubleClickEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonDblClick,
            local,
            _global_for(button, local),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


class TestAFreshPressFinishesTheGestureItSupersedes:
    """A press is one more way an earlier gesture can end.

    A release does not always arrive. The context menu's modal loop takes it,
    and so does hiding the button. The next press then has to finish whatever
    was left in flight before it starts anything of its own, or the earlier
    gesture is silently thrown away: an abandoned resize is never saved, and an
    abandoned press leaves the microphone open.
    """

    def test_abandoned_resize_is_saved_when_a_middle_press_supersedes_it(self, button):
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append((size, pos)))

        _press(button, RING_POINT)
        _move_to(button, QPointF(380, 300))   # grows the button
        grown = button.width()
        button._is_resizing = True            # the release never arrived
        saved.clear()

        _press(button, CENTRE_POINT)

        assert [size for size, _ in saved] == [grown]

    def test_abandoned_resize_is_saved_when_an_edge_press_supersedes_it(self, button):
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append((size, pos)))

        _press(button, RING_POINT)
        _move_to(button, QPointF(380, 300))
        grown = button.width()
        button._is_resizing = True            # the release never arrived
        saved.clear()

        _press(button, RING_POINT)

        assert [size for size, _ in saved] == [grown]

    def test_the_new_resize_measures_from_the_geometry_it_starts_at(self, button):
        """The superseding resize's baseline is the size on screen right now."""
        _press(button, RING_POINT)
        _move_to(button, QPointF(380, 300))
        grown = button.width()
        button._is_resizing = True

        _press(button, RING_POINT)

        assert button._resize_start_size == grown
        assert button._resize_start_pos == button.pos()

    def test_abandoned_press_is_cancelled_when_an_edge_press_supersedes_it(self, button):
        """The microphone must not be left open by the press that got lost."""
        cancelled = []
        button.press_cancelled.connect(lambda: cancelled.append(True))

        _press(button, CENTRE_POINT)          # push-to-talk arms on this
        _press(button, RING_POINT)            # its release never arrived

        assert cancelled == [True]

    def test_a_superseded_press_does_not_also_report_a_normal_release(self, button):
        """press_ended would toggle speech on; the press was abandoned, not made."""
        ended = []
        button.press_ended.connect(lambda: ended.append(True))

        _press(button, CENTRE_POINT)
        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))

        assert ended == []

    def test_the_context_menu_cancels_a_press_in_flight(self, button):
        """The menu's modal loop takes the release, so the press never ends."""
        from PySide6.QtGui import QContextMenuEvent

        cancelled = []
        button.press_cancelled.connect(lambda: cancelled.append(True))

        _press(button, CENTRE_POINT)
        button.contextMenuEvent(
            QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse,
                QPoint(25, 25),
                QPoint(425, 325),
            )
        )

        assert cancelled == [True]

    def test_hiding_the_button_cancels_a_press_in_flight(self, button):
        """The button never receives the release once it is off the screen."""
        from PySide6.QtGui import QHideEvent

        cancelled = []
        button.press_cancelled.connect(lambda: cancelled.append(True))

        _press(button, CENTRE_POINT)
        button.hideEvent(QHideEvent())

        assert cancelled == [True]

    def test_a_cancelled_press_cannot_still_drag_the_button(self, button):
        """The press is gone, so the mouse moving afterwards moves nothing."""
        from PySide6.QtGui import QContextMenuEvent

        dragged = []
        button.drag_started.connect(lambda: dragged.append(True))
        origin = button.pos()

        _press(button, CENTRE_POINT)
        button.contextMenuEvent(
            QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse,
                QPoint(25, 25),
                QPoint(425, 325),
            )
        )
        _move_to(button, QPointF(600, 500))
        _move_to(button, QPointF(650, 550))

        assert button.pos() == origin
        assert dragged == []

    def test_nothing_is_cancelled_when_no_press_was_in_flight(self, button):
        from PySide6.QtGui import QHideEvent, QShowEvent

        cancelled = []
        button.press_cancelled.connect(lambda: cancelled.append(True))

        button.hideEvent(QHideEvent())
        button.showEvent(QShowEvent())
        _press(button, RING_POINT)

        assert cancelled == []


class TestTheSecondQuickGrabOfTheEdgeAlsoResizes:
    """Qt sends the second press of a double-click to its own handler.

    That handler used to only refuse to report a double-click, so the second
    quick grab of the edge recorded no centre and no starting geometry, and
    dragging afterwards did nothing at all.
    """

    def test_the_second_edge_grab_can_still_resize(self, button):
        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))
        starting_size = button.width()

        _double_click(button, RING_POINT)
        _move_to(button, QPointF(380, 300))

        assert button.width() != starting_size

    def test_the_second_edge_grab_records_the_centre_it_grows_around(self, button):
        expected_centre = QPoint(
            button.pos().x() + button.width() // 2,
            button.pos().y() + button.height() // 2,
        )

        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))
        _double_click(button, RING_POINT)

        assert button._is_resizing is True
        assert button._resize_centre == expected_centre

    def test_the_second_edge_grab_still_reports_no_double_click(self, button):
        double_clicked = []
        button.double_clicked.connect(lambda: double_clicked.append(True))

        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))
        _double_click(button, RING_POINT)

        assert double_clicked == []

    def test_a_double_click_in_the_middle_still_reports_one(self, button):
        double_clicked = []
        button.double_clicked.connect(lambda: double_clicked.append(True))

        _double_click(button, CENTRE_POINT)

        assert double_clicked == [True]

    def test_the_second_edge_grab_saves_what_it_resized(self, button):
        saved = []
        button.resize_finished.connect(lambda size, pos: saved.append(size))

        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))
        _double_click(button, RING_POINT)
        _move_to(button, QPointF(380, 300))
        _release(button, QPointF(380, 300))

        assert saved == [button.width()]


def _real_double_click_sequence(button, first: QPointF, second: QPointF) -> list[str]:
    """Drive the event order Qt actually delivers, and record what comes out.

    Qt does not send a second press for a double-click. It sends press,
    release, then a double-click event, then a second release. Tests that call
    the double-click handler on its own miss everything that ordering causes.
    """
    seen: list[str] = []
    button.press_started.connect(lambda: seen.append("press_started"))
    button.press_ended.connect(lambda: seen.append("press_ended"))
    button.press_cancelled.connect(lambda: seen.append("press_cancelled"))
    button.double_clicked.connect(lambda: seen.append("double_clicked"))

    _press(button, first)
    _release(button, _global_for(button, first))
    _double_click(button, second)
    _release(button, _global_for(button, second))
    return seen


class TestTheRealDoubleClickSequence:
    """What the widget reports across a genuine Qt double-click.

    The manager assumed a second press_ended arrives after the double-click
    and used it to swallow one release. That signal was never sent, so the
    swallow was still pending on some later, unrelated release -- and a
    swallowed release leaves the hold timer running, which opens the
    microphone after the mouse is already up.
    """

    def test_a_middle_double_click_reports_one_press_one_end_one_double(self, button):
        seen = _real_double_click_sequence(button, CENTRE_POINT, CENTRE_POINT)

        assert seen == ["press_started", "press_ended", "double_clicked"]

    def test_a_ring_double_click_reports_nothing_at_all(self, button):
        seen = _real_double_click_sequence(button, RING_POINT, RING_POINT)

        assert seen == []


class TestARingGrabIsNotHalfOfADoubleClick:
    """Qt pairs a press with the one before it, whatever we decided that was.

    So grabbing the edge and then pressing the middle arrives as a
    double-click on the middle. Reporting that as a double-click would switch
    the speech interaction mode off one edge grab and one press, and the ring
    boundary is one pixel away from the middle at every size.
    """

    def test_ring_then_middle_does_not_report_a_double_click(self, button):
        seen = _real_double_click_sequence(button, RING_POINT, CENTRE_POINT)

        assert "double_clicked" not in seen

    def test_ring_then_middle_behaves_as_a_single_press(self, button):
        """The user did press the middle once, so it counts as one click."""
        seen = _real_double_click_sequence(button, RING_POINT, CENTRE_POINT)

        assert seen == ["press_started", "press_ended"]

    def test_middle_then_ring_starts_a_resize_and_reports_no_double_click(self, button):
        seen = []
        button.double_clicked.connect(lambda: seen.append("double_clicked"))

        _press(button, CENTRE_POINT)
        _release(button, _global_for(button, CENTRE_POINT))
        _double_click(button, RING_POINT)

        assert seen == []
        assert button._is_resizing is True

    def test_a_real_double_click_after_an_edge_grab_still_counts(self, button):
        """The rule is about the pair, not about the button being touched.

        Once a press in the middle has been seen, the edge grab before it is
        spent. A genuine double-click in the middle after that is the user
        asking for the mode switch, and refusing it would make the switch
        unreachable for anyone who had grabbed the edge earlier.
        """
        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))
        _press(button, CENTRE_POINT)
        _release(button, _global_for(button, CENTRE_POINT))

        seen = _real_double_click_sequence(button, CENTRE_POINT, CENTRE_POINT)

        assert "double_clicked" in seen

    def test_the_pair_after_a_refused_one_is_judged_on_its_own(self, button):
        """The refusal applies to one pair, not to everything that follows."""
        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))
        _double_click(button, CENTRE_POINT)  # refused: paired with the grab
        _release(button, _global_for(button, CENTRE_POINT))

        seen = _real_double_click_sequence(button, CENTRE_POINT, CENTRE_POINT)

        assert "double_clicked" in seen

    def test_the_smallest_button_still_separates_the_two_regions(self, button):
        """At 15 pixels the ring is 2.5 wide, so the two points are adjacent."""
        button.set_size(MIN_BUTTON_SIZE)
        ring = QPointF(0, 7)
        middle = QPointF(7, 7)
        assert button._point_is_on_edge(ring) is True
        assert button._point_is_on_edge(middle) is False

        seen = _real_double_click_sequence(button, ring, middle)

        assert "double_clicked" not in seen


class TestADoubleClickAlsoFinishesWhatItSupersedes:
    """A double-click is one more way an earlier gesture can end.

    The press handler already finishes whatever was in flight before it
    classifies a new press. The double-click handler did so on only one of its
    three paths, so a gesture whose release went missing survived into the
    other two. The damage is the same one this feature keeps producing: a
    press recorded with no release to match it, and a hold timer that fires
    after the mouse is already up.
    """

    def test_a_lost_edge_release_leaves_no_press_recorded(self, button):
        _press(button, RING_POINT)  # resize begins
        # The release goes somewhere else -- another window took it.
        _double_click(button, CENTRE_POINT)  # Qt pairs it with the edge press
        _release(button, _global_for(button, CENTRE_POINT))

        assert button._is_resizing is False
        assert button._initial_press_pos is None

    def test_a_lost_edge_release_ends_the_press_it_started(self, button):
        """Without this the manager is told a press began and never ended."""
        seen = []
        button.press_started.connect(lambda: seen.append("press_started"))
        button.press_ended.connect(lambda: seen.append("press_ended"))
        button.press_cancelled.connect(lambda: seen.append("press_cancelled"))

        _press(button, RING_POINT)
        _double_click(button, CENTRE_POINT)
        _release(button, _global_for(button, CENTRE_POINT))

        assert seen == ["press_started", "press_ended"]

    def test_a_lost_edge_release_still_saves_the_size_it_reached(self, button):
        """The resize happened on screen, so it has to be recorded."""
        saved = []
        button.resize_finished.connect(lambda w, p: saved.append(w))

        _press(button, RING_POINT)
        _move_to(button, QPointF(button.pos().x() + 60, button.pos().y() + 25))
        _double_click(button, CENTRE_POINT)

        assert saved, "the abandoned resize was thrown away"
        assert saved[-1] == button.width()

    def test_a_lost_release_after_a_drag_does_not_survive_a_double_click(self, button):
        _press(button, CENTRE_POINT)
        start = _global_for(button, CENTRE_POINT)
        _move_to(button, QPointF(start.x() + 40, start.y()))
        _move_to(button, QPointF(start.x() + 80, start.y()))
        assert button._is_dragging is True

        _double_click(button, CENTRE_POINT)

        assert button._is_dragging is False
        assert button._initial_press_pos is None

    def test_an_ordinary_double_click_still_reports_itself(self, button):
        """Ending the earlier gesture must not swallow the double-click."""
        seen = []
        button.double_clicked.connect(lambda: seen.append("double_clicked"))

        _press(button, CENTRE_POINT)
        _release(button, _global_for(button, CENTRE_POINT))
        _double_click(button, CENTRE_POINT)

        assert seen == ["double_clicked"]


def _context_menu_event(button):
    """A right-click on the button, as Qt would deliver it."""
    from PySide6.QtGui import QContextMenuEvent

    return QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse,
        QPoint(10, 10),
        QPoint(button.pos().x() + 10, button.pos().y() + 10),
    )

class TestTheButtonHoldsTheMouseForTheWholeGesture:
    """The structural answer to five findings of the same shape.

    Findings .1.6, .1.10, .1.11, .1.14 and .1.18 were all the same accident:
    the release went to some other window, so the gesture was never told it
    had ended, and the state it left behind did damage later. Each was fixed
    by noticing one more way the release could go missing. Holding the mouse
    for the duration removes the cause instead: while a gesture is running,
    Windows delivers every mouse event to this button, so the release cannot
    be delivered anywhere else.

    The checks below are on the button's own record of the hold. A hold on a
    window that was never shown does nothing at the operating-system level,
    and these tests never show one, so what can be verified here is that the
    button asks for the hold and gives it up on every path.
    """

    def test_a_ring_press_holds_the_mouse(self, button):
        _press(button, RING_POINT)

        assert button._mouse_held is True

    def test_a_middle_press_holds_the_mouse(self, button):
        _press(button, CENTRE_POINT)

        assert button._mouse_held is True

    def test_the_release_gives_the_mouse_back(self, button):
        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))

        assert button._mouse_held is False

    def test_the_release_after_a_middle_press_gives_the_mouse_back(self, button):
        _press(button, CENTRE_POINT)
        _release(button, _global_for(button, CENTRE_POINT))

        assert button._mouse_held is False

    def test_the_context_menu_gives_the_mouse_back(self, button):
        """The menu runs its own hold; keeping ours would fight it."""
        _press(button, CENTRE_POINT)
        button.contextMenuEvent(_context_menu_event(button))

        assert button._mouse_held is False

    def test_hiding_the_button_gives_the_mouse_back(self, button):
        """A hold left behind by a window that is gone freezes the desktop."""
        from PySide6.QtGui import QHideEvent

        _press(button, RING_POINT)
        button.hideEvent(QHideEvent())

        assert button._mouse_held is False

    def test_closing_the_button_gives_the_mouse_back(self, button):
        from PySide6.QtGui import QCloseEvent

        _press(button, RING_POINT)
        button.closeEvent(QCloseEvent())

        assert button._mouse_held is False

    def test_a_double_click_on_the_edge_holds_the_mouse(self, button):
        _double_click(button, RING_POINT)

        assert button._mouse_held is True

    def test_a_second_press_does_not_lose_the_hold(self, button):
        """The first gesture ends and the second begins; the hold carries on."""
        _press(button, RING_POINT)
        _press(button, CENTRE_POINT)

        assert button._mouse_held is True

    def test_a_press_before_the_first_state_holds_nothing(self, button):
        button.set_ready_for_gestures(False)

        _press(button, RING_POINT)

        assert button._mouse_held is False


class TestNoGestureBeforeTheFirstStateArrives:
    """The button is on screen before its stored size and position arrive.

    GuiManager.start shows the button and only afterwards asks the Logic
    process for the stored state. A gesture begun in that gap measures against
    the default geometry, and the arriving state then replaces the size and
    position underneath it. The manager already ignores presses until the
    state arrives; the button now does the same for the gestures the manager
    never sees.
    """

    def test_a_ring_press_before_the_first_state_does_nothing(self, button):
        button.set_ready_for_gestures(False)

        _press(button, RING_POINT)

        assert button._is_resizing is False

    def test_a_middle_press_before_the_first_state_does_nothing(self, button):
        started = []
        button.press_started.connect(lambda: started.append(True))
        button.set_ready_for_gestures(False)

        _press(button, CENTRE_POINT)

        assert started == []
        assert button._initial_press_pos is None

    def test_a_double_click_before_the_first_state_does_nothing(self, button):
        seen = []
        button.double_clicked.connect(lambda: seen.append(True))
        button.set_ready_for_gestures(False)

        _double_click(button, CENTRE_POINT)

        assert seen == []

    def test_the_wheel_before_the_first_state_does_nothing(self, button):
        button.set_ready_for_gestures(False)
        before = button.width()

        _ctrl_wheel(button, 2)

        assert button.width() == before

    def test_gestures_work_once_the_state_has_arrived(self, button):
        button.set_ready_for_gestures(False)
        button.set_ready_for_gestures(True)

        _press(button, RING_POINT)

        assert button._is_resizing is True


class TestTheButtonReportsWhenAGestureEnds:
    """One signal for every way a gesture can stop.

    The manager holds back the stored size and position while a gesture is
    running, so it needs to know the moment nothing is running any more --
    including the endings that save nothing, which are the ones that would
    otherwise leave the button somewhere no later message corrects.
    """

    def test_a_release_reports_the_end(self, button):
        seen = []
        button.gesture_ended.connect(lambda: seen.append(True))

        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))
        _release(button, QPointF(385, 325))

        assert seen == [True]

    def test_a_cancelled_press_reports_the_end(self, button):
        seen = []
        button.gesture_ended.connect(lambda: seen.append(True))

        _press(button, CENTRE_POINT)
        button.contextMenuEvent(_context_menu_event(button))

        assert seen == [True]

    def test_a_resize_that_changed_nothing_still_reports_the_end(self, button):
        """The ending that saves nothing is the one that needs reporting."""
        saved = []
        seen = []
        button.resize_finished.connect(lambda w, p: saved.append(w))
        button.gesture_ended.connect(lambda: seen.append(True))

        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))

        assert saved == []
        assert seen == [True]

    def test_nothing_is_reported_when_no_gesture_was_running(self, button):
        seen = []
        button.gesture_ended.connect(lambda: seen.append(True))

        button.contextMenuEvent(_context_menu_event(button))

        assert seen == []

    def test_the_end_is_reported_once_per_gesture(self, button):
        seen = []
        button.gesture_ended.connect(lambda: seen.append(True))

        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))
        button.contextMenuEvent(_context_menu_event(button))

        assert seen == [True]


class TestALostReleaseEndsTheMiddleGestureToo:
    """The backstop behind the hold, for a hold that never took.

    A hold can fail: the window is not on screen yet, or another program holds
    the mouse already. The existing check noticed a resize whose button had
    come up. The press gesture had no such check, so a lost release left the
    press recorded and the hold timer armed, and push-to-talk started with the
    mouse already up.
    """

    def test_a_hover_with_no_button_down_cancels_a_recorded_press(self, button):
        cancelled = []
        button.press_cancelled.connect(lambda: cancelled.append(True))

        _press(button, CENTRE_POINT)
        _move_to(button, QPointF(430, 330), buttons=Qt.MouseButton.NoButton)

        assert button._initial_press_pos is None
        assert cancelled == [True]

    def test_a_hover_with_no_button_down_ends_a_drag(self, button):
        """And saves where the button ended up, rather than cancelling it.

        A drag that has already moved the button on screen is not the same as
        an abandoned press. Cancelling it would clear the same flags but throw
        the new position away, leaving the stored position behind the visible
        one until something else saved it.
        """
        moved = []
        cancelled = []
        button.moved.connect(lambda pos: moved.append(pos))
        button.press_cancelled.connect(lambda: cancelled.append(True))

        _press(button, CENTRE_POINT)
        start = _global_for(button, CENTRE_POINT)
        _move_to(button, QPointF(start.x() + 40, start.y()))
        _move_to(button, QPointF(start.x() + 80, start.y()))
        assert button._is_dragging is True

        _move_to(button, QPointF(start.x() + 90, start.y()), buttons=Qt.MouseButton.NoButton)

        assert button._is_dragging is False
        assert len(moved) == 1
        assert cancelled == []

    def test_a_move_carrying_only_the_right_button_ends_the_gesture(self, button):
        _press(button, CENTRE_POINT)

        _move_to(button, QPointF(430, 330), buttons=Qt.MouseButton.RightButton)

        assert button._initial_press_pos is None

    def test_a_still_held_press_survives_an_ordinary_move(self, button):
        _press(button, CENTRE_POINT)

        _move_to(button, QPointF(430, 330))

        assert button._initial_press_pos is not None


class TestTheSettlingRuleItself:
    """Direct tests of the one place that decides a gesture is over.

    Every ending calls the same method, and some of them call it while another
    gesture is still live -- that is what makes it safe to share between the
    paths that end one gesture and start another in the same breath. Driving
    it through mouse events cannot reach those combinations, so these tests
    call it directly.
    """

    def test_nothing_settles_while_a_resize_is_still_running(self, button):
        seen = []
        button.gesture_ended.connect(lambda: seen.append(True))
        _press(button, RING_POINT)

        button._settle_after_gesture()

        assert seen == []
        assert button._mouse_held is True

    def test_nothing_settles_while_a_press_is_still_recorded(self, button):
        seen = []
        button.gesture_ended.connect(lambda: seen.append(True))
        _press(button, CENTRE_POINT)

        button._settle_after_gesture()

        assert seen == []
        assert button._mouse_held is True

    def test_nothing_settles_while_a_move_is_still_running(self, button):
        seen = []
        button.gesture_ended.connect(lambda: seen.append(True))
        _press(button, CENTRE_POINT)
        start = _global_for(button, CENTRE_POINT)
        _move_to(button, QPointF(start.x() + 40, start.y()))
        _move_to(button, QPointF(start.x() + 80, start.y()))
        assert button._is_dragging is True

        button._settle_after_gesture()

        assert seen == []

    def test_settling_with_no_gesture_running_reports_nothing(self, button):
        seen = []
        button.gesture_ended.connect(lambda: seen.append(True))

        button._settle_after_gesture()

        assert seen == []


class TestTheHoldIsGivenBackNoMatterWhat:
    """A hold left behind makes the whole desktop unclickable.

    That is bad enough that hiding and closing give the mouse back without
    first working out whether a gesture was running. These tests set the hold
    on its own, with no gesture, which is the state those two calls exist for.
    """

    def test_hiding_gives_back_a_hold_with_no_gesture_behind_it(self, button):
        from PySide6.QtGui import QHideEvent

        button._mouse_held = True

        button.hideEvent(QHideEvent())

        assert button._mouse_held is False

    def test_closing_gives_back_a_hold_with_no_gesture_behind_it(self, button):
        from PySide6.QtGui import QCloseEvent

        button._mouse_held = True

        button.closeEvent(QCloseEvent())

        assert button._mouse_held is False


class TestANewButtonRefusesGesturesByDefault:
    """The refusal has to be the starting state, not something switched on.

    Every other test in this file works through the fixture, which grants
    permission the way the manager does on the first state. If the starting
    value were the other way round, every one of them would still pass and the
    gap at startup would be wide open.
    """

    def test_a_button_that_was_told_nothing_refuses_a_resize(self, qapp):
        from gui import FloatingButton

        widget = FloatingButton(initial_size=BUTTON_SIZE)
        widget.move(BUTTON_ORIGIN)
        try:
            _press(widget, RING_POINT)

            assert widget._is_resizing is False
            assert widget._mouse_held is False
        finally:
            widget.deleteLater()

    def test_a_button_that_was_told_nothing_refuses_a_press(self, qapp):
        from gui import FloatingButton

        widget = FloatingButton(initial_size=BUTTON_SIZE)
        widget.move(BUTTON_ORIGIN)
        try:
            _press(widget, CENTRE_POINT)

            assert widget._initial_press_pos is None
        finally:
            widget.deleteLater()


class TestQtIsActuallyAskedForTheGrab:
    """The tests above watch the button's own record, not Qt (.1.23).

    Every other test in this file uses a button that is never put on screen,
    and a window that is not on screen cannot hold the mouse -- so _hold_mouse
    skips the request. That makes those tests blind to the one mutation that
    matters most: deleting the releaseMouse call is what leaves the desktop
    unable to click anything, and the flag would still read False.

    These tests make the button report itself as visible and watch the two Qt
    calls directly. Nothing is shown on screen; only the answer to "are you
    visible" is replaced.
    """

    @pytest.fixture
    def visible_button(self, button):
        with patch.object(type(button), "isVisible", return_value=True), \
             patch.object(type(button), "grabMouse") as grab, \
             patch.object(type(button), "releaseMouse") as release:
            yield button, grab, release

    def test_a_ring_press_asks_qt_for_the_grab(self, visible_button):
        button, grab, _ = visible_button

        _press(button, RING_POINT)

        grab.assert_called_once()

    def test_a_middle_press_asks_qt_for_the_grab(self, visible_button):
        button, grab, _ = visible_button

        _press(button, CENTRE_POINT)

        grab.assert_called_once()

    def test_a_double_click_on_the_edge_asks_qt_for_the_grab(self, visible_button):
        button, grab, _ = visible_button

        _double_click(button, RING_POINT)

        grab.assert_called_once()

    def test_asking_twice_while_already_holding_asks_qt_once(self, visible_button):
        """A repeat request is refused rather than passed on.

        Called directly. Every gesture that starts while another is running
        ends that one first, which hands the grab back, so no sequence of
        mouse events reaches this -- but it is what makes _hold_mouse safe to
        call from each gesture's own starting point without any of them
        checking first.
        """
        button, grab, _ = visible_button

        button._hold_mouse()
        button._hold_mouse()

        grab.assert_called_once()

    def test_the_grab_is_asked_for_once_across_a_whole_gesture(self, visible_button):
        button, grab, _ = visible_button

        _press(button, CENTRE_POINT)
        _move_to(button, QPointF(430, 330))
        _move_to(button, QPointF(470, 330))

        grab.assert_called_once()

    def test_a_button_not_on_screen_never_asks(self, button):
        """Qt prints a warning for a grab it cannot honour."""
        with patch.object(type(button), "isVisible", return_value=False), \
             patch.object(type(button), "grabMouse") as grab:
            _press(button, RING_POINT)

        grab.assert_not_called()

    def test_a_release_hands_the_grab_back_to_qt(self, visible_button):
        button, _, release = visible_button

        _press(button, RING_POINT)
        _release(button, _global_for(button, RING_POINT))

        release.assert_called_once()

    def test_a_release_after_a_middle_press_hands_the_grab_back(self, visible_button):
        button, _, release = visible_button

        _press(button, CENTRE_POINT)
        _release(button, _global_for(button, CENTRE_POINT))

        release.assert_called_once()

    def test_the_context_menu_hands_the_grab_back(self, visible_button):
        """The menu runs its own grab; keeping ours would fight it."""
        button, _, release = visible_button

        _press(button, CENTRE_POINT)
        button.contextMenuEvent(_context_menu_event(button))

        release.assert_called_once()

    def test_hiding_hands_the_grab_back(self, visible_button):
        from PySide6.QtGui import QHideEvent

        button, _, release = visible_button

        _press(button, RING_POINT)
        button.hideEvent(QHideEvent())

        release.assert_called_once()

    def test_closing_hands_the_grab_back(self, visible_button):
        from PySide6.QtGui import QCloseEvent

        button, _, release = visible_button

        _press(button, RING_POINT)
        button.closeEvent(QCloseEvent())

        release.assert_called_once()

    def test_a_lost_release_hands_the_grab_back(self, visible_button):
        """The path the grab exists to prevent, for a grab that did not take."""
        button, _, release = visible_button

        _press(button, CENTRE_POINT)
        _move_to(button, QPointF(430, 330), buttons=Qt.MouseButton.NoButton)

        release.assert_called_once()

    def test_a_gesture_still_running_keeps_the_grab(self, visible_button):
        button, _, release = visible_button

        _press(button, RING_POINT)
        _move_to(button, QPointF(385, 325))

        release.assert_not_called()

    def test_nothing_is_handed_back_when_nothing_was_held(self, visible_button):
        button, _, release = visible_button

        button.contextMenuEvent(_context_menu_event(button))

        release.assert_not_called()
