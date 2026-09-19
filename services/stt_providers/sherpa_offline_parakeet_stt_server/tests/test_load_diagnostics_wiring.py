"""The Parakeet server wires the load metrics to the live capture provider.

wh-stt-load-metrics. The metrics themselves and their formatting are covered
in services/stt_providers/shared/tests/test_capture_load_metrics.py. What is
covered here is only the wiring, which is where this measurement can silently
produce nothing: AudioProcessor reports "n/a" for every capture number when no
reader is supplied, so an unwired server logs a [load-diag] line that looks
complete and answers nothing.

Two things are wired:

  1. AudioProcessor gets a reader bound to this server's own capture provider,
     for the per-utterance line.
  2. The consumer loop drives CaptureLoadReporter for the periodic window line,
     which is what shows a struggling machine between utterances and reports
     loop stalls. That one is gated behind [debug] log_load_diagnostics, the
     same flag shape the google provider uses for its [overflow-diag] block.
"""
from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, Mock, patch

import pytest

import main as parakeet_main


STATS = {'captured': 10, 'drops': 1, 'qsize': 2, 'max_q': 3,
         'status_flags': 4, 'overflow_count': 5}


@pytest.fixture
def server_factory():
    """Builds a ParakeetServer with the model, socket, and microphone stubbed.

    The engine is stubbed because loading Parakeet needs the model files, the
    forwarder because it binds a websocket, and the capture provider because
    it opens the real microphone.
    """
    def build(**kwargs):
        capture = MagicMock()
        capture.get_stats = Mock(return_value=dict(STATS))
        with patch.object(parakeet_main, 'SherpaOfflineEngine'), \
             patch.object(parakeet_main, 'WSForwarder'), \
             patch.object(parakeet_main, 'get_audio_provider',
                          return_value=capture):
            server = parakeet_main.ParakeetServer(
                model_config={'model_path': 'C:/unused', 'use_gpu': False},
                engine_config={},
                **kwargs,
            )
        return server, capture
    return build


class TestTheProcessorReadsThisServersCaptureProvider:
    """Criterion 1: the per-utterance capture numbers must be real numbers."""

    def test_the_processor_is_given_a_capture_stats_reader(self, server_factory):
        server, _ = server_factory()
        assert server.audio_processor._capture_stats is not None

    def test_the_reader_returns_this_servers_own_capture_stats(
            self, server_factory):
        """Bound to this server's provider, not merely to some callable: a
        reader pointed at a different provider would report another
        microphone's drops under this one's utterances."""
        server, capture = server_factory()
        assert server.audio_processor._capture_stats() == STATS
        capture.get_stats.assert_called()


class TestThePeriodicWindowLineIsGated:
    """The periodic line follows the google provider's [debug] flag shape.

    Ungated it would be one line every ten seconds forever. The per-utterance
    line is not gated -- it sits beside [vad_utt_stats], one line per utterance.
    """

    def test_no_reporter_without_the_flag(self, server_factory):
        server, _ = server_factory()
        assert server._load_reporter is None

    def test_the_flag_creates_a_reporter(self, server_factory):
        server, _ = server_factory(log_load_diagnostics=True)
        assert server._load_reporter is not None

    def test_the_reporter_reads_this_servers_capture_provider(
            self, server_factory):
        server, capture = server_factory(log_load_diagnostics=True)
        server._load_reporter.record_iteration()
        capture.get_stats.assert_called()


def _run_loop_once(server):
    """Run exactly one iteration of process_audio_loop and return."""
    def read(timeout=0.02):
        server.running = False
        return None
    server.audio_capture.read = Mock(side_effect=read)
    server.running = True
    server.process_audio_loop()


