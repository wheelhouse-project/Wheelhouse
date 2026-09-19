"""Tests for the open_url action (wh-open-url-action).

Spec: docs/superpowers/specs/2026-08-09-pattern-actions-expansion-design.md
section 5. One parameter: a URL template. The command engine URL-encodes
every value it substitutes into that template (the same quote_plus encoding
``gs`` uses for queries), so spoken text arrives as data, never as URL
structure -- the scheme and host must be written in the template. After
substitution the URL must start with http:// or https:// (case-insensitive);
anything else raises, which stops the pattern's remaining steps (spec rule
2.2). The browser opens via the same non-blocking call as ``gs``, and a
browser-launch failure only logs -- the one deliberate exception to the
stop rule (spec 5.3).

Engine-level tests drive ``TextParser._execute_rule`` with the real pattern
catalog, mirroring tests/test_command_engine_gaps.py; unit tests drive
``ActionFunctions.open_url`` directly, mirroring tests/test_actions.py.
"""
import re
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from speech.actions import ActionFunctions
from speech.command_engine import TextParser
from speech.pattern_catalog import PatternCatalog


@pytest.fixture
def action_funcs():
    """ActionFunctions with a mock speech_handler."""
    handler = MagicMock()
    handler.app = MagicMock()
    return ActionFunctions(handler)


@pytest.fixture
def catalog():
    return PatternCatalog("speech/config/patterns.toml")


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.send_command = AsyncMock()
    app.send_request = AsyncMock(return_value={"status": "success"})
    return app


@pytest.fixture
def parser(catalog, mock_app):
    handler = MagicMock()
    handler.app = mock_app
    return TextParser(handler, catalog)


# ============================================================================
# UNIT: ActionFunctions.open_url
# ============================================================================


