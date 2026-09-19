"""The GUI must not guess the speech state after a push-to-talk release.

wh-ptt-release-disables-speech.1.5, filed by codex.

``GuiManager._stop_ptt`` used to write ``self.speech_enabled = False`` and
``button.set_state(False)`` as soon as it sent the ptt_stop command. That guess
was always right while ``StateManager.ptt_stop`` forced the setting off for an
ordinary release.

Commit 5719a1fa made the release restore the pre-hold setting instead, so the
Logic answer can now be on. The guess then showed an off microphone while the
speech engine was listening, and the correction arrived only on the next queue
poll, 100 ms later. A hands-free user has no other indicator.

An earlier version of this docstring said the correction could be lost, because
the state queue drops messages when it is full. That is wrong, and codex read
it and built finding .1.7 on it. launcher.py creates state_to_gui_queue as
multiprocessing.Queue() with no maxsize, which on this machine holds 2147483647
items before put_nowait raises Full, so a state update is not dropped for a
full queue.

This process cannot work out the answer for itself. A state update carries only
the computed ``speech_enabled``, not the raw setting and not the three
suppression flags, and the hold's own audio override changes the computed
answer while the hold runs. So the release sends its command and shows what
Logic reports.
"""
import threading
from queue import Queue
from unittest.mock import MagicMock, patch

import pytest

# GuiManager.__init__ builds a real QDialog, so these need a QApplication and
# the editor-window replacement. See the mock_editor_window docstring in
# tests/conftest.py.
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")


@pytest.fixture
def manager():
    """A GuiManager with a mocked button and tray icon, and real queues.

    The shutdown event and both queues are real. A Mock shutdown event reports
    itself set, and _check_queues_and_events then shuts the window down instead
    of reading anything.
    """
    with patch("gui.FloatingButton") as MockBtn, \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        MockBtn.return_value = MagicMock()
        from gui import GuiManager
        mgr = GuiManager(threading.Event(), Queue(), Queue())
        mgr.initial_state_received = True
        mgr.speech_interaction_mode = "toggle"
        mgr._press_timer = MagicMock()
        mgr._double_click_timer = MagicMock()
        # A Mock reports every gesture as running, and the geometry then waits
        # for a gesture that never ends instead of being applied.
        mgr.button._gesture_running = False
        # _on_hold_threshold refuses to start a hold while either gesture runs,
        # and a Mock reports both as running.
        mgr.button._is_dragging = False
        mgr.button._is_resizing = False
        return mgr


def _states_set(manager):
    """Every value passed to button.set_state, in order."""
    return [c[0][0] for c in manager.button.set_state.call_args_list if c[0]]


def _last_command(manager):
    """The most recent command the manager queued for the Logic process."""
    cmd = None
    while not manager.commands_to_logic_queue.empty():
        cmd = manager.commands_to_logic_queue.get_nowait()
    assert cmd is not None, "the manager queued no command"
    return cmd


class TestTheReleaseDoesNotGuess:
    def test_the_release_does_not_force_the_speech_state_off(self, manager):
        """The exact case codex reported: speech on, nothing suppressing it."""
        manager._ptt_held = True
        manager.speech_enabled = True

        manager._stop_ptt()

        assert manager.speech_enabled is True

    def test_the_release_does_not_force_the_button_off(self, manager):
        manager._ptt_held = True
        manager.speech_enabled = True
        manager.button.set_state.reset_mock()

        manager._stop_ptt()

        assert False not in _states_set(manager)

    def test_the_release_still_sends_the_stop_command(self, manager):
        """The guess goes; the command that ends the hold stays."""
        manager._ptt_held = True

        manager._stop_ptt()

        cmd = _last_command(manager)
        assert cmd["action"] == "ptt_stop"
        assert manager._ptt_held is False


