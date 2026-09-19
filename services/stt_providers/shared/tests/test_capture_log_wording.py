"""The capture log lines state what was measured and nothing else.

wh-stt-load-metrics.3, acceptance criteria 4 and 5.

Two lines named a cause the code never measures, and both were read as fact
during today's investigation before the numbers contradicted them:

  - overflow_monitor.py said "(audio consumer behind real time)". The
    consumer being behind is a different measurement entirely: it shows up as
    `drops`, the frames the callback threw away because the queue was full.
    On 2026-09-03 every one of 122 utterances had drops=0 while 13 carried
    overflows, so the consumer was not behind and the line said it was.
  - diagnostics.py said "(likely whole-machine CPU starvation)". The tracker
    measures the gap between two loop iterations. Whole-machine CPU was
    20-37% during the 09:13 stalls, and an independent capture from the same
    microphone lost nothing over the same minutes, so the machine was not
    starved and the line said it likely was.

The third case is the restart cap. Reaching it stops further restart
requests and leaves the stream open; the log said nothing at all, so the
operator could not tell "recovered" from "gave up and kept going"
(verified in the 09:17-09:20 log: utterances kept finalizing with 3-22
overflows per 30 s after attempt 3/3).
"""
import logging

from shared_audio.diagnostics import LoopStallTracker
from shared_audio.overflow_monitor import OverflowConfig, OverflowMonitor

# numpy, unittest.mock.patch and a faked MMCSS registration were imported
# here for the two MicrophoneStream callback tests. Those went with the
# PortAudio capture path (wh-portaudio-capture-removal); nothing left in
# this file drives a callback.


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _monitor(**overrides):
    config = OverflowConfig(**{
        "overflow_threshold": 5,
        "window_seconds": 30.0,
        "restart_cooldown_seconds": 60.0,
        "max_restart_attempts": 3,
        "stable_reset_seconds": 300.0,
        **overrides,
    })
    return OverflowMonitor(config)


class TestOverflowSummaryStatesOnlyTheMeasurement:
    def test_summary_names_the_count_the_window_and_the_source(self, caplog):
        monitor = _monitor()
        with caplog.at_level(logging.INFO,
                             logger="shared_audio.overflow_monitor"):
            monitor.report_overflow()
        lines = [r.message for r in caplog.records
                 if r.message.startswith("[overflow] 1")]
        assert lines == [
            "[overflow] 1 audio input overflows in the last 30s, "
            "measured at the capture path"
        ]

    def test_summary_does_not_blame_the_consumer(self, caplog):
        """`drops` is the consumer's number; overflow is not."""
        monitor = _monitor()
        with caplog.at_level(logging.INFO,
                             logger="shared_audio.overflow_monitor"):
            monitor.report_overflow()
        text = " ".join(r.message for r in caplog.records)
        assert "consumer" not in text
        assert "behind real time" not in text


