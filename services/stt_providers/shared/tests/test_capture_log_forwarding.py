"""Capture-path log records reach the wheelhouse log, rate-limited.

wh-stt-load-metrics.2. The capture path logs to the shared_audio logger tree.
The Parakeet provider attaches its WebSocketLogHandler only to its own logger
and sets logging.getLogger('shared_stt').propagate = False, and the launcher
inherits the provider's console without redirecting it. So every capture-path
record -- the overflow-threshold warning, the device-open error, and
OverflowMonitor's rate-limited summary -- reaches the provider console and
nothing else. The overflow evidence the load investigation needs is invisible
off-console.

Google STT already forwards one of those loggers
(google_stt_server/main.py:1197). This adds the same forwarding to Parakeet,
for the whole capture path rather than one logger, with a bound on how many
lines a storm can produce.

The bound is per logger, not per handler. A microphone dropping frames writes
to shared_audio.capture.winrt_capture several times a second; a single shared budget
would let that storm suppress OverflowMonitor's summary, which is the one
record the investigation actually needs.
"""
import logging
import logging.handlers
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_stt.ws_forwarder import RateLimitedWebSocketLogHandler


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _FakeForwarder:
    """Records what a handler asked to send, without a WebSocket."""

    def __init__(self, is_connected=True):
        self.sent = []
        self._current_trace_id = None
        # Defaults True so every test written before
        # wh-stt-load-metrics.2.1.7 keeps asserting what it asserted.
        self.is_connected = is_connected

    def send_log(self, level, message, source, timestamp=None, trace_id=None):
        self.sent.append({
            'level': level,
            'message': message,
            'source': source,
            'timestamp': timestamp,
            'trace_id': trace_id,
        })


def _record(name='shared_audio.capture.winrt_capture', msg='[mic] something',
            level=logging.WARNING):
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )


def _unformattable_record(name='shared_audio.capture.winrt_capture'):
    """A record logging accepts but formatting cannot render.

    logging lets a caller write logger.warning('dropped %d frames', 'lots').
    LogRecord takes it without complaint, and the failure surfaces later, in
    the handler, when getMessage() evaluates msg % args.
    """
    return logging.LogRecord(
        name=name, level=logging.WARNING, pathname=__file__, lineno=1,
        msg='dropped %d frames', args=('not-an-int',), exc_info=None,
    )


def _handler(forwarder, clock, per_window=5, window_s=30.0):
    handler = RateLimitedWebSocketLogHandler(
        forwarder, source='Parakeet',
        max_records_per_window=per_window,
        window_seconds=window_s,
        clock=clock,
    )
    handler.setLevel(logging.INFO)
    return handler


def _messages(forwarder):
    return [entry['message'] for entry in forwarder.sent]


class TestTheCapturePathReachesTheWheelhouseLog:
    """A capture-path record is forwarded through the provider's channel."""

    def test_a_capture_warning_is_forwarded(self):
        forwarder = _FakeForwarder()
        handler = _handler(forwarder, _Clock())

        handler.handle(_record(msg='[mic] Overflow threshold exceeded'))

        assert len(forwarder.sent) == 1
        assert '[mic] Overflow threshold exceeded' in forwarder.sent[0]['message']
        assert forwarder.sent[0]['level'] == 'WARNING'
        assert forwarder.sent[0]['source'] == 'Parakeet'

    def test_the_overflow_summary_is_forwarded(self):
        """The record the load investigation needs off-console."""
        forwarder = _FakeForwarder()
        handler = _handler(forwarder, _Clock())

        handler.handle(_record(
            name='shared_audio.overflow_monitor', level=logging.INFO,
            msg='[overflow] 7 audio input overflows in the last 30s'))

        assert len(forwarder.sent) == 1
        assert '[overflow] 7 audio input overflows' in forwarder.sent[0]['message']