class TestOpenUrlUnit:
    @pytest.mark.asyncio
    async def test_opens_browser_with_https_url(self, action_funcs):
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await action_funcs.open_url("https://example.com/page")
            mock_open.assert_called_once_with("https://example.com/page")
            assert result is None

    @pytest.mark.asyncio
    async def test_http_scheme_allowed(self, action_funcs):
        with patch("speech.actions.webbrowser.open") as mock_open:
            await action_funcs.open_url("http://example.com/")
            mock_open.assert_called_once()

    @pytest.mark.asyncio
    async def test_scheme_check_is_case_insensitive(self, action_funcs):
        # URL schemes are case-insensitive; an uppercase template must not
        # fail the step.
        with patch("speech.actions.webbrowser.open") as mock_open:
            await action_funcs.open_url("HTTPS://example.com/")
            mock_open.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_url",
        [
            "file:///C:/Windows/system.ini",
            "ftp://example.com/x",
            "javascript:alert(1)",
            "example.com/no-scheme",
            "httpss://example.com",
            "",
            None,
        ],
    )
    async def test_non_http_scheme_raises_without_opening(
        self, action_funcs, bad_url
    ):
        with patch("speech.actions.webbrowser.open") as mock_open:
            with pytest.raises(ValueError):
                await action_funcs.open_url(bad_url)
            mock_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_launch_failure_logs_and_does_not_raise(
        self, action_funcs, caplog
    ):
        # Spec 5.3: browser-launch failure is log-only, the one deliberate
        # exception to the stop-remaining-steps rule.
        import logging

        with patch(
            "speech.actions.webbrowser.open",
            side_effect=Exception("browser exploded"),
        ):
            with caplog.at_level(logging.ERROR, logger="speech.actions"):
                result = await action_funcs.open_url("https://example.com/")
        assert result is None
        assert any("open_url" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_false_return_from_browser_logs_error(
        self, action_funcs, caplog
    ):
        # webbrowser.open reports the common failure shape by RETURNING
        # False, not by raising (on Windows, WindowsDefault.open catches
        # the OSError from os.startfile and returns False). Spec 5.3 makes
        # launch failure log-only, not silent -- the log line must still
        # happen (wh-open-url-action.1.2).
        import logging

        with patch("speech.actions.webbrowser.open", return_value=False):
            with caplog.at_level(logging.ERROR, logger="speech.actions"):
                result = await action_funcs.open_url("https://example.com/")
        assert result is None
        assert any("open_url" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_gsearch_false_return_from_browser_logs_error(
        self, action_funcs, caplog
    ):
        # GSearch has the same false-return blind spot open_url was copied
        # from; both actions report the failure (wh-open-url-action.1.2).
        import logging

        with patch("speech.actions.webbrowser.open", return_value=False):
            with caplog.at_level(logging.ERROR, logger="speech.actions"):
                result = await action_funcs.GSearch("kittens")
        assert result is None
        assert any("GSearch" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_false_return_log_redacts_spoken_content(
        self, action_funcs, caplog, monkeypatch
    ):
        # wh-open-url-action.1.5: the URL after substitution carries spoken
        # text; the failure log must go through transcript redaction, which
        # is length-only unless the launcher enabled transcript logging.
        import logging

        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        with patch("speech.actions.webbrowser.open", return_value=False):
            with caplog.at_level(logging.ERROR, logger="speech.actions"):
                await action_funcs.open_url(
                    "https://example.com/?q=sensitive-voice-token"
                )
        assert any("open_url" in r.getMessage() for r in caplog.records)
        assert not any(
            "sensitive-voice-token" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_gsearch_false_return_log_redacts_spoken_content(
        self, action_funcs, caplog, monkeypatch
    ):
        # Same redaction rule for the GSearch sibling
        # (wh-open-url-action.1.5).
        import logging

        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        with patch("speech.actions.webbrowser.open", return_value=False):
            with caplog.at_level(logging.ERROR, logger="speech.actions"):
                await action_funcs.GSearch("sensitive-voice-token")
        assert any("GSearch" in r.getMessage() for r in caplog.records)
        assert not any(
            "sensitive-voice-token" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_bad_scheme_error_message_redacts_url(
        self, action_funcs, monkeypatch
    ):
        # wh-open-url-action.1.5: the bad-scheme ValueError message is
        # logged by the engine's rule-failure handler, so the URL content
        # (spoken text included) must be redacted inside the message.
        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        with patch("speech.actions.webbrowser.open"):
            with pytest.raises(ValueError) as excinfo:
                await action_funcs.open_url(
                    "file:///C:/sensitive-voice-token"
                )
        assert "sensitive-voice-token" not in str(excinfo.value)


# ============================================================================
# ENGINE: URL-encoded substitution, scoped to open_url only
# ============================================================================


class TestEngineSubstitution:
    @pytest.mark.asyncio
    async def test_substituted_group_is_url_encoded(self, parser):
        match = re.fullmatch(r"jira (.+)", "jira hello world & stuff?x")
        steps = [
            {
                "function": "open_url",
                "params": ["https://example.com/search?q=g1"],
            }
        ]
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(
                match, steps, validation_group=None
            )
        assert result is True
        mock_open.assert_called_once_with(
            "https://example.com/search?q=hello+world+%26+stuff%3Fx"
        )

    @pytest.mark.asyncio
    async def test_earlier_step_result_is_url_encoded(self, parser):
        # A stored result substitutes into the template URL-encoded, same
        # as a capture group (spec 5.1).
        mock_pyperclip = MagicMock()
        mock_pyperclip.paste.return_value = "a&b c"
        match = re.fullmatch(r"lookup", "lookup")
        steps = [
            {
                "function": "capture_clipboard",
                "params": [],
                "result": "grabbed",
            },
            {
                "function": "open_url",
                "params": ["https://example.com/?q=grabbed"],
            },
        ]
        with patch.dict("sys.modules", {"pyperclip": mock_pyperclip}):
            with patch("speech.actions.webbrowser.open") as mock_open:
                result = await parser._execute_rule(
                    match, steps, validation_group=None
                )
        assert result is True
        mock_open.assert_called_once_with("https://example.com/?q=a%26b+c")

    @pytest.mark.asyncio
    async def test_literal_group_text_in_path_with_no_capture_opens(
        self, parser
    ):
        # wh-open-url-action.1.4: g1..g9 keys exist in the context for
        # every pattern, value None, even when the pattern defines no
        # capture groups. Literal URL text that merely looks like a marker
        # ("/g1/" as a fixed path segment) must not be treated as an
        # unresolved reference when the pattern has no such group.
        match = re.fullmatch(r"open docs", "open docs")
        steps = [
            {
                "function": "open_url",
                "params": ["https://example.test/g1/reference"],
            }
        ]
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(
                match, steps, validation_group=None
            )
        assert result is True
        mock_open.assert_called_once_with("https://example.test/g1/reference")

    @pytest.mark.asyncio
    async def test_literal_group_text_in_host_with_no_capture_opens(
        self, parser
    ):
        # Same class (wh-open-url-action.1.4), host position: with no
        # capture groups defined, nothing can substitute, so a fixed host
        # containing marker-like text is safe and must open.
        match = re.fullmatch(r"open docs", "open docs")
        steps = [
            {"function": "open_url", "params": ["https://g1.example.test/"]}
        ]
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(
                match, steps, validation_group=None
            )
        assert result is True
        mock_open.assert_called_once_with("https://g1.example.test/")

    @pytest.mark.asyncio
    async def test_marker_in_no_path_query_substitutes(self, parser):
        # wh-open-url-action.1.7: the protected region ends at the first
        # /, ?, or # after the scheme -- a query that follows the host
        # directly (no path slash) is a data position, and a marker there
        # substitutes URL-encoded. Pins the boundary the catalog text
        # describes.
        match = re.fullmatch(r"open (.+)", "open hello world")
        steps = [
            {
                "function": "open_url",
                "params": ["https://example.test?query=g1"],
            }
        ]
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(
                match, steps, validation_group=None
            )
        assert result is True
        mock_open.assert_called_once_with(
            "https://example.test?query=hello+world"
        )

    @pytest.mark.asyncio
    async def test_marker_in_no_path_fragment_substitutes(self, parser):
        # Same boundary (wh-open-url-action.1.7), fragment variant: a #
        # directly after the host also ends the protected region.
        match = re.fullmatch(r"open (.+)", "open section two")
        steps = [
            {"function": "open_url", "params": ["https://example.test#g1"]}
        ]
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(
                match, steps, validation_group=None
            )
        assert result is True
        mock_open.assert_called_once_with("https://example.test#section+two")

    @pytest.mark.asyncio
    async def test_other_actions_substitution_stays_raw(self, parser):
        # The encoding is scoped to open_url; every other action keeps the
        # engine's plain substitution.
        match = re.fullmatch(r"wrap (.+)", "wrap hello world & more")
        steps = [{"function": "type_text", "params": ["<<g1>>"]}]
        result = await parser._execute_rule(match, steps, validation_group=None)
        assert result is True
        payload = parser.speech_handler.app.send_command.call_args_list[0][0][0]
        assert payload.get("params", {}).get("text") == "<<hello world & more>>"


# ============================================================================
# ENGINE: failure stops remaining steps (spec rule 2.2)
# ============================================================================


class TestEngineStopOnFailure:
    @pytest.mark.asyncio
    async def test_bad_scheme_after_substitution_stops_remaining_steps(
        self, parser
    ):
        match = re.fullmatch(r"open (.+)", "open something")
        steps = [
            {"function": "open_url", "params": ["file:///C:/g1"]},
            {"function": "type_text", "params": ["never typed"]},
        ]
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(
                match, steps, validation_group=None
            )
        assert result is False
        mock_open.assert_not_called()
        parser.speech_handler.app.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_marker_in_host_position_fails_step(self, parser):
        # wh-open-url-action.1.1: quote_plus leaves letters, digits, dots,
        # and hyphens unchanged, so an encoded spoken value placed in the
        # host position IS a valid host -- speech would pick the
        # destination. The engine must reject a template whose scheme or
        # host contains a substitution marker, before any launch.
        match = re.fullmatch(r"open (.+)", "open evil.example.com")
        steps = [
            {"function": "open_url", "params": ["http://g1/"]},
            {"function": "type_text", "params": ["never typed"]},
        ]
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(
                match, steps, validation_group=None
            )
        assert result is False
        mock_open.assert_not_called()
        parser.speech_handler.app.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_stored_result_in_host_position_fails_step(self, parser):
        # Same class as the capture-group case: a stored earlier result
        # (here clipboard text) must not become the host either
        # (wh-open-url-action.1.1).
        mock_pyperclip = MagicMock()
        mock_pyperclip.paste.return_value = "evil.example.com"
        match = re.fullmatch(r"lookup", "lookup")
        steps = [
            {"function": "capture_clipboard", "params": []},
            {
                "function": "open_url",
                "params": ["https://capture_clipboard/"],
            },
        ]
        with patch.dict("sys.modules", {"pyperclip": mock_pyperclip}):
            with patch("speech.actions.webbrowser.open") as mock_open:
                result = await parser._execute_rule(
                    match, steps, validation_group=None
                )
        assert result is False
        mock_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_marker_in_scheme_position_fails_step(self, parser):
        # A marker before :// could pick between schemes; the scheme must
        # be written in the template (wh-open-url-action.1.1).
        match = re.fullmatch(r"visit (.+)", "visit https")
        steps = [{"function": "open_url", "params": ["g1://example.com/"]}]
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(
                match, steps, validation_group=None
            )
        assert result is False
        mock_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_unmatched_optional_capture_in_template_fails_step(
        self, parser
    ):
        # wh-open-url-action.1.3: an unmatched optional capture leaves g1
        # as None; the replacement loop would skip it and launch the URL
        # with the literal text "g1" inside. The step must fail instead,
        # matching what the bare-marker path already does.
        match = re.fullmatch(r"lookup(?: (.+))?", "lookup")
        steps = [
            {
                "function": "open_url",
                "params": ["https://search.example/?q=g1"],
            }
        ]
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(
                match, steps, validation_group=None
            )
        assert result is False
        mock_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_result_alias_shadowing_capture_key_cannot_supply_host(
        self, parser
    ):
        # wh-open-url-action.1.6: a step's result key may reuse a capture
        # name ("g1") on a pattern with no capture groups, overwriting the
        # seeded None. The undefined-capture exemption must not apply to
        # that slot once real data was written into it -- otherwise stored
        # data (here clipboard text) chooses the host.
        mock_pyperclip = MagicMock()
        mock_pyperclip.paste.return_value = "attacker.example.test"
        match = re.fullmatch(r"open clipboard site", "open clipboard site")
        steps = [
            {
                "function": "capture_clipboard",
                "params": [],
                "result": "g1",
            },
            {"function": "open_url", "params": ["https://g1/"]},
        ]
        with patch.dict("sys.modules", {"pyperclip": mock_pyperclip}):
            with patch("speech.actions.webbrowser.open") as mock_open:
                result = await parser._execute_rule(
                    match, steps, validation_group=None
                )
        assert result is False
        mock_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_result_alias_in_query_position_substitutes_encoded(
        self, parser
    ):
        # Companion guard for the wh-open-url-action.1.6 fix: a result
        # alias that shadows an undefined capture key is still a working
        # reference in a data position -- it substitutes URL-encoded, like
        # any stored result.
        mock_pyperclip = MagicMock()
        mock_pyperclip.paste.return_value = "a&b c"
        match = re.fullmatch(r"lookup clip", "lookup clip")
        steps = [
            {
                "function": "capture_clipboard",
                "params": [],
                "result": "g1",
            },
            {
                "function": "open_url",
                "params": ["https://example.com/?q=g1"],
            },
        ]
        with patch.dict("sys.modules", {"pyperclip": mock_pyperclip}):
            with patch("speech.actions.webbrowser.open") as mock_open:
                result = await parser._execute_rule(
                    match, steps, validation_group=None
                )
        assert result is True
        mock_open.assert_called_once_with("https://example.com/?q=a%26b+c")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "template",
        [
            "https:///g1/path",
            "http:///g1/path",
            "https:////g1",
            "https://\\/g1",
            "https://\t/g1",
            "https:///fixed.g1/",
        ],
    )
    async def test_separator_run_after_scheme_fails_step(
        self, parser, template
    ):
        # wh-open-url-action.1.8: browsers normalize extra separators
        # (slashes, backslashes, tab/CR/LF) after the scheme before
        # finding the host, so "https:///g1/path" parses the substituted
        # text as the host even though the engine's protected region saw
        # an empty authority with no marker in it. The engine must reject
        # a template whose authority is empty (a separator run follows the
        # scheme) and any template containing control characters.
        match = re.fullmatch(r"open (.+)", "open spoken.example")
        steps = [{"function": "open_url", "params": [template]}]
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(
                match, steps, validation_group=None
            )
        assert result is False
        mock_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_separator_run_with_stored_result_fails_step(self, parser):
        # Same class (wh-open-url-action.1.8), stored-result variant:
        # clipboard text must not become the host through an
        # empty-authority template either.
        mock_pyperclip = MagicMock()
        mock_pyperclip.paste.return_value = "evil.example.com"
        match = re.fullmatch(r"lookup", "lookup")
        steps = [
            {"function": "capture_clipboard", "params": []},
            {
                "function": "open_url",
                "params": ["https:///capture_clipboard/path"],
            },
        ]
        with patch.dict("sys.modules", {"pyperclip": mock_pyperclip}):
            with patch("speech.actions.webbrowser.open") as mock_open:
                result = await parser._execute_rule(
                    match, steps, validation_group=None
                )
        assert result is False
        mock_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_host_marker_rejection_log_redacts_template(
        self, parser, caplog, monkeypatch
    ):
        # wh-open-url-action.1.9: an authored template can carry secrets
        # (signed query values, access tokens). The host-marker rejection
        # message is logged by the engine's rule-failure handler, so the
        # template text must be redacted inside the message.
        import logging

        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        match = re.fullmatch(r"open (.+)", "open something")
        steps = [
            {
                "function": "open_url",
                "params": ["https://g1.example.test/?token=review-secret-token"],
            }
        ]
        with patch("speech.actions.webbrowser.open") as mock_open:
            with caplog.at_level(
                logging.ERROR, logger="speech.command_engine"
            ):
                result = await parser._execute_rule(
                    match, steps, validation_group=None
                )
        assert result is False
        mock_open.assert_not_called()
        assert not any(
            "review-secret-token" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_unresolved_marker_rejection_log_redacts_template(
        self, parser, caplog, monkeypatch
    ):
        # Sibling branch (wh-open-url-action.1.9): the unresolved-marker
        # message redacts the template too.
        import logging

        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        match = re.fullmatch(r"lookup(?: (.+))?", "lookup")
        steps = [
            {
                "function": "open_url",
                "params": [
                    "https://fixed.example.test/?token=review-secret-token&q=g1"
                ],
            }
        ]
        with patch("speech.actions.webbrowser.open") as mock_open:
            with caplog.at_level(
                logging.ERROR, logger="speech.command_engine"
            ):
                result = await parser._execute_rule(
                    match, steps, validation_group=None
                )
        assert result is False
        mock_open.assert_not_called()
        assert not any(
            "review-secret-token" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_bare_group_param_cannot_supply_whole_url(self, parser):
        # Spec 5.1: speech fills in the blank; it does not decide where the
        # browser goes. A bare capture-group param hands the engine's raw
        # value to open_url, which then fails the scheme check even when
        # the spoken text looks like a URL.
        match = re.fullmatch(r"go (.+)", "go https://evil.example.com/x")
        steps = [{"function": "open_url", "params": ["g1"]}]
        with patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(
                match, steps, validation_group=None
            )
        assert result is False
        mock_open.assert_not_called()
