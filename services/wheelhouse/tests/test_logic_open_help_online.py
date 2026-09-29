"""The Help command, on the Logic side.

The GUI process shows the menu but has no copy of the settings, so choosing
Help sends a command and the Logic process decides what happens. The spoken
command "help" reaches the same method, so both ways in behave the same
(criterion W4 of wh-assistant-button-explainer).

The decision, in order: no settings at all, nothing happens and the log says
why; a blank ai.help.gem_url, the existing notice and nothing else; the
setting ai.help.explain_before_open still true, the GUI process is asked for
the explanation window; otherwise the browser opens.

wh-assistant-explainer-once-more adds one case: the setting false and no
marker file (help_explainer_notebook_shown.toml) still shows the window,
once, with its check box ticked. The Assistant button's command carries the
check box state, and Logic saves the setting when the value changes, then
writes the marker; a failed write leaves the marker unwritten and shows a
notice (wh-assistant-explainer-once-more.1.1).
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from click_first_use_hint import load_hint_shown, mark_hint_shown


@pytest.fixture(autouse=True)
def marker(tmp_path, monkeypatch):
    """Redirect the once-more marker file into tmp_path, already written.

    Every test in this file that predates wh-assistant-explainer-once-more
    describes a user who has already seen the window after the move to the
    Gemini Notebook, so the marker exists by default. A test for the
    once-more showing deletes it first.
    """
    import main

    path = tmp_path / "help_explainer_notebook_shown.toml"
    assert mark_hint_shown(path) is True
    monkeypatch.setattr(
        main, "default_help_explainer_marker_path", lambda: path, raising=False
    )
    return path


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """Replace webbrowser.open and os.startfile with a guard that opens
    nothing, and fail the test when code reaches the guard.

    A test that expects the browser patches webbrowser.open itself, and that
    patch replaces the guard for the length of the test. Code that reaches
    the guard instead (an unpatched test, or a mutant in a mutation gate)
    used to open the real browser at https://example.test/help. The guard
    returns False, the answer webbrowser.open gives when no browser starts,
    and the teardown fails the test.
    """
    import os
    import webbrowser

    reached = []

    def refuse(*args, **kwargs):
        reached.append(args)
        return False

    refuse.refuses_the_real_browser = True
    monkeypatch.setattr(webbrowser, "open", refuse)
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", refuse)
    yield
    assert not reached, f"a test reached the real browser opener: {reached!r}"


def test_no_test_in_this_file_can_reach_the_real_browser():
    """David, 2026-09-28: his browser kept opening https://example.test/help
    while tests and mutation sweeps ran. Every test here runs with
    webbrowser.open and os.startfile replaced by a guard."""
    import os
    import webbrowser

    assert getattr(webbrowser.open, "refuses_the_real_browser", False)
    if hasattr(os, "startfile"):
        assert getattr(os.startfile, "refuses_the_real_browser", False)


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
    # These stand-ins hold what is on disk too; the tests with the real
    # ConfigService cover a live value that differs from the file.
    config.get_persisted = MagicMock(side_effect=config.get.side_effect)
    config.save = AsyncMock(return_value=True)
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

        opener.assert_called_once_with(explained=True, source="menu", do_not_show_again=None)

    def test_the_menu_command_carries_no_explained_field(self):
        controller = _controller()

        with patch.object(controller, "start_help_online") as opener:
            handler_map = controller._build_gui_handler_map(
                {"action": "open_help_online"}
            )
            handler_map["open_help_online"]()

        opener.assert_called_once_with(explained=False, source="menu", do_not_show_again=None)


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
        # An async save like the real one, so a mutant that saves while
        # showing the window reaches an assertion instead of a TypeError.
        config.save = AsyncMock(return_value=True)
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


# The two addresses below are written out, not read from main, on purpose:
# before wh-assistant-gemini-notebook, main._WHEELHOUSE_GEM_URL WAS the old
# Gem address, so a test that compared against the constant would pass
# without the substitution.
_OLD_GEM_URL = "https://gemini.google.com/gem/1z3my7h0wNiR2msZW8_NAEzxboZOTjN2A"
_NOTEBOOK_URL = (
    "https://notebook.google.com/notebook/"
    "da51a404-67ec-4804-9ebe-83605df3e9cf/preview"
)


class TestTheOldGemAddressOpensTheNotebookInstead:
    """wh-assistant-gemini-notebook, acceptance criterion 3.

    Release 1.2.0 shipped the Gemini Gem address as the gem_url default, and
    Google ends Gems on 2026-11-17. The installer preserves the user's
    settings file across an update, so an installation made with 1.2.0 still
    holds the Gem address. start_help_online must open the Gemini Notebook
    chat view instead, in the same way it already replaces the retired
    ChatGPT address. An address the user chose is still opened as written;
    TestTheRetiredChatGPTAddressOpensTheGemInstead.
    test_a_custom_address_opens_unchanged covers an ordinary custom address.
    """

    def test_the_shipped_address_is_the_notebook_chat_view(self):
        """The constant every substitution opens is the notebook's chat view."""
        import main

        assert main._WHEELHOUSE_GEM_URL == _NOTEBOOK_URL

    @pytest.mark.asyncio
    async def test_the_old_gem_address_opens_the_notebook(self):
        controller = _controller()
        _settings(controller, gem_url=_OLD_GEM_URL, explain=False)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_called_once_with(_NOTEBOOK_URL)
        assert _queued(controller) == []

    @pytest.mark.asyncio
    async def test_surrounding_whitespace_does_not_defeat_the_gem_check(self):
        """A hand-edited settings file can carry a stray space."""
        controller = _controller()
        _settings(controller, gem_url="  " + _OLD_GEM_URL + "  ", explain=False)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_called_once_with(_NOTEBOOK_URL)

    @pytest.mark.asyncio
    async def test_the_retired_chatgpt_address_still_opens_the_notebook(self):
        """The earlier substitution now leads to the notebook too."""
        import main

        controller = _controller()
        _settings(controller, gem_url=main._RETIRED_CHATGPT_HELP_URL, explain=False)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_called_once_with(_NOTEBOOK_URL)

    @pytest.mark.asyncio
    async def test_another_gem_address_opens_unchanged(self):
        """A different Gem is a choice the user made, not the shipped default."""
        controller = _controller()
        _settings(
            controller,
            gem_url="https://gemini.google.com/gem/0000000000000000000000000000000",
            explain=False,
        )

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_called_once_with(
            "https://gemini.google.com/gem/0000000000000000000000000000000"
        )

    @pytest.mark.asyncio
    async def test_the_gem_substitution_is_logged_without_an_address(self, caplog):
        """The same redaction rule as every other line in start_help_online."""
        controller = _controller()
        _settings(controller, gem_url=_OLD_GEM_URL, explain=False)

        with caplog.at_level(logging.INFO):
            with patch("webbrowser.open"):
                await controller.start_help_online()

        messages = [
            r.getMessage() for r in caplog.records if r.levelno == logging.INFO
        ]
        substitutions = [m for m in messages if "old wheelhouse gem" in m.lower()]
        assert len(substitutions) == 1
        assert "gemini.google.com" not in substitutions[0]
        assert "notebook.google.com" not in substitutions[0]


