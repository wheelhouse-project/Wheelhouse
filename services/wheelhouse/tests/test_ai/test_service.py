"""Tests for AIService -- coordinator for text correction and help Q&A.

Covers:
- Lifecycle (start/stop, knowledge base loading)
- Text correction (fix_text with sanitization)
- Help Q&A (single-turn for Phase 2)
- Concurrency (processing lock, cancel flag)
- Response sanitization (strip LLM artifacts)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai.service import AIService, _legacy_to_result
from ai.help_chat import HelpChatSession
from ai.providers.openai_compat import ChatResult, ChatStatus, OpenAIProvider


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------

class TestLifecycle:

    @pytest.mark.asyncio
    async def test_start_builds_server_provider_from_ai_server(self, ai_config):
        """start() builds a single OpenAIProvider from the [ai.server] block
        (thin-client path, design 5.2) -- no eager load and no multi-provider
        factory; the one server client is wired directly."""
        service = AIService(ai_config)

        with patch.object(service, "_load_knowledge_base", return_value="kb"):
            with patch.object(service, "_launch_background_probes"):
                await service.start()

        assert isinstance(service._provider, OpenAIProvider)
        # Wired from [ai.server].
        assert service._provider._model == "qwen3.5:9b"
        assert service._provider._base_url == "http://localhost:8781/v1"

    @pytest.mark.asyncio
    async def test_start_loads_knowledge_base(self, ai_config):
        """start() loads knowledge base content from file."""
        service = AIService(ai_config)

        with patch.object(
            service, "_load_knowledge_base", return_value="test kb content"
        ):
            with patch.object(service, "_launch_background_probes"):
                await service.start()

        assert service._knowledge_base == "test kb content"

    @pytest.mark.asyncio
    async def test_start_ai_off_builds_no_provider(self, caplog):
        """When AI is off (no [ai.server].base_url) start() builds no provider
        and launches no background probe (design 5.2)."""
        config = MagicMock()
        config.get = MagicMock(side_effect=lambda key, default=None: {
            "ai.enabled": True,
            "ai.server.base_url": "",
            "ai.models_directory": "D:/Models",
        }.get(key, default))
        service = AIService(config)

        with patch.object(service, "_load_knowledge_base", return_value="kb"):
            await service.start()

        assert service._provider is None
        assert service._bg_tasks == []

    @pytest.mark.asyncio
    async def test_start_warns_when_ai_server_missing_but_enabled(self, caplog):
        """One-time warning when ai.enabled is true but [ai.server] is blank
        (upgrade-from-old-config, design 5.4).

        The warning must fire on the real start() path, not via a direct call
        to _build_server_provider() which is unreachable when base_url is blank
        (finding wh-ay6h.6.3). start() emits the warning before the _ai_off()
        early return so the user sees why AI is off.
        """
        config = MagicMock()
        config.get = MagicMock(side_effect=lambda key, default=None: {
            "ai.enabled": True,
            "ai.server.base_url": "",
            "ai.server.enabled": True,
            "ai.models_directory": "D:/Models",
        }.get(key, default))
        service = AIService(config)

        with patch.object(service, "_load_knowledge_base", return_value="kb"):
            await service.start()

        assert "[ai.server]" in caplog.text
        assert service._provider is None

    def test_build_server_provider_marks_cloud_kind(self):
        """A cloud [ai.server].kind is passed to the provider as is_cloud=True,
        so the provider suppresses the misleading non-/v1 warning for the Gemini
        cloud endpoint the installer pins as the default (deepseek round 2,
        finding 1.4)."""
        config = MagicMock()
        config.get = MagicMock(side_effect=lambda key, default=None: {
            "ai.server.base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "ai.server.model": "gemini-2.5-flash-lite",
            "ai.server.kind": "cloud",
            "ai.server.timeout_s": 60,
        }.get(key, default))
        service = AIService(config)
        provider = service._build_server_provider()
        assert provider._is_cloud is True

    def test_build_server_provider_local_kind_is_not_cloud(self):
        """The default local kind leaves is_cloud False, so the non-/v1 warning
        still fires for a local endpoint that is not a /v1 root."""
        config = MagicMock()
        config.get = MagicMock(side_effect=lambda key, default=None: {
            "ai.server.base_url": "http://localhost:11434/v1",
            "ai.server.model": "gemma3:12b",
            "ai.server.kind": "local",
            "ai.server.timeout_s": 60,
        }.get(key, default))
        service = AIService(config)
        provider = service._build_server_provider()
        assert provider._is_cloud is False

    @pytest.mark.asyncio
    async def test_stop_unloads_model_ollama(self, ai_config, mock_provider):
        """stop() calls unload_model() for Ollama-style providers."""
        service = AIService(ai_config)
        service._provider = mock_provider

        await service.stop()

        mock_provider.unload_model.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_closes_provider_session(self, ai_config, mock_provider):
        """stop() calls close() on the provider."""
        service = AIService(ai_config)
        service._provider = mock_provider

        await service.stop()

        mock_provider.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_awaits_background_tasks_before_close(self, ai_config, mock_provider):
        """stop() must await cancelled background tasks before calling
        provider.close(), so the aiohttp session cannot be closed under a
        task still suspended inside a GET request (wh-ay6h.2.1)."""
        service = AIService(ai_config)
        service._provider = mock_provider

        # Simulate a background task that is in-flight (never actually runs).
        async def _never_ending():
            await asyncio.sleep(9999)

        task = asyncio.create_task(_never_ending())
        service._bg_tasks = [task]

        close_call_order = []

        original_close = mock_provider.close

        async def _record_close():
            close_call_order.append("close")
            await original_close()

        mock_provider.close = _record_close

        # Track when the task is done relative to close().
        task.add_done_callback(lambda _t: close_call_order.append("task_done"))

        await service.stop()

        # task_done must appear before close in the call record.
        assert "task_done" in close_call_order
        assert "close" in close_call_order
        assert close_call_order.index("task_done") < close_call_order.index("close")
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_propagates_non_cancelled_bg_task_exception(self, ai_config, mock_provider):
        """stop() re-raises a non-CancelledError BaseException from a background task.

        Regression for wh-ay6h.21.2 / wh-ay6h.21.3: the asyncio.gather result
        loop in stop() must surface any BaseException that is not a
        CancelledError (e.g. _HttpBlockedError from the HTTP guard, or any
        sentinel exception from a rogue task) rather than silently discarding it.
        """
        service = AIService(ai_config)
        service._provider = mock_provider

        class _SentinelError(BaseException):
            """Test-only BaseException sentinel."""

        async def _raises_sentinel():
            raise _SentinelError("bg task failed")

        task = asyncio.create_task(_raises_sentinel())
        # Deterministically drive the task to completion before stop() gathers
        # it: awaiting it raises the already-stored exception, which is the
        # scenario stop() handles. asyncio.sleep(0) is not a guaranteed
        # completion point across event-loop implementations (wh-ay6h.22.13).
        with pytest.raises(_SentinelError, match="bg task failed"):
            await task
        service._bg_tasks = [task]

        with pytest.raises(_SentinelError, match="bg task failed"):
            await service.stop()

        # wh-ay6h.21.4: stop() must still run the full cleanup before it
        # re-raises the propagated exception. Otherwise the propagation path
        # leaves the service half-stopped: the failed task stays in
        # self._bg_tasks and the provider session is never closed.
        assert service._bg_tasks == []
        assert service._provider is None
        mock_provider.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_suppresses_cancelled_error_and_cleans_up(self, ai_config, mock_provider):
        """stop() suppresses CancelledError from cancelled bg tasks and still
        closes the provider and clears _bg_tasks.

        Regression for wh-ay6h.21.2 / wh-ay6h.21.3: CancelledError is the
        normal outcome for a freshly-cancelled task and must not propagate out
        of stop() -- only unexpected BaseException subclasses should.
        """
        service = AIService(ai_config)
        service._provider = mock_provider

        async def _sleeps_forever():
            await asyncio.sleep(9999)

        task = asyncio.create_task(_sleeps_forever())
        service._bg_tasks = [task]

        # stop() must complete without raising and must clean up state.
        await service.stop()

        assert task.cancelled()
        assert service._bg_tasks == []
        assert service._provider is None
        mock_provider.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_propagates_bg_exc_even_when_cleanup_raises(self, ai_config, mock_provider, caplog):
        """stop() re-raises the bg-task BaseException even when cleanup also raises.

        Regression for wh-ay6h.22.1: when a bg task raises a non-CancelledError
        BaseException AND a cleanup step (provider.close) also raises, the
        try/finally in stop() must guarantee the bg-task exception is still
        propagated so the root cause is not silently discarded by a secondary
        teardown failure.

        Also covers wh-ay6h.22.8 (speech.shutdown() still called when close()
        raises) and wh-ay6h.22.10 (cleanup exceptions are logged at WARNING,
        not chained onto the re-raised bg-task exception).
        """
        service = AIService(ai_config)
        service._provider = mock_provider

        class _SentinelError(BaseException):
            """Test-only BaseException sentinel (bg-task failure)."""

        async def _raises_sentinel():
            raise _SentinelError("bg task failed")

        task = asyncio.create_task(_raises_sentinel())
        # Deterministically drive the task to completion (awaiting it raises the
        # already-stored exception). asyncio.sleep(0) is not a guaranteed
        # completion point across event-loop implementations (wh-ay6h.22.13).
        with pytest.raises(_SentinelError, match="bg task failed"):
            await task
        service._bg_tasks = [task]

        # Simulate cleanup failure in provider.close()
        mock_provider.close = AsyncMock(side_effect=RuntimeError("teardown failed"))

        # Mock speech so we can assert shutdown() is still called (wh-ay6h.22.8).
        mock_speech = MagicMock()
        mock_speech.shutdown = AsyncMock()
        service._speech = mock_speech

        # The bg-task BaseException must propagate, not the cleanup RuntimeError.
        with pytest.raises(_SentinelError, match="bg task failed"):
            await service.stop()

        # Cleanup still ran: provider is cleared, _bg_tasks is empty, and
        # provider.close() was actually invoked (wh-ay6h.22.5: _provider is
        # set to None before close() is called, so only asserting _provider is
        # None would pass even if close() were skipped).
        assert service._bg_tasks == []
        assert service._provider is None
        mock_provider.close.assert_called_once()

        # wh-ay6h.22.8: speech.shutdown() must be called even when
        # provider.close() raised -- skipping it leaks the TTS executor.
        mock_speech.shutdown.assert_called_once()

        # wh-ay6h.22.10: cleanup exceptions are swallowed and logged, not
        # chained. provider.close() is wrapped in except Exception (best-effort
        # teardown, wh-ay6h.22.7), so the RuntimeError is handled before
        # pending_exc is re-raised -- pending_exc.__context__ is therefore None
        # by design. The cleanup failure is preserved by the WARNING log, which
        # is the operationally useful signal a refactor must not drop.
        assert "provider.close() raised during stop" in caplog.text

    @pytest.mark.asyncio
    async def test_stop_closes_provider_when_unload_model_raises_baseexception(
        self, ai_config, mock_provider
    ):
        """close() still runs when unload_model() raises a BaseException, and
        the BaseException is not swallowed (wh-ay6h.22.11).

        except Exception around unload_model() does not catch BaseException
        subclasses such as KeyboardInterrupt. _provider is already cleared to
        None before unload_model() runs, so without a finally that guarantees
        close(), the provider session would leak and the reference is lost.
        stop() must run close() and then let the BaseException propagate.
        """
        service = AIService(ai_config)
        service._provider = mock_provider

        class _UnloadInterrupt(BaseException):
            """Test-only BaseException sentinel (unload_model failure)."""

        mock_provider.unload_model = AsyncMock(side_effect=_UnloadInterrupt("interrupted"))
        mock_speech = MagicMock()
        mock_speech.shutdown = AsyncMock()
        service._speech = mock_speech

        with pytest.raises(_UnloadInterrupt, match="interrupted"):
            await service.stop()

        # close() ran despite the BaseException from unload_model() -- the
        # provider session is not leaked.
        mock_provider.close.assert_called_once()
        assert service._provider is None
        # speech.shutdown() still ran (outer finally).
        mock_speech.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_calls_close_after_unload_model_raises(
        self, ai_config, mock_provider
    ):
        """close() still runs when unload_model() raises a normal Exception
        (regression guard for wh-ay6h.22.7 / wh-ay6h.22.14).

        A future refactor that reorders or conditionalizes close() so it does
        not run after an unload_model() failure must be caught here.
        """
        service = AIService(ai_config)
        service._provider = mock_provider

        mock_provider.unload_model = AsyncMock(side_effect=RuntimeError("unload failed"))
        mock_speech = MagicMock()
        mock_speech.shutdown = AsyncMock()
        service._speech = mock_speech

        # A normal Exception from unload_model() is swallowed and logged; stop()
        # completes without raising.
        await service.stop()

        mock_provider.unload_model.assert_called_once()
        mock_provider.close.assert_called_once()
        assert service._provider is None


# ---------------------------------------------------------------------------
# Text correction tests
# ---------------------------------------------------------------------------

class TestFixText:

    @pytest.mark.asyncio
    async def test_fix_text_happy_path(self, ai_config, mock_provider):
        """fix_text returns an OK ChatResult carrying the corrected text."""
        mock_provider.chat = AsyncMock(return_value=ChatResult(status=ChatStatus.OK, text="Corrected text here."))
        service = AIService(ai_config)
        service._provider = mock_provider

        result = await service.fix_text("corrected text here")

        assert isinstance(result, ChatResult)
        assert result.ok is True
        assert result.text == "Corrected text here."

    @pytest.mark.asyncio
    async def test_fix_text_empty_input(self, ai_config, mock_provider):
        """fix_text returns a non-OK ChatResult for empty input."""
        service = AIService(ai_config)
        service._provider = mock_provider

        result = await service.fix_text("")
        assert isinstance(result, ChatResult)
        assert result.ok is False

        result = await service.fix_text("   ")
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_fix_text_provider_failure(self, ai_config, mock_provider):
        """fix_text returns a non-OK ChatResult when provider returns empty."""
        mock_provider.chat = AsyncMock(return_value=ChatResult(status=ChatStatus.EMPTY))
        service = AIService(ai_config)
        service._provider = mock_provider

        result = await service.fix_text("some text")
        assert isinstance(result, ChatResult)
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_fix_text_applies_sanitization(self, ai_config, mock_provider):
        """fix_text runs _sanitize_response on the provider output."""
        mock_provider.chat = AsyncMock(
            return_value=ChatResult(status=ChatStatus.OK, text="```\nCorrected text\n```")
        )
        service = AIService(ai_config)
        service._provider = mock_provider

        result = await service.fix_text("corrected text")

        # Sanitizer should strip the code fences
        assert result.ok is True
        assert result.text == "Corrected text"

    @pytest.mark.asyncio
    async def test_fix_text_uses_correction_prompt(self, ai_config, mock_provider):
        """fix_text uses TEXT_CORRECTION_SYSTEM as the system message."""
        service = AIService(ai_config)
        service._provider = mock_provider

        await service.fix_text("hello world")

        call_args = mock_provider.chat.call_args
        messages = call_args[0][0] if call_args[0] else call_args[1]["messages"]
        system_msg = messages[0]
        assert system_msg["role"] == "system"
        assert "text formatting assistant" in system_msg["content"].lower()


# ---------------------------------------------------------------------------
# Help Q&A tests (single-turn for Phase 2)
# ---------------------------------------------------------------------------

class TestAskHelp:

    @pytest.mark.asyncio
    async def test_ask_help_happy_path(self, ai_config, mock_provider):
        """ask_help returns an OK ChatResult carrying the provider response."""
        mock_provider.chat = AsyncMock(return_value=ChatResult(status=ChatStatus.OK, text="Here is the help answer."))
        service = AIService(ai_config)
        service._provider = mock_provider
        service._knowledge_base = "Some KB content"

        result = await service.ask_help("how do I use commands?")

        assert isinstance(result, ChatResult)
        assert result.ok is True
        assert result.text == "Here is the help answer."

    @pytest.mark.asyncio
    async def test_ask_help_includes_knowledge_base(
        self, ai_config, mock_provider
    ):
        """ask_help system prompt includes the knowledge base content."""
        service = AIService(ai_config)
        service._provider = mock_provider
        service._knowledge_base = "KB: voice commands are triggered by hotwords"

        await service.ask_help("how do commands work?")

        call_args = mock_provider.chat.call_args
        messages = call_args[0][0] if call_args[0] else call_args[1]["messages"]
        system_msg = messages[0]
        assert "KB: voice commands are triggered by hotwords" in system_msg["content"]

    @pytest.mark.asyncio
    async def test_ask_help_missing_knowledge_base(
        self, ai_config, mock_provider
    ):
        """ask_help returns error message when KB is not loaded."""
        service = AIService(ai_config)
        service._provider = mock_provider
        service._knowledge_base = None

        result = await service.ask_help("some question")

        assert isinstance(result, ChatResult)
        assert "knowledge base" in result.text.lower()


# ---------------------------------------------------------------------------
# Concurrency tests
# ---------------------------------------------------------------------------

class TestConcurrency:

    @pytest.mark.asyncio
    async def test_fix_text_no_lock_check(self, ai_config, mock_provider):
        """fix_text does NOT check lock -- caller manages concurrency."""
        mock_provider.chat = AsyncMock(return_value=ChatResult(status=ChatStatus.OK, text="corrected"))
        service = AIService(ai_config)
        service._provider = mock_provider

        # Lock held by caller (fix_text_ai action). fix_text should still work.
        await service._processing_lock.acquire()
        try:
            result = await service.fix_text("should still work")
            assert result.ok is True
            assert result.text == "corrected"
        finally:
            service._processing_lock.release()

    @pytest.mark.asyncio
    async def test_cancel_flag_prevents_result(self, ai_config, mock_provider):
        """When cancel_requested is set, fix_text returns CANCELLED (not EMPTY or a server error)."""
        mock_provider.chat = AsyncMock(return_value=ChatResult(status=ChatStatus.OK, text="corrected"))
        service = AIService(ai_config)
        service._provider = mock_provider
        service.cancel_requested = True

        result = await service.fix_text("some text")

        assert isinstance(result, ChatResult)
        assert result.status is ChatStatus.CANCELLED
        assert result.ok is False
        # Flag should be reset
        assert service.cancel_requested is False

    @pytest.mark.asyncio
    async def test_cancel_racing_non_ok_result_clears_flag(self, ai_config, mock_provider):
        """Regression for wh-ay6h.20.1: cancel set during chat() + provider returns
        non-OK must yield CANCELLED (not TRANSPORT_ERROR) and leave cancel_requested=False.

        Previously fix_text() checked `if not result.ok` before the post-call
        cancellation block, so the flag leaked and silently cancelled the next call.

        The flag is set as a side effect of chat() so the pre-call check at
        service.py:396-399 does NOT short-circuit.  This is the only configuration
        that distinguishes the fixed post-call ordering from the pre-fix buggy ordering.
        """
        service = AIService(ai_config)
        service._provider = mock_provider

        async def chat_then_cancel(*args, **kwargs):
            # Simulate cancel arriving while the network call was in-flight
            service.cancel_requested = True
            return ChatResult(status=ChatStatus.TRANSPORT_ERROR)

        mock_provider.chat = AsyncMock(side_effect=chat_then_cancel)

        result = await service.fix_text("some text")

        assert isinstance(result, ChatResult)
        assert result.status is ChatStatus.CANCELLED, (
            "cancel racing a non-OK result must return CANCELLED, not the error status"
        )
        assert result.ok is False
        # Flag MUST be consumed; a leaked True would silently cancel the next call
        assert service.cancel_requested is False
        # chat() must have been awaited -- confirms post-call path, not pre-call short-circuit
        assert mock_provider.chat.await_count == 1, (
            "chat() was not called; the pre-call cancel check short-circuited instead of "
            "the post-call reorder being exercised"
        )


# ---------------------------------------------------------------------------
# Response sanitizer tests
# ---------------------------------------------------------------------------

class TestSanitizeResponse:

    def _make_service(self, ai_config):
        return AIService(ai_config)

    def test_sanitize_strips_code_fences(self, ai_config):
        """Strip markdown code fences from response."""
        service = self._make_service(ai_config)
        result = service._sanitize_response("```\nHello world\n```")
        assert result == "Hello world"

    def test_sanitize_strips_code_fences_with_language(self, ai_config):
        """Strip code fences with language tag."""
        service = self._make_service(ai_config)
        result = service._sanitize_response("```text\nHello world\n```")
        assert result == "Hello world"

    def test_sanitize_strips_preamble(self, ai_config):
        """Strip common LLM preambles like 'Here is the corrected text:'."""
        service = self._make_service(ai_config)
        result = service._sanitize_response(
            "Here is the corrected text:\nHello world"
        )
        assert result == "Hello world"

    def test_sanitize_strips_thinking_tags(self, ai_config):
        """Strip <think>...</think> reasoning tags."""
        service = self._make_service(ai_config)
        result = service._sanitize_response(
            "<think>I need to fix capitalization</think>Hello world"
        )
        assert result == "Hello world"

    def test_sanitize_strips_trailing_commentary(self, ai_config):
        """Strip trailing LLM commentary after blank line."""
        service = self._make_service(ai_config)
        result = service._sanitize_response(
            "Hello world\n\nI hope this helps!"
        )
        assert result == "Hello world"

    def test_sanitize_preserves_clean_text(self, ai_config):
        """Clean text passes through unchanged."""
        service = self._make_service(ai_config)
        result = service._sanitize_response("Hello world")
        assert result == "Hello world"

    def test_sanitize_handles_multiple_artifacts(self, ai_config):
        """Strip combined fences + preamble."""
        service = self._make_service(ai_config)
        result = service._sanitize_response(
            "Here is the corrected text:\n```\nHello world\n```"
        )
        assert result == "Hello world"

    def test_sanitize_strips_various_preambles(self, ai_config):
        """Strip multiple preamble variants."""
        service = self._make_service(ai_config)

        preambles = [
            "Sure! Here you go:\n",
            "Corrected version:\n",
            "Here's the corrected text:\n",
            "Here is the fixed text:\n",
        ]
        for preamble in preambles:
            result = service._sanitize_response(f"{preamble}Hello world")
            assert result == "Hello world", f"Failed to strip preamble: {preamble!r}"


# ---------------------------------------------------------------------------
# Multi-turn help chat tests
# ---------------------------------------------------------------------------

class TestChatHelp:
    """Tests for AIService.chat_help() -- multi-turn help with history."""

    @pytest.mark.asyncio
    async def test_chat_help_constructs_correct_messages(self, ai_config, mock_provider):
        """chat_help sends system prompt + full history to provider."""
        mock_provider.chat = AsyncMock(return_value=ChatResult(status=ChatStatus.OK, text="Answer about commands."))
        service = AIService(ai_config)
        service._provider = mock_provider
        service._knowledge_base = "KB: commands use hotwords"

        history = [
            {"role": "user", "content": "How do commands work?"},
        ]
        await service.chat_help(history)

        call_args = mock_provider.chat.call_args
        messages = call_args[0][0]
        # First message is system prompt with KB
        assert messages[0]["role"] == "system"
        assert "KB: commands use hotwords" in messages[0]["content"]
        # History follows
        assert messages[1] == {"role": "user", "content": "How do commands work?"}

    @pytest.mark.asyncio
    async def test_chat_help_multi_turn_history(self, ai_config, mock_provider):
        """chat_help includes multi-turn conversation history."""
        mock_provider.chat = AsyncMock(return_value=ChatResult(status=ChatStatus.OK, text="Follow-up answer."))
        service = AIService(ai_config)
        service._provider = mock_provider
        service._knowledge_base = "KB content"

        history = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Follow-up"},
        ]
        await service.chat_help(history)

        messages = mock_provider.chat.call_args[0][0]
        assert len(messages) == 4  # system + 3 history turns
        assert messages[3]["role"] == "user"
        assert messages[3]["content"] == "Follow-up"


# ---------------------------------------------------------------------------
# _legacy_to_result contract tests (wh-ay6h.13.6)
# ---------------------------------------------------------------------------

class TestLegacyToResult:
    """Lock in the TypeError contract for _legacy_to_result.

    All providers now return ChatResult, so the bare-str/None/int branches are
    dead code.  These tests ensure a future accidental revert to loose-normalize
    behaviour is caught immediately.
    """

    def test_passes_chatresult_through_unchanged(self):
        """A ChatResult value is returned as-is (identity, not a copy)."""
        cr = ChatResult(status=ChatStatus.OK, text="ok")
        assert _legacy_to_result(cr) is cr

    def test_raises_on_empty_string(self):
        """An empty string raises TypeError."""
        with pytest.raises(TypeError):
            _legacy_to_result("")

    def test_raises_on_non_empty_string(self):
        """A non-empty string raises TypeError."""
        with pytest.raises(TypeError):
            _legacy_to_result("some text")

    def test_raises_on_none(self):
        """None raises TypeError."""
        with pytest.raises(TypeError):
            _legacy_to_result(None)

    def test_raises_on_int(self):
        """An integer raises TypeError."""
        with pytest.raises(TypeError):
            _legacy_to_result(42)

    @pytest.mark.asyncio
    async def test_chat_help_returns_none_without_kb(self, ai_config, mock_provider):
        """chat_help returns None when knowledge base is not loaded."""
        service = AIService(ai_config)
        service._provider = mock_provider
        service._knowledge_base = None

        result = await service.chat_help([{"role": "user", "content": "q"}])
        assert result is None

    @pytest.mark.asyncio
    async def test_chat_help_returns_none_on_failure(self, ai_config, mock_provider):
        """chat_help returns None when provider returns empty."""
        mock_provider.chat = AsyncMock(return_value=ChatResult(status=ChatStatus.EMPTY))
        service = AIService(ai_config)
        service._provider = mock_provider
        service._knowledge_base = "KB content"

        result = await service.chat_help([{"role": "user", "content": "q"}])
        assert result is None


# ---------------------------------------------------------------------------
# Thin-client start path + no-provider behavior
# ---------------------------------------------------------------------------

class TestEagerLoad:
    """Tests for the thin-client start() path and no-provider behavior.

    The thin-client start() builds the [ai.server] OpenAIProvider without an
    eager load and relies on the background probe for readiness. When no
    provider is present (AI off or post-failure) the inference entry points
    return a non-OK result rather than raising.
    """

    @pytest.mark.asyncio
    async def test_start_does_not_eager_load_thin_client(self, ai_config):
        """The thin-client start() does NOT eager-load -- it builds the
        [ai.server] OpenAIProvider and relies on the background probe for
        readiness (design 5.2)."""
        service = AIService(ai_config)

        with patch.object(service, "_load_knowledge_base", return_value="kb"):
            with patch.object(service, "_launch_background_probes"):
                await service.start()

        assert isinstance(service._provider, OpenAIProvider)
        # KB + help session still initialized.
        assert service._knowledge_base == "kb"
        assert service._help_session is not None

    @pytest.mark.asyncio
    async def test_start_help_session_initialized(self, ai_config, caplog):
        """start() always wires the knowledge base and help session, even on
        the AI-off short-circuit path."""
        config = MagicMock()
        config.get = MagicMock(side_effect=lambda key, default=None: {
            "ai.enabled": False,
            "ai.server.base_url": "http://localhost:8781/v1",
            "ai.models_directory": "D:/Models",
        }.get(key, default))
        service = AIService(config)

        with patch.object(service, "_load_knowledge_base", return_value="kb"):
            await service.start()

        # AI off (ai.enabled False) -> no provider, but KB + help session set.
        assert service._provider is None
        assert service._knowledge_base == "kb"
        assert service._help_session is not None

    @pytest.mark.asyncio
    async def test_chat_help_no_provider_returns_none(self, ai_config):
        """When _provider is None (post-failure), chat_help returns None."""
        service = AIService(ai_config)
        service._provider = None
        service._knowledge_base = "kb"

        result = await service.chat_help([{"role": "user", "content": "q"}])
        assert result is None

    @pytest.mark.asyncio
    async def test_fix_text_no_provider_returns_non_ok(self, ai_config):
        """When _provider is None (AI off), fix_text returns a non-OK ChatResult."""
        service = AIService(ai_config)
        service._provider = None

        result = await service.fix_text("some text")
        assert isinstance(result, ChatResult)
        assert result.ok is False


# ---------------------------------------------------------------------------
# Phase A thin-client coordinator API (design 5.2)
# ---------------------------------------------------------------------------

def _server_config(enabled=True, base_url="http://localhost:11434/v1",
                   kind="local", model="qwen3.5:9b", timeout_s=30,
                   server_enabled=True):
    """A ConfigService mock carrying the additive [ai.server] block.

    server_enabled maps to ai.server.enabled (distinct from ai.enabled).
    Set server_enabled=False to simulate "configured but turned off" (case b
    of state_manager._get_available_ai_providers).
    """
    config = MagicMock()
    values = {
        "ai.enabled": enabled,
        "ai.provider": "ollama",
        "ai.knowledge_base": "knowledge/wheelhouse_help.md",
        "ai.server.base_url": base_url,
        "ai.server.model": model,
        "ai.server.api_key": "",
        "ai.server.timeout_s": timeout_s,
        "ai.server.kind": kind,
        "ai.server.enabled": server_enabled,
        "ai.models_directory": "D:/Models",
        "ai.active_model": "",
        "ai.help.max_response_tokens": 800,
    }
    config.get = MagicMock(side_effect=lambda key, default=None: values.get(key, default))
    return config


class TestServerProviderApiKeyFromEnvOnly:
    """wh-ai-key-from-env: the AI API key must come only from the
    WHEELHOUSE_AI_API_KEY environment variable, never from config.toml (a
    git-tracked file where a stored secret is one commit from leaking).
    _build_server_provider must not hand any config-stored key to the
    provider, so a key placed in [ai.server].api_key is inert."""

    def _config_with_stored_key(self, stored_key):
        config = MagicMock()
        values = {
            "ai.enabled": True,
            "ai.provider": "ollama",
            "ai.knowledge_base": "knowledge/wheelhouse_help.md",
            "ai.server.base_url": "http://localhost:8781/v1",
            "ai.server.model": "m",
            "ai.server.api_key": stored_key,
            "ai.server.timeout_s": 60,
            "ai.server.kind": "local",
            "ai.server.enabled": True,
            "ai.models_directory": "D:/Models",
            "ai.active_model": "",
            "ai.help.max_response_tokens": 800,
        }
        config.get = MagicMock(
            side_effect=lambda key, default=None: values.get(key, default))
        return config

    def test_config_stored_key_is_ignored(self, monkeypatch):
        monkeypatch.delenv("WHEELHOUSE_AI_API_KEY", raising=False)
        service = AIService(self._config_with_stored_key("secret-in-config"))
        provider = service._build_server_provider()
        assert provider._api_key == "", (
            "a key stored in config.toml must never reach the provider"
        )

    def test_env_var_key_is_used(self, monkeypatch):
        monkeypatch.setenv("WHEELHOUSE_AI_API_KEY", "env-secret")
        service = AIService(self._config_with_stored_key("secret-in-config"))
        provider = service._build_server_provider()
        assert provider._api_key == "env-secret"


class TestThinClientCoordinator:
    """Tests for the Phase A coordinator additions on AIService."""

    def test_is_ready_returns_cached_value(self):
        service = AIService(_server_config())
        service._ready = True
        assert service.is_ready() is True
        service._ready = False
        assert service.is_ready() is False

    def test_is_ready_false_when_ai_off(self):
        """is_ready() is false when ai_off regardless of the cached flag."""
        service = AIService(_server_config(enabled=False))
        service._ready = True
        assert service.is_ready() is False

    def test_is_ready_false_when_server_enabled_false(self):
        """is_ready() is false when ai.server.enabled=false (wh-ay6h.6.1).

        Ensures the inference path matches GUI semantics: a server that is
        configured but explicitly turned off (state_manager case b) must not
        be reachable via is_ready().
        """
        service = AIService(_server_config(server_enabled=False))
        service._ready = True
        assert service.is_ready() is False

    def test_is_ready_false_when_no_base_url(self):
        service = AIService(_server_config(base_url=""))
        service._ready = True
        assert service.is_ready() is False

    @pytest.mark.asyncio
    async def test_is_processing_reflects_lock_state(self):
        service = AIService(_server_config())
        assert service.is_processing() is False
        await service._processing_lock.acquire()
        try:
            assert service.is_processing() is True
        finally:
            service._processing_lock.release()
        assert service.is_processing() is False

    @pytest.mark.asyncio
    async def test_set_model_updates_model_name(self):
        service = AIService(_server_config())
        await service.set_model("new-model:7b")
        assert service._model_name == "new-model:7b"

    @pytest.mark.asyncio
    async def test_set_model_acquires_processing_lock(self):
        """set_model waits for the processing lock before mutating the model."""
        service = AIService(_server_config())
        service._model_name = "old-model"

        await service._processing_lock.acquire()

        task = asyncio.create_task(service.set_model("new-model"))
        await asyncio.sleep(0.05)

        # Blocked on the lock: the model is not yet changed.
        assert service._model_name == "old-model"

        service._processing_lock.release()
        await task
        assert service._model_name == "new-model"

    @pytest.mark.asyncio
    async def test_set_model_resets_help_session(self):
        service = AIService(_server_config())
        service._help_session = MagicMock()
        await service.set_model("another")
        service._help_session.reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_model_updates_provider_model(self):
        service = AIService(_server_config())
        provider = MagicMock()
        provider._model = "old"
        service._provider = provider
        await service.set_model("brand-new")
        assert provider._model == "brand-new"

    def test_cached_models_returns_last_refresh(self):
        service = AIService(_server_config())
        service._models = ["a", "b"]
        assert service.cached_models() == ["a", "b"]

    def test_cached_models_returns_copy(self):
        service = AIService(_server_config())
        service._models = ["a"]
        out = service.cached_models()
        out.append("mutated")
        assert service._models == ["a"]

    @pytest.mark.asyncio
    async def test_refresh_models_updates_cache_and_ready(self):
        service = AIService(_server_config())
        provider = MagicMock()
        provider.list_models = AsyncMock(return_value=["m1", "m2"])
        service._provider = provider
        service._ready = False

        await service.refresh_models()

        assert service.cached_models() == ["m1", "m2"]
        assert service._last_refresh_ok is True
        # A successful refresh also corroborates readiness (design 5.2).
        assert service._ready is True
        assert service.is_ready() is True
        # The non-empty list IS the reachability evidence; no extra probe.
        provider.is_available.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_models_empty_list_down_server_not_ready(self):
        """An empty list from an UNREACHABLE server must not mark ready.

        list_models() returns [] for any failure AND for a reachable server
        with no models, so an empty list is ambiguous on its own. The refresh
        corroborates with the real is_available() probe; a failed probe means
        the readiness flag goes False instead of True (reviewer_0 finding
        wh-ay6h.2.6: the unconditional ready-True marked a DOWN server ready
        on every 60 s refresh tick, defeating the is_ready() gate).
        """
        service = AIService(_server_config())
        provider = MagicMock()
        provider.list_models = AsyncMock(return_value=[])
        provider.is_available = AsyncMock(return_value=False)
        service._provider = provider
        service._ready = True
        service._models = ["stale-from-last-good-fetch"]

        await service.refresh_models()

        assert service._ready is False
        assert service.is_ready() is False
        assert service._last_refresh_ok is False
        # A transient outage must not wipe the last good cache.
        assert service.cached_models() == ["stale-from-last-good-fetch"]

    @pytest.mark.asyncio
    async def test_refresh_models_empty_list_reachable_server_stays_ready(self):
        """An empty list from a REACHABLE server keeps the service ready."""
        service = AIService(_server_config())
        provider = MagicMock()
        provider.list_models = AsyncMock(return_value=[])
        provider.is_available = AsyncMock(return_value=True)
        service._provider = provider
        service._ready = False

        await service.refresh_models()

        assert service._ready is True
        assert service._last_refresh_ok is True
        assert service.cached_models() == []

    @pytest.mark.asyncio
    async def test_refresh_models_no_provider_noop(self):
        service = AIService(_server_config())
        service._provider = None
        await service.refresh_models()  # must not raise
        assert service.cached_models() == []

    @pytest.mark.asyncio
    async def test_refresh_loop_per_iteration_try_except_isolation(self):
        """A refresh that raises is caught per-iteration; the loop continues."""
        service = AIService(_server_config())
        provider = MagicMock()
        # First refresh raises, second succeeds. The loop must survive the
        # first and reach the second.
        provider.list_models = AsyncMock(
            side_effect=[RuntimeError("transient"), ["recovered"]]
        )
        service._provider = provider

        # Drive two iterations with a near-zero interval, then cancel.
        task = asyncio.create_task(service._refresh_loop(interval=0))
        # Let both iterations run.
        for _ in range(20):
            await asyncio.sleep(0)
            if service.cached_models() == ["recovered"]:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert service.cached_models() == ["recovered"]

    @pytest.mark.asyncio
    async def test_recheck_ready_updates_and_returns_true(self):
        service = AIService(_server_config())
        provider = MagicMock()
        provider.is_available = AsyncMock(return_value=True)
        service._provider = provider
        service._ready = False

        fresh = await service.recheck_ready()

        assert fresh is True
        assert service._ready is True
        assert service.is_ready() is True

    @pytest.mark.asyncio
    async def test_recheck_ready_updates_and_returns_false(self):
        service = AIService(_server_config())
        provider = MagicMock()
        provider.is_available = AsyncMock(return_value=False)
        service._provider = provider
        service._ready = True

        fresh = await service.recheck_ready()

        assert fresh is False
        assert service._ready is False

    @pytest.mark.asyncio
    async def test_recheck_ready_false_when_ai_off(self):
        service = AIService(_server_config(enabled=False))
        provider = MagicMock()
        provider.is_available = AsyncMock(return_value=True)
        service._provider = provider
        fresh = await service.recheck_ready()
        assert fresh is False

    @pytest.mark.asyncio
    async def test_recheck_ready_false_on_probe_exception(self):
        service = AIService(_server_config())
        provider = MagicMock()
        provider.is_available = AsyncMock(side_effect=RuntimeError("boom"))
        service._provider = provider
        service._ready = True
        fresh = await service.recheck_ready()
        assert fresh is False

    @pytest.mark.asyncio
    async def test_recheck_ready_false_when_is_available_raises_timeout(self):
        """recheck_ready() maps asyncio.TimeoutError from is_available() to False.

        is_available() bounds each probe internally with _PROBE_TIMEOUT_S; no
        outer wait_for is needed in recheck_ready() (finding wh-ay6h.6.2 --
        the outer 5 s wait_for was removed because it cut off the /api/tags
        fallback when the primary probe consumed its full window). This test
        confirms that a TimeoutError escaping is_available() is caught by the
        broad except clause and maps to fresh=False.
        """
        service = AIService(_server_config())
        provider = MagicMock()
        provider.is_available = AsyncMock(side_effect=asyncio.TimeoutError())
        service._provider = provider
        service._ready = True

        fresh = await service.recheck_ready()

        assert fresh is False
        assert service._ready is False

    @pytest.mark.asyncio
    async def test_refresh_models_no_is_available_attribute_marks_not_ready(self):
        """When the provider has list_models() but NOT is_available(), an empty
        list from list_models() must leave _ready=False (wh-ay6h.4.4).

        The hasattr(self._provider, "is_available") guard in refresh_models()
        skips the real probe; reachable stays False, so the stale model cache is
        preserved and _ready is set to False. Previous tests all used MagicMock()
        which auto-creates any attribute lookup, so this branch was never reached.
        """
        service = AIService(_server_config())

        # Use a plain object that truly lacks is_available.
        class MinimalProvider:
            async def list_models(self):
                return []

        provider = MinimalProvider()
        service._provider = provider
        service._ready = True
        service._models = ["stale-model"]

        await service.refresh_models()

        assert service._ready is False
        assert service._last_refresh_ok is False
        # Stale cache preserved -- a transient outage must not wipe last good data.
        assert service.cached_models() == ["stale-model"]

    @pytest.mark.asyncio
    async def test_start_launches_background_probe_not_awaited(self):
        """start() schedules the first reachability probe via create_task and
        returns without awaiting that probe to completion.

        recheck_ready (the background probe) is patched so we can observe that
        start() returns before it has finished -- a slow probe must not delay
        startup. provider.is_available is NOT called eagerly during start()
        (wh-ay6h.2.4); availability comes solely from the background probe.
        """
        service = AIService(_server_config(kind="local"))

        provider = MagicMock()
        provider.load = AsyncMock()
        provider.list_models = AsyncMock(return_value=[])

        probe_done = False

        async def slow_recheck():
            nonlocal probe_done
            await asyncio.sleep(0.2)
            probe_done = True
            service._ready = True
            return True

        with patch.object(service, "_build_server_provider", return_value=provider):
            with patch.object(service, "_load_knowledge_base", return_value="kb"):
                with patch.object(service, "recheck_ready", side_effect=slow_recheck):
                    await asyncio.wait_for(service.start(), timeout=2.0)
                    # start() returned BEFORE the slow background probe finished.
                    assert probe_done is False
                    assert service._bg_tasks
                    # Let the background probe run to completion.
                    await asyncio.sleep(0.3)
                    assert probe_done is True

        for t in service._bg_tasks:
            t.cancel()

    @pytest.mark.asyncio
    async def test_start_does_not_await_provider_is_available(self):
        """start() must NOT call provider.is_available() synchronously (wh-ay6h.2.4).

        A provider whose is_available() would block for 10 s must not delay
        start(). We wire a slow is_available() and assert start() completes
        well inside that latency.
        """
        service = AIService(_server_config(kind="local"))

        provider = MagicMock()
        provider.load = AsyncMock()
        provider.list_models = AsyncMock(return_value=[])

        async def slow_is_available():
            await asyncio.sleep(5.0)  # simulates a black-holed probe
            return False

        provider.is_available = slow_is_available

        with patch.object(service, "_build_server_provider", return_value=provider):
            with patch.object(service, "_load_knowledge_base", return_value="kb"):
                with patch.object(service, "_launch_background_probes"):
                    # Must complete in well under 5 s if is_available is not awaited.
                    await asyncio.wait_for(service.start(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_start_no_background_probe_when_ai_off(self):
        """When ai_off, start() launches no background probes."""
        service = AIService(_server_config(base_url=""))
        provider = MagicMock()
        provider.load = AsyncMock()
        provider.is_available = AsyncMock(return_value=True)
        with patch.object(service, "_build_server_provider", return_value=provider):
            with patch.object(service, "_load_knowledge_base", return_value="kb"):
                await service.start()
        assert service._bg_tasks == []

    @pytest.mark.asyncio
    async def test_start_no_background_probe_when_server_disabled(self):
        """When ai.server.enabled=false, start() must not launch probes (wh-ay6h.6.1).

        The server is configured (base_url present) but the user turned it off.
        The inference path must treat this identically to ai_off: no provider
        built, no background probes, is_ready()=False.
        """
        service = AIService(_server_config(server_enabled=False))
        provider = MagicMock()
        provider.load = AsyncMock()
        provider.is_available = AsyncMock(return_value=True)
        with patch.object(service, "_build_server_provider", return_value=provider):
            with patch.object(service, "_load_knowledge_base", return_value="kb"):
                await service.start()
        assert service._bg_tasks == []
        assert service.is_ready() is False

    @pytest.mark.asyncio
    async def test_start_cloud_kind_starts_no_refresh_loop(self):
        """For kind='cloud' the model-list refresh loop is not started, but a
        readiness probe + loop are (3 tasks for local, 2 for cloud)."""
        service = AIService(_server_config(kind="cloud"))
        provider = MagicMock()
        provider.load = AsyncMock()
        provider.is_available = AsyncMock(return_value=True)
        provider.list_models = AsyncMock(return_value=[])
        with patch.object(service, "_build_server_provider", return_value=provider):
            with patch.object(service, "_load_knowledge_base", return_value="kb"):
                await service.start()
        # cloud: first probe + readiness loop only (no refresh loop).
        assert len(service._bg_tasks) == 2
        for t in service._bg_tasks:
            t.cancel()

    @pytest.mark.asyncio
    async def test_refresh_models_cloud_kind_noop(self):
        """refresh_models() must be a no-op for cloud kind (design 5.2 local-only
        contract). wh-ay6h.7.1: OpenAIProvider implements list_models so the
        hasattr guard does not protect cloud; a kind guard is required."""
        service = AIService(_server_config(kind="cloud"))
        provider = MagicMock()
        provider.list_models = AsyncMock(return_value=["some-cloud-model"])
        service._provider = provider
        await service.refresh_models()
        provider.list_models.assert_not_called()


# ---------------------------------------------------------------------------
# Coordinator return-type contract (design 5.1a Phase B)
# ---------------------------------------------------------------------------

class TestCoordinatorReturnTypes:
    """fix_text / ask_help / help_ask return ChatResult; chat_help keeps its
    str return so HelpChatSession.ask (deadlock decision 26) is unchanged --
    the structured result, including the truncation signal, flows to help_ask
    via the stashed _last_help_result."""

    @pytest.mark.asyncio
    async def test_fix_text_returns_chat_result(self, ai_config, mock_provider):
        mock_provider.chat = AsyncMock(return_value=ChatResult(status=ChatStatus.OK, text="ok"))
        service = AIService(ai_config)
        service._provider = mock_provider
        result = await service.fix_text("hello")
        assert isinstance(result, ChatResult)

    @pytest.mark.asyncio
    async def test_ask_help_returns_chat_result(self, ai_config, mock_provider):
        mock_provider.chat = AsyncMock(return_value=ChatResult(status=ChatStatus.OK, text="ok"))
        service = AIService(ai_config)
        service._provider = mock_provider
        service._knowledge_base = "kb"
        result = await service.ask_help("q")
        assert isinstance(result, ChatResult)

    @pytest.mark.asyncio
    async def test_help_ask_returns_chat_result(self, ai_config, mock_provider):
        mock_provider.chat = AsyncMock(
            return_value=ChatResult(status=ChatStatus.OK, text="answer")
        )
        service = AIService(ai_config)
        service._provider = mock_provider
        service._knowledge_base = "kb"
        service._help_session = HelpChatSession(service)
        result = await service.help_ask("q")
        assert isinstance(result, ChatResult)
        assert result.ok is True
        assert result.text == "answer"

    @pytest.mark.asyncio
    async def test_help_ask_surfaces_truncation_signal(self, ai_config, mock_provider):
        """An OK answer with finish_reason == 'length' makes help_ask's
        ChatResult report truncated == True (finding 2.4)."""
        mock_provider.chat = AsyncMock(
            return_value=ChatResult(
                status=ChatStatus.OK, text="cut off", finish_reason="length"
            )
        )
        service = AIService(ai_config)
        service._provider = mock_provider
        service._knowledge_base = "kb"
        service._help_session = HelpChatSession(service)
        result = await service.help_ask("q")
        assert result.ok is True
        assert result.truncated is True

    @pytest.mark.asyncio
    async def test_help_ask_non_ok_surfaces_status(self, ai_config, mock_provider):
        """When chat_help fails (provider transport error), help_ask returns a
        non-OK ChatResult carrying the failure status."""
        mock_provider.chat = AsyncMock(
            return_value=ChatResult(status=ChatStatus.TRANSPORT_ERROR)
        )
        service = AIService(ai_config)
        service._provider = mock_provider
        service._knowledge_base = "kb"
        service._help_session = HelpChatSession(service)
        result = await service.help_ask("q")
        assert result.ok is False
        assert result.status is ChatStatus.TRANSPORT_ERROR
