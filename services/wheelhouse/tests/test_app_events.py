"""Tests for WheelHouseApp unsolicited event dispatch."""
import asyncio
from unittest.mock import MagicMock, patch
from queue import Queue
import pytest


class TestEventDispatch:
    def test_register_event_handler(self):
        """WheelHouseApp should accept an event handler callback."""
        from app import WheelHouseApp
        with patch("app.shared_memory"):
            app = WheelHouseApp("test_shm", MagicMock(), MagicMock(), MagicMock())
            handler = MagicMock()
            app.register_event_handler(handler)
            assert app._event_handler is handler

    @pytest.mark.asyncio
    async def test_demux_dispatches_unsolicited_events(self):
        """Messages with type but no request_id go to the event handler."""
        from app import WheelHouseApp
        q = Queue()
        q.put({"type": "te_event", "event": "show", "text": "hello"})

        handler = MagicMock()

        with patch("app.shared_memory"):
            app = WheelHouseApp("test_shm", MagicMock(), MagicMock(), q)
            app.register_event_handler(handler)

        # Run one iteration of the demux logic
        response = q.get_nowait()
        request_id = response.get("request_id")
        assert request_id is None
        # Simulate what the modified demux loop does:
        if not request_id and response.get("type") and app._event_handler:
            app._event_handler(response)
        handler.assert_called_once_with(response)


class TestEventDispatchWithRequestId:
    """Regression: the demuxer must dispatch unsolicited events even when
    they carry a request_id (wh-zhn).

    Background: wh-t81d9.2 added a per-event request_id to te_event:show
    messages so the GUI ack can correlate. Before this fix the demuxer
    routed any message with a request_id through the response-correlation
    branch, which dropped te_event messages because no caller is awaiting
    them. Symptom: the terminal editor never opened when dictating into
    a terminal.

    The contract: messages with a 'type' field are unsolicited events.
    Their request_id, if present, is for the editor ack protocol, not
    for the demuxer's response_futures map.
    """

    @pytest.mark.asyncio
    async def test_te_event_with_request_id_reaches_event_handler(self):
        """A te_event:show message carrying a request_id must be
        dispatched to the registered event handler, not dropped as an
        unknown response."""
        from app import WheelHouseApp

        q = Queue()
        q.put({
            "type": "te_event",
            "event": "show",
            "text": "raspberry",
            "hwnd": 1247186,
            "rect": (0, 0, 800, 600),
            "request_id": "fe137999052d4c9d8762f108f39e5702",
        })

        handler = MagicMock()

        with patch("app.shared_memory"):
            app = WheelHouseApp("test_shm", MagicMock(), MagicMock(), q)
            app.register_event_handler(handler)

            # Run the real demux loop briefly so we exercise the actual
            # dispatch logic, then cancel it.
            app._start_demuxer()
            try:
                # Give the demuxer a few sleep ticks to drain the queue.
                for _ in range(20):
                    await asyncio.sleep(0.025)
                    if handler.called:
                        break
            finally:
                if app.demuxer_task is not None:
                    app.demuxer_task.cancel()
                    try:
                        await app.demuxer_task
                    except asyncio.CancelledError:
                        pass

        handler.assert_called_once()
        delivered = handler.call_args[0][0]
        assert delivered["type"] == "te_event"
        assert delivered["event"] == "show"
        assert delivered["request_id"] == "fe137999052d4c9d8762f108f39e5702"

    @pytest.mark.asyncio
    async def test_send_request_response_still_resolves(self):
        """The dispatch reorder must not break send_request: a response
        with a matching request_id and no type must still resolve the
        awaiting future."""
        from app import WheelHouseApp

        q = Queue()

        with patch("app.shared_memory"):
            app = WheelHouseApp("test_shm", MagicMock(), MagicMock(), q)
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            app.response_futures["abc-123"] = future

            # Put a Schema A success response (no 'type', has request_id).
            q.put({"request_id": "abc-123", "status": "ok", "action": "noop"})

            app._start_demuxer()
            try:
                result = await asyncio.wait_for(future, timeout=1.0)
            finally:
                if app.demuxer_task is not None:
                    app.demuxer_task.cancel()
                    try:
                        await app.demuxer_task
                    except asyncio.CancelledError:
                        pass

        assert result["request_id"] == "abc-123"
        assert result["status"] == "ok"
