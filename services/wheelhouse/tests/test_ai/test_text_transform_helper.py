"""Tests for the shared capture-send-paste helper (wh-rewrite-extract-shared-body).

fix_text_ai used to hold the whole sequence -- readiness, the processing lock,
capturing the selection, sending it, checking for cancellation, pasting the
answer back. The rewriting commands need the same sequence with a different
request and different spoken wording, so the sequence moved into
_run_ai_text_transform and fix_text_ai became one short caller.

These tests drive the helper directly with a request that is NOT fix_text and
with wording that is not the correction wording. That is the property the
extraction exists for: nothing in the sequence is specific to correcting text.
The correction behaviour itself is still covered by TestFixTextAI in
test_actions.py, which was not touched.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai.providers.openai_compat import ChatResult, ChatStatus


def _ai(ready=True):
    ai = MagicMock()
    ai.speak = AsyncMock()
    ai.speak_brief = AsyncMock()
    ai.cancel_requested = False
    ai.is_ready = MagicMock(return_value=ready)
    ai.recheck_ready = AsyncMock(return_value=ready)
    lock = asyncio.Lock()
    ai._processing_lock = lock
    ai.is_processing = MagicMock(side_effect=lock.locked)
    return ai


def _actions(ai):
    from speech.actions import ActionFunctions

    speech_handler = MagicMock()
    sm = MagicMock()
    sm.ai_service = ai
    lc = MagicMock()
    lc.service_manager = sm
    speech_handler.logic_controller = lc
    return ActionFunctions(speech_handler)


def _run(actions, send, **overrides):
    """Call the helper with pirate-flavoured wording, so nothing can pass by
    accidentally matching the correction wording."""
    kwargs = dict(
        send=send,
        working_word="Rewriting",
        no_text_message="No text to rewrite.",
        failed_message="Rewrite failed. Original text preserved.",
    )
    kwargs.update(overrides)
    return actions._run_ai_text_transform(**kwargs)


def _ok(text):
    return ChatResult(status=ChatStatus.OK, text=text)


class TestTheSequenceIsNotSpecificToCorrecting:

    @pytest.mark.asyncio
    async def test_the_captured_text_goes_to_the_given_request(self):
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(side_effect=[
            {"text": "original text"},
            {"success": True},
        ])
        send = AsyncMock(return_value=_ok("rewritten text"))

        await _run(actions, send)

        send.assert_awaited_once_with(ai, "original text")

    @pytest.mark.asyncio
    async def test_the_answer_is_pasted_back(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        app.send_request = AsyncMock(side_effect=[
            {"text": "original text"},
            {"success": True},
        ])

        await _run(actions, AsyncMock(return_value=_ok("rewritten text")))

        paste = app.send_request.call_args_list[1]
        assert paste[0] == ("replace_selected_text",)
        assert paste[1] == {"params": {"text": "rewritten text"}}

    @pytest.mark.asyncio
    async def test_the_given_no_text_wording_is_spoken(self):
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(return_value={"text": ""})
        send = AsyncMock()

        await _run(actions, send)

        ai.speak.assert_awaited_once_with("No text to rewrite.")
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_given_working_word_is_spoken_and_shown(self):
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(side_effect=[
            {"text": "original text"},
            {"success": True},
        ])
        shown = []
        actions._send_gui_action = lambda action_dict: shown.append(action_dict)

        await _run(actions, AsyncMock(return_value=_ok("rewritten text")))

        ai.speak_brief.assert_any_await("Rewriting.")
        assert {"action": "show_working", "message": "Rewriting..."} in shown
        assert {"action": "hide_working"} in shown

    @pytest.mark.asyncio
    async def test_the_given_failure_wording_is_spoken(self):
        """The server answered, it just did not do the job."""
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(
            return_value={"text": "original text"}
        )
        send = AsyncMock(return_value=ChatResult(status=ChatStatus.HTTP_ERROR))

        await _run(actions, send)

        ai.speak.assert_awaited_once_with(
            "Rewrite failed. Original text preserved."
        )


class TestTheProtectionsSurvivedTheExtraction:

    @pytest.mark.asyncio
    async def test_an_unreachable_server_is_named_as_such(self):
        ai = _ai()
        ai.recheck_ready = AsyncMock(return_value=False)
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(
            return_value={"text": "original text"}
        )

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.TRANSPORT_ERROR)))

        assert "isn't responding" in ai.speak.call_args[0][0]

    @pytest.mark.asyncio
    async def test_a_cancelled_result_pastes_nothing(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "original text"})

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.CANCELLED)))

        assert app.send_request.call_count == 1
        ai.speak_brief.assert_any_await("Cancelled.")

    @pytest.mark.asyncio
    async def test_a_cancel_that_lands_while_waiting_pastes_nothing(self):
        """The race the flag exists for: the answer arrives, then the user
        says cancel before the paste."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "original text"})

        async def answer_then_cancel(service, text):
            ai.cancel_requested = True
            return _ok("rewritten text")

        await _run(actions, answer_then_cancel)

        assert app.send_request.call_count == 1
        assert ai.cancel_requested is False

    @pytest.mark.asyncio
    async def test_a_second_request_while_one_is_running_is_refused(self):
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(
            return_value={"text": "original text"}
        )
        send = AsyncMock(return_value=_ok("rewritten text"))

        await ai._processing_lock.acquire()
        try:
            await _run(actions, send)
        finally:
            ai._processing_lock.release()

        send.assert_not_awaited()
        assert "already processing" in ai.speak.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_an_unready_service_does_not_capture_anything(self):
        ai = _ai(ready=False)
        actions = _actions(ai)
        app = actions.speech_handler.app
        app.send_request = AsyncMock()

        await _run(actions, AsyncMock())

        app.send_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_missing_model_is_named_rather_than_blamed_on_the_network(self):
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(
            return_value={"text": "original text"}
        )

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.MODEL_NOT_FOUND)))

        assert "model" in ai.speak.call_args[0][0].lower()
        ai.recheck_ready.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unchanged_answer_is_not_pasted(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "original text"})

        await _run(actions, AsyncMock(return_value=_ok("original text")))

        assert app.send_request.call_count == 1
        ai.speak_brief.assert_any_await("No changes needed.")
