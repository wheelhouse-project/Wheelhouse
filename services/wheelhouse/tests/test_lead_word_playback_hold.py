"""StateManager: the transcription hold for lead-word playback.

wh-first-sound-lost.23, acceptance item 22: the app turns transcription off
before the 'Your training word' window plays a recording, keeps it off for
the playback length plus 300 ms, then sets it back to its state before the
playback. The hold tells the engine only; it does not change the user's
speech setting, so no notice appears and nothing is saved.
"""
import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from state_manager import StateManager


@pytest.fixture
def ws():
    manager = Mock()
    manager.broadcast = AsyncMock()
    manager.set_transcription_status = Mock(
        side_effect=lambda enabled, reason=None: {
            "type": "set_transcription_status", "enabled": enabled,
            **({"reason": reason} if reason is not None else {}),
        }
    )
    return manager


@pytest.fixture
def sm(mock_config, mock_event_bus, mock_gui_queue, ws):
    loop = asyncio.new_event_loop()
    loop.create_task = Mock()
    mgr = StateManager(
        config_service=mock_config,
        event_bus=mock_event_bus,
        loop=loop,
        state_to_gui_queue=mock_gui_queue,
        websocket_manager=ws,
    )
    yield mgr
    loop.close()


@pytest.mark.asyncio
async def test_hold_tells_the_engine_transcription_is_off_and_waits_for_the_send(
    sm, ws
):
    sm._speech_enabled = True
    await sm.hold_engine_for_lead_word_playback()
    ws.set_transcription_status.assert_called_once_with(
        False, reason="lead_word_playback"
    )
    ws.broadcast.assert_awaited_once_with(
        {"type": "set_transcription_status", "enabled": False,
         "reason": "lead_word_playback"}
    )


@pytest.mark.asyncio
async def test_hold_does_not_change_the_users_speech_setting(sm):
    sm._speech_enabled = True
    before = sm.speech_enabled
    await sm.hold_engine_for_lead_word_playback()
    assert sm._speech_enabled is True
    assert sm.speech_enabled is before


@pytest.mark.asyncio
async def test_hold_names_a_reason_that_cannot_arm_the_wake_word(sm, ws):
    # The provider package is not installed in this service's environment,
    # so read the table from the source file.
    source = (
        Path(__file__).resolve().parents[2] / "stt_providers" / "shared"
        / "shared_stt" / "wake_word_detector.py"
    ).read_text(encoding="utf-8")
    armed = None
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", "") == "WAKE_WORD_ARMED_REASONS"
        ):
            armed = ast.literal_eval(node.value)
    assert armed, "WAKE_WORD_ARMED_REASONS not found"

    await sm.hold_engine_for_lead_word_playback()
    reason = ws.set_transcription_status.call_args.kwargs["reason"]
    assert all(reason not in reasons for reasons in armed.values())


@pytest.mark.asyncio
async def test_hold_without_a_websocket_manager_is_survivable(
    mock_config, mock_event_bus, mock_gui_queue
):
    loop = asyncio.new_event_loop()
    try:
        mgr = StateManager(
            config_service=mock_config, event_bus=mock_event_bus, loop=loop,
            state_to_gui_queue=mock_gui_queue, websocket_manager=None,
        )
        await mgr.hold_engine_for_lead_word_playback()
        mgr.release_engine_after_lead_word_playback()
    finally:
        loop.close()


def test_release_sends_the_engine_the_state_from_before_when_speech_was_on(sm, ws):
    sm._speech_enabled = True
    sm.release_engine_after_lead_word_playback()
    ws.set_transcription_status.assert_called_once()
    assert ws.set_transcription_status.call_args.args[0] is True
    sm.loop.create_task.assert_called_once()


def test_release_keeps_the_engine_off_when_the_user_had_speech_off(sm, ws):
    sm._speech_enabled = False
    sm.release_engine_after_lead_word_playback()
    assert ws.set_transcription_status.call_args.args[0] is False
    # The reason is the one a user's own switch-off sends, so the wake word
    # can still turn listening back on.
    assert ws.set_transcription_status.call_args.kwargs["reason"] == "manual"


# --------------------------------------------------------------------------
# Strength tests added with the mutation gate (wh-first-sound-lost.23, stage
# G2). Each one is red under a named mutation in
# tests/mutation_gate_lead_word_app_wiring.py.
# --------------------------------------------------------------------------

def test_release_when_speech_is_on_sends_no_reason_and_broadcasts_the_message(
    sm, ws
):
    sm._speech_enabled = True
    sm.release_engine_after_lead_word_playback()
    assert ws.set_transcription_status.call_args.kwargs["reason"] is None
    ws.broadcast.assert_called_once_with(
        {"type": "set_transcription_status", "enabled": True}
    )


def test_release_when_speech_is_off_broadcasts_the_manual_message(sm, ws):
    sm._speech_enabled = False
    sm.release_engine_after_lead_word_playback()
    ws.broadcast.assert_called_once_with(
        {"type": "set_transcription_status", "enabled": False,
         "reason": "manual"}
    )


@pytest.mark.asyncio
async def test_hold_sends_one_message_and_never_calls_the_release_path(sm, ws):
    await sm.hold_engine_for_lead_word_playback()
    assert ws.set_transcription_status.call_count == 1
    assert ws.broadcast.await_count == 1
    sm.loop.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_hold_leaves_the_speech_setting_and_the_gui_untouched(
    sm, mock_gui_queue
):
    sm._speech_enabled = False
    mock_gui_queue.put_nowait.reset_mock()
    await sm.hold_engine_for_lead_word_playback()
    assert sm._speech_enabled is False
    mock_gui_queue.put_nowait.assert_not_called()
