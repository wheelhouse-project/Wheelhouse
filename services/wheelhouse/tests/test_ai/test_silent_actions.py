"""AI feedback remains visible without invoking WheelHouse speech synthesis.

These cases catch reintroduced speech calls and lost outcome notifications at
the Logic-to-GUI boundary. Provider and Input-process I/O stay offline.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai.providers.openai_compat import ChatResult, ChatStatus
from speech.actions import ActionFunctions


def notifications(actions):
    """Messages actually sent to the existing GUI notification route."""
    queue = actions.speech_handler.logic_controller.state_manager.state_to_gui_queue
    return [
        c.args[0]["message"] for c in queue.put_nowait.call_args_list
        if c.args[0].get("action") == "show_notification"
    ]


@pytest.fixture
def wired():
    ai = MagicMock()
    ai.speak = AsyncMock()
    ai.speak_brief = AsyncMock()
    ai.is_ready.return_value = True
    ai._processing_lock = asyncio.Lock()
    ai.is_processing.side_effect = ai._processing_lock.locked
    ai.cancel_requested = False
    ai.recheck_ready = AsyncMock(return_value=True)
    ai.fix_text = AsyncMock(return_value=ChatResult(status=ChatStatus.OK, text="Fixed"))
    ai.rewrite_text = AsyncMock(return_value=ChatResult(status=ChatStatus.OK, text="Fixed"))
    handler = MagicMock()
    handler.config_service.get.return_value = {}
    handler.logic_controller.service_manager.ai_service = ai
    handler.app.send_request = AsyncMock(side_effect=[
        {"text": "original", "capture_token": "selected", "target_hwnd": 123},
        {"success": True},
    ])
    return ActionFunctions(handler), ai


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["fix_text_ai", "rewrite_text_ai"])
@pytest.mark.parametrize("outcome,notice", [
    ("success", "Done."),
    ("unchanged", "No changes needed."),
    ("unavailable", "AI is not available right now."),
    ("busy", "Already processing, please wait."),
    ("empty", "No text to"),
    ("failed", "failed. Original text preserved."),
    ("cancelled", "Cancelled."),
    ("cancel_race", "Cancelled."),
    ("copy_failed", "Could not copy the text."),
    ("capture_failed", "Could not capture the text."),
    ("flush_failed", "Earlier dictated letters were not delivered."),
    ("paste_failed", "Could not paste the corrected text."),
    ("focus_drift", "The window changed while the AI worked."),
])
async def test_transform_is_silent_and_keeps_outcome_feedback(wired, action, outcome, notice):
    actions, ai = wired
    app = actions.speech_handler.app
    response = ChatResult(status=ChatStatus.OK, text="Fixed")
    if outcome == "unavailable":
        ai.is_ready.return_value = False
    elif outcome == "busy":
        ai.is_processing.side_effect = None
        ai.is_processing.return_value = True
    elif outcome == "empty":
        app.send_request.side_effect = [{"text": ""}]
    elif outcome in {"copy_failed", "capture_failed", "flush_failed"}:
        app.send_request.side_effect = [{"text": "", outcome: True}]
    elif outcome in {"paste_failed", "focus_drift"}:
        app.send_request.side_effect = [
            {"text": "original"}, {"success": False, outcome: True},
        ]
    elif outcome == "unchanged":
        response = ChatResult(status=ChatStatus.OK, text="original")
    elif outcome == "failed":
        response = ChatResult(status=ChatStatus.TRANSPORT_ERROR)
    elif outcome == "cancelled":
        response = ChatResult(status=ChatStatus.CANCELLED)
    elif outcome == "cancel_race":
        ai.cancel_requested = True
    ai.fix_text.return_value = response
    ai.rewrite_text.return_value = response

    if action == "fix_text_ai":
        await actions.fix_text_ai()
    else:
        await actions.rewrite_text_ai("Use plain language")

    ai.speak.assert_not_called()
    ai.speak_brief.assert_not_called()
    assert any(notice in message for message in notifications(actions))
    assert not ai._processing_lock.locked()
    replacements = [c for c in app.send_request.call_args_list
                    if c.args[0] == "replace_selected_text"]
    assert bool(replacements) == (outcome in {"success", "paste_failed", "focus_drift"})


@pytest.mark.asyncio
async def test_cancel_is_silent_and_visible(wired):
    actions, ai = wired
    async with ai._processing_lock:
        await actions.cancel_fix()
    assert ai.cancel_requested
    ai.speak.assert_not_called()
    ai.speak_brief.assert_not_called()
    assert "Cancelling." in notifications(actions)


@pytest.mark.asyncio
async def test_unconfigured_help_is_silent(wired):
    """The spoken help command never speaks; the notice is written.

    The wording and the notice itself moved to the Logic controller when
    the spoken command and the Help menu entry were given one shared code
    path (wh-assistant-button-explainer, criterion W4). The notice is
    asserted there, in
    tests/test_logic_open_help_online.py::test_a_blank_address_opens_nothing_and_says_so.
    What stays here is the half this file is about: no speech, whatever
    the outcome.
    """
    actions, ai = wired
    lc = actions.speech_handler.logic_controller
    lc.config_service.get.return_value = ""
    lc.start_help_online = AsyncMock()

    await actions.wheelhouse_help_online()

    lc.start_help_online.assert_awaited_once()
    ai.speak.assert_not_called()
    ai.speak_brief.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled", "timeout"])
async def test_ask_keeps_working_dialog_and_failure_logs_without_speech(wired, outcome, caplog):
    actions, ai = wired
    ai.get_request_timeout_s.return_value = 0 if outcome == "timeout" else 1
    status = {"failed": ChatStatus.TRANSPORT_ERROR,
              "cancelled": ChatStatus.CANCELLED}.get(outcome, ChatStatus.OK)
    ai.ask = AsyncMock(return_value=ChatResult(status=status, text=" answer "))
    with caplog.at_level(logging.INFO, logger="speech.actions"):
        if outcome == "success":
            assert await actions.ask_ai("Question") == "answer"
        else:
            with pytest.raises((RuntimeError, TimeoutError)):
                await actions.ask_ai("Question")
            assert "ask_ai failed" in caplog.text
    ai.speak.assert_not_called()
    ai.speak_brief.assert_not_called()
    queue = actions.speech_handler.logic_controller.state_manager.state_to_gui_queue
    shown, hidden = [c.args[0] for c in queue.put_nowait.call_args_list]
    assert shown["action"] == "show_working"
    assert shown["message"] == "Asking..."
    assert hidden == {"action": "hide_working", "owner": shown["owner"]}
    assert not ai._processing_lock.locked()
    assert ai.cancel_requested is False


@pytest.mark.asyncio
async def test_feedback_queue_failure_keeps_log_and_releases_processing_lock(wired, caplog):
    actions, ai = wired
    queue = actions.speech_handler.logic_controller.state_manager.state_to_gui_queue
    queue.put_nowait.side_effect = RuntimeError("GUI unavailable")
    with caplog.at_level(logging.INFO, logger="speech.actions"):
        await actions.fix_text_ai()
    assert "Done." in caplog.text
    assert not ai._processing_lock.locked()
    ai.speak.assert_not_called()
    ai.speak_brief.assert_not_called()
