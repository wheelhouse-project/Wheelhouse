"""Load metrics that separate capture drops from inference lag.

wh-stt-load-metrics. Transcription degrades when the CPU is very busy, and two
different failures produce that same symptom:

  1. Capture drops -- the audio callback loses frames, so the recognizer is fed
     damaged audio and the words come out wrong or missing.
  2. Inference lag -- the audio arrives intact but the recognizer falls behind,
     so the words are late but correct.

They need different fixes, so the numbers here exist to tell them apart. Three
are collected, all reported once per utterance:

  - the count of PortAudio status flags, and of the input-overflow
    subset. Only the PortAudio capture path ever reported these two, and
    wh-portaudio-capture-removal deleted it, so every shipped provider
    now omits both keys and the lines print n/a for them,
  - the depth of the capture queue that feeds the recognizer,
  - the wall-clock time the engine spends on each chunk of audio.

Scope note (boss ruling 2026-08-29). The live capture path on this machine is
this provider-process path, not services/wheelhouse/stt/audio_capture.py; that
module runs only when stt.mode == "in_process", and the running provider is
Parakeet in the default "remote" mode. These tests therefore exercise the
provider-process capture path and AudioProcessor, and never that module.
The capture class they drove was MicrophoneStream until
wh-portaudio-capture-removal deleted it; WinRTAudioCapture is the class
that survived.
"""
import logging
import pathlib
import threading
import time

import pytest
from unittest.mock import Mock, patch


def _procedure_text():
    if not PROCEDURE_DOC.is_file():
        pytest.skip("development-only CPU-load procedure is absent from this checkout")
    return PROCEDURE_DOC.read_text(encoding='utf-8')


class _StubEngine:
    """A recognition engine that records how often it was fed audio."""

    def __init__(self):
        self.last_result = ""
        self.result = ""
        self.endpoint = False
        self.calls = 0
        self.reset_calls = 0

    def process_audio(self, audio_bytes):
        self.calls += 1

    def get_result(self):
        return self.result

    def is_endpoint(self):
        return self.endpoint

    def reset(self):
        self.reset_calls += 1


def _make_processor(capture_stats=None, engine=None,
                    force_endpoint_silence_ms=None, vad_lead_in_ms=300,
                    log_load_diagnostics=None):
    """An AudioProcessor with VAD and AGC replaced by pass-through stubs.

    log_load_diagnostics=None leaves the constructor's own default in
    force. The off-by-default tests rely on that: a factory that always
    passed False would hide a constructor default of True (mutation
    processor-setting-defaults-on, wh-audit13-preengine-load-review.1).
    """
    from shared_stt.audio_processor import AudioProcessor

    engine = engine if engine is not None else _StubEngine()
    forwarder = Mock()
    processor = AudioProcessor(
        engine=engine,
        forwarder=forwarder,
        sample_rate=16000,
        capture_stats=capture_stats,
        force_endpoint_silence_ms=force_endpoint_silence_ms,
        vad_lead_in_ms=vad_lead_in_ms,
        **({} if log_load_diagnostics is None
           else {'log_load_diagnostics': log_load_diagnostics}),
    )
    processor.vad = Mock()
    processor.vad.is_speech = Mock(return_value=True)
    processor.vad.reset = Mock()
    processor.vad._speech_count = 0
    processor.vad._inference_count = 0
    processor.agc = Mock()
    processor.agc.process = Mock(side_effect=lambda pcm, is_speech: pcm)
    processor.agc.on_stt_outcome = Mock()
    processor.lead_in_buffer = Mock()
    processor.lead_in_buffer.get_lead_in = Mock(return_value=b'')
    processor.lead_in_buffer.clear = Mock()
    processor.lead_in_buffer.add = Mock()
    return processor, engine


# One 30ms chunk of 16kHz mono int16 silence: 480 samples, 960 bytes.
CHUNK_30MS = b'\x00\x00' * 480

# One 10ms chunk of the same audio: 160 samples, 320 bytes. A 2000ms lead-in
# of these needs 200 snapshots, which is what the removed hard cap of 128
# could not hold (wh-stt-load-metrics.1.5).
CHUNK_10MS = b'\x00\x00' * 160


def _load_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith('[load-diag] utt=')]


def _load_line(caplog):
    """The single [load-diag] line from a captured run."""
    lines = _load_lines(caplog)
    assert len(lines) == 1, f"expected one [load-diag] line, got {lines}"
    return lines[0]


def _field(line, name):
    """Read one 'name=value' field out of a [load-diag] line."""
    for part in line.split():
        key, _, value = part.partition('=')
        if key == name:
            return value
    raise AssertionError(f"no field {name!r} in {line!r}")


def _speak_then_end(processor, engine, chunks_before_end=1, text="hello"):
    """Feed N speech chunks, then one chunk on which the engine endpoints."""
    for _ in range(chunks_before_end):
        processor.process_chunk(CHUNK_30MS)
    engine.endpoint = True
    engine.result = text
    processor.process_chunk(CHUNK_30MS)


class TestPerUtteranceLoadLine:
    """One [load-diag] line per finished utterance, beside [vad_utt_stats]."""

    def test_line_is_emitted_at_the_utterance_endpoint(self, caplog):
        processor, engine = _make_processor()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_end(processor, engine)
        assert _field(_load_line(caplog), 'kind') == 'endpoint'

    def test_engine_calls_are_counted(self, caplog):
        processor, engine = _make_processor()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_end(processor, engine, chunks_before_end=3)
        assert _field(_load_line(caplog), 'engine_calls') == '4'

    def test_audio_ms_follows_the_bytes_fed_to_the_engine(self, caplog):
        """audio_ms is the denominator of engine_ratio, so it has to come from
        the audio actually handed to the engine rather than a chunk count
        times a nominal duration.

        The chunks are deliberately different sizes. Engine chunks are not all
        30ms in the running system -- the lead-in buffer prepends audio at the
        start of an utterance -- and a same-size fixture cannot tell the two
        implementations apart: with only 30ms chunks, counting bytes and
        assuming 480 samples produce the same number. A mutation gate proved
        that, so this test fed uniform chunks and passed while claiming
        something it had not shown (wh-stt-load-metrics).

        One 30ms chunk plus one 60ms chunk is 90ms.
        """
        processor, engine = _make_processor()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            processor.process_chunk(CHUNK_30MS)
            engine.endpoint = True
            engine.result = "hello"
            processor.process_chunk(CHUNK_30MS * 2)
        assert _field(_load_line(caplog), 'audio_ms') == '90'

    def test_engine_time_is_measured_around_each_engine_call(self, caplog):
        """Two calls at 10ms and 40ms: total 50ms, max 40ms."""
        processor, engine = _make_processor()
        ticks = [0.0, 0.010, 1.0, 1.040]
        with patch('shared_stt.audio_processor.time.perf_counter',
                   side_effect=ticks):
            with caplog.at_level(logging.INFO,
                                 logger="shared_stt.audio_processor"):
                _speak_then_end(processor, engine)
        line = _load_line(caplog)
        assert _field(line, 'engine_ms_total') == '50.0'
        assert _field(line, 'engine_ms_max') == '40.0'

    def test_engine_ratio_is_engine_time_over_audio_duration(self, caplog):
        """The decisive number: above 1.0 the recognizer cannot keep up with
        real time, which is inference lag rather than lost audio."""
        processor, engine = _make_processor()
        # Two chunks, 60ms of audio; the engine spends 30ms then 60ms = 90ms.
        ticks = [0.0, 0.030, 1.0, 1.060]
        with patch('shared_stt.audio_processor.time.perf_counter',
                   side_effect=ticks):
            with caplog.at_level(logging.INFO,
                                 logger="shared_stt.audio_processor"):
                _speak_then_end(processor, engine)
        assert _field(_load_line(caplog), 'engine_ratio') == '1.50'


