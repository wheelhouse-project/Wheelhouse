"""A capture failure after the model loads ends the run, nonzero.

wh-capture-winrt-required criterion A9.

The defect. A3 covers the capture that never gets BUILT: the factory
refuses, the provider sends the refusal and quits. It cannot cover the
capture that builds and then fails to START -- an AudioGraph that will
not open, a device taken by another process, a permission denied after
the model is already in memory. That path reached
_announce_capture_outcome, logged the failure, sent the startup_failed
notice, and returned with self.running still True. process_audio_loop
then kept turning `while self.running:` on a capture that will never
produce a chunk, so the provider ran deaf for as long as the machine
stayed up: a live process, a healthy-looking service, and every spoken
word discarded.

What A9 adds is the second half of that notice. The notice still goes
out first -- it is the only thing that tells the user why -- and then
the loop ends, cleanup runs, and the process exits nonzero, which is
what lets WheelHouse see a dead provider instead of a silent one.

The exit is sys.exit(REFUSAL_EXIT_CODE) from start(), on the main
thread, after cleanup(). The code is 3 rather than 1 because the
supervisor in shared_stt/launcher.py restarts any nonzero exit reached
inside its fifteen-second crash window
(wh-capture-winrt-required.1.5).
It cannot be raised from the announcement: that runs on a
daemon thread, where SystemExit kills only the thread and leaves the
process exactly as deaf as before. The same shape already sits in this
file for A3 (main.py, the factory refusal in __init__), so the entry
point below needs no exit handling of its own.
"""
from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import pytest

import main as parakeet_main


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
    forwarder = MagicMock()
    forwarder.uri = uri
    answers = []

    def is_connected():
        connected = len(answers) >= asks
        answers.append(connected)
        return connected

    type(forwarder).is_connected = PropertyMock(side_effect=is_connected)
    forwarder.connection_answers = answers
    return forwarder


@pytest.fixture(autouse=True)
def keep_the_process_loggers_intact():
    """Undo the logging start() rewires, for every test in this file.

    start() attaches WebSocket handlers to four loggers and sets
    propagate = False on two of them, so its records do not reach the
    root logger twice. The tests below call the real start(), and
    process-wide logging state outlives the test that set it: a
    propagate = False left behind makes caplog blind in every later test
    file in the same run, and a caplog assertion there then passes
    whatever the code does. Measured on 2026-09-06 -- without this
    fixture, four tests in three other files of this suite failed, none
    of them touching capture.
    """
    names = [
        parakeet_main.logger.name,
        "shared_stt",
        parakeet_main.LOAD_METRICS_LOGGER_NAME,
        parakeet_main.CAPTURE_LOGGER_NAME,
    ]
    saved = [(logging.getLogger(n), logging.getLogger(n).propagate,
              list(logging.getLogger(n).handlers)) for n in names]
    try:
        yield
    finally:
        for log, propagate, handlers in saved:
            log.propagate = propagate
            log.handlers[:] = handlers


