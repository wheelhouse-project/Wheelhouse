"""Google STT puts a notice on the screen when the microphone is lost and
when it comes back (wh-mic-loss-notice).

The capture sees the loss; main() owns the forwarder. So main() hands the
capture an outage_callback at its get_audio_provider call, and that
callback sends through main()'s forwarder with "Google STT", the title its
ready notice carries. The forwarder is built about 120 lines after the
capture, so the callback reads it at call time.

The run below is the real main(), taken down by a capture that never
becomes ready (the harness of test_capture_failure_ends_the_run.py). That
is the shortest honest path past the forwarder's construction, and the
callback is then called the way the capture thread would call it.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as server_main  # noqa: E402
from shared_stt.mic_notice import MIC_LOST_MESSAGE  # noqa: E402
from tests.test_capture_failure_ends_the_run import (  # noqa: E402
    _bounded_silence,
)
from tests.test_main import make_config, make_forwarder  # noqa: E402


def _run_main(*, list_devices=False):
    """Returns (factory mock, forwarder) after one real main() run."""
    args = MagicMock()
    args.list_devices = list_devices
    args.ws_host = None
    args.ws_port = None
    cfg = make_config(mic_check_seconds=0.0)

    mic = MagicMock()
    mic.wait_ready = Mock(return_value=False)
    mic.setup_error = "Device unavailable"
    mic.list_audio_devices.return_value = []
    announcement_threads = []
    mic.read.side_effect = _bounded_silence(announcement_threads)

    real_thread = threading.Thread

    def make_thread(*a, **kwargs):
        thread = real_thread(*a, **kwargs)
        if kwargs.get("target") is server_main.send_startup_notification:
            announcement_threads.append(thread)
        return thread

    forwarder = make_forwarder()

    with patch("main.load_config", return_value=(args, cfg)), \
         patch("main.get_audio_provider", return_value=mic) as factory, \
         patch("main.credentials_preflight",
               return_value=(MagicMock(), None)), \
         patch("main.SileroVAD"), \
         patch("main.SmartAGC"), \
         patch("main.UsageMetrics"), \
         patch("main.WSForwarder", return_value=forwarder), \
         patch("main.threading.Thread", side_effect=make_thread), \
         patch("main.get_startup_banner", return_value="test"):
        server_main.main()

    return factory, forwarder


def _outage_callback(factory):
    factory.assert_called_once()
    callback = factory.call_args.kwargs.get("outage_callback")
    assert callable(callback), (
        "the get_audio_provider call passed no outage_callback")
    return callback


def test_the_callback_sends_both_notices_through_the_forwarder():
    factory, forwarder = _run_main()
    callback = _outage_callback(factory)
    forwarder.send_notification.reset_mock()

    callback("lost", None)
    callback("recovered", "USB Webcam Microphone")

    assert forwarder.send_notification.call_args_list == [
        call("Google STT", MIC_LOST_MESSAGE, kind="mic_lost"),
        call("Google STT",
             "Microphone is back (USB Webcam Microphone). "
             "Speech recognition works again.",
             kind="mic_recovered"),
    ]


def test_the_overflow_callback_is_still_passed():
    """The new keyword sits beside the existing one, not in its place."""
    factory, _forwarder = _run_main()
    assert callable(factory.call_args.kwargs.get("overflow_callback"))


def test_a_list_devices_run_has_no_forwarder_and_sends_nothing():
    """--list-devices returns before the forwarder is built, so the
    callback it was given can find no forwarder and sends nothing."""
    factory, forwarder = _run_main(list_devices=True)
    callback = _outage_callback(factory)

    callback("lost", None)
    callback("recovered", None)

    forwarder.send_notification.assert_not_called()