class TestTheLoopLogsWhatTheReporterReturns:
    """The loop must record every iteration and log the lines it gets back.

    A reporter that is constructed but never driven is the same failure as no
    reporter at all, and it is invisible: the object exists and the log is
    empty.
    """

    def test_the_loop_records_an_iteration(self, server_factory):
        server, _ = server_factory(log_load_diagnostics=True)
        server._load_reporter = Mock()
        server._load_reporter.record_iteration = Mock(return_value=[])
        _run_loop_once(server)
        server._load_reporter.record_iteration.assert_called()

    def test_the_loop_logs_the_returned_lines(self, server_factory, caplog):
        server, _ = server_factory(log_load_diagnostics=True)
        server._load_reporter = Mock()
        server._load_reporter.record_iteration = Mock(
            return_value=['[load-diag] window=10s q_now=2', '[stall] slow'])
        with caplog.at_level(logging.INFO, logger="ParakeetTDT"):
            _run_loop_once(server)
        messages = [r.getMessage() for r in caplog.records]
        assert '[load-diag] window=10s q_now=2' in messages
        assert '[stall] slow' in messages

    def test_an_iteration_is_recorded_even_when_no_audio_arrives(
            self, server_factory):
        """read() returning None is exactly the starved case this measures, so
        the iteration must be counted before the read, not after a chunk
        arrives. Counting only chunk-bearing iterations would make a loop that
        is being starved look like a loop that is idle."""
        server, _ = server_factory(log_load_diagnostics=True)
        server._load_reporter = Mock()
        server._load_reporter.record_iteration = Mock(return_value=[])
        _run_loop_once(server)          # the stub read() returns None
        server._load_reporter.record_iteration.assert_called_once()

    def test_the_loop_runs_normally_with_no_reporter(self, server_factory):
        server, _ = server_factory()
        _run_loop_once(server)
        assert server._load_reporter is None


# Read from the implementation rather than repeated here, so a rename breaks
# the test instead of quietly leaving it testing a logger nobody logs to.
LOAD_METRICS_LOGGER = parakeet_main.LOAD_METRICS_LOGGER_NAME
CAPTURE_LOGGER = parakeet_main.CAPTURE_LOGGER_NAME


@pytest.fixture
def restore_logging():
    """Undo start()'s edits to the global logger tree.

    start() adds handlers and clears propagate flags on module-level loggers
    and never undoes either, so without this a test that calls it leaks its
    handler into every later test in the session.
    """
    names = ("ParakeetTDT", "shared_stt", "shared_stt.audio_processor",
             "shared_stt.ws_forwarder", LOAD_METRICS_LOGGER,
             CAPTURE_LOGGER, "shared_audio.microphone",
             "shared_audio.overflow_monitor", "")
    before = {n: (list(logging.getLogger(n).handlers),
                  logging.getLogger(n).propagate,
                  logging.getLogger(n).level) for n in names}
    yield
    for name, (handlers, propagate, level) in before.items():
        lg = logging.getLogger(name)
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.setLevel(level)


def _emit_at_info(name, message):
    """Log one INFO record, with the level check taken out of the question.

    main.py calls logging.basicConfig(level=INFO) at import, but under pytest
    the root logger already has the plugin's handlers, so basicConfig returns
    without doing anything and the root level stays at WARNING. Both loggers
    here inherit that, so an un-levelled logger.info() is dropped before any
    handler is consulted -- which made the "stays silent" test below pass for
    entirely the wrong reason. Setting the level explicitly leaves the handler
    attachment as the only thing these tests can be measuring.
    """
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.info(message)


def _started(server):
    """Run start() through one loop iteration and its cleanup."""
    def read(timeout=0.02):
        server.running = False
        return None
    server.audio_capture.read = Mock(side_effect=read)
    with patch.object(parakeet_main.signal, 'signal'):
        server.start()


def _forwarded(server):
    """Every message the websocket log handler sent during the test."""
    return [c.kwargs.get('message', '')
            for c in server.forwarder.send_log.call_args_list]


