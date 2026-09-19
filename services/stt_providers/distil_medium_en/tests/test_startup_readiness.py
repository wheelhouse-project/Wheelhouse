"""The Distil server announces readiness only after capture proves it.

wh-provider-ready-handshake criteria 1, 2, 3 and 5. The distil provider
carried the same defect as parakeet, in the same shape:
_send_startup_notification started a daemon thread that slept 3.0
seconds and sent "Transcription service ready", and start() started that
thread BEFORE audio_capture.start(), with an unconditional
logger.info("Audio capture started") right after. A microphone that
never opened produced a ready notice and a log line both saying the
service worked.

The parakeet file
(sherpa_offline_parakeet_stt_server/tests/test_startup_readiness.py) is
the template. The server fixture here follows this service's own
test_hint_handlers.py instead of building a real server: __new__ with no
__init__ needs no engine and no model files.
"""
from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock, Mock, patch

import pytest

import main as distil_main
from main import DistilMediumServer


@pytest.fixture
def server():
    """A DistilMediumServer shell with only the startup collaborators."""
    s = DistilMediumServer.__new__(DistilMediumServer)
    s.forwarder = MagicMock()
    s.audio_capture = MagicMock()
    # __new__ runs no __init__, so nothing else sets these. The
    # announcement reads the flag under the lock, and a shell without
    # either would raise AttributeError instead of testing anything.
    s.running = True
    s._notification_lock = threading.Lock()
    return s


@pytest.fixture
def logged():
    """Every message this module's logger emits.

    caplog cannot be used: start() sets logger.propagate = False
    (main.py, beside the WebSocket log handler), and caplog's handler
    sits on the root logger, so a caplog assertion about these lines
    would pass whatever the code does.
    """
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Collect()
    distil_main.logger.addHandler(handler)
    previous = distil_main.logger.level
    distil_main.logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        distil_main.logger.removeHandler(handler)
        distil_main.logger.setLevel(previous)


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
        """Criterion 5. The 15 second default belongs to the capture
        provider; a number here could disagree with it."""
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
        server.audio_capture.wait_ready = Mock(return_value=False)
        server.audio_capture.setup_error = "Device unavailable"
        server._announce_capture_outcome()
        assert "Device unavailable" in _notifications(server)[0][1]

    def test_a_timeout_with_no_reason_still_names_what_happened(self, server):
        """False with setup_error None means the wait expired with setup
        unfinished. The user still needs a sentence."""
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
    """The 3.0 second sleep was the whole mechanism."""

    def test_the_announcement_does_not_sleep(self, server):
        """Patched on the time module itself, not on a name inside
        main. Removing the 3.0 second sleep left main.py with no use of
        time at all, so its import is gone and a patch.object against
        distil_main.time would raise AttributeError instead of
        testing anything."""
        server.audio_capture.wait_ready = Mock(return_value=True)
        with patch('time.sleep') as slept:
            server._announce_capture_outcome()
        slept.assert_not_called()

    def test_the_notice_still_runs_off_the_command_loop(self, server):
        """Criterion 5. A slow microphone open must delay only the
        notice, so the wait keeps its own thread."""
        with patch.object(distil_main.threading, 'Thread') as thread:
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
        with patch.object(server, '_send_startup_notification',
                          side_effect=lambda: order.append('announce')), \
             patch.object(server, 'process_audio_loop'), \
             patch.object(server, 'cleanup'), \
             patch.object(distil_main.signal, 'signal'):
            server.start()
        assert order == ['capture.start', 'announce']

    def test_start_writes_no_capture_started_line_of_its_own(
            self, server, logged):
        """The line moved into the announcement. One left behind in
        start() would fire for a failed device and undo criterion 3."""
        with patch.object(server, '_send_startup_notification'), \
             patch.object(server, 'process_audio_loop'), \
             patch.object(server, 'cleanup'), \
             patch.object(distil_main.signal, 'signal'):
            server.start()
        assert not any("Audio capture started" in m for m in logged)


