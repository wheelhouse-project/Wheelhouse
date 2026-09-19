"""The plain log handler drops records instead of queueing them while down.

wh-forwarded-log-time-order criterion 4, as replaced by the boss ruling of
2026-08-31.

RateLimitedWebSocketLogHandler already refuses to queue while WheelHouse is
unreachable (wh-stt-load-metrics.2.1.7), because the outbound queue is
unbounded, is created before any connection exists, and is cleared only after
a connection that had already succeeded is lost. A provider that starts while
WheelHouse is down therefore keeps everything it logged during the outage and
sends the whole backlog ahead of its first live transcript.

That reasoning was never about the capture path alone. It applies to any
producer on that queue, and the plain WebSocketLogHandler -- the one every
provider attaches to its own logger -- had no such test. These tests pin the
same behaviour on the base class, using the same is_connected accessor and the
same per-logger counting, so the two handlers cannot drift apart.

The earlier form of criterion 4 asked for a 200-frame drop-oldest bound on the
queue itself. It is retired: the measurement behind the number described
capture records reaching the queue during an outage, and after
wh-stt-load-metrics.2 merged those records no longer reach it.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_stt.ws_forwarder import WebSocketLogHandler


class _FakeForwarder:
    """Records what the handler asked to send, without a WebSocket."""

    def __init__(self, is_connected=True):
        self.sent = []
        self._current_trace_id = ''
        self.is_connected = is_connected

    def send_log(self, level, message, source, timestamp=None, trace_id=None):
        self.sent.append({'level': level, 'message': message,
                          'source': source, 'timestamp': timestamp})


class _SilentForwarder:
    """A forwarder from before is_connected existed."""

    def __init__(self):
        self.sent = []
        self._current_trace_id = ''

    def send_log(self, level, message, source, timestamp=None, trace_id=None):
        self.sent.append({'level': level, 'message': message,
                          'source': source, 'timestamp': timestamp})


def _record(name='parakeet.main', msg='provider says something'):
    return logging.LogRecord(
        name=name, level=logging.WARNING, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )


def _messages(forwarder):
    return [entry['message'] for entry in forwarder.sent]


def _reports(forwarder):
    return [m for m in _messages(forwarder) if 'have been dropped' in m]


class TestPlainHandlerDropsWhileUnreachable:
    def test_a_record_written_while_unreachable_is_not_queued(self):
        forwarder = _FakeForwarder(is_connected=False)
        handler = WebSocketLogHandler(forwarder, source='Parakeet')

        handler.handle(_record(msg='during the outage'))

        assert _messages(forwarder) == [], (
            'the record must not reach the unbounded queue while the '
            'connection is down: %s' % _messages(forwarder))

    def test_a_record_written_while_reachable_still_goes(self):
        """The gate must not cost the handler its whole reason to exist."""
        forwarder = _FakeForwarder(is_connected=True)
        handler = WebSocketLogHandler(forwarder, source='Parakeet')

        handler.handle(_record(msg='while connected'))

        assert _messages(forwarder) == ['while connected']

    def test_a_forwarder_that_cannot_answer_counts_as_reachable(self):
        """Silence is the failure this handler exists to remove.

        Dropping is safe only on a definite no, so a forwarder with no
        accessor at all is treated as up -- the same direction
        RateLimitedWebSocketLogHandler chose.
        """
        forwarder = _SilentForwarder()
        handler = WebSocketLogHandler(forwarder, source='Parakeet')

        handler.handle(_record(msg='no accessor here'))

        assert _messages(forwarder) == ['no accessor here']


class TestPlainHandlerCountsAndReportsDrops:
    def test_drops_are_counted_per_logger(self):
        """Per logger, not one total, so a report names where the loss was."""
        forwarder = _FakeForwarder(is_connected=False)
        handler = WebSocketLogHandler(forwarder, source='Parakeet')

        for _ in range(3):
            handler.handle(_record(name='parakeet.main'))
        for _ in range(2):
            handler.handle(_record(name='parakeet.audio'))

        assert handler._disconnected_drops == {
            'parakeet.main': 3,
            'parakeet.audio': 2,
        }

    def test_the_first_record_after_reconnect_reports_the_total(self):
        forwarder = _FakeForwarder(is_connected=False)
        handler = WebSocketLogHandler(forwarder, source='Parakeet')

        for _ in range(3):
            handler.handle(_record(name='parakeet.main'))
        for _ in range(2):
            handler.handle(_record(name='parakeet.audio'))
        forwarder.is_connected = True
        handler.handle(_record(msg='back up'))

        reports = _reports(forwarder)
        assert len(reports) == 1, _messages(forwarder)
        assert '5' in reports[0]
        assert 'parakeet.main 3' in reports[0]
        assert 'parakeet.audio 2' in reports[0]
        assert 'back up' in _messages(forwarder), (
            'the record that triggered the report must be forwarded too')

    def test_an_outage_with_no_drops_reports_nothing(self):
        """A watermark, not a flag: a reconnect on its own says nothing."""
        forwarder = _FakeForwarder(is_connected=True)
        handler = WebSocketLogHandler(forwarder, source='Parakeet')

        handler.handle(_record(msg='one'))
        handler.handle(_record(msg='two'))

        assert _reports(forwarder) == []

    def test_the_report_is_not_repeated_while_nothing_new_drops(self):
        forwarder = _FakeForwarder(is_connected=False)
        handler = WebSocketLogHandler(forwarder, source='Parakeet')

        handler.handle(_record())
        forwarder.is_connected = True
        handler.handle(_record(msg='recovery one'))
        handler.handle(_record(msg='recovery two'))

        assert len(_reports(forwarder)) == 1, _messages(forwarder)