class TestTheLoadMetricsLoggerIsForwarded:
    """The per-utterance line needs its own logger to reach wheelhouse.log.

    start() attaches the WebSocketLogHandler to the provider's own logger and
    sets logging.getLogger("shared_stt").propagate = False with no handler on
    it, so a record from shared_stt.audio_processor -- where the [load-diag]
    line is produced -- is discarded outright. Measured, not assumed: grep -c
    "vad_utt_stats" over wheelhouse.log and all five rotated files returned 0
    in every one, while the provider's own lines were present.

    The fix attaches the same handler to one named child logger. A record
    fires its own logger's handlers before propagation reaches the silenced
    parent, so only this one line becomes visible and nothing else changes.
    """

    def test_the_handler_is_attached_to_the_load_metrics_logger(
            self, server_factory, restore_logging):
        server, _ = server_factory()
        _started(server)
        assert server._ws_log_handler in logging.getLogger(
            LOAD_METRICS_LOGGER).handlers

    def test_a_load_diag_record_reaches_the_forwarder(
            self, server_factory, restore_logging):
        """The attachment is the point, so assert the record arrives rather
        than that a handler is in a list."""
        server, _ = server_factory()
        _started(server)
        _emit_at_info(LOAD_METRICS_LOGGER, '[load-diag] utt=7 kind=x')
        assert any('[load-diag] utt=7 kind=x' in m for m in _forwarded(server))

    def test_the_ordinary_processor_logger_stays_silent(
            self, server_factory, restore_logging):
        """This is what makes the fix narrow: [vad_utt_stats] and every other
        shared_stt record keep exactly the visibility they have today. A
        change that forwarded the whole package would pass the two tests above
        and fail this one."""
        server, _ = server_factory()
        _started(server)
        _emit_at_info('shared_stt.audio_processor', '[vad_utt_stats] utt=7')
        assert not any('vad_utt_stats' in m for m in _forwarded(server))


class TestTheDebugFlagIsReadFromConfig:
    """Nothing else notices if the flag's key does not match the config.

    A misspelling on either side leaves log_load_diagnostics permanently
    false: the periodic [load-diag] line never appears, no test fails, and a
    load test run against it silently measures nothing. These bind the key
    that main() reads to the key the tracked config.toml declares.
    """

    def test_the_flag_is_read_from_the_debug_section(self):
        assert parakeet_main._load_diagnostics_enabled(
            {"debug": {"log_load_diagnostics": True}}) is True

    def test_the_flag_defaults_to_false(self):
        assert parakeet_main._load_diagnostics_enabled({}) is False

    def test_a_debug_section_without_the_key_is_false(self):
        assert parakeet_main._load_diagnostics_enabled({"debug": {}}) is False

    def test_the_tracked_config_declares_the_key_main_reads(self):
        """The end-to-end binding: turning the flag on in the shipped config
        must reach the code that reads it."""
        import tomllib
        from pathlib import Path

        config_path = Path(parakeet_main.__file__).resolve().parent / "config.toml"
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        assert parakeet_main._load_diagnostics_enabled(config) is False, (
            "the tracked config must ship with the periodic line off")
        config["debug"]["log_load_diagnostics"] = True
        assert parakeet_main._load_diagnostics_enabled(config) is True


class TestTheCaptureBaselineIsTakenBeforeTheMicrophoneOpens:
    """wh-stt-load-metrics.1.10. The baseline must be older than the first
    frame the capture callback can count.

    AudioProcessor is built in __init__, when the capture provider has no
    stream. The provider reported four counters instead of six there, so
    the construction reading withholds overflow and status_flags (.1.9);
    since wh-portaudio-capture-removal the only capture provider reports
    those same four keys at every point in its life. Taking the reading
    after
    audio_capture.start() returns is worse: the callback is already filling
    the queue, the queue is not discarded, and under CPU starvation an
    arbitrary backlog can be captured before the reading -- so the first
    utterance reports overflow=0 for audio that already lost frames (.1.10).

    start() therefore establishes an all-zero baseline BEFORE it opens the
    microphone, which is provably correct there: WinRTAudioCapture.__init__
    sets _frames_captured, _drops and _max_queue_depth to zero
    (shared_audio/capture/winrt_capture.py) and starts no capture thread,
    so get_stats() answers four zeros until start() runs. The deleted
    PortAudio adapter reached the same all-zero state by building its
    MicrophoneStream inside start() (wh-portaudio-capture-removal).
    """

    def test_start_seeds_the_capture_baseline(self, server_factory,
                                              restore_logging):
        server, _ = server_factory()
        server.audio_processor.seed_capture_baseline_before_capture_starts = \
            Mock()
        _started(server)
        (server.audio_processor.seed_capture_baseline_before_capture_starts
         .assert_called_once())

    def test_the_baseline_is_taken_before_the_capture_provider_starts(
            self, server_factory, restore_logging):
        server, capture = server_factory()
        order = MagicMock()
        capture.start = order.capture_start
        server.audio_processor.seed_capture_baseline_before_capture_starts = \
            order.seed
        _started(server)
        names = [call[0] for call in order.mock_calls]
        assert 'capture_start' in names and 'seed' in names, names
        assert names.index('seed') < names.index('capture_start'), names


