"""main.py wiring for the 'Your training word' window (wh-first-sound-lost.23).

The Logic process builds the lead-word controller lazily. These tests check
the pieces main.py owns: the
provider channel (commands go out only with send_command_to_active_stt), the
transcription hold, and the lw_* routing in the GUI handler map.
"""
import asyncio
import queue
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from speech.lead_word_controller import LeadWordController

LW_ACTIONS = (
    "lw_open", "lw_close", "lw_record", "lw_keep", "lw_discard",
    "lw_restore", "lw_off", "lw_on", "lw_play_start", "lw_play_end",
)


def _fake_logic():
    from main import LogicController

    fake = MagicMock(spec=LogicController)
    fake._lead_word_controller = None
    fake.state_manager = MagicMock()
    fake.app = MagicMock()
    fake.app.websocket_manager.send_command_to_active_stt = AsyncMock(
        return_value=True
    )
    return fake


def _run_task(fake):
    """Run the coroutine that the last create_task_with_error_handling got."""
    (coro, _name), _ = fake.create_task_with_error_handling.call_args
    asyncio.run(coro)


def test_get_lead_word_controller_constructs_and_caches():
    from main import LogicController

    fake = _fake_logic()
    first = LogicController._get_lead_word_controller(fake)
    second = LogicController._get_lead_word_controller(fake)
    assert isinstance(first, LeadWordController)
    assert first is second


def test_opening_sends_lw_state_to_the_gui_queue():
    from main import LogicController

    fake = _fake_logic()
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    (msg,), _ = fake.state_manager.state_to_gui_queue.put_nowait.call_args
    assert msg["action"] == "lw_state"
    assert msg["state"]["screen"] == "loading"


def test_a_command_goes_out_only_with_send_command_to_active_stt():
    from main import LogicController

    fake = _fake_logic()
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    _run_task(fake)

    ws = fake.app.websocket_manager
    request_id = controller.current_request_id
    ws.send_command_to_active_stt.assert_awaited_once_with(
        "lead_word_status", request_id=request_id
    )
    ws.send_command_to_stt.assert_not_called()
    ws.broadcast.assert_not_called()


def test_a_send_that_finds_no_active_client_reports_the_failure():
    from main import LogicController

    fake = _fake_logic()
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    fake.app.websocket_manager.send_command_to_active_stt = AsyncMock(
        return_value=False
    )
    _run_task(fake)
    (msg,), _ = fake.state_manager.state_to_gui_queue.put_nowait.call_args
    assert "could not reach" in msg["state"]["error"].lower()


def test_no_websocket_manager_fails_the_send_at_once():
    from main import LogicController

    fake = _fake_logic()
    fake.app.websocket_manager = None
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    (msg,), _ = fake.state_manager.state_to_gui_queue.put_nowait.call_args
    assert "could not reach" in msg["state"]["error"].lower()


def test_a_send_that_raises_does_not_escape_the_task():
    from main import LogicController

    fake = _fake_logic()
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    fake.app.websocket_manager.send_command_to_active_stt = AsyncMock(
        side_effect=RuntimeError("socket closed")
    )
    _run_task(fake)  # an exception here would shut the program down
    (msg,), _ = fake.state_manager.state_to_gui_queue.put_nowait.call_args
    assert msg["state"]["error"]


def test_playback_hold_tells_the_state_manager_and_then_the_controller_goes_on():
    from main import LogicController

    fake = _fake_logic()
    fake.state_manager.hold_engine_for_lead_word_playback = AsyncMock()
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    controller.on_result({
        "request_id": controller.current_request_id, "stage": "status",
        "loaded": True, "recorded": "2026-10-22 15:37", "microphone": "m",
        "previous_exists": False, "key_path": "",
    })
    fake.state_manager.state_to_gui_queue.put_nowait.reset_mock()

    controller.handle_gui_action(
        "lw_play_start", {"which": "current", "duration_ms": 620}
    )
    # The sound waits for the engine hold.
    sent = [c.args[0]["action"] for c in
            fake.state_manager.state_to_gui_queue.put_nowait.call_args_list]
    assert "lw_play_go" not in sent
    _run_task(fake)
    fake.state_manager.hold_engine_for_lead_word_playback.assert_awaited_once()
    sent = [c.args[0]["action"] for c in
            fake.state_manager.state_to_gui_queue.put_nowait.call_args_list]
    assert "lw_play_go" in sent


def test_playback_end_releases_the_state_manager_hold():
    from main import LogicController

    fake = _fake_logic()
    fake.state_manager.hold_engine_for_lead_word_playback = AsyncMock()
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    controller.on_result({
        "request_id": controller.current_request_id, "stage": "status",
        "loaded": True, "recorded": "x", "microphone": "", "previous_exists": False,
        "key_path": "",
    })
    controller.handle_gui_action(
        "lw_play_start", {"which": "current", "duration_ms": 620}
    )
    _run_task(fake)
    controller.handle_gui_action("lw_play_end")
    fake.state_manager.release_engine_after_lead_word_playback.assert_called_once_with()


