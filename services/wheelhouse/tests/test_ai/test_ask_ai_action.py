"""TDD coverage for the ``ask_ai`` pattern action."""

from __future__ import annotations

import asyncio
import logging
import re
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ai.providers.openai_compat import ChatResult, ChatStatus
from ai.service import AIService
from speech.action_catalog import ACTION_CATALOG
from speech.actions import ActionFunctions
from speech.command_engine import TextParser
from speech.pattern_catalog import PatternCatalog


def _result(text: str = "reply", *, status: ChatStatus = ChatStatus.OK) -> ChatResult:
    return ChatResult(status=status, text=text)


def _make_ai(
    *,
    response: ChatResult | None = None,
    ready: bool = True,
    request_timeout_s: float = 60.0,
) -> MagicMock:
    ai = MagicMock()
    ai.ask = AsyncMock(
        return_value=response if response is not None else _result()
    )
    ai.speak_brief = AsyncMock()
    ai.is_ready = MagicMock(return_value=ready)
    ai.get_request_timeout_s = MagicMock(return_value=request_timeout_s)
    ai._processing_lock = asyncio.Lock()
    ai.is_processing = MagicMock(side_effect=ai._processing_lock.locked)
    return ai


def _make_actions(
    ai: MagicMock | None,
    actions_config: dict[str, object] | None = None,
) -> tuple[ActionFunctions, MagicMock]:
    handler = MagicMock()
    handler.config_service.get.return_value = actions_config or {}
    handler.app.send_command = AsyncMock()
    handler.app.send_request = AsyncMock()
    if ai is None:
        handler.logic_controller = None
        return ActionFunctions(handler), MagicMock()

    queue = MagicMock()
    state_manager = MagicMock()
    state_manager.state_to_gui_queue = queue
    service_manager = MagicMock()
    service_manager.ai_service = ai
    logic_controller = MagicMock()
    logic_controller.state_manager = state_manager
    logic_controller.service_manager = service_manager
    handler.logic_controller = logic_controller
    return ActionFunctions(handler), queue