class TestCaptureCountersAreReportedPerUtterance:
    """The capture numbers are differences across the utterance, not totals.

    A capture provider's counters run for the life of the process, so
    reporting them raw would make every utterance after the first look worse
    than it was.
    """

    def test_overflow_is_the_delta_across_the_utterance(self, caplog):
        stats = {'captured': 100, 'drops': 5, 'qsize': 2, 'max_q': 9,
                 'status_flags': 7, 'overflow_count': 4}
        processor, engine = _make_processor(capture_stats=lambda: dict(stats))
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            processor.process_chunk(CHUNK_30MS)
            stats.update(status_flags=10, overflow_count=6, drops=8)
            _speak_then_end(processor, engine, chunks_before_end=0)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == '2'
        assert _field(line, 'status_flags') == '3'
        assert _field(line, 'drops') == '3'

    def test_a_second_utterance_starts_its_counters_again(self, caplog):
        stats = {'captured': 0, 'drops': 0, 'qsize': 0, 'max_q': 0,
                 'status_flags': 0, 'overflow_count': 0}
        processor, engine = _make_processor(capture_stats=lambda: dict(stats))
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            processor.process_chunk(CHUNK_30MS)
            stats.update(overflow_count=5)
            _speak_then_end(processor, engine, chunks_before_end=0, text="one")
            engine.endpoint = False
            _speak_then_end(processor, engine, chunks_before_end=1, text="two")
        lines = _load_lines(caplog)
        assert len(lines) == 2, lines
        assert _field(lines[0], 'overflow') == '5'
        assert _field(lines[1], 'overflow') == '0'

    def test_queue_depth_reports_the_high_water_mark_of_the_utterance(self, caplog):
        depths = iter([1, 7, 3, 3, 3])
        stats = {'captured': 0, 'drops': 0, 'max_q': 0,
                 'status_flags': 0, 'overflow_count': 0}

        def capture_stats():
            return dict(stats, qsize=next(depths))

        processor, engine = _make_processor(capture_stats=capture_stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_end(processor, engine, chunks_before_end=2)
        assert _field(_load_line(caplog), 'q_max') == '7'


class TestLoadMetricsNeverBreakTranscription:
    """Measurement must not be able to take the provider down."""

    def test_no_capture_stats_callable_still_reports_engine_timing(self, caplog):
        """A provider that does not pass capture_stats still gets the half of
        the answer that comes from the engine, and says so for the other half
        rather than printing a zero that reads as 'no drops'."""
        processor, engine = _make_processor(capture_stats=None)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_end(processor, engine)
        line = _load_line(caplog)
        assert _field(line, 'engine_calls') == '2'
        assert _field(line, 'overflow') == 'n/a'

    def test_a_raising_capture_stats_callable_does_not_lose_the_final(self, caplog):
        def boom():
            raise RuntimeError("capture provider went away")

        processor, engine = _make_processor(capture_stats=boom)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_end(processor, engine)
        processor.forwarder.send_final.assert_called_once()

    def test_a_raising_capture_stats_callable_still_reports_the_engine_half(self, caplog):
        def boom():
            raise RuntimeError("capture provider went away")

        processor, engine = _make_processor(capture_stats=boom)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_end(processor, engine)
        assert _field(_load_line(caplog), 'engine_calls') == '2'

    def test_the_existing_vad_utt_stats_line_still_appears(self, caplog):
        """The load line is added beside [vad_utt_stats], not in place of it."""
        processor, engine = _make_processor()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_end(processor, engine)
        assert any(r.getMessage().startswith('[vad_utt_stats]')
                   for r in caplog.records)


class _Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _reporter(stats_source, clock, interval_s=10.0):
    from shared_audio.diagnostics import CaptureLoadReporter

    return CaptureLoadReporter(
        capture_stats=stats_source,
        interval_s=interval_s,
        clock=clock,
    )


def _summary(lines):
    matches = [ln for ln in lines if ln.startswith('[load-diag] window=')]
    assert len(matches) == 1, f"expected one summary line, got {lines}"
    return matches[0]


def _advance(reporter, clock, seconds, step=0.5):
    """Run the reporter forward the way the real consumer loop runs.

    A test that jumps the clock ten seconds in one iteration has, by the
    tracker's own definition, simulated a ten-second stall -- so the reporter
    correctly says so, and a test meaning "ten quiet seconds" gets a [stall]
    line it did not expect. The loop really iterates every ~30ms; stepping in
    sub-threshold increments is what "quiet" looks like. Returns every line the
    iterations produced.
    """
    lines = []
    remaining = seconds
    while remaining > 1e-9:
        clock.advance(min(step, remaining))
        lines.extend(reporter.record_iteration())
        remaining -= step
    return lines


class TestCaptureLoadReporterInterval:
    """The consumer loop calls the reporter every iteration; it speaks rarely.

    A line per iteration would be one line per 30ms of audio. The interval
    matches OverflowMonitor's own 10-second summary so the two read together.
    """

    def test_the_first_iteration_says_nothing(self):
        clock = _Clock()
        stats = {'qsize': 0, 'drops': 0, 'overflow_count': 0, 'status_flags': 0}
        reporter = _reporter(lambda: dict(stats), clock)
        assert reporter.record_iteration() == []

    def test_nothing_is_said_before_the_interval_elapses(self):
        clock = _Clock()
        stats = {'qsize': 0, 'drops': 0, 'overflow_count': 0, 'status_flags': 0}
        reporter = _reporter(lambda: dict(stats), clock)
        reporter.record_iteration()
        assert _advance(reporter, clock, 9.0) == []

    def test_a_summary_appears_once_the_interval_elapses(self):
        clock = _Clock()
        stats = {'qsize': 3, 'drops': 0, 'overflow_count': 0, 'status_flags': 0}
        reporter = _reporter(lambda: dict(stats), clock)
        reporter.record_iteration()
        clock.advance(10.0)
        assert _field(_summary(reporter.record_iteration()), 'q_now') == '3'


class TestCaptureLoadReporterWindow:
    """Every number in the summary belongs to the window it reports."""

    def test_queue_depth_is_the_high_water_mark_of_the_window(self):
        clock = _Clock()
        depths = iter([1, 6, 2, 2])
        stats = {'drops': 0, 'overflow_count': 0, 'status_flags': 0}
        reporter = _reporter(lambda: dict(stats, qsize=next(depths)), clock)
        reporter.record_iteration()
        reporter.record_iteration()
        clock.advance(10.0)
        assert _field(_summary(reporter.record_iteration()), 'q_max') == '6'

    def test_the_high_water_mark_starts_again_each_window(self):
        """A spike in one window must not be reprinted in the next, or a
        transient stall would look permanent."""
        clock = _Clock()
        depths = iter([9, 9, 1, 1, 1])
        stats = {'drops': 0, 'overflow_count': 0, 'status_flags': 0}
        reporter = _reporter(lambda: dict(stats, qsize=next(depths)), clock)
        reporter.record_iteration()
        clock.advance(10.0)
        first = _summary(reporter.record_iteration())
        clock.advance(10.0)
        second = _summary(reporter.record_iteration())
        assert _field(first, 'q_max') == '9'
        assert _field(second, 'q_max') == '1'

    def test_capture_counters_are_window_deltas_not_lifetime_totals(self):
        clock = _Clock()
        stats = {'qsize': 0, 'drops': 100, 'overflow_count': 20,
                 'status_flags': 25}
        reporter = _reporter(lambda: dict(stats), clock)
        reporter.record_iteration()
        stats.update(drops=104, overflow_count=23, status_flags=29)
        clock.advance(10.0)
        line = _summary(reporter.record_iteration())
        assert _field(line, 'drops') == '4'
        assert _field(line, 'overflow') == '3'
        assert _field(line, 'status_flags') == '4'

    def test_loop_stalls_are_reported_in_the_summary(self):
        clock = _Clock()
        stats = {'qsize': 0, 'drops': 0, 'overflow_count': 0, 'status_flags': 0}
        reporter = _reporter(lambda: dict(stats), clock)
        reporter.record_iteration()
        clock.advance(3.0)          # a 3-second gap: the loop was not scheduled
        reporter.record_iteration()
        line = _summary(_advance(reporter, clock, 7.0))
        assert _field(line, 'stalls') == '1'
        assert float(_field(line, 'max_gap_ms')) >= 3000.0


class TestCaptureLoadReporterStallLine:
    """A stall is worth saying immediately, not only at the next summary."""

    def test_a_stall_returns_the_tracker_message(self):
        clock = _Clock()
        stats = {'qsize': 4, 'drops': 0, 'overflow_count': 0, 'status_flags': 0}
        reporter = _reporter(lambda: dict(stats), clock)
        reporter.record_iteration()
        clock.advance(3.0)
        lines = reporter.record_iteration()
        assert any(ln.startswith('[stall]') for ln in lines), lines

    def test_a_normal_iteration_returns_no_stall_message(self):
        clock = _Clock()
        stats = {'qsize': 0, 'drops': 0, 'overflow_count': 0, 'status_flags': 0}
        reporter = _reporter(lambda: dict(stats), clock)
        reporter.record_iteration()
        clock.advance(0.03)
        assert reporter.record_iteration() == []


class TestCaptureLoadReporterSurvivesABadStatsReader:
    """The reporter observes the loop; it must never be able to stop it."""

    def test_a_raising_reader_produces_no_summary_and_no_exception(self):
        clock = _Clock()

        def boom():
            raise RuntimeError("capture provider went away")

        reporter = _reporter(boom, clock)
        reporter.record_iteration()
        lines = _advance(reporter, clock, 10.0)
        # Still no summary: a window with no reading has no numbers to
        # report, and that has not changed. What has changed is that the
        # window is no longer SILENT -- it now carries the reason its
        # readings are missing (wh-capture-load-gaps criterion 1b). This
        # test asserted complete silence, which was the defect.
        assert [ln for ln in lines
                if ln.startswith('[load-diag] window=')] == []
        assert [ln for ln in lines
                if ln.startswith('[load-diag-capture]')] != []

    def test_a_reader_that_recovers_reports_again(self):
        clock = _Clock()
        broken = [True]

        def flaky():
            if broken[0]:
                raise RuntimeError("not yet")
            return {'qsize': 2, 'drops': 0, 'overflow_count': 0,
                    'status_flags': 0}

        reporter = _reporter(flaky, clock)
        reporter.record_iteration()
        clock.advance(10.0)
        reporter.record_iteration()
        broken[0] = False
        clock.advance(10.0)
        reporter.record_iteration()      # first good read sets the baseline
        clock.advance(10.0)
        assert _field(_summary(reporter.record_iteration()), 'q_now') == '2'


# A provider that does not measure a counter at all. The WinRT capture
# provider's get_stats() returns exactly these four keys
# (shared_audio/capture/winrt_capture.py). It carries no status_flags and no
# overflow_count, because PortAudio status flags do not exist on the WinRT
# path. The deleted sounddevice provider answered the same four keys while
# no stream was open (wh-portaudio-capture-removal).
WINRT_SHAPED_STATS = {'captured': 100, 'drops': 2, 'qsize': 1, 'max_q': 3}


class TestAnUnmeasuredCounterReadsNotApplicable:
    """A counter the provider does not report must not print as zero.

    This whole measurement exists to decide between two failures, and
    "overflow=0" is the reading that rules capture loss out. A provider that
    never measured overflows must not be able to produce that reading -- the
    difference between "measured, none happened" and "not measured" is the
    difference between a conclusion and a guess.
    """

    def test_the_utterance_line_says_n_a_for_a_key_the_provider_omits(
            self, caplog):
        processor, engine = _make_processor(
            capture_stats=lambda: dict(WINRT_SHAPED_STATS))
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_end(processor, engine)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == 'n/a'
        assert _field(line, 'status_flags') == 'n/a'

    def test_a_key_the_provider_does_report_stays_a_number(self, caplog):
        """drops is present on every provider, so it must not be swept into
        n/a along with the two that are absent."""
        stats = dict(WINRT_SHAPED_STATS)
        processor, engine = _make_processor(capture_stats=lambda: dict(stats))
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            processor.process_chunk(CHUNK_30MS)
            stats['drops'] = 5           # three more lost during the utterance
            engine.endpoint = True
            engine.result = "hello"
            processor.process_chunk(CHUNK_30MS)
        assert _field(_load_line(caplog), 'drops') == '3'

    def test_the_window_summary_says_n_a_for_a_key_the_provider_omits(self):
        clock = _Clock()
        # The frame counter has to move across the window, and the loop has to
        # keep reading it. A window whose counter never moved is one where no
        # microphone was captured at all (wh-stt-load-metrics.1.15), and a
        # window read only at its two ends is one the loop never watched
        # (wh-stt-load-metrics.1.20); either is marked for that reason rather
        # than this one, which would hide what this test is about.
        stats = dict(WINRT_SHAPED_STATS)

        def read():
            stats['captured'] += 25
            return dict(stats)

        reporter = _reporter(read, clock)
        line = _summary(_window(clock, reporter))
        assert _field(line, 'overflow') == 'n/a'
        assert _field(line, 'status_flags') == 'n/a'
        assert _field(line, 'drops') == '0'


class TestTheLoadLineHasItsOwnLoggerName:
    """The [load-diag] line is logged on a child logger, not the module one.

    Providers silence shared_stt wholesale: the Parakeet server sets
    logging.getLogger("shared_stt").propagate = False with no handler on it
    (main.py), which discards every record from this module. A named child
    lets a provider forward this one line by attaching a handler to that name,
    without changing the visibility of [vad_utt_stats] or anything else under
    shared_stt. The name is exported so the provider and its tests bind to the
    same string.
    """

    def test_the_exported_name_is_a_child_of_this_module(self):
        from shared_stt.audio_processor import LOAD_METRICS_LOGGER_NAME
        assert LOAD_METRICS_LOGGER_NAME.startswith('shared_stt.audio_processor.')

    def test_the_line_is_logged_on_that_logger(self, caplog):
        from shared_stt.audio_processor import LOAD_METRICS_LOGGER_NAME

        processor, engine = _make_processor()
        with caplog.at_level(logging.INFO, logger=LOAD_METRICS_LOGGER_NAME):
            _speak_then_end(processor, engine)
        names = [r.name for r in caplog.records
                 if r.getMessage().startswith('[load-diag]')]
        assert names == [LOAD_METRICS_LOGGER_NAME]

    def test_the_existing_utterance_stats_line_is_not_moved(self, caplog):
        """Only the new line moves. [vad_utt_stats] keeps the module logger,
        so this change cannot alter what any provider already sees."""
        processor, engine = _make_processor()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_end(processor, engine)
        names = [r.name for r in caplog.records
                 if r.getMessage().startswith('[vad_utt_stats]')]
        assert names == ['shared_stt.audio_processor']


class _FinalizingEngine(_StubEngine):
    """An engine whose finalize() runs a measurable final inference.

    sherpa_engine.finalize() calls _run_final_inference(), which concatenates
    the whole utterance buffer and recognizes it in one pass. That is the
    single most expensive engine call of an utterance, and on the forced
    endpoint path it is the last thing that runs before the load line prints.
    """

    FINALIZE_SECONDS = 0.05

    def __init__(self):
        super().__init__()
        self.finalize_calls = 0

    def finalize(self):
        self.finalize_calls += 1
        time.sleep(self.FINALIZE_SECONDS)
        self.result = "forced"


class _RaisingFinalizeEngine(_StubEngine):
    """An engine whose finalize() fails."""

    def finalize(self):
        raise RuntimeError("final inference failed")


def _speak_then_fall_silent(processor, silent_chunks=3):
    """Open the VAD gate with one speech chunk, then feed silence.

    The caller builds the processor with a 30ms force threshold, which is one
    chunk of trailing silence. process_chunk adds the chunk to the trailing
    total and checks the threshold BEFORE feeding the engine, so the first
    silent chunk forces the endpoint without ever being recognized: the
    utterance is exactly the 30ms gate-opening chunk, one engine call, plus
    whatever the final inference costs. After the force endpoint the gate is
    closed, so the leftover silent chunks only refill the lead-in buffer and
    exactly one load line is written.
    """
    processor.process_chunk(CHUNK_30MS)
    processor.vad.is_speech = Mock(return_value=False)
    for _ in range(silent_chunks):
        processor.process_chunk(CHUNK_30MS)


class TestTheForcedEndpointCountsItsFinalInference:
    """engine_ms on a kind=force line must include the finalize() inference.

    Found by deepseek as wh-stt-load-metrics.1.1. _process_speech_audio timed
    every per-chunk process_audio call, but _force_finalize_and_reset called
    engine.finalize() outside any timing. On the parakeet provider that call
    recognizes the whole utterance in one pass, so it is the largest engine
    cost of the utterance and it was missing from the numbers entirely.

    The consequence is a wrong-direction reading, which is the one failure
    this whole measurement exists to prevent: the procedure document maps
    engine_ratio above 1.0 to inference lag, and an understated ratio rules
    inference lag OUT at exactly the moment it is happening.
    """

    def test_the_line_reports_a_forced_endpoint(self, caplog):
        """The fixture really does take the force path, not the engine
        endpoint path, so every other test here measures what it claims."""
        processor, engine = _make_processor(
            engine=_FinalizingEngine(), force_endpoint_silence_ms=30.0)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_fall_silent(processor)
        assert _field(_load_line(caplog), 'kind') == 'force'
        assert engine.finalize_calls == 1

    def test_engine_time_includes_the_final_inference(self, caplog):
        processor, engine = _make_processor(
            engine=_FinalizingEngine(), force_endpoint_silence_ms=30.0)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_fall_silent(processor)
        total_ms = float(_field(_load_line(caplog), 'engine_ms_total'))
        assert total_ms >= 45.0, (
            "the 50ms finalize() inference is missing from engine_ms_total")

    def test_the_final_inference_can_be_the_longest_one(self, caplog):
        """engine_ms_max answers "was any single call slow", so the most
        expensive call of the utterance has to be a candidate for it."""
        processor, engine = _make_processor(
            engine=_FinalizingEngine(), force_endpoint_silence_ms=30.0)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_fall_silent(processor)
        max_ms = float(_field(_load_line(caplog), 'engine_ms_max'))
        assert max_ms >= 45.0

    def test_the_final_inference_is_counted_as_an_engine_call(self, caplog):
        """One gate-opening chunk plus one finalize is two engine calls.
        engine_ms_total divided by engine_calls is how a reader gets a mean
        per-call cost, so an uncounted call skews that too."""
        processor, engine = _make_processor(
            engine=_FinalizingEngine(), force_endpoint_silence_ms=30.0)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_fall_silent(processor)
        assert int(_field(_load_line(caplog), 'engine_calls')) == 2

    def test_the_ratio_crosses_one_when_the_final_inference_is_slow(self, caplog):
        """The whole point of the fix. 50ms of inference over 30ms of audio
        is inference lag, and the line has to say so."""
        processor, engine = _make_processor(
            engine=_FinalizingEngine(), force_endpoint_silence_ms=30.0)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_fall_silent(processor)
        ratio = float(_field(_load_line(caplog), 'engine_ratio'))
        assert ratio >= 1.0, (
            "an untimed final inference reads as engine_ratio 0.00, which "
            "rules inference lag out while it is happening")

    def test_the_final_inference_does_not_inflate_the_audio_duration(self, caplog):
        """finalize() re-recognizes audio already counted once, so counting
        its samples again would grow the denominator and push the ratio back
        down -- the same understatement by another route. Only the gate
        chunk's 30ms is real audio."""
        processor, engine = _make_processor(
            engine=_FinalizingEngine(), force_endpoint_silence_ms=30.0)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_fall_silent(processor)
        assert float(_field(_load_line(caplog), 'audio_ms')) == 30.0

    def test_an_engine_without_finalize_still_reports_its_forced_line(self, caplog):
        """_StubEngine has no finalize attribute at all, which is the shape
        of a streaming-only engine. The timing must not assume the method
        exists."""
        processor, engine = _make_processor(force_endpoint_silence_ms=30.0)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_fall_silent(processor)
        line = _load_line(caplog)
        assert _field(line, 'kind') == 'force'
        assert int(_field(line, 'engine_calls')) == 1

    def test_a_failing_finalize_still_propagates(self, caplog):
        """Pins today's behaviour rather than changing it: finalize() is not
        wrapped in a try/except, and adding timing around it must not quietly
        start swallowing the error."""
        processor, engine = _make_processor(
            engine=_RaisingFinalizeEngine(), force_endpoint_silence_ms=30.0)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            with pytest.raises(RuntimeError, match="final inference failed"):
                _speak_then_fall_silent(processor)


PROCEDURE_DOC = (pathlib.Path(__file__).resolve().parents[4]
                 / 'docs' / 'testing' / 'stt-cpu-load-test-procedure.md')


def _document_prose():
    """The procedure document with its fenced code blocks removed.

    The fences hold the log-line format itself, so every field name appears
    inside them whether or not the document explains it.
    """
    keep, fenced = [], False
    for line in _procedure_text().splitlines():
        if line.startswith('```'):
            fenced = not fenced
            continue
        if not fenced:
            keep.append(line)
    return '\n'.join(keep)


class TestTheProcedureDocumentReadsEveryFieldTheLinePrints:
    """The reading guide has to map every number the line carries.

    Found by deepseek as wh-stt-load-metrics.1.2. The line printed `drops`
    from the first version, but the guide had a bullet for overflow,
    status_flags, engine_ratio, q_max, stalls and n/a, and none for drops.
    A run whose only loss counter moved therefore had no reading at all, and
    the verdict template's `neither` paragraph asked only about `overflow`
    and `engine_ratio` -- so a queue-full run read as "no failure found".
    That matters most on the WinRT capture path, which reports `overflow` as
    `n/a`, leaving `drops` as the line's only loss counter.

    These tests read the shipped document. That couples this suite to a path
    outside the service on purpose: the document and the format string are
    one measurement, and a field added to one and not the other is exactly
    the defect above.
    """

    def test_the_document_is_where_the_tests_expect_it(self):
        assert _procedure_text()

    def test_every_field_of_the_emitted_line_is_read_in_the_document(self, caplog):
        """Reads the field names off a real emitted line rather than off the
        format string, so a field renamed in the code is caught too.

        The document's own copy of the format is excluded from the search:
        every field appears there by construction, so a check that counted it
        would pass for any field whatsoever and prove nothing. Only the prose
        counts, and only as `field` in backticks -- the phrase "no drops" in
        the n/a bullet was the whole of what the pre-fix document said about
        drops, and it is not a reading.
        """
        processor, engine = _make_processor()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _speak_then_end(processor, engine)
        line = _load_line(caplog)
        names = [part.partition('=')[0] for part in line.split()
                 if '=' in part]
        assert names, line
        prose = _document_prose()
        missing = [n for n in names if f'`{n}`' not in prose]
        assert not missing, (
            f"the load line prints {missing} but the procedure document "
            f"never reads them")

    def test_the_reading_guide_maps_drops(self):
        text = _procedure_text()
        assert '- **`drops`' in text, (
            "the reading guide has no bullet for drops, so a queue-full run "
            "has nothing to read it against")

    def test_the_no_failure_verdict_requires_drops_to_be_zero(self):
        """`neither` is the reading that stops both follow-up fixes, so it
        must not be reachable while drops is above zero."""
        text = _procedure_text()
        head, sep, tail = text.partition('`neither` is a real outcome')
        assert sep, "the verdict document no longer explains `neither`"
        assert 'drops' in tail, (
            "the `neither` paragraph asks about overflow and engine_ratio "
            "but not drops, so consumer-side capture loss reads as no failure")

    def test_the_failure_table_does_not_send_queue_full_loss_to_the_capture_thread(self):
        """drops is a consumer-side loss: the callback captured the frame and
        threw it away because the queue was full. Raising the capture
        thread's priority cannot prevent it, so the table must not offer
        MMCSS as its fix."""
        text = _procedure_text()
        rows = [ln for ln in text.splitlines()
                if ln.startswith('|') and '`drops` above 0' in ln]
        assert len(rows) == 1, rows
        assert 'MMCSS' not in rows[0], rows[0]


class _CountingCaptureStats:
    """A capture-stats reader whose loss counters advance on demand."""

    def __init__(self):
        self.drops = 0
        self.status_flags = 0
        self.overflow_count = 0
        self.qsize = 0

    def lose_a_frame(self):
        """One frame lost in the callback, counted every way it is counted."""
        self.drops += 1
        self.status_flags += 1
        self.overflow_count += 1

    def __call__(self):
        return {
            'captured': 0,
            'drops': self.drops,
            'qsize': self.qsize,
            'max_q': 0,
            'status_flags': self.status_flags,
            'overflow_count': self.overflow_count,
        }


def _lead_in_then_speak_then_end(processor, engine, stats, silent_chunks=12,
                                 lose_after=6, text="hello"):
    """Feed a lead-in of silence, lose a frame during it, then speak.

    The lead-in buffer is 300ms by default and its contents are fed to the
    engine as the opening of the utterance, so a frame lost while those
    chunks were being captured is a frame lost from this utterance's audio.
    """
    processor.vad.is_speech = Mock(return_value=False)
    for i in range(silent_chunks):
        if i == lose_after and stats is not None:
            stats.lose_a_frame()
        processor.process_chunk(CHUNK_30MS)
    processor.vad.is_speech = Mock(return_value=True)
    processor.process_chunk(CHUNK_30MS)
    engine.endpoint = True
    engine.result = text
    processor.process_chunk(CHUNK_30MS)


class TestTheCaptureBaselineCoversTheLeadIn:
    """Loss during the lead-in belongs to the utterance it opens.

    Found by codex as wh-stt-load-metrics.1.3. The baseline was read at
    gate-open time, on the consumer thread, from counters the capture
    callback had already advanced for the lead-in frames. Anything lost
    while the onset of speech was being captured therefore sat in both the
    baseline and the closing read and subtracted to zero. The line then
    printed overflow=0 status_flags=0 drops=0, which the procedure document
    maps to no callback loss and no consumer-side loss, for an utterance
    whose first word had already been damaged.
    """

    def test_an_overflow_during_the_lead_in_reaches_the_line(self, caplog):
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, stats)
        assert _field(_load_line(caplog), 'overflow') == '1'

    def test_a_status_flag_during_the_lead_in_reaches_the_line(self, caplog):
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, stats)
        assert _field(_load_line(caplog), 'status_flags') == '1'

    def test_a_queue_full_drop_during_the_lead_in_reaches_the_line(self, caplog):
        """All three delta fields share the one baseline, so all three are
        fixed or none are."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, stats)
        assert _field(_load_line(caplog), 'drops') == '1'

    def test_loss_during_speech_is_still_reported(self, caplog):
        """The earlier baseline must not cost the case that already worked."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            processor.vad.is_speech = Mock(return_value=False)
            for _ in range(12):
                processor.process_chunk(CHUNK_30MS)
            processor.vad.is_speech = Mock(return_value=True)
            processor.process_chunk(CHUNK_30MS)
            stats.lose_a_frame()
            engine.endpoint = True
            engine.result = "hello"
            processor.process_chunk(CHUNK_30MS)
        assert _field(_load_line(caplog), 'overflow') == '1'

    def test_the_previous_utterance_loss_is_not_counted_again(self, caplog):
        """The kept history must not survive an endpoint, or the second
        utterance inherits the first one's baseline and reports its loss a
        second time."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, stats)
            engine.endpoint = False
            _lead_in_then_speak_then_end(processor, engine, None, text="again")
        lines = _load_lines(caplog)
        assert len(lines) == 2, lines
        assert _field(lines[0], 'overflow') == '1'
        assert _field(lines[1], 'overflow') == '0'

    def test_a_provider_with_no_stats_reader_still_prints_n_a(self, caplog):
        """The history must not turn an unknown counter into a number."""
        processor, engine = _make_processor()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, None)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == 'n/a'
        assert _field(line, 'drops') == 'n/a'


def _burst_then_speak_then_end(processor, engine, stats, silent_chunks,
                               lose_after, chunk=CHUNK_30MS, text="hello"):
    """Drain a backlog of silence faster than real time, then speak.

    This is the catch-up burst. The consumer loop was descheduled, the
    microphone kept enqueueing, and the loop now drains every queued chunk
    back to back, so no wall-clock time passes between the chunks. Nothing
    in this helper sleeps, which is exactly the condition being reproduced.
    """
    processor.vad.is_speech = Mock(return_value=False)
    for i in range(silent_chunks):
        if i == lose_after and stats is not None:
            stats.lose_a_frame()
        processor.process_chunk(chunk)
    processor.vad.is_speech = Mock(return_value=True)
    processor.process_chunk(chunk)
    engine.endpoint = True
    engine.result = text
    processor.process_chunk(chunk)


class TestTheCaptureHistoryTracksLeadInAudioNotTheClock:
    """The kept history must be trimmed by the audio the lead-in buffer
    keeps, not by the consumer thread's clock.

    Found by codex as wh-stt-load-metrics.1.5. LeadInBuffer.add evicts by
    audio bytes; the history evicted by a time.monotonic() reading taken on
    the consumer thread. The two rules agree only while chunks arrive in
    real time. A descheduled loop that resumes and drains its backlog gives
    every chunk of that burst nearly the same timestamp, so nothing was
    trimmed and the baseline reached back past audio the lead-in buffer had
    already thrown away. The utterance was then charged with loss that was
    in neither its lead-in nor its speech, which sends the procedure
    document's failure table toward two fixes the run does not need.

    The reciprocal case is the same misalignment in the other direction: a
    hard deque cap of 128 entries could not cover a long lead-in of small
    chunks, so loss from the early part of that lead-in sat in the baseline
    and never reached the line at all.
    """

    def test_loss_before_the_retained_lead_in_is_not_charged(self, caplog):
        """Twenty queued chunks drain at once; the lead-in buffer keeps the
        last ten. A frame lost during the fourth is in neither the retained
        lead-in nor the speech, so this utterance did not lose it."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=20, lose_after=3)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == '0'
        assert _field(line, 'status_flags') == '0'
        assert _field(line, 'drops') == '0'

    def test_loss_inside_the_retained_lead_in_is_still_charged(self, caplog):
        """The same burst, with the loss in audio the lead-in buffer kept.
        Trimming must not throw the real case away with the false one."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=20, lose_after=15)
        assert _field(_load_line(caplog), 'overflow') == '1'

    def test_loss_during_speech_after_a_burst_is_still_charged(self, caplog):
        """A burst before the gate opened must not cost the plainest case."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            processor.vad.is_speech = Mock(return_value=False)
            for _ in range(20):
                processor.process_chunk(CHUNK_30MS)
            processor.vad.is_speech = Mock(return_value=True)
            processor.process_chunk(CHUNK_30MS)
            stats.lose_a_frame()
            engine.endpoint = True
            engine.result = "hello"
            processor.process_chunk(CHUNK_30MS)
        assert _field(_load_line(caplog), 'overflow') == '1'

    def test_the_history_keeps_the_lead_in_buffers_chunks_and_one_more(self):
        """The invariant the fix rests on, checked against a real
        LeadInBuffer: the entries after the oldest are exactly the audio
        that buffer still holds, and the oldest is the baseline entry that
        precedes it."""
        from shared_audio.lead_in_buffer import LeadInBuffer
        stats = _CountingCaptureStats()
        processor, _ = _make_processor(capture_stats=stats)
        processor.lead_in_buffer = LeadInBuffer(lead_time_s=0.3,
                                                sample_rate=16000)
        processor.vad.is_speech = Mock(return_value=False)
        for _ in range(20):
            processor.process_chunk(CHUNK_30MS)
        history = list(processor._load_capture_history)
        assert len(history) == 11
        assert (sum(n for n, _ in history[1:])
                == processor.lead_in_buffer.current_size)

    def test_the_history_cannot_grow_past_the_lead_in_it_covers(self):
        """Removing the hard cap must not leave the history unbounded: the
        audio-byte rule is the bound."""
        stats = _CountingCaptureStats()
        processor, _ = _make_processor(capture_stats=stats)
        processor.vad.is_speech = Mock(return_value=False)
        for _ in range(500):
            processor.process_chunk(CHUNK_30MS)
        assert len(processor._load_capture_history) == 11

    def test_loss_in_the_oldest_retained_chunk_is_charged(self, caplog):
        """The boundary case the extra entry exists for. Twenty chunks drain
        at once and the lead-in buffer keeps the last ten, so chunk ten is
        the oldest audio this utterance opens with. The baseline has to be
        the snapshot BEFORE it, or loss during that chunk sits in the
        baseline and subtracts to zero."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=20, lose_after=10)
        assert _field(_load_line(caplog), 'overflow') == '1'

    def test_a_second_utterance_charges_its_own_lead_in_loss(self, caplog):
        """Clearing the history at the endpoint has to clear the audio total
        with it. A stale total makes the next utterance's window collapse to
        the newest snapshot, and its own lead-in loss then never reaches the
        line."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, stats)
            engine.endpoint = False
            _lead_in_then_speak_then_end(processor, engine, stats,
                                         text="again")
        lines = _load_lines(caplog)
        assert len(lines) == 2, lines
        assert _field(lines[0], 'overflow') == '1'
        assert _field(lines[1], 'overflow') == '1'

    def test_a_long_lead_in_of_small_chunks_is_covered_end_to_end(self, caplog):
        """A 2000ms lead-in of 10ms chunks is 200 chunks of this
        utterance's own opening audio. Every one of them needs a snapshot,
        or loss from the early part of the lead-in sits in the baseline and
        never reaches the line."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats,
                                            vad_lead_in_ms=2000)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=200, lose_after=30,
                                       chunk=CHUNK_10MS)
        assert _field(_load_line(caplog), 'overflow') == '1'


class _LateCaptureStats(_CountingCaptureStats):
    """A capture-stats reader that is not ready when the processor is built.

    A provider whose stream opens after the AudioProcessor is constructed
    cannot answer the construction-time read, so the first utterance has no
    snapshot preceding its own opening audio.
    """

    def __init__(self, fail_first=1):
        super().__init__()
        self.calls = 0
        self._fail_first = fail_first

    def __call__(self):
        self.calls += 1
        if self.calls <= self._fail_first:
            raise RuntimeError("capture not started")
        return super().__call__()


class TestTheBaselinePrecedesTheOldestRetainedChunk:
    """Until the lead-in buffer overflows, its oldest chunk is chunk zero,
    and nothing was sampled before that chunk was captured.

    Found by codex as wh-stt-load-metrics.1.6. The silent path adds the
    chunk to the lead-in buffer and then reads the capture counters, so the
    first history entry already contains whatever that chunk lost. While the
    buffer has not yet evicted anything, that entry is also the baseline, so
    the loss sat in both ends of the delta and the line printed the
    reassuring zero for an utterance whose retained opening audio was
    damaged. It reaches every lead-in at or below the buffer's capacity: a
    single silent chunk, a lead-in that never fills, and one that fills
    exactly without evicting.
    """

    def test_one_silent_chunk_that_lost_a_frame_reaches_the_line(self, caplog):
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=1, lose_after=0)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == '1'
        assert _field(line, 'status_flags') == '1'
        assert _field(line, 'drops') == '1'

    def test_a_lead_in_that_never_fills_still_charges_its_first_chunk(self, caplog):
        """Five 30ms chunks is 150ms, half the 300ms capacity, so the buffer
        has evicted nothing and chunk zero is still this utterance's audio."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=5, lose_after=0)
        assert _field(_load_line(caplog), 'overflow') == '1'

    def test_a_lead_in_filled_exactly_still_charges_its_first_chunk(self, caplog):
        """Ten 30ms chunks is exactly 300ms. LeadInBuffer evicts only when the
        total EXCEEDS capacity, so chunk zero is retained and this is the last
        lead-in length the defect reached."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=10, lose_after=0)
        assert _field(_load_line(caplog), 'overflow') == '1'

    def test_the_second_utterance_gets_its_own_preceding_snapshot(self, caplog):
        """The snapshot taken when the previous utterance ended precedes this
        one's first chunk, so a short second lead-in is covered too."""
        stats = _CountingCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, None,
                                       silent_chunks=3, lose_after=None)
            engine.endpoint = False
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=3, lose_after=0,
                                       text="again")
        lines = _load_lines(caplog)
        assert len(lines) == 2, lines
        assert _field(lines[0], 'overflow') == '0'
        assert _field(lines[1], 'overflow') == '1'

    def test_no_preceding_snapshot_reports_unknown_not_zero(self, caplog):
        """When the reader cannot answer before the first chunk is captured,
        the delta is genuinely unknown. This whole measurement exists to rule
        capture loss in or out, so overflow=0 would be a false all-clear."""
        stats = _LateCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=3, lose_after=0)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == 'n/a'
        assert _field(line, 'status_flags') == 'n/a'
        assert _field(line, 'drops') == 'n/a'

    def test_a_failed_reseed_does_not_leave_the_previous_seal_standing(self):
        """The seal belongs to one utterance. If the reading taken when the
        previous utterance ended did not arrive, the next utterance has no
        predecessor again and must say so, not inherit the last one's word
        that it had one."""
        stats = _CountingCaptureStats()
        processor, _ = _make_processor(capture_stats=stats)
        processor._read_capture_stats = lambda: None
        processor._reset_for_new_utterance()
        processor._read_capture_stats = stats
        processor.vad.is_speech = Mock(return_value=False)
        processor.process_chunk(CHUNK_30MS)
        assert processor._capture_baseline() is None

    def test_a_late_reader_recovers_once_the_buffer_evicts(self, caplog):
        """The unknown state is not permanent: as soon as the lead-in buffer
        evicts a chunk, the entry before the oldest retained one is a real
        snapshot again."""
        stats = _LateCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=20, lose_after=15)
        assert _field(_load_line(caplog), 'overflow') == '1'


    def test_the_seal_lands_on_the_lead_in_buffers_own_first_eviction(
            self, caplog):
        """Recovery is due the moment the lead-in buffer drops a chunk.

        Found by codex as wh-stt-load-metrics.1.8. The seal was set by the
        history's own trim, which keeps one entry MORE than the lead-in
        holds, so it fired one chunk after the buffer's first eviction. At
        the default 300ms lead-in and 30ms chunks that is the eleventh
        silent chunk: the buffer has dropped chunk one and retains chunks
        two to eleven, the reading taken after chunk one therefore precedes
        every retained chunk, and the line still printed n/a for all three
        capture fields while the numbers were sitting there.
        """
        stats = _LateCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=11, lose_after=5)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == '1', line
        assert _field(line, 'status_flags') == '1', line

    def test_one_chunk_before_that_eviction_still_reports_unknown(
            self, caplog):
        """The other side of the same boundary, and the reason the seal
        cannot simply be set on the first sample. Ten 30ms chunks fill the
        300ms lead-in exactly, LeadInBuffer.add evicts only when the total
        EXCEEDS its capacity, so chunk one is still retained and the reading
        taken after it is not a predecessor of anything. n/a is the honest
        answer here, and a fix that seals one chunk early would print a
        delta that already contained the loss."""
        stats = _LateCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _burst_then_speak_then_end(processor, engine, stats,
                                       silent_chunks=10, lose_after=5)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == 'n/a', line