class TestTheWindowOnceMoreAfterTheNotebookMove:
    """wh-assistant-explainer-once-more, design A (boss ruling 07:32).

    A user who ticked "Do not show this again" in 1.2.0 holds
    ai.help.explain_before_open = false. The assistant then moved to a
    Gemini Notebook, which takes only a Google Account, and only the window
    says so. The marker file records that the user has chosen in the window
    since that move; with the setting false and no marker, the window is
    shown once more, with the check box already ticked.
    """

    @pytest.mark.asyncio
    async def test_false_and_no_marker_shows_the_window_pre_ticked(self, marker):
        """Case (a)."""
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=False)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_not_called()
        assert _queued(controller) == [
            {"action": "open_help_explainer", "start_ticked": True}
        ]

    @pytest.mark.asyncio
    async def test_false_and_the_marker_opens_the_browser(self, marker):
        """Case (b)."""
        controller = _controller()
        _settings(controller, explain=False)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_called_once_with("https://example.test/help")
        assert _queued(controller) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "marker_present", [False, True], ids=["no-marker", "marker"]
    )
    async def test_true_shows_the_window_without_the_tick(
        self, marker, marker_present
    ):
        """Case (c): users who never turned the window off see no change."""
        if not marker_present:
            marker.unlink()
        controller = _controller()
        _settings(controller, explain=True)

        with patch("webbrowser.open") as browser:
            await controller.start_help_online()

        browser.assert_not_called()
        assert _queued(controller) == [{"action": "open_help_explainer"}]

    @pytest.mark.asyncio
    async def test_showing_the_window_writes_nothing(self, marker):
        """Only the Assistant button uses up the once-more showing."""
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=False)

        with patch("webbrowser.open"):
            await controller.start_help_online()

        assert not marker.exists()
        controller.config_service.save.assert_not_called()


