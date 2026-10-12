"""A blocked microphone reaches WheelHouse as the blocked-microphone notice.

wh-blocked-mic-message acceptance items 6 and 7, distil's half. The
parakeet file
(sherpa_offline_parakeet_stt_server/tests/test_blocked_microphone_notice.py)
explains why the capture handshake, and not the constructor refusal, is
the path a blocked microphone takes.

These tests drive the real handshake path, _announce_capture_outcome.
The capture object reports setup_error equal to
MICROPHONE_BLOCKED_MESSAGE and wait_ready() returns False. The frame is
built by the real WSForwarder.send_notification, so the cause key is
read from the frame and not from the argument list of a mock.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from main import DistilMediumServer
from shared_audio.capture.winrt_capture import MICROPHONE_BLOCKED_MESSAGE
from shared_stt.ws_forwarder import WSForwarder


class _FrameRecorder:
    """A forwarder that builds frames with WSForwarder's own code.

    send_notification is borrowed unbound, so the frame is exactly the
    dict the real forwarder would queue. The queue's put() records the
    dict and returns None; run_coroutine_threadsafe then rejects None
    and the real method swallows that error, as it does for a closed
    loop.
    """

    is_connected = True
    uri = "ws://localhost:8765"

    def __init__(self):
        self.frames = []
        self._loop = object()
        self._queue = SimpleNamespace(put=self.frames.append)

    send_notification = WSForwarder.send_notification


@pytest.fixture
def server():
    """A DistilMediumServer shell with only the startup collaborators."""
    s = DistilMediumServer.__new__(DistilMediumServer)
    s.forwarder = _FrameRecorder()
    s.audio_capture = MagicMock()
    s.running = True
    s._notification_lock = threading.Lock()
    return s


def _fail_with(server, setup_error):
    server.audio_capture.wait_ready = Mock(return_value=False)
    server.audio_capture.setup_error = setup_error
    server._announce_capture_outcome()
    return server.forwarder.frames


def test_a_blocked_microphone_sends_the_approved_sentence_and_the_cause(
        server):
    (frame,) = _fail_with(server, MICROPHONE_BLOCKED_MESSAGE)

    assert frame["message"] == MICROPHONE_BLOCKED_MESSAGE
    assert frame["cause"] == "microphone_blocked"
    assert frame["kind"] == "startup_failed"


def test_any_other_capture_failure_keeps_the_prefix_and_has_no_cause(
        server):
    (frame,) = _fail_with(server, "Microphone node failed: status=1")

    assert frame["message"] == (
        "Failed to start - transcription will not work: "
        "Microphone node failed: status=1")
    assert frame["cause"] == ""
    assert frame["kind"] == "startup_failed"