class TestAStormIsBounded:
    """A sustained overflow cannot flood the log or the channel."""

    def test_records_past_the_cap_are_not_forwarded(self):
        forwarder = _FakeForwarder()
        handler = _handler(forwarder, _Clock(), per_window=5)

        for i in range(40):
            handler.handle(_record(msg=f'[mic] storm {i}'))

        forwarded = [m for m in _messages(forwarder) if '[mic] storm' in m]
        assert len(forwarded) == 5, (
            'a forty-record storm produced %d forwarded lines; the cap is 5'
            % len(forwarded))

    def test_the_next_window_forwards_again(self):
        forwarder = _FakeForwarder()
        clock = _Clock()
        handler = _handler(forwarder, clock, per_window=5, window_s=30.0)

        for i in range(40):
            handler.handle(_record(msg=f'[mic] first {i}'))
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] second window'))

        assert '[mic] second window' in ' '.join(_messages(forwarder))

    def test_a_suppressed_window_says_how_many_it_dropped(self):
        """Bounded, never silent: the count is the evidence that survives."""
        forwarder = _FakeForwarder()
        clock = _Clock()
        handler = _handler(forwarder, clock, per_window=5, window_s=30.0)

        for i in range(40):
            handler.handle(_record(msg=f'[mic] storm {i}'))
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] after'))

        joined = ' '.join(_messages(forwarder))
        assert '35' in joined, (
            'the window suppressed 35 records and the log never said so: %s'
            % joined)
        assert 'shared_audio.capture.winrt_capture' in joined

    def test_a_window_that_suppressed_nothing_adds_no_line(self):
        forwarder = _FakeForwarder()
        clock = _Clock()
        handler = _handler(forwarder, clock, per_window=5, window_s=30.0)

        handler.handle(_record(msg='[mic] one'))
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] two'))

        assert len(forwarder.sent) == 2, _messages(forwarder)


class TestOneLoggersStormCannotSilenceAnother:
    """The bound is per logger. Otherwise the noisy path hides the useful one."""

    def test_the_overflow_summary_survives_a_microphone_storm(self):
        forwarder = _FakeForwarder()
        handler = _handler(forwarder, _Clock(), per_window=5)

        for i in range(40):
            handler.handle(_record(msg=f'[mic] storm {i}'))
        handler.handle(_record(
            name='shared_audio.overflow_monitor', level=logging.INFO,
            msg='[overflow] 7 audio input overflows in the last 30s'))

        assert '[overflow] 7 audio input overflows' in ' '.join(
            _messages(forwarder)), (
            'a microphone storm consumed the budget and suppressed the '
            'overflow summary, which is the record the investigation needs')


class TestTheProviderConsoleIsUnchanged:
    """The rate limiting lives in this handler, not in the record."""

    def test_a_suppressed_record_still_reaches_another_handler(self):
        """A logging.Filter that rewrote the record would change the console.

        A LogRecord is shared by every handler the emit walk visits, so
        rate limiting that annotates or drops at the record level changes
        what the provider prints. This handler drops its own output only.
        """
        forwarder = _FakeForwarder()
        handler = _handler(forwarder, _Clock(), per_window=1)
        console = logging.handlers.MemoryHandler(capacity=1000)
        console.setLevel(logging.INFO)

        log = logging.getLogger('shared_audio.capture.winrt_capture')
        log.setLevel(logging.INFO)
        log.addHandler(handler)
        log.addHandler(console)
        try:
            for i in range(10):
                log.warning('[mic] storm %d', i)
        finally:
            log.removeHandler(handler)
            log.removeHandler(console)

        assert len(console.buffer) == 10, (
            'the console lost records to the forwarder rate limit')
        forwarded = [m for m in _messages(forwarder) if '[mic] storm' in m]
        assert len(forwarded) == 1

    def test_the_record_message_is_not_rewritten(self):
        forwarder = _FakeForwarder()
        handler = _handler(forwarder, _Clock(), per_window=1)
        record = _record(msg='[mic] storm')

        handler.handle(record)
        handler.handle(record)

        assert record.msg == '[mic] storm'


class TestTheWindowUsesAMonotonicClock:
    """time.time() can jump backward; the summary must not stop for it."""

    def test_the_default_clock_is_monotonic(self):
        import time as time_module

        forwarder = _FakeForwarder()
        handler = RateLimitedWebSocketLogHandler(forwarder, source='Parakeet')

        assert handler._clock is time_module.monotonic


