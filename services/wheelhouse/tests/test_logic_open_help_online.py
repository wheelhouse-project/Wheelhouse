"""The Help command, on the Logic side.

The GUI process shows the menu but has no copy of the settings, so choosing
Help sends a command and the Logic process decides what happens. The spoken
command "help" reaches the same method, so both ways in behave the same
(criterion W4 of wh-assistant-button-explainer).

The decision, in order: no settings at all, nothing happens and the log says
why; a blank ai.help.gem_url, the existing notice and nothing else; the
setting ai.help.explain_before_open still true, the GUI process is asked for
the explanation window; otherwise the browser opens.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest


def _controller():
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller._build_gui_handler_map = (
        LogicController._build_gui_handler_map.__get__(controller)
    )
    controller.start_help_online = (
        LogicController.start_help_online.__get__(controller)
    )
    # The real one, not the stand-in: the tests below check what actually
    # reaches the GUI queue, which a stand-in would swallow.
    controller._send_gui_notification = (
        LogicController._send_gui_notification.__get__(controller)
    )
    controller._request_help_explainer = (
        LogicController._request_help_explainer.__get__(controller)
    )
    controller.state_manager = MagicMock()
    controller.create_task_with_error_handling = MagicMock()
    return controller


def _settings(controller, gem_url="https://example.test/help", explain=False):
    """Give the controller settings that answer each key separately."""
    config = MagicMock()
    values = {
        "ai.help.gem_url": gem_url,
        "ai.help.explain_before_open": explain,
    }
    config.get = MagicMock(side_effect=lambda key, default=None: values.get(key, default))
    controller.config_service = config
    return config


def _queued(controller):
    """Every message the controller put on the GUI queue, in order."""
    calls = controller.state_manager.state_to_gui_queue.put_nowait.call_args_list
    return [call[0][0] for call in calls]


class TestTheHandlerIsReachable:

    def test_the_menu_command_routes_to_the_help_handler(self):
        controller = _controller()

        with patch.object(controller, "start_help_online") as opener:
            handler_map = controller._build_gui_handler_map(
                {"action": "open_help_online"}
            )
            handler_map["open_help_online"]()

        opener.assert_called_once()

    def test_the_explained_field_travels_with_the_command(self):
        """The Assistant button's command must skip the window.

        Without this, choosing Assistant would show the window again.
        """
        controller = _controller()

        with patch.object(controller, "start_help_online") as opener:
            handler_map = controller._build_gui_handler_map(
                {"action": "open_help_online", "explained": True}
            )
            handler_map["open_help_online"]()

        opener.assert_called_once_with(explained=True, source="menu")

    def test_the_menu_command_carries_no_explained_field(self):
        controller = _controller()

        with patch.object(controller, "start_help_online") as opener:
            handler_map = controller._build_gui_handler_map(
                {"action": "open_help_online"}
            )
            handler_map["open_help_online"]()

        opener.assert_called_once_with(explained=False, source="menu")


class TestTheExplanationWindow:

    @pytest.mark.asyncio
    async def test_it_asks_for_the_window_and_opens_no_browser(self):
        controller = _controller()
        _settings(controller, explain=True)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_not_called()
        assert _queued(controller) == [{"action": "open_help_explainer"}]

    @pytest.mark.asyncio
    async def test_a_missing_setting_shows_the_window(self):
        """W3: the key defaults to true when absent."""
        controller = _controller()
        config = MagicMock()
        config.get = MagicMock(
            side_effect=lambda key, default=None: (
                "https://example.test/help" if key == "ai.help.gem_url" else default
            )
        )
        controller.config_service = config

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_not_called()
        assert _queued(controller) == [{"action": "open_help_explainer"}]

    @pytest.mark.asyncio
    async def test_the_assistant_button_skips_the_window(self):
        controller = _controller()
        _settings(controller, explain=True)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online(explained=True)

        browser.assert_called_once_with("https://example.test/help")
        assert _queued(controller) == []


class TestOpeningTheHelpPage:

    @pytest.mark.asyncio
    async def test_it_opens_the_configured_address(self):
        controller = _controller()
        config = _settings(controller, explain=False)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        config.get.assert_any_call("ai.help.gem_url", "")
        browser.assert_called_once_with("https://example.test/help")
        assert _queued(controller) == []

    @pytest.mark.asyncio
    async def test_a_blank_address_opens_nothing_and_says_so(self):
        """Blanking the setting is how a user turns online help off.

        Opening a browser on an empty address would show an error page, and
        saying nothing at all would look like the menu is broken. The window
        must not appear either: there is nothing for the Assistant button to
        open (criterion W6).
        """
        controller = _controller()
        _settings(controller, gem_url="", explain=True)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_not_called()
        queued = _queued(controller)
        assert len(queued) == 1
        assert queued[0]["action"] == "show_notification"
        assert queued[0]["message"] == (
            "Online help is not configured. Set gem_url under [ai.help]."
        )

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
            await controller.start_help_online()

        browser.assert_not_called()
        controller.state_manager.state_to_gui_queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_browser_that_will_not_open_does_not_take_logic_down(self):
        """This runs as a background task in the process that routes speech."""
        controller = _controller()
        _settings(controller, explain=False)

        with patch("webbrowser.open", side_effect=OSError("no browser")):
            await controller.start_help_online()


class TestOnlyTheBooleanTrueSkipsTheWindow:
    """Boss ruling condition 1 on wh-assistant-button-explainer.

    The command arrives from another process, so its fields are whatever
    that process put there. ``bool("false")`` is True, and a check written
    that way would let any non-empty string skip the explanation window.
    Only the literal boolean true may skip it.
    """

    @pytest.mark.parametrize(
        "value",
        ["false", "true", "True", 1, 0, None, [], {}],
        ids=[
            "string-false",
            "string-true",
            "string-True-capital",
            "number-one",
            "number-zero",
            "none",
            "empty-list",
            "empty-dict",
        ],
    )
    def test_a_non_boolean_explained_field_takes_the_full_decision(self, value):
        controller = _controller()

        with patch.object(controller, "start_help_online") as opener:
            handler_map = controller._build_gui_handler_map(
                {"action": "open_help_online", "explained": value}
            )
            handler_map["open_help_online"]()

        assert opener.call_args.kwargs["explained"] is False

    def test_the_boolean_true_still_skips_the_window(self):
        controller = _controller()

        with patch.object(controller, "start_help_online") as opener:
            handler_map = controller._build_gui_handler_map(
                {"action": "open_help_online", "explained": True}
            )
            handler_map["open_help_online"]()

        assert opener.call_args.kwargs["explained"] is True


class TestTheNoticeSurvivesTheExplainedPath:
    """Boss ruling condition 2 on wh-assistant-button-explainer.

    The explanation window is modeless, so the settings can change while it
    is open. A user who blanks ai.help.gem_url and then chooses Assistant
    must get the same notice as anyone else, not an empty browser tab.
    """

    @pytest.mark.asyncio
    async def test_a_blank_address_gives_the_notice_when_explained_is_true(self):
        controller = _controller()
        _settings(controller, gem_url="", explain=False)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online(explained=True)

        browser.assert_not_called()
        queued = _queued(controller)
        assert len(queued) == 1
        assert queued[0]["action"] == "show_notification"
        assert queued[0]["message"] == (
            "Online help is not configured. Set gem_url under [ai.help]."
        )


class TestTheLogNamesThePathTaken:
    """Boss ruling condition 3 on wh-assistant-button-explainer.

    Both ways in reach one method, so the log is the only place that says
    which one ran and what it decided. One INFO line per run, naming the
    source, whether the explanation was already given, and the outcome.
    """

    @pytest.mark.asyncio
    async def test_asking_for_the_window_is_logged_with_its_source(self, caplog):
        controller = _controller()
        _settings(controller, explain=True)

        with caplog.at_level(logging.INFO):
            with patch("webbrowser.open"):
                await controller.start_help_online(source="spoken")

        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len(lines) == 1
        assert "source=spoken" in lines[0]
        assert "explained=False" in lines[0]
        assert "explanation window" in lines[0]

    @pytest.mark.asyncio
    async def test_opening_the_browser_is_logged_with_its_source(self, caplog):
        controller = _controller()
        _settings(controller, explain=False)

        with caplog.at_level(logging.INFO):
            with patch("webbrowser.open"):
                await controller.start_help_online(source="window", explained=True)

        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len(lines) == 1
        assert "source=window" in lines[0]
        assert "explained=True" in lines[0]
        assert "browser" in lines[0]

    @pytest.mark.asyncio
    async def test_the_unconfigured_notice_is_logged_with_its_source(self, caplog):
        controller = _controller()
        _settings(controller, gem_url="", explain=True)

        with caplog.at_level(logging.INFO):
            with patch("webbrowser.open"):
                await controller.start_help_online(source="menu")

        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len(lines) == 1
        assert "source=menu" in lines[0]
        assert "not configured" in lines[0]

    def test_the_menu_command_names_menu_as_its_source(self):
        controller = _controller()

        with patch.object(controller, "start_help_online") as opener:
            handler_map = controller._build_gui_handler_map(
                {"action": "open_help_online"}
            )
            handler_map["open_help_online"]()

        assert opener.call_args.kwargs["source"] == "menu"

    def test_the_command_carries_the_source_it_was_given(self):
        controller = _controller()

        with patch.object(controller, "start_help_online") as opener:
            handler_map = controller._build_gui_handler_map(
                {"action": "open_help_online", "explained": True, "source": "window"}
            )
            handler_map["open_help_online"]()

        assert opener.call_args.kwargs["source"] == "window"


class TestABrowserThatReportsFailureByReturningFalse:
    """Finding wh-assistant-button-explainer.1.1.

    On Windows, webbrowser.open catches the OSError that os.startfile raises
    and returns False instead of raising. This project measured that shape
    and recorded it as wh-open-url-action.1.2; the two browser launches in
    speech/actions.py, at lines 1113 and 1155, both read the return value for
    that reason. Help read only the exception, so a browser that would not
    start opened nothing and told the user nothing, while the log line said
    the browser was opening.

    The address is a user setting, so it stays out of the log line. The two
    wrappers in speech/actions.py redact what they log for the same reason.
    """

    @pytest.mark.asyncio
    async def test_a_false_return_tells_the_user(self):
        controller = _controller()
        _settings(controller, explain=False)

        with patch("webbrowser.open", return_value=False):
            await controller.start_help_online()

        queued = _queued(controller)
        assert len(queued) == 1
        assert queued[0]["action"] == "show_notification"
        assert queued[0]["message"] == "Wheelhouse could not open your browser."

    @pytest.mark.asyncio
    async def test_a_false_return_is_logged_as_a_warning(self, caplog):
        controller = _controller()
        _settings(controller, explain=False)

        with caplog.at_level(logging.WARNING):
            with patch("webbrowser.open", return_value=False):
                await controller.start_help_online()

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "browser" in warnings[0]

    @pytest.mark.asyncio
    async def test_the_warning_does_not_hold_the_address(self, caplog):
        """The address is a user setting and does not belong in the log."""
        controller = _controller()
        _settings(controller, gem_url="https://example.test/private-help")

        with caplog.at_level(logging.WARNING):
            with patch("webbrowser.open", return_value=False):
                await controller.start_help_online()

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "example.test" not in warnings[0]
        assert "private-help" not in warnings[0]

    @pytest.mark.asyncio
    async def test_a_true_return_tells_the_user_nothing(self):
        """The counterpart, so the notice is tied to the false return alone.

        TestOpeningTheHelpPage.test_it_opens_the_configured_address already
        covers a truthy return, because an unconfigured patch answers with a
        MagicMock. This case pins the literal True.
        """
        controller = _controller()
        _settings(controller, explain=False)

        with patch("webbrowser.open", return_value=True) as browser:
            await controller.start_help_online()

        browser.assert_called_once_with("https://example.test/help")
        assert _queued(controller) == []


class TestTheRetiredChatGPTAddressOpensTheGemInstead:
    """wh-gem-replaces-gpt-assistant.2.3, the Codex finding.

    Releases before 1.2.0 shipped the ChatGPT custom GPT address as the
    gem_url default. The installer preserves the user's settings file across
    an update, and start_help_online reads ai.help.gem_url with an empty
    default, so every installation that exists today keeps opening a custom
    GPT that OpenAI stops running on 2026-12-11. Nothing else on this branch
    reaches those users.

    The fix reads only the exact retired address. A user who chose their own
    address, and a user who blanked the setting, are both left alone.
    """

    @pytest.mark.asyncio
    async def test_the_retired_address_opens_the_gem(self):
        import main

        controller = _controller()
        _settings(controller, gem_url=main._RETIRED_CHATGPT_HELP_URL, explain=False)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_called_once_with(main._WHEELHOUSE_GEM_URL)
        assert _queued(controller) == []

    @pytest.mark.asyncio
    async def test_surrounding_whitespace_does_not_defeat_the_check(self):
        """A hand-edited settings file can carry a stray space."""
        import main

        controller = _controller()
        _settings(
            controller,
            gem_url="  " + main._RETIRED_CHATGPT_HELP_URL + "  ",
            explain=False,
        )

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_called_once_with(main._WHEELHOUSE_GEM_URL)

    @pytest.mark.asyncio
    async def test_a_custom_address_opens_unchanged(self):
        """Only the one retired address is replaced, never anything else."""
        controller = _controller()
        _settings(controller, gem_url="https://example.test/my-own-help", explain=False)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_called_once_with("https://example.test/my-own-help")

    @pytest.mark.asyncio
    async def test_another_chatgpt_address_opens_unchanged(self):
        """A different ChatGPT address is a choice, not the shipped default."""
        controller = _controller()
        _settings(
            controller,
            gem_url="https://chatgpt.com/g/g-0000000000000000000000000000-other",
            explain=False,
        )

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_called_once_with(
            "https://chatgpt.com/g/g-0000000000000000000000000000-other"
        )

    @pytest.mark.asyncio
    async def test_a_blank_address_still_shows_the_notice(self):
        """Blanking the setting is still how a user turns online help off.

        The substitution must not resurrect help for someone who switched it
        off, so this pins the blank path against the new code.
        """
        controller = _controller()
        _settings(controller, gem_url="", explain=True)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_not_called()
        queued = _queued(controller)
        assert len(queued) == 1
        # The action is checked before the message so a mutation that queues
        # the explanation window here fails on a named assertion rather than
        # a KeyError, which the mutation gate reports as an error, not a
        # catch.
        assert queued[0]["action"] == "show_notification"
        assert queued[0]["message"] == (
            "Online help is not configured. Set gem_url under [ai.help]."
        )

    @pytest.mark.asyncio
    async def test_the_substitution_is_logged_without_an_address(self, caplog):
        """The same redaction rule as every other line here.

        wh-assistant-button-explainer.1.1: the address is a user setting and
        does not belong in the log.
        """
        import main

        controller = _controller()
        _settings(controller, gem_url=main._RETIRED_CHATGPT_HELP_URL, explain=False)

        with caplog.at_level(logging.INFO):
            with patch("webbrowser.open"):
                await controller.start_help_online()

        messages = [
            r.getMessage() for r in caplog.records if r.levelno == logging.INFO
        ]
        substitutions = [m for m in messages if "retired" in m.lower()]
        assert len(substitutions) == 1
        assert "chatgpt.com" not in substitutions[0]
        assert "gemini.google.com" not in substitutions[0]

    @pytest.mark.asyncio
    async def test_the_explanation_window_still_comes_first(self, caplog):
        """The substitution happens at the browser, not before the window.

        A user who has not turned the window off must still see it, and the
        log must not claim a substitution that has not happened yet.
        """
        import main

        controller = _controller()
        _settings(controller, gem_url=main._RETIRED_CHATGPT_HELP_URL, explain=True)

        with caplog.at_level(logging.INFO):
            with patch("webbrowser.open") as browser:
                await controller.start_help_online()

        browser.assert_not_called()
        assert _queued(controller) == [{"action": "open_help_explainer"}]
        messages = [
            r.getMessage() for r in caplog.records if r.levelno == logging.INFO
        ]
        assert [m for m in messages if "retired" in m.lower()] == []

    def test_the_gem_constant_equals_the_shipped_default(self):
        """The two copies of the Gem address cannot drift apart.

        One lives in services/wheelhouse/config.toml.example as the shipped
        gem_url default; the other is the constant this fallback opens. A
        change to either alone would silently send updated users somewhere
        the fresh installs never go.
        """
        import pathlib
        import tomllib

        import main

        example = (
            pathlib.Path(main.__file__).resolve().parent / "config.toml.example"
        )
        shipped = tomllib.loads(example.read_text(encoding="utf-8"))
        assert shipped["ai"]["help"]["gem_url"] == main._WHEELHOUSE_GEM_URL
