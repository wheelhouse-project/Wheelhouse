"""The Parakeet server announces readiness only after capture proves it.

wh-provider-ready-handshake criteria 1, 2, 3 and 5, and
wh-parakeet-ready-notice-preflight criteria 1, 2 and 3.

The defect: _send_startup_notification started a daemon thread that
slept 3.0 seconds and then sent "Transcription service ready", with
nothing between the sleep and the send that could know whether the
microphone opened. A denied or missing device logged an error and the
user was still told transcription works, while every spoken word was
discarded. David measured it on 2026-09-03: WheelHouse received the
ready notice at 19:32:51.038 while capture stayed unavailable until
19:32:58.

The fix is the handshake the shared capture base defines at
shared_audio/capture/base.py wait_ready(). The deleted Kroko provider
called it correctly and is the reference
(git show 15262e7d:services/stt_providers/sherpa_streaming_kroko_stt_server/main.py
line 678).

The thread body is a named method so these tests can drive it directly.
A test that waited on a real daemon thread would be a test whose failure
mode is a timeout.
"""
from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock, Mock, patch

import pytest

import main as parakeet_main
from sherpa_engine import HotwordsStatus


@pytest.fixture
def server():
    """A ParakeetServer with the engine, forwarder and capture stubbed.

    The engine is stubbed because loading Parakeet needs the model
    files, the forwarder because it binds a websocket, and the capture
    provider because it opens the real microphone. Same shape as
    test_load_diagnostics_wiring.py's fixture.
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
    # announcement now reads that flag, so a fixture left at False would
    # put every test below in the shutdown case.
    built.running = True
    return built


@pytest.fixture
def logged():
    """Every message this module's logger emits, as a list of strings.

    caplog cannot be used here. start() sets logger.propagate = False
    (so the WebSocket log handler does not double-report), and caplog's
    handler sits on the root logger, so a caplog assertion about these
    lines passes whatever the code does. Attaching to the module logger
    itself is the only capture that stays honest across that call.
    """
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Collect()
    parakeet_main.logger.addHandler(handler)
    previous = parakeet_main.logger.level
    parakeet_main.logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        parakeet_main.logger.removeHandler(handler)
        parakeet_main.logger.setLevel(previous)


def _notifications(server):
    """Every (title, message, kind) the server sent, kind normalised."""
    sent = []
    for call in server.forwarder.send_notification.call_args_list:
        args, kwargs = call
        sent.append((args[0], args[1], kwargs.get("kind", "")))
    return sent


class TestTheReadyNoticeFollowsTheHandshake:
    """Criterion 1: ready is sent only after wait_ready() returns True."""

    def test_a_ready_capture_gets_the_ready_notice(self, server):
        server.audio_capture.wait_ready = Mock(return_value=True)
        server._announce_capture_outcome()
        assert [n[1] for n in _notifications(server)] == [
            "Transcription service ready"]

    def test_the_handshake_is_actually_called(self, server):
        server.audio_capture.wait_ready = Mock(return_value=True)
        server._announce_capture_outcome()
        server.audio_capture.wait_ready.assert_called_once()

    def test_the_wait_uses_the_handshakes_own_timeout(self, server):
        """Criterion 5. The default is 15 s and belongs to the capture
        provider, so passing a number here would put the timeout in two
        places that could disagree."""
        server.audio_capture.wait_ready = Mock(return_value=True)
        server._announce_capture_outcome()
        args, kwargs = server.audio_capture.wait_ready.call_args
        assert args == () and kwargs == {}

    def test_a_failed_capture_never_gets_the_ready_notice(self, server):
        server.audio_capture.wait_ready = Mock(return_value=False)
        server.audio_capture.setup_error = "the microphone failed to open"
        server._announce_capture_outcome()
        messages = [n[1] for n in _notifications(server)]
        assert "Transcription service ready" not in messages


class TestTheFailureNotice:
    """Criterion 2: a failed handshake reports startup_failed."""

    def test_a_failed_capture_sends_startup_failed(self, server):
        server.audio_capture.wait_ready = Mock(return_value=False)
        server.audio_capture.setup_error = "the microphone failed to open"
        server._announce_capture_outcome()
        kinds = [n[2] for n in _notifications(server)]
        assert kinds == ["startup_failed"]

    def test_the_failure_notice_names_the_cause(self, server):
        """A notice saying only that something failed leaves the user
        with nothing to act on. setup_error is the only description of
        the device error there is."""
        server.audio_capture.wait_ready = Mock(return_value=False)
        server.audio_capture.setup_error = "Device unavailable"
        server._announce_capture_outcome()
        assert "Device unavailable" in _notifications(server)[0][1]

    def test_a_timeout_with_no_reason_still_names_what_happened(self, server):
        """wait_ready() returning False with setup_error None means the
        wait expired with setup unfinished. The user still needs a
        sentence."""
        server.audio_capture.wait_ready = Mock(return_value=False)
        server.audio_capture.setup_error = None
        server._announce_capture_outcome()
        message = _notifications(server)[0][1]
        assert message.strip() != ""
        assert "None" not in message

    def test_the_ready_notice_carries_no_startup_failed_kind(self, server):
        server.audio_capture.wait_ready = Mock(return_value=True)
        server._announce_capture_outcome()
        assert _notifications(server)[0][2] != "startup_failed"

    def test_the_ready_notice_carries_kind_ready(self, server):
        """WheelHouse routes on kind, never on the message text.

        The websocket manager used to accept any kind-less notice whose
        text contained "ready". That also matched this provider's own
        "Hint '<word>' already exists" notice, so the duplicate-hint
        notice completed the launch and never reached the user. The
        fallback is gone, so this notice completes a launch only when it
        carries kind="ready" (wh-ready-connection-stamp.2.2.1).
        """
        server.audio_capture.wait_ready = Mock(return_value=True)
        server._announce_capture_outcome()
        assert _notifications(server)[0][2] == "ready"


class TestTheCaptureStartedLogLine:
    """Criterion 3: the log line follows the proof, not the attempt."""

    def test_the_line_is_written_after_a_successful_handshake(
            self, server, logged):
        server.audio_capture.wait_ready = Mock(return_value=True)
        server._announce_capture_outcome()
        assert any("Audio capture started" in m for m in logged)

    def test_the_line_is_not_written_for_a_failed_handshake(
            self, server, logged):
        """This is the line an operator reads in wheelhouse.log to
        decide whether the microphone worked. Writing it for a device
        that never opened makes the log say the opposite of the truth."""
        server.audio_capture.wait_ready = Mock(return_value=False)
        server.audio_capture.setup_error = "Device unavailable"
        server._announce_capture_outcome()
        assert not any("Audio capture started" in m for m in logged)

    def test_a_failed_handshake_logs_the_failure_instead(
            self, server, logged):
        server.audio_capture.wait_ready = Mock(return_value=False)
        server.audio_capture.setup_error = "Device unavailable"
        server._announce_capture_outcome()
        assert any("Device unavailable" in m for m in logged)


class TestTheTimerThreadIsGone:
    """wh-parakeet-ready-notice-preflight criterion 1."""

    def test_the_announcement_does_not_sleep(self, server):
        """The 3.0 second sleep was the whole mechanism: it made the
        notice look like it had waited for something. Any sleep left
        here would delay the notice for a reason nothing measured."""
        server.audio_capture.wait_ready = Mock(return_value=True)
        with patch.object(parakeet_main.time, 'sleep') as slept:
            server._announce_capture_outcome()
        slept.assert_not_called()

    def test_the_notice_still_runs_off_the_command_loop(self, server):
        """Criterion 5. A slow microphone open must delay only the
        notice, so the wait keeps its own thread."""
        with patch.object(parakeet_main.threading, 'Thread') as thread:
            server._send_startup_notification()
        thread.assert_called_once()
        assert thread.call_args.kwargs['target'] == \
            server._announce_capture_outcome
        assert thread.call_args.kwargs['daemon'] is True
        thread.return_value.start.assert_called_once()


class TestStartOrdersTheStepsCorrectly:
    """The handshake cannot answer before capture has been started."""

    def test_capture_starts_before_the_announcement(self, server):
        order = []
        server.audio_capture.start = Mock(
            side_effect=lambda: order.append('capture.start'))
        server.audio_processor = MagicMock()
        server.audio_processor.seed_capture_baseline_before_capture_starts = \
            Mock(side_effect=lambda: order.append('seed'))
        with patch.object(server, '_send_startup_notification',
                          side_effect=lambda: order.append('announce')), \
             patch.object(server, 'process_audio_loop'), \
             patch.object(server, 'cleanup'), \
             patch.object(parakeet_main.signal, 'signal'):
            server.start()
        assert order == ['seed', 'capture.start', 'announce']

    def test_start_writes_no_capture_started_line_of_its_own(
            self, server, logged):
        """The line moved into the announcement. One left behind in
        start() would fire for a failed device and undo criterion 3."""
        server.audio_processor = MagicMock()
        with patch.object(server, '_send_startup_notification'), \
             patch.object(server, 'process_audio_loop'), \
             patch.object(server, 'cleanup'), \
             patch.object(parakeet_main.signal, 'signal'):
            server.start()
        assert not any("Audio capture started" in m for m in logged)


class TestAnIntentionalShutdownSendsNoNotice:
    """A stop that lands while the handshake is still waiting.

    WinRTAudioCapture.stop() clears _capture_alive and joins the capture
    thread (shared_audio/capture/winrt_capture.py:260-298), and that
    thread sets _setup_done on every exit path (:620), so wait_ready()
    returns False with setup_error still None (:356). cleanup() stops
    capture before it stops the forwarder, so the notice is still
    deliverable and the user reads a startup failure for a stop they
    asked for.
    """

    def _announce_while_stopping(self, server, ready):
        """Run the announcement, request the stop mid-wait, release it.

        The handshake blocks on an event this method owns, so the stop
        is guaranteed to land inside the wait rather than before or
        after it.
        """
        waiting = threading.Event()
        release = threading.Event()

        def blocking_wait_ready():
            waiting.set()
            release.wait(5)
            return ready

        server.audio_capture.wait_ready = Mock(
            side_effect=blocking_wait_ready)
        announcing = threading.Thread(
            target=server._announce_capture_outcome)
        announcing.start()
        assert waiting.wait(5), "the handshake never started"
        server.running = False
        release.set()
        announcing.join(5)
        assert not announcing.is_alive(), "the announcement never finished"

    def test_a_stop_during_the_wait_sends_no_failure_notice(self, server):
        """The stop releases wait_ready() as False, which is the same
        answer a dead microphone gives."""
        self._announce_while_stopping(server, ready=False)
        assert _notifications(server) == []

    def test_a_stop_during_the_wait_sends_no_ready_notice(self, server):
        """"Transcription service ready" for a service that is stopping
        is as wrong as the failure notice, so both branches are
        suppressed."""
        self._announce_while_stopping(server, ready=True)
        assert _notifications(server) == []

    def test_cleanup_marks_the_server_stopped(self, server):
        """The flag the announcement reads has to be honest on the path
        where stop() was never called.

        run() is a try/finally around process_audio_loop, so a loop that
        raises reaches cleanup() directly. Setting the flag only in
        stop() would leave it reading True for the whole of that
        teardown, and the announcement thread would send its notice into
        it.
        """
        server.cleanup()
        assert server.running is False


class TestAStopCannotLandBetweenTheGuardAndTheSend:
    """wh-provider-ready-handshake.1.2: the read and the send are one
    step, not two.

    The shutdown check above is a one-time read. A stop that lands after
    that read and before forwarder.send_notification still reaches the
    user, because WSForwarder.stop() gives already-queued frames a
    bounded 2.0 second chance to deliver before it stops its loop
    (shared_stt/ws_forwarder.py:912-953). The window is real work rather
    than a theoretical instant: two logger.info calls sit in it, and
    each goes through WebSocketLogHandler to the forwarder.

    The tests above cannot catch that. Every one of them sets the state
    while wait_ready() is still blocked, which is BEFORE the read, and
    the read then reports the stop correctly.

    What the lock buys is what these tests assert, and it is narrower
    than "no notice is sent". The lock cannot cancel a send the
    announcement has already decided on: a stop requested during the
    pause below is made to wait, and the notice then goes out. What the
    lock guarantees is that the notice is never decided on and handed
    to the forwarder's loop while the recorded state already says
    stopping, and that teardown -- audio_capture.stop(), with
    forwarder.stop() behind it -- has not begun at that hand-off.

    The hand-off is the boundary, not queue acceptance:
    send_notification schedules the queue put with
    asyncio.run_coroutine_threadsafe and never waits on the future
    (shared_stt/ws_forwarder.py:848-874), so the lock does not order
    that put at all: the forwarder's loop can run it before or after the
    lock is released (wh-provider-ready-handshake.1.3, .1.4).

    The stop is requested through cleanup(), the real path that takes
    the lock. A bare "server.running = False" takes no lock and would
    make these tests vacuous.
    """

    def _state_when_the_notice_is_sent(self, server, ready):
        """Pause the announcement between the read and its send, ask
        cleanup() for the stop from a second thread, and report what the
        shutdown state read at the instant each notice was handed to
        the forwarder.

        Returns one (running, capture_stopped) pair per notification the
        forwarder was offered.
        """
        at_the_send = threading.Event()
        release = threading.Event()
        stopped = threading.Event()
        observed = []

        server.audio_capture.wait_ready = Mock(return_value=ready)
        server.forwarder.send_notification = Mock(
            side_effect=lambda *a, **k: observed.append(
                (server.running, server.audio_capture.stop.called)))

        # The pause hangs the announcement on the last log line before
        # its send. A logging.Handler cannot be used for it: Handler.
        # handle() holds that handler's own lock across emit(), so a
        # pause inside emit() would also block cleanup()'s "Cleaning
        # up..." line and the unfixed run would deadlock instead of
        # showing the defect.
        marker = ("Sending 'ready' notification" if ready
                  else "Audio capture is not ready")
        real_info = parakeet_main.logger.info
        real_error = parakeet_main.logger.error

        def pausing(write):
            def hook(message, *args, **kwargs):
                write(message, *args, **kwargs)
                if marker in str(message) and not at_the_send.is_set():
                    at_the_send.set()
                    release.wait(5)
            return hook

        with patch.object(parakeet_main.logger, 'info',
                          side_effect=pausing(real_info)), \
             patch.object(parakeet_main.logger, 'error',
                          side_effect=pausing(real_error)):
            announcing = threading.Thread(
                target=server._announce_capture_outcome)
            announcing.start()
            assert at_the_send.wait(5), \
                "the announcement never reached its send"

            def request_the_stop():
                server.cleanup()
                stopped.set()

            stopping = threading.Thread(target=request_the_stop)
            stopping.start()
            # Bounded on purpose. With the serialization in place
            # cleanup() is blocked on the lock and this wait expires,
            # which is the passing case; without it cleanup() runs
            # straight through and this returns at once, which is the
            # case the assertions reject.
            stopped.wait(1.0)
            release.set()
            announcing.join(5)
            stopping.join(5)
        assert not announcing.is_alive(), "the announcement never finished"
        assert not stopping.is_alive(), "the stop never finished"
        return observed

    def test_the_ready_notice_is_never_sent_into_a_stopping_server(
            self, server):
        observed = self._state_when_the_notice_is_sent(
            server, ready=True)
        assert len(observed) == 1, "the announcement sent nothing"
        running, capture_stopped = observed[0]
        assert running is True, \
            "the ready notice was sent after the stop was recorded"
        assert capture_stopped is False, \
            "the ready notice was sent after teardown had started"

    def test_the_failure_notice_is_never_sent_into_a_stopping_server(
            self, server):
        server.audio_capture.setup_error = "Device unavailable"
        observed = self._state_when_the_notice_is_sent(
            server, ready=False)
        assert len(observed) == 1, "the announcement sent nothing"
        running, capture_stopped = observed[0]
        assert running is True, \
            "the failure notice was sent after the stop was recorded"
        assert capture_stopped is False, \
            "the failure notice was sent after teardown had started"


class TestTheReadyNoticeReportsBoosting:
    """wh-parakeet-hotword-vocab criterion 3.

    The engine records, on every load path, whether hint boosting was
    requested and whether it actually started. Before this, a
    vocabulary the engine rejected produced one warning in a log file
    the user never opens, and the same "Transcription service ready"
    notice as a run with boosting working. The user then had no way to
    tell a run where their hints were being boosted from a run where
    they were silently doing nothing.
    """

    @staticmethod
    def _ready_message(server, status):
        server.audio_capture.wait_ready = Mock(return_value=True)
        server.engine.hotwords_status = status
        server._announce_capture_outcome()
        sent = _notifications(server)
        assert len(sent) == 1, f"expected one notification, got {sent}"
        return sent[0][1]

    def test_a_rejected_vocabulary_is_never_reported_as_boosting_on(
            self, server):
        message = self._ready_message(
            server,
            HotwordsStatus(requested=True, active=False,
                           detail="the vocabulary file is missing"))
        assert "boosting is on" not in message.lower()

    def test_a_rejected_vocabulary_says_boosting_is_off(self, server):
        message = self._ready_message(
            server,
            HotwordsStatus(requested=True, active=False,
                           detail="the vocabulary file is missing"))
        assert "boosting is off" in message.lower()

    def test_the_reason_reaches_the_user(self, server):
        """The reason is the only part that tells the user what to fix."""
        message = self._ready_message(
            server,
            HotwordsStatus(
                requested=True, active=False,
                detail="the vocabulary file is missing: C:/m/bpe.vocab"))
        assert "C:/m/bpe.vocab" in message

    def test_the_notice_still_says_the_service_is_ready(self, server):
        """Boosting is an extra; transcription itself works without it."""
        message = self._ready_message(
            server,
            HotwordsStatus(requested=True, active=False,
                           detail="no vocabulary"))
        assert message.startswith("Transcription service ready")

    def test_working_boosting_says_so(self, server):
        message = self._ready_message(
            server, HotwordsStatus(requested=True, active=True))
        assert "boosting is on" in message.lower()

    def test_boosting_that_was_never_requested_adds_nothing(self, server):
        message = self._ready_message(server, HotwordsStatus())
        assert message == "Transcription service ready"

    def test_an_engine_without_a_status_adds_nothing(self, server):
        """The stub engines other tests build carry no status at all, and
        an unknown state must not become a claim either way."""
        server.audio_capture.wait_ready = Mock(return_value=True)
        server.engine = object()
        server._announce_capture_outcome()
        assert [n[1] for n in _notifications(server)] == [
            "Transcription service ready"]
