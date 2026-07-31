"""The Help entry on the right-click menu, on the Logic side.

The GUI process shows the menu but has no copy of the settings, so choosing
Help sends a command and the Logic process opens the browser. The address is
the same ai.help gem_url setting the spoken command "wheelhouse help online"
uses, read the same way, so changing it in one place changes both.
"""

from unittest.mock import MagicMock, patch

import pytest


def _controller():
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller._build_gui_handler_map = (
        LogicController._build_gui_handler_map.__get__(controller)
    )
    controller._open_help_online = (
        LogicController._open_help_online.__get__(controller)
    )
    # The real one, not the stand-in: the tests below check what actually
    # reaches the GUI queue, which a stand-in would swallow.
    controller._send_gui_notification = (
        LogicController._send_gui_notification.__get__(controller)
    )
    controller.state_manager = MagicMock()
    controller.create_task_with_error_handling = MagicMock()
    return controller


class TestTheHandlerIsReachable:

    def test_the_menu_command_routes_to_the_help_handler(self):
        controller = _controller()

        with patch.object(controller, "_open_help_online") as opener:
            handler_map = controller._build_gui_handler_map(
                {"action": "open_help_online"}
            )
            handler_map["open_help_online"]()

        opener.assert_called_once()


class TestOpeningTheHelpPage:

    @pytest.mark.asyncio
    async def test_it_opens_the_configured_address(self):
        controller = _controller()
        controller.config_service = MagicMock()
        controller.config_service.get.return_value = "https://example.test/help"

        with patch("webbrowser.open") as browser:
            await controller._open_help_online()

        controller.config_service.get.assert_called_with("ai.help.gem_url", "")
        browser.assert_called_once_with("https://example.test/help")

    @pytest.mark.asyncio
    async def test_a_blank_address_opens_nothing_and_says_so(self):
        """Blanking the setting is how a user turns online help off.

        Opening a browser on an empty address would show an error page, and
        saying nothing at all would look like the menu is broken.
        """
        controller = _controller()
        controller.config_service = MagicMock()
        controller.config_service.get.return_value = ""

        with patch("webbrowser.open") as browser:
            await controller._open_help_online()

        browser.assert_not_called()
        controller.state_manager.state_to_gui_queue.put_nowait.assert_called_once()
        message = controller.state_manager.state_to_gui_queue.put_nowait.call_args[0][0]
        assert message["action"] == "show_notification"

    @pytest.mark.asyncio
    async def test_no_settings_at_all_opens_nothing_and_tells_nobody(self):
        """Startup can fail before the settings are read.

        Not the same as a blank address, and it must not be reported the same
        way: telling the user to set gem_url would send them to fix a setting
        that is not the problem. This one goes to the log.
        """
        controller = _controller()
        controller.config_service = None

        with patch("webbrowser.open") as browser:
            await controller._open_help_online()

        browser.assert_not_called()
        controller.state_manager.state_to_gui_queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_browser_that_will_not_open_does_not_take_logic_down(self):
        """This runs as a background task in the process that routes speech."""
        controller = _controller()
        controller.config_service = MagicMock()
        controller.config_service.get.return_value = "https://example.test/help"

        with patch("webbrowser.open", side_effect=OSError("no browser")):
            await controller._open_help_online()