class _PreStreamCaptureStats(_CountingCaptureStats):
    """A capture reader before and after its own stream exists.

    The deleted sounddevice reader answered four keys while its _stream was
    None -- captured, drops, qsize, max_q -- and six once start() had built
    the MicrophoneStream, because status_flags and overflow_count lived on
    the stream object (wh-portaudio-capture-removal). A processor built
    before the provider started therefore read the shorter shape, and this
    stub is that shape change. The shape itself still has to be handled: a
    reading that gains keys after construction is what the baseline order
    below must survive.
    """

    def __init__(self):
        super().__init__()
        self.stream_open = False

    def open_stream(self):
        self.stream_open = True

    def __call__(self):
        stats = super().__call__()
        if self.stream_open:
            return stats
        return {key: value for key, value in stats.items()
                if key not in ('status_flags', 'overflow_count')}


class TestTheBaselinePrecedesEveryFrameTheCallbackCounts:
    """The baseline must be older than the first frame the callback can lose.

    Found by codex as wh-stt-load-metrics.1.9, then as .1.10 inside the fix
    for .1.9. Two orderings are wrong and one is right.

    Reading at construction is too early in the wrong way: the capture object
    has no stream yet, its get_stats answers four keys instead of six, and
    _log_load_metrics prints n/a for a key missing from EITHER end. That
    withheld the first utterance's callback counters (.1.9).

    Reading after audio_capture.start() returns is too LATE. The callback
    begins filling the queue the moment the stream opens, and under the CPU
    starvation this measurement exists to expose the provider thread can be
    preempted before it takes the reading. The queue is not discarded, so
    those frames become the first utterance's audio while their loss already
    sits in the baseline -- overflow=0 for an utterance that lost frames,
    which is the one reading that rules capture loss out (.1.10).

    The right moment is before the stream opens, where the counters are
    provably zero: the capture object zeroes its frame and drop counters in
    __init__, and only start() can move them. This was measured on
    MicrophoneStream and SounddeviceAudioCapture, which
    wh-portaudio-capture-removal deleted; WinRTAudioCapture.__init__ sets
    _frames_captured = 0 and _drops = 0 the same way.
    """

    def test_loss_before_the_consumer_reads_is_charged_to_the_utterance(
            self, caplog):
        """The .1.10 defect. The frame is lost after the stream opens and
        before the consumer loop reads its first chunk, and the audio it was
        lost from is this utterance's lead-in."""
        stats = _PreStreamCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        processor.seed_capture_baseline_before_capture_starts()
        stats.open_stream()
        stats.lose_a_frame()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, stats,
                                         silent_chunks=3, lose_after=None)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == '1', line
        assert _field(line, 'status_flags') == '1', line

    def test_a_queue_full_drop_before_the_first_read_is_charged(self, caplog):
        """drops shares the same baseline, so it is fixed or broken with the
        other two."""
        stats = _PreStreamCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        processor.seed_capture_baseline_before_capture_starts()
        stats.open_stream()
        stats.lose_a_frame()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, stats,
                                         silent_chunks=3, lose_after=None)
        assert _field(_load_line(caplog), 'drops') == '1'

    def test_the_callback_counters_are_reported_not_withheld(self, caplog):
        """The .1.9 case stays fixed. A baseline taken before the stream
        exists still carries the two counters the stream will report, so the
        first utterance prints numbers rather than n/a."""
        stats = _PreStreamCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        processor.seed_capture_baseline_before_capture_starts()
        stats.open_stream()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, stats,
                                         silent_chunks=3, lose_after=1)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == '1', line
        assert _field(line, 'status_flags') == '1', line

    def test_a_provider_that_never_counts_still_reports_unknown(self, caplog):
        """The trap in the zero baseline, and the reason it is safe. The
        WinRT capture path has no PortAudio status flags and never reports
        those two keys at either end. A zero baseline must not turn that
        honest n/a into overflow=0, which is the reading that rules capture
        loss out."""
        stats = _PreStreamCaptureStats()      # its stream never opens
        processor, engine = _make_processor(capture_stats=stats)
        processor.seed_capture_baseline_before_capture_starts()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, stats,
                                         silent_chunks=3, lose_after=1)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == 'n/a', line
        assert _field(line, 'status_flags') == 'n/a', line
        assert _field(line, 'drops') == '1', line

    def test_the_zero_baseline_replaces_any_earlier_reading(self, caplog):
        """The seed taken at construction is not kept beside the new one.
        Leaving it in place would make the older entry the baseline and undo
        the whole point."""
        stats = _PreStreamCaptureStats()
        processor, engine = _make_processor(capture_stats=stats)
        processor.vad.is_speech = Mock(return_value=False)
        processor.process_chunk(CHUNK_30MS)
        stats.lose_a_frame()
        processor.process_chunk(CHUNK_30MS)
        processor.seed_capture_baseline_before_capture_starts()
        stats.open_stream()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, stats,
                                         silent_chunks=3, lose_after=None)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == '1', line

    def test_the_zero_baseline_needs_no_working_reader(self, caplog):
        """It is a statement about the capture source, not a measurement of
        it, so a reader that cannot answer yet does not stop it. That is what
        makes it usable before the provider has opened anything."""
        stats = _LateCaptureStats(fail_first=1)
        processor, engine = _make_processor(capture_stats=None)
        processor._capture_stats = stats
        processor.seed_capture_baseline_before_capture_starts()
        with caplog.at_level(logging.INFO, logger="shared_stt.audio_processor"):
            _lead_in_then_speak_then_end(processor, engine, stats,
                                         silent_chunks=3, lose_after=1)
        line = _load_line(caplog)
        assert _field(line, 'overflow') == '1', line


class _SwitchableCaptureStats:
    """A window reader that can be made unavailable and available again."""

    def __init__(self):
        self.available = True
        self.drops = 0

    def __call__(self):
        if not self.available:
            raise RuntimeError("provider restarting")
        return {
            'captured': 0,
            'drops': self.drops,
            'qsize': 0,
            'max_q': 0,
            'status_flags': 0,
            'overflow_count': 0,
        }


class TestStallsDoNotCrossAnUnmeasuredWindow:
    """A window whose summary could not be produced must still close the
    stall tracker's window.

    Found by codex as wh-stt-load-metrics.1.7. record_iteration advanced the
    window and cleared the queue high-water mark at every elapsed boundary,
    but the stall tracker was reset only inside _summary, after the guard
    that returns None when either end of the capture reading is missing. A
    provider being restarted while the consumer still drains audio therefore
    banked its stalls, and the first window that could print a line reported
    them as its own.
    """

    def _stall(self, reporter, clock, seconds=3.0):
        clock.advance(seconds)
        return reporter.record_iteration()

    def test_a_stall_in_an_unmeasured_window_is_not_reported_later(self, caplog):
        stats = _SwitchableCaptureStats()
        clock = _Clock()
        reporter = _reporter(stats, clock)

        stats.available = False
        _advance(reporter, clock, 2.0)
        self._stall(reporter, clock)
        _advance(reporter, clock, 6.0)

        stats.available = True
        _advance(reporter, clock, 10.0)
        lines = _advance(reporter, clock, 10.0)

        summary = _summary(lines)
        assert 'stalls=0' in summary, summary
        # max_gap_ms records EVERY gap, not only gaps past the stall
        # threshold, so this window's own value is the 500ms between its
        # iterations. What must not appear is the 3000 from the window whose
        # summary could not be produced.
        assert 'max_gap_ms=500' in summary, summary

    def test_a_stall_is_not_repeated_in_the_next_reported_window(self):
        """Closing the window has to actually clear the counters, not only
        read them. Two windows that both print must not both claim the one
        stall."""
        stats = _SwitchableCaptureStats()
        clock = _Clock()
        reporter = _reporter(stats, clock)

        _advance(reporter, clock, 10.0)
        _advance(reporter, clock, 2.0)
        self._stall(reporter, clock)
        _advance(reporter, clock, 10.0)
        lines = _advance(reporter, clock, 10.0)

        summary = _summary(lines)
        assert 'stalls=0' in summary, summary
        assert 'max_gap_ms=500' in summary, summary

    def test_a_stall_in_the_reported_window_is_still_reported(self):
        """Closing the tracker at every boundary must not cost the case that
        already worked."""
        stats = _SwitchableCaptureStats()
        clock = _Clock()
        reporter = _reporter(stats, clock)

        _advance(reporter, clock, 10.0)
        _advance(reporter, clock, 2.0)
        self._stall(reporter, clock)
        lines = _advance(reporter, clock, 10.0)

        summary = _summary(lines)
        assert 'stalls=1' in summary, summary
        assert 'max_gap_ms=3000' in summary, summary


class _BusyClock:
    """A wall clock and a work-seconds reader that move independently.

    The real pair moves together: time.monotonic() runs while the recognizer
    runs, and the recognizer's own perf_counter timing accumulates over the
    same seconds. Splitting them in a test is what lets one case say "1.2
    seconds passed and the recognizer was working for all of it" and another
    say "1.2 seconds passed and it was working for none of it".
    """

    def __init__(self, start=1000.0):
        self.now = start
        self.busy = 0.0

    def __call__(self):
        return self.now

    def read_busy(self):
        return self.busy

    def advance(self, seconds, busy=0.0):
        """Move the wall clock. `busy` of those seconds were the loop's own
        work."""
        assert busy <= seconds + 1e-9, (
            "a test cannot spend more time working than passed")
        self.now += seconds
        self.busy += busy


def _busy_reporter(clock, busy_reader, interval_s=10.0, stats=None):
    from shared_audio.diagnostics import CaptureLoadReporter

    if stats is None:
        stats = {'qsize': 0, 'drops': 0, 'overflow_count': 0,
                 'status_flags': 0}
    return CaptureLoadReporter(
        capture_stats=lambda: dict(stats),
        interval_s=interval_s,
        clock=clock,
        busy_seconds=busy_reader,
    )


class TestSynchronousInferenceIsNotCalledStarvation:
    """Time inside the recognizer is not time the loop failed to run.

    Found by codex as wh-stt-load-metrics.1.12. The consumer loop calls
    record_iteration() at the top and then runs process_chunk() to the end of
    the iteration, so the gap the stall tracker measures contains the whole
    recognizer call. On the parakeet provider a forced endpoint recognizes the
    entire utterance buffer in one pass, which passes a second on a slower
    CPU, so an otherwise idle machine printed

        [stall] consumer loop made no progress for 1.2s
        (likely whole-machine CPU starvation)

    and the window line printed stalls=1. The loop had made progress for every
    one of those 1.2 seconds. That reading is the opposite of the fact, and
    this bead exists to tell scheduler starvation from inference lag, so the
    field that names starvation cannot be the one that reports inference.

    The fix subtracts the loop's own measured work from the gap. The
    recognizer's cost is already timed to the microsecond for engine_ms_total,
    so the subtraction uses a measurement that already exists rather than a
    new estimate.
    """

    def test_a_slow_engine_call_produces_no_stall_line(self):
        clock = _BusyClock()
        reporter = _busy_reporter(clock, clock.read_busy)
        reporter.record_iteration()
        clock.advance(1.2, busy=1.2)
        lines = reporter.record_iteration()
        assert not [ln for ln in lines if ln.startswith('[stall]')], lines

    def test_the_window_line_does_not_count_a_slow_engine_call_as_a_stall(self):
        clock = _BusyClock()
        reporter = _busy_reporter(clock, clock.read_busy)
        lines = []
        reporter.record_iteration()
        for _ in range(10):
            clock.advance(1.2, busy=1.2)
            lines.extend(reporter.record_iteration())
        summary = _summary(lines)
        assert 'stalls=0' in summary, summary

    def test_real_starvation_is_still_reported(self):
        """The subtraction must not silence the thing being measured."""
        clock = _BusyClock()
        reporter = _busy_reporter(clock, clock.read_busy)
        reporter.record_iteration()
        clock.advance(3.0, busy=0.0)
        lines = reporter.record_iteration()
        assert any(ln.startswith('[stall]') for ln in lines), lines

    def test_a_part_starved_part_working_gap_reports_only_the_starved_part(self):
        """The message states a duration and the procedure document reads it.
        Reporting the whole gap overstates starvation by exactly the
        recognizer's cost."""
        clock = _BusyClock()
        reporter = _busy_reporter(clock, clock.read_busy)
        reporter.record_iteration()
        clock.advance(3.0, busy=1.2)
        lines = [ln for ln in reporter.record_iteration()
                 if ln.startswith('[stall]')]
        assert len(lines) == 1, lines
        assert 'made no progress for 1.8s' in lines[0], lines[0]

    def test_a_reporter_with_no_work_reader_is_unchanged(self):
        """google_stt_server builds a bare LoopStallTracker and the capture
        callback builds another. Neither passes work time, and neither may
        change behaviour."""
        clock = _Clock()
        stats = {'qsize': 0, 'drops': 0, 'overflow_count': 0,
                 'status_flags': 0}
        reporter = _reporter(lambda: dict(stats), clock)
        reporter.record_iteration()
        clock.advance(3.0)
        lines = reporter.record_iteration()
        assert any(ln.startswith('[stall]') for ln in lines), lines

    def test_a_raising_work_reader_cannot_produce_a_stall_claim(self):
        """Without the work time the reporter cannot tell work from
        starvation, and claiming starvation without knowing is this finding's
        whole defect. It stays quiet instead."""
        clock = _BusyClock()

        def boom():
            raise RuntimeError('no reader')

        reporter = _busy_reporter(clock, boom)
        reporter.record_iteration()
        clock.advance(3.0)
        lines = reporter.record_iteration()
        assert not [ln for ln in lines if ln.startswith('[stall]')], lines

    def test_a_raising_work_reader_does_not_break_the_loop(self):
        clock = _BusyClock()
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError('no reader')

        reporter = _busy_reporter(clock, boom)
        for _ in range(4):
            clock.advance(0.03)
            reporter.record_iteration()
        assert len(calls) == 4

    def test_work_time_beyond_the_gap_does_not_go_negative(self):
        """The wall clock and the recognizer's timer are two different
        clocks, so rounding can make the work look marginally longer than the
        gap. A negative gap must read as zero, not as an enormous one."""
        clock = _BusyClock()
        reporter = _busy_reporter(clock, clock.read_busy)
        reporter.record_iteration()
        clock.now += 0.5
        clock.busy += 0.6
        lines = reporter.record_iteration()
        assert not [ln for ln in lines if ln.startswith('[stall]')], lines
        snap = reporter.stall_tracker.snapshot_and_reset_window()
        assert snap['max_gap_ms'] == 0.0, snap

    def test_the_first_work_reading_is_not_charged_to_any_gap(self):
        """Reads the helper directly, because no gap can show this.

        The reader is cumulative for the processor's life, so a first reading
        of 50 seconds means the recognizer has run for 50 seconds, not that
        the coming gap contains 50 seconds of work. The end-to-end path
        happens to hide the difference: the iteration that takes the first
        reading is also the iteration that opens the tracker's timeline, and
        that iteration never judges a gap. The guard still has to be right,
        because any later change that separates those two moments would
        charge one gap with the whole history and silence a real stall.
        """
        clock = _BusyClock()
        clock.busy = 50.0
        reporter = _busy_reporter(clock, clock.read_busy)
        assert reporter._read_busy() == 0.0
        clock.advance(0.03, busy=0.02)
        assert reporter._read_busy() == pytest.approx(0.02)

    def test_the_first_iteration_charges_no_work_to_a_gap_it_never_saw(self):
        """The reader is cumulative for the life of the processor. A reporter
        that starts after the recognizer has already run must not credit its
        first gap with all of that earlier work."""
        clock = _BusyClock()
        clock.busy = 50.0
        reporter = _busy_reporter(clock, clock.read_busy)
        reporter.record_iteration()
        clock.advance(3.0, busy=0.0)
        lines = reporter.record_iteration()
        assert any(ln.startswith('[stall]') for ln in lines), lines


