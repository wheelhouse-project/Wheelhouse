"""The detector that tells StateManager an app holds the speaker in exclusive mode.

wh-kodi-playback-echo-stutter, stage 2. ExclusiveSpeakerMonitor watches the
default render endpoint and publishes SpeakerExclusiveStateChangedEvent when an
app takes or gives back sole control of it (acceptance items 4 to 7).

Two methods, both with the COM layer replaced by FakeBackend:
- method B: a shared-mode stream that is initialized and never started; a
  session disconnect with reason 5 (exclusive-mode override) means held.
- method A: a fresh shared-mode initialize on the endpoint; at every poll
  while held or while method B has no stream, otherwise every 4.5 s (backup);
  AUDCLNT_E_DEVICE_IN_USE means held, S_OK means free.

No test here touches a real speaker.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import SpeakerExclusiveStateChangedEvent
from services.wheelhouse.handlers import exclusive_speaker_monitor as esm

LOGGER_NAME = "services.wheelhouse.handlers.exclusive_speaker_monitor"
IN_USE = 0x8889000A
EP1 = ("{ep-1}", "Speakers A")
EP2 = ("{ep-2}", "Headphones B")


class FakeBackend:
    """The COM layer, scripted. Records the thread of every call."""

    def __init__(self):
        self.endpoint = EP1  # a tuple, None (no endpoint) or an exception to raise
        self.a_result = 0  # an HRESULT int, or an exception to raise
        self.open_error = None  # an HRESULT int or an exception to raise
        self.init_error = None
        self.block = None  # an Event: check_in_use waits on it
        self.entered = threading.Event()
        self.callbacks = []  # the on_disconnect of every opened stream
        self.opened_endpoints = []
        self.closed = []
        self.a_checks = 0
        self.calls = []
        self.initialized = False
        self.shut_down = False

    def _note(self, name):
        self.calls.append((name, threading.get_ident()))

    def initialize(self):
        self._note("initialize")
        if self.init_error is not None:
            raise self.init_error
        self.initialized = True

    def shutdown(self):
        self._note("shutdown")
        self.shut_down = True

    def default_endpoint(self):
        self._note("default_endpoint")
        if isinstance(self.endpoint, BaseException):
            raise self.endpoint
        return self.endpoint

    def check_in_use(self, endpoint_id):
        self._note("check_in_use")
        self.a_checks += 1
        if self.block is not None:
            self.entered.set()
            self.block.wait(10)
        if isinstance(self.a_result, BaseException):
            raise self.a_result
        return self.a_result

    def open_held_stream(self, endpoint_id, on_disconnect):
        self._note("open_held_stream")
        if isinstance(self.open_error, BaseException):
            raise self.open_error
        if self.open_error is not None:
            raise esm.BackendHResultError(self.open_error)
        self.opened_endpoints.append(endpoint_id)
        self.callbacks.append(on_disconnect)
        return object()

    def close_held_stream(self, handle):
        self._note("close_held_stream")
        self.closed.append(handle)


def make_monitor(fake, poll_interval=esm.POLL_INTERVAL_SECONDS, backup_interval=None):
    """A monitor whose hand-back runs inline, so no event loop is needed.

    backup_interval=None keeps the constructor default (4.5 s). 0.0 makes the
    backup method A check due at every poll.
    """
    loop = Mock()
    loop.call_soon_threadsafe.side_effect = lambda fn, *args: fn(*args)
    bus = Mock()
    extra = {} if backup_interval is None else {"backup_interval": backup_interval}
    monitor = esm.ExclusiveSpeakerMonitor(
        loop, bus, backend_factory=lambda: fake, poll_interval=poll_interval, **extra
    )
    monitor._backend = fake
    return monitor, bus


class FakeClock:
    """A settable time.monotonic for tests that call _poll_once directly."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    """Replaces only the monitor module's reference to `time`; the test's own
    timing helpers keep the real clock."""
    fake_clock = FakeClock()
    monkeypatch.setattr(esm, "time", SimpleNamespace(monotonic=fake_clock))
    return fake_clock


def published(bus):
    return [
        (c.args[0].held, c.args[0].endpoint_name, c.args[0].method)
        for c in bus.publish.call_args_list
    ]


def monitor_warnings(caplog):
    return [
        r for r in caplog.records
        if r.name == LOGGER_NAME and r.levelno == logging.WARNING
    ]


def wait_until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def monitor_threads():
    return [t for t in threading.enumerate() if t.name == esm.THREAD_NAME]


@pytest.fixture
def running():
    """Collects started monitors and fakes; stops them whatever the test did."""
    started = []
    yield started
    for monitor, fake in started:
        monitor.stop()
        if fake.block is not None:
            fake.block.set()
    wait_until(lambda: not monitor_threads(), timeout=5.0)


# ---------------------------------------------------------------- timing


class TestPollInterval:
    def test_default_interval_is_at_most_one_second(self):
        """Start <= 1 s and end <= 2 s need a check at least once per second."""
        assert esm.POLL_INTERVAL_SECONDS <= 0.5
        monitor, _ = make_monitor(FakeBackend())
        assert monitor.poll_interval <= 0.5

    def test_thread_polls_once_per_interval(self, running):
        """Method B has no stream here, so every poll runs method A."""
        fake = FakeBackend()
        fake.open_error = 0x88890008
        monitor, _ = make_monitor(fake, poll_interval=0.02)
        running.append((monitor, fake))
        monitor.start()
        assert wait_until(lambda: fake.a_checks >= 5, timeout=2.0)

    def test_thread_polls_the_endpoint_once_per_interval_while_method_a_is_idle(
        self, running
    ):
        """Free with a ready stream: the endpoint is read at every poll, method A is not."""
        fake = FakeBackend()
        monitor, _ = make_monitor(fake, poll_interval=0.02)
        running.append((monitor, fake))
        monitor.start()
        reads = lambda: [n for n, _ in fake.calls].count("default_endpoint")  # noqa: E731
        assert wait_until(lambda: reads() >= 5, timeout=2.0)
        assert fake.a_checks == 1

    def test_thread_does_not_check_faster_than_the_interval(self, running):
        """Method B has no stream, so method A runs at every poll and only the interval limits it."""
        fake = FakeBackend()
        fake.open_error = 0x88890008
        monitor, _ = make_monitor(fake, poll_interval=30.0)
        running.append((monitor, fake))
        monitor.start()
        assert wait_until(lambda: fake.a_checks >= 1)
        time.sleep(0.3)
        assert fake.a_checks == 1


# ---------------------------------------------------------------- method A


class TestMethodA:
    def test_in_use_answer_starts_held(self):
        fake = FakeBackend()
        fake.a_result = IN_USE
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        assert published(bus) == [(True, "Speakers A", esm.METHOD_SHARED_CHECK)]

    def test_s_ok_after_held_ends_it(self):
        fake = FakeBackend()
        fake.a_result = IN_USE
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        fake.a_result = 0
        monitor._poll_once()
        assert published(bus) == [
            (True, "Speakers A", esm.METHOD_SHARED_CHECK),
            (False, "Speakers A", esm.METHOD_SHARED_CHECK),
        ]

    def test_free_answer_from_the_start_publishes_nothing(self):
        fake = FakeBackend()
        monitor, bus = make_monitor(fake)
        for _ in range(4):
            monitor._poll_once()
        assert published(bus) == []

    def test_publishes_only_on_a_change_of_state(self):
        fake = FakeBackend()
        fake.a_result = IN_USE
        monitor, bus = make_monitor(fake)
        for _ in range(5):
            monitor._poll_once()
        assert len(published(bus)) == 1

    def test_checks_the_default_endpoint(self):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        seen = []
        original = fake.check_in_use
        fake.check_in_use = lambda endpoint_id: (seen.append(endpoint_id), original(endpoint_id))[1]
        monitor._poll_once()
        assert seen == [EP1[0]]


# ------------------------------------------- when method A runs (boss ruling 00:20)


class TestWhenMethodARuns:
    """Method A costs audiodg.exe CPU, so it runs at every poll only while the
    speaker is held or while method B has no stream. While free with a ready
    stream it runs once per backup interval (4.5 s)."""

    def test_backup_constants_are_at_most_five_seconds(self):
        """Start <= 5 s when only method A sees it (acceptance item 2).

        The backup check runs at the first poll on or after its due time, so the
        worst case is the backup interval plus one poll interval.
        """
        assert esm.BACKUP_CHECK_SECONDS + esm.POLL_INTERVAL_SECONDS <= 5.0
        monitor, _ = make_monitor(FakeBackend())
        assert monitor.backup_interval + monitor.poll_interval <= 5.0

    def test_first_poll_runs_method_a(self, clock):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        monitor._poll_once()
        assert fake.a_checks == 1

    def test_while_free_with_a_ready_stream_a_poll_skips_method_a_until_the_backup_time(
        self, clock
    ):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        monitor._poll_once()  # first poll: method A runs, the stream opens
        assert fake.a_checks == 1
        assert len(fake.opened_endpoints) == 1
        backup = esm.BACKUP_CHECK_SECONDS
        polls_before = int(backup / 0.5) - 1  # 0.5 s ... backup - 0.5 s
        for _ in range(polls_before):
            clock.advance(0.5)
            monitor._poll_once()
        assert fake.a_checks == 1
        clock.advance(0.5)  # the backup time after the first check
        monitor._poll_once()
        assert fake.a_checks == 2

    def test_the_backup_time_follows_the_last_method_a_run(self, clock):
        backup = esm.BACKUP_CHECK_SECONDS
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        monitor._poll_once()
        clock.advance(backup)
        monitor._poll_once()  # backup check
        assert fake.a_checks == 2
        clock.advance(backup - 0.5)
        monitor._poll_once()
        assert fake.a_checks == 2  # not yet due again
        clock.advance(0.5)
        monitor._poll_once()
        assert fake.a_checks == 3

    def test_the_backup_interval_argument_sets_the_backup_time(self, clock):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake, backup_interval=2.0)
        monitor._poll_once()
        clock.advance(1.5)
        monitor._poll_once()
        assert fake.a_checks == 1
        clock.advance(0.5)
        monitor._poll_once()
        assert fake.a_checks == 2

    def test_while_held_every_poll_runs_method_a_and_s_ok_ends_the_pause(self, clock):
        fake = FakeBackend()
        fake.a_result = IN_USE
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        assert published(bus) == [(True, "Speakers A", esm.METHOD_SHARED_CHECK)]
        for expected in (2, 3, 4):
            clock.advance(0.5)
            monitor._poll_once()
            assert fake.a_checks == expected
        fake.a_result = 0  # the speaker is free again
        clock.advance(0.5)
        monitor._poll_once()
        assert fake.a_checks == 5
        assert published(bus)[-1] == (False, "Speakers A", esm.METHOD_SHARED_CHECK)

    @pytest.mark.parametrize(
        "open_error", [0x88890008, OSError("activate failed")], ids=["hresult", "exception"]
    )
    def test_while_method_b_is_not_ready_every_poll_runs_method_a(self, clock, open_error):
        fake = FakeBackend()
        fake.open_error = open_error
        monitor, _ = make_monitor(fake)
        for expected in (1, 2, 3, 4):
            monitor._poll_once()
            assert fake.a_checks == expected
            clock.advance(0.5)

    def test_method_a_stops_running_every_poll_once_method_b_is_ready(self, clock):
        fake = FakeBackend()
        fake.open_error = 0x88890008
        monitor, _ = make_monitor(fake)
        monitor._poll_once()
        monitor._poll_once()
        assert fake.a_checks == 2
        fake.open_error = None
        clock.advance(0.5)
        monitor._poll_once()  # method A runs (no stream yet), then the stream opens
        assert fake.a_checks == 3
        assert len(fake.opened_endpoints) == 1
        clock.advance(0.5)
        monitor._poll_once()
        assert fake.a_checks == 3

    def test_a_free_answer_after_a_held_period_opens_the_stream_in_the_same_poll(
        self, clock
    ):
        fake = FakeBackend()
        fake.a_result = IN_USE
        fake.open_error = IN_USE  # the real speaker refuses a stream while held
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        assert fake.opened_endpoints == []
        fake.a_result = 0
        fake.open_error = None
        clock.advance(0.5)
        monitor._poll_once()
        assert published(bus)[-1] == (False, "Speakers A", esm.METHOD_SHARED_CHECK)
        assert fake.opened_endpoints == [EP1[0]]

    def test_the_backup_check_catches_a_start_that_method_b_missed(self, clock):
        fake = FakeBackend()
        monitor, bus = make_monitor(fake)
        monitor._poll_once()  # free; the stream is ready
        fake.a_result = IN_USE  # an app takes the speaker; no callback arrives
        for _ in range(8):  # 0.5 s ... 4.0 s
            clock.advance(0.5)
            monitor._poll_once()
            assert published(bus) == []
        clock.advance(0.49)  # 4.49 s: a poll that wakes a little early
        monitor._poll_once()
        assert published(bus) == []
        clock.advance(0.5)  # 4.99 s: the next poll is still inside 5 s
        monitor._poll_once()
        assert published(bus) == [(True, "Speakers A", esm.METHOD_SHARED_CHECK)]

    def test_after_a_start_that_method_b_missed_a_fresh_stream_is_held(self, clock):
        """The old stream did not report, so it is released and a new one is opened."""
        fake = FakeBackend()
        monitor, bus = make_monitor(fake)
        monitor._poll_once()  # free; stream 1 is ready
        fake.a_result = IN_USE  # no callback arrives
        clock.advance(esm.BACKUP_CHECK_SECONDS)
        monitor._poll_once()
        assert published(bus)[-1][0] is True
        assert len(fake.closed) == 1  # stream 1 released at the in-use answer
        fake.a_result = 0
        clock.advance(0.5)
        monitor._poll_once()
        assert published(bus)[-1][0] is False
        assert fake.opened_endpoints == [EP1[0], EP1[0]]  # stream 2 opened

    def test_after_an_endpoint_change_the_first_poll_runs_method_a(self, clock):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        seen = []
        original = fake.check_in_use
        fake.check_in_use = lambda endpoint_id: (seen.append(endpoint_id), original(endpoint_id))[1]
        monitor._poll_once()
        clock.advance(0.5)
        monitor._poll_once()  # same endpoint, backup not due: no check
        assert seen == [EP1[0]]
        fake.endpoint = EP2
        clock.advance(0.5)
        monitor._poll_once()
        assert seen == [EP1[0], EP2[0]]

    def test_after_the_endpoint_goes_away_and_returns_the_first_poll_runs_method_a(
        self, clock
    ):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        monitor._poll_once()
        fake.endpoint = None
        clock.advance(0.5)
        monitor._poll_once()
        fake.endpoint = EP1
        clock.advance(0.5)
        monitor._poll_once()
        assert fake.a_checks == 2


# ---------------------------------------------------------------- method B


class TestMethodB:
    def test_reason_5_starts_held_without_waiting_for_the_next_check(self):
        fake = FakeBackend()
        monitor, bus = make_monitor(fake)
        monitor._poll_once()  # method A says free; the stream is now held open
        checks_before = fake.a_checks
        fake.callbacks[-1](esm.DISCONNECT_REASON_EXCLUSIVE_OVERRIDE)
        monitor._drain_notifications()
        assert published(bus) == [(True, "Speakers A", esm.METHOD_SESSION_DISCONNECT)]
        assert fake.a_checks == checks_before

    def test_reason_5_through_the_thread_publishes_before_the_next_check(self, running):
        fake = FakeBackend()
        monitor, bus = make_monitor(fake, poll_interval=30.0)
        running.append((monitor, fake))
        monitor.start()
        assert wait_until(lambda: fake.callbacks)
        t0 = time.monotonic()
        fake.callbacks[-1](5)
        assert wait_until(lambda: published(bus), timeout=1.0)
        assert time.monotonic() - t0 < 1.0
        assert fake.a_checks == 1
        assert published(bus) == [(True, "Speakers A", esm.METHOD_SESSION_DISCONNECT)]

    def test_held_stream_is_opened_on_the_default_endpoint_and_never_closed_while_free(self):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        for _ in range(3):
            monitor._poll_once()
        assert fake.opened_endpoints == [EP1[0]]
        assert fake.closed == []

    def test_dead_stream_is_replaced_at_the_next_check(self):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        monitor._poll_once()
        fake.callbacks[-1](5)
        monitor._drain_notifications()
        assert len(fake.closed) == 1
        monitor._poll_once()
        assert len(fake.opened_endpoints) == 2

    def test_reason_5_while_already_held_by_method_a_publishes_nothing_more(self):
        fake = FakeBackend()
        monitor, bus = make_monitor(fake, backup_interval=0.0)  # method A due at every poll
        monitor._poll_once()
        fake.a_result = IN_USE
        monitor._poll_once()
        assert len(published(bus)) == 1  # method A held it before the callback
        fake.callbacks[-1](5)
        monitor._drain_notifications()
        assert len(published(bus)) == 1

    def test_end_after_reason_5_comes_from_the_shared_check(self):
        fake = FakeBackend()
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        fake.callbacks[-1](5)
        monitor._drain_notifications()
        fake.open_error = IN_USE
        fake.a_result = IN_USE
        monitor._poll_once()
        fake.open_error = None
        fake.a_result = 0
        monitor._poll_once()
        assert published(bus) == [
            (True, "Speakers A", esm.METHOD_SESSION_DISCONNECT),
            (False, "Speakers A", esm.METHOD_SHARED_CHECK),
        ]

    def test_callback_from_a_replaced_stream_is_ignored(self):
        fake = FakeBackend()
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        old_callback = fake.callbacks[-1]
        old_callback(2)  # format changed: the stream dies, no pause
        monitor._drain_notifications()
        monitor._poll_once()
        assert len(fake.callbacks) == 2
        old_callback(5)
        monitor._drain_notifications()
        assert published(bus) == []

    def test_callback_only_queues_so_a_bad_reason_cannot_raise_on_the_com_thread(self):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        monitor._poll_once()
        fake.callbacks[-1]("not a number")  # must not raise on the COM thread
        monitor._drain_notifications()


# ------------------------------------------------- results that change nothing


class TestOtherResultsLeaveListeningAsIs:
    @pytest.mark.parametrize("held_first", [False, True], ids=["free", "held"])
    def test_other_hresult_changes_nothing_and_warns_once(self, caplog, held_first):
        fake = FakeBackend()
        fake.a_result = IN_USE if held_first else 0
        monitor, bus = make_monitor(fake, backup_interval=0.0)  # method A due at every poll
        monitor._poll_once()
        expected = published(bus)
        fake.a_result = 0x88890008  # AUDCLNT_E_UNSUPPORTED_FORMAT
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(6):
                monitor._poll_once()
        assert published(bus) == expected
        assert len(monitor_warnings(caplog)) == 1

    def test_two_kinds_of_hresult_warn_once_each(self, caplog):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake, backup_interval=0.0)  # method A due at every poll
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for result in (0x88890008, 0x88890004, 0x88890008, 0x88890004):
                fake.a_result = result
                monitor._poll_once()
        assert len(monitor_warnings(caplog)) == 2

    @pytest.mark.parametrize("held_first", [False, True], ids=["free", "held"])
    def test_com_exception_changes_nothing_and_warns_once(self, caplog, held_first):
        fake = FakeBackend()
        fake.a_result = IN_USE if held_first else 0
        monitor, bus = make_monitor(fake, backup_interval=0.0)  # method A due at every poll
        monitor._poll_once()
        expected = published(bus)
        fake.a_result = OSError("COM exploded")
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(6):
                monitor._poll_once()
        assert published(bus) == expected
        assert len(monitor_warnings(caplog)) == 1

    def test_no_render_endpoint_changes_nothing_and_warns_once(self, caplog):
        fake = FakeBackend()
        fake.a_result = 0
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        expected = published(bus)
        fake.endpoint = None
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(6):
                monitor._poll_once()
        assert published(bus) == expected
        assert len(monitor_warnings(caplog)) == 1

    def test_no_render_endpoint_ends_a_pause_the_old_speaker_caused(self, caplog):
        """Item 6: the default speaker went away, so the pause it caused ends."""
        fake = FakeBackend()
        fake.a_result = IN_USE
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        assert published(bus)[-1][0] is True
        fake.endpoint = None
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(6):
                monitor._poll_once()
        assert published(bus)[-1] == (False, EP1[1], esm.METHOD_DEFAULT_CHANGE)
        assert len([e for e in published(bus) if e[0] is False]) == 1
        assert len(monitor_warnings(caplog)) == 1

    def test_endpoint_query_failure_changes_nothing_and_warns_once(self, caplog):
        fake = FakeBackend()
        fake.endpoint = OSError("enumerator gone")
        monitor, bus = make_monitor(fake)
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(6):
                monitor._poll_once()
        assert published(bus) == []
        assert len(monitor_warnings(caplog)) == 1

    @pytest.mark.parametrize("reason", [0, 1, 2, 3, 4])
    def test_other_disconnect_reason_changes_nothing_and_warns_once(self, caplog, reason):
        fake = FakeBackend()
        monitor, bus = make_monitor(fake)
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(4):
                monitor._poll_once()  # opens a stream when none is held
                fake.callbacks[-1](reason)
                monitor._drain_notifications()
        assert published(bus) == []
        assert len(monitor_warnings(caplog)) == 1

    def test_two_disconnect_reasons_warn_once_each(self, caplog):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for reason in (0, 2, 0, 2):
                monitor._poll_once()
                fake.callbacks[-1](reason)
                monitor._drain_notifications()
        assert len(monitor_warnings(caplog)) == 2

    def test_other_disconnect_reason_does_not_end_a_pause(self):
        fake = FakeBackend()
        fake.a_result = IN_USE
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        fake.callbacks[-1](2)
        monitor._drain_notifications()
        assert published(bus) == [(True, "Speakers A", esm.METHOD_SHARED_CHECK)]

    def test_held_stream_refused_with_in_use_is_expected_and_silent(self, caplog):
        fake = FakeBackend()
        fake.a_result = IN_USE
        fake.open_error = IN_USE
        monitor, bus = make_monitor(fake)
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(5):
                monitor._poll_once()
        assert len(published(bus)) == 1
        assert monitor_warnings(caplog) == []

    def test_held_stream_refused_for_another_reason_warns_once(self, caplog):
        fake = FakeBackend()
        fake.open_error = 0x88890008
        monitor, bus = make_monitor(fake)
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(5):
                monitor._poll_once()
        assert published(bus) == []
        assert len(monitor_warnings(caplog)) == 1

    def test_held_stream_com_exception_warns_once(self, caplog):
        fake = FakeBackend()
        fake.open_error = OSError("activate failed")
        monitor, bus = make_monitor(fake)
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(5):
                monitor._poll_once()
        assert published(bus) == []
        assert len(monitor_warnings(caplog)) == 1

    @pytest.mark.parametrize("how", ["initialize", "factory"])
    def test_backend_that_fails_to_start_changes_nothing_and_warns_once(
        self, caplog, how
    ):
        fake = FakeBackend()
        bus = Mock()
        loop = Mock()
        loop.call_soon_threadsafe.side_effect = lambda fn, *args: fn(*args)
        if how == "initialize":
            fake.init_error = OSError("COM would not start")
            factory = lambda: fake  # noqa: E731
        else:
            def factory():
                raise ImportError("no comtypes")
        monitor = esm.ExclusiveSpeakerMonitor(
            loop, bus, backend_factory=factory, poll_interval=0.01
        )
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            monitor.start()
            assert wait_until(lambda: not monitor_threads(), timeout=3.0)
        assert bus.publish.call_count == 0
        assert len(monitor_warnings(caplog)) == 1
        assert fake.a_checks == 0


# ------------------------------------------------------------ device changes


class TestDefaultEndpointChange:
    def test_follows_the_new_default_and_closes_the_old_stream(self):
        fake = FakeBackend()
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        fake.endpoint = EP2
        monitor._poll_once()
        assert fake.opened_endpoints == [EP1[0], EP2[0]]
        assert len(fake.closed) == 1
        assert published(bus) == []

    def test_checks_the_new_endpoint_after_a_change(self):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        seen = []
        original = fake.check_in_use
        fake.check_in_use = lambda endpoint_id: (seen.append(endpoint_id), original(endpoint_id))[1]
        monitor._poll_once()
        fake.endpoint = EP2
        monitor._poll_once()
        assert seen == [EP1[0], EP2[0]]

    def test_a_pause_the_old_endpoint_caused_ends_on_the_change(self):
        fake = FakeBackend()
        fake.a_result = IN_USE
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        fake.endpoint = EP2
        fake.a_result = 0
        monitor._poll_once()
        assert published(bus) == [
            (True, "Speakers A", esm.METHOD_SHARED_CHECK),
            (False, "Speakers A", esm.METHOD_DEFAULT_CHANGE),
        ]

    def test_new_endpoint_that_is_also_held_pauses_again(self):
        fake = FakeBackend()
        fake.a_result = IN_USE
        monitor, bus = make_monitor(fake)
        monitor._poll_once()
        fake.endpoint = EP2
        monitor._poll_once()
        assert published(bus) == [
            (True, "Speakers A", esm.METHOD_SHARED_CHECK),
            (False, "Speakers A", esm.METHOD_DEFAULT_CHANGE),
            (True, "Headphones B", esm.METHOD_SHARED_CHECK),
        ]

    def test_stream_closed_after_session_death_is_not_closed_twice_on_a_change(self):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        monitor._poll_once()
        fake.callbacks[-1](5)
        monitor._drain_notifications()
        assert len(fake.closed) == 1
        fake.endpoint = EP2
        fake.open_error = IN_USE
        monitor._poll_once()
        assert len(fake.closed) == 1

    def test_change_is_seen_at_the_next_check(self):
        """The new default is followed by the very next check, within 2 s."""
        fake = FakeBackend()
        monitor, _ = make_monitor(fake)
        monitor._poll_once()
        fake.endpoint = EP2
        monitor._poll_once()
        assert fake.opened_endpoints[-1] == EP2[0]


# ----------------------------------------------------------- threads and exit


class TestThreading:
    def test_constructing_starts_no_thread(self):
        make_monitor(FakeBackend())
        assert monitor_threads() == []

    def test_work_runs_on_one_named_daemon_thread(self, running):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake, poll_interval=0.01, backup_interval=0.0)
        running.append((monitor, fake))
        monitor.start()
        assert wait_until(lambda: fake.a_checks >= 3)
        threads = monitor_threads()
        assert len(threads) == 1
        assert threads[0].daemon is True
        assert threads[0].ident != threading.get_ident()

    def test_every_backend_call_runs_off_the_calling_thread_on_one_thread(self, running):
        fake = FakeBackend()
        fake.a_result = IN_USE
        monitor, _ = make_monitor(fake, poll_interval=0.01)
        running.append((monitor, fake))
        monitor.start()
        assert wait_until(lambda: fake.a_checks >= 3)
        monitor.stop()
        assert wait_until(lambda: fake.shut_down)
        idents = {ident for _, ident in fake.calls}
        assert len(idents) == 1
        assert threading.get_ident() not in idents
        names = [name for name, _ in fake.calls]
        assert names[0] == "initialize"
        assert names[-1] == "shutdown"

    def test_start_twice_runs_one_thread(self, running):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake, poll_interval=0.01, backup_interval=0.0)
        running.append((monitor, fake))
        monitor.start()
        monitor.start()
        assert wait_until(lambda: fake.a_checks >= 2)
        assert len(monitor_threads()) == 1
        assert [n for n, _ in fake.calls].count("initialize") == 1

    def test_stop_returns_at_once_while_the_backend_blocks(self, running):
        fake = FakeBackend()
        fake.block = threading.Event()
        monitor, _ = make_monitor(fake, poll_interval=0.01)
        running.append((monitor, fake))
        monitor.start()
        assert fake.entered.wait(3.0)
        t0 = time.monotonic()
        monitor.stop()
        assert time.monotonic() - t0 < 0.5
        assert monitor_threads(), "the blocked thread is still alive; stop() did not wait"

    def test_nothing_is_published_after_stop(self, running):
        fake = FakeBackend()
        fake.block = threading.Event()
        fake.a_result = IN_USE
        monitor, bus = make_monitor(fake, poll_interval=0.01)
        running.append((monitor, fake))
        monitor.start()
        assert fake.entered.wait(3.0)
        monitor.stop()
        fake.block.set()
        assert wait_until(lambda: not monitor_threads(), timeout=3.0)
        assert published(bus) == []

    def test_stop_wakes_a_thread_that_is_waiting_between_checks(self, running):
        fake = FakeBackend()
        monitor, _ = make_monitor(fake, poll_interval=30.0)
        running.append((monitor, fake))
        monitor.start()
        assert wait_until(lambda: fake.a_checks >= 1)
        monitor.stop()
        assert wait_until(lambda: not monitor_threads(), timeout=2.0)
        assert fake.shut_down

    def test_stop_before_start_is_harmless(self):
        monitor, _ = make_monitor(FakeBackend())
        monitor.stop()

    def test_closed_loop_does_not_raise_from_the_thread(self):
        loop = asyncio.new_event_loop()
        loop.close()
        fake = FakeBackend()
        fake.a_result = IN_USE
        monitor = esm.ExclusiveSpeakerMonitor(loop, Mock(), backend_factory=lambda: fake)
        monitor._backend = fake
        monitor._poll_once()  # hands the event back to a closed loop

    def test_closed_loop_through_the_thread_leaves_no_thread_exception(self, running):
        loop = asyncio.new_event_loop()
        loop.close()
        fake = FakeBackend()
        fake.a_result = IN_USE
        errors = []
        monitor = esm.ExclusiveSpeakerMonitor(
            loop, Mock(), backend_factory=lambda: fake, poll_interval=0.01
        )
        running.append((monitor, fake))
        with patch.object(threading, "excepthook", lambda args: errors.append(args)):
            monitor.start()
            assert wait_until(lambda: fake.a_checks >= 3)
            monitor.stop()
            assert wait_until(lambda: not monitor_threads(), timeout=3.0)
        assert errors == []


class TestEventReachesTheLoop:
    async def test_event_is_delivered_to_a_subscriber_on_the_loop_thread(self):
        loop = asyncio.get_running_loop()
        bus = EventBus()
        received = []
        delivered = asyncio.Event()

        async def handler(event):
            received.append((event, threading.get_ident()))
            delivered.set()

        bus.subscribe(SpeakerExclusiveStateChangedEvent, handler)
        fake = FakeBackend()
        fake.a_result = IN_USE
        monitor = esm.ExclusiveSpeakerMonitor(
            loop, bus, backend_factory=lambda: fake, poll_interval=0.05
        )
        try:
            monitor.start()
            await asyncio.wait_for(delivered.wait(), timeout=3.0)
        finally:
            monitor.stop()
        event, ident = received[0]
        assert ident == threading.get_ident()
        assert isinstance(event, SpeakerExclusiveStateChangedEvent)
        assert (event.held, event.endpoint_name, event.method) == (
            True, "Speakers A", esm.METHOD_SHARED_CHECK,
        )


class SlowCheckBackend(FakeBackend):
    """Method A takes a moment, so an answer signalled before the poll ends is early."""

    def check_in_use(self, endpoint_id):
        time.sleep(0.1)
        return super().check_in_use(endpoint_id)


class TestFirstAnswer:
    """wait_first_answer: startup waits until StateManager can know the state.

    wh-kodi-playback-echo-stutter.1.1: the first answer is ready when the first
    poll finished AND every event it published has been delivered to the
    subscribers, or when the backend did not start.
    """

    @staticmethod
    def _real_monitor(fake, bus, **kwargs):
        return esm.ExclusiveSpeakerMonitor(
            asyncio.get_running_loop(),
            bus,
            backend_factory=lambda: fake,
            poll_interval=0.05,
            **kwargs,
        )

    async def test_the_answer_is_ready_only_after_a_subscriber_got_the_held_event(self):
        bus = EventBus()
        received = []

        async def slow_handler(event):
            await asyncio.sleep(0.3)  # delivery takes time; the answer must wait for it
            received.append(event)

        bus.subscribe(SpeakerExclusiveStateChangedEvent, slow_handler)
        fake = SlowCheckBackend()
        fake.a_result = IN_USE
        monitor = self._real_monitor(fake, bus)
        try:
            monitor.start()
            assert await monitor.wait_first_answer(timeout=5.0) is True
            assert [e.held for e in received] == [True]
        finally:
            monitor.stop()

    async def test_the_answer_is_ready_after_the_first_poll_when_the_speaker_is_free(self):
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(SpeakerExclusiveStateChangedEvent, handler)
        fake = FakeBackend()
        monitor = self._real_monitor(fake, bus)
        try:
            monitor.start()
            assert await monitor.wait_first_answer(timeout=5.0) is True
            assert fake.a_checks >= 1
            assert received == []
        finally:
            monitor.stop()

    async def test_the_answer_is_ready_after_a_backend_start_failure(self, caplog):
        fake = FakeBackend()
        fake.init_error = RuntimeError("no COM")
        monitor = self._real_monitor(fake, EventBus())
        t0 = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            monitor.start()
            assert await monitor.wait_first_answer(timeout=5.0) is True
        assert time.monotonic() - t0 < 2.0
        monitor.stop()
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(messages) == 1  # the start failure; no timeout WARNING
        assert "did not start" in messages[0]

    async def test_a_backend_that_never_answers_times_out_with_one_warning(
        self, caplog, running
    ):
        fake = FakeBackend()
        fake.block = threading.Event()  # check_in_use never returns
        monitor = self._real_monitor(fake, EventBus())
        running.append((monitor, fake))
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticking = asyncio.create_task(ticker())
        t0 = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            monitor.start()
            assert await monitor.wait_first_answer(timeout=0.2) is False
        elapsed = time.monotonic() - t0
        ticking.cancel()
        assert elapsed < 1.0  # the timeout argument bounds the wait
        assert ticks >= 5  # the loop kept running during the wait
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "exclusive-mode detector" in warnings[0].getMessage()
        assert "no answer" in warnings[0].getMessage()

    def test_the_wait_is_bounded_by_two_seconds(self):
        assert esm.FIRST_ANSWER_TIMEOUT_SECONDS == 2.0


# --------------------------------------------------------------- source rules


class TestSourceRules:
    @pytest.fixture(scope="class")
    def source(self):
        return Path(esm.__file__).read_text(encoding="utf-8")

    def test_no_exclusive_share_mode_anywhere(self, source):
        assert "SHAREMODE_EXCLUSIVE" not in source

    def test_audio_client_is_never_started(self, source):
        assert re.search(r"\.Start\s*\(", source) is None
        # The vtable slots carry other names, so check those names too.
        assert re.search(r"\.(RunSlot|HaltSlot|ResetSlot)\s*\(", source) is None

    def test_nothing_is_ever_written_to_the_speaker(self, source):
        assert "IAudioRenderClient" not in source
        assert re.search(r"\bGetBuffer\b", source) is None  # GetBufferSize is read-only

    def test_every_initialize_is_shared_mode(self, source):
        modes = re.findall(r"\.Initialize\(\s*([A-Za-z_]+)", source)
        assert modes, "expected an Initialize call"
        assert set(modes) == {"AUDCLNT_SHAREMODE_SHARED"}
        # The held stream (method B) and the check (method A) both go through it.
        assert len(re.findall(r"\b_initialize_shared\(", source)) >= 3  # def + 2 calls

    def test_com_work_is_not_given_to_the_loop_executor(self, source):
        assert "to_thread" not in source
        assert "run_in_executor" not in source

    def test_thread_is_a_daemon_thread(self, source):
        assert "daemon=True" in source


# ------------------------------------------------------------ service manager


class TestServiceManagerWiring:
    @pytest.fixture
    def manager(self):
        from service_manager import ServiceManager

        config_service = MagicMock()
        config_service.get.return_value = None
        config_service.get_config.return_value = {}
        app = MagicMock()
        app.get_screen_dimensions.return_value = (1920, 1080)
        sm = ServiceManager(
            config_service=config_service,
            event_bus=MagicMock(),
            loop=MagicMock(),
            app=app,
            state_manager=MagicMock(),
        )
        sm.software_dimmer = None
        sm.brightness_coordinator = None
        sm.mouse_handler = None
        sm.audio_monitor = None
        sm.plugin_registry = None
        return sm

    def test_exclusive_speaker_monitor_starts_as_none(self, manager):
        assert manager.exclusive_speaker_monitor is None

    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    @patch("service_manager.ExclusiveSpeakerMonitor")
    def test_initialize_services_builds_the_monitor(
        self, mock_monitor_cls, mock_launcher_cls, mock_plugin_cls,
        mock_speech_cls, mock_mouse_cls, mock_audio_cls, mock_bravia_cls,
        manager
    ):
        mock_launcher_cls.return_value.discover_providers.return_value = []
        manager.initialize_services()
        mock_monitor_cls.assert_called_once_with(manager.loop, manager.event_bus)
        assert manager.exclusive_speaker_monitor is mock_monitor_cls.return_value

    def test_start_services_starts_it_without_a_task_to_wait_on(self, manager):
        monitor = MagicMock()
        manager.exclusive_speaker_monitor = monitor
        tasks = manager.start_services()
        monitor.start.assert_called_once_with()
        assert tasks == []

    def test_start_services_starts_it_whatever_the_suppression_setting_says(self, manager):
        manager.config_service.get.side_effect = lambda key, default=None: (
            False if key == "ENABLE_AUDIO_SUPPRESSION" else default
        )
        monitor = MagicMock()
        manager.exclusive_speaker_monitor = monitor
        manager.start_services()
        monitor.start.assert_called_once_with()

    def test_start_services_works_without_the_monitor(self, manager):
        manager.exclusive_speaker_monitor = None
        assert manager.start_services() == []

    async def test_shutdown_services_stops_it(self, manager):
        monitor = MagicMock()
        manager.exclusive_speaker_monitor = monitor
        manager.remote_stt_launcher = None
        await manager.shutdown_services()
        monitor.stop.assert_called_once_with()

    async def test_shutdown_continues_when_the_monitor_stop_raises(self, manager):
        monitor = MagicMock()
        monitor.stop.side_effect = RuntimeError("stop failed")
        manager.exclusive_speaker_monitor = monitor
        manager.remote_stt_launcher = None
        mouse = MagicMock()
        manager.mouse_handler = mouse
        await manager.shutdown_services()
        mouse.stop_listeners.assert_called_once()