def _run_loop_once_with(server, chunk):
    """Run one whole iteration in which the provider yields *chunk*.

    The loop checks self.running again between the read and the work, so a
    stub that clears it during the first read would return before doing any
    of the work these tests measure. The second read ends the loop instead.
    """
    reads = []

    def read(timeout=0.02):
        reads.append(1)
        if len(reads) == 1:
            return chunk
        server.running = False
        return None
    server.audio_capture.read = Mock(side_effect=read)
    server.running = True
    server.process_audio_loop()


class TestTheStallFieldExcludesEveryKindOfLoopWork:
    """wh-stt-load-metrics.1.12, corrected by .1.14. The reporter measures the
    gap between the tops of two consumer-loop iterations, and everything the
    loop does sits inside that gap. Without the loop's own work time it cannot
    tell a busy loop from an unscheduled one, and it named the wrong one.

    The .1.12 fix subtracted the recognizer's seconds, which is every
    recognizer call but not every piece of work. The loop also runs the Silero
    VAD on each chunk, the AGC, a keep_warm decode during silence, a
    send_vad_start over the websocket, and -- in the idle branch, which never
    reaches AudioProcessor at all -- the wake-word model. Any of those can
    pass the one-second stall threshold on a contended machine, and each one
    was reported as "likely whole-machine CPU starvation".

    The loop now times its own two branches end to end. That is what makes the
    accounting complete rather than a list of sites: the whole of
    process_chunk is inside the measurement, the forced endpoint's finalize()
    included, so no future work added inside it can escape the subtraction.
    """

    def test_the_reporter_is_given_the_loops_own_work_reader(
            self, server_factory):
        server, _ = server_factory(log_load_diagnostics=True)
        reader = server._load_reporter._busy_seconds
        assert reader is not None, (
            "the reporter was built without a work-time reader, so every "
            "recognizer call over a second reads as CPU starvation")
        assert reader() == pytest.approx(server._load_work_s)

    def test_the_reader_follows_the_loop_rather_than_a_snapshot(
            self, server_factory):
        server, _ = server_factory(log_load_diagnostics=True)
        server._load_work_s += 2.5
        assert server._load_reporter._busy_seconds() == pytest.approx(2.5)

    def test_the_counter_only_ever_grows(self, server_factory):
        """The reporter subtracts a difference, so its reader must never go
        back. The per-utterance engine counters are zeroed for every new
        utterance; a reader over one of those would see the reset as negative
        work and, clamped at zero, charge the whole next gap to starvation."""
        server, _ = server_factory(log_load_diagnostics=True)
        server.transcription_enabled.set()
        server.audio_processor.process_chunk = Mock(
            side_effect=lambda chunk: time.sleep(0.02))
        readings = [server._load_reporter._busy_seconds()]
        for _ in range(3):
            _run_loop_once_with(server, b'\x00\x00' * 320)
            readings.append(server._load_reporter._busy_seconds())
        assert readings == sorted(readings), (
            "the work counter went backwards between iterations, which the "
            "reporter differences and clamps at zero -- the gap after the "
            "drop would be charged to starvation in full")
        assert readings[-1] >= 0.055, (
            "three 20ms bodies ran and the counter holds less than one of "
            "them, so it is being restarted rather than accumulated")

    def test_the_time_inside_process_chunk_is_counted_as_work(
            self, server_factory):
        """A real 50ms body, timed on the real clock: the same shape the
        forced endpoint has, which recognizes a whole utterance in one pass
        and is the single call most likely to pass a second."""
        server, _ = server_factory(log_load_diagnostics=True)
        server.transcription_enabled.set()
        server.audio_processor.process_chunk = Mock(
            side_effect=lambda chunk: time.sleep(0.05))
        _run_loop_once_with(server, b'\x00\x00' * 320)
        assert server._load_work_s >= 0.045, (
            "the 50ms the loop spent inside process_chunk is missing from "
            "the work total the stall tracker subtracts, so it reads as "
            "whole-machine CPU starvation")

    def test_the_time_inside_a_wake_word_frame_is_counted_as_work(
            self, server_factory):
        """The idle branch never touches AudioProcessor, so no recognizer
        counter can see it. openWakeWord runs its own model on every frame."""
        server, _ = server_factory(log_load_diagnostics=True)
        server.transcription_enabled.clear()
        server._wake_word_listening = True
        server._wake_word_detector = Mock()
        server._wake_word_detector.process = Mock(
            side_effect=lambda chunk: time.sleep(0.05) or None)
        _run_loop_once_with(server, b'\x00\x00' * 320)
        assert server._load_work_s >= 0.045, (
            "a 50ms wake-word inference is missing from the work total, so "
            "a slow one reads as whole-machine CPU starvation")

    def test_waiting_for_the_microphone_is_not_counted_as_work(
            self, server_factory):
        """The load-bearing exclusion. read() is where a starved loop sits,
        so its seconds are the stall signal itself. A loop that counted its
        own wait as work would report no stall however starved it was."""
        server, _ = server_factory(log_load_diagnostics=True)

        def read(timeout=0.02):
            server.running = False
            time.sleep(0.05)
            return None
        server.audio_capture.read = Mock(side_effect=read)
        server.running = True
        server.process_audio_loop()
        assert server._load_work_s == 0.0, (
            "the loop charged its wait for the microphone to its own work, "
            "which is the one gap the stall field exists to see")


