"""A queued log frame survives a disconnect; a stale transcript does not.

wh-forwarded-log-time-order defects 2 and 3.

The outbound queue carries transcripts, lifecycle frames and forwarded log
records together. On a send failure the sender loop put the frame back at the
TAIL of that same queue and broke out of the drain loop; every path that had a
live connection then reached _clear_queue, which emptied the queue. So the
failed frame and everything queued behind it were discarded. The observable
result was LOSS, not the reordering the bead was first written against.

Losing a stale transcript is correct: WheelHouse must not receive words from
before the outage as if they were current speech. Losing a log record is not.
A capture-outage warning describes a moment that has already passed, its own
timestamp says when, and the disconnect that discarded it is often the very
event the reader is trying to explain.

So the split here is by frame type. _clear_queue keeps log frames and drops
everything else, and a log frame whose send failed waits in a single pending
slot that is read before the queue, so it goes out first on reconnect.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_stt.ws_forwarder import WSForwarder


def _log_frame(message):
    return {
        "type": "log",
        "level": "WARNING",
        "message": message,
        "source": "Parakeet",
        "timestamp": "2026-08-31T08:00:00",
        "utterance_id": 0,
        "is_partial": False,
        "trace_id": "",
    }


def _transcript_frame(text):
    return {
        "type": "final",
        "text": text,
        "utterance_id": 1,
        "is_partial": False,
        "trace_id": "",
    }


def _eos_frame():
    return {"type": "eos", "utterance_id": 1, "trace_id": ""}


def _notification_frame():
    return {
        "type": "notification",
        "title": "t",
        "message": "m",
        "kind": "k",
        "utterance_id": 0,
        "is_partial": False,
    }


def _forwarder():
    """A forwarder with a queue but no thread, for direct coroutine calls."""
    import threading

    return WSForwarder(
        host="localhost",
        port=59939,
        transcription_enabled_event=threading.Event(),
        debug=False,
    )


class TestClearQueueKeepsLogFrames:
    """Criterion 2: the keep/drop split, both halves."""

    @pytest.mark.asyncio
    async def test_clear_queue_keeps_log_frames_in_order(self):
        forwarder = _forwarder()
        forwarder._queue = asyncio.Queue()
        for message in ("first", "second", "third"):
            forwarder._queue.put_nowait(_log_frame(message))

        await forwarder._clear_queue()

        kept = []
        while not forwarder._queue.empty():
            kept.append(forwarder._queue.get_nowait())
        assert [frame["message"] for frame in kept] == [
            "first",
            "second",
            "third",
        ]

    @pytest.mark.asyncio
    async def test_clear_queue_still_drops_transcripts_and_lifecycle(self):
        """The stale-transcript flood this clear exists to stop must still go.

        Interleaved with log frames on purpose: the clear has to sort them
        apart, not decide once for the whole queue.
        """
        forwarder = _forwarder()
        forwarder._queue = asyncio.Queue()
        forwarder._queue.put_nowait(_transcript_frame("stale words"))
        forwarder._queue.put_nowait(_log_frame("kept"))
        forwarder._queue.put_nowait(_eos_frame())
        forwarder._queue.put_nowait(_notification_frame())

        await forwarder._clear_queue()

        kept = []
        while not forwarder._queue.empty():
            kept.append(forwarder._queue.get_nowait())
        assert [frame["type"] for frame in kept] == ["log"]
        assert kept[0]["message"] == "kept"


class _FakeState:
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class _FakeWebSocket:
    """One scripted connection.

    fail_on_payload: the 1-based payload send that raises. The capabilities
    frame is sent first and is never the one that fails, so the numbering
    counts only the frames the drain loop pulls off the queue.
    stop_after_payloads: set the forwarder's stop event once this many
    payloads have gone out, so the sender loop exits without a timer.
    """

    def __init__(self, forwarder, fail_on_payload=None,
                 stop_after_payloads=None):
        self.state = _FakeState.OPEN
        self.payloads = []
        self._forwarder = forwarder
        self._fail_on_payload = fail_on_payload
        self._stop_after_payloads = stop_after_payloads
        self._payload_count = 0

    async def send(self, data):
        import json

        frame = json.loads(data)
        if frame.get("type") == "capabilities":
            return
        self._payload_count += 1
        if self._payload_count == self._fail_on_payload:
            self.state = _FakeState.CLOSED
            raise ConnectionResetError("peer closed mid-drain")
        self.payloads.append(frame)
        if self._payload_count == self._stop_after_payloads:
            self._forwarder._stop_evt.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        # The command listener waits for a message that never arrives; the
        # sender loop cancels this task on its way out.
        await asyncio.Event().wait()
        raise StopAsyncIteration


class _FakeConnect:
    """Hands over one scripted connection and will not let it hang.

    When the frames a test expects never arrive, stop_after_payloads never
    fires and the drain loop waits on an empty queue forever. The watchdog
    closes the socket after idle_timeout so the loop exits and the test
    fails on its own assertion, naming the frames that did arrive, rather
    than on a bare timeout that says only that something did not finish.
    """

    def __init__(self, websocket, forwarder, idle_timeout=1.5):
        self._websocket = websocket
        self._forwarder = forwarder
        self._idle_timeout = idle_timeout
        self._watchdog = None

    async def _close_when_idle(self):
        await asyncio.sleep(self._idle_timeout)
        self._websocket.state = _FakeState.CLOSED
        self._forwarder._stop_evt.set()

    async def __aenter__(self):
        self._watchdog = asyncio.create_task(self._close_when_idle())
        return self._websocket

    async def __aexit__(self, *exc_info):
        if self._watchdog is not None:
            self._watchdog.cancel()
        return False


class _FakeWebsockets:
    """Stands in for the websockets module inside the sender loop."""

    State = _FakeState

    def __init__(self, connections, forwarder):
        self._connections = list(connections)
        self._forwarder = forwarder

    def connect(self, uri, **kwargs):
        if not self._connections:
            # Nothing scripted left: end the run rather than reconnect
            # forever. The loop treats this as a connect error.
            self._forwarder._stop_evt.set()
            raise ConnectionRefusedError("no more scripted connections")
        return _FakeConnect(self._connections.pop(0), self._forwarder)


async def _run_sender_loop(forwarder, connections):
    fake = _FakeWebsockets(connections, forwarder)
    real_import = __import__("importlib").import_module

    def _import(name, *args, **kwargs):
        if name == "websockets":
            return fake
        return real_import(name, *args, **kwargs)

    with patch("importlib.import_module", side_effect=_import):
        await asyncio.wait_for(forwarder._sender_loop(), timeout=10.0)


class TestFailedLogFrameGoesFirstOnReconnect:
    """Criterion 3: the head pending slot, and the transcript that still drops."""

    @pytest.mark.asyncio
    async def test_a_failed_log_frame_sends_first_on_reconnect(self):
        """The failed frame leads the next connection, ahead of the queue.

        Tail requeueing put it behind everything already queued, and the
        disconnect clear then discarded the whole queue including it.
        """
        forwarder = _forwarder()
        forwarder._loop = asyncio.get_running_loop()
        forwarder._queue = asyncio.Queue()
        forwarder._queue.put_nowait(_log_frame("the one that failed"))
        forwarder._queue.put_nowait(_log_frame("queued behind it"))

        first = _FakeWebSocket(forwarder, fail_on_payload=1)
        second = _FakeWebSocket(forwarder, stop_after_payloads=2)
        await _run_sender_loop(forwarder, [first, second])

        assert [frame["message"] for frame in second.payloads] == [
            "the one that failed",
            "queued behind it",
        ]

    @pytest.mark.asyncio
    async def test_a_failed_transcript_frame_drops_with_the_stale_clear(self):
        """A transcript whose send failed must NOT come back on reconnect.

        The log frame queued behind it is what proves the reconnect happened
        and drained; the transcript's absence is the assertion.

        The second connection deliberately has no stop_after_payloads: the
        failed transcript is requeued at the TAIL, so it would arrive after
        the log frame, and a run that stopped at the first payload would end
        before the frame it is looking for could appear. The watchdog closes
        the connection instead, once nothing more is coming.
        """
        forwarder = _forwarder()
        forwarder._loop = asyncio.get_running_loop()
        forwarder._queue = asyncio.Queue()
        forwarder._queue.put_nowait(_transcript_frame("stale words"))
        forwarder._queue.put_nowait(_log_frame("still here"))

        first = _FakeWebSocket(forwarder, fail_on_payload=1)
        second = _FakeWebSocket(forwarder)
        await _run_sender_loop(forwarder, [first, second])

        assert [frame["type"] for frame in second.payloads] == ["log"]
        assert second.payloads[0]["message"] == "still here"
