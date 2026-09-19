"""Protect existing send_request integration; no known dev failure is fixed."""

import asyncio
from unittest.mock import MagicMock, Mock, patch

import pytest

from app import IpcDeliveryError, WheelHouseApp


@pytest.fixture
def app():
    # Construct the real transport without opening live shared memory or tasks.
    with patch("app.shared_memory.SharedMemory") as shared_memory:
        shared_memory.return_value.buf = bytearray(64 * 1024)
        return WheelHouseApp("synthetic-merge-coverage", MagicMock(), MagicMock(), MagicMock())


@pytest.mark.asyncio
async def test_registration_exists_before_actual_enqueue(app, monkeypatch):
    callback = Mock()
    put = app._outbound_q.put_nowait
    observed = []
    response = {"status": "done"}

    def observe_enqueue(queued):
        request_id = queued["request_id"]
        observed.append((request_id, app._late_response_callbacks.get(request_id)))
        put(queued)
        app.response_futures[request_id].set_result(response)

    monkeypatch.setattr(app._outbound_q, "put_nowait", observe_enqueue)
    assert await app.send_request("get_selection", on_late_response=callback) is response
    assert len(observed) == 1
    request_id, registration = observed[0]
    assert registration is not None, "late registration must precede the enqueue attempt"
    assert registration.callback is callback, "the enqueued request must retain its own callback"
    assert registration.armed is False
    assert app._outbound_q.get_nowait()["request_id"] == request_id
    assert not app._late_response_callbacks
    assert not app.response_futures
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_actual_queue_full_refusal_removes_only_its_registration(app, monkeypatch):
    callback = Mock()
    unrelated = Mock()
    app._register_late_response("unrelated", unrelated, 60.0)
    app._outbound_q = asyncio.Queue(maxsize=1)
    existing = object()
    app._outbound_q.put_nowait(existing)
    put = app._outbound_q.put_nowait
    attempted = []
    refusals = []

    def observe_refusal(queued):
        request_id = queued["request_id"]
        attempted.append((request_id, app._late_response_callbacks.get(request_id)))
        try:
            put(queued)
        except asyncio.QueueFull as exc:
            refusals.append(exc)
            raise

    monkeypatch.setattr(app._outbound_q, "put_nowait", observe_refusal)
    with pytest.raises(IpcDeliveryError) as caught:
        await app.send_request("get_selection", on_late_response=callback)
    assert len(attempted) == len(refusals) == 1
    assert caught.value.__context__ is refusals[0], "refusal must originate in the real bounded queue"
    request_id, registration = attempted[0]
    assert registration is not None and registration.callback is callback
    assert request_id not in app._late_response_callbacks, "a refused request must lose its registration"
    assert app._late_response_callbacks["unrelated"].callback is unrelated
    assert app._outbound_q.qsize() == 1
    assert app._outbound_q.get_nowait() is existing
    assert not app.response_futures
    assert not app._request_trace_meta
    callback.assert_not_called()
    unrelated.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_abandons_the_queued_entry_and_removes_its_registration(app, monkeypatch):
    callback = Mock()
    unrelated = Mock()
    app._register_late_response("unrelated", unrelated, 60.0)
    enqueued = asyncio.Event()
    observed = []
    put = app._outbound_q.put_nowait

    def observe_enqueue(queued):
        put(queued)
        observed.append(queued)
        enqueued.set()

    monkeypatch.setattr(app._outbound_q, "put_nowait", observe_enqueue)
    caller = asyncio.create_task(app.send_request("get_selection", on_late_response=callback))
    try:
        await asyncio.wait_for(enqueued.wait(), timeout=1.0)
        assert len(observed) == app._outbound_q.qsize() == 1
        queued = observed[0]
        request_id = queued["request_id"]
        assert app._late_response_callbacks[request_id].callback is callback
        assert not caller.done()
        assert not queued.get("_delivery_abandoned", False)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert app._outbound_q.get_nowait() is queued
        assert queued.get("_delivery_abandoned") is True, "cancellation must abandon the actual queued entry"
        assert request_id not in app._late_response_callbacks, "cancellation must remove its late registration"
        assert app._late_response_callbacks["unrelated"].callback is unrelated
        assert not app.response_futures
        assert not app._request_trace_meta
        callback.assert_not_called()
        unrelated.assert_not_called()
    finally:
        if not caller.done():
            caller.cancel()
        try:
            await caller
        except asyncio.CancelledError:
            pass
