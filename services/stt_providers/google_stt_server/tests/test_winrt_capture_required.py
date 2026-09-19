"""Google STT refuses to start when the WinRT capture cannot be built.

wh-capture-winrt-required criterion A3. David ruled on 2026-09-05: "The
rule is we always use WinRT." No fallback and no degraded start -- a
provider that runs without a microphone looks healthy and transcribes
silence.

The absence of winsdk is simulated at its real source: WINRT_AUDIO_AVAILABLE
in shared_audio/capture/factory.py is the flag the factory reads, so
patching it False drives the real factory down the real refusal path
rather than asserting against a raise the test invented.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_main import make_config  # noqa: E402

# The wording David approved on 2026-09-05, spelled out here rather than
# imported: a test that reads the constant it is checking cannot notice
# the constant changing.
REFUSAL = (
    "The speech service cannot start: the audio package winsdk is not "
    "installed. Re-run the WheelHouse installer. Developers: run "
    "bootstrap.ps1."
)


def _args():
    args = MagicMock()
    args.list_devices = False
    args.ws_host = None
    args.ws_port = None
    return args


@patch("shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE", False)
@patch("main.load_config")
def test_main_sends_the_refusal_and_returns_nonzero(mock_load_config):
    """The user is told why, and the process does not stay up.

    main() is the whole entry point: `sys.exit(main())` is the last line
    of the module, so a nonzero return IS the nonzero exit.
    """
    mock_load_config.return_value = (_args(), make_config())

    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder = forwarder_class.return_value
        forwarder.is_connected = True

        from main import main
        result = main()

    forwarder.send_notification.assert_called_once_with(
        "Google STT", REFUSAL, kind="startup_failed"
    )
    assert result != 0


@patch("shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE", False)
@patch("main.load_config")
def test_the_refusal_connection_declares_the_google_provider(mock_load_config):
    """WheelHouse drops a startup failure from a connection that never
    named its provider, so the throwaway connection has to name it and
    has to use the port the real forwarder would have used."""
    cfg = make_config(ws_host="127.0.0.1", ws_port=5099)
    mock_load_config.return_value = (_args(), cfg)

    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder_class.return_value.is_connected = True

        from main import main
        main()

    kwargs = forwarder_class.call_args.kwargs
    assert kwargs["provider_name"] == "google_stt"
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 5099


@patch("shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE", False)
@patch("main.load_config")
def test_list_devices_refuses_on_stderr_and_builds_no_forwarder(
        mock_load_config, capsys):
    """--list-devices builds the same capture, and meets the same
    refusal -- but it must NOT send a startup_failed notice.

    WheelHouse never launched a --list-devices run, so a notice from one
    is addressed to nobody. It cannot be ignored either: the notice
    names the provider, so WheelHouse reads it against whatever launch
    of that provider is live and ends a session the person never
    touched. The person who typed the flag is reading the console, so
    the refusal goes to stderr and the process exits nonzero.

    Google has no run_list_devices function: the flag is handled inside
    main(), a few lines BELOW the refusal both paths share. So the
    refusal site itself has to know which run it is on, which is why
    this test pins the branch rather than a separate function.
    """
    args = _args()
    args.list_devices = True
    mock_load_config.return_value = (args, make_config())

    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        from main import main
        result = main()

    assert REFUSAL in capsys.readouterr().err
    forwarder_class.assert_not_called()
    assert result != 0


@patch("shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE", False)
@patch("main.load_config")
def test_forwarding_turned_off_means_no_refusal_connection(
        mock_load_config, capsys):
    """A developer who turned forwarding off gets no connection opened.

    The real forwarder is built only inside `if cfg.forward_ws:`
    (main.py), and every notice google sent before this branch rode that
    forwarder, so all of them inherited the gate. The refusal notice does
    not ride it: shared_stt/startup_refusal.py opens a throwaway
    forwarder of its own, on purpose, because the capture is built before
    the real forwarder exists. That is what makes the gate easy to lose,
    and losing it means a run with forwarding off still reaches out to
    WheelHouse and can end a live session (wh-capture-winrt-required.1.1).

    The refusal itself is not silenced -- it goes to stderr, the same
    place --list-devices puts it, because the person watching a
    forwarding-off run is watching the console.
    """
    mock_load_config.return_value = (_args(), make_config(forward_ws=False))

    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        from main import main
        result = main()

    forwarder_class.assert_not_called()
    assert REFUSAL in capsys.readouterr().err
    assert result != 0


@patch("shared_audio.capture.factory.WINRT_AUDIO_AVAILABLE", False)
@patch("main.load_config")
def test_the_capture_refusal_exits_with_the_refusal_code(mock_load_config):
    """3, not 1, so the supervisor does not start the provider again.

    A normal WheelHouse launch does not run main.py: it runs
    launcher.py, and run_launcher() in shared_stt/launcher.py restarts
    any nonzero exit reached inside crash_threshold_s (15 seconds), up
    to max_crashes (3). This refusal is reached in well under a second,
    so exit 1 was read as a crash and the provider was started three
    times over, each attempt refusing again against a launch WheelHouse
    had already recorded as stopped (wh-capture-winrt-required.1.5).

    `sys.exit(main())` is the last line of the module, so this returned
    code IS the process exit status.

    The literal rather than the constant, for the same reason the
    approved wording above is spelled out: this number is read back off
    a returncode by a SEPARATE process, and a test that read the same
    constant on both sides could not notice the value moving onto one
    another failure already uses.
    """
    mock_load_config.return_value = (_args(), make_config())

    with patch("shared_stt.startup_refusal.WSForwarder") as forwarder_class:
        forwarder_class.return_value.is_connected = True

        from main import main
        result = main()

    assert result == 3