class TestAnIntentionalShutdownSendsNoNotice:
    """A stop that lands while the handshake is still waiting.

    Same mechanism as parakeet: WinRTAudioCapture.stop() clears
    _capture_alive and joins the capture thread
    (shared_audio/capture/winrt_capture.py:260-298), that thread sets
    _setup_done on every exit path (:620), and wait_ready() then returns
    False with setup_error still None (:356). cleanup() stops capture
    before it stops the forwarder, so the notice is still deliverable
    and the user reads a startup failure for a stop they asked for.
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
        # The fixture is a __new__ shell carrying only the collaborators
        # the announcement itself touches, and cleanup() reaches one
        # more. Without this the call raises AttributeError before it
        # reaches the flag, and the test would then fail under any
        # mutation for a reason that has nothing to do with the flag --
        # a catch that proves nothing.
        server.engine = MagicMock()
        server.cleanup()
        assert server.running is False


class TestAStopCannotLandBetweenTheGuardAndTheSend:
    """wh-provider-ready-handshake.1.2: the read and the send are one
    step, not two.

    Same defect and same fix as parakeet, and that file
    (sherpa_offline_parakeet_stt_server/tests/test_startup_readiness.py)
    carries the fuller reasoning. In short: the shutdown check above is
    a one-time read, a stop landing between it and
    forwarder.send_notification still reaches the user because
    WSForwarder.stop() gives already-queued frames a bounded 2.0 second
    chance to deliver (shared_stt/ws_forwarder.py:912-953), and the
    tests above all set the state while wait_ready() is still blocked,
    which is before the read.

    The lock cannot cancel a send the announcement has already decided
    on, so these tests assert what it does guarantee: the notice is
    never decided on and handed to the forwarder's loop while the
    recorded state already says stopping, and teardown has not begun at
    that hand-off.

    The hand-off is the boundary, not queue acceptance:
    send_notification schedules the queue put with
    asyncio.run_coroutine_threadsafe and never waits on the future
    (shared_stt/ws_forwarder.py:848-874), so the lock does not order
    that put at all: the forwarder's loop can run it before or after the
    lock is released (wh-provider-ready-handshake.1.3, .1.4).

    The stop goes through cleanup(), the real path that takes the lock;
    a bare "server.running = False" takes no lock and would prove
    nothing.
    """

    def _state_when_the_notice_is_sent(self, server, ready):
        """Pause the announcement between the read and its send, ask
        cleanup() for the stop from a second thread, and report what the
        shutdown state read at the instant each notice was handed to
        the forwarder.
        """
        at_the_send = threading.Event()
        release = threading.Event()
        stopped = threading.Event()
        observed = []

        # cleanup() reaches one collaborator the announcement does not,
        # exactly as test_cleanup_marks_the_server_stopped explains.
        server.engine = MagicMock()
        server.audio_capture.wait_ready = Mock(return_value=ready)
        server.forwarder.send_notification = Mock(
            side_effect=lambda *a, **k: observed.append(
                (server.running, server.audio_capture.stop.called)))

        # A logging.Handler cannot carry the pause: Handler.handle()
        # holds that handler's own lock across emit(), so a pause inside
        # emit() would also block cleanup()'s "Cleaning up..." line and
        # the unfixed run would deadlock instead of showing the defect.
        marker = ("Sending 'ready' notification" if ready
                  else "Audio capture is not ready")
        real_info = distil_main.logger.info
        real_error = distil_main.logger.error

        def pausing(write):
            def hook(message, *args, **kwargs):
                write(message, *args, **kwargs)
                if marker in str(message) and not at_the_send.is_set():
                    at_the_send.set()
                    release.wait(5)
            return hook

        with patch.object(distil_main.logger, 'info',
                          side_effect=pausing(real_info)), \
             patch.object(distil_main.logger, 'error',
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