class TestABacklogCannotBuildWhileWheelhouseIsUnreachable:
    """Rate limiting bounds the rate, not the backlog. This bounds the backlog.

    Codex round 5 (wh-stt-load-metrics.2.1.7). WSForwarder's outbound queue is
    unbounded, is created before any connection exists, and is cleared only
    after a connection that had already succeeded is lost -- a run of INITIAL
    connect failures keeps every queued item. This handler admits five records
    per logger per 30-second window, so a capture path that keeps failing
    while WheelHouse is unreachable adds to that queue for the whole outage.
    The queue is also the one transcripts use, so the whole backlog would go
    out ahead of the first live transcript.

    The fix belongs on this handler, not on the queue: this change is what
    added the capture path as a producer, and the queue's clear policy is
    shared with transcripts and with every other provider.
    """

    def test_a_record_written_while_disconnected_is_not_forwarded(self):
        forwarder = _FakeForwarder(is_connected=False)
        handler = _handler(forwarder, _Clock())

        handler.handle(_record(msg='[mic] Overflow threshold exceeded'))

        assert forwarder.sent == []

    def test_the_records_dropped_while_disconnected_are_reported(self):
        """Bounded, but never silent -- the same property the window has."""
        forwarder = _FakeForwarder(is_connected=False)
        handler = _handler(forwarder, _Clock())

        for _ in range(3):
            handler.handle(_record(msg='[mic] frames dropped'))
        forwarder.is_connected = True
        handler.handle(_record(msg='[mic] frames dropped'))

        messages = _messages(forwarder)
        assert len(messages) == 2
        assert '3' in messages[0]
        assert 'shared_audio.capture.winrt_capture' in messages[0]
        assert forwarder.sent[0]['level'] == 'WARNING'
        assert '[mic] frames dropped' in messages[1]

    def test_the_report_names_each_logger_that_lost_records(self):
        forwarder = _FakeForwarder(is_connected=False)
        handler = _handler(forwarder, _Clock())

        handler.handle(_record(name='shared_audio.capture.winrt_capture', msg='a'))
        handler.handle(_record(name='shared_audio.overflow_monitor', msg='b'))
        forwarder.is_connected = True
        handler.handle(_record(name='shared_audio.capture.winrt_capture', msg='c'))

        report = _messages(forwarder)[0]
        assert 'shared_audio.capture.winrt_capture' in report
        assert 'shared_audio.overflow_monitor' in report

    def test_a_disconnected_drop_does_not_consume_the_window_budget(self):
        """The outage must not spend the budget the recovery needs."""
        forwarder = _FakeForwarder(is_connected=False)
        clock = _Clock()
        handler = _handler(forwarder, clock, per_window=5)

        for _ in range(10):
            handler.handle(_record(msg='[mic] during the outage'))
        forwarder.is_connected = True
        for index in range(6):
            handler.handle(_record(msg=f'[mic] after {index}'))

        forwarded = [m for m in _messages(forwarder) if '[mic] after' in m]
        assert len(forwarded) == 5

    def test_a_forwarder_that_cannot_answer_is_treated_as_connected(self):
        """Never lose evidence because the connectivity read failed.

        Dropping is the safe direction only when the answer is a definite no.
        An older forwarder with no accessor at all, or one whose property
        raises, must still forward: this handler exists to make the capture
        path visible, and silence is the failure it was written to remove.
        """
        class _NoAnswerForwarder(_FakeForwarder):
            def __init__(self):
                self.sent = []
                self._current_trace_id = None

            @property
            def is_connected(self):
                raise RuntimeError("cannot tell")

        forwarder = _NoAnswerForwarder()
        handler = _handler(forwarder, _Clock())

        handler.handle(_record(msg='[mic] still worth sending'))

        assert len(forwarder.sent) == 1
        assert '[mic] still worth sending' in forwarder.sent[0]['message']


