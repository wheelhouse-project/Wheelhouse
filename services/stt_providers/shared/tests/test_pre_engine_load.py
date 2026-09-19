"""Attribute slow non-recognizer work without changing engine_ratio.

The clock advances inside the model doubles, never by sleeping. Production
AudioProcessor/WakeWordDetector do all timing, windowing and log formatting.
"""
import logging
from unittest.mock import Mock

import pytest

from tests.test_capture_load_metrics import _make_processor, CHUNK_30MS, _field


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('time.monotonic', clock)
    monkeypatch.setattr('time.perf_counter', clock)
    return clock


def lines(caplog, scope=None):
    result = [r.getMessage() for r in caplog.records
              if r.getMessage().startswith('[load-diag]')]
    if scope:
        result = [line for line in result if f'work_scope={scope}' in line]
    return result


def value(line, field):
    return float(_field(line, field))


def finish(processor, engine):
    engine.result = 'hello'
    engine.endpoint = True
    processor.process_chunk(CHUNK_30MS)


@pytest.mark.parametrize('stage', ['vad', 'agc', 'keep_warm'])
def test_slow_stage_is_attributed_beside_unchanged_engine_ratio(stage, clock, caplog):
    from shared_audio.lead_in_buffer import LeadInBuffer
    processor, engine = _make_processor(vad_lead_in_ms=60)
    processor.lead_in_buffer = LeadInBuffer(0.060, 16000)
    speech = [False, True, True]

    def vad(pcm):
        if stage == 'vad':
            clock.advance(0.400)
        return speech.pop(0)

    def agc(pcm, is_speech):
        if stage == 'agc':
            clock.advance(0.400)
        return pcm

    def warm(audio):
        if stage == 'keep_warm':
            clock.advance(1.200)

    processor.vad.is_speech.side_effect = vad
    processor.agc.process.side_effect = agc
    engine.keep_warm = warm
    engine.process_audio = lambda audio: clock.advance(0.003)
    with caplog.at_level(logging.INFO):
        processor.process_chunk(CHUNK_30MS)
        processor.process_chunk(CHUNK_30MS)
        finish(processor, engine)
    utterance = [line for line in lines(caplog) if 'utt=' in line]
    assert len(utterance) == 1
    line = utterance[0]
    assert value(line, 'pre_engine_ms_total') == pytest.approx(1200.0)
    assert value(line, stage + '_ms') == pytest.approx(1200.0)
    assert value(line, 'pre_engine_audio_ms') == 90.0
    assert value(line, 'engine_ms_total') == 9.0
    assert value(line, 'engine_ratio') == 0.10


def test_evicted_silence_is_not_charged_to_next_utterance(clock, caplog):
    from shared_audio.lead_in_buffer import LeadInBuffer
    processor, engine = _make_processor(vad_lead_in_ms=30)
    processor.lead_in_buffer = LeadInBuffer(0.030, 16000)
    processor.vad.is_speech.side_effect = [False, False, True, True]
    delays = iter([1.2, 0.05])
    engine.keep_warm = lambda audio: clock.advance(next(delays))
    with caplog.at_level(logging.INFO):
        processor.process_chunk(CHUNK_30MS)
        processor.process_chunk(CHUNK_30MS)
        processor.process_chunk(CHUNK_30MS)
        finish(processor, engine)
    line = [line for line in lines(caplog) if 'utt=' in line][0]
    assert value(line, 'keep_warm_ms') == 50.0
    assert value(line, 'pre_engine_audio_ms') == 90.0


