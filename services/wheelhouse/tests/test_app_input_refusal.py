"""Input refusals reach the Logic process as data, not as an ERROR.

wh-safety-word-free-commands, stage S1.

The Input process refuses an activate on purpose when no window matches and
nothing starts, or when Windows will not bring a window forward. The Logic
process decides what the user sees, so the refusal must reach the caller as a
typed exception that carries the reply, and it must not be logged at ERROR:
every ERROR record shows a Windows error popup (main.py), and a refused
command is an ordinary outcome, not a defect. Any other error reply keeps
today's behaviour: a plain RuntimeError, logged at ERROR.
"""
import asyncio
import logging
from queue import Empty, Queue
from unittest.mock import MagicMock, Mock, patch

import pytest


@pytest.fixture
def mock_shm():
    shm = MagicMock()
    shm.buf = bytearray(1024 * 64)
    shm.size = 1024 * 64
    shm.name = "test_shm"
    return shm


@pytest.fixture
def mock_response_queue():
    q = MagicMock()
    q.get_nowait = Mock(side_effect=Empty)
    return q


@pytest.fixture
def app(mock_shm, mock_response_queue):
    command_ready = MagicMock()
    command_ready.is_set.return_value = False
    ui_ready = MagicMock()
    ui_ready.is_set.return_value = True
    with patch("app.shared_memory.SharedMemory", return_value=mock_shm):
        from app import WheelHouseApp
        return WheelHouseApp(
            shm_name="test_shm",
            command_ready_event=command_ready,
            ui_ready_event=ui_ready,
            response_queue=mock_response_queue,
            shm_bytes=1024 * 64,
            response_timeout_s=2.0,
        )


async def _resolve(app, mock_response_queue, response):
    """Run the demuxer over one reply and return the future it resolved."""
    future = asyncio.get_running_loop().create_future()
    app.response_futures[response["request_id"]] = future
    calls = 0

    def side_effect():
        nonlocal calls
        calls += 1
        if calls == 1:
            return response
        raise Empty

    mock_response_queue.get_nowait.side_effect = side_effect
    task = asyncio.create_task(app._demux_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return future


@pytest.mark.asyncio
async def test_a_refusal_reply_resolves_as_input_refused(
    app, mock_response_queue
):
    from app import InputRefused

    response = {
        "request_id": "rq-refused", "error": True, "refusal": True,
        "message": "No program matched notepad.",
        "outcome": "stopped", "action": "activate_window",
    }
    future = await _resolve(app, mock_response_queue, response)

    with pytest.raises(InputRefused, match="No program matched notepad."):
        future.result()
    assert isinstance(future.exception(), RuntimeError)
    assert future.exception().reply == response


@pytest.mark.asyncio
async def test_a_plain_error_reply_stays_a_plain_runtime_error(
    app, mock_response_queue
):
    from app import InputRefused

    response = {"request_id": "rq-plain", "error": True, "message": "boom"}
    future = await _resolve(app, mock_response_queue, response)

    assert type(future.exception()) is RuntimeError
    assert not isinstance(future.exception(), InputRefused)


async def _send_and_answer(app, reply_fields):
    """send_request one activate and answer it through the real demuxer."""
    app.response_queue = Queue()
    demux = asyncio.create_task(app._demux_loop())
    try:
        task = asyncio.create_task(
            app.send_request("activate_window", {"target": "x"}, timeout_s=2.0)
        )
        await asyncio.sleep(0)
        item = app._outbound_q.get_nowait()
        app.response_queue.put({
            "request_id": item["request_id"], "error": True,
            "action": "activate_window", **reply_fields,
        })
        return await task
    finally:
        demux.cancel()
        await asyncio.gather(demux, return_exceptions=True)


@pytest.mark.asyncio
async def test_send_request_logs_a_refusal_below_error(app, caplog):
    from app import InputRefused

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(InputRefused):
            await _send_and_answer(
                app, {"refusal": True, "message": "No program matched x."}
            )

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.asyncio
async def test_send_request_still_logs_a_plain_error_at_error(app, caplog):
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError):
            await _send_and_answer(app, {"message": "boom"})

    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_a_timeout_logs_at_error_by_default(app, caplog):
    """Every other awaited step keeps today's ERROR record on a timeout."""
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(
                "hotkey_action", {"keys": ["a"]}, timeout_s=0.05
            )

    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_quiet_timeout_logs_below_error_and_still_raises(app, caplog):
    """wh-safety-word-free-commands: a slow window lookup is not a defect,
    and an ERROR record raises a Windows popup."""
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(
                "activate_window", {"target": "x"},
                timeout_s=0.05, quiet_timeout=True,
            )

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert any(
        "timed out" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    )
