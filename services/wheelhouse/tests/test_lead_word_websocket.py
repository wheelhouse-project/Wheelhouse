"""WebSocketManager routing for the lead-word window (wh-first-sound-lost.23).

Acceptance item 10: the provider's ``lead_word_result`` reaches the app as
``engine_settings_result`` did before the voice-teaching feature was removed,
from the ACTIVE client only; the capabilities
frame carries a ``lead_word`` field and an absent field means supported =
false; the app sends the seven lead_word_* commands only with
``send_command_to_active_stt``.
"""
import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest


def _make_mock_ws(messages, remote_address=("127.0.0.1", 9999)):
    encoded = [json.dumps(m) if isinstance(m, dict) else m for m in messages]

    async def _gen():
        for item in encoded:
            yield item

    ws = AsyncMock()
    ws.remote_address = remote_address
    ws.send = AsyncMock()
    ws.__aiter__ = Mock(return_value=_gen())
    return ws


class _GatedWS:
    """A fake websocket that delivers its frames only after a gate opens."""

    def __init__(self, messages, gate, remote_address=("127.0.0.1", 9999)):
        self._messages = [
            json.dumps(m) if isinstance(m, dict) else m for m in messages
        ]
        self._gate = gate
        self.remote_address = remote_address
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        await self._gate.wait()
        for m in self._messages:
            yield m


async def _wait_until(predicate, timeout=2.0):
    async def _poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def controller():
    return MagicMock()


@pytest.fixture
def manager(event_loop, controller):
    from integrations.websocket_manager import WebSocketManager

    mgr = WebSocketManager(loop=event_loop)
    mgr.state_manager = MagicMock()
    logic = SimpleNamespace(
        _get_lead_word_controller=lambda: controller,
    )
    mgr.speech_handler = SimpleNamespace(logic_controller=logic)
    return mgr


RESULT = {
    "type": "lead_word_result",
    "request_id": "abc",
    "stage": "passed",
    "reason": "",
    "measures": {"voiced_ms": 620},
    "key_path": "",
}


@pytest.mark.asyncio
async def test_a_result_from_the_active_client_reaches_the_controller(
    manager, controller
):
    await manager.handle_connection(_make_mock_ws([RESULT]))
    controller.on_result.assert_called_once_with(RESULT)


@pytest.mark.asyncio
async def test_a_result_from_a_client_that_is_not_active_is_dropped(
    manager, controller
):
    stale_gate = asyncio.Event()
    active_gate = asyncio.Event()
    stale_ws = _GatedWS([RESULT], stale_gate, ("127.0.0.1", 9001))
    active_ws = _GatedWS([], active_gate, ("127.0.0.1", 9002))

    stale_task = asyncio.create_task(manager.handle_connection(stale_ws))
    await _wait_until(lambda: manager._active_stt_client is stale_ws)
    active_task = asyncio.create_task(manager.handle_connection(active_ws))
    await _wait_until(lambda: manager._active_stt_client is active_ws)

    stale_gate.set()
    await stale_task
    active_gate.set()
    await active_task

    controller.on_result.assert_not_called()


@pytest.mark.asyncio
async def test_a_result_with_no_controller_is_survivable(manager, caplog):
    manager.speech_handler = None
    with caplog.at_level("ERROR"):
        await manager.handle_connection(_make_mock_ws([RESULT]))
    # The message loop swallows an exception and logs it, so a result that
    # reached a missing controller shows only in the log.
    assert "Error processing message" not in caplog.text


@pytest.mark.asyncio
async def test_a_result_is_not_read_as_a_transcript(manager):
    manager.word_queue = MagicMock()
    await manager.handle_connection(_make_mock_ws([RESULT]))
    manager.word_queue.put_nowait.assert_not_called()


def _supported_calls(controller):
    return [c.args[0] for c in controller.set_supported.call_args_list]


@pytest.mark.asyncio
async def test_a_capabilities_frame_with_lead_word_supported_tells_the_controller(
    manager, controller
):
    await manager.handle_connection(_make_mock_ws([{
        "type": "capabilities", "provider": "parakeet_tdt", "emits_eos": True,
        "lead_word": {"supported": True, "loaded": False, "recorded": ""},
    }]))
    # The first call is the reset at connect, the last is the disconnect.
    assert _supported_calls(controller)[1] is True