class TestTheForwarderAnswersWhetherItIsConnected:
    """The handler's backlog bound is only as good as this one accessor.

    wh-stt-load-metrics.2.1.7. RateLimitedWebSocketLogHandler drops records
    while WheelHouse is unreachable, and it learns that from this property.
    An accessor reading the wrong field would disable the whole bound in
    silence, so the accessor is pinned on its own.
    """

    def _forwarder(self):
        from shared_stt.ws_forwarder import WSForwarder

        return WSForwarder.__new__(WSForwarder)

    def test_a_forwarder_that_never_connected_is_not_connected(self):
        forwarder = self._forwarder()
        forwarder._ws_connected = False

        assert forwarder.is_connected is False

    def test_a_connected_forwarder_says_so(self):
        forwarder = self._forwarder()
        forwarder._ws_connected = True

        assert forwarder.is_connected is True


class TestAReportThatNeverArrivesIsNotLost:
    """A queued report can still be lost, so the counts behind it are cumulative.

    Codex round 6 (wh-stt-load-metrics.2.1.8). WSForwarder.send_log returns no
    acceptance or delivery answer: it swallows every exception from
    run_coroutine_threadsafe and returns None. A report can therefore be lost
    AFTER it is queued. The route this docstring first named -- a failed send
    re-queued at the tail, then discarded by _clear_queue on the disconnect --
    was closed by wh-forwarded-log-time-order defect 2: _clear_queue now KEEPS
    log frames, and a log frame whose send failed waits in the forwarder's
    _pending_log slot and leads the next connection. What remains is process
    exit before delivery: before the queue drains, or with the frame still in
    that slot, which stop() never waits on. If the handler cleared its counts
    when it queued the report, that outage would be permanently silent while
    later outages kept counting.

    Both counters are cumulative instead, so the next report carries every
    earlier one. That is why these tests read the SECOND report.
    """

    def test_a_lost_outage_report_is_recovered_by_the_next_one(self):
        forwarder = _FakeForwarder(is_connected=False)
        handler = _handler(forwarder, _Clock())

        for _ in range(3):
            handler.handle(_record(msg='[mic] first outage'))
        forwarder.is_connected = True
        handler.handle(_record(msg='[mic] recovery one'))

        # The report reached the forwarder and was then lost before delivery,
        # which is exactly what the handler cannot observe. _clear_queue keeps
        # log frames since wh-forwarded-log-time-order defect 2, so the
        # reachable loss is process exit before delivery -- before the queue
        # drains, or with the frame in the pending slot that stop() never
        # waits on; the sent.clear() below stands in for it.
        forwarder.sent.clear()
        forwarder.is_connected = False
        for _ in range(2):
            handler.handle(_record(msg='[mic] second outage'))
        forwarder.is_connected = True
        handler.handle(_record(msg='[mic] recovery two'))

        report = _messages(forwarder)[0]
        assert '5 capture-path records' in report, (
            'the second report must carry the three drops whose report was '
            'lost as well as the two new ones: %s' % report)

    def test_a_recovery_with_no_new_drops_does_not_repeat_the_report(self):
        """Cumulative must not mean repeated. The watermark is what stops it."""
        forwarder = _FakeForwarder(is_connected=False)
        handler = _handler(forwarder, _Clock())

        handler.handle(_record(msg='[mic] outage'))
        forwarder.is_connected = True
        handler.handle(_record(msg='[mic] recovery one'))
        handler.handle(_record(msg='[mic] recovery two'))

        reports = [m for m in _messages(forwarder)
                   if 'capture-path records' in m]
        assert len(reports) == 1, _messages(forwarder)

    def test_a_lost_suppression_report_is_recovered_by_the_next_one(self):
        """The sibling instance codex named: same send, same loss, same cure."""
        forwarder = _FakeForwarder()
        clock = _Clock()
        handler = _handler(forwarder, clock, per_window=5, window_s=30.0)

        for index in range(10):
            handler.handle(_record(msg=f'[mic] first storm {index}'))
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] rollover one'))

        forwarder.sent.clear()
        for index in range(10):
            handler.handle(_record(msg=f'[mic] second storm {index}'))
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] rollover two'))

        reports = [m for m in _messages(forwarder) if 'cap 5 per' in m]
        assert len(reports) == 1, _messages(forwarder)
        assert '11 shared_audio.capture.winrt_capture' in reports[0], (
            'the first window suppressed 5 and the second 6; a report that '
            'only names 6 has lost the first window: %s' % reports[0])

    def test_a_window_that_adds_no_suppression_does_not_repeat_the_report(self):
        forwarder = _FakeForwarder()
        clock = _Clock()
        handler = _handler(forwarder, clock, per_window=5, window_s=30.0)

        for index in range(10):
            handler.handle(_record(msg=f'[mic] storm {index}'))
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] rollover one'))
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] rollover two'))

        reports = [m for m in _messages(forwarder) if 'cap 5 per' in m]
        assert len(reports) == 1, _messages(forwarder)