class TestTheDocumentDoesNotReadInferenceTimeAsStarvation:
    """The document is half of this measurement. A correct number with a
    wrong reading produces the wrong follow-up fix."""

    def test_the_stalls_bullet_says_recognizer_time_is_excluded(self):
        text = _procedure_text()
        head, sep, tail = text.partition('- **`stalls` above 0**')
        assert sep, "the reading guide no longer has a stalls bullet"
        bullet = tail.split('- **')[0]
        assert 'The loop times its own work and subtracts it' in bullet, (
            "the stalls bullet does not say that time inside the recognizer "
            "is excluded, so a slow inference reads as CPU starvation")
        assert 'recognizer' in bullet

    def test_the_stalls_bullet_covers_work_that_is_not_the_recognizer(self):
        """wh-stt-load-metrics.1.15's sibling finding .1.14: the wake-word
        model and the VAD model are inferences the recognizer knows nothing
        about, and an operator told only about "recognizer time" would read a
        slow wake-word frame as whole-machine starvation."""
        text = _procedure_text()
        head, sep, tail = text.partition('- **`stalls` above 0**')
        assert sep, "the reading guide no longer has a stalls bullet"
        bullet = tail.split('- **')[0]
        assert 'the wake word model, which runs while' in bullet, (
            "the stalls bullet names only the recognizer, so a slow "
            "wake-word inference still reads as CPU starvation")


def _ready_reporter(clock, ready_reader, interval_s=10.0, stats=None):
    """A reporter that can be asked whether the capture source is running."""
    from shared_audio.diagnostics import CaptureLoadReporter

    if stats is None:
        # What a capture provider reports when its device never opened: a
        # real dict of real zeros, which is exactly why the zeros cannot be
        # told from a clean window without asking the provider.
        stats = {'qsize': 0, 'drops': 0, 'overflow_count': 0,
                 'status_flags': 0}
    return CaptureLoadReporter(
        capture_stats=lambda: dict(stats),
        interval_s=interval_s,
        clock=clock,
        capture_ready=ready_reader,
    )


def _window(clock, reporter, seconds=10.0, step=0.5):
    """Advance one whole window, collecting lines.

    The step is counted rather than accumulated, and 0.5 is exact in binary,
    so the last iteration lands on the boundary instead of a hair short of it.
    Steps below the one-second stall threshold keep these windows free of
    stall lines the capture tests are not about.
    """
    lines = list(reporter.record_iteration())
    for _ in range(int(round(seconds / step))):
        clock.advance(step)
        lines.extend(reporter.record_iteration())
    return lines


class TestAWindowWithNoWorkingMicrophoneIsNotACleanCapture:
    """A dead capture source must not read as a clean capture window.

    Found by codex as wh-stt-load-metrics.1.13. The sounddevice capture
    swallowed a device-open failure: it recorded setup_error, left _started
    False, and kept the MicrophoneStream object. get_stats() tested only
    whether that object existed, so it answered with six real zeros. Both
    classes are deleted (wh-portaudio-capture-removal). The Parakeet
    loop keeps running because read() returns None, and ParakeetServer never
    calls wait_ready(), so the periodic line printed
    "drops=0 overflow=0 status_flags=0" for a microphone that never captured
    anything. The operator turns this line on to run the CPU load procedure,
    so that window made a failed microphone look like a healthy capture path.
    """

    def test_a_window_with_capture_never_ready_is_marked_unavailable(self):
        clock = _Clock()
        reporter = _ready_reporter(clock, lambda: False)
        line = _summary(_window(clock, reporter))
        assert 'capture=unavailable' in line

    def test_that_window_reports_no_capture_number_at_all(self):
        """The specific defect: zeros that read as a measurement."""
        clock = _Clock()
        reporter = _ready_reporter(clock, lambda: False)
        line = _summary(_window(clock, reporter))
        for field in ('drops', 'overflow', 'status_flags', 'q_now', 'q_max'):
            assert f'{field}=n/a' in line, f'{field} still reports a number'
        assert 'drops=0' not in line

    def test_that_window_still_reports_the_loop_numbers_it_measured(self):
        """The loop really did run, and its stalls are the whole point of
        this bead. Suppressing them with the capture numbers would throw away
        the CPU starvation signal the operator turned this line on to see."""
        clock = _Clock()
        reporter = _ready_reporter(clock, lambda: False)
        lines = []
        for _ in range(3):
            lines.extend(reporter.record_iteration())
            clock.advance(2.0)
        # Three 2s gaps so far; this one runs to 7s, closing the window.
        clock.advance(5.0)
        lines.extend(reporter.record_iteration())
        line = _summary(lines)
        assert 'stalls=3' in line
        assert 'max_gap_ms=7000' in line

    def test_capture_ready_for_the_whole_window_is_not_marked(self):
        clock = _Clock()
        reporter = _ready_reporter(clock, lambda: True)
        line = _summary(_window(clock, reporter))
        assert 'capture=unavailable' not in line
        assert 'drops=0' in line

    def test_a_window_that_began_before_capture_was_ready_is_marked(self):
        """The deltas span the whole window, so a baseline taken from a dead
        provider poisons the window even if the device opens a moment later.
        Marking only on the closing reading would report that window clean."""
        ready = {'now': False}
        clock = _Clock()
        reporter = _ready_reporter(clock, lambda: ready['now'])
        lines = reporter.record_iteration()
        ready['now'] = True
        clock.advance(0.02)
        lines.extend(_window(clock, reporter))
        assert 'capture=unavailable' in _summary(lines)

    def test_the_window_whose_baseline_came_from_a_dead_provider_is_marked(
            self):
        """Recovery cannot be immediate, and pretending it is would report a
        clean window over a period that lost audio.

        Frames missed while the device was down appear in no counter at all,
        so a window overlapping that period cannot subtract its way to an
        honest drops figure. The reading that closes a dead window is also the
        next window's baseline, so that next window overlaps it.
        """
        ready = {'now': False}
        clock = _Clock()
        reporter = _ready_reporter(clock, lambda: ready['now'])
        assert 'capture=unavailable' in _summary(_window(clock, reporter))
        ready['now'] = True
        assert 'capture=unavailable' in _summary(_window(clock, reporter))

    def test_capture_reports_real_numbers_again_once_a_window_was_all_ready(
            self):
        """The marker follows the capture source; it does not latch."""
        ready = {'now': False}
        clock = _Clock()
        reporter = _ready_reporter(clock, lambda: ready['now'])
        _window(clock, reporter)
        ready['now'] = True
        _window(clock, reporter)
        third = _summary(_window(clock, reporter))
        assert 'capture=unavailable' not in third
        assert 'drops=0' in third

    def test_a_reading_that_goes_unready_at_the_close_marks_both_windows(self):
        """The closing reading is also the next window's baseline, so a
        provider that died at that moment poisons the next window too."""
        ready = {'now': True}
        clock = _Clock()
        reporter = _ready_reporter(clock, lambda: ready['now'])
        lines = list(reporter.record_iteration())
        for _ in range(19):
            clock.advance(0.5)
            lines.extend(reporter.record_iteration())
        ready['now'] = False
        clock.advance(0.5)
        lines.extend(reporter.record_iteration())   # closes window one
        first = _summary(lines)
        assert 'capture=unavailable' in first
        ready['now'] = True
        assert 'capture=unavailable' in _summary(_window(clock, reporter))

    def test_a_readiness_reader_that_raises_does_not_claim_clean_capture(self):
        """Not knowing whether capture works is not the same as knowing it
        does. Reporting zeros here is the defect itself."""
        def boom():
            raise RuntimeError('provider gone')

        clock = _Clock()
        reporter = _ready_reporter(clock, boom)
        line = _summary(_window(clock, reporter))
        assert 'capture=unavailable' in line
        assert 'drops=0' not in line

    def test_a_raising_reader_costs_the_reading_and_nothing_else(self, caplog):
        def boom():
            raise RuntimeError('provider gone')

        clock = _Clock()
        reporter = _ready_reporter(clock, boom)
        with caplog.at_level(logging.DEBUG):
            line = _summary(_window(clock, reporter))
        assert line.startswith('[load-diag] window=')

    def test_a_reporter_with_no_readiness_reader_is_unchanged(self):
        """Protects every caller that does not pass one. A reporter that
        cannot ask must keep reporting exactly what it reported before."""
        clock = _Clock()
        stats = {'qsize': 0, 'drops': 0, 'overflow_count': 0,
                 'status_flags': 0}
        reporter = _reporter(lambda: dict(stats), clock)
        line = _summary(_window(clock, reporter))
        assert 'capture=unavailable' not in line
        assert 'drops=0' in line


class TestAFailedProviderStillAnswersWithZeros:
    """Why the reporter has to ask, instead of reading the numbers.

    wh-stt-load-metrics.1.13. The capture provider keeps answering get_stats()
    after its device setup failed, and the answer is real zeros. Zeros are
    what a healthy quiet window looks like too, so the numbers alone cannot
    tell the two apart. These tests run the real adapter. The deleted
    sounddevice provider answered the same way (wh-portaudio-capture-removal).
    """

    def _dead_winrt(self):
        """A WinRT provider whose graph setup raised.

        winsdk is not installed in this service venv, so the availability
        flag is patched too; without it start() refuses before it can reach
        the setup failure under test. The failure itself is the real one:
        _setup_graph raises on the capture thread, which is where a denied
        microphone permission surfaces.
        """
        from shared_audio.capture import winrt_capture
        from shared_audio.capture.base import AudioConfig

        cls = winrt_capture.WinRTAudioCapture
        capture = cls(AudioConfig())
        with patch.object(winrt_capture, 'WINRT_AUDIO_AVAILABLE', True), \
             patch.object(cls, '_setup_graph',
                          side_effect=RuntimeError('permission denied')):
            capture.start()
            assert capture.wait_ready(timeout=2.0) is False
        return capture

    def test_a_winrt_provider_whose_setup_failed_is_not_ready(self):
        """The surviving backend. It was auto-selected ahead of the
        sounddevice one until wh-portaudio-capture-removal deleted that.
        Its setup runs on the capture thread, so the failure is recorded
        rather than raised."""
        capture = self._dead_winrt()
        assert capture.wait_ready(timeout=0.0) is False
        assert capture.setup_error is not None

    def test_that_winrt_provider_still_reports_zero_drops(self):
        """It reports four counters, not six, so the window would have said
        drops=0 with overflow and status_flags reading n/a -- which is the
        same false all-clear in a form that looks more careful."""
        stats = self._dead_winrt().get_stats()
        assert stats['drops'] == 0
        assert 'overflow_count' not in stats

    def test_a_window_over_a_dead_winrt_provider_is_marked_unavailable(self):
        capture = self._dead_winrt()
        clock = _Clock()
        reporter = _ready_reporter(
            clock, lambda: capture.wait_ready(timeout=0.0))
        reporter._capture_stats = capture.get_stats
        line = _summary(_window(clock, reporter))
        assert 'capture=unavailable' in line
        assert 'drops=0' not in line


class TestAWindowOverACaptureThatDiedIsMarkedUnavailable:
    """What the liveness answer changes for the operator's line.

    wh-stt-load-metrics.2. ParakeetServer reads wait_ready(timeout=0.0) on
    every loop iteration as the reporter's readiness reader
    (sherpa_offline_parakeet_stt_server/main.py). A provider that opened and
    later died used to answer True forever, so the window that held the death
    printed drops=0 overflow=0 -- the same shape a clean quiet window has.
    It now says capture=unavailable.

    Both directions are pinned. The change is only worth having if a live
    provider still reports its numbers: a readiness reader that answered
    False too eagerly would blank the very fields the load procedure exists
    to read, and it would do it silently.
    """

    def _line_over(self, capture):
        """A window whose frames keep arriving, over a real provider.

        The frame counter is fed rather than read from the provider, and
        deliberately: the AudioGraph is mocked here, so the real counter
        never moves, and a frozen counter marks the window
        by itself (wh-stt-load-metrics.1.15). Feeding a moving one leaves the
        readiness answer as the only thing that differs between these tests.
        """
        clock = _Clock()
        reporter = _ready_reporter(
            clock, lambda: capture.wait_ready(timeout=0.0), stats=None)
        reporter._capture_stats = _FrameCountingStats(
            captured=4200, per_read=1)
        return _summary(_window(clock, reporter))

    def test_a_winrt_graph_that_dies_marks_the_window(self):
        from shared_audio.capture import winrt_capture
        from shared_audio.capture.base import AudioConfig

        cls = winrt_capture.WinRTAudioCapture
        capture = cls(AudioConfig())
        release = threading.Event()
        with patch.object(winrt_capture, 'WINRT_AUDIO_AVAILABLE', True), \
             patch.object(cls, '_setup_graph',
                          return_value=(Mock(), Mock(), Mock())), \
             patch.object(cls, '_cleanup_graph'), \
             patch.object(cls, '_poll_frames',
                          side_effect=lambda *_: release.wait(5.0)):
            capture.start()
            try:
                assert capture.wait_ready(timeout=5.0) is True

                release.set()           # the graph torn down under us
                capture._capture_thread.join(timeout=5.0)
                assert capture._capture_thread.is_alive() is False

                line = self._line_over(capture)
                assert 'capture=unavailable' in line
                assert 'drops=0' not in line
            finally:
                release.set()
                capture.stop()

    def test_a_live_winrt_graph_still_reports_its_numbers(self):
        from shared_audio.capture import winrt_capture
        from shared_audio.capture.base import AudioConfig

        cls = winrt_capture.WinRTAudioCapture
        capture = cls(AudioConfig())
        release = threading.Event()
        with patch.object(winrt_capture, 'WINRT_AUDIO_AVAILABLE', True), \
             patch.object(cls, '_setup_graph',
                          return_value=(Mock(), Mock(), Mock())), \
             patch.object(cls, '_cleanup_graph'), \
             patch.object(cls, '_poll_frames',
                          side_effect=lambda *_: release.wait(5.0)):
            capture.start()
            try:
                assert capture.wait_ready(timeout=5.0) is True

                line = self._line_over(capture)
                assert 'capture=unavailable' not in line, (
                    'a running AudioGraph was marked as no microphone at all')
            finally:
                release.set()
                capture.stop()


class TestTheDocumentExplainsAnUnavailableCaptureWindow:
    """A new field the operator will meet in the log needs a reading, or the
    fix only moves the confusion from a wrong number to an unknown word
    (wh-stt-load-metrics.1.13)."""

    def test_the_reading_guide_covers_the_unavailable_marker(self):
        text = _procedure_text()
        head, sep, tail = text.partition('- **`capture=unavailable`')
        assert sep, (
            "the reading guide has no bullet for capture=unavailable, so an "
            "operator meeting it in the log has nothing to read it by")
        bullet = tail.split('- **')[0]
        assert 'microphone' in bullet
        assert 'zero' in bullet, (
            "the bullet does not say that a failed provider still answers "
            "with zeros, which is the reason the marker exists")

    def test_the_reading_guide_covers_a_microphone_that_died_later(self):
        """wh-stt-load-metrics.1.15. The bullet described only a device that
        never opened, so it promised the operator less than the code gave for
        a device that opened and then stopped producing frames.

        This docstring used to explain that gap by saying the reporter asks
        only whether SETUP succeeded, and that a dead device still answers
        yes. The readiness work made that false: the capture now takes the
        answer back when capture ends, so the readiness condition catches a
        backend that reports its own death. What the frame counter still
        uniquely catches is the other case, a capture source that stays alive
        by every measure its provider has and delivers nothing
        (wh-stt-load-metrics.2.1.10)."""
        text = _procedure_text()
        head, sep, tail = text.partition('- **`capture=unavailable`')
        assert sep, "the reading guide has no bullet for capture=unavailable"
        bullet = tail.split('- **')[0]
        assert 'does not move across a whole window' in bullet, (
            "the bullet does not say that a window with no captured frames "
            "is marked, so an operator whose microphone died mid-test is "
            "told the setup handshake covers a case it cannot see")

    def test_the_reading_guide_no_longer_calls_the_handshake_sticky(self):
        """wh-stt-load-metrics.2. The guide explained why the frame
        counter was needed by saying the provider never takes the ready
        answer back. It now does. An operator still reading
        that sentence has been told, in the reference they were sent to,
        that an unavailable window the handshake produced cannot happen.
        """
        text = _procedure_text()
        head, sep, tail = text.partition('- **`capture=unavailable`')
        assert sep, "the reading guide has no bullet for capture=unavailable"
        bullet = ' '.join(tail.split('- **')[0].split())
        assert 'neither provider takes that answer back' not in bullet, (
            "the guide still tells the operator the readiness answer is sticky")
        assert 'now stops answering "ready"' in bullet, (
            "the guide does not say that a capture path which dies marks "
            "its own window")


