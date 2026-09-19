"""A capture failure after the client builds ends the run, nonzero.

wh-capture-winrt-required criterion A9. The parakeet file
(sherpa_offline_parakeet_stt_server/tests/test_capture_failure_ends_the_run.py)
carries the fuller reasoning; this provider had the same defect in a
different shape.

The defect. A3 covers the capture that never gets BUILT: the factory
refuses, the provider sends the refusal and quits. It cannot cover the
capture that builds and then fails to START -- an AudioGraph that will
not open, a device taken by another process, a permission denied after
the client is already built. That path reached
send_startup_notification, sent the startup_failed notice, and returned
with nothing that could reach main()'s loop, so `while not stop:` kept
turning on a capture that will never produce a chunk and the provider
ran deaf for as long as the machine stayed up.

Google's mechanism differs from the other two providers, and this is
where that difference lives. Parakeet and distil keep the run state on
the server object, so their announcement writes self.running and their
own loop reads it. Google has no server object: main() keeps `stop` as
a LOCAL, and the announcement is a module-level function on a daemon
thread, which cannot assign to it. So the answer travels back as a
threading.Event -- the same device google already uses for the shutdown
read (`shutting_down`) -- which the loop condition reads and main()'s
return statement turns into the exit code. The exit is a returned code
rather than sys.exit for the same reason: `sys.exit(main())` is the last
line of the module, so a nonzero return IS the nonzero exit.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as server_main  # noqa: E402
from tests.test_main import make_config, make_forwarder  # noqa: E402


def _mic(ready, setup_error=None):
    """A capture provider that answers the handshake and nothing else."""
    return SimpleNamespace(
        wait_ready=Mock(return_value=ready),
        setup_error=setup_error,
    )


def _forwarder_that_connects_after(asks, uri="ws://localhost:8765"):
    """A forwarder whose connection comes up on the `asks`-th question.

    WSForwarder.is_connected is a PROPERTY that reads the sender
    thread's connection flag, so the stand-in is a property too: the
    refusal path has to ask, and a plain attribute would record no
    asking at all. The answers are kept so a test can say how many times
    it was asked and what it was told -- `asks=0` is a connection that
    is already up, and a number larger than the wait can poll is a
    connection that never opens.
    """
    forwarder = make_forwarder()
    forwarder.uri = uri
    answers = []

    def is_connected():
        connected = len(answers) >= asks
        answers.append(connected)
        return connected

    type(forwarder).is_connected = PropertyMock(side_effect=is_connected)
    forwarder.connection_answers = answers
    return forwarder


def _send(startup_error, mic, capture_failed, shutting_down=None,
          forwarder=None):
    """Drive the real sender with a real event, the way main() does."""
    forwarder = MagicMock() if forwarder is None else forwarder
    server_main.send_startup_notification(
        forwarder, startup_error, mic,
        shutting_down if shutting_down is not None else threading.Event(),
        threading.Lock(), capture_failed)
    return forwarder


class TestTheRunIsMarkedFailed:
    """The event is the only route from the announcement thread back to
    main()'s local `stop`."""

    def test_a_failed_capture_marks_the_run_failed(self):
        capture_failed = threading.Event()
        _send(None, _mic(False, "Device unavailable"), capture_failed)
        assert capture_failed.is_set()

    def test_a_working_capture_leaves_the_run_alive(self):
        """The exit belongs to the failure only. A run marked failed
        after a healthy handshake would take the service down on every
        launch."""
        capture_failed = threading.Event()
        _send(None, _mic(True), capture_failed)
        assert not capture_failed.is_set()

    def test_a_credentials_failure_does_not_mark_the_run_failed(self):
        """A9 is about capture. The credentials failure is a different
        event with its own recovery: the restart handler reloads the
        config, rebuilds the client, and sends a fresh completion notice
        (wh-google-creds-file-picker.1.12). Ending the run here would
        take that away, and capture is not even asked in this case --
        capture_handshake returns None rather than False."""
        capture_failed = threading.Event()
        _send("keyfile missing", _mic(True), capture_failed)
        assert not capture_failed.is_set()

    def test_an_ordinary_stop_does_not_mark_the_run_failed(self):
        """A user-requested stop is not a failure.

        mic.stop() is what releases wait_ready(), and it releases it as
        False -- the same answer a dead microphone gives
        (shared_audio/capture/winrt_capture.py:260-298, :620, :356). The
        shutdown read is what tells the two apart, and it already
        suppresses the notice; the run must not be marked failed either,
        or a stop the user asked for would be reported as a crash.
        """
        capture_failed = threading.Event()
        stopping = threading.Event()
        stopping.set()
        forwarder = _send(None, _mic(False, None), capture_failed, stopping)
        forwarder.send_notification.assert_not_called()
        assert not capture_failed.is_set()


