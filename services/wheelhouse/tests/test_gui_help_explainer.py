"""The GUI process side of the assistant explanation window.

The Logic process owns the decision and asks for the window with
{"action": "open_help_explainer"}. The GUI process shows one window,
raises it when it is already open, and sends the user's choice back.

Criteria W2, W3, and W5 of wh-assistant-button-explainer.
"""

from unittest.mock import MagicMock, patch

import pytest

from tests.test_gui_release_does_not_guess_speech import manager as _manager

manager = _manager

# GuiManager needs a live QApplication and the editor-window replacement.
# Without these the process dies with no traceback (measured: pytest
# collected 8 items, then exited 127 with no output).
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")


def _commands(mgr):
    """Every command the manager queued for the Logic process, in order."""
    sent = []
    while not mgr.commands_to_logic_queue.empty():
        sent.append(mgr.commands_to_logic_queue.get_nowait())
    return sent


@pytest.fixture
def fake_window():
    """Stand in for the real window: these tests check the GUI plumbing."""
    with patch("help_explainer_window.HelpExplainerWindow") as cls:
        cls.return_value = MagicMock()
        yield cls


class TestOpeningTheWindow:
    def test_the_logic_request_opens_the_window(self, manager, fake_window):
        manager.state_from_logic_queue.put_nowait(
            {"action": "open_help_explainer"}
        )
        manager._check_queues_and_events()

        fake_window.assert_called_once()
        fake_window.return_value.show.assert_called_once()

    def test_a_second_request_raises_the_open_window(self, manager, fake_window):
        """W5: never a second window."""
        for _ in range(2):
            manager.state_from_logic_queue.put_nowait(
                {"action": "open_help_explainer"}
            )
            manager._check_queues_and_events()

        fake_window.assert_called_once()
        window = fake_window.return_value
        assert window.show.call_count == 2
        assert window.raise_.call_count == 2
        assert window.activateWindow.call_count == 2

    def test_the_check_box_is_cleared_before_each_showing(self, manager, fake_window):
        manager.state_from_logic_queue.put_nowait(
            {"action": "open_help_explainer"}
        )
        manager._check_queues_and_events()

        fake_window.return_value.prepare_to_show.assert_called_once()

    def test_the_manager_listens_for_the_choice(self, manager, fake_window):
        manager.state_from_logic_queue.put_nowait(
            {"action": "open_help_explainer"}
        )
        manager._check_queues_and_events()

        fake_window.return_value.assistant_chosen.connect.assert_called_once_with(
            manager._on_help_explainer_choice
        )

    def test_the_queue_keeps_running_while_the_window_is_open(
        self, manager, fake_window
    ):
        """W5: the window is modeless, so later messages are still handled."""
        manager.state_from_logic_queue.put_nowait(
            {"action": "open_help_explainer"}
        )
        manager.state_from_logic_queue.put_nowait(
            {"action": "show_notification", "title": "Wheelhouse", "message": "still here"}
        )

        with patch("gui.send_notice", return_value=True) as notice:
            manager._check_queues_and_events()

        notice.assert_called_once()
        assert notice.call_args[0][1] == "still here"


class TestTheChoiceGoesToLogic:
    def test_assistant_asks_logic_to_open_the_page(self, manager):
        manager._on_help_explainer_choice(False)

        assert _commands(manager) == [
            {
                "action": "open_help_online",
                "explained": True,
                # The source keeps the Logic process log honest: this run
                # began at the Assistant button (boss ruling condition 3).
                "source": "window",
            }
        ]

    def test_the_checked_box_also_saves_the_setting(self, manager):
        """W3: the setting is saved only with the box checked."""
        manager._on_help_explainer_choice(True)

        sent = _commands(manager)
        assert {
            "action": "open_help_online",
            "explained": True,
            "source": "window",
        } in sent
        saves = [c for c in sent if c.get("action") == "set_config_value"]
        assert len(saves) == 1
        assert saves[0]["key"] == "ai.help.explain_before_open"
        assert saves[0]["value"] is False

    def test_a_clear_box_saves_nothing(self, manager):
        manager._on_help_explainer_choice(False)

        sent = _commands(manager)
        assert [c for c in sent if c.get("action") == "set_config_value"] == []