@pytest.mark.asyncio
async def test_a_capabilities_frame_without_the_field_means_not_supported(
    manager, controller
):
    await manager.handle_connection(_make_mock_ws([{
        "type": "capabilities", "provider": "google_stt",
        "emits_eos": True,
    }]))
    calls = _supported_calls(controller)
    assert calls[1] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [{"supported": "true"}, {"supported": 1}, {"loaded": True}, "yes", None, []],
)
async def test_only_a_literal_true_counts_as_supported(manager, controller, field):
    await manager.handle_connection(_make_mock_ws([{
        "type": "capabilities", "provider": "parakeet_tdt", "lead_word": field,
    }]))
    assert _supported_calls(controller)[1] is False


@pytest.mark.asyncio
async def test_a_capabilities_frame_from_a_client_that_is_not_active_is_ignored(
    manager, controller
):
    stale_gate = asyncio.Event()
    active_gate = asyncio.Event()
    stale_ws = _GatedWS(
        [{"type": "capabilities", "provider": "parakeet_tdt",
          "lead_word": {"supported": True}}],
        stale_gate, ("127.0.0.1", 9001))
    active_ws = _GatedWS([], active_gate, ("127.0.0.1", 9002))

    stale_task = asyncio.create_task(manager.handle_connection(stale_ws))
    await _wait_until(lambda: manager._active_stt_client is stale_ws)
    active_task = asyncio.create_task(manager.handle_connection(active_ws))
    await _wait_until(lambda: manager._active_stt_client is active_ws)
    controller.set_supported.reset_mock()

    stale_gate.set()
    await stale_task
    assert True not in _supported_calls(controller)
    active_gate.set()
    await active_task


@pytest.mark.asyncio
async def test_a_new_active_client_starts_as_not_supported(manager, controller):
    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 5000)
    await manager.add_client(ws)
    assert _supported_calls(controller) == [False]


@pytest.mark.asyncio
async def test_losing_the_active_client_clears_the_support(manager, controller):
    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 5000)
    await manager.add_client(ws)
    controller.set_supported.reset_mock()

    manager.remove_client(ws)

    assert _supported_calls(controller) == [False]


@pytest.mark.asyncio
async def test_the_lead_word_commands_go_only_to_the_active_client(manager):
    old_ws = AsyncMock()
    old_ws.remote_address = ("127.0.0.1", 5001)
    new_ws = AsyncMock()
    new_ws.remote_address = ("127.0.0.1", 5002)
    await manager.add_client(old_ws)
    await manager.add_client(new_ws)
    old_ws.send.reset_mock()
    new_ws.send.reset_mock()

    sent = await manager.send_command_to_active_stt(
        "lead_word_record", request_id="r1"
    )

    assert sent is True
    old_ws.send.assert_not_called()
    payload = json.loads(new_ws.send.call_args.args[0])
    assert payload == {"type": "lead_word_record", "request_id": "r1"}


@pytest.mark.asyncio
async def test_a_lead_word_command_with_no_active_client_is_not_sent(
    manager, caplog
):
    assert manager._active_stt_client is None

    with caplog.at_level(logging.WARNING):
        sent = await manager.send_command_to_active_stt(
            "lead_word_record", request_id="r1"
        )

    assert sent is False
    assert "No active STT client; dropping command: lead_word_record" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_lead_word_send_reports_not_sent(manager, caplog):
    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 5003)
    await manager.add_client(ws)
    ws.send.reset_mock()
    ws.send.side_effect = ConnectionError("closed")

    with caplog.at_level(logging.WARNING):
        sent = await manager.send_command_to_active_stt(
            "lead_word_record", request_id="r1"
        )

    assert sent is False
    ws.send.assert_called_once()
    assert "Failed to send lead_word_record to active STT client" in caplog.text