class TestTheCodeSaysWhichConditionCatchesWhichDeath:
    """wh-stt-load-metrics.2.1.10.

    The reading guide was corrected when the providers stopped answering
    ready for a dead capture path, but the code comment that explains the
    same three conditions was not. A maintainer reading _capture_unavailable
    was told the readiness condition cannot see a microphone that opened and
    then died, so the frame counter looked like the only thing standing
    between them and a silent failure. Both conditions catch a death now,
    and they catch different ones.
    """

    def test_the_unavailable_docstring_no_longer_calls_readiness_sticky(self):
        from shared_audio.diagnostics import CaptureLoadReporter
        text = CaptureLoadReporter._capture_unavailable.__doc__
        collapsed = ' '.join(text.split())
        assert 'ready for the life of the process' not in collapsed, (
            "the docstring still says the handshake answers ready for the "
            "life of the process, which wh-stt-load-metrics.2 made false")

    def test_the_unavailable_docstring_separates_the_two_deaths(self):
        from shared_audio.diagnostics import CaptureLoadReporter
        collapsed = ' '.join(
            CaptureLoadReporter._capture_unavailable.__doc__.split())
        assert 'no frame at all' in collapsed, (
            "the docstring does not say that the frame counter's own case is "
            "a capture source that stays alive and delivers nothing")

    def test_the_unavailable_docstring_names_the_class_that_answers(self):
        """wh-stt-load-metrics.2.1.12.

        The docstring credited MicrophoneStream with asking PortAudio on
        every call, and MicrophoneStream never had a wait_ready at all --
        it exposed an is_active property, which
        SounddeviceAudioCapture.wait_ready read on every probe. A
        maintainer following the docstring went looking for a method that
        did not exist. Both classes are deleted
        (wh-portaudio-capture-removal), so WinRTAudioCapture is the class
        that answers readiness and the docstring has to name it.
        """
        from shared_audio.diagnostics import CaptureLoadReporter
        collapsed = ' '.join(
            CaptureLoadReporter._capture_unavailable.__doc__.split())
        assert 'MicrophoneStream asks PortAudio' not in collapsed, (
            'the docstring still credits MicrophoneStream with the '
            'readiness answer, and MicrophoneStream never had a wait_ready')
        assert 'WinRTAudioCapture' in collapsed, (
            'the docstring does not name WinRTAudioCapture, whose '
            'wait_ready is what takes the readiness answer back')

    def test_the_unavailable_docstring_states_every_way_readiness_says_no(
            self):
        """The corrected sentence must not overclaim in its turn.

        WinRTAudioCapture.wait_ready answers False in three separate
        ways, each a different path through the capture thread: its
        setup wait expires, its capture thread has ended, or
        POLL_FAILURES_BEFORE_DEAD consecutive frame polls have raised
        (shared_audio/capture/winrt_capture.py). A maintainer weighing a
        change to the readiness path needs all three, so the docstring
        must name them. The sentence used to list the deleted PortAudio
        pair's two ways instead (wh-portaudio-capture-removal).
        """
        from shared_audio.diagnostics import CaptureLoadReporter
        collapsed = ' '.join(
            CaptureLoadReporter._capture_unavailable.__doc__.split())
        assert 'setup wait expires' in collapsed, (
            'the docstring does not say WinRTAudioCapture answers False '
            'when its setup wait expires')
        assert 'the capture thread has ended' in collapsed, (
            'the docstring does not say WinRTAudioCapture answers False '
            'once its capture thread has ended')
        assert ('POLL_FAILURES_BEFORE_DEAD consecutive frame polls have '
                'raised') in collapsed, (
            'the docstring does not say WinRTAudioCapture answers False '
            'after POLL_FAILURES_BEFORE_DEAD consecutive frame polls have '
            'raised')


class _FrameCountingStats:
    """A capture provider's stats, with a frame counter that can be frozen.

    The capture provider counts every frame it receives, silence included:
    WinRTAudioCapture increments 'captured' in its frame-poll loop, and the
    deleted PortAudio callback did the same in shared_audio/microphone.py
    (wh-portaudio-capture-removal). A counter that does not move across a
    whole window is
    therefore direct evidence that no microphone frames arrived, which is the
    evidence a one-time setup handshake cannot give.
    """

    def __init__(self, captured=0, per_read=1):
        self.captured = captured
        self.per_read = per_read

    def __call__(self):
        snapshot = self.captured
        self.captured += self.per_read
        return {'captured': snapshot, 'qsize': 0, 'drops': 0,
                'overflow_count': 0, 'status_flags': 0}


def _frame_reporter(clock, stats_source, ready_reader=None, interval_s=10.0):
    """A reporter whose provider counts frames and answers the handshake."""
    from shared_audio.diagnostics import CaptureLoadReporter

    return CaptureLoadReporter(
        capture_stats=stats_source,
        interval_s=interval_s,
        clock=clock,
        capture_ready=(lambda: True) if ready_reader is None else ready_reader,
    )


class TestAWindowThatCapturedNoFramesIsNotAMeasuredWindow:
    """wh-stt-load-metrics.1.15. The handshake used to answer once and never
    take it back, so a microphone that opened and then died kept saying
    "ready" while capturing nothing. It now reports its own death
    (wh-stt-load-metrics.2), which covers the cable pulled and the graph
    failing inside _poll_frames.

    What it still cannot report is a provider that is alive by every
    measure it has and delivering nothing: the WinRT thread goes on
    polling a graph that returns no frame. The frame counter is measured
    rather
    than asserted, so it says what no handshake can.
    """

    def test_a_window_whose_frame_counter_never_moved_is_marked(self):
        clock = _Clock()
        stats = _FrameCountingStats(captured=4200, per_read=0)
        line = _summary(_window(clock, _frame_reporter(clock, stats)))
        assert 'capture=unavailable' in line, (
            "ten seconds passed and not one frame was captured, and the "
            "window still reported its capture numbers as measurements")
        assert _field(line, 'drops') == 'n/a'
        assert _field(line, 'overflow') == 'n/a'
        assert _field(line, 'status_flags') == 'n/a'

    def test_the_handshake_answering_ready_does_not_save_it(self):
        """A provider that reports itself alive is not evidence of audio.

        This was the .1.15 case, where the handshake was sticky and said
        True for the rest of the process's life. It is now the muted or
        stalled device: the provider is running and answering honestly,
        and no frame arrives for ten seconds.
        """
        clock = _Clock()
        stats = _FrameCountingStats(captured=4200, per_read=0)
        reporter = _frame_reporter(clock, stats, ready_reader=lambda: True)
        assert 'capture=unavailable' in _summary(_window(clock, reporter))

    def test_a_window_whose_frames_kept_arriving_reports_its_numbers(self):
        clock = _Clock()
        stats = _FrameCountingStats(captured=4200, per_read=1)
        line = _summary(_window(clock, _frame_reporter(clock, stats)))
        assert 'capture=unavailable' not in line, (
            "a live microphone was marked as no microphone at all")
        assert _field(line, 'drops') == '0'

    def test_the_loop_numbers_survive_a_window_with_no_frames(self):
        """The loop really did run, and its stalls are the whole reason the
        operator turned this line on. Only the capture fields are unknown."""
        clock = _Clock()
        stats = _FrameCountingStats(captured=4200, per_read=0)
        reporter = _frame_reporter(clock, stats)
        reporter.record_iteration()
        clock.advance(3.0)                  # the loop went unscheduled
        reporter.record_iteration()
        line = _summary(_advance(reporter, clock, 7.0))
        assert 'capture=unavailable' in line
        assert _field(line, 'stalls') == '1'
        assert float(_field(line, 'max_gap_ms')) >= 3000.0

    def test_a_provider_that_does_not_count_frames_reads_as_before(self):
        """Not knowing is not evidence of death. Marking every window of such
        a provider would blank numbers it really did measure; the production
        provider reports the counter, so this is the defensive case."""
        clock = _Clock()
        stats = {'qsize': 0, 'drops': 0, 'overflow_count': 0,
                 'status_flags': 0}
        reporter = _frame_reporter(clock, lambda: dict(stats))
        assert 'capture=unavailable' not in _summary(_window(clock, reporter))

    def test_a_counter_that_went_backwards_marks_the_window(self):
        """A provider restarted inside the window leaves no honest reading:
        the old counter's frames and the new one's cannot be differenced."""
        clock = _Clock()
        stats = _FrameCountingStats(captured=4200, per_read=0)
        reporter = _frame_reporter(clock, stats)
        reporter.record_iteration()
        stats.captured = 5                  # the provider began again
        stats.per_read = 1
        assert 'capture=unavailable' in _summary(_advance(reporter, clock,
                                                          10.0))


class _IntermittentFrameStats:
    """A provider whose frame counter advances only inside given clock spans.

    The production capture provider counts every frame it receives, silence
    included, so a counter that stops advancing while the consumer loop keeps
    iterating is direct evidence that the microphone stopped delivering. This
    helper reproduces the shape a two-endpoint comparison cannot see: frames
    at one end of a window and a long silence inside it.

    The count is snapshotted before the increment, the same order
    _FrameCountingStats uses, so a reading reports the frames that existed
    before it and an outage shows up one reading late.
    """

    def __init__(self, clock, spans, captured=0):
        self._clock = clock
        self._spans = spans
        self.captured = captured

    def __call__(self):
        snapshot = self.captured
        now = self._clock()
        if any(start <= now <= end for start, end in self._spans):
            self.captured += 1
        return {'captured': snapshot, 'qsize': 0, 'drops': 0,
                'overflow_count': 0, 'status_flags': 0}


class TestASustainedCaptureOutageIsNotAMeasuredWindow:
    """wh-stt-load-metrics.1.16. The .1.15 fix compared the frame counter at
    the two ends of a window, which proves only that SOME frame arrived. One
    frame just after a window opens, followed by a microphone that dies, makes
    the endpoint difference positive while about ten seconds hold no audio --
    and the window went back to printing drops=0 overflow=0 status_flags=0 for
    a capture path that was absent for nearly all of it.

    The counter is now read every iteration, so the longest span in which it
    did not move is measured rather than inferred from the two ends.
    """

    def test_an_early_frame_then_a_dead_microphone_is_marked(self):
        """The finding's exact sequence: the device delivers just after the
        window opens, then fails, and the loop keeps iterating."""
        clock = _Clock()
        stats = _IntermittentFrameStats(clock, [(1000.0, 1001.0)])
        line = _summary(_window(clock, _frame_reporter(clock, stats)))
        assert 'capture=unavailable' in line, (
            "one frame at the start of the window let nine dead seconds "
            "report themselves as measured capture")
        assert _field(line, 'drops') == 'n/a'
        assert _field(line, 'overflow') == 'n/a'
        assert _field(line, 'status_flags') == 'n/a'

    def test_the_window_says_how_long_the_outage_lasted(self):
        """Marking the window says the numbers are worthless. The operator
        also needs the size of the hole, which is the one fact the counter
        can give and the endpoint difference throws away."""
        clock = _Clock()
        stats = _IntermittentFrameStats(clock, [(1000.0, 1001.0)])
        line = _summary(_window(clock, _frame_reporter(clock, stats)))
        assert float(_field(line, 'max_frame_gap_ms')) >= 8000.0, line

    def test_a_short_gap_leaves_the_window_measured(self):
        """A window is not condemned by ordinary jitter. Under the load this
        whole procedure creates, a moment without a frame is expected, and
        blanking the capture numbers there would delete the overflow reading
        that says what actually happened."""
        clock = _Clock()
        stats = _IntermittentFrameStats(
            clock, [(1000.0, 1003.0), (1006.0, 1010.0)])
        line = _summary(_window(clock, _frame_reporter(clock, stats)))
        assert 'capture=unavailable' not in line, (
            "a three-second gap blanked a window that measured seven good "
            "seconds")
        assert _field(line, 'drops') == '0'
        gap_ms = float(_field(line, 'max_frame_gap_ms'))
        assert 2500.0 <= gap_ms <= 3500.0, line

    def test_an_outage_that_recovers_is_still_marked(self):
        """Frames at both ends make the endpoint difference positive however
        long the hole between them is."""
        clock = _Clock()
        stats = _IntermittentFrameStats(
            clock, [(1000.0, 1001.0), (1008.0, 1010.0)])
        line = _summary(_window(clock, _frame_reporter(clock, stats)))
        assert 'capture=unavailable' in line, (
            "the counter moved at both ends, so six silent seconds in the "
            "middle reported themselves as measured capture")
        assert float(_field(line, 'max_frame_gap_ms')) >= 6000.0, line

    def test_a_live_microphone_is_not_marked(self):
        """The counter moving on every reading is the healthy case, and it
        must keep reporting real numbers."""
        clock = _Clock()
        stats = _FrameCountingStats(captured=4200, per_read=1)
        line = _summary(_window(clock, _frame_reporter(clock, stats)))
        assert 'capture=unavailable' not in line
        assert float(_field(line, 'max_frame_gap_ms')) < 1000.0, line

    def test_a_provider_that_omits_the_counter_reports_no_gap_reading(self):
        """Not knowing is not evidence of an outage. A zero here would read
        as "measured, none happened", which is the distinction this file
        already makes for every other capture field."""
        clock = _Clock()
        stats = {'qsize': 0, 'drops': 0, 'overflow_count': 0,
                 'status_flags': 0}
        line = _summary(_window(clock,
                                _frame_reporter(clock, lambda: dict(stats))))
        assert 'capture=unavailable' not in line
        assert _field(line, 'max_frame_gap_ms') == 'n/a', line

    def test_the_gap_reading_restarts_each_window(self):
        """Every number in the summary belongs to the window it reports. A
        gap carried forward would keep condemning windows whose microphone
        recovered."""
        clock = _Clock()
        stats = _IntermittentFrameStats(
            clock, [(1000.0, 1003.0), (1006.0, 1030.0)])
        reporter = _frame_reporter(clock, stats)
        first = _summary(_window(clock, reporter))
        assert float(_field(first, 'max_frame_gap_ms')) >= 2500.0, first
        second = _summary(_window(clock, reporter))
        assert float(_field(second, 'max_frame_gap_ms')) < 1000.0, second
        assert 'capture=unavailable' not in second

    def test_the_marked_window_keeps_its_loop_numbers(self):
        """The loop really did run, and its stalls are the whole reason the
        operator turned this line on."""
        clock = _Clock()
        stats = _IntermittentFrameStats(clock, [(1000.0, 1001.0)])
        line = _summary(_window(clock, _frame_reporter(clock, stats)))
        assert 'capture=unavailable' in line
        assert _field(line, 'stalls') == '0'
        assert float(_field(line, 'max_gap_ms')) >= 0.0
        assert _field(line, 'max_frame_gap_ms') != 'n/a'


class TestAFrameGapIsMeasuredInsideItsOwnWindow:
    """wh-stt-load-metrics.1.18. The .1.16 fix kept the clock reading at which
    the counter last moved across a window boundary, so an outage straddling
    one stayed one frozen span instead of restarting. It then measured that
    span from the old reading and compared the whole length against THIS
    window's half-window bound. A microphone that stopped three seconds before
    a boundary and recovered three seconds after it blanked the second window,
    which held frames for seven of its ten seconds, and printed a gap longer
    than the window it was printed for.

    The reading still survives the boundary -- that is what keeps the span
    frozen rather than restarted -- but only the part of it inside the
    reporting window is measured.
    """

    def _two_windows(self, clock, reporter):
        return (_summary(_window(clock, reporter)),
                _summary(_window(clock, reporter)))

    def test_an_outage_ending_early_in_the_next_window_leaves_it_measured(self):
        """The finding's exact sequence: frames through 1007, none until
        1013, so the second window holds about three frameless seconds out of
        ten and its capture numbers describe the other seven."""
        clock = _Clock()
        stats = _IntermittentFrameStats(
            clock, [(1000.0, 1007.0), (1013.0, 1030.0)])
        first, second = self._two_windows(clock, _frame_reporter(clock, stats))
        assert 'capture=unavailable' not in first, first
        assert 'capture=unavailable' not in second, (
            "three frameless seconds blanked a window that measured seven "
            "good ones, because the gap was measured from a reading taken in "
            "the window before it")
        assert _field(second, 'drops') == '0'
        assert _field(second, 'overflow') == '0'
        assert _field(second, 'status_flags') == '0'

    def test_the_second_windows_gap_describes_only_that_window(self):
        """Every number on a window's line is about that window. A gap that
        began before it started overstates the hole the operator is being
        asked to act on."""
        clock = _Clock()
        stats = _IntermittentFrameStats(
            clock, [(1000.0, 1007.0), (1013.0, 1030.0)])
        first, second = self._two_windows(clock, _frame_reporter(clock, stats))
        assert 2000.0 <= float(_field(first, 'max_frame_gap_ms')) <= 3000.0, (
            first)
        gap_ms = float(_field(second, 'max_frame_gap_ms'))
        assert 2500.0 <= gap_ms <= 3500.0, (
            "the second window reported the whole cross-boundary outage "
            "instead of the part inside it: %s" % second)

    def test_a_gap_is_never_longer_than_the_window_it_is_printed_for(self):
        """An outage that continues past the boundary keeps growing. Printed
        against a window it outlasts, it is a length no reading of that
        window could have produced."""
        clock = _Clock()
        stats = _IntermittentFrameStats(clock, [(1000.0, 1007.0)])
        _first, second = self._two_windows(clock,
                                           _frame_reporter(clock, stats))
        window_ms = float(_field(second, 'window').rstrip('s')) * 1000.0
        assert float(_field(second, 'max_frame_gap_ms')) <= window_ms, second

    def test_an_outage_that_fills_the_second_window_is_still_marked(self):
        """Measuring inside the window must not shrink a real outage out of
        the bound. A window with no frames at all is the whole window."""
        clock = _Clock()
        stats = _IntermittentFrameStats(clock, [(1000.0, 1007.0)])
        _first, second = self._two_windows(clock,
                                           _frame_reporter(clock, stats))
        assert 'capture=unavailable' in second, second
        assert float(_field(second, 'max_frame_gap_ms')) >= 9000.0, second

    def test_an_outage_wholly_inside_the_second_window_is_still_marked(self):
        """The frames that arrive after the boundary are what the span is
        measured from. A rule that always measured from the window start
        would condemn every window, and one that never looked before the
        last arrival would miss this one."""
        clock = _Clock()
        stats = _IntermittentFrameStats(
            clock, [(1000.0, 1011.0), (1030.0, 1040.0)])
        first, second = self._two_windows(clock, _frame_reporter(clock, stats))
        assert 'capture=unavailable' not in first, first
        assert 'capture=unavailable' in second, (
            "the microphone died one second into this window and stayed "
            "dead for the rest of it")
        assert float(_field(second, 'max_frame_gap_ms')) >= 8000.0, second

    def test_the_guide_says_the_gap_belongs_to_its_own_window(self):
        """The document is half of this measurement. An operator told the
        gap is simply "the longest span in which the count did not move"
        would read a straddling outage as one window's fault."""
        text = _procedure_text()
        head, sep, tail = text.partition(
            'A window is marked for a third reason')
        assert sep, "the reading guide no longer has the third-reason bullet"
        bullet = tail.split('- **')[0]
        assert 'the part of it that fell inside that window' in bullet, (
            "the guide does not say that an outage straddling a window "
            "boundary is measured in each window as its own part of it")


def _busy_frame_reporter(clock, stats_source, interval_s=10.0):
    """A reporter whose provider counts frames and whose loop reports its own
    work, so a long recognizer call is charged to the loop rather than read as
    a stall. This is the production shape: main.py adds every synchronous
    second to _load_work_s, and the stall tracker subtracts them.
    """
    from shared_audio.diagnostics import CaptureLoadReporter

    return CaptureLoadReporter(
        capture_stats=stats_source,
        interval_s=interval_s,
        clock=clock,
        busy_seconds=clock.read_busy,
        capture_ready=lambda: True,
    )


