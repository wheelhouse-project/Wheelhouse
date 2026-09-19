"""Parakeet refuses to start when the WinRT capture cannot be built.

wh-capture-winrt-required criterion A3. David ruled on 2026-09-05: "The
rule is we always use WinRT." No fallback and no degraded start -- a
provider that runs without a microphone looks healthy and transcribes
silence.

The absence of winsdk is simulated at its real source: WINRT_AUDIO_AVAILABLE
in shared_audio/capture/factory.py is the flag the factory reads, so
patching it False drives the real factory down the real refusal path
rather than asserting against a raise the test invented.

The capture is built in ParakeetServer.__init__ BEFORE the WSForwarder
a few lines below it, so the refusal cannot use self.forwarder: there
is none. It goes out over a forwarder opened for that one notice.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import main as parakeet_main

# The wording David approved on 2026-09-05, spelled out here rather than
# imported: a test that reads the constant it is checking cannot notice
# the constant changing.
REFUSAL = (
    "The speech service cannot start: the audio package winsdk is not "
    "installed. Re-run the WheelHouse installer. Developers: run "
    "bootstrap.ps1."
)


def _build_server(ws_host="localhost", ws_port=8765, use_gpu=False):
    """Construct a ParakeetServer with winsdk simulated as absent.

    The engine and the real forwarder are stubbed for the same reasons
    as the fixture in test_startup_readiness.py: loading Parakeet needs
    the model files and the forwarder binds a websocket. Neither is
    reached on this path, and stubbing them keeps the test honest about
    what it is measuring if that ever changes.
    """
    return parakeet_main.ParakeetServer(
        model_config={"model_path": "C:/unused", "use_gpu": use_gpu},
        engine_config={},
        ws_host=ws_host,
        ws_port=ws_port,
    )


def test_construction_sends_the_refusal_and_exits_nonzero():
    """The user is told why, and the process does not stay up.

    sys.exit inside __init__ raises SystemExit, which the __main__ block
    does not catch (its try covers server.start(), not the construction
    above it), so the interpreter exits with this code.
    """
    with patch("shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE", False), \
         patch.object(parakeet_main, "SherpaOfflineEngine"), \
         patch.object(parakeet_main, "WSForwarder"), \
         patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder = forwarder_class.return_value
        forwarder.is_connected = True

        with pytest.raises(SystemExit) as exit_info:
            _build_server()

    forwarder.send_notification.assert_called_once_with(
        "Parakeet v3 (CPU)", REFUSAL, kind="startup_failed"
    )
    assert exit_info.value.code != 0


def test_the_refusal_connection_declares_the_parakeet_provider():
    """WheelHouse drops a startup failure from a connection that never
    named its provider, so the throwaway connection has to name it and
    has to use the port the real forwarder would have used."""
    with patch("shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE", False), \
         patch.object(parakeet_main, "SherpaOfflineEngine"), \
         patch.object(parakeet_main, "WSForwarder"), \
         patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder_class.return_value.is_connected = True

        with pytest.raises(SystemExit):
            _build_server(ws_host="127.0.0.1", ws_port=5099)

    kwargs = forwarder_class.call_args.kwargs
    assert kwargs["provider_name"] == "parakeet_tdt"
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 5099


def test_no_engine_is_loaded_once_the_capture_has_refused():
    """Refusing early is the point: loading the Parakeet model takes
    seconds and pins a GPU for a process that cannot transcribe."""
    with patch("shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE", False), \
         patch.object(parakeet_main, "SherpaOfflineEngine") as engine_class, \
         patch.object(parakeet_main, "WSForwarder"), \
         patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder_class.return_value.is_connected = True

        with pytest.raises(SystemExit):
            _build_server()

    engine_class.assert_not_called()


def test_list_devices_refuses_on_stderr_and_builds_no_forwarder(capsys):
    """--list-devices builds the same capture, and meets the same
    refusal -- but it must NOT send a startup_failed notice.

    WheelHouse never launched a --list-devices run, so a notice from one
    is addressed to nobody. It cannot be ignored either: the notice
    names the provider, so WheelHouse reads it against whatever launch
    of that provider is live and ends a session the person never
    touched. The person who typed the flag is reading the console, so
    the refusal goes to stderr and the process exits nonzero.

    The earlier version of this test asserted the opposite -- that the
    notice went out -- and it was right about the code at the time. The
    ruling that changed it came after A4.
    """
    with patch("shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE", False), \
         patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        code = parakeet_main.run_list_devices()

    assert REFUSAL in capsys.readouterr().err
    forwarder_class.assert_not_called()
    assert code != 0


def test_list_devices_still_lists_devices_when_capture_loads():
    """The refusal path must not have replaced the thing the flag is
    for."""
    mic = MagicMock()
    mic.list_audio_devices.return_value = [{"index": 0, "name": "Test Mic"}]

    with patch.object(parakeet_main, "get_audio_provider", return_value=mic):
        code = parakeet_main.run_list_devices()

    mic.list_audio_devices.assert_called_once()
    assert code == 0


def test_the_constructor_refusal_exits_with_the_refusal_code():
    """3, not 1, so the supervisor does not start the provider again.

    A normal WheelHouse launch does not run main.py: it runs
    launcher.py, and run_launcher() in shared_stt/launcher.py restarts
    any nonzero exit reached inside crash_threshold_s (15 seconds), up
    to max_crashes (3). This refusal is reached in well under a second,
    so exit 1 was read as a crash and the provider was started three
    times over, each attempt refusing again against a launch WheelHouse
    had already recorded as stopped (wh-capture-winrt-required.1.5).

    The literal rather than the constant, for the same reason the
    approved wording above is spelled out: this number is a process exit
    status that a SEPARATE process reads back off a returncode, and a
    test that read the same constant on both sides could not notice the
    value moving onto one another failure already uses.
    """
    with patch("shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE", False), \
         patch.object(parakeet_main, "SherpaOfflineEngine"), \
         patch.object(parakeet_main, "WSForwarder"), \
         patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder_class.return_value.is_connected = True

        with pytest.raises(SystemExit) as exit_info:
            _build_server()

    assert exit_info.value.code == 3