class TestASuppressionReportLostToADisconnectIsSentAgain:
    """The watermark records an attempt, and an attempt can still be lost.

    Codex round 7 (wh-stt-load-metrics.2.1.9). _forward_suppression_count
    writes _reported_suppressed[name] before the send, and send_log gives no
    delivery answer. If that report is then lost, the running total still
    equals the watermark, so the cumulative counter -- which round 6 added
    precisely to survive a lost report -- goes on matching and that logger
    never reports again.

    The loss route has changed since this was written. _clear_queue no longer
    discards a queued log frame (wh-forwarded-log-time-order defect 2), so a
    queued report usually SURVIVES a disconnect; what remains is process exit
    before delivery -- before the queue drains, or with the frame in the
    pending slot, which stop() never waits on. The watermark reset in
    _on_disconnected_drop is
    what this test drives, and it still fires on the first dropped record, so
    a report that did arrive can be counted a second time -- the accepted
    trade-off marked crewcut: at that reset.

    Worse than the outage counter, which self-heals: every observed outage
    drops a record and so pushes the drop total past its watermark. A
    suppression total moves only when a window actually suppresses, and a
    quiet logger never suppresses again.
    """

    def test_a_disconnect_makes_the_next_window_report_the_count_again(self):
        forwarder = _FakeForwarder()
        clock = _Clock()
        handler = _handler(forwarder, clock, per_window=5, window_s=30.0)

        for index in range(10):
            handler.handle(_record(msg=f'[mic] storm {index}'))
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] rollover'))
        forwarder.sent.clear()

        forwarder.is_connected = False
        handler.handle(_record(msg='[mic] during the outage'))
        forwarder.is_connected = True
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] after recovery'))

        reports = [m for m in _messages(forwarder) if 'cap 5 per' in m]
        assert len(reports) == 1, _messages(forwarder)
        assert '5 shared_audio.capture.winrt_capture' in reports[0], (
            'the report lost across the disconnect was never sent again: %s'
            % reports[0])

    def test_a_logger_that_falls_silent_is_reported_by_another(self):
        """The other half: the quiet logger cannot report for itself.

        A capture path whose report was lost and which then stops writing
        altogether never opens another window, so nothing on its own logger
        can carry its count. Any logger opening a window reports every
        outstanding total, so a path that keeps working carries it instead.
        """
        forwarder = _FakeForwarder()
        clock = _Clock()
        handler = _handler(forwarder, clock, per_window=5, window_s=30.0)

        for index in range(10):
            handler.handle(_record(msg=f'[mic] storm {index}'))
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] rollover'))
        forwarder.sent.clear()

        forwarder.is_connected = False
        handler.handle(_record(msg='[mic] during the outage'))
        forwarder.is_connected = True

        handler.handle(_record(
            name='shared_audio.overflow_monitor', level=logging.INFO,
            msg='[overflow] 7 audio input overflows in the last 30s'))

        reports = [m for m in _messages(forwarder) if 'cap 5 per' in m]
        assert len(reports) == 1, _messages(forwarder)
        assert '5 shared_audio.capture.winrt_capture' in reports[0], (
            'the microphone went quiet after its report was lost, and '
            'the working logger did not carry its count: %s' % reports[0])

    def test_a_healthy_run_still_reports_each_total_once(self):
        """The rollback must not turn one count into a line every window."""
        forwarder = _FakeForwarder()
        clock = _Clock()
        handler = _handler(forwarder, clock, per_window=5, window_s=30.0)

        for index in range(10):
            handler.handle(_record(msg=f'[mic] storm {index}'))
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] rollover one'))
        clock.advance(30.0)
        handler.handle(_record(msg='[mic] rollover two'))
        handler.handle(_record(
            name='shared_audio.overflow_monitor', level=logging.INFO,
            msg='[overflow] quiet'))

        reports = [m for m in _messages(forwarder) if 'cap 5 per' in m]
        assert len(reports) == 1, _messages(forwarder)