class TestAnUnwatchedIntervalIsNotMeasuredCapture:
    """wh-stt-load-metrics.1.20. The .1.16 fix read the frame count on every
    loop iteration, which is only as often as the loop iterates. A reading
    that saw the count move was treated as a frame arriving at that instant,
    and the seconds since the previous reading were charged to nothing.

    One long recognizer call is all it takes: the loop reads captured=100,
    consumes a chunk, spends ten seconds inside process_chunk, and reads
    captured=101 on the other side. Those ten seconds are deliberately charged
    to the loop's own work, so they are not a stall, and the count did move,
    so the window printed drops=0 overflow=0 status_flags=0 max_frame_gap_ms=0
    for ten seconds it never looked at.

    A reading is the only moment this loop learns anything about capture. The
    span is now measured on every reading, the one that sees the count move
    included, so an iteration that itself spans seconds leaves a span with no
    observed arrival exactly as a dead microphone does.
    """

    def test_a_window_the_loop_only_looked_at_twice_is_not_measured(self):
        """The finding's exact sequence, with the ten seconds charged to the
        loop's own work so nothing else can flag the window."""
        clock = _BusyClock()
        stats = _IntermittentFrameStats(clock, [(1000.0, 1000.0)])
        reporter = _busy_frame_reporter(clock, stats)
        lines = list(reporter.record_iteration())
        clock.advance(10.0, busy=10.0)
        lines.extend(reporter.record_iteration())
        line = _summary(lines)
        assert 'capture=unavailable' in line, (
            "one frame seen on the far side of a ten-second engine call let "
            "the whole window report itself as measured capture")
        assert _field(line, 'drops') == 'n/a'
        assert _field(line, 'overflow') == 'n/a'
        assert _field(line, 'status_flags') == 'n/a'

    def test_the_window_says_how_long_it_went_unwatched(self):
        """Marking says the numbers are worthless. The operator still needs
        the size of the hole, and here the hole is the interval between two
        readings."""
        clock = _BusyClock()
        stats = _IntermittentFrameStats(clock, [(1000.0, 1000.0)])
        reporter = _busy_frame_reporter(clock, stats)
        lines = list(reporter.record_iteration())
        clock.advance(10.0, busy=10.0)
        lines.extend(reporter.record_iteration())
        line = _summary(lines)
        assert float(_field(line, 'max_frame_gap_ms')) >= 9000.0, line

    def test_the_engine_call_is_not_reported_as_a_stall(self):
        """Pins the production shape the finding depends on. The loop's own
        seconds are subtracted from the stall gap, so the frame measurement
        is the only thing that can flag this window."""
        clock = _BusyClock()
        stats = _IntermittentFrameStats(clock, [(1000.0, 1000.0)])
        reporter = _busy_frame_reporter(clock, stats)
        lines = list(reporter.record_iteration())
        clock.advance(10.0, busy=10.0)
        lines.extend(reporter.record_iteration())
        line = _summary(lines)
        assert _field(line, 'stalls') == '0', line
        assert float(_field(line, 'max_gap_ms')) == 0.0, line

    def test_a_look_away_in_the_middle_of_a_healthy_window_is_marked(self):
        """Every reading in this window saw the count move. The window is
        still not measured, because for six of its ten seconds nothing was
        read at all."""
        clock = _BusyClock()
        stats = _IntermittentFrameStats(clock, [(1000.0, 1030.0)])
        reporter = _busy_frame_reporter(clock, stats)
        lines = list(reporter.record_iteration())
        for _ in range(4):
            clock.advance(0.5)
            lines.extend(reporter.record_iteration())
        clock.advance(6.0, busy=6.0)
        lines.extend(reporter.record_iteration())
        for _ in range(4):
            clock.advance(0.5)
            lines.extend(reporter.record_iteration())
        line = _summary(lines)
        assert 'capture=unavailable' in line, (
            "the loop stopped reading for six of this window's ten seconds "
            "and the window still called its capture numbers measured")
        assert float(_field(line, 'max_frame_gap_ms')) >= 5500.0, line

    def test_a_closely_watched_window_is_still_measured(self):
        """The guard against over-correcting. A loop that reads twenty times
        a window, with frames on every reading, must keep its numbers."""
        clock = _BusyClock()
        stats = _IntermittentFrameStats(clock, [(1000.0, 1030.0)])
        reporter = _busy_frame_reporter(clock, stats)
        lines = list(reporter.record_iteration())
        for _ in range(20):
            clock.advance(0.5)
            lines.extend(reporter.record_iteration())
        line = _summary(lines)
        assert 'capture=unavailable' not in line, line
        assert _field(line, 'drops') == '0'
        assert float(_field(line, 'max_frame_gap_ms')) < 1000.0, line

    def test_the_guide_says_a_reading_is_the_only_thing_that_counts(self):
        """The document is half of this measurement. An operator told the
        span is "the longest span in which the count did not move" would not
        expect a window flagged while the count moved on every reading."""
        text = _procedure_text()
        head, sep, tail = text.partition(
            'A window is marked for a third reason')
        assert sep, "the reading guide no longer has the third-reason bullet"
        bullet = tail.split('- **')[0]
        assert 'seconds nothing was observed' in bullet, (
            "the guide does not say that the seconds inside one long loop "
            "iteration are seconds the capture count was not read")


class TestTheDocumentDescribesTheMetricsThatExist:
    """wh-stt-load-metrics.1.17. The document is half of this measurement, and
    a reading guide that names a number the code never fills sends the
    operator to a conclusion nothing measured."""

    def test_the_stalls_bullet_does_not_send_other_work_to_engine_ratio(self):
        """engine_ratio is built from engine.process_audio and
        engine.finalize only. The VAD, the AGC, the keep-warm decode and the
        wake word all run outside both timers, and the wake-word branch never
        reaches AudioProcessor at all, so it produces no utterance line to
        read a ratio from."""
        text = _procedure_text()
        head, sep, tail = text.partition('- **`stalls` above 0**')
        assert sep, "the reading guide no longer has a stalls bullet"
        bullet = tail.split('- **')[0]
        assert 'belongs to `engine_ratio`' not in bullet, (
            "the stalls bullet still sends non-recognizer work to "
            "engine_ratio, which measures only the recognizer's own calls")

    def test_the_guide_says_what_engine_ratio_actually_measures(self):
        text = _procedure_text()
        assert 'engine.process_audio' in text, (
            "the guide does not say which engine calls engine_ratio is "
            "built from, so an operator cannot tell what it excludes")
        assert 'engine.finalize' in text, (
            "the guide does not name the forced-endpoint call that "
            "engine_ratio also counts")

    def test_the_guide_names_the_separate_non_recognizer_measurement(self):
        """The added counter must not be mistaken for engine_ratio."""
        text = _procedure_text()
        head, sep, tail = text.partition('- **`stalls` above 0**')
        assert sep, "the reading guide no longer has a stalls bullet"
        bullet = tail.split('- **')[0]
        assert '`pre_engine_ms_total`' in bullet
        assert '`wake_word_ms`' in bullet
        assert 'engine_ratio=n/a' in bullet

    def test_the_guide_says_which_breakdown_fields_shipped_providers_fill(self):
        """wh-audit13-preengine-load-review.2: an operator who reads 0.0 in
        keep_warm_ms or wake_word_ms must know that it is the expected value
        for that line, not a broken timer."""
        prose = ' '.join(_document_prose().split())
        head, sep, tail = prose.partition('give its breakdown.')
        assert sep, "the guide no longer has the breakdown sentence"
        nearby = tail[:700]
        assert '`keep_warm_ms` is 0.0 in every shipped provider' in nearby, (
            "the guide does not say that keep_warm_ms stays 0.0 until an "
            "engine defines keep_warm")
        assert 'no engine defines `keep_warm`' in nearby
        assert ('`wake_word_ms` is non-zero only on `work_scope=wake_word` '
                'lines') in nearby, (
            "the guide does not say which lines can carry wake_word_ms")
        assert '0.0 on `utt=` and `work_scope=silence` lines' in nearby
        assert ('`vad_ms`, `agc_ms` and `keep_warm_ms` are 0.0 on '
                '`work_scope=wake_word` lines') in nearby, (
            "the guide does not say that a wake-word line times only the "
            "detector's own inference")

    def test_the_reading_guide_covers_a_partial_capture_outage(self):
        """wh-stt-load-metrics.1.16."""
        text = _procedure_text()
        head, sep, tail = text.partition('- **`capture=unavailable`')
        assert sep, "the reading guide has no bullet for capture=unavailable"
        bullet = tail.split('- **')[0]
        assert 'max_frame_gap_ms' in bullet, (
            "the guide does not explain the field that says how long the "
            "microphone stopped delivering inside a window")


def _outage_events(lines):
    """Every whole-outage event line a run produced."""
    return [ln for ln in lines
            if ln.startswith('[load-diag] capture-outage ')]


def _outage_event(lines):
    """The single whole-outage event line the run produced."""
    events = _outage_events(lines)
    assert len(events) == 1, f"expected one outage event, got {events}"
    return events[0]


def _window_lines(lines):
    """Every per-window summary line a run produced."""
    return [ln for ln in lines if ln.startswith('[load-diag] window=')]


def _dead_then_alive(clock, alive_from):
    """A provider whose frame counter is frozen until `alive_from`.

    A frozen counter is the second of the three ways a window is marked
    unavailable, and the one that needs no cooperation from the readiness
    handshake, so it is the cheapest way to build a run of marked windows.
    """
    return _IntermittentFrameStats(clock, [(alive_from, alive_from + 1000.0)])


class _UnreadableForASpan:
    """A frozen frame counter whose reader stops answering inside a span.

    A window whose capture reading is missing is not a window with a measured
    outage: _summary returns None for it and no line is printed at all.
    """

    def __init__(self, clock, blind_spans):
        self._clock = clock
        self._blind = blind_spans

    def __call__(self):
        now = self._clock()
        if any(start <= now <= end for start, end in self._blind):
            raise RuntimeError("provider restarting")
        return {'captured': 0, 'qsize': 0, 'drops': 0,
                'overflow_count': 0, 'status_flags': 0}


class TestACaptureOutageSpanningWindowsIsOneEvent:
    """wh-whole-outage-metric. One capture outage that runs across several
    reporting windows printed one `capture=unavailable` line per window and
    nothing that stated its true start, end or total length. An operator
    reading the log had to count the lines and multiply, and a run whose
    windows were not all the same length could not be counted at all.

    The reporter now records the run of marked windows and emits one event
    line when the outage ends, beside the per-window lines, which are
    unchanged.
    """

    def _run(self, reporter, clock, windows):
        lines = []
        for _ in range(windows):
            lines.extend(_window(clock, reporter))
        return lines

    def test_an_outage_across_three_windows_produces_one_event(self):
        """The acceptance criterion: three marked windows, one event."""
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 1030.0))
        lines = self._run(reporter, clock, 4)
        assert len(_outage_events(lines)) == 1, lines

    def test_the_event_says_when_the_outage_began(self):
        """Seconds before this line's own timestamp, because the reporter's
        clock is monotonic and its origin means nothing to a reader."""
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 1030.0))
        event = _outage_event(self._run(reporter, clock, 4))
        assert _field(event, 'start_s_ago') == '40', event

    def test_the_event_says_when_the_outage_ended(self):
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 1030.0))
        event = _outage_event(self._run(reporter, clock, 4))
        assert _field(event, 'end_s_ago') == '10', event

    def test_the_event_says_how_long_the_outage_lasted(self):
        """The one number the per-window lines cannot give: three windows of
        ten seconds is a thirty-second hole, not three separate ones."""
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 1030.0))
        event = _outage_event(self._run(reporter, clock, 4))
        assert _field(event, 'duration_s') == '30', event

    def test_the_event_counts_the_windows_it_spans(self):
        """Ties the event to the lines above it in the log."""
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 1030.0))
        event = _outage_event(self._run(reporter, clock, 4))
        assert _field(event, 'windows') == '3', event

    def test_a_single_window_outage_produces_one_event(self):
        """The acceptance criterion's other half. One marked window is still
        an outage, and it must not report two events for it."""
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 1010.0))
        event = _outage_event(self._run(reporter, clock, 2))
        assert _field(event, 'windows') == '1', event
        assert _field(event, 'duration_s') == '10', event

    def test_the_event_is_not_repeated_once_it_is_reported(self):
        """Every healthy window after the recovery would otherwise reprint
        the same outage for the life of the process."""
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 1030.0))
        lines = self._run(reporter, clock, 6)
        assert len(_outage_events(lines)) == 1, lines

    def test_a_healthy_run_reports_no_event(self):
        clock = _Clock()
        stats = _FrameCountingStats(captured=4200, per_read=1)
        reporter = _frame_reporter(clock, stats)
        lines = self._run(reporter, clock, 3)
        assert _outage_events(lines) == [], lines

    def test_an_outage_still_running_reports_no_event_yet(self):
        """crewcut in the source: the event is emitted when the outage ends,
        so an outage still running when the process exits is never reported
        as one. The per-window lines are still there for the operator."""
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 9000.0))
        lines = self._run(reporter, clock, 3)
        assert _outage_events(lines) == [], lines
        assert len(_window_lines(lines)) == 3, lines

    def test_the_per_window_lines_are_unchanged(self):
        """The event is an addition. Every window still reports exactly what
        it reported before, and the recovering window still reports real
        numbers."""
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 1030.0))
        windows = _window_lines(self._run(reporter, clock, 4))
        assert len(windows) == 4, windows
        for marked in windows[:3]:
            assert 'capture=unavailable' in marked, windows
        assert 'capture=unavailable' not in windows[3], windows
        assert _field(windows[3], 'drops') == '0', windows[3]

    def test_the_event_line_comes_before_the_window_that_closed_it(self):
        """The outage ended before the window that reports the recovery, and
        the log reads in the order the two things happened."""
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 1030.0))
        lines = self._run(reporter, clock, 4)
        event = _outage_event(lines)
        assert lines.index(event) < lines.index(_window_lines(lines)[3])

    def test_an_unmeasured_window_ends_the_event_rather_than_spanning_it(self):
        """A window whose capture reading is missing is not evidence of an
        outage and not evidence of recovery, so no event may claim its
        seconds. This is the rule _track_frame_progress already applies: a
        reading the provider cannot give restarts the timeline."""
        clock = _Clock()
        stats = _UnreadableForASpan(clock, [(1010.5, 1020.0)])
        reporter = _frame_reporter(clock, stats)
        lines = self._run(reporter, clock, 2)
        assert len(_window_lines(lines)) == 1, lines
        event = _outage_event(lines)
        assert _field(event, 'windows') == '1', event
        assert _field(event, 'duration_s') == '10', event

    def test_windows_of_unequal_length_are_measured_not_multiplied(self):
        """Found by codex as wh-whole-outage-metric.1.2. Every other fixture
        in this class runs ten-second windows, so a reporter that multiplied
        its configured interval by the window count would pass all of them
        and still misreport the run this feature exists to measure -- an
        operator counting lines and multiplying is the very thing it
        replaces.

        A consumer loop that is descheduled closes its window late, which is
        the machine under load the procedure is run on. The middle window
        here runs seventeen seconds because no reading happens inside it.
        """
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 1027.0))
        # 1000 -> 1010, marked; the outage record opens.
        lines = _window(clock, reporter)
        # Descheduled: the clock moves and the loop takes no reading, so the
        # next window closes seventeen seconds after it opened.
        clock.advance(17.0)
        lines.extend(reporter.record_iteration())
        # 1027 -> 1037, frames again; the event closes here.
        lines.extend(_window(clock, reporter))
        event = _outage_event(lines)
        assert _field(event, 'duration_s') == '27', event
        assert _field(event, 'start_s_ago') == '37', event
        assert _field(event, 'end_s_ago') == '10', event
        assert _field(event, 'windows') == '2', event

    def test_a_readiness_failure_alone_opens_the_event(self):
        """Found by codex as wh-whole-outage-metric.1.5. Every other fixture
        here freezes the frame counter, which satisfies the frame-arrival
        signature and the interior-gap signature at the same time, so none of
        them can tell the three ways a window is marked apart. This one
        drives only the readiness handshake: the counter advances at every
        reading, so a record that watched frames alone would report nothing
        for a microphone that never started.
        """
        clock = _Clock()
        stats = _FrameCountingStats(captured=4200, per_read=1)
        reporter = _frame_reporter(clock, stats,
                                   ready_reader=lambda: clock() >= 1010.0)
        lines = []
        for _ in range(2):
            lines.extend(_window(clock, reporter))
        assert 'capture=unavailable' in _window_lines(lines)[0], lines
        event = _outage_event(lines)
        assert _field(event, 'windows') == '1', event
        assert _field(event, 'duration_s') == '10', event
        assert _field(event, 'start_s_ago') == '20', event
        assert _field(event, 'end_s_ago') == '10', event

    def test_an_interior_silence_alone_opens_the_event(self):
        """The third signature, also from codex as
        wh-whole-outage-metric.1.5. The counter moves at both edges of the
        window, so the endpoint comparison sees frames and the handshake
        answers yes; only the eight-second silence inside the window marks
        it. A record that compared the two endpoints would report nothing for
        a microphone that stopped for most of the window.
        """
        clock = _Clock()
        stats = _IntermittentFrameStats(
            clock, [(1000.0, 1001.0), (1009.0, 2000.0)])
        reporter = _frame_reporter(clock, stats)
        lines = []
        for _ in range(2):
            lines.extend(_window(clock, reporter))
        first = _window_lines(lines)[0]
        assert 'capture=unavailable' in first, lines
        assert float(_field(first, 'max_frame_gap_ms')) > 5000.0, first
        event = _outage_event(lines)
        assert _field(event, 'windows') == '1', event
        assert _field(event, 'duration_s') == '10', event
        assert _field(event, 'start_s_ago') == '20', event
        assert _field(event, 'end_s_ago') == '10', event


class TestTheDocumentExplainsTheWholeOutageEvent:
    """A new line the operator will meet in the log needs a reading, or the
    measurement only moves the confusion from a missing number to an unknown
    word -- the rule wh-stt-load-metrics.1.13 set for capture=unavailable."""

    def _event_line(self):
        clock = _Clock()
        reporter = _frame_reporter(clock, _dead_then_alive(clock, 1030.0))
        lines = []
        for _ in range(4):
            lines.extend(_window(clock, reporter))
        return _outage_event(lines)

    def test_every_field_of_the_event_line_is_read_in_the_document(self):
        """Reads the field names off a real emitted line rather than off the
        format string, so a field renamed in the code is caught too."""
        line = self._event_line()
        names = [part.partition('=')[0] for part in line.split()
                 if '=' in part]
        assert names, line
        prose = _document_prose()
        missing = [n for n in names if f'`{n}`' not in prose]
        assert not missing, (
            f"the outage event prints {missing} but the procedure document "
            f"never reads them")

    def test_the_opening_inventory_counts_the_outage_line(self):
        """Found by codex as wh-whole-outage-metric.1.6. The section that
        introduces the measurement counts the log lines, and the count was
        left saying two when this change added a third. The count here is
        derived from the document's own format blocks, so the sentence cannot
        drift away from what the section shows.
        """
        text = _procedure_text()
        head, sep, tail = text.partition(
            '## The numbers, and how to read them')
        assert sep, "the procedure has no section introducing the numbers"
        section = tail.split('Read them like this:')[0]
        kinds = ('[load-diag] utt=', '[load-diag] window=',
                 '[load-diag] work_scope=silence',
                 '[load-diag] work_scope=wake_word',
                 '[load-diag] capture-outage',
                 '[load-diag-capture] unavailable:')
        shown = [k for k in kinds if k in section]
        words = {2: 'Two', 3: 'Three', 4: 'Four', 5: 'Five', 6: 'Six'}
        assert len(shown) in words, shown
        stated = words[len(shown)]
        assert f'{stated} log lines carry the measurement' in section, (
            f"the section shows {len(shown)} forms of load-diag line but "
            f"does not say {stated}")

    def test_the_reading_guide_covers_the_outage_event(self):
        text = _procedure_text()
        head, sep, tail = text.partition('- **`capture-outage`')
        assert sep, (
            "the reading guide has no bullet for capture-outage, so an "
            "operator meeting it in the log has nothing to read it by")
        bullet = tail.split('- **')[0]
        assert 'timestamp' in bullet, (
            "the bullet does not say that the two times are counted back "
            "from the line's own timestamp, which is the only thing that "
            "makes a monotonic clock readable")

    def test_the_guide_says_an_outage_still_running_is_not_reported(self):
        """The accepted limit has to be in the operator's hands, or a run
        whose microphone never came back reads as a run with no outage."""
        text = _procedure_text()
        head, sep, tail = text.partition('- **`capture-outage`')
        assert sep, "the reading guide has no bullet for capture-outage"
        bullet = tail.split('- **')[0]
        assert 'still running' in bullet, (
            "the bullet does not say that an outage still running at the end "
            "of the test prints no event")

    def test_the_guide_says_the_two_times_are_window_edges(self):
        """Raised by deepseek in review round 1 and judged not a defect in
        the numbers: the event's start and end are window boundaries, so an
        operator who read them as the exact moments the microphone stopped
        and started would overstate the outage by part of a window at each
        end. The finer measurement is already on the marked lines."""
        text = _procedure_text()
        head, sep, tail = text.partition('- **`capture-outage`')
        assert sep, "the reading guide has no bullet for capture-outage"
        bullet = tail.split('- **')[0]
        assert 'window edges' in bullet, (
            "the bullet does not say that the event's two times are window "
            "boundaries rather than the moments capture stopped and started")
        assert 'max_frame_gap_ms' in bullet, (
            "the bullet does not send the operator to the finer measurement "
            "on the marked lines")


