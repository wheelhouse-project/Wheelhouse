"""The Google provider's ready notice names the capture path it took.

wh-capture-winrt-required criterion A4. The refusal in A3 covers the
case where winsdk is absent. It cannot cover a provider that takes a
path nobody expected, which is what happened on 2026-09-05: a run
captured through PortAudio for hours and no log line said so.

Only the ready notice carries the name. The credentials failure and the
capture failure both describe a provider that cannot transcribe, and
naming a working capture backend on either would be a false statement.
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, Mock

import main as google_main


def _send(mic, startup_error=None):
    """Drive the real send path with capture answering through mic."""
    forwarder = MagicMock()
    google_main.send_startup_notification(
        forwarder, startup_error, mic, threading.Event(), threading.Lock(),
        threading.Event())
    return forwarder


def test_the_ready_notice_names_the_winrt_capture():
    mic = MagicMock()
    mic.wait_ready = Mock(return_value=True)

    forwarder = _send(mic)

    kwargs = forwarder.send_notification.call_args.kwargs
    assert kwargs["kind"] == "ready"
    assert kwargs["capture_backend"] == "winrt"


def test_the_name_comes_from_the_factory_that_built_the_capture():
    """Not a literal typed into the provider. A provider that spelled
    the name itself could report a path the factory did not build."""
    from shared_audio.capture import CAPTURE_BACKEND_NAME

    mic = MagicMock()
    mic.wait_ready = Mock(return_value=True)

    forwarder = _send(mic)

    kwargs = forwarder.send_notification.call_args.kwargs
    assert kwargs["capture_backend"] == CAPTURE_BACKEND_NAME


def test_a_capture_that_never_opened_claims_no_backend():
    """The failure notice must not name a working capture path."""
    mic = MagicMock()
    mic.wait_ready = Mock(return_value=False)
    mic.setup_error = "device denied"

    forwarder = _send(mic)

    kwargs = forwarder.send_notification.call_args.kwargs
    assert kwargs["kind"] != "ready"
    assert kwargs.get("capture_backend", "") == ""


def test_the_restart_completion_notice_can_be_ready():
    """Why the second send site matters at all. A credentials reload
    that succeeds sends kind="ready", so it is a ready notification A4
    covers, and it is not the one the tests above drive."""
    _title, _message, kind = google_main.restart_completion_notification(None)
    assert kind == "ready"


def test_the_restart_completion_send_site_passes_the_backend():
    """A source check, and it is weaker than the tests above -- it reads
    the call rather than running it. Driving that send needs the whole
    restart handler: a config reload, a credentials rebuild and a live
    forwarder. The check is here because without it the second ready
    send site has no guard at all, and the first one's tests would keep
    passing while every reload dropped the field.

    Its strength comes from the A6 mutation gate, which removes the
    argument from this site and requires this test to fail.
    """
    source = Path(google_main.__file__).read_text(encoding="utf-8")
    marker = "title, message, kind = restart_completion_notification("
    assert source.count(marker) == 1, "the restart send site moved"
    tail = source.split(marker, 1)[1]
    call = tail.split("send_notification(", 1)[1].split(")", 1)[0]
    assert "capture_backend" in call, (
        "the restart-completion ready sends no capture backend")


def test_a_credentials_failure_claims_no_backend():
    """The credentials branch never asks capture at all, so it has
    nothing true to say about which path was taken."""
    mic = MagicMock()
    mic.wait_ready = Mock(return_value=True)

    forwarder = _send(mic, startup_error="ValueError: bad key")

    kwargs = forwarder.send_notification.call_args.kwargs
    assert kwargs["kind"] != "ready"
    assert kwargs.get("capture_backend", "") == ""