@pytest.fixture
def server():
    """A ParakeetServer with the engine, forwarder and capture stubbed.

    The same fixture as test_startup_readiness.py: the engine is stubbed
    because loading Parakeet needs the model files, the forwarder
    because it binds a websocket, and the capture provider because it
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
    # __init__ leaves running False; start() is what sets it True. The
    # announcement reads that flag, so a fixture left at False would put
    # every test below in the shutdown case.
    built.running = True
    return built


def _capture_that_never_opened(server):
    """Point the fixture's capture at the failure A9 is about: the
    handshake answers False with a device error."""
    server.audio_capture.wait_ready = Mock(return_value=False)
    server.audio_capture.setup_error = "Device unavailable"


def _bounded_silence(limit=500):
    """A read() that answers None -- a capture that will never produce a
    chunk -- and then gives up.

    The bound is what keeps the unfixed run finite. On the shipped code
    self.running is still True when process_audio_loop is entered, so an
    unbounded None read spins for ever and the test hangs instead of
    failing. process_audio_loop catches Exception and returns, so the
    raise ends the loop the way a real error would and the assertion
    below is what reports the defect.
    """
    reads = []

    def read(timeout=None):
        reads.append(1)
        if len(reads) > limit:
            raise RuntimeError(
                "the audio loop kept running after the capture failed - "
                "the provider is deaf and still up")
        return None

    return read


class TestTheAudioLoopEnds:
    """The loop is the thing that kept the deaf provider alive."""

    def test_a_failed_handshake_ends_the_audio_loop(self, server):
        """The real loop, on its own thread, against the real
        announcement.

        The loop is proved to be TURNING before the announcement runs.
        Without that the announcement could finish first, the loop would
        never enter, and this test would pass on the shipped code
        without ever exercising the behaviour.
        """
        turning = threading.Event()

        def read(timeout=None):
            turning.set()
            return None

        server.audio_capture.read = Mock(side_effect=read)
        _capture_that_never_opened(server)

        loop = threading.Thread(target=server.process_audio_loop,
                                daemon=True)
        loop.start()
        assert turning.wait(5), "the audio loop never started"

        server._announce_capture_outcome()

        loop.join(5)
        try:
            assert not loop.is_alive(), (
                "the audio loop kept running after the capture failed - "
                "the provider is deaf and still up")
        finally:
            # Whatever the assertion said, this thread does not outlive
            # the test.
            server.running = False
            loop.join(5)

    def test_the_failure_is_recorded_as_a_stop(self, server):
        """process_audio_loop reads self.running and nothing else, so
        this flag is the whole mechanism that ends it."""
        _capture_that_never_opened(server)
        server._announce_capture_outcome()
        assert server.running is False


class TestTheNoticeStillGoesOutFirst:
    """The user is told why before anything is torn down."""

    def test_the_notice_is_sent_before_the_loop_is_stopped(self, server):
        """A stop recorded first would put the send in the state the
        shutdown guard suppresses (wh-provider-ready-handshake.1.1), and
        the user would read nothing at all."""
        observed = []
        server.forwarder.send_notification = Mock(
            side_effect=lambda *a, **k: observed.append(server.running))
        _capture_that_never_opened(server)

        server._announce_capture_outcome()

        assert observed == [True], (
            "the run was marked stopped before the failure notice went "
            "out")

    def test_the_notice_is_still_the_startup_failed_one(self, server):
        """A9 adds to criterion 2 rather than replacing it."""
        _capture_that_never_opened(server)
        server._announce_capture_outcome()
        args, kwargs = server.forwarder.send_notification.call_args
        assert kwargs["kind"] == "startup_failed"
        assert "Device unavailable" in args[1]


class TestTheProcessExits:
    """A provider that stays up looks healthy to WheelHouse."""

    @staticmethod
    def _start_with_a_failed_capture(server):
        """Run the real start(), with the announcement taken on this
        thread so the outcome is settled before the loop is entered.

        The real process_audio_loop and the real cleanup() both run:
        the loop returns at once because the announcement already
        recorded the stop, and cleanup() is what the assertions below
        are about.
        """
        _capture_that_never_opened(server)
        server.audio_capture.read = Mock(side_effect=_bounded_silence())
        server.audio_processor = MagicMock()
        with patch.object(server, '_send_startup_notification',
                          side_effect=server._announce_capture_outcome), \
             patch.object(parakeet_main.signal, 'signal'), \
             pytest.raises(SystemExit) as raised:
            server.start()
        return raised.value

    def test_start_exits_nonzero_after_a_failed_capture(self, server):
        assert self._start_with_a_failed_capture(server).code != 0

    def test_start_exits_with_the_refusal_code(self, server):
        """3, not 1, so the supervisor does not start the provider
        again.

        A normal WheelHouse launch does not run main.py: it runs
        launcher.py, and run_launcher() in shared_stt/launcher.py
        restarts any nonzero exit reached inside crash_threshold_s
        (15 seconds), up to max_crashes (3). A machine whose model is
        already in the file cache reaches this refusal well inside that
        window, so exit 1 was read as a crash and the provider was
        started three times over, each attempt refusing again against a
        launch WheelHouse had already recorded as stopped
        (wh-capture-winrt-required.1.5).

        The literal rather than the constant: this number is a process
        exit status that a SEPARATE process reads back off a returncode,
        and a test that read the same constant on both sides could not
        notice the value moving onto one another failure already uses.
        """
        assert self._start_with_a_failed_capture(server).code == 3

    def test_cleanup_runs_before_the_exit(self, server):
        """Exiting with the microphone still open and the forwarder
        still connected leaves the device held by a process that is
        going away."""
        self._start_with_a_failed_capture(server)
        server.audio_capture.stop.assert_called_once()
        server.forwarder.stop.assert_called_once()

    def test_a_working_capture_does_not_exit(self, server):
        """The exit belongs to the failure only. A start() that quit
        after a healthy handshake would take the whole service down on
        every launch."""
        server.audio_capture.wait_ready = Mock(return_value=True)
        server.audio_processor = MagicMock()
        with patch.object(server, '_send_startup_notification',
                          side_effect=server._announce_capture_outcome), \
             patch.object(server, 'process_audio_loop'), \
             patch.object(server, 'cleanup'), \
             patch.object(parakeet_main.signal, 'signal'):
            server.start()  # no SystemExit

    def test_an_ordinary_stop_does_not_exit_nonzero(self, server):
        """A user-requested stop is not a failure.

        The stop lands inside the handshake, which is where a real one
        lands: audio_capture.stop() is what releases wait_ready(), and
        it releases it as False -- the same answer a dead microphone
        gives. The announcement's shutdown guard returns before the
        failure branch, so nothing marks the run failed and start()
        returns normally rather than reporting a failure the user asked
        for.
        """
        def stop_during_the_wait():
            server.stop()
            return False

        server.audio_capture.wait_ready = Mock(
            side_effect=stop_during_the_wait)
        server.audio_processor = MagicMock()
        with patch.object(server, '_send_startup_notification',
                          side_effect=server._announce_capture_outcome), \
             patch.object(server, 'process_audio_loop'), \
             patch.object(server, 'cleanup'), \
             patch.object(parakeet_main.signal, 'signal'):
            server.start()  # no SystemExit


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
    _notification_lock, which cleanup() also takes before it stops the
    forwarder, so the teardown cannot begin while the notice is still
    waiting for a connection.
    """

    def test_a_live_connection_is_confirmed_before_the_refusal_ends_the_run(
            self, server):
        """Already connected is the common case and it must cost
        nothing: one question, one answer, no polling."""
        server.forwarder = _forwarder_that_connects_after(0)
        _capture_that_never_opened(server)

        server._announce_capture_outcome()

        assert server.forwarder.connection_answers == [True], (
            "the refusal path never asked whether the connection was "
            "live")
        server.forwarder.send_notification.assert_called_once()

    def test_a_connection_that_opens_during_the_wait_gets_the_notice(
            self, server):
        """The cell this bead is about: the provider is up, WheelHouse
        is reachable, and the handshake simply has not finished yet. The
        wait polls until it does."""
        server.forwarder = _forwarder_that_connects_after(3)
        _capture_that_never_opened(server)

        server._announce_capture_outcome()

        assert server.forwarder.connection_answers == [
            False, False, False, True], (
            "the refusal path did not wait for the connection to open")
        server.forwarder.send_notification.assert_called_once()

    def test_a_notice_that_never_connects_is_reported_lost_with_its_address(
            self, server, caplog):
        """A developer running the provider from a console has no
        WheelHouse at all. The wait must end, and the log must name the
        address that was tried -- without it the line says a notice was
        lost and gives the reader nothing to check.
        """
        server.forwarder = _forwarder_that_connects_after(
            10 ** 9, uri="ws://127.0.0.1:5099")
        _capture_that_never_opened(server)

        with patch("shared_stt.startup_refusal.CONNECT_TIMEOUT_S", 0.0), \
             caplog.at_level(logging.ERROR,
                            logger="shared_stt.startup_refusal"):
            server._announce_capture_outcome()

        assert "ws://127.0.0.1:5099" in caplog.text, (
            "nothing reported the lost notice or the address it tried")
        assert server._capture_failed is True, (
            "an undeliverable notice left the run alive")

    def test_a_lost_notice_still_ends_the_run_with_the_refusal_code(
            self, server):
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
        This runs the real start(), which attaches a
        WebSocketLogHandler to the forwarder, and that handler asks
        is_connected about every record it is given -- so the count in
        this path measures logging, not the wait.

        The literal 3 for the reason the other exit-code tests in this
        file give: it is a process exit status another process reads
        back off a returncode.
        """
        server.forwarder = _forwarder_that_connects_after(10 ** 9)

        with patch("shared_stt.startup_refusal.CONNECT_TIMEOUT_S", 0.0):
            raised = TestTheProcessExits._start_with_a_failed_capture(server)

        assert raised.code == 3