class TestTheReporterCanTellWhetherCaptureIsRunning:
    """wh-stt-load-metrics.1.13. A capture provider whose device failed to
    open still answers get_stats() with real zeros, and this server never
    calls wait_ready(), so the periodic line reported a clean capture window
    for a microphone that never captured anything. The reporter has to ask.
    """

    def test_the_reporter_is_given_a_capture_readiness_reader(
            self, server_factory):
        server, _ = server_factory(log_load_diagnostics=True)
        assert server._load_reporter._capture_ready is not None, (
            "the reporter was built without a readiness reader, so a dead "
            "microphone reports drops=0 overflow=0 status_flags=0")

    def test_the_reader_asks_this_servers_own_capture_provider(
            self, server_factory):
        """Bound to this server's provider, not merely to some callable: a
        reader pointed elsewhere would clear this window using another
        microphone's readiness."""
        server, capture = server_factory(log_load_diagnostics=True)
        server._load_reporter._capture_ready()
        capture.wait_ready.assert_called()

    def test_the_reader_never_blocks_the_consumer_loop(self, server_factory):
        """It runs once per 20 ms iteration of the loop this bead measures.
        A blocking wait here would itself create the stalls being measured."""
        server, capture = server_factory(log_load_diagnostics=True)
        server._load_reporter._capture_ready()
        _, kwargs = capture.wait_ready.call_args
        assert kwargs.get('timeout') == 0.0

    def test_no_reader_is_built_when_the_periodic_line_is_off(
            self, server_factory):
        server, _ = server_factory(log_load_diagnostics=False)
        assert server._load_reporter is None