class TestTheNoticeStillGoesOutFirst:
    """The user is told why before anything is torn down."""

    def test_the_notice_is_sent_before_the_run_is_marked_failed(self):
        capture_failed = threading.Event()
        observed = []
        forwarder = MagicMock()
        forwarder.send_notification = Mock(
            side_effect=lambda *a, **k: observed.append(
                capture_failed.is_set()))

        server_main.send_startup_notification(
            forwarder, None, _mic(False, "Device unavailable"),
            threading.Event(), threading.Lock(), capture_failed)

        assert observed == [False], (
            "the run was marked failed before the failure notice went out")

    def test_the_notice_is_still_the_startup_failed_one(self):
        """A9 adds to criterion 2 rather than replacing it."""
        forwarder = _send(None, _mic(False, "Device unavailable"),
                          threading.Event())
        args, kwargs = forwarder.send_notification.call_args
        assert kwargs["kind"] == "startup_failed"
        assert "Device unavailable" in args[1]


def _bounded_silence(announcement_threads, limit=500):
    """A read() that answers None -- a capture that will never produce a
    chunk -- and then gives up.

    Wait for the real announcement thread before spending the read budget.
    Immediate None reads otherwise race its scheduling, unlike a real
    microphone's timed read. Joining the thread also waits past the notice
    being queued: the failure event is set only after the connection wait.

    Both bounds matter: a stuck announcement fails its join, and a provider
    that ignores the completed announcement still exhausts the read limit.
    """
    reads = []

    def read(timeout=None):
        if not reads:
            assert len(announcement_threads) == 1, "no announcement thread"
            announcement = announcement_threads[0]
            announcement.join(10)
            assert not announcement.is_alive(), "announcement did not finish"
        reads.append(1)
        if len(reads) > limit:
            raise AssertionError(
                "the audio loop kept running after the capture failed - "
                "the provider is deaf and still up")
        return None

    return read


