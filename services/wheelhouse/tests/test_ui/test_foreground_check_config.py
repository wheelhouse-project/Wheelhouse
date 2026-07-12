"""Tests for the [ui_actions.foreground_check] config wiring (wh-ix1z.22, wh-fc1x.2).

The canonical browser list lives in services/wheelhouse/config.toml under
[ui_actions.foreground_check].same_process_browser_names so users can edit
it without changing code. ui.hwnd_utils.resolve_same_process_browser_names
reads that key, validates entries via coerce_browser_name_list, merges any
backward-compat same_process_browser_names_extend entries, and returns a
lower-cased frozenset.

UIActionHandler.__init__ calls the resolver once and passes the resolved
frozenset to both VerifiedUnicodeStrategy (via same_process_browser_names
kwarg) and ClipboardOperations (which independently calls the resolver
from the same config dict, producing the same set).
"""
from unittest.mock import MagicMock, patch

from ui.hwnd_utils import (
    _FALLBACK_SAME_PROCESS_BROWSER_NAMES,
    coerce_browser_name_list,
    resolve_same_process_browser_names,
)


_MOD = "ui.ui_action_handler"


# --- TestCoerceBrowserNameList -------------------------------------------


class TestCoerceBrowserNameList:
    """Generic list-of-strings validator used by the resolver."""

    def test_none_returns_empty(self):
        assert coerce_browser_name_list(None, key_name="x") == []

    def test_empty_list_returns_empty(self):
        assert coerce_browser_name_list([], key_name="x") == []

    def test_list_of_strings_returns_strings(self):
        assert coerce_browser_name_list(
            ["code.exe", "discord.exe"], key_name="x",
        ) == ["code.exe", "discord.exe"]

    def test_tuple_of_strings_also_accepted(self):
        assert coerce_browser_name_list(
            ("code.exe",), key_name="x",
        ) == ["code.exe"]

    def test_string_value_warns_and_returns_empty(self, caplog):
        with caplog.at_level("WARNING"):
            result = coerce_browser_name_list("code.exe", key_name="my_key")
        assert result == []
        assert any(
            "my_key must be a list of strings" in r.message
            for r in caplog.records
        )

    def test_dict_value_warns_and_returns_empty(self, caplog):
        with caplog.at_level("WARNING"):
            result = coerce_browser_name_list(
                {"name": "code.exe"}, key_name="my_key",
            )
        assert result == []
        assert any(
            "my_key must be a list of strings" in r.message
            for r in caplog.records
        )

    def test_non_string_entries_inside_list_skipped_with_warning(self, caplog):
        with caplog.at_level("WARNING"):
            result = coerce_browser_name_list(
                ["good.exe", 42, None, "another.exe"], key_name="my_key",
            )
        assert result == ["good.exe", "another.exe"]
        non_string_warnings = [
            r for r in caplog.records
            if "non-string entry" in r.message and "my_key" in r.message
        ]
        assert len(non_string_warnings) == 2


# --- TestResolveSameProcessBrowserNames ----------------------------------


class TestResolveSameProcessBrowserNames:
    """Resolver reads canonical list + extend key from config and merges."""

    def test_canonical_list_from_config_replaces_fallback(self):
        config = {
            "ui_actions": {
                "foreground_check": {
                    "same_process_browser_names": ["custom1.exe", "custom2.exe"],
                },
            },
        }
        result = resolve_same_process_browser_names(config)
        assert result == frozenset({"custom1.exe", "custom2.exe"})

    def test_canonical_list_lowercases_entries(self):
        config = {
            "ui_actions": {
                "foreground_check": {
                    "same_process_browser_names": ["Brave.EXE", "CHROME.exe"],
                },
            },
        }
        result = resolve_same_process_browser_names(config)
        assert "brave.exe" in result
        assert "chrome.exe" in result

    def test_missing_canonical_uses_fallback(self):
        config: dict = {"ui_actions": {"foreground_check": {}}}
        result = resolve_same_process_browser_names(config)
        # The hardcoded fallback fires when config is missing the key.
        for entry in _FALLBACK_SAME_PROCESS_BROWSER_NAMES:
            assert entry in result

    def test_missing_section_entirely_uses_fallback(self):
        result = resolve_same_process_browser_names({})
        for entry in _FALLBACK_SAME_PROCESS_BROWSER_NAMES:
            assert entry in result

    def test_extend_adds_to_canonical(self):
        config = {
            "ui_actions": {
                "foreground_check": {
                    "same_process_browser_names": ["brave.exe"],
                    "same_process_browser_names_extend": ["code.exe"],
                },
            },
        }
        result = resolve_same_process_browser_names(config)
        assert result == frozenset({"brave.exe", "code.exe"})

    def test_extend_adds_to_fallback_when_canonical_missing(self):
        # Backward-compat path: older configs that only set the extend
        # key still get their entries merged on top of the fallback.
        config = {
            "ui_actions": {
                "foreground_check": {
                    "same_process_browser_names_extend": ["code.exe"],
                },
            },
        }
        result = resolve_same_process_browser_names(config)
        for entry in _FALLBACK_SAME_PROCESS_BROWSER_NAMES:
            assert entry in result
        assert "code.exe" in result

    def test_malformed_canonical_logs_and_returns_empty_canonical(self, caplog):
        config = {
            "ui_actions": {
                "foreground_check": {
                    "same_process_browser_names": "brave.exe",
                },
            },
        }
        with caplog.at_level("WARNING"):
            result = resolve_same_process_browser_names(config)
        # The canonical key was malformed (string not list), so the
        # validator returns an empty list. No fallback fires because the
        # key is present (just the wrong type). Result is empty.
        assert result == frozenset()
        assert any(
            "same_process_browser_names must be a list of strings"
            in r.message
            for r in caplog.records
        )


