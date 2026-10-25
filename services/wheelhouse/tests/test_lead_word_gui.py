"""GuiManager side of the 'Your training word' window (wh-first-sound-lost.23).

Covers the Logic-to-GUI messages (open_lead_word, lw_state, lw_play_go), the
one-window rule of acceptance item 19, the commands that leave the GUI
process, and the facts the GUI gives the window.
"""
import threading
from queue import Empty, Queue
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("qapp")


def _drain(queue):
    items = []
    while True:
        try:
            items.append(queue.get_nowait())
        except Empty:
            return items


@pytest.fixture
def manager():
    with patch("gui.FloatingButton") as mock_button, \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        mock_button.return_value = MagicMock()
        from gui import GuiManager

        mgr = GuiManager(threading.Event(), Queue(), Queue())
    mgr.initial_state_received = True
    mgr.button._gesture_running = False
    mgr.stt_provider = "parakeet_tdt"
    mgr.speech_enabled = True
    mgr.microphone_name = "Desk mic"
    return mgr


@pytest.fixture
def fake_dialog_class():
    """A stand-in window class: no window is built or shown.

    ``visible`` is what isVisible() answers, set by the test.
    """
    instances = []

    class _Fake:
        def __init__(self, parent=None):
            self.parent_given = parent
            self.visible = False
            self.lead_word_action = MagicMock()
            self.begin_session = MagicMock()
            self.set_environment = MagicMock()
            self.handle_state = MagicMock()
            self.play_now = MagicMock()
            self.show = MagicMock(side_effect=self._show)
            self.raise_ = MagicMock()
            self.activateWindow = MagicMock()
            self.winId = MagicMock(return_value=4242)
            self.minimized = False
            instances.append(self)

        def _show(self):
            self.visible = True

        def isVisible(self):
            return self.visible

        def windowState(self):
            from PySide6.QtCore import Qt
            return (
                Qt.WindowState.WindowMinimized
                if self.minimized else Qt.WindowState.WindowNoState
            )

        def setWindowState(self, state):
            from PySide6.QtCore import Qt
            self.minimized = bool(state & Qt.WindowState.WindowMinimized)

    with patch("lead_word_dialog.LeadWordDialog", _Fake), \
         patch("terminal_editor_window._steal_foreground") as steal:
        _Fake.instances = instances
        _Fake.steal = steal
        yield _Fake


def _logic_commands(manager):
    return _drain(manager.commands_to_logic_queue)


def test_open_lead_word_builds_one_window_and_asks_logic_to_open(
    manager, fake_dialog_class
):
    manager._open_lead_word()
    assert len(fake_dialog_class.instances) == 1
    dialog = fake_dialog_class.instances[0]
    dialog.begin_session.assert_called_once_with()
    dialog.show.assert_called_once_with()
    dialog.raise_.assert_called_once_with()
    dialog.activateWindow.assert_called_once_with()
    assert _logic_commands(manager) == [{"action": "lw_open"}]


def test_opening_when_the_window_is_open_brings_it_to_the_front_only(
    manager, fake_dialog_class
):
    manager._open_lead_word()
    _logic_commands(manager)
    dialog = fake_dialog_class.instances[0]
    dialog.begin_session.reset_mock()
    dialog.raise_.reset_mock()
    dialog.activateWindow.reset_mock()

    manager._open_lead_word()

    assert len(fake_dialog_class.instances) == 1
    # No second session: Logic is not asked to open again, the view is kept.
    assert _logic_commands(manager) == []
    dialog.begin_session.assert_not_called()
    dialog.raise_.assert_called_once_with()
    dialog.activateWindow.assert_called_once_with()


def test_opening_a_minimized_window_restores_it(manager, fake_dialog_class):
    manager._open_lead_word()
    dialog = fake_dialog_class.instances[0]
    dialog.minimized = True
    manager._open_lead_word()
    assert dialog.minimized is False