class TestTheDocumentReadsTheUnreadableReason:
    """Found by deepseek as wh-capture-load-gaps.1.2.

    The same rule wh-stt-load-metrics.1.13 set for capture=unavailable: a
    new line the operator will meet in the log needs a reading, or the
    measurement only moves the confusion from a missing number to an
    unknown word. It matters more than usual for this line, because the
    loaded run that reads it back is the next piece of work on this bead
    and the operator runs it from this document.
    """

    def _reason_line(self, stats_source):
        clock = _Clock()
        reporter = _reporter(stats_source, clock, interval_s=10.0)
        lines = []
        for _ in range(2):
            clock.advance(10.1)
            lines.extend(reporter.record_iteration())
        reasons = [ln for ln in lines
                   if ln.startswith('[load-diag-capture]')]
        assert len(reasons) == 1, lines
        return reasons[0]

    def test_every_reason_the_line_prints_is_read_in_the_document(self):
        """Reads the reason words off real emitted lines rather than off
        the source, so a reason renamed in the code is caught too -- the
        same construction as the outage line's field test above."""
        def raiser():
            raise RuntimeError('device gone')

        def not_a_dict():
            return ['not a dict']

        names = set()
        for source in (None, raiser, not_a_dict):
            body = self._reason_line(source).partition('unavailable: ')[2]
            names.update(pair.partition('=')[0]
                         for pair in body.partition(' (last:')[0].split())
        assert names == {'no-reader', 'reader-raised', 'non-dict'}, names
        prose = _document_prose()
        missing = [n for n in sorted(names) if f'`{n}`' not in prose]
        assert not missing, (
            f"the reason line can print {missing} but the procedure "
            f"document never reads them")

    def test_the_reading_guide_has_a_bullet_for_the_reason_line(self):
        text = _procedure_text()
        head, sep, tail = text.partition('- **`[load-diag-capture]')
        assert sep, (
            "the reading guide has no bullet for [load-diag-capture], so "
            "an operator meeting it in the log has nothing to read it by")
        bullet = tail.split('- **')[0]
        assert '[load-diag] window=' in bullet, (
            "the bullet does not say where to look for this line: a "
            "reading that failed at a window boundary leaves no window "
            "line at all, and that silence is what it explains")
        assert 'printed once' in bullet, (
            "the bullet does not say that no-reader is printed once for "
            "the process, so an operator would read a single line as a "
            "single failure rather than a permanent fact")
        assert 'below the startup lines' in bullet, (
            "the bullet does not say where in the log the one no-reader "
            "line appears; it is written at the first capture reading "
            "after the provider starts, not at launch, so an operator "
            "checking the top of the log would conclude it is absent "
            "(wh-capture-load-gaps.2.1)")


class TestTheUnreadableCaptureReasonIsVisible:
    """_read_capture_stats says WHICH of its three None cases fired.

    The loaded run of 2026-08-30 lost capture readings in 13 of 83 windows
    and the log could not say why: two of the three None returns were
    silent, and the third logged at debug on the module logger, which
    providers silence wholesale (Parakeet sets
    logging.getLogger("shared_stt").propagate = False with no handler).
    The reason now goes to the loadmetrics child logger, which already
    reaches wheelhouse.log, so a band of missing windows can be read back
    to its cause (wh-capture-load-gaps criterion 1).

    The rate bound is not decoration. _read_capture_stats runs on two
    per-chunk paths -- _sample_capture_history and
    _sample_capture_queue_depth -- about 33 times a second at 30 ms chunks,
    so an unbounded line per failure would bury the very band it is meant
    to explain.
    """

    #: The reason line carries a prefix of its own. It is NOT the
    #: per-utterance "[load-diag]" line, and _load_line() asserts there is
    #: exactly one of those per run.
    PREFIX = '[load-diag-capture]'

    @classmethod
    def _reason_lines(cls, caplog):
        return [r.getMessage() for r in caplog.records
                if r.getMessage().startswith(cls.PREFIX)]

    def _read_from_birth(self, caplog, capture_stats):
        """Build a processor and read once, both inside the capture block.

        __init__ calls _seed_capture_history, which takes a capture
        reading, so a processor built OUTSIDE the block has already
        reported its reason and started the rate window -- and the test's
        own read is then correctly suppressed, leaving nothing to see.
        Building inside the block is what makes that first report visible.
        """
        from shared_stt import audio_processor as ap

        with caplog.at_level(logging.INFO,
                             logger=ap.LOAD_METRICS_LOGGER_NAME):
            processor, _ = _make_processor(capture_stats=capture_stats)
            processor._read_capture_stats()
        return self._reason_lines(caplog)

    def test_a_missing_reader_names_itself(self, caplog):
        """The provider never passed a capture_stats callable."""
        lines = self._read_from_birth(caplog, None)
        assert len(lines) == 1, lines
        assert 'no-reader' in lines[0]

    def test_a_raising_reader_names_itself(self, caplog):
        """The reader raised. The error text rides along, because which
        exception it is decides where to look next."""
        def boom():
            raise RuntimeError('device gone')

        lines = self._read_from_birth(caplog, boom)
        assert len(lines) == 1, lines
        assert 'reader-raised' in lines[0]
        assert 'device gone' in lines[0]

    def test_a_non_dict_reader_names_itself(self, caplog):
        """The reader answered with something that is not a dict."""
        lines = self._read_from_birth(caplog, lambda: ['not', 'a', 'dict'])
        assert len(lines) == 1, lines
        assert 'non-dict' in lines[0]

    def test_a_reading_that_succeeds_says_nothing(self, caplog):
        """No line when the reader works. The band is what matters."""
        lines = self._read_from_birth(caplog, lambda: {'qsize': 1})
        assert lines == []

    def test_the_reason_is_on_the_loadmetrics_logger(self, caplog):
        """Not the module logger. A record on the module logger reaches no
        log at all under Parakeet, which is the whole reason this bead
        exists.

        The reading is taken here rather than left to the constructor: a
        missing reader is no longer reported from __init__, because the
        provider has not attached its handler that early
        (wh-capture-load-gaps.2.1). What this test asserts is the logger
        the record carries, which that change does not touch.
        """
        from shared_stt import audio_processor as ap

        with caplog.at_level(logging.INFO,
                             logger=ap.LOAD_METRICS_LOGGER_NAME):
            processor, _ = _make_processor(capture_stats=None)
            processor._read_capture_stats()
        names = [r.name for r in caplog.records
                 if r.getMessage().startswith(self.PREFIX)]
        assert names == [ap.LOAD_METRICS_LOGGER_NAME]

    def test_the_reason_line_is_not_the_per_utterance_load_line(self, caplog):
        """The two records must stay tellable apart. _load_line() asserts a
        run has exactly one [load-diag] line, so a shared prefix would make
        that helper count a reason line as a measurement."""
        from shared_stt import audio_processor as ap

        with caplog.at_level(logging.INFO,
                             logger=ap.LOAD_METRICS_LOGGER_NAME):
            processor, engine = _make_processor(capture_stats=None)
            _speak_then_end(processor, engine)
        assert len(self._reason_lines(caplog)) == 1
        assert len(_load_lines(caplog)) == 1

    def test_the_constructor_reading_starts_the_rate_window(self, caplog):
        """A processor built before the capture has already reported, so
        the next reading inside the window prints nothing. This is the
        suppression working, and it is why _read_from_birth exists.

        The reader raises rather than being absent: a missing reader is
        reported once per instance now (wh-capture-load-gaps.1.1), so with
        capture_stats=None this test would pass whether the rate window
        worked or not.
        """
        from shared_stt import audio_processor as ap

        def boom():
            raise RuntimeError('device gone')

        processor, _ = _make_processor(capture_stats=boom)
        with caplog.at_level(logging.INFO,
                             logger=ap.LOAD_METRICS_LOGGER_NAME):
            # The constructor's line can already be in caplog.records when
            # the run forces a level of its own (--log-cli-level=INFO).
            # This test is about the NEXT reading, so drop what came before.
            caplog.clear()
            processor._read_capture_stats()
        assert self._reason_lines(caplog) == []

    def test_a_burst_of_one_reason_yields_one_line(self, caplog):
        """Thirty failures inside the interval print once, not thirty.

        A raising reader, because a missing reader is reported once per
        instance now (wh-capture-load-gaps.1.1) and would give one line
        whether the rate bound worked or not.
        """
        from shared_stt import audio_processor as ap

        def boom():
            raise RuntimeError('device gone')

        clock = _Clock()
        with caplog.at_level(logging.INFO,
                             logger=ap.LOAD_METRICS_LOGGER_NAME):
            with patch.object(ap.time, 'monotonic', clock):
                processor, _ = _make_processor(capture_stats=boom)
                for _ in range(30):
                    processor._read_capture_stats()
        assert len(self._reason_lines(caplog)) == 1

    def test_the_next_line_counts_what_it_suppressed(self, caplog):
        """The suppressed calls are counted, not discarded: a reader that
        fails 30 times reads differently from one that failed twice."""
        from shared_stt import audio_processor as ap

        def boom():
            raise RuntimeError('device gone')

        burst = 30
        clock = _Clock()
        with caplog.at_level(logging.INFO,
                             logger=ap.LOAD_METRICS_LOGGER_NAME):
            with patch.object(ap.time, 'monotonic', clock):
                processor, _ = _make_processor(capture_stats=boom)
                for _ in range(burst):
                    processor._read_capture_stats()
                clock.advance(ap.CAPTURE_REASON_LOG_INTERVAL_S + 0.1)
                processor._read_capture_stats()
        lines = self._reason_lines(caplog)
        assert len(lines) == 2, lines
        # The constructor's own reading printed line 0 and cleared the
        # count, so line 1 carries the burst plus the reading that printed
        # it -- not the constructor's. A raising reader, because a missing
        # reader is now reported once per instance and never counted again
        # (wh-capture-load-gaps.1.1).
        assert f'reader-raised={burst + 1}' in lines[1], lines[1]

    def test_a_missing_reader_says_so_once_for_the_whole_process(
            self, caplog):
        """Found by deepseek as wh-capture-load-gaps.1.1.

        distil_medium_en builds its AudioProcessor with no capture_stats
        (services/stt_providers/distil_medium_en/main.py), which is
        deliberate and correct for that provider, and it attaches its log
        handler to shared_stt.audio_processor, the parent of the
        loadmetrics logger -- so this line does reach wheelhouse.log
        there, provided it is said after start() has attached that
        handler, which is what wh-capture-load-gaps.2.1 fixed and
        TestTheMissingReaderLineOutlivesTheConstructor pins.
        self._capture_stats is assigned once at __init__ and never
        rebound, so "no reader" can neither start nor stop being true.
        Counting it on the two per-chunk paths therefore reported a
        permanent configuration choice as a fresh measurement failure
        every five seconds, about 720 lines an hour, for the life of the
        process -- and those lines are indistinguishable at a glance from
        the real failure this bead exists to make visible.

        One line for the process is the whole of what the case can say.
        """
        from shared_stt import audio_processor as ap

        clock = _Clock()
        with caplog.at_level(logging.INFO,
                             logger=ap.LOAD_METRICS_LOGGER_NAME):
            with patch.object(ap.time, 'monotonic', clock):
                processor, _ = _make_processor(capture_stats=None)
                # Well past the rate window each time, so the bound is not
                # what is being measured here.
                for _ in range(200):
                    clock.advance(ap.CAPTURE_REASON_LOG_INTERVAL_S + 0.1)
                    processor._read_capture_stats()
        lines = self._reason_lines(caplog)
        assert len(lines) == 1, lines
        assert 'no-reader=1' in lines[0], lines[0]

    def test_a_second_reason_is_named_in_its_own_right(self, caplog):
        """Two different causes inside one interval are both named, so a
        band that mixes them cannot be read as one cause."""
        from shared_stt import audio_processor as ap

        clock = _Clock()
        # The first answer is the constructor's own reading. It must
        # succeed, so that the window under test starts empty.
        answers = [{'qsize': 0}, None, None, ['not a dict']]

        def reader():
            # A fifth reading repeats the non-dict answer on purpose: an
            # exhausted list would raise IndexError and land in the
            # reader-raised arm by accident, testing something else.
            value = answers.pop(0) if answers else ['not a dict']
            if value is None:
                raise RuntimeError('device gone')
            return value

        with caplog.at_level(logging.INFO,
                             logger=ap.LOAD_METRICS_LOGGER_NAME):
            with patch.object(ap.time, 'monotonic', clock):
                processor, _ = _make_processor(capture_stats=reader)
                processor._read_capture_stats()   # raises: prints line 0
                processor._read_capture_stats()   # raises: suppressed
                processor._read_capture_stats()   # non-dict: suppressed
                clock.advance(ap.CAPTURE_REASON_LOG_INTERVAL_S + 0.1)
                processor._read_capture_stats()   # non-dict: prints line 1
        lines = self._reason_lines(caplog)
        assert len(lines) == 2, lines
        assert 'reader-raised=1' in lines[1], lines[1]
        assert 'non-dict=2' in lines[1], lines[1]


def _attach_like_a_provider_start(handler):
    """Attach to shared_stt.audio_processor, as distil's start() does.

    Returns a callable that puts the logging state back. The level and the
    propagate flag are saved and restored because other tests in this file
    and the providers themselves both change them, and a test that leaves
    them changed breaks whatever runs next.

    At module level rather than on one class because two classes need it:
    the constructor-lifecycle tests and the reachability tests below them.
    """
    from shared_stt import audio_processor as ap

    parent = logging.getLogger('shared_stt.audio_processor')
    metrics = logging.getLogger(ap.LOAD_METRICS_LOGGER_NAME)
    before = (metrics.level, metrics.propagate)
    parent.addHandler(handler)
    metrics.setLevel(logging.INFO)
    metrics.propagate = True

    def restore():
        parent.removeHandler(handler)
        metrics.setLevel(before[0])
        metrics.propagate = before[1]

    return restore


