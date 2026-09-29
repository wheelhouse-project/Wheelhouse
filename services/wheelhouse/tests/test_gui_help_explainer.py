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
def steal_foreground():
    """Replace the Win32 foreground request so no test moves a real window."""
    with patch("terminal_editor_window._steal_foreground") as steal:
        yield steal


@pytest.fixture
def fake_window(steal_foreground):
    """Stand in for the real window: these tests check the GUI plumbing."""
    with patch("help_explainer_window.HelpExplainerWindow") as cls:
        cls.return_value = MagicMock()
        # Like a real window, it is not visible until it is shown; a
        # MagicMock would otherwise answer isVisible() with a true value.
        cls.return_value.isVisible.return_value = False
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


class TestTakingTheForeground:
    """wh-assistant-window-behind: the spoken 'help' request arrives by the
    Logic-to-GUI queue, so the GUI process holds no foreground right and
    activateWindow() alone only flashes the taskbar button. The window must
    also go through the terminal editor's AttachThreadInput bypass."""

    @staticmethod
    def _record(fake_window, steal_foreground):
        calls = []
        window = fake_window.return_value
        window.winId.return_value = 4242
        window.show.side_effect = lambda: calls.append(("show",))
        window.raise_.side_effect = lambda: calls.append(("raise_",))
        window.activateWindow.side_effect = (
            lambda: calls.append(("activateWindow",))
        )
        return calls

    def test_the_window_takes_the_foreground_with_its_handle(
        self, manager, fake_window, steal_foreground
    ):
        calls = self._record(fake_window, steal_foreground)
        steal_foreground.side_effect = (
            lambda hwnd: calls.append(("_steal_foreground", hwnd))
        )

        manager.state_from_logic_queue.put_nowait(
            {"action": "open_help_explainer"}
        )
        manager._check_queues_and_events()

        steal_foreground.assert_called_once_with(4242)
        # The foreground request comes last, after the Qt activation calls.
        assert calls == [
            ("show",),
            ("raise_",),
            ("activateWindow",),
            ("_steal_foreground", 4242),
        ], calls

    def test_a_foreground_failure_does_not_escape(
        self, manager, fake_window, steal_foreground
    ):
        calls = self._record(fake_window, steal_foreground)
        steal_foreground.side_effect = OSError("SetForegroundWindow refused")

        # Called directly: the queue loop's own guards must not be the
        # thing that hides the failure.
        manager._open_help_explainer()

        steal_foreground.assert_called_once_with(4242)
        assert calls == [("show",), ("raise_",), ("activateWindow",)], calls


class TestThePreTickedCheckBox:
    """wh-assistant-explainer-once-more: the once-more showing starts with
    the box ticked, which matches the user's earlier choice."""

    def test_the_flag_ticks_the_box(self, manager, fake_window):
        manager.state_from_logic_queue.put_nowait(
            {"action": "open_help_explainer", "start_ticked": True}
        )
        manager._check_queues_and_events()

        fake_window.return_value.prepare_to_show.assert_called_once_with(
            start_ticked=True
        )

    def test_no_flag_leaves_the_box_clear(self, manager, fake_window):
        manager.state_from_logic_queue.put_nowait(
            {"action": "open_help_explainer"}
        )
        manager._check_queues_and_events()

        fake_window.return_value.prepare_to_show.assert_called_once_with(
            start_ticked=False
        )

    @pytest.mark.parametrize("value", ["true", 1, "yes"])
    def test_only_the_boolean_true_ticks_the_box(self, manager, fake_window, value):
        """The flag crosses a process boundary, so bool("false") is True."""
        manager.state_from_logic_queue.put_nowait(
            {"action": "open_help_explainer", "start_ticked": value}
        )
        manager._check_queues_and_events()

        fake_window.return_value.prepare_to_show.assert_called_once_with(
            start_ticked=False
        )


class TestTheChoiceGoesToLogic:
    @pytest.mark.parametrize("ticked", [False, True], ids=["clear", "ticked"])
    def test_assistant_sends_one_command_with_the_box_state(self, manager, ticked):
        """The Logic process decides whether to save the setting, so the GUI
        sends the box state and never a set_config_value of its own."""
        manager._on_help_explainer_choice(ticked)

        assert _commands(manager) == [
            {
                "action": "open_help_online",
                "explained": True,
                # The source keeps the Logic process log honest: this run
                # began at the Assistant button (boss ruling condition 3).
                "source": "window",
                "do_not_show_again": ticked,
            }
        ]


class TestASecondRequestKeepsAnUnsavedChoice:
    """wh-assistant-explainer-once-more.1.2: a Help request that arrives while
    the window is already visible raises it and leaves the check box alone.

    The window is modeless, so the user can ask for Help again (by voice or
    from the menu) while the box holds a choice that the Assistant button
    has not yet sent. Resetting the box then would change that choice
    without the user seeing it. For the once-more showing the loss is
    permanent: the marker written on the next Assistant press stops the
    window from being shown again. Most of these tests use the real window,
    because the check box state is the thing under test.
    """

    @pytest.fixture
    def real_window(self, manager, steal_foreground):
        yield
        window = getattr(manager, "_help_explainer", None)
        if window is not None:
            window.close()
            window.deleteLater()
            manager._help_explainer = None

    @staticmethod
    def _ask(manager, **fields):
        manager.state_from_logic_queue.put_nowait(
            {"action": "open_help_explainer", **fields}
        )
        manager._check_queues_and_events()
        return manager._help_explainer

    def test_a_cleared_once_more_box_stays_cleared(self, manager, real_window):
        window = self._ask(manager, start_ticked=True)
        assert window.isVisible() is True
        assert window._do_not_show_again.isChecked() is True
        window._do_not_show_again.setChecked(False)

        again = self._ask(manager, start_ticked=True)

        assert again is window
        assert window._do_not_show_again.isChecked() is False

    def test_a_ticked_clear_start_box_stays_ticked(self, manager, real_window):
        window = self._ask(manager)
        assert window.isVisible() is True
        assert window._do_not_show_again.isChecked() is False
        window._do_not_show_again.setChecked(True)

        self._ask(manager)

        assert window._do_not_show_again.isChecked() is True

    @pytest.mark.parametrize(
        "fields, expected",
        [({}, False), ({"start_ticked": True}, True)],
        ids=["clear-start", "once-more"],
    )
    def test_a_hidden_window_is_reset_when_shown_again(
        self, manager, real_window, fields, expected
    ):
        """Existing behaviour: a window closed with a choice left in the box
        starts from the requested state at the next showing."""
        window = self._ask(manager)
        window._do_not_show_again.setChecked(not expected)
        window.hide()
        assert window.isVisible() is False

        self._ask(manager, **fields)

        assert window.isVisible() is True
        assert window._do_not_show_again.isChecked() is expected

    def test_a_visible_window_is_still_raised_and_activated(
        self, manager, fake_window
    ):
        """The second request raises and activates the visible window, but
        does not reset its check box."""
        window = fake_window.return_value
        # Hidden at the first request, visible at the second.
        window.isVisible.side_effect = [False, True]
        for _ in range(2):
            manager.state_from_logic_queue.put_nowait(
                {"action": "open_help_explainer", "start_ticked": True}
            )
            manager._check_queues_and_events()

        window.prepare_to_show.assert_called_once_with(start_ticked=True)
        assert window.raise_.call_count == 2
        assert window.activateWindow.call_count == 2