class TestTheAssistantButtonRecordsTheChoice:
    """Cases (d)-(g) and (i): Logic, not the GUI, writes the setting now.

    Every ConfigService.save rewrites the whole settings file and drops the
    user's comments, so a write happens only when the value really changes.
    """

    @staticmethod
    async def _press_assistant(controller, ticked):
        with patch("webbrowser.open") as browser:
            await controller.start_help_online(
                explained=True, source="window", do_not_show_again=ticked
            )
        return browser

    @pytest.mark.asyncio
    async def test_ticked_with_the_setting_false_saves_no_config(self, marker):
        """Case (d)."""
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=False)

        browser = await self._press_assistant(controller, True)

        assert load_hint_shown(marker) is True
        controller.config_service.save.assert_not_called()
        browser.assert_called_once_with("https://example.test/help")

    @pytest.mark.asyncio
    async def test_ticked_with_the_setting_true_saves_false(self, marker):
        """Case (e)."""
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=True)

        await self._press_assistant(controller, True)

        assert load_hint_shown(marker) is True
        controller.config_service.save.assert_awaited_once_with(
            values={"ai.help.explain_before_open": False}
        )

    @pytest.mark.asyncio
    async def test_unticked_with_the_setting_false_saves_true(self, marker):
        """Case (f): the box is what the user chose, so the window returns."""
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=False)

        await self._press_assistant(controller, False)

        assert load_hint_shown(marker) is True
        controller.config_service.save.assert_awaited_once_with(
            values={"ai.help.explain_before_open": True}
        )

    @pytest.mark.asyncio
    async def test_unticked_with_the_setting_true_saves_no_config(self, marker):
        """Case (g)."""
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=True)

        await self._press_assistant(controller, False)

        assert load_hint_shown(marker) is True
        controller.config_service.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_choice_is_recorded_when_help_is_not_configured(
        self, marker
    ):
        """The address can be blanked while the modeless window is open; the
        choice the user made in the window is still recorded."""
        marker.unlink()
        controller = _controller()
        _settings(controller, gem_url="", explain=True)

        await self._press_assistant(controller, True)

        assert load_hint_shown(marker) is True
        controller.config_service.save.assert_awaited_once_with(
            values={"ai.help.explain_before_open": False}
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "explain", [False, True], ids=["setting-false", "setting-true"]
    )
    async def test_a_command_without_the_box_state_writes_nothing(
        self, marker, explain
    ):
        """Case (i): Cancel, Escape, and the X send nothing to Logic. The
        menu, the spoken command, and an explained command without the box
        state record no choice either."""
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=explain)

        with patch("webbrowser.open"):
            await controller.start_help_online(explained=True, source="window")
            await controller.start_help_online(source="menu")

        assert not marker.exists()
        controller.config_service.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_missing_setting_counts_as_true_for_the_choice(self, marker):
        """W3 applies to the choice too: an absent key reads as true.

        A user who never touched the setting and presses Assistant with the
        box clear changes nothing, so the settings file must not be
        rewritten (every save drops the user's comments). Found by the
        mutation gate (the-choice-reads-a-missing-setting-as-false): every
        other test here names the setting explicitly.
        """
        marker.unlink()
        controller = _controller()
        config = MagicMock()
        config.get = MagicMock(
            side_effect=lambda key, default=None: (
                "https://example.test/help" if key == "ai.help.gem_url" else default
            )
        )
        config.get_persisted = MagicMock(side_effect=config.get.side_effect)
        config.save = AsyncMock(return_value=True)
        controller.config_service = config

        await self._press_assistant(controller, False)

        assert load_hint_shown(marker) is True
        controller.config_service.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_existing_marker_is_not_rewritten(self, marker):
        before = marker.read_bytes()
        controller = _controller()
        _settings(controller, explain=False)

        await self._press_assistant(controller, True)

        assert marker.read_bytes() == before


