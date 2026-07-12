"""Tests for AI action functions in speech/actions.py.

Tests fix_text_ai, cancel_fix, wheelhouse_help, wheelhouse_help_new,
and the _get_ai_service helper. These are the voice command handlers
that bridge the speech pipeline to AIService.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from ai.providers.openai_compat import ChatResult, ChatStatus


def _result(text="corrected text", *, ok=True, finish_reason=None,
            status=None):
    """Build a ChatResult for fix_text mocks (the new coordinator return)."""
    if ok:
        return ChatResult(status=ChatStatus.OK, text=text, finish_reason=finish_reason)
    return ChatResult(status=status or ChatStatus.TRANSPORT_ERROR)


def _make_actions(ai_service=None, provider_set=True):
    """Create ActionFunctions with mocked speech_handler chain.

    Builds the chain: speech_handler -> logic_controller -> service_manager -> ai_service.

    Args:
        ai_service: Mock AIService (or None to simulate missing).
        provider_set: Whether ai_service._provider is set (True) or None (False).
    """
    from speech.actions import ActionFunctions

    speech_handler = MagicMock()
    actions = ActionFunctions(speech_handler)

    # Build the access chain
    if ai_service is not None:
        if not provider_set:
            ai_service._provider = None
        sm = MagicMock()
        sm.ai_service = ai_service
        lc = MagicMock()
        lc.service_manager = sm
        speech_handler.logic_controller = lc
    else:
        # No ai_service at all
        speech_handler.logic_controller = None

    return actions


def _make_ai_service(*, fix_response="corrected text", help_response="help answer",
                     locked=False, ready=True):
    """Create a mock AIService with configurable behavior.

    fix_text now returns a ChatResult (design 5.1a). Readiness/processing are
    public predicates (is_ready / is_processing) -- the action layer no longer
    reaches into _processing_lock.locked() (finding 1.9 / s7).
    """
    ai = MagicMock()
    ai.fix_text = AsyncMock(return_value=_result(fix_response))
    ai.ask_help = AsyncMock(return_value=help_response)
    ai.new_help_conversation = AsyncMock(return_value=help_response)
    ai.speak = AsyncMock()
    ai.speak_brief = AsyncMock()
    ai.cancel_requested = False
    ai._provider = MagicMock()  # Provider is set

    # Public readiness / processing predicates and the recheck probe.
    ai.is_ready = MagicMock(return_value=ready)
    ai.recheck_ready = AsyncMock(return_value=ready)

    # Processing lock + is_processing() bound to its real locked() state so the
    # `async with ai._processing_lock` block in fix_text_ai still serializes.
    lock = asyncio.Lock()
    ai._processing_lock = lock
    ai.is_processing = MagicMock(side_effect=lambda: lock.locked())
    if locked:
        lock._locked = True

    return ai


# =========================================================================
# _get_ai_service
# =========================================================================

class TestGetAIService:
    """Tests for the _get_ai_service helper -- existence-only (finding 1.9).

    The helper no longer gates on provider readiness; it returns the service
    whenever the chain resolves. Readiness is checked at the action level via
    ai.is_ready().
    """

    def test_returns_ai_service(self):
        """Returns AIService when full chain is available."""
        ai = _make_ai_service()
        actions = _make_actions(ai_service=ai)

        result = actions._get_ai_service()

        assert result is ai

    def test_returns_none_when_no_logic_controller(self):
        """Returns None when speech_handler has no logic_controller."""
        actions = _make_actions(ai_service=None)

        result = actions._get_ai_service()

        assert result is None

    def test_returns_service_even_when_no_provider(self):
        """Existence-only: returns the service even when _provider is None.

        The old is_ready gate (``if svc and not svc._provider: return None``)
        was removed in Phase B (finding 1.9). A provider-less service is still
        returned; readiness is decided by ai.is_ready() in the action.
        """
        ai = _make_ai_service()
        actions = _make_actions(ai_service=ai, provider_set=False)

        result = actions._get_ai_service()

        assert result is ai

    def test_returns_none_when_no_service_manager(self):
        """Returns None when logic_controller has no service_manager."""
        from speech.actions import ActionFunctions
        speech_handler = MagicMock()
        actions = ActionFunctions(speech_handler)
        lc = MagicMock(spec=[])  # spec=[] means no attributes
        speech_handler.logic_controller = lc

        result = actions._get_ai_service()

        assert result is None


# =========================================================================
# fix_text_ai
# =========================================================================

class TestFixTextAI:
    """Tests for the fix_text_ai action function."""

    @pytest.mark.asyncio
    async def test_full_flow(self):
        """Happy path: capture -> correct -> replace."""
        ai = _make_ai_service(fix_response="Corrected text")
        actions = _make_actions(ai_service=ai)

        # Mock IPC: capture returns text, replace succeeds
        app = actions.speech_handler.app
        app.send_request = AsyncMock(side_effect=[
            {"text": "original text"},  # capture
            {"success": True},           # replace
        ])

        result = await actions.fix_text_ai()

        assert result is None
        ai.fix_text.assert_awaited_once_with("original text")
        # Should have sent replace with corrected text
        assert app.send_request.call_count == 2
        replace_call = app.send_request.call_args_list[1]
        assert replace_call[0] == ("replace_selected_text",)
        assert replace_call[1] == {"params": {"text": "Corrected text"}}

    @pytest.mark.asyncio
    async def test_no_text_captured(self):
        """Speaks 'No text to correct' when capture returns empty."""
        ai = _make_ai_service()
        actions = _make_actions(ai_service=ai)

        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": ""})

        await actions.fix_text_ai()

        ai.speak.assert_awaited_once()
        assert "no text" in ai.speak.call_args[0][0].lower()
        ai.fix_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_ai_service(self):
        """Returns None silently when AIService not available."""
        actions = _make_actions(ai_service=None)

        result = await actions.fix_text_ai()

        assert result is None

    @pytest.mark.asyncio
    async def test_concurrent_rejection(self):
        """Speaks 'Already processing' when lock is held."""
        ai = _make_ai_service()
        actions = _make_actions(ai_service=ai)

        # Hold the lock
        await ai._processing_lock.acquire()
        try:
            await actions.fix_text_ai()
        finally:
            ai._processing_lock.release()

        ai.speak.assert_awaited_once()
        assert "already processing" in ai.speak.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_correction_failed(self):
        """Speaks a failure notice when fix_text returns a non-OK ChatResult.

        recheck_ready() returns True here (server is reachable) so the wording
        is the generic 'Correction failed' rather than 'isn't responding'.
        """
        ai = _make_ai_service()
        ai.fix_text = AsyncMock(return_value=_result(ok=False))
        ai.recheck_ready = AsyncMock(return_value=True)
        actions = _make_actions(ai_service=ai)

        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "some text"})

        await actions.fix_text_ai()

        # Should speak failure, not try to replace
        assert any("failed" in str(c).lower() for c in ai.speak.call_args_list)

    @pytest.mark.asyncio
    async def test_model_not_found_speaks_distinct_notice(self):
        """MODEL_NOT_FOUND gets its own wording naming the model problem
        (wh-75m), not the generic 'Correction failed'. The server DID
        respond (404 on the model), so no reachability re-probe runs."""
        ai = _make_ai_service()
        ai.fix_text = AsyncMock(
            return_value=_result(ok=False, status=ChatStatus.MODEL_NOT_FOUND)
        )
        actions = _make_actions(ai_service=ai)

        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "some text"})

        await actions.fix_text_ai()

        spoken = " ".join(str(c) for c in ai.speak.call_args_list).lower()
        assert "model" in spoken
        assert "correction failed" not in spoken
        ai.recheck_ready.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reasoning_exhausted_speaks_distinct_notice(self):
        """EMPTY + finish_reason == 'length' is the reasoning-model
        signature: HTTP 200, no content, whole budget spent on hidden
        thinking (wh-ai-reasoning-model-empty). Gets its own wording and
        no reachability re-probe (the server DID respond)."""
        ai = _make_ai_service()
        ai.fix_text = AsyncMock(
            return_value=ChatResult(
                status=ChatStatus.EMPTY, finish_reason="length"
            )
        )
        actions = _make_actions(ai_service=ai)

        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "some text"})

        await actions.fix_text_ai()

        spoken = " ".join(str(c) for c in ai.speak.call_args_list).lower()
        assert "reasoning" in spoken
        assert "correction failed" not in spoken
        ai.recheck_ready.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plain_empty_result_keeps_generic_wording(self):
        """EMPTY WITHOUT the length signal (e.g. model returned no content
        but did not exhaust the budget) stays on the generic failure path
        -- the reasoning wording must not fire for ordinary empties."""
        ai = _make_ai_service()
        ai.fix_text = AsyncMock(
            return_value=ChatResult(status=ChatStatus.EMPTY)
        )
        ai.recheck_ready = AsyncMock(return_value=True)
        actions = _make_actions(ai_service=ai)

        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "some text"})

        await actions.fix_text_ai()

        spoken = " ".join(str(c) for c in ai.speak.call_args_list).lower()
        assert "reasoning" not in spoken
        assert "failed" in spoken

    @pytest.mark.asyncio
    async def test_correction_failed_server_down_says_not_responding(self):
        """On a non-OK result with recheck_ready() False, speaks the
        'isn't responding' wording (s7 / decision 27)."""
        ai = _make_ai_service()
        ai.fix_text = AsyncMock(return_value=_result(ok=False))
        ai.recheck_ready = AsyncMock(return_value=False)
        actions = _make_actions(ai_service=ai)

        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "some text"})

        await actions.fix_text_ai()

        ai.recheck_ready.assert_awaited_once()
        assert any(
            "isn't responding" in str(c) or "not responding" in str(c).lower()
            for c in ai.speak.call_args_list
        )

    @pytest.mark.asyncio
    async def test_not_ready_speaks_graceful_notice(self):
        """When is_ready() is False, fix_text_ai speaks a graceful-off notice
        (today it was silent) and does not attempt correction (s7)."""
        ai = _make_ai_service(ready=False)
        actions = _make_actions(ai_service=ai)

        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "some text"})

        await actions.fix_text_ai()

        ai.speak.assert_awaited()
        assert any("not available" in str(c).lower() for c in ai.speak.call_args_list)
        # No capture/correction attempted.
        ai.fix_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_changes_needed(self):
        """Speaks 'No changes needed' when corrected == original."""
        ai = _make_ai_service(fix_response="same text")
        actions = _make_actions(ai_service=ai)

        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "same text"})

        await actions.fix_text_ai()

        # Should speak "no changes" and NOT send replace
        assert any("no changes" in str(c).lower() for c in ai.speak_brief.call_args_list)
        assert app.send_request.call_count == 1  # Only capture, no replace

    @pytest.mark.asyncio
    async def test_cancelled_after_correction(self):
        """Speaks 'Cancelled' when fix_text returns CANCELLED status.

        The real fix_text returns ChatStatus.CANCELLED (not OK) when
        cancel_requested is set -- it clears the flag itself and returns
        CANCELLED before the caller ever sees cancel_requested=True.
        The mock must reflect that semantics so the test exercises the branch
        the user actually hits (finding wh-ay6h.6.5 / wh-ay6h.6.4).
        """
        ai = _make_ai_service(fix_response="Corrected text")
        actions = _make_actions(ai_service=ai)

        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "original text"})

        # Real fix_text clears cancel_requested and returns CANCELLED.
        async def fix_and_cancel(text):
            return ChatResult(status=ChatStatus.CANCELLED)

        ai.fix_text = AsyncMock(side_effect=fix_and_cancel)

        await actions.fix_text_ai()

        # Should speak cancelled, NOT send replace
        assert any("cancel" in str(c).lower() for c in ai.speak_brief.call_args_list)
        assert app.send_request.call_count == 1  # Only capture

    @pytest.mark.asyncio
    async def test_large_text_warning(self):
        """Speaks the word-count notice for text over 200 words.

        The old seconds-based time estimate was dropped in Phase B -- the thin
        client has no local-tier basis for a seconds estimate (design s4). The
        word-count notice is retained; the assertion below confirms no
        'second' wording is spoken.
        """
        ai = _make_ai_service(fix_response="corrected")
        actions = _make_actions(ai_service=ai)

        large_text = " ".join(["word"] * 250)
        app = actions.speech_handler.app
        app.send_request = AsyncMock(side_effect=[
            {"text": large_text},
            {"success": True},
        ])

        await actions.fix_text_ai()

        # Word-count notice spoken; no time-estimate call or wording.
        speak_calls = [str(c) for c in ai.speak.call_args_list]
        assert any("250" in c or "word" in c for c in speak_calls)
        assert not any("second" in c.lower() for c in speak_calls)