def test_a_failed_hold_still_lets_the_controller_go_on():
    from main import LogicController

    fake = _fake_logic()
    fake.state_manager.hold_engine_for_lead_word_playback = AsyncMock(
        side_effect=RuntimeError("no socket")
    )
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    controller.on_result({
        "request_id": controller.current_request_id, "stage": "status",
        "loaded": True, "recorded": "x", "microphone": "", "previous_exists": False,
        "key_path": "",
    })
    controller.handle_gui_action(
        "lw_play_start", {"which": "current", "duration_ms": 620}
    )
    _run_task(fake)
    sent = [c.args[0]["action"] for c in
            fake.state_manager.state_to_gui_queue.put_nowait.call_args_list]
    assert "lw_play_go" in sent


def test_handle_lead_word_action_routes_to_the_controller():
    from main import LogicController

    fake = _fake_logic()
    controller = MagicMock()
    fake._get_lead_word_controller = MagicMock(return_value=controller)
    command = {"action": "lw_play_start", "which": "current", "duration_ms": 5}
    LogicController._handle_lead_word_action(fake, "lw_play_start", command)
    controller.handle_gui_action.assert_called_once_with("lw_play_start", command)


def test_handle_lead_word_action_survives_a_construction_failure():
    from main import LogicController

    fake = _fake_logic()
    fake._get_lead_word_controller = MagicMock(side_effect=RuntimeError("boom"))
    LogicController._handle_lead_word_action(fake, "lw_open", {})


@pytest.mark.parametrize("action", LW_ACTIONS)
def test_the_handler_map_binds_each_lw_action_to_the_lead_word_handler(action):
    from main import LogicController

    fake = _fake_logic()
    fake._handle_lead_word_action = MagicMock()
    command = {"action": action, "which": "current", "duration_ms": 5}
    handler_map = LogicController._build_gui_handler_map(fake, command)
    assert action in handler_map
    handler_map[action]()
    fake._handle_lead_word_action.assert_called_once_with(action, command)


def test_the_handler_map_has_exactly_the_documented_lw_actions():
    from main import LogicController

    fake = _fake_logic()
    handler_map = LogicController._build_gui_handler_map(fake, {})
    assert sorted(k for k in handler_map if k.startswith("lw_")) == sorted(LW_ACTIONS)


# --------------------------------------------------------------------------
# Strength tests added with the mutation gate (wh-first-sound-lost.23, stage
# G2). Each one is red under a named mutation in
# tests/mutation_gate_lead_word_app_wiring.py.
# --------------------------------------------------------------------------

def _open_with_status(controller, fake):
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    controller.on_result({
        "request_id": controller.current_request_id, "stage": "status",
        "loaded": True, "recorded": "x", "microphone": "", "previous_exists": False,
        "key_path": "",
    })
    fake.state_manager.state_to_gui_queue.put_nowait.reset_mock()


def _last_state(fake):
    (msg,), _ = fake.state_manager.state_to_gui_queue.put_nowait.call_args
    return msg["state"]


def test_the_send_and_the_hold_run_as_named_tasks():
    from main import LogicController

    fake = _fake_logic()
    fake.state_manager.hold_engine_for_lead_word_playback = AsyncMock()
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    (coro, name), _ = fake.create_task_with_error_handling.call_args
    assert name == "LeadWordSendCommand"
    coro.close()
    _open_with_status(controller, fake)
    controller.handle_gui_action(
        "lw_play_start", {"which": "current", "duration_ms": 620}
    )
    (coro, name), _ = fake.create_task_with_error_handling.call_args
    assert name == "LeadWordHoldTranscription"
    coro.close()


def test_a_send_that_reaches_the_active_client_reports_no_failure():
    from main import LogicController

    fake = _fake_logic()
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    fake.state_manager.state_to_gui_queue.put_nowait.reset_mock()
    _run_task(fake)
    fake.state_manager.state_to_gui_queue.put_nowait.assert_not_called()
    assert controller.current_request_id is not None


def test_the_send_failure_names_the_request_that_failed():
    from main import LogicController

    fake = _fake_logic()
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    status_id = controller.current_request_id
    status_coro = fake.create_task_with_error_handling.call_args.args[0]
    controller.on_result({
        "request_id": status_id, "stage": "status", "loaded": False,
        "recorded": "", "microphone": "", "previous_exists": False,
        "key_path": "",
    })
    controller.handle_gui_action("lw_record")
    fake.state_manager.state_to_gui_queue.put_nowait.reset_mock()
    # The older status send fails late: it is not the current request.
    fake.app.websocket_manager.send_command_to_active_stt = AsyncMock(
        return_value=False
    )
    asyncio.run(status_coro)
    fake.state_manager.state_to_gui_queue.put_nowait.assert_not_called()
    assert controller.current_request_id is not None