CHOICE_NOT_SAVED = {
    "action": "show_notification",
    "title": "Wheelhouse",
    "message": (
        "Wheelhouse could not save your choice from the Assistant window. "
        "The window can appear again when you choose Help."
    ),
    "timeout": 5,
}


class TestAFailedWriteKeepsTheChoiceOpen:
    """wh-assistant-explainer-once-more.1.1: the setting is saved before the
    marker is written, a failed save writes no marker, and every failed
    write tells the user.

    The marker ends the once-more showing. Written before a save that then
    fails, it would keep a false setting whose change the user asked for,
    and the window that offers the choice would never come back. Before the
    choice moved to Logic, the GUI sent this save through its acknowledged
    settings path, which shows a notice on failure; Logic now shows the
    notice itself.
    """

    @staticmethod
    async def _press_assistant(controller, ticked):
        with patch("webbrowser.open") as browser:
            await controller.start_help_online(
                explained=True, source="window", do_not_show_again=ticked
            )
        return browser

    @pytest.mark.asyncio
    async def test_the_setting_is_saved_before_the_marker(self, marker):
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=False)
        marker_at_save = []

        async def save(*, values):
            marker_at_save.append(marker.exists())
            return True

        controller.config_service.save = AsyncMock(side_effect=save)

        await self._press_assistant(controller, False)

        assert marker_at_save == [False]
        assert load_hint_shown(marker) is True
        assert CHOICE_NOT_SAVED not in _queued(controller)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome",
        [False, OSError("the disk is full")],
        ids=["returns-false", "raises"],
    )
    @pytest.mark.parametrize(
        "explain, ticked",
        [(False, False), (True, True)],
        ids=["re-enable", "turn-off"],
    )
    async def test_a_failed_save_writes_no_marker_and_says_so(
        self, marker, outcome, explain, ticked
    ):
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=explain)
        if isinstance(outcome, Exception):
            controller.config_service.save = AsyncMock(side_effect=outcome)
        else:
            controller.config_service.save = AsyncMock(return_value=outcome)

        browser = await self._press_assistant(controller, ticked)

        assert not marker.exists()
        assert _queued(controller) == [CHOICE_NOT_SAVED]
        # The browser still opens: the user asked for the assistant.
        browser.assert_called_once_with("https://example.test/help")

    @pytest.mark.asyncio
    async def test_the_window_comes_back_after_a_failed_save(self, marker):
        """Restart after a partial choice: a user with the setting false
        clears the box, the save fails, and the next Help shows the
        once-more window again, so the choice can be made again."""
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=False)
        controller.config_service.save = AsyncMock(return_value=False)
        await self._press_assistant(controller, False)

        restarted = _controller()
        _settings(restarted, explain=False)
        with patch("webbrowser.open") as browser:
            await restarted.start_help_online()

        browser.assert_not_called()
        assert _queued(restarted) == [
            {"action": "open_help_explainer", "start_ticked": True}
        ]

    @pytest.mark.asyncio
    async def test_a_marker_that_cannot_be_written_is_reported(
        self, marker, monkeypatch, tmp_path
    ):
        """A file where the data folder should be makes the real writer
        fail. No save is needed (ticked, setting already false), so the
        marker is the whole choice."""
        import main

        blocker = tmp_path / "not-a-folder"
        blocker.write_text("")
        monkeypatch.setattr(
            main,
            "default_help_explainer_marker_path",
            lambda: blocker / "help_explainer_notebook_shown.toml",
        )
        controller = _controller()
        _settings(controller, explain=False)

        browser = await self._press_assistant(controller, True)

        controller.config_service.save.assert_not_called()
        assert _queued(controller) == [CHOICE_NOT_SAVED]
        browser.assert_called_once_with("https://example.test/help")

    @pytest.mark.asyncio
    async def test_a_marker_writer_that_raises_is_reported(self, marker):
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=False)

        with patch(
            "services.wheelhouse.click_first_use_hint.mark_hint_shown",
            side_effect=RuntimeError("no writer"),
        ):
            await self._press_assistant(controller, True)

        assert not marker.exists()
        assert _queued(controller) == [CHOICE_NOT_SAVED]

    @pytest.mark.asyncio
    async def test_no_state_manager_writes_no_marker(self, marker):
        """Without a state manager the setting cannot be saved, so the
        choice is not complete and the marker stays unwritten."""
        import main

        marker.unlink()
        config = MagicMock()
        config.get = MagicMock(
            side_effect=lambda key, default=None: (
                False if key == "ai.help.explain_before_open" else default
            )
        )
        config.get_persisted = MagicMock(side_effect=config.get.side_effect)
        config.save = AsyncMock(return_value=True)

        notice = await main._record_help_explainer_choice(config, None, False)

        assert not marker.exists()
        assert notice == CHOICE_NOT_SAVED["message"]
        config.save.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "explain, ticked",
        [(False, True), (True, True), (False, False), (True, False)],
        ids=["no-save-ticked", "save-false", "save-true", "no-save-clear"],
    )
    async def test_a_kept_choice_shows_no_notice(self, marker, explain, ticked):
        marker.unlink()
        controller = _controller()
        _settings(controller, explain=explain)

        await self._press_assistant(controller, ticked)

        assert load_hint_shown(marker) is True
        assert _queued(controller) == []


