"""Parakeet's ready notice names the capture path it took.

wh-capture-winrt-required criterion A4. The refusal in A3 covers the
case where winsdk is absent. It cannot cover a provider that takes a
path nobody expected, which is what happened on 2026-09-05: the run that
started at 15:25 captured through PortAudio for hours and no log line
said so. Naming the path at ready is what makes that visible.

Only the ready notice carries the name. A failure notice that claimed a
working WinRT capture would be a false statement about a provider that
has no microphone.
"""
from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest

import main as parakeet_main


@pytest.fixture
def server():
    """A ParakeetServer with the engine, forwarder and capture stubbed.

    Same shape as the fixture in test_startup_readiness.py: the engine
    needs model files, the forwarder binds a websocket, and the capture
    opens the real microphone.
    """
    capture = MagicMock()
    capture.get_stats = Mock(return_value={
        'captured': 0, 'drops': 0, 'qsize': 0, 'max_q': 0})
    with patch.object(parakeet_main, 'SherpaOfflineEngine'), \
         patch.object(parakeet_main, 'WSForwarder'), \
         patch.object(parakeet_main, 'get_audio_provider',
                      return_value=capture):
        built = parakeet_main.ParakeetServer(
            model_config={'model_path': 'C:/unused', 'use_gpu': False},
            engine_config={},
        )
    built.forwarder = MagicMock()
    built.running = True
    return built


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