class TestAFormatFailureDoesNotSpendTheWindowBudget:
    """wh-stt-load-metrics.2.1.14. A record that never left must not count.

    The window budget bounds how many records a storm can put in
    wheelhouse.log. A record whose args make formatting raise puts nothing
    there: WebSocketLogHandler.emit catches the failure and calls
    handleError, which writes to stderr and returns. Counting it against the
    budget spends a slot on a line nobody can read, and enough of them
    silence the capture warnings the load investigation exists to collect.
    """

    def test_a_record_that_could_not_be_formatted_leaves_the_budget_whole(
            self):
        forwarder = _FakeForwarder()
        handler = _handler(forwarder, _Clock(), per_window=2)
        errors = []
        handler.handleError = errors.append

        handler.handle(_unformattable_record())
        handler.handle(_unformattable_record())
        handler.handle(_record(msg='[mic] Overflow threshold exceeded'))
        handler.handle(_record(msg='[mic] a second real warning'))

        assert len(errors) == 2, 'the format failures were not reported'
        assert _messages(forwarder) == [
            '[mic] Overflow threshold exceeded',
            '[mic] a second real warning',
        ], (
            'two records that reached nobody spent the whole window budget, '
            'so the real warnings were suppressed: %s' % _messages(forwarder))

    def test_the_failure_is_still_reported_rather_than_swallowed(self):
        """The budget fix must not turn a broken record into silence."""
        forwarder = _FakeForwarder()
        handler = _handler(forwarder, _Clock())
        errors = []
        handler.handleError = errors.append

        handler.handle(_unformattable_record())

        assert len(errors) == 1, 'the format failure was swallowed'
        assert forwarder.sent == [], 'a record that cannot be formatted was sent'

    def test_a_forwarded_record_still_spends_a_slot(self):
        """The fix must not stop counting the records that do get through."""
        forwarder = _FakeForwarder()
        handler = _handler(forwarder, _Clock(), per_window=2)

        for index in range(4):
            handler.handle(_record(msg='[mic] storm %d' % index))

        assert _messages(forwarder) == ['[mic] storm 0', '[mic] storm 1'], (
            'the cap stopped bounding a storm: %s' % _messages(forwarder))


class TestThePlainHandlerIsUnchangedByTheBudgetFix:
    """Every other caller of WebSocketLogHandler.emit sees what it saw.

    wh-stt-load-metrics.2.1.14 moves the body of emit into a helper that
    reports whether the record was sent. Google STT attaches the plain
    handler (google_stt_server/main.py), so emit itself must keep returning
    None and must keep reporting a format failure through handleError.
    """

    def _plain(self, forwarder):
        from shared_stt.ws_forwarder import WebSocketLogHandler

        handler = WebSocketLogHandler(forwarder, source='Google STT')
        handler.setLevel(logging.INFO)
        return handler

    def test_the_plain_handler_forwards_a_record(self):
        forwarder = _FakeForwarder()
        handler = self._plain(forwarder)

        handler.handle(_record(msg='[mic] plain path'))

        assert _messages(forwarder) == ['[mic] plain path']

    def test_the_plain_handler_reports_a_format_failure(self):
        forwarder = _FakeForwarder()
        handler = self._plain(forwarder)
        errors = []
        handler.handleError = errors.append

        assert handler.emit(_unformattable_record()) is None, (
            'emit grew a return value its other callers do not expect')
        assert len(errors) == 1, 'the plain handler swallowed a format failure'
        assert forwarder.sent == []
