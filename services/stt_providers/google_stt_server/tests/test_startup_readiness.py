"""The Google server announces readiness only after capture proves it.

wh-provider-ready-handshake criteria 1, 2, 5 and 6. Google carried the
parakeet defect in a different shape: the startup notification ran on a
thread that slept 3.0 seconds and then reported the credentials
preflight result, which says nothing about the microphone. A device that
never opened produced "Transcription service ready" while every spoken
word was discarded.

Criterion 3 does not apply here. Google writes no "Audio capture
started" line to move: grep -rn "Audio capture started" over
services/stt_providers/google_stt_server/ returns nothing. The half of
the criterion that does apply -- a failed handshake logs the failure --
is pinned below.

Criterion 6 is covered by the existing tests in
test_credentials_preflight.py: TestStartupNotification and
TestRestartCompletionNotification still call the unchanged
startup_notification and restart_completion_notification.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as server_main


def _mic(ready=True, setup_error=None):
    """A capture provider that answers the handshake and nothing else."""
    return SimpleNamespace(
        wait_ready=Mock(return_value=ready),
        setup_error=setup_error,
    )


def _running():
    """The shutdown event of a server that is not shutting down.

    startup_notification_after_capture takes the event positionally, so
    every call has to say which of the two states it is asking about.
    """
    return threading.Event()


def _failed():
    """The capture-failure event of a run nobody has failed yet.

    send_startup_notification takes it positionally and required
    (wh-capture-winrt-required A9): it is the announcement thread's only
    route back to main()'s loop, and a default would let a call site
    drop the whole mechanism in silence.
    """
    return threading.Event()


def _lock():
    """The notification lock of a server nobody else is stopping.

    send_startup_notification takes it positionally and required
    (wh-provider-ready-handshake.1.2): a default would let a call site
    skip the serialization in silence.
    """
    return threading.Lock()


def _after_capture(startup_error, mic, shutting_down):
    """The handshake and the decision, in the order the sender runs
    them.

    They are two functions rather than one since
    wh-provider-ready-handshake.1.2: the wait has to happen before the
    notification lock is taken, and the decision has to happen under it.
    Driving both halves here keeps every test below sensitive to a
    change in either, which one call to the decision alone would not be.
    """
    ready = server_main.capture_handshake(startup_error, mic)
    return server_main.startup_notification_after_capture(
        startup_error, ready, mic, shutting_down)


@pytest.fixture
def logged():
    """Every message this module's logger emits.

    caplog cannot be used: main() sets logger.propagate = False
    (main.py:1193) beside the WebSocket log handler, and caplog's
    handler sits on the root logger, so a caplog assertion about these
    lines would pass whatever the code does.
    """
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Collect()
    server_main.logger.addHandler(handler)
    previous = server_main.logger.level
    server_main.logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        server_main.logger.removeHandler(handler)
        server_main.logger.setLevel(previous)


class TestTheReadyNoticeFollowsTheHandshake:
    """Criterion 1: ready is sent only after wait_ready() returns True."""

    def test_a_ready_capture_gets_the_ready_notice(self):
        assert _after_capture(
            None, _mic(ready=True), _running()) == (
                "Google STT", "Transcription service ready", "ready")

    def test_the_handshake_is_actually_called(self):
        mic = _mic(ready=True)
        _after_capture(
            None, mic, _running())
        mic.wait_ready.assert_called_once()

    def test_the_wait_uses_the_handshakes_own_timeout(self):
        """Criterion 5. The 15 second default belongs to the capture
        provider; a number here could disagree with it."""
        mic = _mic(ready=True)
        _after_capture(
            None, mic, _running())
        args, kwargs = mic.wait_ready.call_args
        assert args == () and kwargs == {}

    def test_a_failed_capture_never_gets_the_ready_notice(self):
        _t, message, _k = _after_capture(
            None, _mic(ready=False, setup_error="Device unavailable"),
            _running())
        assert message != "Transcription service ready"


class TestTheFailureNotice:
    """Criterion 2: a failed handshake reports startup_failed."""

    def test_a_failed_capture_sends_startup_failed(self):
        _t, _m, kind = _after_capture(
            None, _mic(ready=False, setup_error="Device unavailable"),
            _running())
        assert kind == "startup_failed"

    def test_the_failure_notice_names_the_cause(self):
        _t, message, _k = _after_capture(
            None, _mic(ready=False, setup_error="Device unavailable"),
            _running())
        assert "Device unavailable" in message

    def test_a_timeout_with_no_reason_still_names_what_happened(self):
        """False with setup_error None means the wait expired with setup
        unfinished. The user still needs a sentence."""
        _t, message, _k = _after_capture(
            None, _mic(ready=False, setup_error=None), _running())
        assert message.strip() != ""
        assert "None" not in message

    def test_the_notice_keeps_the_providers_own_title(self):
        """WheelHouse routes on the title the other notices use."""
        title, _m, _k = _after_capture(
            None, _mic(ready=False, setup_error="Device unavailable"),
            _running())
        assert title == server_main.startup_notification(None)[0]

    def test_the_failure_is_logged(self, logged):
        """The half of criterion 3 that applies to google: there is no
        success line here to move, but a failure must leave a trace an
        operator can read in wheelhouse.log."""
        _after_capture(
            None, _mic(ready=False, setup_error="Device unavailable"),
            _running())
        assert any("Device unavailable" in m for m in logged)


class TestTheCredentialsFailureKeepsPrecedence:
    """A provider with no client will not transcribe whatever the
    microphone does, so the credentials message is the one to act on."""

    def test_a_credentials_error_is_reported_not_the_capture_error(self):
        triple = _after_capture(
            "keyfile missing", _mic(ready=False, setup_error="Device gone"),
            _running())
        assert triple == server_main.startup_notification("keyfile missing")

    def test_a_credentials_error_does_not_wait_on_capture(self):
        """Waiting out the handshake before sending a notice that will
        not mention the microphone only delays the notice."""
        mic = _mic(ready=False, setup_error="Device gone")
        _after_capture(
            "keyfile missing", mic, _running())
        mic.wait_ready.assert_not_called()

    def test_a_credentials_error_with_a_working_mic_still_fails(self):
        triple = _after_capture(
            "keyfile missing", _mic(ready=True), _running())
        assert triple == server_main.startup_notification("keyfile missing")


class TestTheNotificationSender:
    """The thread body is a module-level function so its behaviour can
    be pinned; it used to be a closure inside main()."""

    def test_a_ready_capture_sends_the_ready_notification(self):
        forwarder = MagicMock()
        server_main.send_startup_notification(
            forwarder, None, _mic(True), _running(), _lock(), _failed())
        # capture_backend joined this call in wh-capture-winrt-required
        # A4, which names the path the factory built so WheelHouse can
        # write it to wheelhouse.log. Kept as an exact-call assertion:
        # it is what makes a silently added or dropped argument visible
        # here, and that visibility is the reason this line changed
        # rather than being loosened.
        forwarder.send_notification.assert_called_once_with(
            "Google STT", "Transcription service ready", kind="ready",
            capture_backend="winrt")

    def test_a_failed_capture_sends_the_failure_notification(self):
        forwarder = MagicMock()
        server_main.send_startup_notification(
            forwarder, None, _mic(False, "Device unavailable"),
            _running(), _lock(), _failed())
        _args, kwargs = forwarder.send_notification.call_args
        assert kwargs["kind"] == "startup_failed"

    def test_it_does_not_sleep(self):
        """The 3.0 second timer was the whole mechanism. Nothing
        replaces it: WSForwarder.start() creates its loop and queue
        synchronously before starting its thread
        (shared_stt/ws_forwarder.py:163-164), the queue is unbounded,
        and the disconnect clear runs only after a connection that had
        already succeeded is lost, so a notice queued before the first
        connection is delivered when that connection opens."""
        with patch.object(server_main.time, 'sleep') as slept:
            server_main.send_startup_notification(
                MagicMock(), None, _mic(True), _running(), _lock(),
                _failed())
        slept.assert_not_called()


class TestAnIntentionalShutdownSendsNoNotice:
    """A stop that lands while the handshake is still waiting.

    mic.stop() clears _capture_alive and joins the capture thread
    (shared_audio/capture/winrt_capture.py:260-298), that thread sets
    _setup_done on every exit path (:620), and wait_ready() then returns
    False with setup_error still None (:356) -- the same answer a dead
    microphone gives. main()'s finally block stops the microphone before
    it stops the forwarder, so the notice is still deliverable and the
    user reads a startup failure for a stop they asked for.
    """

    def _send_while_stopping(self, ready):
        """Run the sender, set the event mid-wait, release the wait.

        The handshake blocks on an event this method owns, so the stop
        is guaranteed to land inside the wait rather than before or
        after it. Returns the forwarder that was offered the notice.
        """
        forwarder = MagicMock()
        shutting_down = threading.Event()
        waiting = threading.Event()
        release = threading.Event()

        def blocking_wait_ready():
            waiting.set()
            release.wait(5)
            return ready

        mic = SimpleNamespace(
            wait_ready=Mock(side_effect=blocking_wait_ready),
            setup_error=None,
        )
        sending = threading.Thread(
            target=server_main.send_startup_notification,
            args=(forwarder, None, mic, shutting_down, _lock(),
                  _failed()))
        sending.start()
        assert waiting.wait(5), "the handshake never started"
        shutting_down.set()
        release.set()
        sending.join(5)
        assert not sending.is_alive(), "the sender never finished"
        return forwarder

    def test_a_stop_during_the_wait_sends_no_failure_notice(self):
        """The stop releases wait_ready() as False, which is the same
        answer a dead microphone gives."""
        forwarder = self._send_while_stopping(ready=False)
        forwarder.send_notification.assert_not_called()

    def test_a_stop_during_the_wait_sends_no_ready_notice(self):
        """"Transcription service ready" for a service that is stopping
        is as wrong as the failure notice, so both answers are
        suppressed."""
        forwarder = self._send_while_stopping(ready=True)
        forwarder.send_notification.assert_not_called()


class TestAStopCannotLandBetweenTheGuardAndTheSend:
    """wh-provider-ready-handshake.1.2: the read and the send are one
    step, not two.

    The shutdown read above is a one-time read. A stop that lands after
    it and before forwarder.send_notification still reaches the user,
    because WSForwarder.stop() gives already-queued frames a bounded 2.0
    second chance to deliver before it stops its loop
    (shared_stt/ws_forwarder.py:912-953). The tests above cannot catch
    that: each sets the event while wait_ready() is still blocked, which
    is before the read, and the read then reports the stop correctly.

    The lock cannot cancel a send the sender has already decided on, so
    these tests assert what it does guarantee: the notice is never
    decided on and handed to the forwarder's loop while the recorded
    shutdown already says stopping, and teardown -- mic.stop(), with
    forwarder.stop() behind it -- has not begun at that hand-off.

    The hand-off is the boundary, not queue acceptance:
    send_notification schedules the queue put with
    asyncio.run_coroutine_threadsafe and never waits on the future
    (shared_stt/ws_forwarder.py:848-874), so the lock does not order
    that put at all: the forwarder's loop can run it before or after the
    lock is released (wh-provider-ready-handshake.1.3, .1.4).

    The stop goes through begin_shutdown, the real path main()'s finally
    takes; a bare shutting_down.set() takes no lock and would make these
    vacuous.
    """

    def _state_when_the_notice_is_sent(self, ready):
        """Pause the sender between the read and its send, request the
        shutdown from a second thread in the order main()'s finally
        uses, and report what the shutdown state read at the instant
        each notice was handed to the forwarder.
        """
        forwarder = MagicMock()
        shutting_down = threading.Event()
        notification_lock = threading.Lock()
        mic = SimpleNamespace(
            wait_ready=Mock(return_value=ready),
            setup_error="Device unavailable",
            stop=Mock(),
        )
        at_the_send = threading.Event()
        release = threading.Event()
        stopped = threading.Event()
        observed = []

        forwarder.send_notification = Mock(
            side_effect=lambda *a, **k: observed.append(
                (shutting_down.is_set(), mic.stop.called)))

        # The pause rides on the last log line before the send. A
        # logging.Handler cannot carry it: Handler.handle() holds that
        # handler's own lock across emit(), so a pause inside emit()
        # would block any line the shutdown thread writes as well.
        real_info = server_main.logger.info

        def pausing(message, *args, **kwargs):
            real_info(message, *args, **kwargs)
            if "Sending startup notification" in str(message) \
                    and not at_the_send.is_set():
                at_the_send.set()
                release.wait(5)

        with patch.object(server_main.logger, 'info', side_effect=pausing):
            sending = threading.Thread(
                target=server_main.send_startup_notification,
                args=(forwarder, None, mic, shutting_down,
                      notification_lock, threading.Event()))
            sending.start()
            assert at_the_send.wait(5), "the sender never reached its send"

            def request_the_stop():
                # main()'s finally in its own order: the shutdown is
                # recorded first, and only then does teardown touch the
                # microphone.
                server_main.begin_shutdown(shutting_down, notification_lock)
                mic.stop()
                stopped.set()

            stopping = threading.Thread(target=request_the_stop)
            stopping.start()
            # Bounded on purpose. With the serialization in place
            # begin_shutdown is blocked on the lock and this wait
            # expires, which is the passing case; without it the
            # shutdown runs straight through and this returns at once,
            # which is the case the assertions reject.
            stopped.wait(1.0)
            release.set()
            sending.join(5)
            stopping.join(5)
        assert not sending.is_alive(), "the sender never finished"
        assert not stopping.is_alive(), "the shutdown never finished"
        return observed

    def test_the_ready_notice_is_never_sent_into_a_stopping_server(self):
        observed = self._state_when_the_notice_is_sent(ready=True)
        assert len(observed) == 1, "the sender sent nothing"
        shutting_down_at_send, capture_stopped = observed[0]
        assert shutting_down_at_send is False, \
            "the ready notice was sent after the stop was recorded"
        assert capture_stopped is False, \
            "the ready notice was sent after teardown had started"

    def test_the_failure_notice_is_never_sent_into_a_stopping_server(
            self):
        observed = self._state_when_the_notice_is_sent(ready=False)
        assert len(observed) == 1, "the sender sent nothing"
        shutting_down_at_send, capture_stopped = observed[0]
        assert shutting_down_at_send is False, \
            "the failure notice was sent after the stop was recorded"
        assert capture_stopped is False, \
            "the failure notice was sent after teardown had started"