class TestStallMessageStatesOnlyTheMeasurement:
    def test_message_names_the_label_and_the_gap(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(2.0)
        assert tracker.record() == (
            "[stall] consumer loop made no progress for 2.0s")

    def test_message_keeps_the_queue_depth_suffix(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(2.0)
        assert tracker.record(queue_depth=7) == (
            "[stall] consumer loop made no progress for 2.0s"
            "; capture queue depth now 7")

    def test_message_does_not_guess_at_cpu_starvation(self):
        clock = FakeClock()
        tracker = LoopStallTracker(
            stall_threshold_s=1.0, clock=clock, label="capture callback")
        tracker.record()
        clock.advance(2.0)
        msg = tracker.record()
        assert msg is not None
        assert "starvation" not in msg
        assert "likely" not in msg


class TestRestartCapWarning:
    """Exactly one WARNING when the attempt cap stops further requests.

    Requests, not restarts: the cap stops the monitor asking and
    leaves the capture stream open, which is what this module's own
    opening text says (wh-audio-callback-log.2.9).
    """

    def _exhaust(self, monitor):
        """Drive the monitor to its restart cap and return it."""
        for _ in range(monitor.config.max_restart_attempts):
            monitor.last_restart_time = 0.0  # clear the cooldown gate
            for _ in range(monitor.config.overflow_threshold):
                monitor.report_overflow()
            monitor.overflow_times.clear()
        return monitor

    def test_cap_logs_one_warning(self, caplog):
        monitor = self._exhaust(_monitor())
        monitor.last_restart_time = 0.0
        with caplog.at_level(logging.WARNING,
                             logger="shared_audio.overflow_monitor"):
            for _ in range(monitor.config.overflow_threshold):
                monitor.report_overflow()
        warnings = [r.message for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert warnings == [
            "[overflow] Restart attempt limit reached (3/3); no further "
            "restarts will be requested and the capture stream is left "
            "open, so capture continues degraded"
        ]

    def test_warning_is_not_repeated_on_later_overflows(self, caplog):
        monitor = self._exhaust(_monitor())
        monitor.last_restart_time = 0.0
        with caplog.at_level(logging.WARNING,
                             logger="shared_audio.overflow_monitor"):
            for _ in range(40):
                monitor.report_overflow()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_no_warning_before_the_cap_is_reached(self, caplog):
        monitor = _monitor()
        with caplog.at_level(logging.WARNING,
                             logger="shared_audio.overflow_monitor"):
            monitor.last_restart_time = 0.0
            for _ in range(monitor.config.overflow_threshold):
                monitor.report_overflow()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []
        assert monitor.restart_attempts == 1

    def test_cap_still_refuses_the_restart(self):
        monitor = self._exhaust(_monitor())
        monitor.last_restart_time = 0.0
        for _ in range(monitor.config.overflow_threshold - 1):
            monitor.report_overflow()
        assert monitor.report_overflow() is False
        assert monitor.restart_attempts == 3

    def test_warning_does_not_claim_a_restart_happened(self, caplog):
        """The Parakeet provider connects no restart callback and never
        reopens its stream, so the line must not describe a restart as
        something that took place (wh-stt-load-metrics.3 criterion 3)."""
        monitor = self._exhaust(_monitor())
        monitor.last_restart_time = 0.0
        with caplog.at_level(logging.WARNING,
                             logger="shared_audio.overflow_monitor"):
            for _ in range(monitor.config.overflow_threshold):
                monitor.report_overflow()
        text = " ".join(r.message for r in caplog.records
                        if r.levelno == logging.WARNING)
        assert "restarted" not in text
        assert "Restarting" not in text


class TestRestartRequestLinesStateTheRequestNotTheAct:
    """Boss ruling on wh-stt-load-metrics.3, 2026-09-03: these two lines
    describe the request, never a restart the provider does not perform.

    The Parakeet provider passes no overflow_callback (main.py:178), so
    OverflowMonitor.restart_callback is None there and nothing reads
    restart_requested. google_stt_server does pass one --
    on_overflow_detected -- but it writes a line and returns
    (main.py:1086-1099), under the banner "auto-restart disabled" at
    :1276. Neither shipped provider reopens on an overflow; the
    restarts that do happen are asked for by a user through the
    add-hint and restart-service WebSocket handlers.
    """

    def _trip_threshold(self, monitor):
        monitor.last_restart_time = 0.0
        for _ in range(monitor.config.overflow_threshold):
            monitor.report_overflow()

    def test_monitor_line_names_the_request_and_the_attempt(self, caplog):
        monitor = _monitor()
        with caplog.at_level(logging.INFO,
                             logger="shared_audio.overflow_monitor"):
            self._trip_threshold(monitor)
        lines = [r.message for r in caplog.records
                 if "threshold crossed" in r.message]
        assert lines == [
            "[overflow] Overflow threshold crossed; restart requested "
            "(attempt 1/3)"
        ]

    def test_monitor_line_does_not_announce_a_restart(self, caplog):
        monitor = _monitor()
        with caplog.at_level(logging.INFO,
                             logger="shared_audio.overflow_monitor"):
            self._trip_threshold(monitor)
        text = " ".join(r.message for r in caplog.records)
        assert "TRIGGERING RESTART" not in text
        assert "Restarting" not in text


class TestTheSummaryNamesWhatActuallyOverflowed:
    """The summary line says where the frames were lost.

    It said PortAudio whichever backend was running. The WinRT backend
    has no PortAudio in its capture path: its capture thread drops a
    chunk when the queue it hands to the forwarder is full. So on the
    shipped default the line named a component that was not in the
    process (David, QUESTIONS-2026-09-02 item 55; boss ruling 2026-09-04).
    The other backend has since been deleted
    (wh-portaudio-capture-removal), which leaves the source a parameter
    with one caller and the wording still worth pinning.

    The phrases are written out here rather than imported. A test of
    wording that imports the wording proves only that two names agree.
    """

    QUEUE = "the queue between capture and the forwarder"
    PORTAUDIO = "PortAudio"

    def _summary(self, caplog, **overrides):
        monitor = _monitor(**overrides)
        with caplog.at_level(logging.INFO,
                             logger="shared_audio.overflow_monitor"):
            monitor.report_overflow()
        return [r.message for r in caplog.records
                if r.message.startswith("[overflow] 1")]

    def test_the_summary_names_whatever_source_it_is_given(
            self, caplog):
        assert self._summary(caplog, overflow_source=self.PORTAUDIO) == [
            "[overflow] 1 audio input overflows in the last 30s, "
            "measured at PortAudio"
        ]

    def test_the_winrt_backend_names_the_queue(self, caplog):
        assert self._summary(caplog, overflow_source=self.QUEUE) == [
            "[overflow] 1 audio input overflows in the last 30s, "
            "measured at the queue between capture and the forwarder"
        ]

    def test_the_winrt_backend_hands_the_monitor_the_queue(self):
        """The backend that loses the frames is the one that names it."""
        from shared_audio.capture.winrt_capture import WinRTAudioCapture

        capture = WinRTAudioCapture()
        source = getattr(
            capture.overflow_monitor.config, "overflow_source", None)
        assert source == self.QUEUE

    def test_the_backend_publishes_the_phrase_a_provider_can_read(self):
        """A provider logs its own overflow line and needs the same words.

        A provider cannot reach the monitor before capture begins, so the
        class attribute is what it reads instead. Two backends published
        one each until the PortAudio path was deleted
        (wh-portaudio-capture-removal); WinRT is the only publisher left.
        """
        from shared_audio.capture.winrt_capture import WinRTAudioCapture

        assert getattr(WinRTAudioCapture, "OVERFLOW_SOURCE", None) == self.QUEUE

    def test_the_published_phrase_is_not_the_portaudio_one(self):
        """The two phrases stayed distinct because the places differ.

        PORTAUDIO_SOURCE outlives its backend in overflow_monitor.py under
        a crewcut comment, so the confusion this guards against is still
        reachable by a future edit.
        """
        assert self.QUEUE != self.PORTAUDIO


class TestTheRefusalLinesDoNotAskForARestart:
    """"Restart needed but ..." states a need the monitor never establishes.

    Both lines are written from _should_restart, which is deciding whether
    to ASK for one. What follows an ask is the provider's business: the
    Parakeet provider passes no overflow_callback at all (main.py:178), so
    nothing there reads the answer, and the google provider's callback only
    writes a log line. A reader of either line was told a restart was
    needed and that something was in the way of it; what actually happened
    is that no restart was requested (bead
    wh-stt-overflow-config-and-wording, criterion 4).
    """

    def _trip_threshold(self, monitor):
        for _ in range(monitor.config.overflow_threshold):
            monitor.report_overflow()

    def test_the_cooldown_line_says_no_request_was_made(self, caplog):
        import time as _time

        monitor = _monitor()
        monitor.last_restart_time = _time.time()
        with caplog.at_level(logging.DEBUG,
                             logger="shared_audio.overflow_monitor"):
            self._trip_threshold(monitor)
        lines = [r.message for r in caplog.records
                 if "cooldown" in r.message]
        assert len(lines) == 1, lines
        assert lines[0].startswith(
            "[overflow] No restart requested: still inside the 60.0s "
            "cooldown ("
        )

    def test_the_attempt_limit_line_says_no_request_was_made(self, caplog):
        monitor = _monitor()
        monitor.last_restart_time = 0.0
        monitor.restart_attempts = monitor.config.max_restart_attempts
        with caplog.at_level(logging.DEBUG,
                             logger="shared_audio.overflow_monitor"):
            self._trip_threshold(monitor)
        lines = [r.message for r in caplog.records
                 if "attempt limit" in r.message
                 and r.levelno == logging.DEBUG]
        assert lines == [
            "[overflow] No restart requested: the attempt limit is "
            "reached (3/3)"
        ]

    def test_neither_refusal_claims_a_restart_is_needed(self, caplog):
        """The phrase itself, in both branches, at any level."""
        import time as _time

        cooled = _monitor()
        cooled.last_restart_time = _time.time()
        capped = _monitor()
        capped.last_restart_time = 0.0
        capped.restart_attempts = capped.config.max_restart_attempts
        with caplog.at_level(logging.DEBUG,
                             logger="shared_audio.overflow_monitor"):
            self._trip_threshold(cooled)
            self._trip_threshold(capped)
        text = " ".join(r.message for r in caplog.records)
        assert "Restart needed" not in text



class TestTheRestartWordingHoldsInTheSourceToo:
    """The log lines above are pinned by tests; the prose beside them was
    not, and it drifted (wh-audio-callback-log.2.6).

    Three places went on describing the boolean as a restart the monitor
    performs, after the lines themselves had been corrected. A
    documentation-only change cannot be caught by a log assertion, so
    these read the text itself -- the same approach
    tests/test_capture_load_metrics.py already uses for the
    _capture_unavailable docstring.
    """

    def test_report_overflow_does_not_promise_a_restart(self):
        collapsed = ' '.join(OverflowMonitor.report_overflow.__doc__.split())
        assert 'restart should be triggered' not in collapsed, collapsed
        assert 'restart was requested' in collapsed

    def test_should_restart_describes_a_decision_to_ask(self):
        collapsed = ' '.join(OverflowMonitor._should_restart.__doc__.split())
        assert 'should be triggered' not in collapsed, collapsed
        assert 'ask for a restart' in collapsed

    def test_the_success_path_comment_says_request(self):
        """Not a docstring, so __doc__ cannot see it; getsource can."""
        import inspect
        source = inspect.getsource(OverflowMonitor._should_restart)
        assert 'trigger restart' not in source, (
            "the success-path comment calls the request a trigger again")
        assert '# All checks passed - request a restart' in source

    def test_the_config_fields_are_described_as_request_limits(self):
        """The cooldown and the cap bound requests, not restarts.

        Both are field comments rather than docstrings, so this reads
        the class source. The field NAMES keep their spelling on
        purpose -- they are public and callers pass them by keyword.
        """
        import inspect
        source = inspect.getsource(OverflowConfig)
        assert 'restart attempts' not in source, source
        assert 'restart requests' in source
        assert 'restart-request behavior' in OverflowConfig.__doc__

    def test_this_module_does_not_call_the_cap_a_restart_stop(self):
        """The opening text of this file already says requests."""
        assert 'stops further restarts' not in (
            TestRestartCapWarning.__doc__)
