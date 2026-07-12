"""Tests for HelpChatSession -- multi-turn conversation state manager."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from ai.providers.openai_compat import ChatResult, ChatStatus


def _make_ai_service(response="Test answer"):
    """Create a mock AIService with configurable chat_help response."""
    ai = MagicMock()
    ai.chat_help = AsyncMock(return_value=response)
    return ai


class TestAsk:
    """Tests for HelpChatSession.ask()."""

    @pytest.mark.asyncio
    async def test_ask_returns_response(self):
        """ask() returns the model response."""
        from ai.help_chat import HelpChatSession

        ai = _make_ai_service(response="Here is the answer.")
        session = HelpChatSession(ai)

        result = await session.ask("How do I use commands?")
        assert result == "Here is the answer."

    @pytest.mark.asyncio
    async def test_ask_appends_to_history(self):
        """ask() adds user and assistant messages to history."""
        from ai.help_chat import HelpChatSession

        ai = _make_ai_service(response="Answer 1")
        session = HelpChatSession(ai)

        await session.ask("Question 1")

        assert len(session._history) == 2
        assert session._history[0] == {"role": "user", "content": "Question 1"}
        assert session._history[1] == {"role": "assistant", "content": "Answer 1"}

    @pytest.mark.asyncio
    async def test_ask_passes_full_history(self):
        """ask() passes all accumulated turns to chat_help."""
        from ai.help_chat import HelpChatSession

        ai = _make_ai_service(response="Answer")
        session = HelpChatSession(ai)

        await session.ask("First question")

        ai.chat_help.reset_mock()
        ai.chat_help.return_value = "Second answer"
        await session.ask("Second question")

        # chat_help should receive all 3 messages (Q1, A1, Q2)
        history_arg = ai.chat_help.call_args[0][0]
        assert len(history_arg) == 3  # Q1, A1, Q2
        assert history_arg[0]["content"] == "First question"
        assert history_arg[1]["content"] == "Answer"
        assert history_arg[2]["content"] == "Second question"

    @pytest.mark.asyncio
    async def test_ask_removes_question_on_failure(self):
        """ask() removes the user question from history when chat_help returns
        a falsy response (the str-return contract ask consumes), and the
        coordinator surfaces that failure as a non-OK ChatResult via help_ask.
        """
        from ai.help_chat import HelpChatSession

        ai = _make_ai_service(response=None)
        session = HelpChatSession(ai)

        result = await session.ask("Failed question")

        assert result is None
        assert len(session._history) == 0  # Question was removed

    @pytest.mark.asyncio
    async def test_help_ask_returns_non_ok_chat_result_on_failure(self, ai_config):
        """help_ask() returns a non-OK ChatResult when the underlying chat
        fails (design 5.1a): the failure that pops the question from history
        is surfaced to callers as a non-OK ChatResult, not None."""
        from ai.service import AIService
        from ai.help_chat import HelpChatSession

        service = AIService(ai_config)
        provider = MagicMock()
        provider.chat = AsyncMock(
            return_value=ChatResult(status=ChatStatus.TRANSPORT_ERROR)
        )
        service._provider = provider
        service._knowledge_base = "kb"
        service._help_session = HelpChatSession(service)

        result = await service.help_ask("Failed question")

        assert isinstance(result, ChatResult)
        assert result.ok is False
        assert result.status is ChatStatus.TRANSPORT_ERROR
        # The unanswered question was popped from history.
        assert len(service._help_session._history) == 0

    @pytest.mark.asyncio
    async def test_multi_turn_accumulation(self):
        """History accumulates correctly across multiple successful turns."""
        from ai.help_chat import HelpChatSession

        ai = MagicMock()
        ai.chat_help = AsyncMock(side_effect=["A1", "A2", "A3"])
        session = HelpChatSession(ai)

        await session.ask("Q1")
        await session.ask("Q2")
        await session.ask("Q3")

        assert len(session._history) == 6  # 3 user + 3 assistant
        assert session._history[4] == {"role": "user", "content": "Q3"}
        assert session._history[5] == {"role": "assistant", "content": "A3"}


class TestReset:
    """Tests for HelpChatSession.reset()."""

    @pytest.mark.asyncio
    async def test_reset_clears_history(self):
        """reset() empties conversation history."""
        from ai.help_chat import HelpChatSession

        ai = _make_ai_service(response="Answer")
        session = HelpChatSession(ai)

        await session.ask("Question")
        assert len(session._history) == 2

        session.reset()
        assert len(session._history) == 0

    @pytest.mark.asyncio
    async def test_reset_then_ask_starts_fresh(self):
        """After reset, next ask() sends only the new question."""
        from ai.help_chat import HelpChatSession

        ai = _make_ai_service(response="Fresh answer")
        session = HelpChatSession(ai)

        await session.ask("Old question")
        session.reset()
        ai.chat_help.reset_mock()

        await session.ask("New question")

        history_arg = ai.chat_help.call_args[0][0]
        assert len(history_arg) == 1
        assert history_arg[0]["content"] == "New question"


class TestHandleHelpAskTruncationHint:
    """Tests for _handle_help_ask's section-7 notices (finding 2.4)."""

    def _make_controller(self, *, ready=True, result=None, recheck=True):
        """Build a LogicController bound to mock services for _handle_help_ask."""
        from main import LogicController

        ai = MagicMock()
        ai.is_ready = MagicMock(return_value=ready)
        ai.help_ask = AsyncMock(return_value=result)
        ai.recheck_ready = AsyncMock(return_value=recheck)

        service_manager = MagicMock()
        service_manager.ai_service = ai

        controller = MagicMock(spec=LogicController)
        controller.service_manager = service_manager
        controller.config_service = MagicMock()
        # _send_help_response / _send_help_error capture their argument.
        controller._sent_response = None
        controller._sent_error = None
        controller._send_help_response = lambda text: setattr(
            controller, "_sent_response", text
        )
        controller._send_help_error = lambda msg: setattr(
            controller, "_sent_error", msg
        )
        return LogicController, controller, ai

    @pytest.mark.asyncio
    async def test_truncated_ok_result_shows_too_small_hint(self):
        """An OK ChatResult with finish_reason == 'length' appends the
        'model may be too small for help' hint (finding 2.4)."""
        result = ChatResult(
            status=ChatStatus.OK, text="partial answer", finish_reason="length"
        )
        LogicController, controller, ai = self._make_controller(result=result)

        await LogicController._handle_help_ask(controller, "how do I move a window")

        assert controller._sent_response is not None
        assert "partial answer" in controller._sent_response
        assert "too small" in controller._sent_response.lower()

    @pytest.mark.asyncio
    async def test_untruncated_ok_result_has_no_hint(self):
        """A non-truncated OK ChatResult is sent verbatim, no hint."""
        result = ChatResult(status=ChatStatus.OK, text="full answer", finish_reason="stop")
        LogicController, controller, ai = self._make_controller(result=result)

        await LogicController._handle_help_ask(controller, "q")

        assert controller._sent_response == "full answer"

    @pytest.mark.asyncio
    async def test_not_ready_sends_error_without_calling_help_ask(self):
        """is_ready() False short-circuits before help_ask (finding 1.6)."""
        LogicController, controller, ai = self._make_controller(ready=False)

        await LogicController._handle_help_ask(controller, "q")

        ai.help_ask.assert_not_awaited()
        assert controller._sent_error is not None

    @pytest.mark.asyncio
    async def test_model_not_found_sends_distinct_error(self):
        """MODEL_NOT_FOUND gets its own error naming the model problem
        (wh-75m). The server DID respond (404 on the model), so no
        reachability re-probe runs and the 'isn't responding' wording is
        wrong for this case."""
        result = ChatResult(status=ChatStatus.MODEL_NOT_FOUND)
        LogicController, controller, ai = self._make_controller(result=result)

        await LogicController._handle_help_ask(controller, "q")

        ai.recheck_ready.assert_not_awaited()
        assert controller._sent_error is not None
        assert "model" in controller._sent_error.lower()
        assert "responding" not in controller._sent_error.lower()

    @pytest.mark.asyncio
    async def test_reasoning_exhausted_sends_distinct_error(self):
        """EMPTY + finish_reason == 'length' is the reasoning-model
        signature (wh-ai-reasoning-model-empty): distinct error wording,
        no reachability re-probe (the server DID respond)."""
        result = ChatResult(status=ChatStatus.EMPTY, finish_reason="length")
        LogicController, controller, ai = self._make_controller(result=result)

        await LogicController._handle_help_ask(controller, "q")

        ai.recheck_ready.assert_not_awaited()
        assert controller._sent_error is not None
        assert "reasoning" in controller._sent_error.lower()

    @pytest.mark.asyncio
    async def test_non_ok_result_rechecks_before_wording(self):
        """A non-OK ChatResult triggers recheck_ready() before the error
        wording (s7 / decision 27)."""
        result = ChatResult(status=ChatStatus.TRANSPORT_ERROR)
        LogicController, controller, ai = self._make_controller(
            result=result, recheck=False
        )

        await LogicController._handle_help_ask(controller, "q")

        ai.recheck_ready.assert_awaited_once()
        assert controller._sent_error is not None
        assert "responding" in controller._sent_error.lower()