def test_opening_after_the_window_was_closed_starts_a_new_session(
    manager, fake_dialog_class
):
    manager._open_lead_word()
    _logic_commands(manager)
    dialog = fake_dialog_class.instances[0]
    dialog.visible = False  # the user closed it
    manager._open_lead_word()
    assert len(fake_dialog_class.instances) == 1
    assert _logic_commands(manager) == [{"action": "lw_open"}]
    assert dialog.begin_session.call_count == 2


def test_the_window_gets_the_environment_before_logic_is_asked(
    manager, fake_dialog_class
):
    manager.speech_interaction_mode = "push_to_talk"
    manager.speech_enabled = False
    manager._open_lead_word()
    dialog = fake_dialog_class.instances[0]
    dialog.set_environment.assert_called_with(
        speech_enabled=False, push_to_talk=True,
        provider="parakeet_tdt", microphone="Desk mic",
    )


def test_a_state_update_refreshes_the_environment(manager, fake_dialog_class):
    manager._open_lead_word()
    dialog = fake_dialog_class.instances[0]
    dialog.set_environment.reset_mock()
    manager.state_from_logic_queue.put_nowait({
        "action": "state_update", "speech_enabled": False,
        "stt_provider": "google_stt", "microphone_name": "Headset",
        "speech_interaction_mode": "toggle",
    })
    manager._check_queues_and_events()
    dialog.set_environment.assert_called_with(
        speech_enabled=False, push_to_talk=False,
        provider="google_stt", microphone="Headset",
    )


def test_a_state_update_before_the_window_exists_builds_nothing(manager, caplog):
    manager.state_from_logic_queue.put_nowait({
        "action": "state_update", "speech_enabled": True,
    })
    with caplog.at_level("ERROR"):
        manager._check_queues_and_events()
    assert getattr(manager, "_lw_dialog", None) is None
    # The refresh swallows an exception and logs it, so a missing guard
    # shows only in the log.
    assert "environment refresh failed" not in caplog.text


def test_the_open_lead_word_message_opens_the_window(manager):
    with patch.object(manager, "_open_lead_word") as opened:
        manager.state_from_logic_queue.put_nowait({"action": "open_lead_word"})
        manager._check_queues_and_events()
    opened.assert_called_once_with()


def test_lw_state_goes_to_the_window(manager, fake_dialog_class):
    manager._open_lead_word()
    dialog = fake_dialog_class.instances[0]
    state = {"screen": "idle", "supported": True}
    manager.state_from_logic_queue.put_nowait(
        {"action": "lw_state", "state": state}
    )
    manager._check_queues_and_events()
    dialog.handle_state.assert_called_once_with(state)


def test_lw_state_with_no_window_is_ignored(manager, caplog):
    manager.state_from_logic_queue.put_nowait(
        {"action": "lw_state", "state": {"screen": "idle"}}
    )
    with caplog.at_level("ERROR"):
        manager._check_queues_and_events()
    # The queue loop swallows an exception and logs it, so a missing guard
    # shows only in the log.
    assert "Error processing GUI state queue" not in caplog.text
    assert getattr(manager, "_lw_dialog", None) is None


def test_lw_play_go_starts_the_sound_in_the_window(manager, fake_dialog_class):
    manager._open_lead_word()
    dialog = fake_dialog_class.instances[0]
    manager.state_from_logic_queue.put_nowait(
        {"action": "lw_play_go", "which": "candidate"}
    )
    manager._check_queues_and_events()
    dialog.play_now.assert_called_once_with("candidate")


def test_lw_play_go_with_no_window_is_ignored(manager, caplog):
    manager.state_from_logic_queue.put_nowait(
        {"action": "lw_play_go", "which": "current"}
    )
    with caplog.at_level("ERROR"):
        manager._check_queues_and_events()
    assert "Error processing GUI state queue" not in caplog.text
    assert getattr(manager, "_lw_dialog", None) is None


def test_a_window_command_goes_to_the_logic_queue(manager):
    manager._send_lead_word_command({"action": "lw_record"})
    assert _logic_commands(manager) == [{"action": "lw_record"}]