class TestMainEndsTheRun:
    """The whole path, through the real main(): the announcement runs on
    its own daemon thread the way it does in production, and the loop
    and the return code are the real ones."""

    @staticmethod
    def _run_main_with_a_failed_capture(forwarder=None):
        """Returns (exit code, mic, forwarder).

        credentials_preflight is stubbed to succeed because A9 is about
        capture: an unstubbed preflight on a machine with no key file
        takes the credentials branch, where capture is never asked at
        all.
        """
        args = MagicMock()
        args.list_devices = False
        args.ws_host = None
        args.ws_port = None
        cfg = make_config(mic_check_seconds=0.0)

        mic = MagicMock()
        mic.wait_ready = Mock(return_value=False)
        mic.setup_error = "Device unavailable"
        announcement_threads = []
        mic.read.side_effect = _bounded_silence(announcement_threads)

        real_thread = threading.Thread

        def make_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            if kwargs.get("target") is server_main.send_startup_notification:
                announcement_threads.append(thread)
            return thread

        forwarder = make_forwarder() if forwarder is None else forwarder
        notice_sent = threading.Event()
        forwarder.send_notification.side_effect = \
            lambda *a, **k: notice_sent.set()

        with patch("main.load_config", return_value=(args, cfg)), \
             patch("main.get_audio_provider", return_value=mic), \
             patch("main.credentials_preflight",
                   return_value=(MagicMock(), None)), \
             patch("main.SileroVAD"), \
             patch("main.SmartAGC"), \
             patch("main.UsageMetrics"), \
             patch("main.WSForwarder", return_value=forwarder), \
             patch("main.threading.Thread", side_effect=make_thread), \
             patch("main.get_startup_banner", return_value="test"):
            result = server_main.main()

        assert notice_sent.is_set(), "the failure notice never went out"
        return result, mic, forwarder

    def test_main_returns_nonzero_after_a_capture_failure(self):
        """`sys.exit(main())` is the last line of the module, so a
        nonzero return IS the nonzero exit."""
        result, _mic_used, _forwarder = self._run_main_with_a_failed_capture()
        assert result != 0

    def test_main_returns_the_refusal_code_after_a_capture_failure(self):
        """3, not 1, so the supervisor does not start the provider
        again.

        A normal WheelHouse launch does not run main.py: it runs
        launcher.py, and run_launcher() in shared_stt/launcher.py
        restarts any nonzero exit reached inside crash_threshold_s
        (15 seconds), up to max_crashes (3). A machine whose client
        builds quickly reaches this refusal well inside that window, so
        exit 1 was read as a crash and the provider was started three
        times over, each attempt refusing again against a launch
        WheelHouse had already recorded as stopped
        (wh-capture-winrt-required.1.5).

        The literal rather than the constant: this number is read back
        off a returncode by a SEPARATE process, and a test that read the
        same constant on both sides could not notice the value moving
        onto one another failure already uses.
        """
        result, _mic_used, _forwarder = self._run_main_with_a_failed_capture()
        assert result == 3

    def test_the_teardown_still_runs(self):
        """Exiting with the microphone still open and the forwarder
        still connected leaves the device held by a process that is
        going away. main()'s finally is what runs both, so an exit that
        skipped it would be worse than staying up."""
        _result, mic, forwarder = self._run_main_with_a_failed_capture()
        mic.stop.assert_called_once()
        forwarder.stop.assert_called_once()

    @pytest.mark.parametrize(
        "release_window_expired", [False, True],
        ids=["released-on-read", "release-window-expired"],
    )
    def test_a_descheduled_announcement_does_not_exhaust_the_read_bound(
            self, release_window_expired):
        """Force the sender to wait until the fixture yields to its thread.

        The old 500 immediate reads fail before permitting the sender to
        run. No wall-clock delay or lucky scheduler interleaving is needed.
        Expiring the artificial delay must also run the real sender; silently
        skipping it would blame a correct provider for the fixture's timeout.
        """
        release = threading.Event()
        wait_for_release = Mock(wraps=release.wait)
        if release_window_expired:
            # Model the delay expiring before the main thread gets to read().
            wait_for_release.return_value = False
        threads = []
        real_thread = threading.Thread
        sender = server_main.send_startup_notification

        class DelayedAnnouncement(real_thread):
            def run(self):
                # The deadline bounds the artificial delay, not the sender.
                wait_for_release(10)
                super().run()

            def join(self, timeout=None):
                release.set()
                return super().join(timeout)

        def make_thread(*args, **kwargs):
            if kwargs.get("target") is sender:
                thread = DelayedAnnouncement(*args, **kwargs)
                threads.append(thread)
                return thread
            return real_thread(*args, **kwargs)

        try:
            with patch("main.threading.Thread", side_effect=make_thread):
                result, _, _ = self._run_main_with_a_failed_capture()
            assert result == 3
        finally:
            release.set()
            for thread in threads:
                thread.join(10)
                assert not thread.is_alive(), "announcement did not finish"