class TestARetryAfterAFailedSaveKeepsTheChoice:
    """wh-assistant-explainer-once-more.1.1, round 2: the real ConfigService.

    A save without a staged value changes the live setting first and does
    not undo that change when the file cannot be replaced. The live value
    then matches the choice while the file does not, so a second press of
    the Assistant button in the same process saved nothing and wrote the
    marker; after a restart, the file's old value plus the marker hid the
    choice for good. The choice is now compared with the value on disk
    (get_persisted), and saved with the staged save, which changes the
    live value only when the file was replaced.
    """

    KEY = "ai.help.explain_before_open"

    @staticmethod
    def _settings_file(tmp_path, explain):
        path = tmp_path / "settings.toml"
        path.write_text(
            "[ai.help]\n"
            'gem_url = "https://example.test/help"\n'
            f"explain_before_open = {'true' if explain else 'false'}\n"
        )
        return path

    @staticmethod
    def _block_the_settings_file(monkeypatch, path):
        """Make the atomic replace of this one file fail, as a read-only
        settings file does. Every other replace (the marker writer uses
        one) still works. Returns a switch that ends the failure."""
        import os

        real_replace = os.replace
        target = os.path.normcase(os.path.abspath(path))
        failing = {"on": True}

        def replace(src, dst, *args, **kwargs):
            if failing["on"] and os.path.normcase(os.path.abspath(dst)) == target:
                raise PermissionError("the settings file is read-only")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace)
        return failing

    @staticmethod
    def _controller_with(config):
        from queue import Queue

        from state_manager import StateManager

        controller = _controller()
        controller.config_service = config
        controller.state_manager = StateManager(
            config, MagicMock(), asyncio.get_running_loop(), Queue(), None
        )
        return controller

    @staticmethod
    def _drain(controller):
        """Every message on the real GUI queue, in order, then empty it."""
        queue = controller.state_manager.state_to_gui_queue
        messages = []
        while not queue.empty():
            messages.append(queue.get_nowait())
        return messages

    @classmethod
    def _notices(cls, messages):
        return [m for m in messages if m.get("action") == "show_notification"]

    @staticmethod
    async def _press_assistant(controller, ticked):
        with patch("webbrowser.open") as browser:
            await controller.start_help_online(
                explained=True, source="window", do_not_show_again=ticked
            )
        return browser

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "on_disk, ticked",
        [(False, False), (True, True)],
        ids=["re-enable", "turn-off"],
    )
    async def test_a_second_press_after_a_failed_save_keeps_the_choice(
        self, marker, tmp_path, monkeypatch, on_disk, ticked
    ):
        from config_service import ConfigService

        marker.unlink()
        path = self._settings_file(tmp_path, on_disk)
        config = ConfigService(str(path))
        controller = self._controller_with(config)
        self._block_the_settings_file(monkeypatch, path)

        for attempt in (1, 2):
            await self._press_assistant(controller, ticked)
            messages = self._drain(controller)
            assert self._notices(messages) == [CHOICE_NOT_SAVED], attempt
            assert not marker.exists(), attempt
            # The live value is unchanged: the file still holds the old one.
            assert config.get(self.KEY) is on_disk, attempt
            assert config.get_persisted(self.KEY) is on_disk, attempt

        # The next Help, in the same process, offers the choice again.
        with patch("webbrowser.open") as browser:
            await controller.start_help_online()
        browser.assert_not_called()
        window = [
            m for m in self._drain(controller)
            if m.get("action") == "open_help_explainer"
        ]
        assert window == [
            {"action": "open_help_explainer", "start_ticked": True}
            if not on_disk
            else {"action": "open_help_explainer"}
        ]

        # After a restart the file still holds the old value, and no marker
        # hides the window that offers the choice.
        assert ConfigService(str(path)).get(self.KEY) is on_disk
        assert not marker.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "on_disk, ticked",
        [(False, False), (True, True)],
        ids=["re-enable", "turn-off"],
    )
    async def test_a_press_after_the_file_is_writable_again_keeps_the_choice(
        self, marker, tmp_path, monkeypatch, on_disk, ticked
    ):
        """A successful staged save changes the live value too, and a state
        update follows it."""
        from config_service import ConfigService

        marker.unlink()
        path = self._settings_file(tmp_path, on_disk)
        config = ConfigService(str(path))
        controller = self._controller_with(config)
        failing = self._block_the_settings_file(monkeypatch, path)
        await self._press_assistant(controller, ticked)
        self._drain(controller)

        failing["on"] = False
        await self._press_assistant(controller, ticked)
        messages = self._drain(controller)

        assert self._notices(messages) == []
        assert any(m.get("action") == "state_update" for m in messages)
        assert load_hint_shown(marker) is True
        assert config.get(self.KEY) is (not on_disk)
        assert ConfigService(str(path)).get(self.KEY) is (not on_disk)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "on_disk, ticked",
        [(False, False), (True, True)],
        ids=["re-enable", "turn-off"],
    )
    async def test_a_live_value_that_never_reached_the_file_is_saved(
        self, marker, tmp_path, on_disk, ticked
    ):
        """The comparison uses the value on disk. A live value that already
        matches the choice but was never saved still needs the save."""
        from config_service import ConfigService

        marker.unlink()
        path = self._settings_file(tmp_path, on_disk)
        config = ConfigService(str(path))
        config.set(self.KEY, not on_disk)
        controller = self._controller_with(config)

        await self._press_assistant(controller, ticked)

        assert self._notices(self._drain(controller)) == []
        assert load_hint_shown(marker) is True
        assert ConfigService(str(path)).get(self.KEY) is (not on_disk)

    @pytest.mark.asyncio
    async def test_the_save_waits_for_a_gui_settings_write(self, marker, tmp_path):
        """The save takes the lock StateManager._save_gui_settings holds, so
        a GUI settings write and its acknowledgement are never interleaved
        with it."""
        from config_service import ConfigService

        marker.unlink()
        path = self._settings_file(tmp_path, False)
        config = ConfigService(str(path))
        controller = self._controller_with(config)
        real_save = config.save
        saves = []

        async def save(**kwargs):
            saves.append(kwargs)
            return await real_save(**kwargs)

        config.save = save
        lock = asyncio.Lock()
        controller.state_manager._gui_settings_lock = lock
        await lock.acquire()
        press = asyncio.create_task(self._press_assistant(controller, False))
        for _ in range(20):
            await asyncio.sleep(0)
        waited = saves == []
        lock.release()
        await press

        assert waited, "the save ran while a GUI settings write held the lock"
        assert saves == [{"values": {self.KEY: True}}]
        assert ConfigService(str(path)).get(self.KEY) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "on_disk, first_ticked",
        [(True, True), (False, False)],
        ids=["tick-then-clear", "clear-then-tick"],
    )
    async def test_a_later_opposite_choice_waits_for_a_pending_save(
        self, marker, tmp_path, on_disk, first_ticked
    ):
        """wh-assistant-explainer-once-more.1.3: the window hides at once,
        so the user can choose again while the first save is still pending.
        The staged save leaves the value on disk unchanged until it ends,
        so the second choice must compare under the same lock; otherwise it
        matches the old value, saves nothing, and the first save then
        replaces the user's last choice."""
        from config_service import ConfigService

        marker.unlink()
        path = self._settings_file(tmp_path, on_disk)
        config = ConfigService(str(path))
        controller = self._controller_with(config)
        real_save = config.save
        release = asyncio.Event()
        saves = []

        async def save(**kwargs):
            saves.append(kwargs)
            if len(saves) == 1:
                await release.wait()
            return await real_save(**kwargs)

        config.save = save

        def press(ticked):
            return controller.start_help_online(
                explained=True, source="window", do_not_show_again=ticked
            )

        # One patch around both presses. A patch per press, as
        # _press_assistant makes, is undone by the first press to finish
        # while the second is still running, and the second then opened
        # the real browser (David, 2026-09-28).
        with patch("webbrowser.open") as browser:
            first = asyncio.create_task(press(first_ticked))
            for _ in range(20):
                await asyncio.sleep(0)
            assert len(saves) == 1
            second = asyncio.create_task(press(not first_ticked))
            for _ in range(20):
                await asyncio.sleep(0)
            release.set()
            await first
            await second

        assert browser.call_count == 2
        assert saves == [
            {"values": {self.KEY: on_disk is False}},
            {"values": {self.KEY: on_disk}},
        ]
        assert config.get(self.KEY) is on_disk
        assert ConfigService(str(path)).get(self.KEY) is on_disk
        assert self._notices(self._drain(controller)) == []

    @pytest.mark.asyncio
    async def test_the_lock_is_created_when_the_manager_has_none(
        self, marker, tmp_path
    ):
        """StateManager creates its lock on first use; so does this save,
        and the one it creates is the one StateManager then uses."""
        from config_service import ConfigService

        marker.unlink()
        path = self._settings_file(tmp_path, False)
        config = ConfigService(str(path))
        controller = self._controller_with(config)
        assert not hasattr(controller.state_manager, "_gui_settings_lock")

        await self._press_assistant(controller, False)

        assert isinstance(
            getattr(controller.state_manager, "_gui_settings_lock", None),
            asyncio.Lock,
        )
        assert ConfigService(str(path)).get(self.KEY) is True

    @pytest.mark.asyncio
    async def test_a_matching_file_is_not_rewritten(self, marker, tmp_path):
        """No save when the value on disk already matches: every save
        rewrites the whole file and drops the user's comments."""
        from config_service import ConfigService

        marker.unlink()
        path = self._settings_file(tmp_path, False)
        path.write_text("# the user's comment\n" + path.read_text())
        before = path.read_bytes()
        config = ConfigService(str(path))
        controller = self._controller_with(config)

        await self._press_assistant(controller, True)

        assert path.read_bytes() == before
        assert load_hint_shown(marker) is True
        assert self._notices(self._drain(controller)) == []


