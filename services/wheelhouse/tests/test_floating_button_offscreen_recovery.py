# Tests for bringing the floating button back when it is stored off screen.
#
# The fault these guard against: the floating button's stored position put it
# outside every visible screen, no control remained to bring it back, and the
# only recovery was editing config.toml by hand (wh-floating-button-offscreen).
#
# The arithmetic lives in floating_button_geometry and is tested separately in
# test_floating_button_geometry.py. These tests cover the connection to it:
# which apply paths correct the position, which of them save the correction,
# and what happens when the screen layout changes underneath a running app.

import json
import logging
import struct
from contextlib import contextmanager
from queue import Empty
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from floating_button_geometry import correct_onto_any_screen

# GuiManager.__init__ builds a real QApplication-dependent dialog whatever else
# a test patches, and without these two fixtures the process dies with no
# output at all (conftest.mock_editor_window, wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")


ONE_SCREEN = [(0, 0, 1920, 1080)]
TWO_SCREENS = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]
# What a Remote Desktop session reconnecting at a smaller size leaves behind,
# and what a display-scale change leaves on a single-screen machine: the same
# monitor, fewer logical pixels across.
SMALL_SCREEN = [(0, 0, 1280, 720)]

# Far outside ONE_SCREEN, the way a position stored on a monitor that is now
# gone reads back. correct_onto_any_screen puts a 50 px button at (1870, 1030).
LOST_POS = [3000, 2000]
LOST_POS_CORRECTED = (1870, 1030)
VISIBLE_POS = [400, 300]
SIZE = 50

# A 1920x1080 screen with a 48 px taskbar along the bottom. The whole screen
# and the area Windows leaves for ordinary windows are different rectangles,
# and which one the correction measures against decides what happens to a
# button parked on the strip.
TASKBAR_SCREEN = (0, 0, 1920, 1080)
TASKBAR_USABLE = (0, 0, 1920, 1032)
# A 50 px button parked on the taskbar next to the tray. It spans y 1030 to
# 1080, so only 50 columns by 2 rows -- 100 px^2 -- lie in the usable area,
# against a threshold of half the button's 2500 px^2, which is 1250 px^2.
TASKBAR_POS = [1860, 1030]
TASKBAR_POS_AGAINST_THE_USABLE_AREA = (1860, 982)

# Where a drag parks the button in the whole-sequence test below: 30 of its 50
# columns hang off the right edge of the one screen, so 20 x 50 = 1000 px^2
# remain visible against the 1250 px^2 threshold, and the correction pulls it
# back to the edge at x = 1920 - 50. The drag itself must NOT clamp -- a user
# is allowed to park the button anywhere, including over the taskbar -- so the
# correction is what arrives afterwards.
DRAGGED_POS = (1900, 500)
DRAGGED_CORRECTED = (1870, 500)


@contextmanager
def _manager_patches():
    """Hold every patch a GuiManager needs, and yield the stand-in button.

    Separate from the fixture because one test has to add a patch of its own
    before the constructor runs, and repeating this list would let the two
    copies drift apart.

    gui.send_notice MUST be patched here. _settings_show_status(failure=True)
    calls it, and it delivers a real Windows notification to whoever is at
    the machine. The conftest guard does not cover it: that guard patches
    utils.speech_notifier.send_notice, while gui.py imports its own name from
    utils.notice_text (gui.py:71). Without this line, every run of
    test_a_refused_write_corrects_without_calling_itself_again put a
    "WheelHouse settings" notice on David's desktop, and a mutation gate
    selecting that test put one there per mutation. test_gui.py's own manager
    fixture patches it for the same reason.
    """
    with patch("gui.FloatingButton") as mock_button_cls, \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"), \
         patch("gui.send_notice"), \
         patch("soft_allow_write_failed_toast.SoftAllowWriteFailedToast"):
        mock_button = MagicMock()
        mock_button_cls.return_value = mock_button
        mock_pystray.Icon.return_value = MagicMock()
        yield mock_button


def _build_manager(mock_button):
    """Construct a GuiManager inside _manager_patches and attach the button."""
    from gui import GuiManager
    shutdown = MagicMock()
    shutdown.is_set.return_value = False
    mgr = GuiManager(shutdown, MagicMock(), MagicMock())
    mgr.button = mock_button
    # A plain MagicMock reports every attribute as truthy, so without this
    # the stand-in button claims to be mid-gesture and every test would
    # read as passing for the wrong reason.
    mock_button._is_resizing = False
    mock_button._is_dragging = False
    mock_button._gesture_running = False
    return mgr


@pytest.fixture
def manager():
    """A GuiManager with a stand-in button and a real QPoint.

    test_gui.py's own fixture patches gui.QPoint, which makes the position the
    button receives unreadable. These tests are about that exact value, so
    QPoint stays real here.

    This fixture YIELDS inside the patches and must keep doing so. A ``return``
    there exits the context manager before the test body runs, and every patch
    with it -- including gui.send_notice, which then delivers a real Windows
    notification to whoever is at the machine. The test below named
    test_the_desktop_notice_is_a_stand_in_during_the_test_body is the guard
    against that mistake coming back.
    """
    with _manager_patches() as mock_button:
        yield _build_manager(mock_button)


@pytest.fixture
def manager_with_a_real_button():
    """A GuiManager whose button is a genuine FloatingButton.

    The whole-drag test needs the real mouse handlers -- the move to the raw
    pointer position and the signal that reports it -- so a stand-in button
    cannot serve there.

    ``gui.FloatingButton`` is read BEFORE _manager_patches replaces it. The
    manager's own constructor still builds the stand-in; only the button
    attached afterwards is real. No test file in this suite imports gui at
    file scope, so capturing the class here is what keeps that rule.
    """
    import gui

    real_button_class = gui.FloatingButton
    with _manager_patches() as mock_button:
        manager = _build_manager(mock_button)
        widget = real_button_class(initial_size=SIZE)
        # The real button refuses gestures until the stored size and position
        # arrive from the Logic process, which is what the manager reports on
        # the first state message.
        widget.set_ready_for_gestures(True)
        widget.move(QPoint(*VISIBLE_POS))
        manager.button = widget
        # The two connections gui.py makes for this sequence (gui.py:1531 and
        # gui.py:1533). Nothing else in the drag reaches the manager.
        widget.moved.connect(manager.send_pos_change_command)
        widget.gesture_ended.connect(manager._on_gesture_ended)
        yield manager
        widget.deleteLater()


def _mouse_event(widget, kind, global_point, button, buttons):
    """Build the QMouseEvent Qt would deliver for a virtual-desktop point."""
    local = QPointF(
        global_point.x() - widget.pos().x(), global_point.y() - widget.pos().y()
    )
    return QMouseEvent(
        kind, local, global_point, button, buttons, Qt.KeyboardModifier.NoModifier
    )


