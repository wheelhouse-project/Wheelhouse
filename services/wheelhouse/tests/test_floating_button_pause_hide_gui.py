"""The GUI half of wh-kodi-pause-hide-button.

The Logic process sends ``button_hidden_for_exclusive_speaker`` in every state
snapshot. The GUI hides the floating button while it is True and the button
follows the saved ``button_visible`` setting otherwise. These tests cover:

- the visibility rule in ``update_ui_state`` (acceptance 1)
- a GUI that starts or reconnects during a pause hides the button with its
  FIRST message (acceptance 6, BOSS RULING 10:45)
- the tray check mark and the settings checkbox keep showing the saved
  setting while the button is hidden for the pause (acceptance 3)
- one INFO line when the button hides for the pause and one when it comes
  back, none on a repeat (acceptance 6)
- a push-to-talk press in flight when the hide happens is cancelled
"""

import logging
from queue import Empty
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.test_gui import menu_manager  # noqa: F401  (fixture)

KEY = "button_hidden_for_exclusive_speaker"
HIDE_LINE = "floating button hidden while a program has sole control of the speaker"
SHOW_LINE = "floating button no longer hidden for the exclusive-speaker pause"


@pytest.fixture(autouse=True)
def _no_settings_notice_widget():
    with patch("soft_allow_write_failed_toast.SoftAllowWriteFailedToast"):
        yield


@pytest.fixture
def manager(qapp):
    with patch("gui.FloatingButton") as button_cls, \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as pystray, \
         patch("gui.QTimer"), \
         patch("gui.QPoint") as qpoint:
        button = MagicMock()
        button_cls.return_value = button
        pystray.Icon.return_value = MagicMock()
        qpoint.side_effect = lambda *args: MagicMock()
        from gui import GuiManager

        shutdown = MagicMock()
        shutdown.is_set.return_value = False
        mgr = GuiManager(shutdown, MagicMock(), MagicMock())
        mgr.button = button
        button._is_resizing = False
        button._is_dragging = False
        button._gesture_running = False
        # update_ui_state also rebuilds the tray menu and refreshes the
        # settings window; neither is under test here.
        mgr.update_tray_menu = MagicMock()
        mgr._refresh_settings_window = MagicMock()
        yield mgr


def _receive(manager, *messages):
    manager.state_from_logic_queue.get_nowait.side_effect = [*messages, Empty()]
    manager._check_queues_and_events()


def _state(action="state_update", **fields):
    message = {"action": action, "speech_enabled": False, "button_visible": True}
    message.update(fields)
    return message


class TestTheVisibilityRule:

    @pytest.mark.parametrize(
        "saved, hidden, shown",
        [(True, False, True), (True, True, False),
         (False, False, False), (False, True, False)],
    )
    def test_the_button_shows_only_when_the_setting_is_on_and_no_pause_hides_it(
        self, manager, saved, hidden, shown
    ):
        manager.button_visible = saved
        manager.button_hidden_for_exclusive_speaker = hidden

        manager.update_ui_state()

        manager.button.setVisible.assert_called_once_with(shown)

    def test_a_state_update_with_the_key_hides_a_button_whose_setting_is_on(
        self, manager
    ):
        manager.initial_state_received = True
        _receive(manager, _state(**{KEY: True}))

        manager.button.setVisible.assert_called_with(False)
        assert manager.button_visible is True

    def test_the_key_going_false_shows_the_button_again(self, manager):
        manager.initial_state_received = True
        _receive(manager, _state(**{KEY: True}), _state(**{KEY: False}))

        assert manager.button.setVisible.call_args_list[-1].args == (True,)

    def test_a_setting_that_is_off_keeps_the_button_hidden_after_the_pause(
        self, manager
    ):
        manager.initial_state_received = True
        _receive(
            manager,
            _state(button_visible=False, **{KEY: True}),
            _state(button_visible=False, **{KEY: False}),
        )

        assert manager.button.setVisible.call_args_list[-1].args == (False,)

    def test_a_message_without_the_key_means_not_hidden(self, manager):
        manager.initial_state_received = True
        manager.button_hidden_for_exclusive_speaker = True
        _receive(manager, _state())

        assert manager.button_hidden_for_exclusive_speaker is False
        manager.button.setVisible.assert_called_with(True)


class TestAGuiThatStartsDuringThePause:
    """BOSS RULING 10:45: the first message already carries True, and the GUI
    hides the button in that same update, with no wait of its own."""

    def test_the_first_message_hides_the_button(self, manager):
        assert manager.initial_state_received is False

        _receive(manager, _state(action="initial_state", **{KEY: True}))

        assert manager.initial_state_received is True
        manager.button.setVisible.assert_called_once_with(False)

    def test_the_first_state_update_hides_the_button_too(self, manager):
        _receive(manager, _state(**{KEY: True}))

        manager.button.setVisible.assert_called_once_with(False)

    def test_the_first_message_logs_the_hide(self, manager, caplog):
        with caplog.at_level(logging.INFO, logger="gui"):
            _receive(manager, _state(action="initial_state", **{KEY: True}))

        assert [r.getMessage() for r in caplog.records if HIDE_LINE in r.getMessage()]