def test_a_hold_that_fails_is_logged(caplog):
    from main import LogicController

    fake = _fake_logic()
    fake.state_manager.hold_engine_for_lead_word_playback = AsyncMock(
        side_effect=RuntimeError("no socket")
    )
    controller = LogicController._get_lead_word_controller(fake)
    _open_with_status(controller, fake)
    controller.handle_gui_action(
        "lw_play_start", {"which": "current", "duration_ms": 620}
    )
    with caplog.at_level("ERROR"):
        _run_task(fake)
    assert "Lead-word transcription hold failed" in caplog.text


def test_a_playback_start_that_fails_after_the_hold_is_logged_not_raised(caplog):
    from main import LogicController

    fake = _fake_logic()
    controller = LogicController._get_lead_word_controller(fake)
    channel = controller._provider

    def broken_done():
        raise RuntimeError("gui queue gone")

    fake.state_manager.hold_engine_for_lead_word_playback = AsyncMock()
    with caplog.at_level("ERROR"):
        asyncio.run(channel._hold_and_continue(broken_done))
    assert "Lead-word playback start failed" in caplog.text


def test_the_done_callback_runs_after_the_hold_and_not_before():
    from main import LogicController

    fake = _fake_logic()
    controller = LogicController._get_lead_word_controller(fake)
    channel = controller._provider
    order = []

    async def hold():
        order.append("hold")

    fake.state_manager.hold_engine_for_lead_word_playback = hold
    asyncio.run(channel._hold_and_continue(lambda: order.append("done")))
    assert order == ["hold", "done"]


def test_a_missing_websocket_manager_is_logged(caplog):
    from main import LogicController

    fake = _fake_logic()
    fake.app.websocket_manager = None
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    with caplog.at_level("ERROR"):
        controller.handle_gui_action("lw_open")
    assert "WebSocket manager not available" in caplog.text


def test_a_send_that_raises_is_logged(caplog):
    from main import LogicController

    fake = _fake_logic()
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    fake.app.websocket_manager.send_command_to_active_stt = AsyncMock(
        side_effect=RuntimeError("socket closed")
    )
    with caplog.at_level("ERROR"):
        _run_task(fake)
    assert "failed to send" in caplog.text


def test_a_failed_action_is_logged(caplog):
    from main import LogicController

    fake = _fake_logic()
    fake._get_lead_word_controller = MagicMock(side_effect=RuntimeError("boom"))
    with caplog.at_level("ERROR"):
        LogicController._handle_lead_word_action(fake, "lw_open", {})
    assert "Lead-word action 'lw_open' failed" in caplog.text


def test_the_lead_word_action_names_are_the_documented_ten():
    import main

    assert main._LEAD_WORD_GUI_ACTIONS == LW_ACTIONS


def test_each_lw_handler_passes_the_command_through_unchanged():
    from main import LogicController

    fake = _fake_logic()
    fake._handle_lead_word_action = MagicMock()
    command = {"action": "lw_play_start", "which": "candidate", "duration_ms": 9}
    handler_map = LogicController._build_gui_handler_map(fake, command)
    for name in LW_ACTIONS:
        handler_map[name]()
    assert [c.args for c in fake._handle_lead_word_action.call_args_list] == [
        (name, command) for name in LW_ACTIONS
    ]


def test_the_controller_sends_its_states_to_the_gui_queue_put_nowait():
    from main import LogicController

    fake = _fake_logic()
    controller = LogicController._get_lead_word_controller(fake)
    controller.set_supported(True)
    controller.handle_gui_action("lw_open")
    queue = fake.state_manager.state_to_gui_queue
    assert queue.put_nowait.call_count == 1
    assert queue.put.call_count == 0


@pytest.mark.asyncio
async def test_a_new_logic_controller_has_no_lead_word_controller_yet():
    from main import LogicController

    logic = LogicController(
        MagicMock(), MagicMock(), threading.Event(), MagicMock(), MagicMock(),
        MagicMock(),
    )
    assert logic._lead_word_controller is None


@pytest.mark.asyncio
async def test_the_gui_listener_routes_an_lw_action_through_the_map():
    from main import LogicController

    fake = MagicMock(spec=LogicController)
    shutdown = threading.Event()
    fake.shutdown_event = shutdown
    seen = []

    def handler():
        seen.append("handled")
        shutdown.set()

    fake._build_gui_handler_map = MagicMock(return_value={"lw_open": handler})
    q = queue.Queue()
    q.put({"action": "lw_open"})
    await LogicController._listen_for_gui_commands(fake, q)
    assert seen == ["handled"]