class TestTheRefusalReachesWheelhouse:
    """The queued notice is given a connection before anything stops.

    wh-capture-winrt-required.1.4. send_notification only schedules a
    Queue.put on the forwarder's sender loop; it sends no frame and
    waits for nothing. WSForwarder.stop() drains that queue only when a
    connection is already live -- otherwise it sets its stop event at
    once, and the sender loop's `while capabilities_sent and not
    self._stop_evt.is_set():` never reads the queue again. The A9 path
    queued the refusal and ended the run with no wait between them, so a
    capture failure that landed before the provider's first handshake
    finished discarded the one message the user could act on, even when
    WheelHouse accepted the connection a fraction of a second later.

    The wait is the one already written for the constructor refusal
    (shared_stt/startup_refusal.py, CONNECT_TIMEOUT_S = 5 seconds), not
    a second copy: that path had the same problem and its own docstring
    describes it.

    Where the wait sits matters as much as its length. It is taken under
    the notification lock, which begin_shutdown() also takes in main()'s
    finally before forwarder.stop() runs, so the teardown cannot begin
    while the notice is still waiting for a connection.
    """

    def test_a_live_connection_is_confirmed_before_the_refusal_ends_the_run(
            self):
        """Already connected is the common case and it must cost
        nothing: one question, one answer, no polling."""
        forwarder = _forwarder_that_connects_after(0)

        _send(None, _mic(False, "Device unavailable"), threading.Event(),
              forwarder=forwarder)

        assert forwarder.connection_answers == [True], (
            "the refusal path never asked whether the connection was "
            "live")
        forwarder.send_notification.assert_called_once()

    def test_a_connection_that_opens_during_the_wait_gets_the_notice(self):
        """The cell this bead is about: the provider is up, WheelHouse
        is reachable, and the handshake simply has not finished yet. The
        wait polls until it does."""
        forwarder = _forwarder_that_connects_after(3)

        _send(None, _mic(False, "Device unavailable"), threading.Event(),
              forwarder=forwarder)

        assert forwarder.connection_answers == [False, False, False, True], (
            "the refusal path did not wait for the connection to open")
        forwarder.send_notification.assert_called_once()

    def test_a_notice_that_never_connects_is_reported_lost_with_its_address(
            self, caplog):
        """A developer running the provider from a console has no
        WheelHouse at all. The wait must end, and the log must name the
        address that was tried -- without it the line says a notice was
        lost and gives the reader nothing to check.
        """
        forwarder = _forwarder_that_connects_after(
            10 ** 9, uri="ws://127.0.0.1:5099")
        capture_failed = threading.Event()

        with patch("shared_stt.startup_refusal.CONNECT_TIMEOUT_S", 0.0), \
             caplog.at_level(logging.ERROR,
                            logger="shared_stt.startup_refusal"):
            _send(None, _mic(False, "Device unavailable"), capture_failed,
                  forwarder=forwarder)

        assert "ws://127.0.0.1:5099" in caplog.text, (
            "nothing reported the lost notice or the address it tried")
        assert capture_failed.is_set(), (
            "an undeliverable notice left the run alive")

    def test_a_lost_notice_still_ends_the_run_with_the_refusal_code(self):
        """The control for the bounded attempt: it must not change the
        outcome. A wait that raised out of the announcement thread, or
        that decided an undeliverable notice was a reason to keep
        running, would leave the provider up and deaf -- the exact state
        A9 exists to end.

        Not red-first evidence, and it is not counted as such: the
        refusal exit code arrived with .1.5 and this passes before the
        wait exists. The three tests above are the red-first ones. This
        one guards the exit against the change they ask for.

        No assertion on connection_answers here, unlike the tests above.
        This runs the real main(), which attaches a WebSocketLogHandler
        to the forwarder, and that handler asks is_connected about every
        record it is given -- so the count in this path measures
        logging, not the wait.

        The literal 3 for the reason the other exit-code tests in this
        file give: it is a process exit status another process reads
        back off a returncode.
        """
        forwarder = _forwarder_that_connects_after(10 ** 9)

        with patch("shared_stt.startup_refusal.CONNECT_TIMEOUT_S", 0.0):
            result, _mic_used, _forwarder = TestMainEndsTheRun. \
                _run_main_with_a_failed_capture(forwarder)

        assert result == 3