@pytest.mark.parametrize('lead_in_ms', [0, -30])
def test_nonpositive_lead_in_keeps_no_work_and_does_not_break_capture(lead_in_ms, clock, caplog):
    from shared_audio.lead_in_buffer import LeadInBuffer
    # Provider config passes this value through. LeadInBuffer tolerates it
    # by retaining no chunks; diagnostics must preserve that behavior.
    processor, engine = _make_processor(vad_lead_in_ms=lead_in_ms)
    processor.lead_in_buffer = LeadInBuffer(lead_in_ms / 1000, 16000)
    processor.vad.is_speech.side_effect = [False, True, True]
    engine.keep_warm = lambda audio: clock.advance(1.2)
    error = None
    with caplog.at_level(logging.INFO):
        try:
            processor.process_chunk(CHUNK_30MS)
        except Exception as exc:
            error = exc
        assert error is None, f'diagnostics broke zero-retention capture: {error!r}'
        processor.process_chunk(CHUNK_30MS)
        finish(processor, engine)
    line = [line for line in lines(caplog) if 'utt=' in line][0]
    assert value(line, 'keep_warm_ms') == 0.0
    assert value(line, 'pre_engine_audio_ms') == 60.0


def test_new_utterance_and_manual_reset_do_not_inherit_work(clock, caplog):
    processor, engine = _make_processor()
    processor.vad.is_speech.side_effect = lambda pcm: (clock.advance(.1) or True)
    with caplog.at_level(logging.INFO):
        processor.process_chunk(CHUNK_30MS)
        processor.reset_utterance()
        finish(processor, engine)
        finish(processor, engine)
    utterances = [line for line in lines(caplog) if 'utt=' in line]
    assert [value(line, 'vad_ms') for line in utterances] == [100.0, 100.0]
    assert [value(line, 'pre_engine_audio_ms') for line in utterances] == [30.0, 30.0]


def test_forced_endpoint_includes_last_vad_and_agc_but_not_finalize(clock, caplog):
    processor, engine = _make_processor(force_endpoint_silence_ms=30)
    processor.vad.is_speech.side_effect = [True, False]
    processor.agc.process.side_effect = lambda pcm, speech: (clock.advance(.2) or pcm)
    engine.finalize = lambda: clock.advance(.9)
    with caplog.at_level(logging.INFO):
        processor.process_chunk(CHUNK_30MS)
        processor.process_chunk(CHUNK_30MS)
    line = [line for line in lines(caplog) if 'utt=' in line][0]
    assert value(line, 'agc_ms') == 400.0
    assert value(line, 'pre_engine_ms_total') == 400.0
    assert value(line, 'pre_engine_audio_ms') == 60.0
    assert value(line, 'engine_ms_total') == 900.0
    assert value(line, 'engine_ratio') == 30.0


def test_silence_work_is_reported_without_any_recognizer_utterance(clock, caplog):
    processor, engine = _make_processor(log_load_diagnostics=True)
    processor.vad.is_speech.return_value = False
    engine.keep_warm = lambda audio: clock.advance(1.2)
    with caplog.at_level(logging.INFO):
        for _ in range(9):
            processor.process_chunk(CHUNK_30MS)
    idle = lines(caplog, 'silence')
    assert len(idle) == 1, 'long silence must report even when no utterance ever opens'
    assert value(idle[0], 'keep_warm_ms') == 10800.0
    assert value(idle[0], 'pre_engine_audio_ms') == 270.0
    assert _field(idle[0], 'engine_ratio') == 'n/a'
    assert engine.calls == 0


def test_silence_idle_line_is_off_by_default(clock, caplog):
    """wh-audit13-preengine-load-review.1: the ten-second silence line
    follows [debug] log_load_diagnostics and is off unless a provider turns
    it on. Silence is the normal state of the app, so an always-on line
    writes all day into wheelhouse.log."""
    processor, engine = _make_processor()
    processor.vad.is_speech.return_value = False
    engine.keep_warm = lambda audio: clock.advance(1.2)
    with caplog.at_level(logging.INFO):
        for _ in range(9):
            processor.process_chunk(CHUNK_30MS)
    assert lines(caplog, 'silence') == []


def test_speech_onset_line_still_reports_the_current_window_when_off(clock, caplog):
    """The one-per-event onset line stays, and the silent ten-second reset
    still bounds it: eleven silent chunks are 13.2 s, the window closed
    without a line at 10.8 s, so the onset reports the 2.4 s after it."""
    processor, engine = _make_processor()
    speech = [False] * 11 + [True]
    processor.vad.is_speech.side_effect = lambda pcm: speech.pop(0)
    engine.keep_warm = lambda audio: clock.advance(1.2)
    with caplog.at_level(logging.INFO):
        for _ in range(12):
            processor.process_chunk(CHUNK_30MS)
    idle = lines(caplog, 'silence')
    assert len(idle) == 1
    assert 'work_end=speech' in idle[0]
    assert value(idle[0], 'keep_warm_ms') == 2400.0
    assert value(idle[0], 'pre_engine_audio_ms') == 60.0


