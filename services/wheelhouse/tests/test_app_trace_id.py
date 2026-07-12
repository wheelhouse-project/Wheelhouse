"""Tests for trace_id injection into IPC payloads in app.py."""

import asyncio
import pytest
from unittest.mock import MagicMock, Mock, AsyncMock

from utils.trace_context import set_trace, get_trace_id


@pytest.fixture
def mock_shm():
    shm = MagicMock()
    buf = bytearray(1024 * 64)
    shm.buf = buf
    shm.size = 1024 * 64
    shm.name = "test_shm"
    return shm


@pytest.fixture
def mock_command_ready_event():
    event = MagicMock()
    event.is_set.return_value = False
    return event


@pytest.fixture
def mock_ui_ready_event():
    event = MagicMock()
    event.is_set.return_value = True
    return event


@pytest.fixture
def mock_response_queue():
    return MagicMock()


@pytest.fixture
def app(mock_shm, mock_command_ready_event, mock_ui_ready_event, mock_response_queue):
    from app import WheelHouseApp

    with MagicMock() as shm_patch:
        app = WheelHouseApp.__new__(WheelHouseApp)
        app.shm = mock_shm
        app.command_ready_event = mock_command_ready_event
        app.ui_ready_event = mock_ui_ready_event
        app.response_queue = mock_response_queue
        app.response_futures = {}
        app.response_timeout_s = 5.0
        app._outbound_q = asyncio.Queue()
        app.demuxer_task = None
        app._sender_task = None
        app._ws_manager = None
        return app


class TestSendCommandTraceId:
    """send_command injects trace_id from ContextVar into payloads."""

    @pytest.mark.asyncio
    async def test_send_command_injects_trace_id(self, app):
        """Payload enqueued by send_command should have trace_id field."""
        set_trace("T-000042")
        await app.send_command({"action": "press", "params": {"key": "enter"}})

        payload = app._outbound_q.get_nowait()
        assert payload.get("trace_id") == "T-000042"

    @pytest.mark.asyncio
    async def test_send_command_empty_trace_id(self, app):
        """When no trace is active, trace_id should be empty string."""
        set_trace("")
        await app.send_command({"action": "press", "params": {"key": "a"}})

        payload = app._outbound_q.get_nowait()
        assert payload.get("trace_id") == ""


class TestSendRequestTraceId:
    """send_request injects trace_id from ContextVar into payloads."""

    @pytest.mark.asyncio
    async def test_send_request_injects_trace_id(self, app):
        """Payload enqueued by send_request should have trace_id field."""
        set_trace("T-000099")

        # We need to avoid waiting for the Future (it will never resolve in test).
        # Patch wait_for to raise immediately so we can inspect the queue.
        async def instant_timeout(coro, timeout):
            raise asyncio.TimeoutError()

        import unittest.mock
        with unittest.mock.patch("asyncio.wait_for", side_effect=instant_timeout):
            with pytest.raises(asyncio.TimeoutError):
                await app.send_request("get_selection", params={"format": "text"})

        payload = app._outbound_q.get_nowait()
        assert payload.get("trace_id") == "T-000099"
        assert "request_id" in payload  # Still has request_id
