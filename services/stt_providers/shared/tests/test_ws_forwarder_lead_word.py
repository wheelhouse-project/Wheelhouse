"""The lead_word_* commands, the lead_word_result frame and the lead_word
capabilities field of WSForwarder (wh-first-sound-lost.23, acceptance item 10
and the contract in section 2, 3 and 5).
"""
import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_stt.ws_forwarder import WSForwarder

from .test_ws_forwarder_capabilities import (
    _FakeWS,
    _install_fake_websockets,
    _run_sender_loop,
)

COMMANDS = [
    "lead_word_status",
    "lead_word_record",
    "lead_word_keep",
    "lead_word_discard",
    "lead_word_restore",
    "lead_word_off",
    "lead_word_on",
]


async def _serve(handler):
    import websockets

    server = await websockets.serve(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", COMMANDS)
async def test_each_command_reaches_the_callback_with_its_request_id(command):
    calls = []
    fired = threading.Event()
    threads = []
    tasks = []

    def on_command(name, request_id):
        calls.append((name, request_id))
        threads.append(threading.current_thread())
        # Scheduled with call_soon_threadsafe, the callback runs as a plain
        # loop callback, outside any task. A direct call from the receive
        # coroutine would run inside that coroutine's task.
        try:
            tasks.append(asyncio.current_task())
        except RuntimeError:
            tasks.append(None)
        fired.set()

    ready = asyncio.Event()
    holder = {}

    async def handler(websocket):
        holder["ws"] = websocket
        await websocket.send('{"type": "status", "transcription_enabled": true}')
        ready.set()
        try:
            await websocket.wait_closed()
        except Exception:
            pass

    server, port = await _serve(handler)
    event = threading.Event()
    event.set()
    forwarder = WSForwarder(
        host="127.0.0.1", port=port, transcription_enabled_event=event,
        lead_word_callback=on_command, debug=False)
    forwarder.start()
    await asyncio.wait_for(ready.wait(), timeout=2.0)
    await asyncio.sleep(0.1)
    await holder["ws"].send(json.dumps({"type": command, "request_id": "r-7"}))
    assert fired.wait(timeout=3.0), "callback did not fire"
    thread = forwarder._thread
    forwarder.stop()
    server.close()
    await server.wait_closed()

    assert calls == [(command, "r-7")]
    # The callback runs on the forwarder's own thread (its event loop).
    assert threads == [thread]
    assert tasks == [None]


@pytest.mark.asyncio
async def test_a_command_with_no_request_id_gives_an_empty_one():
    calls = []
    fired = threading.Event()

    def on_command(name, request_id):
        calls.append((name, request_id))
        fired.set()

    ready = asyncio.Event()
    holder = {}

    async def handler(websocket):
        holder["ws"] = websocket
        await websocket.send('{"type": "status", "transcription_enabled": true}')
        ready.set()
        try:
            await websocket.wait_closed()
        except Exception:
            pass

    server, port = await _serve(handler)
    event = threading.Event()
    event.set()
    forwarder = WSForwarder(
        host="127.0.0.1", port=port, transcription_enabled_event=event,
        lead_word_callback=on_command, debug=False)
    forwarder.start()
    await asyncio.wait_for(ready.wait(), timeout=2.0)
    await asyncio.sleep(0.1)
    await holder["ws"].send(json.dumps({"type": "lead_word_status"}))
    assert fired.wait(timeout=3.0)
    forwarder.stop()
    server.close()
    await server.wait_closed()
    assert calls == [("lead_word_status", "")]


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [5, None, ["r"], {"id": "r"}, 1.5, True])
async def test_a_request_id_that_is_not_text_gives_an_empty_one(request_id):
    calls = []
    fired = threading.Event()

    def on_command(name, rid):
        calls.append((name, rid))
        fired.set()

    ready = asyncio.Event()
    holder = {}

    async def handler(websocket):
        holder["ws"] = websocket
        await websocket.send('{"type": "status", "transcription_enabled": true}')
        ready.set()
        try:
            await websocket.wait_closed()
        except Exception:
            pass

    server, port = await _serve(handler)
    event = threading.Event()
    event.set()
    forwarder = WSForwarder(
        host="127.0.0.1", port=port, transcription_enabled_event=event,
        lead_word_callback=on_command, debug=False)
    forwarder.start()
    await asyncio.wait_for(ready.wait(), timeout=2.0)
    await asyncio.sleep(0.1)
    await holder["ws"].send(
        json.dumps({"type": "lead_word_keep", "request_id": request_id}))
    assert fired.wait(timeout=3.0)
    forwarder.stop()
    server.close()
    await server.wait_closed()
    assert calls == [("lead_word_keep", "")]