def detector(clock, *, confidence=0.9, failure=False, log_load_diagnostics=None):
    from shared_stt.wake_word_detector import WakeWordDetector
    # None leaves the constructor default in force, so the off-by-default
    # test exercises that default (mutation wake-setting-defaults-on).
    instance = WakeWordDetector('computer', '.', enabled=False,
                                **({} if log_load_diagnostics is None
                                   else {'log_load_diagnostics': log_load_diagnostics}))
    instance.is_loaded = True

    def predict(audio):
        clock.advance(1.2)
        if failure:
            raise RuntimeError('inference failed')
        return {'computer': confidence}

    instance._model = Mock(predict=predict)
    return instance


def test_slow_wake_word_is_reported_at_detection_without_engine_ratio(clock, caplog):
    wake = detector(clock)
    with caplog.at_level(logging.INFO):
        assert wake.process(CHUNK_30MS) == 'computer'
    records = lines(caplog, 'wake_word')
    assert len(records) == 1, 'detected wake word must expose its inference cost'
    assert value(records[0], 'wake_word_ms') == 1200.0
    assert value(records[0], 'pre_engine_ms_total') == 1200.0
    assert value(records[0], 'pre_engine_audio_ms') == 30.0
    assert _field(records[0], 'engine_ratio') == 'n/a'
    assert 'work_end=detected' in records[0]


@pytest.mark.parametrize('failure', [False, True])
def test_wake_word_without_detection_still_reports_bounded_windows(failure, clock, caplog):
    wake = detector(clock, confidence=0.1, failure=failure, log_load_diagnostics=True)
    with caplog.at_level(logging.INFO):
        for _ in range(18):
            assert wake.process(CHUNK_30MS) is None
    records = lines(caplog, 'wake_word')
    assert len(records) == 2, 'idle inference must not need a future utterance to report'
    assert [value(line, 'wake_word_ms') for line in records] == [10800.0, 10800.0]
    assert [value(line, 'pre_engine_audio_ms') for line in records] == [270.0, 270.0]


def test_wake_word_idle_line_is_off_by_default(clock, caplog):
    """wh-audit13-preengine-load-review.1: the ten-second wake-word line
    follows [debug] log_load_diagnostics, like the silence line."""
    wake = detector(clock, confidence=0.1)
    with caplog.at_level(logging.INFO):
        for _ in range(18):
            assert wake.process(CHUNK_30MS) is None
    assert lines(caplog, 'wake_word') == []


def test_wake_word_detection_line_still_reports_the_current_window_when_off(clock, caplog):
    """The one-per-event detection line stays, and the silent ten-second
    reset still bounds it: the window closed without a line at 10.8 s, so
    the detection at 14.4 s reports the three chunks after it."""
    wake = detector(clock, confidence=0.1)
    with caplog.at_level(logging.INFO):
        for _ in range(11):
            assert wake.process(CHUNK_30MS) is None
        wake.sensitivity = 0.05
        assert wake.process(CHUNK_30MS) == 'computer'
    records = lines(caplog, 'wake_word')
    assert len(records) == 1
    assert 'work_end=detected' in records[0]
    assert value(records[0], 'wake_word_ms') == 3600.0


def test_wake_word_reset_flushes_partial_window(clock, caplog):
    wake = detector(clock, confidence=0.1)
    with caplog.at_level(logging.INFO):
        wake.process(CHUNK_30MS)
        wake.reset()
        wake.reset()
        # Reset is a WebSocket control callback in the providers. The audio
        # consumer owns counters and flushes on its next call, not that thread.
        wake.process(CHUNK_30MS)
    records = lines(caplog, 'wake_word')
    assert len(records) == 1
    assert value(records[0], 'wake_word_ms') == 1200.0
    assert 'work_end=reset' in records[0]