# --------------------------------------------------------------------------
# Strength tests added with the mutation gate (wh-first-sound-lost.23, stage
# G2). Each one is red under a named mutation in
# tests/mutation_gate_lead_word_app_wiring.py.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_losing_a_client_that_is_not_active_leaves_the_support_alone(
    manager, controller
):
    old_ws = AsyncMock()
    old_ws.remote_address = ("127.0.0.1", 5001)
    new_ws = AsyncMock()
    new_ws.remote_address = ("127.0.0.1", 5002)
    await manager.add_client(old_ws)
    await manager.add_client(new_ws)
    controller.set_supported.reset_mock()

    manager.remove_client(old_ws)

    controller.set_supported.assert_not_called()


@pytest.mark.asyncio
async def test_a_controller_that_fails_on_support_does_not_break_a_connect(
    manager, controller, caplog
):
    controller.set_supported.side_effect = RuntimeError("boom")
    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 5000)
    with caplog.at_level("ERROR"):
        await manager.add_client(ws)
    assert "Lead-word capability handling failed" in caplog.text
    assert manager._active_stt_client is ws


def test_the_controller_lookup_returns_the_controller(manager, controller):
    assert manager._lead_word_controller() is controller


def test_the_controller_lookup_without_a_speech_handler_returns_none(manager):
    manager.speech_handler = None
    assert manager._lead_word_controller() is None


def test_the_controller_lookup_without_the_getter_returns_none(manager, caplog):
    manager.speech_handler = SimpleNamespace(logic_controller=SimpleNamespace())
    with caplog.at_level("ERROR"):
        assert manager._lead_word_controller() is None
    # The lookup swallows a failed call and logs it, so a missing guard
    # shows only in the log.
    assert "Lead-word controller lookup failed" not in caplog.text


@pytest.mark.asyncio
async def test_a_support_change_with_no_controller_logs_no_error(manager, caplog):
    manager.speech_handler = None
    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 5000)
    with caplog.at_level("ERROR"):
        await manager.add_client(ws)
    # The capability call swallows an exception and logs it, so a missing
    # guard shows only in the log.
    assert "Lead-word capability handling failed" not in caplog.text


def test_a_failing_controller_lookup_returns_none_and_is_logged(manager, caplog):
    def boom():
        raise RuntimeError("not built")

    manager.speech_handler = SimpleNamespace(
        logic_controller=SimpleNamespace(_get_lead_word_controller=boom)
    )
    with caplog.at_level("ERROR"):
        assert manager._lead_word_controller() is None
    assert "Lead-word controller lookup failed" in caplog.text


@pytest.mark.asyncio
async def test_a_result_with_no_controller_is_logged_as_dropped(manager, caplog):
    manager.speech_handler = None
    with caplog.at_level("WARNING"):
        await manager.handle_connection(_make_mock_ws([RESULT]))
    assert "no lead-word controller is available" in caplog.text


@pytest.mark.asyncio
async def test_a_result_from_a_client_that_is_not_active_is_logged(
    manager, controller, caplog
):
    stale_gate = asyncio.Event()
    active_gate = asyncio.Event()
    stale_ws = _GatedWS([RESULT], stale_gate, ("127.0.0.1", 9001))
    active_ws = _GatedWS([], active_gate, ("127.0.0.1", 9002))
    stale_task = asyncio.create_task(manager.handle_connection(stale_ws))
    await _wait_until(lambda: manager._active_stt_client is stale_ws)
    active_task = asyncio.create_task(manager.handle_connection(active_ws))
    await _wait_until(lambda: manager._active_stt_client is active_ws)

    with caplog.at_level("INFO"):
        stale_gate.set()
        await stale_task
    assert "Ignoring lead_word_result from non-active STT client" in caplog.text
    active_gate.set()
    await active_task


@pytest.mark.asyncio
async def test_a_result_is_handled_once_and_goes_no_further(
    manager, controller, caplog
):
    """The result is not also read as an engine-settings or transcript frame."""
    manager.word_queue = MagicMock()
    with caplog.at_level("WARNING"):
        await manager.handle_connection(_make_mock_ws([RESULT, RESULT]))
    assert controller.on_result.call_count == 2
    manager.word_queue.put_nowait.assert_not_called()
    # A frame that fell through every handler would end in the warning for an
    # unexpected message type; no other handler touches this frame.
    assert "Unexpected message type" not in caplog.text