def _drag_from_the_middle(widget, path):
    """Press in the middle, move along ``path``, release at its last point.

    The handlers are called directly rather than posted, so the sequence runs
    without an event loop and without the button ever being shown.
    """
    centre = QPointF(
        widget.pos().x() + widget.width() / 2, widget.pos().y() + widget.height() / 2
    )
    widget.mousePressEvent(
        _mouse_event(
            widget,
            QEvent.Type.MouseButtonPress,
            centre,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    for point in path:
        widget.mouseMoveEvent(
            _mouse_event(
                widget,
                QEvent.Type.MouseMove,
                point,
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
            )
        )
    widget.mouseReleaseEvent(
        _mouse_event(
            widget,
            QEvent.Type.MouseButtonRelease,
            path[-1],
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )


def _drag_through(widget, path, during=None):
    """Press in the middle, move along ``path``, release at its last point.

    ``during`` is called after the first move, while the drag is live and the
    button reports a gesture running -- the moment a screen-layout signal has
    to arrive for the gesture to hold it back.

    _drag_from_the_middle above keeps its own copy of these three calls on
    purpose. The test pinned on it proves a drag past an edge is still kept,
    and the acceptance criteria for wh-floating-button-offscreen.1.2 require
    that test to stay exactly as it is, its helper included.
    """
    centre = QPointF(
        widget.pos().x() + widget.width() / 2, widget.pos().y() + widget.height() / 2
    )
    widget.mousePressEvent(
        _mouse_event(
            widget,
            QEvent.Type.MouseButtonPress,
            centre,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    for index, point in enumerate(path):
        widget.mouseMoveEvent(
            _mouse_event(
                widget,
                QEvent.Type.MouseMove,
                point,
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
            )
        )
        if index == 0 and during is not None:
            during()
    widget.mouseReleaseEvent(
        _mouse_event(
            widget,
            QEvent.Type.MouseButtonRelease,
            path[-1],
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )


def _a_drag_to_the_edge(start):
    """The two pointer positions that carry a button at ``start`` past the edge.

    The offset between pointer and corner is recorded on the first move, so
    the second point is the corner the drag should reach plus that offset.
    """
    return [
        QPointF(start[0] + 125, start[1] + 25),
        QPointF(DRAGGED_POS[0] + 125, DRAGGED_POS[1] + 25),
    ]


def _one_request_id(manager):
    """Return the ID of the single settings write waiting for its answer."""
    pending = list(manager._settings_requests)
    assert len(pending) == 1, f"expected one pending write, got {pending}"
    return pending[0]


def _moved_to(manager):
    """Return the (x, y) the button was last moved to."""
    assert manager.button.move.called, "the button was never moved"
    point = manager.button.move.call_args[0][0]
    return point.x(), point.y()


def _saved_positions(manager):
    """Return every FLOATING_BUTTON_POS value that reached the command queue."""
    saved = []
    for call in manager.commands_to_logic_queue.put_nowait.call_args_list:
        command = call[0][0]
        if command.get("key") == "FLOATING_BUTTON_POS":
            saved.append(command["value"])
        values = command.get("values") or {}
        if "FLOATING_BUTTON_POS" in values:
            saved.append(values["FLOATING_BUTTON_POS"])
    return saved


class TestApplyingAStoredPosition:
    """Every path that puts a stored position on the button goes through one
    method, so correcting there covers the startup message, the settings
    acknowledgement, the deferred-gesture apply and the rollback at once."""

    def test_a_position_on_no_screen_is_corrected_before_the_button_moves(self, manager):
        manager._apply_geometry((SIZE, LOST_POS), screens=ONE_SCREEN)
        assert _moved_to(manager) == LOST_POS_CORRECTED

    def test_a_position_that_is_already_visible_is_applied_unchanged(self, manager):
        manager._apply_geometry((SIZE, VISIBLE_POS), screens=ONE_SCREEN)
        assert _moved_to(manager) == tuple(VISIBLE_POS)

    def test_the_size_is_still_applied_when_the_position_is_corrected(self, manager):
        manager._apply_geometry((SIZE, LOST_POS), screens=ONE_SCREEN)
        manager.button.set_size.assert_called_once_with(SIZE)

    def test_a_corrected_position_is_saved_so_the_config_file_stops_holding_it(self, manager):
        # Without this the button comes back on screen but config.toml keeps
        # the off-screen value, and the correction runs again every start.
        manager._apply_geometry((SIZE, LOST_POS), screens=ONE_SCREEN)
        assert _saved_positions(manager) == [list(LOST_POS_CORRECTED)]

    def test_a_position_needing_no_correction_saves_nothing(self, manager):
        # A save per apply would write config.toml on every state message.
        manager._apply_geometry((SIZE, VISIBLE_POS), screens=ONE_SCREEN)
        assert _saved_positions(manager) == []

    def test_a_button_across_two_monitors_is_left_where_the_user_put_it(self, manager):
        # 20 px on the left monitor and 30 px on the right: fully visible.
        manager._apply_geometry((SIZE, [1900, 500]), screens=TWO_SCREENS)
        assert _moved_to(manager) == (1900, 500)
        assert _saved_positions(manager) == []


class TestEveryCallerReachesTheCorrectionOnOneScreen:
    """The same correction, driven through each real caller, on ONE screen.

    The user who reported the fault had a single monitor, so the two-monitor
    story never applied to him: a position stored when the screen was larger,
    carried over from another machine, or left behind by a display-scale
    change puts the button off the only screen there is. The class above
    calls _apply_geometry directly and proves the correction works; these
    prove each caller arrives at it.

    update_ui_state is replaced in the tests that reach it. It repaints the
    tray icon and the button, which is unrelated to where the button sits,
    and test_gui.py's own state-message tests replace it for the same reason.
    """

    def test_the_first_state_message_corrects_a_position_off_the_only_screen(
        self, manager
    ):
        """The start-up apply (gui.py:1847), through the real queue poll.

        This is the case the user sees: WheelHouse starts, the Logic process
        sends the stored geometry, and the button appears where no screen is.
        """
        message = {
            "action": "initial_state",
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": LOST_POS,
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [message, Empty()]
        with patch("gui._screen_bounds", return_value=ONE_SCREEN), \
             patch.object(manager, "update_ui_state"):
            manager._check_queues_and_events()
        assert _moved_to(manager) == LOST_POS_CORRECTED
        assert _saved_positions(manager) == [list(LOST_POS_CORRECTED)]

    def test_a_settings_acknowledgement_corrects_a_position_off_the_only_screen(
        self, manager
    ):
        """The apply inside _handle_settings_result (gui.py:3214).

        Registered through the real _send_settings_command, so the request ID
        and the pending bookkeeping are the ones the acknowledgement has to
        match, and the reply echoes the written key the way state_manager
        does (state_manager.py:1202-1206).
        """
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        with patch("gui._screen_bounds", return_value=ONE_SCREEN), \
             patch.object(manager, "update_ui_state"):
            manager._send_settings_command(
                {"action": "set_config_value",
                 "key": "FLOATING_BUTTON_POS", "value": LOST_POS}
            )
            manager._handle_settings_result({
                "action": "config_write_result",
                "request_id": _one_request_id(manager),
                "saved": True,
                "values": {"FLOATING_BUTTON_POS": LOST_POS},
            })
        assert _moved_to(manager) == LOST_POS_CORRECTED
        assert _saved_positions(manager) == [LOST_POS, list(LOST_POS_CORRECTED)]

    def test_an_apply_held_back_for_a_gesture_corrects_when_the_gesture_ends(
        self, manager
    ):
        """The deferred apply (gui.py:3311), held back and then released.

        Both halves run here: the state message arrives mid-gesture and moves
        nothing, and the gesture ending applies what was held. Setting
        _deferred_geometry by hand would skip the half that decides what is
        held, and a stored position that is off screen is exactly what a
        gesture can delay.
        """
        manager.button._gesture_running = True
        message = {
            "action": "state_update",
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": LOST_POS,
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [message, Empty()]
        with patch("gui._screen_bounds", return_value=ONE_SCREEN), \
             patch.object(manager, "update_ui_state"):
            manager.initial_state_received = True
            manager._check_queues_and_events()
            manager.button.move.assert_not_called()
            assert manager._deferred_geometry == (SIZE, LOST_POS)

            manager.button._gesture_running = False
            manager._on_gesture_ended()
        assert _moved_to(manager) == LOST_POS_CORRECTED

    def test_a_drag_past_the_edge_is_kept_then_corrected_then_saved(
        self, manager_with_a_real_button
    ):
        """The whole sequence, with a genuine button and real mouse events.

        A drag moves the button to the raw pointer position and reports that
        position verbatim (gui.py:880, gui.py:866 and gui.py:897). Nothing
        clamps it, on purpose: a user must be able to park the button
        anywhere, including over the taskbar. The correction is what arrives
        afterwards, when the write comes back acknowledged.
        """
        manager = manager_with_a_real_button
        widget = manager.button
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": list(VISIBLE_POS),
        }

        # The first move only starts the drag; the second carries the button
        # to DRAGGED_POS. The offset between pointer and corner is recorded
        # on the first one, so the second point is that corner plus it.
        _drag_from_the_middle(widget, [
            QPointF(VISIBLE_POS[0] + 125, VISIBLE_POS[1] + 25),
            QPointF(DRAGGED_POS[0] + 125, DRAGGED_POS[1] + 25),
        ])
        assert widget.pos() == QPoint(*DRAGGED_POS), (
            "the drag itself moved the button somewhere other than the "
            "pointer; parking it past an edge must stay possible"
        )
        assert _saved_positions(manager) == [list(DRAGGED_POS)]

        with patch("gui._screen_bounds", return_value=ONE_SCREEN), \
             patch.object(manager, "update_ui_state"):
            manager._handle_settings_result({
                "action": "config_write_result",
                "request_id": _one_request_id(manager),
                "saved": True,
                "values": {"FLOATING_BUTTON_POS": list(DRAGGED_POS)},
            })
        assert widget.pos() == QPoint(*DRAGGED_CORRECTED), (
            "the acknowledged position was applied uncorrected, so the button "
            "stayed where the drag left it, mostly past the edge"
        )
        assert _saved_positions(manager) == [
            list(DRAGGED_POS), list(DRAGGED_CORRECTED)
        ], "config.toml keeps the position the drag left, so the next start "\
           "puts the button back past the edge"


class TestAButtonParkedOnTheTaskbar:
    """The taskbar is an edge users park the button on, so the correction
    must measure the button against the whole screen.

    The floating button is an always-on-top frameless window: it paints over
    the taskbar and takes clicks there, and parking a small overlay next to
    the tray is ordinary use. The area Windows reserves for ordinary windows
    says nothing about whether this button is visible.

    The numbers, for the case below: a 1920x1080 screen with a 48 px taskbar
    along the bottom leaves (0, 0, 1920, 1032) usable. A 50 px button parked
    at (1860, 1030) puts 100 px^2 in that usable area against a threshold of
    1250 px^2, so measured against it the button is corrected to (1860, 982)
    -- and every apply but the rollback then writes that moved position back
    to the settings, so the next start moves it again. Measured against the
    whole screen the button is fully visible and nothing happens at all.
    """

    def test_a_position_on_the_taskbar_is_left_where_the_user_put_it(self, manager):
        """The real _screen_bounds runs here, over a screen that answers the
        two geometry questions differently. Which one the seam asks is the
        whole of what this test measures: ask for the usable area and the
        assertions below both break."""
        screen = _FakeScreen(*TASKBAR_SCREEN, usable=TASKBAR_USABLE)
        with patch("gui.QGuiApplication") as app:
            app.screens.return_value = [screen]
            manager._apply_geometry((SIZE, TASKBAR_POS))
        assert _moved_to(manager) == tuple(TASKBAR_POS), (
            "a button the user parked on the taskbar was moved off it; "
            "measured against the usable area this position corrects to "
            f"{TASKBAR_POS_AGAINST_THE_USABLE_AREA}"
        )
        assert _saved_positions(manager) == [], (
            "the moved position was written back to the settings, so the "
            "user's parking place is gone from config.toml as well"
        )


class TestTheResizeClampMeasuresTheSameRectangle:
    """The edge-drag resize has a correction of its own, and the two must
    agree about where a button may sit.

    _apply_resize_to corrects against the bounds recorded when the drag began
    (gui.py:793-794), and _current_screen_bounds is what records them. While
    that method asked for the usable area, the two rules disagreed about the
    same button: growing a button parked on the taskbar pulled it up off the
    strip, and the next apply put it straight back. The numbers, for the case
    below: a 50 px button at (1860, 1030) resized back to 50 px keeps its
    corner at (1860, 1030) against the whole screen, and lands at
    (1860, 982) against the area a 48 px taskbar leaves.
    """

    def _button(self):
        """A real FloatingButton on a screen with a taskbar along the bottom.

        gui.FloatingButton is NOT patched in this class -- nothing here uses
        the manager fixture -- so this is the shipped widget.
        """
        import gui

        widget = gui.FloatingButton(initial_size=SIZE)
        widget.move(QPoint(*TASKBAR_POS))
        widget.screen = lambda: _FakeScreen(*TASKBAR_SCREEN, usable=TASKBAR_USABLE)
        return widget

    def test_the_bounds_recorded_at_the_press_are_the_whole_screen(self):
        widget = self._button()
        try:
            assert widget._current_screen_bounds() == TASKBAR_SCREEN, (
                "the resize measures against the area left for ordinary "
                "windows, which the always-on-top button is not"
            )
        finally:
            widget.deleteLater()

    def test_a_resize_does_not_pull_the_button_off_the_taskbar(self):
        """The recorded bounds reaching the correction, not just their value."""
        widget = self._button()
        try:
            widget._begin_resize()
            # 25 px below the centre at (1885, 1055), so the diameter comes
            # back to the 50 it started at and only the correction can move
            # the corner.
            widget._apply_resize_to(QPoint(1885, 1080))
            assert widget.width() == SIZE
            assert widget.pos() == QPoint(*TASKBAR_POS), (
                "the resize pulled a button off the taskbar; measured against "
                f"the usable area it lands at {TASKBAR_POS_AGAINST_THE_USABLE_AREA}"
            )
        finally:
            widget.deleteLater()


class TestNoTestSendsARealDesktopNotice:
    """_settings_show_status(failure=True) calls gui.send_notice, which puts a
    real Windows notification in front of whoever is at the machine.

    This happened. Every run of the rollback test below showed David a
    "WheelHouse settings" notice, and a mutation gate selecting that test
    showed him one per mutation. Two separate mistakes caused it: the manager
    fixture did not patch gui.send_notice at all, and it used ``return``
    inside its patches, which ended every patch before the test body ran.

    The conftest guard does not cover this. It patches
    utils.speech_notifier.send_notice; gui.py imports its own name from
    utils.notice_text at gui.py:71, and a patch of one name never touches the
    other.
    """

    def test_the_desktop_notice_is_a_stand_in_during_the_test_body(self, manager):
        """Fails if the fixture returns instead of yielding, or drops the
        gui.send_notice patch. Either mistake sends real notices again."""
        import gui

        assert isinstance(gui.send_notice, MagicMock), (
            "gui.send_notice is the real function during a test body; this "
            "run is delivering Windows notifications to the desktop"
        )

    def test_the_refused_write_really_reaches_the_notice(self, manager):
        """Proves the test above guards a path these tests actually take.

        Without this, the assertion above could pass while nothing in the
        file ever called gui.send_notice, and the guard would be measuring
        nothing.
        """
        import gui
        from queue import Full

        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        manager.commands_to_logic_queue.put_nowait.side_effect = Full()
        with patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._send_settings_command(
                {"action": "set_config_value", "key": "FLOATING_BUTTON_POS", "value": VISIBLE_POS}
            )
        assert gui.send_notice.call_args[0][0] == "WheelHouse settings"


class TestTheRollbackDoesNotSave:
    """The queue-failure rollback corrects the button but must not save.

    _send_settings_command calls _apply_geometry when the command queue
    refuses the write (gui.py, the except around put_nowait). Saving the
    correction from there would send another command down the same refusing
    queue, which would fail, roll back, correct, and save again without end.
    """

    def test_the_rollback_still_corrects_the_button(self, manager):
        manager._apply_geometry((SIZE, LOST_POS), save_correction=False, screens=ONE_SCREEN)
        assert _moved_to(manager) == LOST_POS_CORRECTED

    def test_the_rollback_saves_nothing(self, manager):
        manager._apply_geometry((SIZE, LOST_POS), save_correction=False, screens=ONE_SCREEN)
        assert _saved_positions(manager) == []

    def test_a_refused_write_corrects_without_calling_itself_again(self, manager):
        """The real route, not a direct call: prove the loop cannot start."""
        from queue import Full

        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": LOST_POS,
        }
        manager.commands_to_logic_queue.put_nowait.side_effect = Full()
        with patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._send_settings_command(
                {"action": "set_config_value", "key": "FLOATING_BUTTON_POS", "value": LOST_POS}
            )
        # One refused write, and no second command chasing it.
        assert manager.commands_to_logic_queue.put_nowait.call_count == 1
        assert _moved_to(manager) == LOST_POS_CORRECTED


class TestTheScreenLayoutChanging:
    """A monitor unplugged, or a Remote Desktop session at another size,
    changes the screens under a position that was fine when it was stored."""

    def test_a_layout_change_re_applies_the_stored_geometry(self, manager):
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": LOST_POS,
        }
        with patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
        assert _moved_to(manager) == LOST_POS_CORRECTED

    def test_a_layout_change_that_needs_no_correction_moves_nothing_new(self, manager):
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        with patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
        assert _moved_to(manager) == tuple(VISIBLE_POS)
        assert _saved_positions(manager) == []

    def test_a_removed_screen_is_left_out_of_the_new_layout(self, manager):
        """Qt emits screenRemoved with the screen it is about to drop.

        Whether QGuiApplication.screens() still lists it at that moment is
        Qt's business. Excluding the object the signal carries means the
        correction never depends on the answer.
        """
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": [2500, 500],
        }
        going = MagicMock()
        going.geometry.return_value = _rect(1920, 0, 1920, 1080)
        staying = MagicMock()
        staying.geometry.return_value = _rect(0, 0, 1920, 1080)
        with patch("gui.QGuiApplication") as app:
            app.screens.return_value = [staying, going]
            manager._on_screen_removed(going)
        # With the second monitor still counted, (2500, 500) needs no
        # correction at all, so this value proves the exclusion happened.
        assert _moved_to(manager) == (1870, 500)

    def test_a_layout_change_during_a_gesture_is_held_back(self, manager):
        """Correcting mid-drag would fight the pointer the user is holding."""
        manager.button._gesture_running = True
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": LOST_POS,
        }
        with patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
        manager.button.move.assert_not_called()
        assert manager._deferred_geometry == (SIZE, LOST_POS)


class TestALayoutChangeThatTheGestureWouldOtherwiseDiscard:
    """A layout signal during a drag or a resize must still reach the button.

    _on_screens_changed holds its correction back while a gesture runs, by
    storing the confirmed settings in _deferred_geometry. But the gesture's
    own completion clears exactly that: mouseReleaseEvent emits moved and
    _finish_resize emits resize_finished BEFORE either one calls
    _settle_after_gesture, and both manager slots set _deferred_geometry to
    None. So _on_gesture_ended used to find nothing, and the correction the
    layout change asked for was lost (wh-floating-button-offscreen.1.2).

    The recovery is a flag of its own, separate from _deferred_geometry
    because the gesture's completion clears that one. What it applies is the
    button's JUST-DRAGGED geometry -- its current width and current position
    -- and never the confirmed settings, because the user has just moved the
    button and those settings are a moment out of date.

    It does not save. The gesture's own write is already in flight and its
    acknowledgement corrects and saves at gui.py:3229, so a second saved
    write here would be one write chasing another.
    """

    def _confirmed(self, manager):
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": list(VISIBLE_POS),
        }

    def test_a_layout_change_during_a_drag_corrects_when_the_drag_ends(
        self, manager_with_a_real_button
    ):
        """The drag path: moved fires first and clears the held-back apply."""
        import gui

        manager = manager_with_a_real_button
        widget = manager.button
        self._confirmed(manager)

        with patch("gui._screen_bounds", return_value=ONE_SCREEN), \
             patch.object(manager, "update_ui_state"):
            _drag_through(
                widget,
                _a_drag_to_the_edge(VISIBLE_POS),
                during=manager._on_screens_changed,
            )

        assert widget.pos() == QPoint(*DRAGGED_CORRECTED), (
            "the layout change that arrived mid-drag was discarded by the "
            "drag's own completion, so the button stayed at "
            f"{(widget.pos().x(), widget.pos().y())}, past the edge of the "
            "only screen there is"
        )
        assert _saved_positions(manager) == [list(DRAGGED_POS)], (
            "the correction sent a second saved write; the gesture's own "
            "write is already in flight and its acknowledgement saves the "
            "correction, so these two would race for the last word"
        )
        gui.send_notice.assert_not_called()

    def test_a_layout_change_during_a_resize_corrects_when_the_resize_ends(
        self, manager_with_a_real_button
    ):
        """The resize path: resize_finished fires first and clears it.

        The resize corrects against the bounds recorded when the drag began
        (gui.py:793-794), so the grown button lands inside the screen that
        was there at the press. It is the NEW, smaller layout that leaves it
        off screen, and only the held-back correction reports that.
        """
        manager = manager_with_a_real_button
        widget = manager.button
        self._confirmed(manager)
        # The connection gui.py makes for a resize (gui.py:1547); the fixture
        # wires the drag's pair, not this one.
        widget.resize_finished.connect(manager.send_resize_commit_command)
        widget.screen = lambda: _FakeScreen(*ONE_SCREEN[0])
        widget.move(QPoint(1800, 1000))

        widget._begin_resize()
        with patch("gui._screen_bounds", return_value=SMALL_SCREEN):
            manager._on_screens_changed()
        # 40 px below and right of the centre, so the button grows and both
        # its size and its corner change -- which is what makes
        # _finish_resize emit at all.
        widget._apply_resize_to(
            QPoint(
                widget.pos().x() + widget.width() // 2 + 40,
                widget.pos().y() + widget.height() // 2 + 40,
            )
        )
        parked = (widget.pos().x(), widget.pos().y())
        grown = widget.width()
        expected = correct_onto_any_screen(parked[0], parked[1], grown, SMALL_SCREEN)
        assert expected != parked, (
            "the set-up leaves the resized button on the new layout already, "
            "so the assertion below would hold with no correction at all"
        )

        with patch("gui._screen_bounds", return_value=SMALL_SCREEN), \
             patch.object(manager, "update_ui_state"):
            widget._finish_resize()

        assert widget.pos() == QPoint(*expected), (
            "the layout change that arrived mid-resize was discarded by the "
            f"resize's own completion, so the button stayed at {parked} with "
            f"the screens now {SMALL_SCREEN}"
        )
        assert widget.width() == grown, (
            "the correction changed the size the user just dragged to"
        )

    def test_a_write_that_is_never_answered_still_leaves_the_button_on_screen(
        self, manager_with_a_real_button
    ):
        """The one path to harm, end to end.

        A healthy Logic process answers the gesture's write and the
        acknowledgement corrects the position at gui.py:3229. This is the
        case where it never answers: the reconciliation retries twice on a
        five-second deadline and then retires the request through
        _settings_fail_request (gui.py:3242), which shows a failure and
        applies nothing. Without the held-back correction the button is left
        where the drag put it, off every screen, until a restart.
        """
        import gui

        manager = manager_with_a_real_button
        widget = manager.button
        self._confirmed(manager)

        with patch("gui._screen_bounds", return_value=ONE_SCREEN), \
             patch.object(manager, "update_ui_state"):
            _drag_through(
                widget,
                _a_drag_to_the_edge(VISIBLE_POS),
                during=manager._on_screens_changed,
            )
            request_id = _one_request_id(manager)
            # Four passes: three retries and the retirement. The deadline is
            # pushed into the past each time rather than the clock forward,
            # so nothing here depends on real elapsed time.
            for _ in range(4):
                if request_id not in manager._settings_requests:
                    break
                manager._settings_requests[request_id]["deadline"] = 0.0
                manager._check_settings_timeout()

        assert request_id not in manager._settings_requests, (
            "the write was never retired, so this run does not reach "
            "_settings_fail_request and measures nothing"
        )
        assert gui.send_notice.call_args_list[-1][0][1] == (
            "Couldn't confirm the settings. They may not have been saved."
        ), "the last notice is not the one _settings_fail_request sends"
        position = (widget.pos().x(), widget.pos().y())
        assert correct_onto_any_screen(
            position[0], position[1], widget.width(), ONE_SCREEN
        ) == position, (
            f"the button ended at {position}, which is off every screen in "
            f"{ONE_SCREEN}; nothing else moves it until the next start"
        )
        assert position == DRAGGED_CORRECTED

    def test_a_later_gesture_with_no_layout_signal_is_left_where_it_lands(
        self, manager_with_a_real_button
    ):
        """The flag is consumed, so it corrects that gesture and no other.

        Parking the button past an edge, or over the taskbar, is deliberate
        and must stay possible. A flag that is set and never cleared would
        correct every gesture after the first layout change for as long as
        the program runs.
        """
        manager = manager_with_a_real_button
        widget = manager.button
        self._confirmed(manager)

        with patch("gui._screen_bounds", return_value=ONE_SCREEN), \
             patch.object(manager, "update_ui_state"):
            _drag_through(
                widget,
                _a_drag_to_the_edge(VISIBLE_POS),
                during=manager._on_screens_changed,
            )
            assert widget.pos() == QPoint(*DRAGGED_CORRECTED), (
                "the first drag was not corrected at all, so the second one "
                "below cannot show whether the flag was consumed"
            )
            # A second drag, with no layout signal anywhere in it.
            _drag_through(widget, _a_drag_to_the_edge(DRAGGED_CORRECTED))

        assert widget.pos() == QPoint(*DRAGGED_POS), (
            "a drag with no layout signal was corrected anyway, so the flag "
            "the earlier layout change set was never cleared; a user can no "
            "longer park the button past an edge"
        )


class TestTheScreenBoundsSeam:
    """_screen_bounds turns Qt's screen list into the rectangles the
    arithmetic takes, and is the one place tests replace."""

    def test_it_reports_each_screen_as_x_y_width_height(self):
        import gui

        first = MagicMock()
        first.geometry.return_value = _rect(0, 0, 1920, 1080)
        second = MagicMock()
        second.geometry.return_value = _rect(1920, -200, 2560, 1440)
        with patch("gui.QGuiApplication") as app:
            app.screens.return_value = [first, second]
            assert gui._screen_bounds() == [
                (0, 0, 1920, 1080),
                (1920, -200, 2560, 1440),
            ]

    def test_it_leaves_out_a_screen_it_is_told_to_exclude(self):
        import gui

        first = MagicMock()
        first.geometry.return_value = _rect(0, 0, 1920, 1080)
        second = MagicMock()
        second.geometry.return_value = _rect(1920, 0, 1920, 1080)
        with patch("gui.QGuiApplication") as app:
            app.screens.return_value = [first, second]
            assert gui._screen_bounds(exclude=second) == [(0, 0, 1920, 1080)]


def _rect(x, y, width, height):
    """A stand-in for QRect that answers the four accessors used here."""
    rect = MagicMock()
    rect.x.return_value = x
    rect.y.return_value = y
    rect.width.return_value = width
    rect.height.return_value = height
    return rect


class TestTheTestsAgreeWithTheArithmetic:
    """The expected values above are computed, not copied, so a change to the
    shared function cannot leave these tests asserting stale numbers."""

    def test_the_lost_position_really_corrects_to_the_value_used_above(self):
        assert correct_onto_any_screen(
            LOST_POS[0], LOST_POS[1], SIZE, ONE_SCREEN
        ) == LOST_POS_CORRECTED

    def test_the_visible_position_really_needs_no_correction(self):
        assert correct_onto_any_screen(
            VISIBLE_POS[0], VISIBLE_POS[1], SIZE, ONE_SCREEN
        ) == tuple(VISIBLE_POS)

    def test_the_dragged_position_really_corrects_to_the_value_used_above(self):
        assert correct_onto_any_screen(
            DRAGGED_POS[0], DRAGGED_POS[1], SIZE, ONE_SCREEN
        ) == DRAGGED_CORRECTED


class _RecordingSignal:
    """A stand-in for a Qt signal that remembers what is connected to it."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)


class _FakeRect:
    """A stand-in for the QRect the two geometry signals carry.

    Deliberately not a MagicMock and deliberately not iterable, for the same
    reason as _FakeScreen below.
    """

    def __init__(self, x, y, width, height):
        self.values = (x, y, width, height)


class _FakeScreen:
    """A stand-in for QScreen, deliberately NOT a MagicMock.

    A real QScreen is not iterable, and neither is this. A MagicMock IS
    iterable -- it yields nothing -- so a defect that handed this object to a
    screens list would fail later on an empty sequence instead of on the
    object itself, and the test would be red for the wrong reason.

    It answers both geometry questions, and ``usable`` is what makes the two
    answers differ: pass the area a taskbar leaves behind and the object can
    show which of the two a caller asked for. Left out, both answers are the
    whole screen, which is a screen with nothing docked on it.
    """

    def __init__(self, x, y, width, height, usable=None):
        self.geometryChanged = _RecordingSignal()
        self.availableGeometryChanged = _RecordingSignal()
        self.logicalDotsPerInchChanged = _RecordingSignal()
        self._rectangle = _rect(x, y, width, height)
        self._usable = _rect(*usable) if usable is not None else self._rectangle

    def geometry(self):
        return self._rectangle

    def availableGeometry(self):
        return self._usable


class _FakeApp:
    """A stand-in for QGuiApplication.instance() with recording signals."""

    def __init__(self):
        self.screenAdded = _RecordingSignal()
        self.screenRemoved = _RecordingSignal()
        self.primaryScreenChanged = _RecordingSignal()


def _watch_the_layout(manager, screens):
    """Run the real _watch_the_screen_layout against stand-in screens."""
    app = _FakeApp()
    with patch("gui.QGuiApplication") as qt_application:
        qt_application.instance.return_value = app
        qt_application.screens.return_value = list(screens)
        manager._watch_the_screen_layout()
    return app


def _only_slot(signal):
    """Return the one slot connected to a signal, and prove it is one."""
    assert len(signal.slots) == 1, f"expected one connected slot, got {signal.slots}"
    return signal.slots[0]


class TestEveryLayoutSignalReachesASlotThatFitsIt:
    """Six signals report a layout change. Four of them reach a slot that
    discards the argument they carry, and those four carry three different
    kinds: a QScreen, a QRect, and a number. A slot whose first parameter is
    the screen LIST would read whatever its signal carries as that list, so
    the correction would run against a QScreen, a QRect or a number instead
    of the screens. Nothing else in this file drives the connections, so
    without these tests the six connections are not exercised at all.
    """

    def _lost(self, manager):
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": LOST_POS,
        }

    def test_a_primary_screen_change_corrects_the_stored_position(self, manager):
        """primaryScreenChanged carries the new primary QScreen."""
        self._lost(manager)
        screen = _FakeScreen(0, 0, 1920, 1080)
        app = _watch_the_layout(manager, [screen])
        with patch("gui._screen_bounds", return_value=ONE_SCREEN):
            _only_slot(app.primaryScreenChanged)(screen)
        assert _moved_to(manager) == LOST_POS_CORRECTED

    def test_a_screen_resize_corrects_the_stored_position(self, manager):
        """geometryChanged carries a QRect.

        This pair is why each screen is watched on its own. The case it
        guards against is a Remote Desktop reconnect that brings the same
        screen back at a different size, with none added and none removed,
        and Qt has no application-level signal for that. Whether Windows and
        Qt report a reconnect that way is not measured, so this connection is
        made rather than left to the application-level signals.
        """
        self._lost(manager)
        screen = _FakeScreen(0, 0, 1920, 1080)
        _watch_the_layout(manager, [screen])
        with patch("gui._screen_bounds", return_value=ONE_SCREEN):
            _only_slot(screen.geometryChanged)(_FakeRect(0, 0, 1280, 720))
        assert _moved_to(manager) == LOST_POS_CORRECTED

    def test_a_usable_area_change_corrects_the_stored_position(self, manager):
        """availableGeometryChanged carries a QRect as well."""
        self._lost(manager)
        screen = _FakeScreen(0, 0, 1920, 1080)
        _watch_the_layout(manager, [screen])
        with patch("gui._screen_bounds", return_value=ONE_SCREEN):
            _only_slot(screen.availableGeometryChanged)(_FakeRect(0, 0, 1920, 1040))
        assert _moved_to(manager) == LOST_POS_CORRECTED

    def test_a_display_scale_change_corrects_the_stored_position(self, manager):
        """logicalDotsPerInchChanged carries a float, the new scale.

        The stored position is in the logical pixels QWidget.move takes, and
        a change of scale can redefine them: the same monitor is fewer
        logical pixels across at 150 per cent than at 125, so a position near
        the right edge can end up past the new one. Whether Windows also
        fires one of the two geometry signals on a change of scale is not
        measured, so this test pins the third connection on its own instead
        of letting one of the others stand for it. A repeated signal computes
        the same correction from the same stored position, so the button ends
        where the first one put it.
        """
        self._lost(manager)
        screen = _FakeScreen(0, 0, 1920, 1080)
        _watch_the_layout(manager, [screen])
        with patch("gui._screen_bounds", return_value=ONE_SCREEN):
            _only_slot(screen.logicalDotsPerInchChanged)(144.0)
        assert _moved_to(manager) == LOST_POS_CORRECTED

    def test_an_added_screen_is_watched_and_the_position_re_checked(self, manager):
        """screenAdded carries the new QScreen, and that one needs watching
        too -- a monitor that arrives and is later resized would otherwise
        report nothing."""
        self._lost(manager)
        first = _FakeScreen(0, 0, 1920, 1080)
        app = _watch_the_layout(manager, [first])
        arriving = _FakeScreen(1920, 0, 1920, 1080)
        with patch("gui._screen_bounds", return_value=ONE_SCREEN):
            _only_slot(app.screenAdded)(arriving)
        assert _moved_to(manager) == LOST_POS_CORRECTED
        assert len(arriving.geometryChanged.slots) == 1
        assert len(arriving.availableGeometryChanged.slots) == 1
        assert len(arriving.logicalDotsPerInchChanged.slots) == 1

    def test_a_removed_screen_reaches_the_slot_that_leaves_it_out(self, manager):
        """screenRemoved carries the screen Qt is dropping, and that one is
        the argument the correction must USE rather than discard."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": [2500, 500],
        }
        staying = _FakeScreen(0, 0, 1920, 1080)
        going = _FakeScreen(1920, 0, 1920, 1080)
        app = _watch_the_layout(manager, [staying, going])
        with patch("gui.QGuiApplication") as qt_application:
            qt_application.screens.return_value = [staying, going]
            _only_slot(app.screenRemoved)(going)
        # With the second monitor still counted, (2500, 500) needs no
        # correction at all, so this value proves the exclusion happened.
        assert _moved_to(manager) == (1870, 500)

    def test_a_new_manager_starts_watching_the_screen_layout(self):
        """Nothing else connects these signals. A GuiManager that does not do
        it at start-up never reacts to a layout change at all."""
        import gui

        with _manager_patches() as mock_button, \
             patch.object(gui.GuiManager, "_watch_the_screen_layout") as watching:
            _build_manager(mock_button)
        watching.assert_called_once_with()

    def test_no_qt_application_yet_is_reported_as_nothing_at_all(self, manager, caplog):
        """Nothing can be showing a button before the application exists, so
        there is no position to protect and nothing to report.

        The log is what makes this measurable. Without the early return the
        connections run against None, the surrounding except catches the
        AttributeError, and every start writes an exception into the log for
        a state that is not a fault.
        """
        caplog.set_level(logging.ERROR)
        with patch("gui.QGuiApplication") as qt_application:
            qt_application.instance.return_value = None
            manager._watch_the_screen_layout()
        assert caplog.records == []


# What a stale native rectangle looks like. The button is stored at
# VISIBLE_POS (400, 300) at SIZE 50, and the screen now reports a device
# pixel ratio of 2.0, so Windows should hold a 100 px window at (800, 600).
# These are the numbers probe 4 measured, scaled to this test's button:
# Windows keeps the window where the OLD ratio of 3.0 put it, and keeps the
# old physical size with it.
AGREEING_NATIVE = (800, 600, 900, 700)
STALE_NATIVE = (1200, 900, 1350, 1050)
NEW_RATIO = 2.0


def _windows_reports(manager, native, ratio=NEW_RATIO, pos=VISIBLE_POS, size=SIZE):
    """Make the stand-in button report a native rectangle and a screen scale.

    The only stand-in here is the Windows call itself. Everything else is the
    shipped code reading the button it was given: its window handle, its
    logical position, its width, and the scale of the screen it is on.
    """
    manager.button.winId.return_value = 4242
    manager.button.pos.return_value = QPoint(*pos)
    manager.button.width.return_value = size
    screen = manager.button.windowHandle.return_value.screen.return_value
    screen.devicePixelRatio.return_value = ratio
    return patch("win32gui.GetWindowRect", return_value=native)


def _idle_activity_payload():
    """The bytes _check_activity_shm expects to find in the segment.

    A four-byte big-endian length followed by that many bytes of UTF-8 JSON
    (gui.py:1659-1664). The handler returns early when the length is 0 or
    above 200, so a tick reaches its real work only with a payload like this
    one.
    """
    body = json.dumps({"state": "idle", "utterance_id": -1}).encode("utf-8")
    return struct.pack(">I", len(body)) + body


class _FakeSharedMemory:
    """A stand-in for the GUI shared-memory segment the activity tick reads.

    ``buf`` is a bytearray because that is the whole of what
    _check_activity_shm asks of it: it slices ``buf[:4]`` and
    ``buf[4:4+size]`` and calls ``bytes()`` on the second slice.
    """

    def __init__(self, payload):
        self.buf = bytearray(payload)


class TestForcingTheWindowBackAfterADisplayChange:
    """Qt sends nothing to Windows when a move() carries the geometry it has
    already cached, so re-applying the stored position cannot repair a window
    whose physical rectangle Windows changed underneath it.

    Probe 4 measured both halves on 2026-09-20. The fault: after a resolution
    change the button's native rectangle stayed at the old physical position
    and the old physical size, wholly outside the new desktop, while Qt still
    reported the stored logical position and every repeated move() to it did
    nothing. The remedy: a move to a DIFFERENT position reaches Windows, and
    the setGeometry after it lands the wanted position and the wanted size.
    """

    def test_a_disagreeing_native_rectangle_is_forced_back(self, manager):
        """Criterion B1. The trigger is the disagreement; the repair is the
        pair of calls, in that order."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        with _windows_reports(manager, STALE_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
        moves = [call[0][0] for call in manager.button.move.call_args_list]
        away = moves[-1]
        assert (away.x(), away.y()) == (VISIBLE_POS[0] + 1, VISIBLE_POS[1] + 1), (
            "the move before the setGeometry must carry a position Qt has not"
            " cached, or Qt sends nothing to Windows at all"
        )
        manager.button.setGeometry.assert_called_once_with(
            VISIBLE_POS[0], VISIBLE_POS[1], SIZE, SIZE)

    def test_the_comparison_uses_the_screen_ratio_not_the_window_ratio(self, manager):
        """Criterion B1. Probe 4 measured the window's own ratio lag behind:
        three display signals in a row read the window at 3.0 while the screen
        already read 2.0. A comparison against the window's ratio reports
        agreement at the exact moment the fault exists, so the window's ratio
        must not decide anything."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        with _windows_reports(manager, STALE_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            # Under the window's stale ratio of 3.0 the stale rectangle
            # agrees exactly. Only the screen's 2.0 exposes the fault.
            manager.button.windowHandle.return_value.devicePixelRatio.return_value = 3.0
            manager._on_screens_changed()
        manager.button.setGeometry.assert_called_once_with(
            VISIBLE_POS[0], VISIBLE_POS[1], SIZE, SIZE)

    def test_a_size_that_disagrees_alone_is_forced_back(self, manager):
        """Criterion B1. Probe 4's return direction was exactly this: the
        window sat at the right position and carried the size the old scale
        gave it, 99 physical pixels across where 150 was right."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        size_only = (800, 600, 890, 690)
        with _windows_reports(manager, size_only),              patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
        manager.button.setGeometry.assert_called_once_with(
            VISIBLE_POS[0], VISIBLE_POS[1], SIZE, SIZE)

    def test_a_position_that_disagrees_alone_is_forced_back(self, manager):
        """Criterion B1, the other half: the size Windows reports is right and
        the position is not."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        position_only = (1200, 900, 1300, 1000)
        with _windows_reports(manager, position_only),              patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
        manager.button.setGeometry.assert_called_once_with(
            VISIBLE_POS[0], VISIBLE_POS[1], SIZE, SIZE)

    def test_an_agreeing_native_rectangle_changes_nothing(self, manager):
        """Criterion B6. No forcing, no extra move, no settings write."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        with _windows_reports(manager, AGREEING_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
        manager.button.setGeometry.assert_not_called()
        assert len(manager.button.move.call_args_list) == 1, (
            "only the ordinary re-apply may move the button when nothing is wrong")
        assert _saved_positions(manager) == []

    def test_three_layout_signals_leave_one_delayed_re_apply(self, manager):
        """Criterion B4. A resolution change fires several signals together,
        and probe 4 measured four of them for one change."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        with patch("gui.QTimer") as timer, \
             _windows_reports(manager, AGREEING_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
            manager._on_screens_changed()
            manager._on_screens_changed()
            assert timer.call_count == 1, (
                "three signals together must leave one timer, not three")
            the_timer = timer.return_value
            assert the_timer.timeout.connect.call_count == 1, (
                "one timer carries one re-check, however many signals arrive")
            run_it = the_timer.timeout.connect.call_args[0][0]
            run_it()
            manager._on_screens_changed()
            assert timer.call_count == 1, (
                "the same timer serves the next change; it is never rebuilt")

    def test_the_delayed_re_apply_schedules_nothing_further(self, manager):
        """Criterion B3 and B4. The re-apply must not start a chain."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        with patch("gui.QTimer") as timer, \
             _windows_reports(manager, AGREEING_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
            the_timer = timer.return_value
            run_it = the_timer.timeout.connect.call_args[0][0]
            started_before = the_timer.start.call_count
            run_it()
            assert the_timer.start.call_count == started_before, (
                "the delayed re-apply must not start the delay again")

    def test_a_second_display_change_restarts_the_settling_delay(self, manager):
        """Codex finding wh-floating-button-offscreen.4.2.

        Probe 4 measured the screen's device pixel ratio lagging a change by
        725 to 809 milliseconds, well inside the 1500 the delay waits. A
        second, independent display change that lands near the end of the
        first delay used to inherit whatever was left of it. The re-check then
        read a ratio that was still old and nothing came after it. The delay
        starts again on every change, so the last change always gets a whole
        settling time to itself.
        """
        import gui

        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        with patch("gui.QTimer") as timer_class, \
             _windows_reports(manager, AGREEING_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
            manager._on_screens_changed()
            the_timer = timer_class.return_value
            assert timer_class.call_count == 1, (
                "one reusable timer, not a new one for every change")
            the_timer.setSingleShot.assert_called_once_with(True)
            assert the_timer.start.call_count == 2, (
                "a second display change must start the settling delay again")
            for one_start in the_timer.start.call_args_list:
                assert one_start[0][0] == gui._FORCED_REAPPLY_DELAY_MS, (
                    "every start waits the whole delay, never what is left of it")

    def test_a_held_button_that_spanned_a_display_change_is_forced_back(self, manager):
        """Codex finding wh-floating-button-offscreen.4.1.

        _begin_press sets _gesture_running on ANY press, so a press-and-hold
        for push-to-talk holds it for as long as the user talks. A display
        change during that hold is held back, and the apply that runs when the
        hold ends re-applies the SAME logical geometry, which is the call Qt
        drops. Without the forcing here, the window keeps the rectangle the
        old scale gave it and the button can stay off screen.
        """
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        manager.button._gesture_running = True
        with _windows_reports(manager, STALE_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
            manager.button.setGeometry.assert_not_called()
            assert manager._layout_changed_during_gesture is True

            manager.button._gesture_running = False
            manager._on_gesture_ended()
        manager.button.setGeometry.assert_called_once_with(
            VISIBLE_POS[0], VISIBLE_POS[1], SIZE, SIZE)

    def test_a_gesture_that_no_display_change_ran_into_forces_nothing(self, manager):
        """The other half of criterion B2 for this path. An ordinary press
        and release must reach neither the forcing nor the delayed re-check.
        """
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        with patch("gui.QTimer") as timer_class, \
             _windows_reports(manager, STALE_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_gesture_ended()
            manager.button.setGeometry.assert_not_called()
            timer_class.assert_not_called()

    def test_the_forcing_after_a_gesture_uses_the_geometry_the_apply_settled_on(
        self, manager
    ):
        """A drag that finishes inside the same hold keeps its new place.

        The drag's own completion clears the parked geometry, so the apply
        here is the flag's one: the button's just-dragged width and corner.
        The forcing must carry THAT rectangle to Windows, not the confirmed
        settings the display change parked, or the fix would undo the move
        the user just made.
        """
        dragged = [700, 500]
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        manager.button._gesture_running = True
        with _windows_reports(manager, STALE_NATIVE, pos=dragged), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
            # moved and resize_finished reach their slots before gesture_ended
            # and both clear the parked geometry; the flag is what survives.
            manager._deferred_geometry = None
            manager.button._gesture_running = False
            manager._on_gesture_ended()
        manager.button.setGeometry.assert_called_once_with(
            dragged[0], dragged[1], SIZE, SIZE)

    def test_a_held_button_that_spanned_a_display_change_gets_one_delayed_re_check(
        self, manager
    ):
        """Criterion B4 on this path. The ratio lags the change, so the hold's
        release owes the same single delayed re-check any other display change
        gets, and exactly one of them."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        manager.button._gesture_running = True
        with patch("gui.QTimer") as timer_class, \
             _windows_reports(manager, AGREEING_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
            timer_class.assert_not_called()

            manager.button._gesture_running = False
            manager._on_gesture_ended()
            assert timer_class.call_count == 1, (
                "the released hold owes one delayed re-check")
            assert timer_class.return_value.start.call_count == 1, (
                "one start, so one re-check is waiting")

    def test_the_correction_path_starts_no_repeating_timer(self, manager):
        """Criterion B3. Only a single-shot timer, never a repeating one."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        with patch("gui.QTimer") as timer, \
             _windows_reports(manager, STALE_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
            the_timer = timer.return_value
            the_timer.setSingleShot.assert_called_once_with(True)
            called = [one_call[0] for one_call in the_timer.mock_calls]
            assert called.index("setSingleShot") < called.index("start"), (
                "the timer is made single-shot before it is ever started")

    def test_many_ticks_with_no_display_signal_never_run_the_correction_path(
        self, manager
    ):
        """Criterion B3, the half a grep cannot prove.

        B3 forbids any timer or periodic tick that reads the screen list
        looking for a change. This test and its sibling drive two handlers:
        _check_queues_and_events, the 100 ms queue poll connected at
        gui.py:1605-1606 and started in start() at gui.py:1641, and
        _check_activity_shm, the 10 ms activity poll connected at
        gui.py:1627-1628 and started at gui.py:1648 whenever _gui_shm_name is
        set -- which the launcher always arranges, because launcher.py:906-925
        creates the GUI shared-memory segment and passes its name on every
        launch. This test drives the queue poll; the sibling test below drives
        the activity poll.

        What the pair does NOT drive, in gui.py as this commit leaves it.
        Three other handlers run on repeating timers: _reassert_topmost
        (gui.py:593), which raises the button while it is visible, on the 3 s
        timer connected at gui.py:390-391; _animate_dots (gui.py:1190), which
        advances the dot count and sets the label text, on the 400 ms timer
        connected at gui.py:1095-1096; and _pulse_tick (gui.py:686), which
        advances the pulse phase and repaints the button, on the 50 ms timer
        connected at gui.py:368-369. At this commit none of those three
        handlers reads the screen list: their whole bodies are an isVisible()
        check and raise_(), a phase increment and update(), and a counter
        increment and setText().

        Rebuild the list of repeating timers instead of trusting this prose.
        One command, over services/wheelhouse/gui.py and no other file:

            grep -n -A1 "QTimer(" services/wheelhouse/gui.py

        It prints each QTimer( line with the line after it. A QTimer( line
        whose next line does not call setSingleShot is a repeating
        timer. At this commit that leaves five: gui.py:368, 390, 1095, 1605
        and 1627. The command reads that one file, so this docstring claims
        nothing about timers in any other module of the GUI process. The pair
        of tests also does not cover a correction gated on a counter whose
        period exceeds the 150 ticks driven here.

        The queue raises Empty on every call, so no state message reaches the
        apply, and _settings_requests is empty, so the timeout check inside
        the tick does nothing either. What is left is the tick itself.
        """
        manager.state_from_logic_queue.get_nowait.side_effect = Empty
        with patch("gui.QTimer") as timer_class, \
             patch.object(manager, "_force_native_geometry") as forcing, \
             patch.object(manager, "_schedule_one_delayed_reapply") as delayed, \
             patch("gui._screen_bounds", return_value=ONE_SCREEN) as screen_bounds:
            for _ in range(150):
                manager._check_queues_and_events()

            assert screen_bounds.call_count == 0, (
                "a tick read the screen list; B3 forbids a periodic tick "
                "that polls the screens looking for a change, and a poll "
                "that corrects only on a difference leaves every assertion "
                "below green")
            forcing.assert_not_called()
            delayed.assert_not_called()
            manager.button.setGeometry.assert_not_called()
            manager.button.move.assert_not_called()
            timer_class.assert_not_called()
            assert timer_class.singleShot.call_count == 0, (
                "a tick scheduled a deferred correction; the constructor "
                "assertion above cannot see it, because a static-method call "
                "on a mocked class leaves the class's own .called False")
            assert timer_class.return_value.start.call_count == 0, (
                "no tick may start the settling delay")
        assert manager._delayed_reapply_timer is None, (
            "150 ticks with no display signal build no timer at all")

    def test_many_activity_ticks_with_no_display_signal_run_no_correction(
        self, manager
    ):
        """Criterion B3, on the second of the two driven handlers.

        The activity poll runs at 10 ms (gui.py:1648) against the queue
        poll's 100 ms, so it is the more attractive home for a correction or
        a screen-list poll than the timer the test above drives, and B3
        forbids both there for the same reason. It runs in production on
        every launch: launcher.py:906-925 always creates the GUI
        shared-memory segment and passes gui_shm_name, which is the condition
        start() checks before starting this timer.

        The stand-in segment carries a valid idle payload so the handler
        passes both of its early returns -- the falsy-segment one at
        gui.py:1655 and the size one at gui.py:1660 -- and reaches the state
        handling. A tick that returned early would measure nothing, so the
        first assertion below proves the handler did real work.
        """
        manager._gui_shm = _FakeSharedMemory(_idle_activity_payload())
        with patch("gui.QTimer") as timer_class, \
             patch.object(manager, "_force_native_geometry") as forcing, \
             patch.object(manager, "_schedule_one_delayed_reapply") as delayed, \
             patch("gui._screen_bounds", return_value=ONE_SCREEN) as screen_bounds:
            for _ in range(150):
                manager._check_activity_shm()

            assert manager.button.set_activity_state.called, (
                "the handler took an early return, so these 150 ticks say "
                "nothing about what the activity poll does")
            assert screen_bounds.call_count == 0, (
                "an activity tick read the screen list; B3 forbids a "
                "periodic tick that polls the screens looking for a change")
            forcing.assert_not_called()
            delayed.assert_not_called()
            manager.button.setGeometry.assert_not_called()
            manager.button.move.assert_not_called()
            timer_class.assert_not_called()
            assert timer_class.singleShot.call_count == 0, (
                "an activity tick scheduled a deferred correction; the "
                "constructor assertion above cannot see a static-method call "
                "on a mocked class")
            assert timer_class.return_value.start.call_count == 0, (
                "no activity tick may start the settling delay")
        assert manager._delayed_reapply_timer is None, (
            "150 activity ticks with no display signal build no timer at all")

    def test_the_normal_path_writes_two_debug_lines(self, manager, caplog):
        """Criterion B5. One names the signal that arrived; one gives the
        native rectangle before the re-apply and the one after it."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        caplog.set_level(logging.DEBUG, logger="gui")
        with _windows_reports(manager, AGREEING_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screen_layout_signal("QScreen.availableGeometryChanged")
        written = [record.getMessage() for record in caplog.records
                   if record.levelno == logging.DEBUG
                   and "floating button" in record.getMessage().lower()]
        assert len(written) == 2, written
        assert "QScreen.availableGeometryChanged" in written[0]
        assert str(AGREEING_NATIVE[0]) in written[1]

    def test_a_native_reading_that_fails_leaves_the_button_working(self, manager):
        """Criterion B7. The button keeps today's behaviour, and the failure
        is a debug line, not an exception and not a notice."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": LOST_POS,
        }
        manager.button.winId.return_value = 4242
        with patch("win32gui.GetWindowRect", side_effect=OSError("no window")), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager._on_screens_changed()
        assert _moved_to(manager) == LOST_POS_CORRECTED
        manager.button.setGeometry.assert_not_called()

    def test_a_forcing_call_that_fails_leaves_the_button_working(self, manager):
        """Criterion B7. The second half: the reading works and the forcing
        call itself raises."""
        manager._settings_confirmed = {
            "FLOATING_BUTTON_SIZE": SIZE,
            "FLOATING_BUTTON_POS": VISIBLE_POS,
        }
        with _windows_reports(manager, STALE_NATIVE), \
             patch("gui._screen_bounds", return_value=ONE_SCREEN):
            manager.button.setGeometry.side_effect = RuntimeError("window gone")
            # No exception may reach the caller, which is the whole claim.
            manager._on_screens_changed()
        assert manager.button.setGeometry.called