class TestTheBoxStateTravelsWithTheCommand:

    def test_the_box_state_reaches_start_help_online(self):
        controller = _controller()

        with patch.object(controller, "start_help_online") as opener:
            handler_map = controller._build_gui_handler_map({
                "action": "open_help_online",
                "explained": True,
                "source": "window",
                "do_not_show_again": True,
            })
            handler_map["open_help_online"]()

        opener.assert_called_once_with(
            explained=True, source="window", do_not_show_again=True
        )

    @pytest.mark.parametrize("value", ["false", "true", 1, 0, None, []])
    def test_only_a_real_boolean_is_passed_on(self, value):
        """The field crosses a process boundary, like ``explained``."""
        controller = _controller()

        with patch.object(controller, "start_help_online") as opener:
            handler_map = controller._build_gui_handler_map({
                "action": "open_help_online",
                "explained": True,
                "do_not_show_again": value,
            })
            handler_map["open_help_online"]()

        assert opener.call_args.kwargs["do_not_show_again"] is None

    def test_the_marker_lives_in_the_data_folder(self, monkeypatch):
        from pathlib import Path

        import main

        # Undo the autouse redirection to read the real default.
        monkeypatch.undo()
        path = main.default_help_explainer_marker_path()
        assert path == (
            Path(main.__file__).resolve().parent
            / "data"
            / "help_explainer_notebook_shown.toml"
        )