# =========================================================================
# cancel_fix
# =========================================================================

class TestCancelFix:
    """Tests for the cancel_fix action function."""

    @pytest.mark.asyncio
    async def test_sets_cancel_flag(self):
        """Sets cancel_requested when lock is held."""
        ai = _make_ai_service()
        actions = _make_actions(ai_service=ai)

        # Hold the lock (simulates fix_text_ai in progress)
        await ai._processing_lock.acquire()
        try:
            await actions.cancel_fix()
        finally:
            ai._processing_lock.release()

        assert ai.cancel_requested is True

    @pytest.mark.asyncio
    async def test_noop_when_not_processing(self):
        """Does nothing when lock is not held."""
        ai = _make_ai_service()
        actions = _make_actions(ai_service=ai)

        await actions.cancel_fix()

        assert ai.cancel_requested is False

    @pytest.mark.asyncio
    async def test_noop_when_no_ai_service(self):
        """Returns None silently when AIService not available."""
        actions = _make_actions(ai_service=None)

        result = await actions.cancel_fix()

        assert result is None


# =========================================================================
# wheelhouse_help
# =========================================================================

class TestWheelhouseHelp:
    """Tests for the wheelhouse_help action -- opens help chat window."""

    @pytest.mark.asyncio
    async def test_sends_show_help_chat_to_gui(self):
        """wheelhouse_help sends show_help_chat action to GUI."""
        ai = _make_ai_service()
        actions = _make_actions(ai_service=ai)

        # Mock the _send_gui_action method
        actions._send_gui_action = MagicMock()

        await actions.wheelhouse_help()

        actions._send_gui_action.assert_called_once_with(
            {"action": "show_help_chat"}
        )

    @pytest.mark.asyncio
    async def test_sends_question_when_provided(self):
        """wheelhouse_help with question includes it in the payload."""
        ai = _make_ai_service()
        actions = _make_actions(ai_service=ai)
        actions._send_gui_action = MagicMock()

        await actions.wheelhouse_help("how do I move a window")

        actions._send_gui_action.assert_called_once_with(
            {"action": "show_help_chat", "question": "how do I move a window"}
        )

    @pytest.mark.asyncio
    async def test_no_question_opens_empty(self):
        """wheelhouse_help without question opens chat with no question."""
        ai = _make_ai_service()
        actions = _make_actions(ai_service=ai)
        actions._send_gui_action = MagicMock()

        await actions.wheelhouse_help("")

        actions._send_gui_action.assert_called_once_with(
            {"action": "show_help_chat"}
        )