class TestTheMissingReaderLineOutlivesTheConstructor:
    """The one no-reader line must be said where wheelhouse.log can hear it.

    Found by codex as wh-capture-load-gaps.2.1. Both providers build the
    AudioProcessor inside their own __init__ and attach the handler that
    carries provider records to wheelhouse.log later, inside start():
    distil_medium_en/main.py attaches to shared_stt.audio_processor, the
    parent of the loadmetrics logger, and the Parakeet server attaches to
    LOAD_METRICS_LOGGER_NAME itself. AudioProcessor.__init__ takes a capture
    reading of its own through _seed_capture_history.

    So a no-reader report made from the constructor reaches the provider's
    stdout and nothing else, and the latch that holds the line to one per
    process then suppresses every later reading. wheelhouse.log ends up with
    no [load-diag-capture] line at all -- on distil_medium_en, the one
    provider whose deliberate configuration this line exists to explain, and
    the one the procedure document now tells operators to read it on.

    The reading these tests take is the direct _read_capture_stats() call the
    rest of this file uses. That the two per-chunk paths reach it is pinned
    by TestTheUnreadableCaptureReasonIsVisible; what is under test here is
    when the line is said, not which caller says it.
    """

    PREFIX = '[load-diag-capture]'

    @staticmethod
    def _recorder():
        """A real handler plus the messages it receives.

        caplog is deliberately not used. caplog installs its handler before
        the block runs, which is the very ordering this test exists to
        refuse: the defect is only visible to a handler attached AFTER the
        processor was constructed, which is what both providers do.
        """
        messages = []

        class _Recorder(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        handler = _Recorder()
        handler.setLevel(logging.INFO)
        return handler, messages

    def _lines(self, messages):
        return [m for m in messages if m.startswith(self.PREFIX)]

    def test_the_line_reaches_a_handler_attached_after_construction(self):
        """Build with no reader first, attach the handler, then read.

        This is the production order. Before the fix the constructor said
        the line to stdout and closed the latch, so nothing reached the
        handler and this assertion found no lines at all.
        """
        processor, _ = _make_processor(capture_stats=None)

        handler, messages = self._recorder()
        restore = _attach_like_a_provider_start(handler)
        try:
            processor._read_capture_stats()
        finally:
            restore()

        lines = self._lines(messages)
        assert len(lines) == 1, lines
        assert 'no-reader=1' in lines[0], lines[0]

    def test_the_line_is_still_said_only_once_once_the_handler_is_there(
            self):
        """The fix moves the report; it does not un-bound it.

        wh-capture-load-gaps.1.1 is the reason the line is said once per
        process. A fix that made it reach wheelhouse.log by saying it again
        every window would reintroduce that flood, so the count is pinned
        here in the same place as the delivery.
        """
        from shared_stt import audio_processor as ap

        clock = _Clock()
        processor, _ = _make_processor(capture_stats=None)

        handler, messages = self._recorder()
        restore = _attach_like_a_provider_start(handler)
        try:
            with patch.object(ap.time, 'monotonic', clock):
                for _ in range(200):
                    clock.advance(ap.CAPTURE_REASON_LOG_INTERVAL_S + 0.1)
                    processor._read_capture_stats()
        finally:
            restore()

        lines = self._lines(messages)
        assert len(lines) == 1, lines
        assert 'no-reader=1' in lines[0], lines[0]


class TestTheMissingReaderLineWaitsForAReachableWheelhouse:
    """Attached is not the same question as able to deliver.

    Found by codex as wh-capture-load-gaps.2.2, one layer out from
    wh-capture-load-gaps.2.1. That earlier fix held the one no-reader
    report until the provider had attached its WebSocketLogHandler. But
    both providers attach that handler and then keep going: start() calls
    forwarder.start(), which only starts a thread, and neither provider
    waits for the WebSocket to come up before starting capture and
    entering its audio loop. WSForwarder.is_connected stays False until
    that thread finishes websockets.connect().

    So the first capture reading after start() can arrive while the
    connection is still down, and WebSocketLogHandler._drop_while_unreachable
    counts that record and returns without forwarding it. The latch closed
    on a line nobody received, and every later reading returns at the
    closed latch, so wheelhouse.log ends with no [load-diag-capture] line
    at all -- the same end state wh-capture-load-gaps.2.1 was fixed to
    prevent, reached by a different route.

    A slow start is enough on its own, and heavy CPU load is exactly the
    condition this whole bead is about, so the window is wider than it
    looks.

    These tests use the real WebSocketLogHandler rather than a recording
    handler of their own. That is the point: the round-1 tests attach a
    plain in-process handler, which proves attachment ordering and cannot
    see the handler's disconnected-drop policy at all.
    """

    PREFIX = '[load-diag-capture]'

    class _Forwarder:
        """The parts of WSForwarder that WebSocketLogHandler touches.

        A Mock would answer True for is_connected on attribute access,
        which is the answer under test, so the fake is written out.
        """

        def __init__(self, connected: bool):
            self.is_connected = connected
            self._current_trace_id = ''
            self.sent = []

        def send_log(self, level, message, source, timestamp, trace_id):
            self.sent.append(message)

    def _forwarded_lines(self, forwarder):
        return [m for m in forwarder.sent if m.startswith(self.PREFIX)]

    def _processor_and_handler(self, forwarder):
        """A processor with no capture reader, forwarding through handler."""
        from shared_stt.ws_forwarder import WebSocketLogHandler

        processor, _ = _make_processor(capture_stats=None)
        processor.forwarder = forwarder
        handler = WebSocketLogHandler(forwarder, source='test-provider')
        handler.setLevel(logging.INFO)
        return processor, handler

    def test_a_reading_taken_before_the_connection_says_nothing(self):
        """Disconnected: no line is forwarded and none is lost to the latch.

        Before the fix the reading was reported anyway. The handler dropped
        it, the latch closed, and the second reading below found nothing
        left to say.
        """
        forwarder = self._Forwarder(connected=False)
        processor, handler = self._processor_and_handler(forwarder)

        restore = _attach_like_a_provider_start(handler)
        try:
            processor._read_capture_stats()
            assert self._forwarded_lines(forwarder) == []

            forwarder.is_connected = True
            processor._read_capture_stats()
        finally:
            restore()

        lines = self._forwarded_lines(forwarder)
        assert len(lines) == 1, lines
        assert 'no-reader=1' in lines[0], lines[0]

    def test_the_deferred_readings_are_not_counted_into_the_line(self):
        """A held reading counts nothing, exactly as the constructor one does.

        The delivered line must read no-reader=1 however many readings were
        held back, because the count is a pointer to a single condition,
        not a tally of how long the WebSocket took to come up.
        """
        from shared_stt import audio_processor as ap

        clock = _Clock()
        forwarder = self._Forwarder(connected=False)
        processor, handler = self._processor_and_handler(forwarder)

        restore = _attach_like_a_provider_start(handler)
        try:
            with patch.object(ap.time, 'monotonic', clock):
                for _ in range(50):
                    clock.advance(ap.CAPTURE_REASON_LOG_INTERVAL_S + 0.1)
                    processor._read_capture_stats()
                assert self._forwarded_lines(forwarder) == []

                forwarder.is_connected = True
                clock.advance(ap.CAPTURE_REASON_LOG_INTERVAL_S + 0.1)
                processor._read_capture_stats()
        finally:
            restore()

        lines = self._forwarded_lines(forwarder)
        assert len(lines) == 1, lines
        assert 'no-reader=1' in lines[0], lines[0]

    def test_the_line_is_still_said_only_once_after_the_connection(self):
        """Holding the line does not un-bound it once delivery is possible.

        wh-capture-load-gaps.1.1 is why this line is said once per process.
        A fix that waited for the connection and then said it every window
        would trade one defect for that flood.
        """
        from shared_stt import audio_processor as ap

        clock = _Clock()
        forwarder = self._Forwarder(connected=True)
        processor, handler = self._processor_and_handler(forwarder)

        restore = _attach_like_a_provider_start(handler)
        try:
            with patch.object(ap.time, 'monotonic', clock):
                for _ in range(200):
                    clock.advance(ap.CAPTURE_REASON_LOG_INTERVAL_S + 0.1)
                    processor._read_capture_stats()
        finally:
            restore()

        lines = self._forwarded_lines(forwarder)
        assert len(lines) == 1, lines
        assert 'no-reader=1' in lines[0], lines[0]

    def test_a_forwarder_that_cannot_answer_is_treated_as_reachable(self):
        """Unsure means say it. Withholding forever is the worse failure.

        WebSocketLogHandler._wheelhouse_is_reachable already reads a
        forwarder with no is_connected accessor as reachable, for the same
        reason: these handlers exist to remove silence. The processor-side
        check must not disagree with the handler it writes through, or a
        line the handler would forward is never written at all.
        """
        from shared_stt.ws_forwarder import WebSocketLogHandler

        class _OlderForwarder:
            def __init__(self):
                self._current_trace_id = ''
                self.sent = []

            def send_log(self, level, message, source, timestamp, trace_id):
                self.sent.append(message)

        forwarder = _OlderForwarder()
        processor, _ = _make_processor(capture_stats=None)
        processor.forwarder = forwarder
        handler = WebSocketLogHandler(forwarder, source='test-provider')
        handler.setLevel(logging.INFO)

        restore = _attach_like_a_provider_start(handler)
        try:
            processor._read_capture_stats()
        finally:
            restore()

        lines = self._forwarded_lines(forwarder)
        assert len(lines) == 1, lines
        assert 'no-reader=1' in lines[0], lines[0]


    def test_nothing_is_written_at_all_while_wheelhouse_cannot_be_reached(
            self):
        """The preflight decides whether the line is WRITTEN, and that job
        is its own.

        f3d49a46 made a dropped record leave the latch open, which is a
        second defence against the same lost report. That fix masks the
        older catchers for this behaviour: with the preflight removed the
        line is written, the handler drops it, the latch stays open, the
        counts are cleared, and a later reading delivers exactly one
        no-reader=1 line -- so a test reading the forwarder sees the right
        answer whether the preflight is there or not.

        What the preflight still owns alone is that no record is written at
        all. A written record reaches every other handler on the logger,
        and it makes WebSocketLogHandler count a disconnected drop and run
        its drop callback, for a line nobody will read. This test watches
        the logger rather than the forwarder, which is the one observation
        the receipt layer cannot cover.
        """
        forwarder = self._Forwarder(connected=False)
        processor, handler = self._processor_and_handler(forwarder)

        written = []

        class _Watcher(logging.Handler):
            def emit(self, record):
                written.append(record.getMessage())

        watcher = _Watcher()
        watcher.setLevel(logging.INFO)
        parent = logging.getLogger('shared_stt.audio_processor')

        restore = _attach_like_a_provider_start(handler)
        parent.addHandler(watcher)
        try:
            processor._read_capture_stats()
        finally:
            parent.removeHandler(watcher)
            restore()

        assert self._forwarded_lines(forwarder) == []
        seen = [m for m in written if m.startswith(self.PREFIX)]
        assert seen == [], seen


class TestTheMissingReaderLatchWaitsForTheHandoverItself:
    """The latch is spent by the handover, not by a look at the forwarder.

    Found by codex as wh-capture-load-gaps.2.3, one layer out from
    wh-capture-load-gaps.2.2. That fix asked
    wheelhouse_can_receive_logs(self.forwarder) on the audio thread and
    spent the one-shot latch on the answer. WebSocketLogHandler asks the
    same shared question again inside emit, and between the two reads
    WSForwarder._sender_loop -- a different thread -- can write
    self._ws_connected = False on an established connection that closes
    (ws_forwarder.py:461 and :482). Neither read nor the flag is
    synchronised.

    The record is then dropped by _drop_while_unreachable while the latch
    is already closed, so every later reading returns at the closed latch
    and wheelhouse.log ends with no [load-diag-capture] line at all. An
    ordinary WebSocket close during a live provider is enough.

    The fix makes the handler report the handover back. WebSocketLogHandler
    ._send_record already answers that exact question -- it returns True
    only when the record was handed to the forwarder -- so the processor
    leaves a receipt on the record and commits the latch only when the
    receipt comes back stamped. There is no new observation to disagree
    with; the operation that decides the outcome is the one that reports
    it.

    This works because logging dispatches handlers synchronously on the
    calling thread: Logger.info -> Logger.handle -> callHandlers ->
    Handler.emit all run before info() returns. No STT provider installs a
    QueueHandler, which would move emit to another thread and break that
    (grep -rn "QueueHandler" services/ --include=*.py matches only under
    services/wheelhouse/, the WheelHouse app process, never under
    services/stt_providers/).
    """

    PREFIX = '[load-diag-capture]'

    class _RacingForwarder:
        """A forwarder whose answer can change between two reads.

        answers is consumed one read at a time, then default answers every
        read after it. That is what lets one reading see True at the
        processor's check and False at the handler's check, which is the
        race under test and which no stable fake can produce.
        """

        def __init__(self, answers, default):
            self.answers = list(answers)
            self.default = default
            self._current_trace_id = ''
            self.sent = []

        @property
        def is_connected(self):
            if self.answers:
                return self.answers.pop(0)
            return self.default

        def send_log(self, level, message, source, timestamp, trace_id):
            self.sent.append(message)

    def _forwarded_lines(self, forwarder):
        return [m for m in forwarder.sent if m.startswith(self.PREFIX)]

    def _processor_and_handler(self, forwarder):
        from shared_stt.ws_forwarder import WebSocketLogHandler

        processor, _ = _make_processor(capture_stats=None)
        processor.forwarder = forwarder
        handler = WebSocketLogHandler(forwarder, source='test-provider')
        handler.setLevel(logging.INFO)
        return processor, handler

    def test_a_connection_lost_after_the_check_does_not_spend_the_line(self):
        """True at the processor's read, False at the handler's: not spent.

        Before the fix this reading spent the latch on a record the handler
        dropped, and the second reading below found nothing left to say, so
        the assertion saw an empty list.
        """
        forwarder = self._RacingForwarder(answers=[True, False],
                                          default=True)
        processor, handler = self._processor_and_handler(forwarder)

        restore = _attach_like_a_provider_start(handler)
        try:
            processor._read_capture_stats()
            assert self._forwarded_lines(forwarder) == []

            processor._read_capture_stats()
        finally:
            restore()

        lines = self._forwarded_lines(forwarder)
        assert len(lines) == 1, lines
        assert 'no-reader=1' in lines[0], lines[0]

    def test_a_later_outage_does_not_reopen_the_delivered_line(self):
        """Once the handover happened, the latch stays shut for good.

        The guard against overcorrecting: a fix that reopened the latch on
        every drop would say the line again on the next outage, which is
        the flood wh-capture-load-gaps.1.1 removed.
        """
        from shared_stt import audio_processor as ap

        clock = _Clock()
        forwarder = self._RacingForwarder(answers=[True, False],
                                          default=True)
        processor, handler = self._processor_and_handler(forwarder)

        restore = _attach_like_a_provider_start(handler)
        try:
            with patch.object(ap.time, 'monotonic', clock):
                clock.advance(ap.CAPTURE_REASON_LOG_INTERVAL_S + 0.1)
                processor._read_capture_stats()
                clock.advance(ap.CAPTURE_REASON_LOG_INTERVAL_S + 0.1)
                processor._read_capture_stats()
                assert len(self._forwarded_lines(forwarder)) == 1

                forwarder.default = False
                for _ in range(50):
                    clock.advance(ap.CAPTURE_REASON_LOG_INTERVAL_S + 0.1)
                    processor._read_capture_stats()
                forwarder.default = True
                for _ in range(50):
                    clock.advance(ap.CAPTURE_REASON_LOG_INTERVAL_S + 0.1)
                    processor._read_capture_stats()
        finally:
            restore()

        lines = self._forwarded_lines(forwarder)
        assert len(lines) == 1, lines

    def test_a_send_that_raises_leaves_the_line_to_be_said_again(self):
        """_send_record returning False is a drop like any other.

        The handler is reachable and does not drop the record, so the
        preflight cannot see this one at all: send_log raises, the handler
        calls handleError, and the record reached nobody. The receipt is
        what makes that case behave like the disconnected one.
        """
        forwarder = self._RacingForwarder(answers=[], default=True)
        processor, handler = self._processor_and_handler(forwarder)

        failures = {'left': 1}
        delivered = []

        def send_log(level, message, source, timestamp, trace_id):
            if failures['left']:
                failures['left'] -= 1
                raise RuntimeError('socket write failed')
            delivered.append(message)

        forwarder.send_log = send_log

        restore = _attach_like_a_provider_start(handler)
        try:
            with patch.object(handler, 'handleError', lambda record: None):
                processor._read_capture_stats()
                assert delivered == []

                processor._read_capture_stats()
        finally:
            restore()

        lines = [m for m in delivered if m.startswith(self.PREFIX)]
        assert len(lines) == 1, lines
        assert 'no-reader=1' in lines[0], lines[0]

    def test_a_suppressed_record_is_marked_dropped_too(self):
        """Every way a handler ends without handing the record over counts.

        The rate limit is the third one, after the unreachable drop and the
        failed send. It cannot reach the no-reader line today, because that
        line is on shared_stt.audio_processor.loadmetrics while this
        handler carries the shared_audio tree -- but the receipt's contract
        is that an unmarked receipt means no handler reported a failure,
        and a suppression that left no mark would break that contract for
        the next caller to ask for one.
        """
        from shared_stt.ws_forwarder import (
            DELIVERY_RECEIPT_ATTR,
            RateLimitedWebSocketLogHandler,
            new_delivery_receipt,
        )

        forwarder = self._RacingForwarder(answers=[], default=True)
        handler = RateLimitedWebSocketLogHandler(
            forwarder, source='test-provider', max_records_per_window=1)
        handler.setLevel(logging.INFO)

        receipts = []
        for _ in range(2):
            receipt = new_delivery_receipt()
            receipts.append(receipt)
            record = logging.LogRecord(
                name='shared_audio.capture.winrt_capture', level=logging.INFO,
                pathname=__file__, lineno=1, msg='frames dropped',
                args=(), exc_info=None)
            setattr(record, DELIVERY_RECEIPT_ATTR, receipt)
            handler.handle(record)

        assert receipts[0]['dropped'] is False, receipts
        assert receipts[1]['dropped'] is True, receipts


class TestTheUnreadableWindowNamesItsCause:
    """CaptureLoadReporter._read says WHICH of its three None cases fired.

    This is the reader behind the "[load-diag] window=" lines and the
    capture-outage event, so the 13-of-83 missing windows of the
    2026-08-30 loaded run were produced here. Before this, the missing
    reader and the non-dict answer were silent and the exception logged at
    debug on this module's logger, so a window could say
    capture=unavailable and nothing could say why
    (wh-capture-load-gaps criterion 1b).

    The line is RETURNED from record_iteration, like every other line this
    class produces, rather than logged here. The Parakeet consumer loop
    logs what it returns (main.py, process_audio_loop), which is the route
    the window lines already take to wheelhouse.log -- so the reason
    arrives wherever the window line it explains arrives.

    The rate bound is the window boundary, and it is needed: that loop
    polls with a 20 ms timeout, so _read is asked 50 or more times a
    second. No new timer was added for it; the counts are reported when
    the window closes, next to the line they explain.
    """

    @staticmethod
    def _reason_lines(lines):
        return [ln for ln in lines if ln.startswith('[load-diag-capture]')]

    def _close_one_window(self, stats_source, reads_before_close=3):
        """Open a window, take some readings, then close it.

        The first record_iteration opens the window and says nothing about
        a window that has not happened yet, so the count the closing line
        carries includes that opening reading.
        """
        clock = _Clock()
        reporter = _reporter(stats_source, clock, interval_s=10.0)
        lines = []
        for _ in range(reads_before_close):
            clock.advance(0.1)
            lines.extend(reporter.record_iteration())
        clock.advance(10.1)
        lines.extend(reporter.record_iteration())
        return lines

    def test_a_window_with_no_reader_names_it(self):
        """The provider never passed a capture_stats callable.

        The count is 1, not one per reading: self._capture_stats is
        assigned once and never rebound, so a missing reader is a
        permanent fact reported once per reporter, not a measurement that
        can fail again (wh-capture-load-gaps.1.1).
        """
        lines = self._close_one_window(None)
        reasons = self._reason_lines(lines)
        assert len(reasons) == 1, lines
        assert 'no-reader=1' in reasons[0], reasons[0]

    def test_a_window_whose_reader_raised_names_it(self):
        """The reader raised. The error text rides along, because which
        exception it is decides where to look next."""
        def boom():
            raise RuntimeError('device gone')

        reasons = self._reason_lines(self._close_one_window(boom))
        assert len(reasons) == 1, reasons
        assert 'reader-raised=4' in reasons[0], reasons[0]
        assert 'device gone' in reasons[0], reasons[0]

    def test_a_window_given_a_non_dict_names_it(self):
        """The reader answered with something that is not a dict."""
        reasons = self._reason_lines(
            self._close_one_window(lambda: ['not', 'a', 'dict']))
        assert len(reasons) == 1, reasons
        assert 'non-dict=4' in reasons[0], reasons[0]

    def test_a_reader_that_works_says_nothing(self):
        """No line when the readings arrive. The band is what matters."""
        reasons = self._reason_lines(
            self._close_one_window(lambda: {'qsize': 1}))
        assert reasons == []

    def test_one_line_a_window_however_many_readings_failed(self):
        """The consumer loop polls with a 20 ms timeout, so _read is asked
        50 or more times a second. One line per failure would bury the very
        band it exists to explain.

        A raising reader, because a missing reader is reported once per
        reporter now (wh-capture-load-gaps.1.1) and would give one line
        whether the window bound worked or not.
        """
        def boom():
            raise RuntimeError('device gone')

        reasons = self._reason_lines(
            self._close_one_window(boom, reads_before_close=40))
        assert len(reasons) == 1, reasons
        assert 'reader-raised=41' in reasons[0], reasons[0]

    def test_a_missing_reader_names_itself_in_one_window_only(self):
        """The twin of wh-capture-load-gaps.1.1 in this class. No shipped
        provider builds a reporter without a reader today -- Parakeet's
        main.py always passes capture_stats=self.audio_capture.get_stats
        and distil builds no reporter at all -- but the field is assigned
        once here too, so the shape that floods the log is the same one.
        Fixed together so the two readers cannot drift apart.
        """
        clock = _Clock()
        reporter = _reporter(None, clock, interval_s=10.0)
        lines = []
        for _ in range(6):
            clock.advance(10.1)
            lines.extend(reporter.record_iteration())
        assert len(self._reason_lines(lines)) == 1, lines

    def test_the_reason_speaks_where_the_window_line_cannot(self):
        """A window whose readings all failed produces no window line at
        all -- it has no numbers to report. That silence is the 13-of-83
        gap this bead is about, and the reason line is what now fills it.

        It must not wear the window prefix: _summary() asserts a run
        carries exactly one '[load-diag] window=' line, and
        tools/stt_load_test/logparse.py matches that literal.
        """
        lines = self._close_one_window(None)
        assert [ln for ln in lines
                if ln.startswith('[load-diag] window=')] == [], lines
        assert len(self._reason_lines(lines)) == 1, lines

    def test_each_window_counts_only_its_own_readings(self):
        """The counts are cleared when a line is written, so the second
        window does not reprint the first window's failures."""
        def boom():
            raise RuntimeError('device gone')

        clock = _Clock()
        reporter = _reporter(boom, clock, interval_s=10.0)
        first = []
        for _ in range(3):
            clock.advance(0.1)
            first.extend(reporter.record_iteration())
        clock.advance(10.1)
        first.extend(reporter.record_iteration())
        second = []
        clock.advance(10.1)
        second.extend(reporter.record_iteration())
        # A raising reader, because a missing reader is reported once per
        # reporter now (wh-capture-load-gaps.1.1), so the second window
        # would carry no line at all and the per-window claim would go
        # untested.
        assert 'reader-raised=4' in self._reason_lines(first)[0]
        assert 'reader-raised=1' in self._reason_lines(second)[0]

    def test_two_reasons_in_one_window_are_both_named(self):
        """A window that mixes two causes cannot be read as one cause."""
        answers = [None, ['not a dict'], ['not a dict']]

        def reader():
            # The later readings repeat the non-dict answer on purpose: an
            # exhausted list would raise IndexError and land in the
            # reader-raised arm by accident, testing something else.
            value = answers.pop(0) if answers else ['not a dict']
            if value is None:
                raise RuntimeError('device gone')
            return value

        reasons = self._reason_lines(self._close_one_window(reader))
        assert len(reasons) == 1, reasons
        assert 'reader-raised=1' in reasons[0], reasons[0]
        assert 'non-dict=3' in reasons[0], reasons[0]


class TestTheReadingTheReporterTook:
    """wh-stt-load-metrics.4 G1a.

    The Google consumer loop reports the queue in two places -- the
    reporter's periodic window line and its own per-utterance load line
    -- and reading the queue twice would give them two different depths
    for one iteration. The two would disagree most under exactly the
    load the lines exist to measure, since that is when the queue moves
    fastest between the reads. So the reporter publishes the reading it
    took and the loop hands that same number to the utterance line.
    """

    def test_a_reporter_that_has_read_nothing_says_zero(self):
        clock = _Clock()
        reporter = _reporter(lambda: None, clock)

        assert reporter.last_queue_depth == 0

    def test_the_published_depth_is_the_one_the_tracker_was_given(self):
        clock = _Clock()
        reporter = _reporter(lambda: {'qsize': 37}, clock)

        reporter.record_iteration()

        assert reporter.last_queue_depth == 37

    def test_an_unreadable_reading_keeps_the_last_depth(self):
        """The reader failing costs its own numbers and nothing else.
        The tracker is given the same stale depth, so the two still
        agree about the iteration -- which is the property this exists
        for."""
        clock = _Clock()
        readings = [{'qsize': 12}, None]
        reporter = _reporter(lambda: readings.pop(0), clock)

        reporter.record_iteration()
        reporter.record_iteration()

        assert reporter.last_queue_depth == 12
