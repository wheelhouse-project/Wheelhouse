"""A provider with no forwarder yet can still tell WheelHouse why it quit.

wh-capture-winrt-required criterion A3.

Every STT provider builds its audio capture BEFORE it builds the
WSForwarder that carries notices to WheelHouse: parakeet main.py builds
the capture in ParakeetServer.__init__ and the forwarder a few lines
later in the same __init__; google main.py builds the capture in main()
about seventy lines before the forwarder; distil main.py has the same
shape as parakeet. The capture factory now refuses when winsdk is
missing, and the refusal has to reach the user, so the provider needs a
way to send one notice without the forwarder it has not built yet.

send_startup_failed_notice opens a forwarder that lives only for that
notice. The tests below hold it to the three things WheelHouse needs:
the notice must carry kind="startup_failed", the connection must name
its provider, and the forwarder must be stopped whether or not the
notice went out.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from shared_stt.startup_refusal import send_startup_failed_notice

REFUSAL = (
    "The speech service cannot start: the audio package winsdk is not "
    "installed. Re-run the WheelHouse installer. Developers: run "
    "bootstrap.ps1."
)


def test_sends_the_startup_failed_notice_with_the_message_it_was_given():
    """The notice carries the refusal text unchanged and kind=startup_failed.

    kind is the whole test WheelHouse applies
    (integrations/websocket_manager.py: `if kind == "startup_failed"`).
    A kind-less notice would reach the user as a plain toast and would
    never end the launcher's starting state.
    """
    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder = forwarder_class.return_value
        forwarder.is_connected = True

        delivered = send_startup_failed_notice(
            "Parakeet v3 (CPU)", REFUSAL, "localhost", 8765, "parakeet_tdt"
        )

    forwarder.send_notification.assert_called_once_with(
        "Parakeet v3 (CPU)", REFUSAL, kind="startup_failed"
    )
    assert delivered is True


def test_the_short_lived_connection_names_its_provider():
    """A connection that never declares its provider has its startup
    failure DROPPED as a signal: websocket_manager.py logs "Dropping a
    startup failure from a connection that never declared its provider"
    and records it against a guessed launch instead. The provider name
    travels in the capabilities frame WSForwarder sends on connect, so
    it has to be passed to the constructor."""
    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder_class.return_value.is_connected = True

        send_startup_failed_notice(
            "Google STT", REFUSAL, "127.0.0.1", 9001, "google_stt",
            emits_eos=True,
        )

    kwargs = forwarder_class.call_args.kwargs
    assert kwargs["provider_name"] == "google_stt"
    assert kwargs["emits_eos"] is True
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9001


def test_the_forwarder_is_stopped_after_the_notice_is_queued():
    """stop() is what delivers it. WSForwarder.send_notification only
    schedules a queue put on the sender loop, and WSForwarder.stop()
    gives already-queued frames a bounded 2.0 second chance to go out
    before it stops that loop. A process that exited without calling
    stop() would abandon the notice."""
    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder = forwarder_class.return_value
        forwarder.is_connected = True

        send_startup_failed_notice(
            "Distil-Whisper Medium (GPU)", REFUSAL, "localhost", 8765,
            "distil_medium_en",
        )

    forwarder.stop.assert_called_once()


def test_a_connection_that_never_opens_stops_the_forwarder_and_says_so():
    """No WheelHouse is listening when a developer runs the provider
    from a console. The helper must not block past its timeout, must
    not pretend the notice was delivered, and must still stop the
    forwarder -- the provider is about to exit either way."""
    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder = forwarder_class.return_value
        forwarder.is_connected = False

        delivered = send_startup_failed_notice(
            "Parakeet v3 (CPU)", REFUSAL, "localhost", 8765, "parakeet_tdt",
            connect_timeout=0.0,
        )

    assert delivered is False
    forwarder.send_notification.assert_not_called()
    forwarder.stop.assert_called_once()


def test_the_refusal_is_logged_even_when_nothing_is_listening(caplog):
    """The console run above is also the run where the log line is the
    only copy of the message the user will ever see."""
    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder_class.return_value.is_connected = False

        with caplog.at_level("ERROR", logger="shared_stt.startup_refusal"):
            send_startup_failed_notice(
                "Parakeet v3 (CPU)", REFUSAL, "localhost", 8765,
                "parakeet_tdt", connect_timeout=0.0,
            )

    assert REFUSAL in caplog.text


def test_a_forwarder_that_fails_to_start_does_not_mask_the_answer():
    """start() creates an event loop and a thread. If that fails, the
    caller is a provider in the middle of reporting a missing audio
    package; a WebSocket traceback would replace the one message the
    user can act on with one they cannot."""
    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder = forwarder_class.return_value
        forwarder.start = MagicMock(side_effect=RuntimeError("no event loop"))

        delivered = send_startup_failed_notice(
            "Parakeet v3 (CPU)", REFUSAL, "localhost", 8765, "parakeet_tdt"
        )

    assert delivered is False


def test_a_forwarder_that_fails_to_stop_does_not_mask_the_answer():
    """stop() joins a thread and touches an event loop. A failure there
    must not turn an exit that was about to report a missing audio
    package into a traceback about a WebSocket."""
    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder = forwarder_class.return_value
        forwarder.is_connected = True
        forwarder.stop = MagicMock(side_effect=OSError("loop already closed"))

        delivered = send_startup_failed_notice(
            "Parakeet v3 (CPU)", REFUSAL, "localhost", 8765, "parakeet_tdt"
        )

    assert delivered is True


def test_a_forwarder_that_cannot_be_built_does_not_mask_the_answer():
    """The construction is the third seam, and it is the one that used
    to escape: the caller is inside an except handler on its way to
    sys.exit, so an exception raised while building the forwarder would
    replace the refusal with a WebSocket traceback AND skip the exit
    that follows the call, leaving a provider running without a
    microphone -- the exact outcome this bead exists to prevent."""
    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder_class.side_effect = RuntimeError("cannot build a forwarder")

        delivered = send_startup_failed_notice(
            "Parakeet v3 (CPU)", REFUSAL, "localhost", 8765, "parakeet_tdt"
        )

    assert delivered is False
