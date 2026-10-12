"""A blocked microphone reaches WheelHouse as the blocked-microphone notice.

wh-blocked-mic-message acceptance items 6 and 7, google's half. The
parakeet file
(sherpa_offline_parakeet_stt_server/tests/test_blocked_microphone_notice.py)
explains why the capture handshake, and not the constructor refusal, is
the path a blocked microphone takes.

These tests drive the real handshake path, send_startup_notification.
The capture object reports setup_error equal to
MICROPHONE_BLOCKED_MESSAGE and wait_ready() returns False. The frame is
built by the real WSForwarder.send_notification, so the cause key is
read from the frame and not from the argument list of a mock.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as server_main
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


def _fail_with(setup_error):
    """Run the real send path for a microphone that never opened."""
    forwarder = _FrameRecorder()
    mic = SimpleNamespace(
        wait_ready=Mock(return_value=False), setup_error=setup_error)
    server_main.send_startup_notification(
        forwarder, None, mic, threading.Event(), threading.Lock(),
        threading.Event())
    return forwarder.frames


def test_a_blocked_microphone_sends_the_approved_sentence_and_the_cause():
    (frame,) = _fail_with(MICROPHONE_BLOCKED_MESSAGE)

    assert frame["message"] == MICROPHONE_BLOCKED_MESSAGE
    assert frame["cause"] == "microphone_blocked"
    assert frame["kind"] == "startup_failed"
    assert frame["title"] == "Google STT"


def test_any_other_capture_failure_keeps_the_prefix_and_has_no_cause():
    (frame,) = _fail_with("Microphone node failed: status=1")

    assert frame["message"] == (
        "Failed to start - transcription will not work: "
        "Microphone node failed: status=1")
    assert frame["cause"] == ""
    assert frame["kind"] == "startup_failed"


def test_a_credentials_failure_carries_no_cause():
    """The credentials branch never asks capture, so it has no
    microphone to blame."""
    forwarder = _FrameRecorder()
    mic = SimpleNamespace(
        wait_ready=Mock(return_value=False),
        setup_error=MICROPHONE_BLOCKED_MESSAGE)

    server_main.send_startup_notification(
        forwarder, "keyfile missing", mic, threading.Event(),
        threading.Lock(), threading.Event())

    (frame,) = forwarder.frames
    assert frame["cause"] == ""
    assert frame["message"].startswith("Google credentials problem")


def test_a_ready_capture_carries_no_cause():
    forwarder = _FrameRecorder()
    mic = SimpleNamespace(wait_ready=Mock(return_value=True),
                          setup_error=None)

    server_main.send_startup_notification(
        forwarder, None, mic, threading.Event(), threading.Lock(),
        threading.Event())

    (frame,) = forwarder.frames
    assert frame["kind"] == "ready"
    assert frame["cause"] == ""