class TestTheUsersSettingStaysVisible:

    def test_the_tray_check_mark_follows_the_saved_setting(self, menu_manager):
        import pystray

        menu_manager.button_visible = True
        menu_manager.button_hidden_for_exclusive_speaker = True
        menu = menu_manager._create_menu(is_tray_menu=True)
        item = [i for i in menu if i is not pystray.Menu.SEPARATOR
                and i.text == "Show Floating Button"][0]

        assert item.checked is True

        menu_manager.button_visible = False
        menu = menu_manager._create_menu(is_tray_menu=True)
        item = [i for i in menu if i is not pystray.Menu.SEPARATOR
                and i.text == "Show Floating Button"][0]

        assert item.checked is False

    def test_the_window_menu_check_mark_follows_the_saved_setting(
        self, menu_manager
    ):
        menu_manager.button_visible = True
        menu_manager.button_hidden_for_exclusive_speaker = True
        menu = menu_manager._create_menu(is_tray_menu=False)
        action = [a for a in menu.actions()
                  if not a.isSeparator() and a.text() == "Show Floating Button"][0]

        assert action.isChecked() is True

    def test_the_settings_checkbox_follows_the_saved_setting(self, menu_manager):
        menu_manager.button_visible = True
        menu_manager.button_hidden_for_exclusive_speaker = True

        assert menu_manager._settings_window_live_state()["button_visible"] is True

    def test_the_toggle_still_writes_the_opposite_of_the_saved_setting(
        self, manager
    ):
        manager.initial_state_received = True
        manager.button_visible = True
        manager.button_hidden_for_exclusive_speaker = True
        manager.send_command = MagicMock()

        manager.toggle_button_visibility()

        command = manager.send_command.call_args.args[0]
        assert command["key"] == "FLOATING_BUTTON_VISIBLE"
        assert command["value"] is False


class TestTheLogLines:

    def _lines(self, caplog):
        return [r.getMessage() for r in caplog.records]

    def test_one_line_when_the_button_hides_and_none_on_a_repeat(
        self, manager, caplog
    ):
        manager.initial_state_received = True
        with caplog.at_level(logging.INFO, logger="gui"):
            _receive(manager, _state(**{KEY: True}), _state(**{KEY: True}))

        lines = self._lines(caplog)
        assert sum(HIDE_LINE in line for line in lines) == 1
        assert not any(SHOW_LINE in line for line in lines)

    def test_one_line_when_the_button_comes_back_and_none_on_a_repeat(
        self, manager, caplog
    ):
        manager.initial_state_received = True
        _receive(manager, _state(**{KEY: True}))
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="gui"):
            _receive(manager, _state(**{KEY: False}), _state(**{KEY: False}))

        lines = self._lines(caplog)
        assert sum(SHOW_LINE in line for line in lines) == 1
        assert not any(HIDE_LINE in line for line in lines)

    def test_a_state_that_never_hides_logs_nothing(self, manager, caplog):
        manager.initial_state_received = True
        with caplog.at_level(logging.INFO, logger="gui"):
            _receive(manager, _state(), _state(**{KEY: False}))

        lines = self._lines(caplog)
        assert not any(HIDE_LINE in line or SHOW_LINE in line for line in lines)


class TestAPushToTalkPressInFlight:
    """FloatingButton.hideEvent drops the press when the button leaves the
    screen. The hide for the pause goes through the same setVisible(False)."""

    def test_hiding_for_the_pause_cancels_a_press_in_flight(self, qapp):
        from PySide6.QtCore import Qt

        from gui import FloatingButton, GuiManager
        from tests.test_floating_button_resize_drag import (
            BUTTON_ORIGIN,
            BUTTON_SIZE,
            CENTRE_POINT,
            _press,
        )

        button = FloatingButton(initial_size=BUTTON_SIZE)
        button.move(BUTTON_ORIGIN)
        button.set_ready_for_gestures(True)
        button.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        button.show()
        cancelled = []
        button.press_cancelled.connect(lambda: cancelled.append(True))
        _press(button, CENTRE_POINT)

        fake = SimpleNamespace(
            button=button,
            speech_enabled=True,
            _ptt_feedback=None,
            speech_interaction_mode="push_to_talk",
            button_visible=True,
            button_hidden_for_exclusive_speaker=True,
            update_tray_menu=MagicMock(),
            _refresh_settings_window=MagicMock(),
        )
        GuiManager.update_ui_state(fake)

        assert not button.isVisible()
        assert cancelled == [True]
        button.deleteLater()