def test_reset_during_wake_inference_does_not_lose_its_time(clock, caplog):
    wake = detector(clock, confidence=0.1)
    windows_at_inference_start = []

    def predict(audio):
        windows_at_inference_start.append(len(lines(caplog, 'wake_word')))
        clock.advance(.6)
        wake.reset()  # control callback runs while the model owns this call
        clock.advance(.6)
        return {'computer': .1}

    wake._model.predict = predict
    with caplog.at_level(logging.INFO):
        wake.process(CHUNK_30MS)
        wake.process(CHUNK_30MS)
    records = lines(caplog, 'wake_word')
    assert windows_at_inference_start == [0, 1], 'only the consumer may close the prior window'
    assert len(records) == 1
    assert value(records[0], 'wake_word_ms') == 1200.0
    assert value(records[0], 'pre_engine_audio_ms') == 30.0


def test_diagnostic_handler_failure_cannot_discard_a_detected_wake_word(clock, caplog):
    wake = detector(clock)
    wake._diagnostic_logger = Mock(info=Mock(side_effect=RuntimeError('log unavailable')))
    assert wake.process(CHUNK_30MS) == 'computer'


def test_unrecognized_idle_lines_do_not_create_fake_parser_utterances(clock, caplog):
    from tools.stt_load_test.logparse import parse_log
    wake = detector(clock)
    with caplog.at_level(logging.INFO):
        wake.process(CHUNK_30MS)
    parsed = parse_log('\n'.join(lines(caplog)))
    assert parsed.utterances == []
    assert parsed.windows == []


def test_google_wake_cost_uses_the_logger_that_reaches_its_forwarder(clock):
    """Execute Google's real detector construction with a local model double.

    The emitted record, not AST text, proves that the model's measured cost
    reaches Google's existing log route.
    """
    logger = logging.getLogger('test.google.pre-engine')
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Collect()
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        wake = _google_detector(clock, logger)
        assert wake.process(CHUNK_30MS) == 'computer'
        assert any('wake_word_ms=1200.0' in record for record in records), records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _google_wake_word_block():
    """The `if wake_word_enabled` block of google_stt_server/main.py's main().

    The block is isolated because running main would open a real
    microphone/credential client.
    """
    import ast
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / 'google_stt_server/main.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == 'main')
    block = next(node for node in main.body if isinstance(node, ast.If)
                 and isinstance(node.test, ast.Call)
                 and any(isinstance(arg, ast.Constant) and arg.value == 'wake_word_enabled'
                         for arg in node.test.args))
    return source, ast.Module(body=[block], type_ignores=[])


def _google_detector(clock, logger, *, log_load_diagnostics=False, confidence=0.9):
    """Run Google's real detector construction with a local model double."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from shared_stt.wake_word_detector import WakeWordDetector
    source, module = _google_wake_word_block()
    namespace = dict(cfg=SimpleNamespace(wake_word_enabled=True, wake_word_keyword='computer',
                                         debug=SimpleNamespace(
                                             log_load_diagnostics=log_load_diagnostics)),
                     wake_word_mode='idle_recovery', logger=logger)
    with patch.object(WakeWordDetector, '_resolve_model_path', return_value=None):
        exec(compile(module, str(source), 'exec'), namespace)
    wake = namespace['wake_word_detector']
    wake.is_loaded = True
    wake._model = detector(clock, confidence=confidence)._model
    return wake


@pytest.mark.parametrize('flag, windows', [(False, 0), (True, 2)])
def test_google_wake_detector_follows_the_load_diagnostics_setting(flag, windows, clock, caplog):
    """The idle ten-second wake-word line in the Google provider follows
    [debug] log_load_diagnostics, the flag its window line already follows."""
    logger = logging.getLogger('test.google.pre-engine-flag')
    wake = _google_detector(clock, logger, log_load_diagnostics=flag, confidence=0.1)
    with caplog.at_level(logging.INFO, logger=logger.name):
        for _ in range(18):
            assert wake.process(CHUNK_30MS) is None
    assert len(lines(caplog, 'wake_word')) == windows