# --- TestUIActionHandlerWiresConfig --------------------------------------


def _make_config(*, canonical=None, extend=None):
    fc: dict = {}
    if canonical is not None:
        fc["same_process_browser_names"] = canonical
    if extend is not None:
        fc["same_process_browser_names_extend"] = extend
    return {
        "ui_actions": {
            "timing": {"utterance_clipboard_timeout_seconds": 1.0},
            "foreground_check": fc,
        }
    }


class TestUIActionHandlerWiresConfig:
    """UIActionHandler.__init__ resolves the same-process browser list
    once via the shared resolver and passes the resolved frozenset to
    VerifiedUnicodeStrategy.
    """

    def _build_handler_capturing_strategy_kwargs(self, config):
        with patch(f"{_MOD}.TextPerfector"), \
             patch(f"{_MOD}.ClipboardOperations"), \
             patch(f"{_MOD}.WindowFocusManager"), \
             patch(f"{_MOD}.SelectionTransformer"), \
             patch(f"{_MOD}.UtteranceClipboardManager"), \
             patch(f"{_MOD}.ShadowBufferManager"), \
             patch(f"{_MOD}.TerminalEditorProxy"), \
             patch(f"{_MOD}.InsertionRouter"), \
             patch(f"{_MOD}.VerifiedUnicodeStrategy") as MockVUS:
            from ui.ui_action_handler import UIActionHandler
            UIActionHandler(response_queue=MagicMock(), config=config)
            return MockVUS.call_args

    def test_canonical_from_config_passes_through(self):
        config = _make_config(canonical=["brave.exe", "chrome.exe"])
        call = self._build_handler_capturing_strategy_kwargs(config)
        passed = call.kwargs.get("same_process_browser_names")
        assert passed == frozenset({"brave.exe", "chrome.exe"})

    def test_extend_merges_with_canonical(self):
        config = _make_config(
            canonical=["brave.exe"], extend=["code.exe", "discord.exe"],
        )
        call = self._build_handler_capturing_strategy_kwargs(config)
        passed = call.kwargs.get("same_process_browser_names")
        assert passed == frozenset({"brave.exe", "code.exe", "discord.exe"})

    def test_extend_lowercases_entries(self):
        config = _make_config(canonical=["brave.exe"], extend=["Code.EXE"])
        call = self._build_handler_capturing_strategy_kwargs(config)
        passed = call.kwargs.get("same_process_browser_names")
        assert "code.exe" in passed

    def test_missing_canonical_uses_fallback(self):
        # Older config without the canonical key: resolver falls back
        # to the hardcoded baseline. The strategy still gets a frozenset.
        config = _make_config(extend=[])
        call = self._build_handler_capturing_strategy_kwargs(config)
        passed = call.kwargs.get("same_process_browser_names")
        for entry in _FALLBACK_SAME_PROCESS_BROWSER_NAMES:
            assert entry in passed

    def test_missing_section_uses_fallback(self):
        config = {
            "ui_actions": {
                "timing": {"utterance_clipboard_timeout_seconds": 1.0},
            },
        }
        call = self._build_handler_capturing_strategy_kwargs(config)
        passed = call.kwargs.get("same_process_browser_names")
        for entry in _FALLBACK_SAME_PROCESS_BROWSER_NAMES:
            assert entry in passed

    def test_malformed_extend_logs_and_falls_back(self, caplog):
        # A bare string instead of a list -- the validator yields [] and
        # construction must not raise.
        config = _make_config(extend="code.exe")
        with caplog.at_level("WARNING"):
            call = self._build_handler_capturing_strategy_kwargs(config)
        # No canonical key set, so result is the fallback.
        passed = call.kwargs.get("same_process_browser_names")
        for entry in _FALLBACK_SAME_PROCESS_BROWSER_NAMES:
            assert entry in passed
        assert any(
            "must be a list of strings" in r.message for r in caplog.records
        )