class TestTheCapturePathIsForwarded:
    """Criterion: the capture path must be readable off the provider console.

    Every module under shared_audio logs to logging.getLogger(__name__), and
    this provider attaches nothing to that tree, so the overflow warning, the
    device-open error, and OverflowMonitor's summary reach the provider
    console and nothing else. A load investigation reading wheelhouse.log
    cannot tell a capture drop from an inference stall
    (wh-stt-load-metrics.2). The google provider already forwards one of
    those loggers (google_stt_server/main.py:1197).

    Forwarding is rate-limited because the same failure that makes these
    records worth reading also makes them frequent: a microphone dropping
    frames writes several records a second.
    """

    def test_the_capture_tree_gets_its_own_handler(
            self, server_factory, restore_logging):
        """A SEPARATE instance, not the provider's own handler.

        _handle_set_log_level() raises the level of self._ws_log_handler, so
        one shared object would let a set_log_level("WARNING") command stop
        the capture forwarding as a side effect.
        """
        server, _ = server_factory()
        _started(server)
        handlers = logging.getLogger(CAPTURE_LOGGER).handlers
        assert handlers, 'nothing forwards the capture path'
        assert server._ws_log_handler not in handlers

    def test_a_capture_warning_reaches_the_forwarder(
            self, server_factory, restore_logging):
        server, _ = server_factory()
        _started(server)
        _emit_at_info('shared_audio.microphone',
                      '[mic] Overflow threshold exceeded: 9 in 30s')
        assert any('[mic] Overflow threshold exceeded' in m
                   for m in _forwarded(server))

    def test_the_overflow_summary_reaches_the_forwarder(
            self, server_factory, restore_logging):
        """The one record the load investigation is actually looking for."""
        server, _ = server_factory()
        _started(server)
        _emit_at_info('shared_audio.overflow_monitor',
                      '[overflow] 7 audio input overflows in the last 30s')
        assert any('[overflow] 7 audio input overflows' in m
                   for m in _forwarded(server))

    def test_a_storm_does_not_flood_the_log(
            self, server_factory, restore_logging):
        """A plain WebSocketLogHandler here would forward all forty.

        That is not a tidiness concern: these records share the WebSocket
        queue with the transcripts, and the machine is by definition already
        struggling when they appear.
        """
        server, _ = server_factory()
        _started(server)
        for i in range(40):
            _emit_at_info('shared_audio.microphone', f'[mic] storm {i}')
        forwarded = [m for m in _forwarded(server) if '[mic] storm' in m]
        assert 0 < len(forwarded) < 40, (
            'a forty-record storm produced %d forwarded lines'
            % len(forwarded))

    def test_lowering_the_forwarding_level_leaves_the_capture_path_alone(
            self, server_factory, restore_logging):
        """This is what the separate handler instance buys.

        _handle_set_log_level("WARNING") is a command WheelHouse can send at
        any time. With one shared handler object it would silently stop
        forwarding OverflowMonitor's INFO summary, and the capture evidence
        would disappear exactly when someone turned the provider's own
        chatter down to look for it.
        """
        server, _ = server_factory()
        _started(server)
        server._handle_set_log_level('WARNING')
        _emit_at_info('shared_audio.overflow_monitor',
                      '[overflow] 7 audio input overflows in the last 30s')
        assert any('[overflow] 7 audio input overflows' in m
                   for m in _forwarded(server))

    def test_the_handler_stays_off_the_root_logger(
            self, server_factory, restore_logging):
        """The attachment must name the capture tree, not a broad parent.

        WSForwarder logs each send it makes (ws_forwarder.py:381, through
        self._log). A root handler would forward those, and each forwarded
        record would produce another record to forward. Today the propagate
        line two statements above start()'s attachment happens to stop that
        particular loop on its own, which is exactly why this is a
        structural check and not an emit: an emit-based version of this test
        cannot fail, so it would report nothing about where the handler is.
        """
        server, _ = server_factory()
        _started(server)
        assert server._capture_log_handler in logging.getLogger(
            CAPTURE_LOGGER).handlers
        assert server._capture_log_handler not in logging.getLogger().handlers


class TestTheIdleWorkLinesFollowTheSameFlag:
    """wh-audit13-preengine-load-review.1: the ten-second silence and
    wake-word work lines follow [debug] log_load_diagnostics as well.

    AudioProcessor and WakeWordDetector default the setting to off, so a
    server that forgets to pass it logs nothing and looks correct until a
    load test finds no idle lines. The behaviour is covered in
    services/stt_providers/shared/tests/test_pre_engine_load.py.
    """

    def test_the_processor_is_off_without_the_flag(self, server_factory):
        server, _ = server_factory()
        assert server.audio_processor._log_load_diagnostics is False

    def test_the_flag_reaches_the_processor(self, server_factory):
        server, _ = server_factory(log_load_diagnostics=True)
        assert server.audio_processor._log_load_diagnostics is True

    def test_the_flag_reaches_the_wake_word_detector(self, server_factory):
        with patch('shared_stt.wake_word_detector.WakeWordDetector') as detector_cls:
            server_factory(log_load_diagnostics=True, wake_word_enabled=True)
        assert detector_cls.call_args.kwargs['log_load_diagnostics'] is True