@pytest.mark.asyncio
async def test_a_command_with_no_callback_registered_does_not_crash():
    logged = []
    ready = asyncio.Event()
    holder = {}

    async def handler(websocket):
        holder["ws"] = websocket
        await websocket.send('{"type": "status", "transcription_enabled": true}')
        ready.set()
        try:
            await websocket.wait_closed()
        except Exception:
            pass

    server, port = await _serve(handler)
    event = threading.Event()
    event.set()
    forwarder = WSForwarder(
        host="127.0.0.1", port=port, transcription_enabled_event=event,
        log_func=logged.append, debug=False)
    forwarder.start()
    await asyncio.wait_for(ready.wait(), timeout=2.0)
    await asyncio.sleep(0.1)
    await holder["ws"].send(
        json.dumps({"type": "lead_word_record", "request_id": "x"}))
    await asyncio.sleep(0.3)
    forwarder.stop()
    server.close()
    await server.wait_closed()
    assert any("lead_word_record" in line and "no callback" in line
               for line in logged)


async def _collect_one(send):
    """Run a forwarder against a server; call send(forwarder); return frames."""
    frames = []
    ready = asyncio.Event()

    async def handler(websocket):
        await websocket.send('{"type": "status", "transcription_enabled": true}')
        ready.set()
        try:
            async for message in websocket:
                frame = json.loads(message)
                if frame.get("type") not in ("capabilities", "log"):
                    frames.append(frame)
        except Exception:
            pass

    server, port = await _serve(handler)
    event = threading.Event()
    event.set()
    forwarder = WSForwarder(
        host="127.0.0.1", port=port, transcription_enabled_event=event,
        debug=False)
    forwarder.start()
    await asyncio.wait_for(ready.wait(), timeout=2.0)
    await asyncio.sleep(0.1)
    send(forwarder)
    await asyncio.sleep(0.5)
    forwarder.stop()
    server.close()
    await server.wait_closed()
    return frames


@pytest.mark.asyncio
async def test_listening_frame_has_no_key_path_and_no_state_fields():
    frames = await _collect_one(
        lambda f: f.send_lead_word_result("r1", "listening"))
    assert frames == [{
        "type": "lead_word_result", "request_id": "r1", "stage": "listening",
        "reason": "", "measures": {},
    }]


@pytest.mark.asyncio
async def test_checking_frame_has_no_key_path_and_no_state_fields():
    frames = await _collect_one(
        lambda f: f.send_lead_word_result(
            "r1b", "checking", key_path="x", loaded=True, recorded="y",
            microphone="z", previous_exists=True, current_exists=True))
    assert frames == [{
        "type": "lead_word_result", "request_id": "r1b", "stage": "checking",
        "reason": "", "measures": {},
    }]


@pytest.mark.asyncio
async def test_status_frame_has_key_path_and_state_fields():
    frames = await _collect_one(lambda f: f.send_lead_word_result(
        "r2", "status", key_path="C:\\a\\hello.wav", loaded=True,
        recorded="2026-10-22 15:37", microphone="Headset", previous_exists=True,
        current_exists=True))
    assert frames == [{
        "type": "lead_word_result", "request_id": "r2", "stage": "status",
        "reason": "", "measures": {}, "key_path": "C:\\a\\hello.wav",
        "loaded": True, "recorded": "2026-10-22 15:37",
        "microphone": "Headset", "previous_exists": True,
        "current_exists": True,
    }]


@pytest.mark.asyncio
async def test_passed_frame_has_measures_and_key_path_but_no_state_fields():
    measures = {"voiced_ms": 600, "level_dbfs": -13.9, "decode": "hello"}
    frames = await _collect_one(lambda f: f.send_lead_word_result(
        "r3", "passed", measures=measures, key_path="C:\\a\\hello.wav",
        loaded=True, recorded="x", microphone="y", previous_exists=True,
        current_exists=True))
    assert frames == [{
        "type": "lead_word_result", "request_id": "r3", "stage": "passed",
        "reason": "", "measures": measures, "key_path": "C:\\a\\hello.wav",
        "noise_advice": False,
    }]


@pytest.mark.asyncio
async def test_passed_frame_carries_the_noise_advice_as_a_boolean():
    # wh-training-word-noisy-reject: the provider decides the advice.
    frames = await _collect_one(lambda f: f.send_lead_word_result(
        "r3", "passed", measures={}, key_path="k", noise_advice=1))
    assert frames[0]["noise_advice"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", ["listening", "checking", "refused", "status", "kept",
              "restored", "off", "on", "failed"])
