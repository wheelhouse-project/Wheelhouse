"""Tests for speech/actions.py - ActionFunctions and words_to_int.

Covers:
- words_to_int: digit, word, None, invalid conversions
- hotkey: repeat count extraction, None filtering, clamping
- press: repeat count with clamping
- press_keys: multi-word combos, two-word aliases, modifier sorting, unrecognized
- Payload builders: handle_literal, type_text, insert_text, text, wrap_or_insert,
  transform_selection, number_point, insert_newlines, activate_window, skip_clipboard_restore
- Local actions: format_date, async_sleep, run_program, GSearch, capture_clipboard
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import pytest
from unittest.mock import Mock, MagicMock, AsyncMock, patch
from datetime import datetime

from speech.actions import words_to_int, ActionFunctions, _normalize_key, SPOKEN_KEY_MAP


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def action_funcs():
    """ActionFunctions with a mock speech_handler."""
    handler = MagicMock()
    handler.app = MagicMock()
    return ActionFunctions(handler)


# ============================================================================
# words_to_int
# ============================================================================

class TestWordsToInt:
    def test_digit_string(self):
        assert words_to_int("5") == 5

    def test_word_number(self):
        assert words_to_int("three") == 3

    def test_zero(self):
        assert words_to_int("zero") == 0

    def test_ten(self):
        assert words_to_int("ten") == 10

    def test_none_returns_default_one(self):
        assert words_to_int(None) == 1

    def test_invalid_returns_none(self):
        assert words_to_int("banana") is None

    def test_homophones(self):
        assert words_to_int("to") == 2
        assert words_to_int("too") == 2
        assert words_to_int("for") == 4

    def test_case_insensitive(self):
        assert words_to_int("Three") == 3
        assert words_to_int("FIVE") == 5

    def test_whitespace_stripped(self):
        assert words_to_int(" three ") == 3

    def test_multi_digit(self):
        assert words_to_int("42") == 42


# ============================================================================
# _normalize_key
# ============================================================================

class TestNormalizeKey:
    def test_spoken_alias(self):
        assert _normalize_key("control") == "ctrl"
        assert _normalize_key("escape") == "esc"
        assert _normalize_key("return") == "enter"

    def test_vk_code_map_key(self):
        assert _normalize_key("enter") == "enter"
        assert _normalize_key("tab") == "tab"
        assert _normalize_key("space") == "space"

    def test_unrecognized_returns_none(self):
        assert _normalize_key("xyzzy") is None

    def test_case_insensitive(self):
        assert _normalize_key("CTRL") is not None
        assert _normalize_key("Enter") is not None

    def test_tuple_alias(self):
        result = _normalize_key("left parenthesis")
        assert isinstance(result, tuple)
        assert result == ("shift", "9")

    def test_punctuation_alias(self):
        assert _normalize_key("slash") == "/"
        assert _normalize_key("semicolon") == ";"


# ============================================================================
# hotkey
# ============================================================================

class TestHotkey:
    def test_basic_hotkey(self, action_funcs):
        result = action_funcs.hotkey("ctrl", "c")
        assert result["action"] == "hotkey_action"
        assert result["params"]["keys"] == ["ctrl", "c"]
        assert result["params"]["repeat"] == 1

    def test_with_repeat_count(self, action_funcs):
        result = action_funcs.hotkey("ctrl", "z", "3")
        assert result["params"]["keys"] == ["ctrl", "z"]
        assert result["params"]["repeat"] == 3

    def test_with_word_repeat(self, action_funcs):
        result = action_funcs.hotkey("ctrl", "z", "three")
        assert result["params"]["keys"] == ["ctrl", "z"]
        assert result["params"]["repeat"] == 3

    def test_none_filtered(self, action_funcs):
        result = action_funcs.hotkey("ctrl", "z", None)
        assert result["params"]["keys"] == ["ctrl", "z"]
        assert result["params"]["repeat"] == 1

    def test_string_none_filtered(self, action_funcs):
        result = action_funcs.hotkey("ctrl", "z", "None")
        assert result["params"]["keys"] == ["ctrl", "z"]
        assert result["params"]["repeat"] == 1

    def test_repeat_capped_at_50(self, action_funcs):
        result = action_funcs.hotkey("ctrl", "z", "100")
        assert result["params"]["repeat"] == 50

    def test_invalid_last_arg_kept_as_key(self, action_funcs):
        result = action_funcs.hotkey("ctrl", "z", "nonsense")
        assert result["params"]["keys"] == ["ctrl", "z", "nonsense"]
        assert result["params"]["repeat"] == 1

    def test_empty_keys(self, action_funcs):
        result = action_funcs.hotkey()
        assert result["params"]["keys"] == []
        assert result["params"]["repeat"] == 1

    def test_single_key(self, action_funcs):
        result = action_funcs.hotkey("f5")
        # "f5" is not a number, so it's treated as a key with repeat 1
        assert result["params"]["keys"] == ["f5"]
        assert result["params"]["repeat"] == 1


# ============================================================================
# press
# ============================================================================

class TestPress:
    def test_basic_press(self, action_funcs):
        result = action_funcs.press("backspace")
        assert result["action"] == "press_key_action"
        assert result["params"]["key"] == "backspace"
        assert result["params"]["repeat"] == 1

    def test_with_repeat(self, action_funcs):
        result = action_funcs.press("delete", "3")
        assert result["params"]["repeat"] == 3

    def test_with_word_repeat(self, action_funcs):
        result = action_funcs.press("delete", "five")
        assert result["params"]["repeat"] == 5

    def test_none_repeat_defaults_to_one(self, action_funcs):
        result = action_funcs.press("enter", None)
        assert result["params"]["repeat"] == 1

    def test_invalid_repeat_defaults_to_one(self, action_funcs):
        result = action_funcs.press("enter", "banana")
        assert result["params"]["repeat"] == 1

    def test_repeat_capped_at_50(self, action_funcs):
        result = action_funcs.press("space", "100")
        assert result["params"]["repeat"] == 50

    def test_zero_repeat_defaults_to_one(self, action_funcs):
        result = action_funcs.press("space", "0")
        assert result["params"]["repeat"] == 1


# ============================================================================
# press_keys
# ============================================================================

class TestPressKeys:
    def test_single_key(self, action_funcs):
        result = action_funcs.press_keys("enter")
        assert result is not None
        assert "enter" in result["params"]["keys"]

    def test_multi_key_combination(self, action_funcs):
        result = action_funcs.press_keys("control alt delete")
        assert result is not None
        keys = result["params"]["keys"]
        assert "ctrl" in keys
        assert "alt" in keys
        assert "delete" in keys

    def test_two_word_alias(self, action_funcs):
        result = action_funcs.press_keys("page up")
        assert result is not None
        assert "pageup" in result["params"]["keys"]

    def test_two_word_alias_left_brace(self, action_funcs):
        result = action_funcs.press_keys("left brace")
        assert result is not None
        assert "{" in result["params"]["keys"]

    def test_two_word_tuple_alias(self, action_funcs):
        # "left parenthesis" -> ("shift", "9") -> hotkey("shift", "9")
        # hotkey treats "9" as repeat count (valid digit), so keys=["shift"], repeat=9
        result = action_funcs.press_keys("left parenthesis")
        assert result is not None
        assert "shift" in result["params"]["keys"]
        assert result["params"]["repeat"] == 9

    def test_modifier_sorting(self, action_funcs):
        result = action_funcs.press_keys("delete ctrl alt")
        keys = result["params"]["keys"]
        # Modifiers should come before non-modifiers
        ctrl_idx = keys.index("ctrl")
        alt_idx = keys.index("alt")
        delete_idx = keys.index("delete")
        assert ctrl_idx < delete_idx
        assert alt_idx < delete_idx

    def test_unrecognized_key_returns_none(self, action_funcs):
        result = action_funcs.press_keys("xyzzy zorp")
        assert result is None

    def test_empty_string_returns_none(self, action_funcs):
        result = action_funcs.press_keys("")
        assert result is None

    def test_spoken_punctuation(self, action_funcs):
        result = action_funcs.press_keys("slash")
        assert result is not None
        assert "/" in result["params"]["keys"]

    # --- Hyphenated key combos (Whisper transcription normalization) ---

    def test_hyphenated_function_key(self, action_funcs):
        """Whisper transcribes 'f 11' as 'f-11' -- should resolve to f11."""
        result = action_funcs.press_keys("f-11")
        assert result is not None
        assert "f11" in result["params"]["keys"]

    def test_hyphenated_modifier_combo(self, action_funcs):
        """Whisper transcribes 'control alt' as 'control-alt' -- should split."""
        result = action_funcs.press_keys("control-alt delete")
        assert result is not None
        keys = result["params"]["keys"]
        assert "ctrl" in keys
        assert "alt" in keys
        assert "delete" in keys

    def test_hyphenated_modifier_with_function_key(self, action_funcs):
        """'control-f1' should resolve to ctrl + f1."""
        result = action_funcs.press_keys("control-f1")
        assert result is not None
        keys = result["params"]["keys"]
        assert "ctrl" in keys
        assert "f1" in keys

    def test_non_hyphenated_still_works(self, action_funcs):
        """Non-hyphenated input should continue to work unchanged."""
        result = action_funcs.press_keys("control alt delete")
        assert result is not None
        keys = result["params"]["keys"]
        assert "ctrl" in keys
        assert "alt" in keys
        assert "delete" in keys


# ============================================================================
# PAYLOAD BUILDERS
# ============================================================================

class TestPayloadBuilders:
    def test_handle_literal(self, action_funcs):
        result = action_funcs.handle_literal("hello world")
        assert result == {"action": "type_text", "params": {"text": "hello world"}}

    def test_type_text(self, action_funcs):
        result = action_funcs.type_text("hello")
        assert result == {"action": "type_text", "params": {"text": "hello"}}

    def test_insert_text(self, action_funcs):
        result = action_funcs.insert_text("hello")
        assert result == {"action": "intelligent_insert_text", "params": {"insertion_string": "hello"}}

    def test_text_delegates_to_insert_text(self, action_funcs):
        result = action_funcs.text("hello")
        assert result == action_funcs.insert_text("hello")

    def test_text_empty_string_returns_none(self, action_funcs):
        """Empty replacement text should return None to suppress IPC insertion.

        Patterns like non-speech sounds (*cough*) replace with "" to silently
        consume the text. Sending an empty string to the input process causes
        side effects (e.g., terminal editor opening).
        """
        result = action_funcs.text("")
        assert result is None

    def test_transform_selection(self, action_funcs):
        result = action_funcs.transform_selection("snake_case")
        assert result == {"action": "transform_selection", "params": {"transformation_type": "snake_case"}}

    def test_activate_window(self, action_funcs):
        result = action_funcs.activate_window("brave.exe")
        assert result == {"action": "activate_window", "params": {"target": "brave.exe"}}

    def test_skip_clipboard_restore(self, action_funcs):
        result = action_funcs.skip_clipboard_restore()
        assert result == {"action": "skip_clipboard_restore", "params": {"enable": True}}

    def test_skip_clipboard_restore_false(self, action_funcs):
        result = action_funcs.skip_clipboard_restore(enable=False)
        assert result["params"]["enable"] is False


# ============================================================================
# default_browser target resolution
# ============================================================================

class TestExeNameFromCommand:
    """_exe_name_from_command parses a registry shell-open command string."""

    def test_quoted_path(self):
        from speech.actions import _exe_name_from_command
        cmd = '"C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe" -- "%1"'
        assert _exe_name_from_command(cmd) == "brave.exe"

    def test_unquoted_path(self):
        from speech.actions import _exe_name_from_command
        cmd = 'C:\\PROGRA~1\\MOZILL~1\\firefox.exe -osint -url "%1"'
        assert _exe_name_from_command(cmd) == "firefox.exe"

    def test_quoted_path_no_arguments(self):
        from speech.actions import _exe_name_from_command
        cmd = '"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"'
        assert _exe_name_from_command(cmd) == "msedge.exe"

    def test_non_exe_command_returns_none(self):
        from speech.actions import _exe_name_from_command
        assert _exe_name_from_command('rundll32.exe.dll,OpenURL "%1"') is None

    def test_rundll32_host_command_returns_none(self):
        """rundll32 is a shell host, not the browser itself; returning it
        would make 'browser' silently activate nothing instead of the Edge
        fallback (reviewer_0 finding wh-user-patterns-split.12.2)."""
        from speech.actions import _exe_name_from_command
        assert _exe_name_from_command(
            'rundll32.exe url.dll,FileProtocolHandler %1'
        ) is None

    def test_launchwinapp_host_command_returns_none(self):
        from speech.actions import _exe_name_from_command
        assert _exe_name_from_command(
            '"C:\\Windows\\System32\\LaunchWinApp.exe" "%1"'
        ) is None

    def test_empty_returns_none(self):
        from speech.actions import _exe_name_from_command
        assert _exe_name_from_command("") is None

    def test_unterminated_quote_returns_none(self):
        from speech.actions import _exe_name_from_command
        assert _exe_name_from_command('"C:\\broken\\path.exe') is None

    def test_non_string_returns_none(self):
        from speech.actions import _exe_name_from_command
        assert _exe_name_from_command(None) is None


class TestDefaultBrowserExe:
    """_default_browser_exe reads the registry; msedge.exe on any failure."""

    def test_resolves_from_registry(self):
        import speech.actions as actions_mod
        import winreg

        def fake_query(key, name=None):
            if name == "ProgId":
                return ("BraveHTML", winreg.REG_SZ)
            return ('"C:\\Apps\\Brave\\brave.exe" -- "%1"', winreg.REG_SZ)

        with patch.object(winreg, "OpenKey", MagicMock()), \
             patch.object(winreg, "QueryValueEx", side_effect=fake_query):
            assert actions_mod._default_browser_exe() == "brave.exe"

    def test_registry_error_falls_back_to_edge(self):
        import speech.actions as actions_mod
        import winreg

        with patch.object(winreg, "OpenKey", side_effect=OSError("no key")):
            assert actions_mod._default_browser_exe() == "msedge.exe"

    def test_unparseable_command_falls_back_to_edge(self):
        import speech.actions as actions_mod
        import winreg

        def fake_query(key, name=None):
            if name == "ProgId":
                return ("WeirdHTML", winreg.REG_SZ)
            return ("not-an-executable-command", winreg.REG_SZ)

        with patch.object(winreg, "OpenKey", MagicMock()), \
             patch.object(winreg, "QueryValueEx", side_effect=fake_query):
            assert actions_mod._default_browser_exe() == "msedge.exe"


class TestActivateWindowDefaultBrowser:
    """activate_window('default_browser') resolves to the real browser exe."""

    def test_default_browser_target_is_resolved(self, action_funcs):
        with patch("speech.actions._default_browser_exe", return_value="brave.exe"):
            result = action_funcs.activate_window("default_browser")
        assert result == {"action": "activate_window", "params": {"target": "brave.exe"}}

    def test_ordinary_target_is_not_resolved(self, action_funcs):
        with patch("speech.actions._default_browser_exe", return_value="brave.exe"):
            result = action_funcs.activate_window("notepad.exe")
        assert result == {"action": "activate_window", "params": {"target": "notepad.exe"}}


class TestWrapOrInsert:
    def test_with_text(self, action_funcs):
        result = action_funcs.wrap_or_insert("(", ")", " hello")
        assert result["action"] == "wrap_or_insert"
        assert result["params"]["left_fence"] == "("
        assert result["params"]["right_fence"] == ")"
        assert result["params"]["text"] == " hello"

    def test_without_text(self, action_funcs):
        result = action_funcs.wrap_or_insert('"', '"')
        assert result["params"]["text"] == ""

    def test_empty_string_text(self, action_funcs):
        result = action_funcs.wrap_or_insert("[", "]", "")
        assert result["params"]["text"] == ""


class TestNumberPoint:
    def test_word_number(self, action_funcs):
        result = action_funcs.number_point("one")
        assert result == {"action": "intelligent_insert_text", "params": {"insertion_string": "1."}}

    def test_digit_string(self, action_funcs):
        result = action_funcs.number_point("5")
        assert result == {"action": "intelligent_insert_text", "params": {"insertion_string": "5."}}

    def test_invalid_word_uses_raw(self, action_funcs):
        result = action_funcs.number_point("banana")
        assert result == {"action": "intelligent_insert_text", "params": {"insertion_string": "banana."}}


class TestInsertNewlines:
    def test_single_newline(self, action_funcs):
        result = action_funcs.insert_newlines("1")
        assert result["params"]["insertion_string"] == "\n"

    def test_multiple_newlines(self, action_funcs):
        result = action_funcs.insert_newlines("3")
        assert result["params"]["insertion_string"] == "\n\n\n"

    def test_word_count(self, action_funcs):
        result = action_funcs.insert_newlines("three")
        assert result["params"]["insertion_string"] == "\n\n\n"

    def test_capped_at_50(self, action_funcs):
        result = action_funcs.insert_newlines("100")
        assert len(result["params"]["insertion_string"]) == 50

    def test_invalid_defaults_to_one(self, action_funcs):
        result = action_funcs.insert_newlines("banana")
        assert result["params"]["insertion_string"] == "\n"

    def test_zero_defaults_to_one(self, action_funcs):
        result = action_funcs.insert_newlines("zero")
        assert result["params"]["insertion_string"] == "\n"


# ============================================================================
# LOCAL ACTIONS
# ============================================================================

class TestFormatDate:
    def test_default_format(self, action_funcs):
        result = action_funcs.format_date()
        # Should be YYYY-MM-DD format
        assert len(result) == 10
        assert result[4] == "-"
        assert result[7] == "-"

    def test_custom_format(self, action_funcs):
        result = action_funcs.format_date("%Y")
        assert result == str(datetime.now().year)


class TestAsyncSleep:
    @pytest.mark.asyncio
    async def test_valid_duration(self, action_funcs):
        result = await action_funcs.async_sleep("0.01")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_duration(self, action_funcs):
        # Should log warning but not raise
        result = await action_funcs.async_sleep("not_a_number")
        assert result is None


class TestRunProgram:
    @pytest.mark.asyncio
    async def test_run_program_calls_popen(self, action_funcs):
        with patch("speech.actions.subprocess.Popen") as mock_popen:
            result = await action_funcs.run_program("notepad.exe")
            assert result is None
            mock_popen.assert_called_once_with("notepad.exe", shell=True)

    @pytest.mark.asyncio
    async def test_run_program_handles_error(self, action_funcs):
        with patch("speech.actions.subprocess.Popen", side_effect=FileNotFoundError("not found")):
            result = await action_funcs.run_program("nonexistent.exe")
            assert result is None  # Should not raise


class TestGSearch:
    @pytest.mark.asyncio
    async def test_gsearch_opens_browser(self, action_funcs):
        with patch("speech.actions.webbrowser.open") as mock_open:
            await action_funcs.GSearch("test query")
            mock_open.assert_called_once()
            url = mock_open.call_args[0][0]
            assert "google.com/search" in url
            assert "test+query" in url

    @pytest.mark.asyncio
    async def test_gsearch_none_query(self, action_funcs):
        with patch("speech.actions.webbrowser.open") as mock_open:
            await action_funcs.GSearch(None)
            mock_open.assert_called_once()

    @pytest.mark.asyncio
    async def test_gsearch_handles_error(self, action_funcs):
        with patch("speech.actions.webbrowser.open", side_effect=Exception("browser error")):
            result = await action_funcs.GSearch("test")
            assert result is None


class TestCaptureClipboard:
    def test_capture_clipboard_returns_content(self, action_funcs):
        mock_pyperclip = MagicMock()
        mock_pyperclip.paste.return_value = "clipboard content"
        with patch.dict("sys.modules", {"pyperclip": mock_pyperclip}):
            result = action_funcs.capture_clipboard()
            assert result == "clipboard content"

    def test_capture_clipboard_import_error(self, action_funcs):
        # Simulate pyperclip not being installed
        with patch("builtins.__import__", side_effect=ImportError("no pyperclip")):
            result = action_funcs.capture_clipboard()
            assert result == ""


# ============================================================================
# FUNCTION REGISTRY
# ============================================================================

class TestFunctionRegistry:
    def test_get_functions_returns_dict(self, action_funcs):
        funcs = action_funcs.get_functions()
        assert isinstance(funcs, dict)

    def test_all_expected_functions_registered(self, action_funcs):
        funcs = action_funcs.get_functions()
        expected = [
            "hk", "press", "press_keys", "activate", "literal",
            "type_text", "insert_text", "insert_newlines",
            "transform_selection", "text", "number_point",
            "wrap_or_insert", "skip_clipboard_restore",
            "run", "sleep", "date", "gs",
            "capture_clipboard", "add_hint_to_stt",
        ]
        for name in expected:
            assert name in funcs, f"Missing function: {name}"


# ============================================================================
# SET SPEECH INTERACTION MODE
# ============================================================================

class TestSetSpeechInteractionMode:
    """Test set_speech_interaction_mode action function."""

    @pytest.fixture
    def action_funcs_with_sm(self):
        """ActionFunctions with mocked speech_handler chain including state_manager."""
        from speech.actions import ActionFunctions
        handler = MagicMock()
        handler.logic_controller = MagicMock()
        handler.logic_controller.state_manager = MagicMock()
        handler.logic_controller.state_manager.state_to_gui_queue = MagicMock()
        return ActionFunctions(handler)

    def test_function_registered(self, action_funcs_with_sm):
        assert "set_speech_interaction_mode" in action_funcs_with_sm.get_functions()

    def test_sends_mode_to_state_manager(self, action_funcs_with_sm):
        action_funcs_with_sm.set_speech_interaction_mode("push_to_talk")
        sm = action_funcs_with_sm.speech_handler.logic_controller.state_manager
        sm.set_speech_interaction_mode.assert_called_once_with("push_to_talk")

    def test_sends_toggle_mode_to_state_manager(self, action_funcs_with_sm):
        action_funcs_with_sm.set_speech_interaction_mode("toggle")
        sm = action_funcs_with_sm.speech_handler.logic_controller.state_manager
        sm.set_speech_interaction_mode.assert_called_once_with("toggle")

    def test_returns_none(self, action_funcs_with_sm):
        result = action_funcs_with_sm.set_speech_interaction_mode("push_to_talk")
        assert result is None

    def test_no_logic_controller_logs_warning(self, action_funcs_with_sm, caplog):
        action_funcs_with_sm.speech_handler.logic_controller = None
        import logging
        with caplog.at_level(logging.WARNING, logger="speech.actions"):
            action_funcs_with_sm.set_speech_interaction_mode("push_to_talk")
        assert "logic_controller not available" in caplog.text

    def test_no_state_manager_logs_warning(self, action_funcs_with_sm, caplog):
        action_funcs_with_sm.speech_handler.logic_controller.state_manager = None
        import logging
        with caplog.at_level(logging.WARNING, logger="speech.actions"):
            action_funcs_with_sm.set_speech_interaction_mode("push_to_talk")
        assert "state_manager not available" in caplog.text


# ============================================================================
# click_element (wh-tab7j) -- parse g1, generate trace_id, delegate to the
# Logic-side awaiter LogicController.forward_click_element
# ============================================================================

class TestClickElementAction:
    """The click_element action parses g1 into an ElementQuery, generates a
    trace_id, and delegates to ``logic_controller.forward_click_element``.

    wh-tab7j replaced the wh-vjwdl parse-only stub with the real delegation.
    The action is async and returns None on every path: unparseable input
    falls through to dictation (no delegation); a parseable target delegates
    to the awaiter (which owns the IPC round trip + degrade paths). The action
    itself never calls ``app`` directly -- the send_request lives inside the
    awaiter so it uses the [click].response_timeout_ms timeout.
    """

    @pytest.fixture
    def click_funcs(self):
        """ActionFunctions whose logic_controller.forward_click_element is an
        observable async stub."""
        from speech.actions import ActionFunctions

        handler = MagicMock()
        handler.app = MagicMock()
        lc = MagicMock()
        captured = {}

        async def _fwd(query, trace_id):
            captured["query"] = query
            captured["trace_id"] = trace_id

        lc.forward_click_element = _fwd
        handler.logic_controller = lc
        funcs = ActionFunctions(handler)
        funcs._captured = captured  # type: ignore[attr-defined]
        return funcs

    def test_registered(self, action_funcs):
        assert "click_element" in action_funcs.get_functions()

    def test_parseable_delegates_to_awaiter(self, click_funcs):
        result = asyncio.run(click_funcs.click_element("the cancel button"))
        assert result is None
        captured = click_funcs._captured
        assert captured["query"].name == "cancel"
        assert captured["query"].role == "Button"
        assert captured["trace_id"]  # a trace_id was generated/propagated

    def test_unparseable_returns_none_and_does_not_delegate(self, click_funcs):
        # Whitespace-only collapses to no name -> benign None, no delegation.
        result = asyncio.run(click_funcs.click_element("   "))
        assert result is None
        assert "query" not in click_funcs._captured

    def test_action_does_not_call_app_directly(self, click_funcs):
        # The send_request lives inside forward_click_element (so it uses the
        # [click] timeout); the action must not call app.* itself.
        asyncio.run(click_funcs.click_element("the cancel button"))
        click_funcs.speech_handler.app.send_request.assert_not_called()
        click_funcs.speech_handler.app.send_command.assert_not_called()

    def test_logs_parsed_query(self, click_funcs, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="speech.actions"):
            asyncio.run(click_funcs.click_element("the cancel button"))
        assert "name='cancel'" in caplog.text
        assert "Button" in caplog.text


# ============================================================================
# click grammar routing (wh-vjwdl) -- routing against the real patterns
# ============================================================================

class TestClickPatternHotwordGating:
    """Click grammar routing against the real patterns.toml.

    History: wh-vjwdl originally made the click pattern hotword-required.
    Commit bc91e701 (2026-07-05) reversed that by user decision: click,
    apply numbers, and dismiss numbers fire WITHOUT the hotword now. The
    accepted trade-off is that command-mode text beginning with 'click'
    routes to a UI click attempt. These tests lock the current contract;
    click-to-talk must still win over the greedy click pattern.
    """

    @pytest.fixture
    def matcher(self):
        from speech.pattern_catalog import PatternCatalog
        from speech.pattern_matcher import PatternMatcher

        patterns_path = Path(__file__).parent.parent / "speech" / "config" / "patterns.toml"
        catalog = PatternCatalog(str(patterns_path))
        assert catalog.pattern_count > 0  # sanity: real file loaded
        return PatternMatcher(catalog)

    def test_click_without_hotword_matches_click_element(self, matcher):
        # bc91e701: the click pattern is no longer hotword-gated, so a
        # command-mode buffer starting with 'click' matches click_element
        # even when the hotword is inactive.
        result = matcher.match_complete(
            "click here to continue",
            pattern_type="command",
            hotword_active=False,
            first_word="click",
        )
        assert result is not None and result.matched
        funcs = [a.get("function") for a in (result.actions or [])]
        assert "click_element" in funcs

    def test_click_with_hotword_matches_click_element(self, matcher):
        # Hotword active: the click pattern matches and routes to click_element.
        result = matcher.match_complete(
            "click cancel",
            pattern_type="command",
            hotword_active=True,
            first_word="click",
        )
        assert result is not None and result.matched
        funcs = [a.get("function") for a in (result.actions or [])]
        assert "click_element" in funcs

    def test_click_to_talk_mode_still_matches_without_hotword(self, matcher):
        # The existing non-hotword exact command must keep working and win
        # over the greedy click pattern (first-match-wins file ordering).
        result = matcher.match_complete(
            "click to talk mode",
            pattern_type="command",
            hotword_active=False,
            first_word="click",
        )
        assert result is not None and result.matched
        funcs = [a.get("function") for a in (result.actions or [])]
        assert "set_speech_interaction_mode" in funcs
        assert "click_element" not in funcs
