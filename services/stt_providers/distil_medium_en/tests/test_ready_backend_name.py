"""Distil-Whisper's ready notice names the capture path it took.

wh-capture-winrt-required criterion A4. This provider is the reason the
criterion matters most: before A1 it had no winsdk dependency at all, so
it could never take the WinRT path, and nothing anywhere said which path
it did take. Naming the path at ready is what would have shown that.

Only the ready notice carries the name. A failure notice that claimed a
working WinRT capture would be a false statement about a provider that
has no microphone.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, Mock

import pytest

from main import DistilMediumServer


@pytest.fixture
def server():
    """A DistilMediumServer shell with only the startup collaborators.

    Same shape as the fixture in test_startup_readiness.py: __new__ with
    no __init__ needs no engine and no model files.
    """
    s = DistilMediumServer.__new__(DistilMediumServer)
    s.forwarder = MagicMock()
    s.audio_capture = MagicMock()
    s.running = True
    s._notification_lock = threading.Lock()
    return s


def test_the_ready_notice_names_the_winrt_capture(server):
    server.audio_capture.wait_ready = Mock(return_value=True)

    server._announce_capture_outcome()

    kwargs = server.forwarder.send_notification.call_args.kwargs
    assert kwargs["kind"] == "ready"
    assert kwargs["capture_backend"] == "winrt"


def test_the_name_comes_from_the_factory_that_built_the_capture(server):
    """Not a literal typed into the provider. A provider that spelled
    the name itself could report a path the factory did not build."""
    from shared_audio.capture import CAPTURE_BACKEND_NAME

    server.audio_capture.wait_ready = Mock(return_value=True)

    server._announce_capture_outcome()

    kwargs = server.forwarder.send_notification.call_args.kwargs
    assert kwargs["capture_backend"] == CAPTURE_BACKEND_NAME


def test_a_capture_that_never_opened_claims_no_backend(server):
    """The failure notice must not name a working capture path. This
    provider has no microphone, and saying "winrt" here would tell an
    operator reading wheelhouse.log that the WinRT path was in use."""
    server.audio_capture.wait_ready = Mock(return_value=False)
    server.audio_capture.setup_error = "device denied"

    server._announce_capture_outcome()

    kwargs = server.forwarder.send_notification.call_args.kwargs
    assert kwargs["kind"] != "ready"
    assert kwargs.get("capture_backend", "") == ""