class TestAskAIAction:
    @pytest.mark.asyncio
    async def test_returns_trimmed_reply_and_shows_working_indication(self):
        ai = _make_ai(response=_result("  concise reply \n"))
        actions, queue = _make_actions(ai)

        result = await actions.ask_ai("Answer about the substituted value")

        assert result == "concise reply"
        ai.ask.assert_awaited_once_with("Answer about the substituted value")
        ai.speak_brief.assert_not_called()
        sent = [c.args[0] for c in queue.put_nowait.call_args_list]
        assert len(sent) == 2
        show, hide = sent
        assert show["action"] == "show_working"
        assert show["message"] == "Asking..."
        assert hide["action"] == "hide_working"
        # The close names the request that raised the dialog. Without
        # that, this close dismissed the "Loading <engine>" dialog of a
        # provider switch started while the request ran, and the engine
        # went on starting with nothing on screen
        # (wh-dialog-ownership-token).
        assert show["owner"] == hide["owner"]
        assert show["owner"].startswith("ai:")

    @pytest.mark.asyncio
    async def test_accepts_a_precomposed_substitution_prompt_without_rewriting_it(self):
        ai = _make_ai()
        actions, _queue = _make_actions(ai)
        prompt = "Answer topic=g1, previous=result-from-run-capture."

        await actions.ask_ai(prompt)

        ai.ask.assert_awaited_once_with(prompt)

    @pytest.mark.asyncio
    async def test_missing_ai_service_raises_and_logs_the_unavailable_reason(self, caplog):
        actions, _queue = _make_actions(None)

        with caplog.at_level(logging.WARNING), pytest.raises(
            RuntimeError, match="AI service unavailable"
        ):
            await actions.ask_ai("question")

        assert "ask_ai failed: AI service unavailable" in caplog.text

    @pytest.mark.asyncio
    async def test_disabled_or_unconfigured_ai_raises_and_logs_its_reason(self, caplog):
        ai = _make_ai(ready=False)
        actions, _queue = _make_actions(ai)

        with caplog.at_level(logging.WARNING), pytest.raises(
            RuntimeError, match="not configured or unavailable"
        ):
            await actions.ask_ai("question")

        assert "ask_ai failed: AI subsystem is not configured or unavailable" in caplog.text
        ai.ask.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_ok_server_response_raises_and_logs_its_reason(self, caplog):
        ai = _make_ai(response=_result(status=ChatStatus.TRANSPORT_ERROR))
        actions, queue = _make_actions(ai)

        with caplog.at_level(logging.WARNING), pytest.raises(
            RuntimeError, match="request not ok"
        ):
            await actions.ask_ai("question")

        assert "ask_ai failed: AI request not ok" in caplog.text
        hides = [
            c.args[0] for c in queue.put_nowait.call_args_list
            if c.args[0].get("action") == "hide_working"
        ]
        assert len(hides) == 1
        assert hides[0]["owner"].startswith("ai:")

    @pytest.mark.asyncio
    async def test_request_timeout_raises_and_logs_its_reason(self, caplog):
        ai = _make_ai(request_timeout_s=0.01)

        async def never_returns(_prompt):
            await asyncio.Event().wait()

        ai.ask = AsyncMock(side_effect=never_returns)
        actions, queue = _make_actions(ai)

        with caplog.at_level(logging.WARNING), pytest.raises(
            TimeoutError, match="request timed out"
        ):
            await actions.ask_ai("question")

        assert "ask_ai failed: request timed out" in caplog.text
        hides = [
            c.args[0] for c in queue.put_nowait.call_args_list
            if c.args[0].get("action") == "hide_working"
        ]
        assert len(hides) == 1
        assert hides[0]["owner"].startswith("ai:")

    @pytest.mark.asyncio
    async def test_timeout_consumes_a_cancel_requested_during_the_request(self):
        ai = _make_ai(request_timeout_s=0.01)
        ai.cancel_requested = False

        async def never_returns(_prompt):
            ai.cancel_requested = True
            await asyncio.Event().wait()

        ai.ask = AsyncMock(side_effect=never_returns)
        actions, _queue = _make_actions(ai)

        with pytest.raises(TimeoutError, match="request timed out"):
            await actions.ask_ai("question")

        assert ai.cancel_requested is False

    @pytest.mark.asyncio
    async def test_wait_uses_the_sixty_second_ceiling(self):
        ai = _make_ai(request_timeout_s=120.0)
        actions, _queue = _make_actions(ai)
        original_wait_for = asyncio.wait_for
        observed_timeouts: list[float] = []

        async def recording_wait_for(awaitable, *, timeout):
            observed_timeouts.append(timeout)
            return await original_wait_for(awaitable, timeout=timeout)

        with patch("speech.actions.asyncio.wait_for", new=recording_wait_for):
            assert await actions.ask_ai("question") == "reply"

        assert observed_timeouts == [60.0]

    @pytest.mark.asyncio
    async def test_reply_over_output_cap_raises_and_logs_its_reason(self, caplog):
        ai = _make_ai(response=_result("six chars"))
        actions, queue = _make_actions(ai, {"output_cap_chars": 5})

        with caplog.at_level(logging.WARNING), pytest.raises(
            ValueError, match="reply exceeds output_cap_chars"
        ):
            await actions.ask_ai("question")

        assert "ask_ai failed: reply exceeds output_cap_chars" in caplog.text
        hides = [
            c.args[0] for c in queue.put_nowait.call_args_list
            if c.args[0].get("action") == "hide_working"
        ]
        assert len(hides) == 1
        assert hides[0]["owner"].startswith("ai:")

    @pytest.mark.asyncio
    async def test_mid_request_cancel_is_consumed_and_does_not_cancel_next_transform(
        self, ai_config, mock_provider, caplog
    ):
        """A cancel during ask_ai must not leak into the next text transform."""
        service = AIService(ai_config)
        service._provider = mock_provider
        service._ready = True
        service.speak_brief = AsyncMock()
        actions, _queue = _make_actions(service)
        cancelled = False

        async def response_after_cancel(*_args, **_kwargs):
            nonlocal cancelled
            if not cancelled:
                cancelled = True
                await actions.cancel_fix()
                return _result("discarded ask reply")
            return _result("corrected text")

        mock_provider.chat = AsyncMock(side_effect=response_after_cancel)

        with caplog.at_level(logging.WARNING), pytest.raises(
            RuntimeError, match="request cancelled"
        ):
            await actions.ask_ai("question")

        assert "ask_ai failed: request cancelled" in caplog.text
        assert service.cancel_requested is False
        result = await service.fix_text("original text")
        assert result.status is ChatStatus.OK
        assert result.text == "corrected text"
        assert mock_provider.chat.await_count == 2

    @pytest.mark.asyncio
    async def test_busy_processing_lock_raises_and_logs_its_reason(self, caplog):
        ai = _make_ai()
        actions, _queue = _make_actions(ai)
        await ai._processing_lock.acquire()
        try:
            with caplog.at_level(logging.WARNING), pytest.raises(
                RuntimeError, match="processing lock busy"
            ):
                await actions.ask_ai("question")
        finally:
            ai._processing_lock.release()

        assert "ask_ai failed: processing lock busy" in caplog.text
        ai.ask.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prompt", [None, "", " \t\n "])
    async def test_blank_prompt_raises_and_logs_its_reason(self, prompt, caplog):
        ai = _make_ai()
        actions, _queue = _make_actions(ai)

        with caplog.at_level(logging.WARNING), pytest.raises(
            ValueError, match="prompt is blank or missing"
        ):
            await actions.ask_ai(prompt)

        assert "ask_ai failed: prompt is blank or missing" in caplog.text
        ai.ask.assert_not_awaited()