class TestWheelhouseHelpOnline:
    """Tests for the wheelhouse_help_online action -- opens browser."""

    @pytest.mark.asyncio
    async def test_opens_browser_with_gem_url(self):
        """wheelhouse_help_online opens browser with configured gem_url."""
        ai = _make_ai_service()
        actions = _make_actions(ai_service=ai)

        # Wire config_service with key-based lookup
        lc = actions.speech_handler.logic_controller
        config = MagicMock()
        config.get = MagicMock(side_effect=lambda key, default="": {
            "ai.help.gem_url": "https://example.com/gem",
        }.get(key, default))
        lc.config_service = config

        import webbrowser
        with patch.object(webbrowser, "open") as mock_open:
            await actions.wheelhouse_help_online()
            mock_open.assert_called_once_with("https://example.com/gem")

    @pytest.mark.asyncio
    async def test_speaks_when_gem_url_not_configured(self):
        """wheelhouse_help_online speaks error when gem_url is empty."""
        ai = _make_ai_service()
        actions = _make_actions(ai_service=ai)

        lc = actions.speech_handler.logic_controller
        config = MagicMock()
        config.get = MagicMock(side_effect=lambda key, default="": {
            "ai.help.gem_url": "",
        }.get(key, default))
        lc.config_service = config

        await actions.wheelhouse_help_online()

        ai.speak_brief.assert_awaited_once()
        assert "not configured" in ai.speak_brief.call_args[0][0].lower()


# =========================================================================
# Registration
# =========================================================================

class TestRegistration:
    """Test that AI action functions are registered."""

    def test_functions_registered(self):
        """All AI action functions are in the function registry."""
        from speech.actions import ActionFunctions
        actions = ActionFunctions(MagicMock())
        funcs = actions.get_functions()

        assert "fix_text_ai" in funcs
        assert "cancel_fix" in funcs
        assert "wheelhouse_help" in funcs
        assert "wheelhouse_help_online" in funcs
        assert "wheelhouse_help_new" not in funcs  # Removed