async def test_no_other_stage_carries_the_noise_advice(stage):
    frames = await _collect_one(lambda f: f.send_lead_word_result(
        "r9", stage, noise_advice=True))
    assert "noise_advice" not in frames[0]


@pytest.mark.asyncio
async def test_refused_frame_has_reason_and_measures():
    frames = await _collect_one(lambda f: f.send_lead_word_result(
        "r4", "refused", reason="Too quiet", measures={"level_dbfs": -40.0}))
    assert frames == [{
        "type": "lead_word_result", "request_id": "r4", "stage": "refused",
        "reason": "Too quiet", "measures": {"level_dbfs": -40.0},
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["kept", "restored", "off", "on"])
async def test_state_stages_carry_state_fields_and_no_key_path(stage):
    frames = await _collect_one(lambda f: f.send_lead_word_result(
        "r5", stage, key_path="ignored", loaded=True,
        recorded="2026-10-22 15:37", microphone="Headset",
        previous_exists=True, current_exists=True))
    assert frames == [{
        "type": "lead_word_result", "request_id": "r5", "stage": stage,
        "reason": "", "measures": {}, "loaded": True,
        "recorded": "2026-10-22 15:37", "microphone": "Headset",
        "previous_exists": True, "current_exists": True,
    }]


@pytest.mark.asyncio
async def test_failed_frame_carries_the_error_text():
    frames = await _collect_one(lambda f: f.send_lead_word_result(
        "r6", "failed", reason="Keep failed: disk full"))
    assert frames[0]["stage"] == "failed"
    assert frames[0]["reason"] == "Keep failed: disk full"
    assert "key_path" not in frames[0] and "loaded" not in frames[0]


@pytest.mark.asyncio
async def test_the_state_fields_are_sent_as_booleans():
    """loaded, previous_exists and current_exists reach the wire as true or
    false, never as the caller's own value (1 equals True in Python, but the
    JSON text differs)."""
    frames = await _collect_one(lambda f: f.send_lead_word_result(
        "r7", "status", key_path="k", loaded=1, recorded="a", microphone="m",
        previous_exists="yes", current_exists=1))
    assert frames[0]["loaded"] is True
    assert frames[0]["previous_exists"] is True
    assert frames[0]["current_exists"] is True


def test_unknown_stage_is_refused():
    forwarder = WSForwarder("127.0.0.1", 1, threading.Event())
    with pytest.raises(ValueError):
        forwarder.send_lead_word_result("r", "bogus")


def test_send_lead_word_result_before_start_does_not_raise():
    forwarder = WSForwarder("127.0.0.1", 1, threading.Event())
    forwarder.send_lead_word_result("r", "listening")


def _capabilities_frame(monkeypatch, **kwargs):
    stop_evt = threading.Event()
    ws = _FakeWS(stop_evt, after_first_send="stop")
    _install_fake_websockets(monkeypatch, [ws])
    fwd = WSForwarder(
        "127.0.0.1", 9999, threading.Event(), provider_name="parakeet_tdt",
        **kwargs)
    fwd._stop_evt = stop_evt
    _run_sender_loop(fwd)
    return ws.sent[0]


def test_capabilities_carry_the_lead_word_field_when_the_provider_gives_one(
        monkeypatch):
    frame = _capabilities_frame(
        monkeypatch,
        lead_word_capabilities=lambda: {
            "supported": True, "loaded": False, "recorded": ""})
    assert frame["lead_word"] == {
        "supported": True, "loaded": False, "recorded": ""}


def test_capabilities_have_no_lead_word_field_without_a_provider_callable(
        monkeypatch):
    frame = _capabilities_frame(monkeypatch)
    assert "lead_word" not in frame


def test_a_failing_lead_word_callable_leaves_the_field_out_and_sends_the_frame(
        monkeypatch):
    def boom():
        raise RuntimeError("no state")

    frame = _capabilities_frame(monkeypatch, lead_word_capabilities=boom)
    assert frame["type"] == "capabilities"
    assert "lead_word" not in frame


def test_microphone_device_property_reads_the_last_name_sent():
    forwarder = WSForwarder("127.0.0.1", 1, threading.Event())
    assert forwarder.microphone_device == ""
    forwarder.send_microphone("Headset Mic")
    assert forwarder.microphone_device == "Headset Mic"