class TestAskAIEngine:
    @pytest.mark.asyncio
    async def test_stores_named_reply_for_a_later_step_after_generic_substitution(self):
        ai = _make_ai(response=_result("  model answer  "))
        actions, _queue = _make_actions(ai)
        handler = actions.speech_handler
        handler.speech_processor = None
        parser = TextParser(handler, PatternCatalog("speech/config/patterns.toml"))
        parser.action_functions._functions["previous_value"] = (
            lambda: "captured result"
        )

        match = re.fullmatch(r"ask (.+)", "ask project status")
        assert match is not None
        steps = [
            {"function": "previous_value", "params": [], "result": "source"},
            {
                "function": "ask_ai",
                "params": ["Answer g1 using source."],
                "result": "answer",
            },
            {"function": "insert_text", "params": ["answer"]},
        ]

        assert await parser._execute_rule(match, steps, validation_group=None) is True
        ai.ask.assert_awaited_once_with(
            "Answer project status using captured result."
        )
        payload = handler.app.send_command.call_args.args[0]
        assert payload["params"]["insertion_string"] == "model answer"


class TestAskAIService:
    @pytest.mark.asyncio
    async def test_ask_uses_the_in_process_provider_without_taking_the_lock(
        self, ai_config, mock_provider
    ):
        mock_provider.chat = AsyncMock(
            return_value=_result("provider reply")
        )
        service = AIService(ai_config)
        service._provider = mock_provider
        await service._processing_lock.acquire()
        try:
            result = await service.ask("What is the status?")
        finally:
            service._processing_lock.release()

        assert result == _result("provider reply")
        messages = mock_provider.chat.call_args.args[0]
        assert messages[-1] == {"role": "user", "content": "What is the status?"}

    @pytest.mark.asyncio
    async def test_ask_consumes_a_preexisting_cancel_before_calling_provider(
        self, ai_config, mock_provider
    ):
        service = AIService(ai_config)
        service._provider = mock_provider
        service.cancel_requested = True

        result = await service.ask("What is the status?")

        assert result.status is ChatStatus.CANCELLED
        assert service.cancel_requested is False
        mock_provider.chat.assert_not_awaited()

    def test_request_timeout_reads_the_ai_server_setting(self):
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "ai.server.model": "model",
            "ai.server.timeout_s": 17,
        }.get(key, default)

        assert AIService(config).get_request_timeout_s() == 17.0

    def test_request_timeout_defaults_for_a_huge_integer(self, caplog):
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "ai.server.model": "model",
            "ai.server.timeout_s": 10**1000,
        }.get(key, default)

        with caplog.at_level(logging.WARNING):
            assert AIService(config).get_request_timeout_s() == 60.0

        assert (
            "Invalid ai.server.timeout_s for action request; using 60 seconds"
            in caplog.text
        )


def test_ask_ai_catalog_entry_has_one_text_prompt_parameter():
    entry = next(item for item in ACTION_CATALOG if item["name"] == "ask_ai")
    assert entry["audience"] == "advanced"
    assert entry["params"] == [
        {
            "name": "prompt",
            "summary": entry["params"][0]["summary"],
            "kind": "text",
        }
    ]