# --------------------------------------------------------------------------
# Strength tests added with the mutation gate (wh-first-sound-lost.23, stage
# G2). Each one is red under a named mutation in
# tests/mutation_gate_lead_word_app_wiring.py.
# --------------------------------------------------------------------------

def test_the_window_has_no_parent_and_its_signal_goes_to_the_logic_forwarder(
    manager, fake_dialog_class
):
    manager._open_lead_word()
    dialog = fake_dialog_class.instances[0]
    assert dialog.parent_given is None
    dialog.lead_word_action.connect.assert_called_once_with(
        manager._send_lead_word_command
    )


def test_the_window_is_connected_once_however_often_it_is_opened(
    manager, fake_dialog_class
):
    manager._open_lead_word()
    manager._open_lead_word()
    dialog = fake_dialog_class.instances[0]
    dialog.lead_word_action.connect.assert_called_once()


def test_the_foreground_request_names_the_window_handle(manager, fake_dialog_class):
    manager._open_lead_word()
    fake_dialog_class.steal.assert_called_once_with(4242)


def test_a_failed_foreground_request_does_not_stop_the_window(
    manager, fake_dialog_class
):
    fake_dialog_class.steal.side_effect = RuntimeError("no foreground right")
    manager._open_lead_word()
    dialog = fake_dialog_class.instances[0]
    dialog.show.assert_called_once_with()
    assert _logic_commands(manager) == [{"action": "lw_open"}]


def test_the_environment_is_given_before_the_window_is_shown(
    manager, fake_dialog_class
):
    order = []
    original = fake_dialog_class.__init__

    def tracking_init(self, parent=None):
        original(self, parent)
        self.set_environment.side_effect = lambda **kw: order.append("environment")
        self.show.side_effect = lambda: (order.append("show"), self._show())

    fake_dialog_class.__init__ = tracking_init
    manager._open_lead_word()
    assert order == ["environment", "show"]


def test_an_unknown_microphone_reaches_the_window_as_empty_text(
    manager, fake_dialog_class
):
    manager.microphone_name = None
    manager._open_lead_word()
    dialog = fake_dialog_class.instances[0]
    assert dialog.set_environment.call_args.kwargs["microphone"] == ""


def test_the_environment_values_are_plain_booleans(manager, fake_dialog_class):
    manager.speech_enabled = 1
    manager.speech_interaction_mode = "toggle"
    manager._open_lead_word()
    kwargs = fake_dialog_class.instances[0].set_environment.call_args.kwargs
    assert kwargs["speech_enabled"] is True
    assert kwargs["push_to_talk"] is False


def test_a_failing_environment_refresh_is_logged_and_does_not_escape(
    manager, fake_dialog_class, caplog
):
    manager._open_lead_word()
    dialog = fake_dialog_class.instances[0]
    dialog.set_environment.side_effect = RuntimeError("bad window")
    with caplog.at_level("ERROR"):
        manager._refresh_lead_word_environment()
    assert "environment refresh failed" in caplog.text


def test_lw_state_without_a_state_gives_the_window_an_empty_one(
    manager, fake_dialog_class
):
    manager._open_lead_word()
    dialog = fake_dialog_class.instances[0]
    manager.state_from_logic_queue.put_nowait({"action": "lw_state"})
    manager._check_queues_and_events()
    dialog.handle_state.assert_called_once_with({})


def test_the_window_signal_is_wired_to_the_logic_queue(manager):
    """The real window's signal reaches the Logic queue unchanged.

    A subclass replaces the three calls that would show a real window;
    patching them on the PySide6 class itself crashes the interpreter.
    """
    from lead_word_dialog import LeadWordDialog

    class _Quiet(LeadWordDialog):
        def show(self):
            pass

        def raise_(self):
            pass

        def activateWindow(self):
            pass

    with patch("terminal_editor_window._steal_foreground"),          patch("lead_word_dialog.LeadWordDialog", _Quiet):
        manager._open_lead_word()
    _logic_commands(manager)
    manager._lw_dialog.lead_word_action.emit({"action": "lw_keep"})
    assert _logic_commands(manager) == [{"action": "lw_keep"}]
    manager._lw_dialog.deleteLater()