class TestTheStateUpdateDecides:
    def test_a_state_update_saying_on_leaves_the_button_on(self, manager):
        """Logic restored a pre-hold enabled setting with nothing suppressing it.

        A supporting guard, not a catcher: it passes with the guess in place
        too, because the state update overwrites the guess. It protects the
        path the release now depends on.
        """
        manager._ptt_held = True
        manager.speech_enabled = True
        manager._stop_ptt()
        manager.button.set_state.reset_mock()

        manager.state_from_logic_queue.put(
            {"action": "state_update", "speech_enabled": True}
        )
        manager._check_queues_and_events()

        assert manager.speech_enabled is True
        assert _states_set(manager)[-1] is True

    def test_a_state_update_saying_off_turns_the_button_off(self, manager):
        """The suppressed case, and push-to-talk mode, still end off.

        The same supporting guard for the other answer.
        """
        manager._ptt_held = True
        manager.speech_enabled = True
        manager._stop_ptt()
        manager.button.set_state.reset_mock()

        manager.state_from_logic_queue.put(
            {"action": "state_update", "speech_enabled": False}
        )
        manager._check_queues_and_events()

        assert manager.speech_enabled is False
        assert _states_set(manager)[-1] is False


def _hold(manager):
    """Take a real press and let the hold timer open the microphone."""
    manager._on_button_press()
    manager._on_hold_threshold()
    assert manager._ptt_held is True


CANCELLATIONS = [
    ("drag_cancel", "_on_drag_started"),
    ("gesture_cancel", "_on_press_cancelled"),
]


class TestTheCancellationsDoNotGuessEither:
    """wh-ptt-release-disables-speech.1.8.

    _cancel_pending_press restored the value saved at the press, which is the
    computed display, while StateManager.ptt_stop restores the raw setting.
    Commit 10983243 made an explicit mid-hold decision move the raw value that
    Logic restores. The pre-press snapshot did not move with it, so a
    cancellation put the display back to a decision the user had replaced.
    """

    @pytest.mark.parametrize("reason,trigger", CANCELLATIONS)
    def test_a_cancellation_does_not_undo_a_mid_hold_decision(
        self, manager, reason, trigger
    ):
        """Speech was on at the press; a decision during the hold turned it off."""
        manager.speech_enabled = True
        _hold(manager)

        manager.state_from_logic_queue.put(
            {"action": "state_update", "speech_enabled": False}
        )
        manager._check_queues_and_events()
        assert manager.speech_enabled is False, "Logic reported the decision"
        manager.button.set_state.reset_mock()

        getattr(manager, trigger)()

        assert manager.speech_enabled is False
        assert True not in _states_set(manager)

    @pytest.mark.parametrize("reason,trigger", CANCELLATIONS)
    def test_a_cancellation_does_not_undo_a_mid_hold_decision_the_other_way(
        self, manager, reason, trigger
    ):
        """Speech was off at the press; a decision during the hold turned it on."""
        manager.speech_enabled = False
        _hold(manager)

        manager.state_from_logic_queue.put(
            {"action": "state_update", "speech_enabled": True}
        )
        manager._check_queues_and_events()
        assert manager.speech_enabled is True
        manager.button.set_state.reset_mock()

        getattr(manager, trigger)()

        assert manager.speech_enabled is True
        assert False not in _states_set(manager)

    @pytest.mark.parametrize("reason,trigger", CANCELLATIONS)
    def test_a_cancellation_still_sends_its_stop_command(
        self, manager, reason, trigger
    ):
        """The guess goes; the command that ends the hold stays."""
        manager.speech_enabled = True
        _hold(manager)

        getattr(manager, trigger)()

        cmd = _last_command(manager)
        assert cmd["action"] == "ptt_stop"
        assert cmd["reason"] == reason
        assert manager._ptt_held is False

    @pytest.mark.parametrize("reason,trigger", CANCELLATIONS)
    def test_a_cancellation_shows_what_logic_reports_afterwards(
        self, manager, reason, trigger
    ):
        """Logic restored the raw setting and the display follows it."""
        manager.speech_enabled = True
        _hold(manager)
        getattr(manager, trigger)()
        manager.button.set_state.reset_mock()

        manager.state_from_logic_queue.put(
            {"action": "state_update", "speech_enabled": False}
        )
        manager._check_queues_and_events()

        assert manager.speech_enabled is False
        assert _states_set(manager)[-1] is False
