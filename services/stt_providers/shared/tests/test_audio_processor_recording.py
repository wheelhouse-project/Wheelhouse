"""Recording mode of AudioProcessor.process_chunk (wh-first-sound-lost.23).

Acceptance items 2, 3 and 4: the recorder keeps the raw chunks before the AGC,
the engine and the transcript messages stay untouched while it records, and the
capture ends on one of three conditions measured in audio samples.

Chunks here are 500 samples (31.25 ms) so that 1.0 s, 4.0 s and 10.0 s are
exactly 32, 128 and 320 chunks.
"""
import logging
import threading
from unittest.mock import Mock, call

import numpy as np
import pytest

from shared_stt.audio_processor import AudioProcessor
from shared_stt.chunk_recorder import (
    END_MAX,
    END_NO_SPEECH,
    END_SILENCE,
    ChunkRecorder,
)

CHUNK = 500  # samples
RATE = 16000


def make_chunk(value: int, n: int = CHUNK) -> bytes:
    return (np.full(n, value, dtype="<i2")).tobytes()


class Script:
    """A VAD that answers from a list the test controls."""

    def __init__(self):
        self.speech = False
        self.calls = 0

    def is_speech(self, pcm):
        self.calls += 1
        return self.speech

    def reset(self):
        pass


@pytest.fixture
def parts():
    engine = Mock()
    engine.get_result = Mock(return_value="")
    engine.is_endpoint = Mock(return_value=False)
    engine.is_ready = Mock(return_value=True)
    engine.last_result = ""
    forwarder = Mock()
    started = []
    finished = []
    recorder = ChunkRecorder(
        sample_rate=RATE,
        lead_in_ms=300,
        end_silence_s=1.0,
        max_s=4.0,
        no_speech_timeout_s=10.0,
        on_start=lambda: started.append(True),
        on_finish=finished.append,
    )
    processor = AudioProcessor(
        engine=engine, forwarder=forwarder, sample_rate=RATE,
        vad_lead_in_ms=300, chunk_recorder=recorder,
    )
    vad = Script()
    processor.vad = vad
    agc = Mock()
    agc.outputs = []

    def double(pcm, speech):
        # The AGC changes the gain of every chunk: it returns the chunk doubled.
        out = ((np.frombuffer(pcm, dtype="<i2").astype(np.int32) * 2)
               .astype("<i2").tobytes())
        agc.outputs.append(out)
        return out

    agc.process = Mock(side_effect=double)
    agc.apply_last_gain = Mock(side_effect=lambda pcm: pcm)
    processor.agc = agc
    return processor, recorder, vad, engine, forwarder, agc, started, finished


def feed(processor, vad, speech: bool, value: int = 1000, count: int = 1):
    vad.speech = speech
    for _ in range(count):
        processor.process_chunk(make_chunk(value))


class TestRawCapture:
    def test_saved_samples_equal_input_while_agc_changes_the_gain(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        sent = []
        # Silence chunks 100, 101; then speech chunks 200, 201, 202.
        for value, speech in [(100, False), (101, False), (200, True),
                              (201, True), (202, True)]:
            vad.speech = speech
            chunk = make_chunk(value)
            sent.append(chunk)
            processor.process_chunk(chunk)
        feed(processor, vad, False, value=5, count=32)
        assert len(finished) == 1
        recording = finished[0]
        expected = b"".join(sent) + make_chunk(5) * 32
        assert recording.samples.tobytes() == expected
        # The AGC did change the gain of the same chunks: it received each
        # chunk that was sent, in order, and what it returned (the chunk
        # doubled) is not what the recorder saved.
        sent_all = sent + [make_chunk(5)] * 32
        assert [c.args[0] for c in agc.process.call_args_list] == sent_all
        assert agc.outputs != sent_all
        assert recording.samples.tobytes() != b"".join(agc.outputs)
        # The AGC output is the saved audio with a gain of 2: the two differ
        # by the gain only, and the recorder kept the audio before it.
        assert np.array_equal(
            recording.samples.astype(np.int32) * 2,
            np.frombuffer(b"".join(agc.outputs), dtype="<i2"))

    def test_vad_and_agc_run_on_every_chunk_while_recording(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, False, count=3)
        feed(processor, vad, True, count=2)
        assert vad.calls == 5
        assert agc.process.call_count == 5

    def test_lead_in_is_trimmed_to_300_ms(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        # A processor with a 500 ms lead-in buffer still records 300 ms.
        processor.lead_in_buffer.capacity_bytes = 500 * 16 * 2
        recorder.request()
        for value in range(1, 21):  # 20 silent chunks = 625 ms
            vad.speech = False
            processor.process_chunk(make_chunk(value))
        vad.speech = True
        processor.process_chunk(make_chunk(99))
        feed(processor, vad, False, value=5, count=32)
        samples = finished[0].samples
        # 300 ms = 4800 samples of lead-in, then the speech chunk and the tail.
        assert len(samples) == 4800 + CHUNK + 32 * CHUNK
        assert samples[4800 - 1] == 20  # the last lead-in chunk is value 20
        assert samples[4800] == 99


class TestNoRecognitionWhileRecording:
    def test_hello_types_no_text_and_runs_no_command(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, False, count=4)
        feed(processor, vad, True, count=10)  # the spoken word
        feed(processor, vad, False, count=32)
        assert len(finished) == 1
        forwarder.send_vad_start.assert_not_called()
        forwarder.send_stable.assert_not_called()
        forwarder.send_final.assert_not_called()
        engine.process_audio.assert_not_called()
        engine.keep_warm.assert_not_called()
        engine.get_result.assert_not_called()
        # The only engine call is the reset that ends the capture.
        assert engine.mock_calls == [call.reset()]

    def test_recognition_continues_with_the_next_chunk(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=2)
        feed(processor, vad, False, count=32)
        assert len(finished) == 1
        assert not processor.is_gate_open
        feed(processor, vad, True, count=1)
        assert processor.is_gate_open
        forwarder.send_vad_start.assert_called_once()
        engine.process_audio.assert_called()

    def test_one_request_makes_one_take(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=2)
        feed(processor, vad, False, count=32)
        assert len(finished) == 1
        feed(processor, vad, True, count=3)
        feed(processor, vad, False, count=40)
        assert len(finished) == 1
        assert started == [True]


class TestEndConditions:
    def test_ends_1s_after_the_last_speech_chunk(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=3)
        feed(processor, vad, False, count=31)  # 31 * 500 = 15500 < 16000
        assert finished == []
        feed(processor, vad, False, count=1)  # 16000 samples of silence
        assert len(finished) == 1
        assert finished[0].end == END_SILENCE
        assert len(finished[0].samples) == (3 + 32) * CHUNK

    def test_speech_in_the_tail_restarts_the_1s_count(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=3)
        feed(processor, vad, False, count=31)
        feed(processor, vad, True, count=1)
        feed(processor, vad, False, count=31)
        assert finished == []
        feed(processor, vad, False, count=1)
        assert len(finished) == 1 and finished[0].end == END_SILENCE

    def test_ends_4s_after_the_gate_opened(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=127)  # 63500 < 64000
        assert finished == []
        feed(processor, vad, True, count=1)  # 64000 samples after the gate opened
        assert len(finished) == 1
        assert finished[0].end == END_MAX
        assert len(finished[0].samples) == 128 * CHUNK

    def test_max_counts_from_the_gate_not_from_the_request(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, False, count=100)  # 100 chunks of silence first
        feed(processor, vad, True, count=127)
        assert finished == []
        feed(processor, vad, True, count=1)
        assert len(finished) == 1 and finished[0].end == END_MAX

    def test_no_speech_within_10s_ends_with_no_capture(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, False, count=319)  # 159500 < 160000
        assert finished == []
        feed(processor, vad, False, count=1)  # 160000 samples = 10.0 s
        assert len(finished) == 1
        assert finished[0].end == END_NO_SPEECH
        assert finished[0].samples is None
        assert finished[0].waited_samples == 320 * CHUNK

    def test_the_ten_seconds_count_audio_not_wall_time(self, parts, monkeypatch):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        # Wall time must not matter: freeze the clocks.
        monkeypatch.setattr("time.monotonic", lambda: 1000.0)
        recorder.request()
        feed(processor, vad, False, count=320)
        assert len(finished) == 1 and finished[0].end == END_NO_SPEECH

    def test_the_end_resets_the_utterance_state(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        before = processor.current_utterance_id
        recorder.request()
        feed(processor, vad, True, count=2)
        feed(processor, vad, False, count=32)
        assert processor.current_utterance_id == before + 1
        assert len(processor.lead_in_buffer._chunks) == 0


class TestTheEndResetsBeforeTheCallback:
    def test_process_chunk_resets_the_utterance_state_before_the_callback(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        seen = []
        recorder._on_finish = lambda rec: seen.append(
            (processor.current_utterance_id,
             len(processor.lead_in_buffer._chunks),
             engine.mock_calls[-1:] == [call.reset()]))
        before = processor.current_utterance_id
        recorder.request()
        feed(processor, vad, False, count=3)
        feed(processor, vad, True, count=2)
        feed(processor, vad, False, count=32)
        assert seen == [(before + 1, 0, True)]


class TestStartTiming:
    def test_starts_at_the_next_chunk(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        assert recorder.request() is True
        assert started == []
        feed(processor, vad, False, count=1)
        assert started == [True]

    def test_a_second_request_while_busy_is_refused(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        assert recorder.request() is True
        assert recorder.request() is False
        feed(processor, vad, False, count=1)
        assert recorder.request() is False  # active now

    def test_start_waits_for_the_utterance_in_progress(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        processor._force_endpoint_silence_ms = 300
        feed(processor, vad, True, count=3)  # an utterance is in progress
        assert processor.is_gate_open
        recorder.request()
        feed(processor, vad, True, count=2)
        assert started == []
        assert engine.process_audio.call_count >= 5  # the engine kept the audio
        # The utterance ends on VAD silence (300 ms = 10 chunks of 500 > 4800).
        feed(processor, vad, False, count=10)
        assert not processor.is_gate_open
        assert started == []
        engine.process_audio.reset_mock()
        # The next chunk starts recording mode.
        feed(processor, vad, False, count=1)
        assert started == [True]
        engine.process_audio.assert_not_called()


class TestCancel:
    def test_cancel_before_start_means_it_never_starts(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        recorder.cancel()
        assert recorder.busy is False
        feed(processor, vad, False, count=2)
        assert started == []

    def test_cancel_while_recording_ends_it_without_a_result(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=3)
        assert recorder.busy is True
        recorder.cancel()
        feed(processor, vad, False, count=1)
        assert finished == []
        assert recorder.busy is False
        # Recognition is normal again at once.
        feed(processor, vad, True, count=1)
        forwarder.send_vad_start.assert_called_once()

    def test_a_cancelled_capture_leaves_clean_utterance_state(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=3)
        before = processor.current_utterance_id
        recorder.cancel()
        feed(processor, vad, False, count=1)
        assert processor.current_utterance_id == before + 1


class TestCancelBeforeTheGateOpened:
    """A cancel before any speech took no lead-in, so the utterance state
    stays as it is: no new utterance id, no engine reset, lead-in kept."""

    def test_process_chunk_does_not_reset_after_a_cancel_before_speech(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, False, count=3)
        before = processor.current_utterance_id
        recorder.cancel()
        feed(processor, vad, False, count=1)
        assert recorder.busy is False
        assert recorder.gate_opened is False
        assert processor.current_utterance_id == before
        engine.reset.assert_not_called()
        # The chunks heard while recording stay in the lead-in buffer, and
        # the chunk after the cancel joins them.
        assert len(processor.lead_in_buffer._chunks) == 4

    def test_end_recording_now_does_not_reset_after_a_cancel_before_speech(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, False, count=3)
        before = processor.current_utterance_id
        recorder.cancel()
        processor.end_recording_now()
        assert finished == []
        assert processor.current_utterance_id == before
        engine.reset.assert_not_called()
        assert len(processor.lead_in_buffer._chunks) == 3


class TestCallbackFailure:
    def test_a_failing_finish_callback_never_stops_the_audio_loop(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts

        def boom(recording):
            raise RuntimeError("the check failed")

        recorder._on_finish = boom
        recorder.request()
        feed(processor, vad, True, count=2)
        feed(processor, vad, False, count=32)  # does not raise
        feed(processor, vad, True, count=1)
        forwarder.send_vad_start.assert_called_once()


class TestStartCallbackFailure:
    def test_a_failing_start_callback_never_stops_the_take(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts

        def boom():
            raise RuntimeError("the start failed")

        recorder._on_start = boom
        recorder.request()
        feed(processor, vad, True, count=2)
        feed(processor, vad, False, count=32)  # does not raise
        assert len(finished) == 1
        assert len(finished[0].samples) == 34 * CHUNK


class TestDelivery:
    def test_a_finished_capture_is_delivered_once(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=2)
        feed(processor, vad, False, count=32)
        recorder.deliver()
        recorder.deliver()
        assert len(finished) == 1

    def test_a_recorder_without_a_finish_callback_ends_quietly(self, parts, caplog):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder._on_finish = None
        recorder.request()
        with caplog.at_level(logging.ERROR):
            feed(processor, vad, True, count=2)
            feed(processor, vad, False, count=32)
        assert recorder.busy is False
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    def test_the_saved_samples_are_a_writable_int16_array(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=2)
        feed(processor, vad, False, count=32)
        samples = finished[0].samples
        assert samples.dtype == np.int16
        assert samples.flags.writeable


class TestASecondTake:
    def test_a_second_take_starts_with_a_clean_capture(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=3)
        feed(processor, vad, False, count=32)
        assert len(finished) == 1 and finished[0].end == END_SILENCE
        # A second take that hears no speech: the gate of the first take is
        # gone, and the audio it waited for counts from zero.
        recorder.request()
        feed(processor, vad, False, count=4)
        processor.end_recording_now()
        assert len(finished) == 2
        assert finished[1].end == END_NO_SPEECH
        assert finished[1].samples is None
        assert finished[1].waited_samples == 4 * CHUNK

    def test_end_recording_now_after_a_take_does_nothing(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=2)
        feed(processor, vad, False, count=32)
        assert len(finished) == 1
        before = processor.current_utterance_id
        processor.end_recording_now()
        assert len(finished) == 1
        assert processor.current_utterance_id == before


class _CancelOnFirstEnter:
    """A lock that drops the pending request the first time it is entered.

    It stands for a cancel() from another thread that lands between the
    unlocked check at the top of feed() and the lock.
    """

    def __init__(self, recorder):
        self._real = threading.Lock()
        self._recorder = recorder
        self._fired = False

    def __enter__(self):
        self._real.acquire()
        if not self._fired:
            self._fired = True
            self._recorder._pending = False
        return self

    def __exit__(self, *exc):
        self._real.release()

    def locked(self):
        return self._real.locked()


class TestLockUse:
    def test_a_cancel_between_the_quick_check_and_the_lock_means_it_never_starts(
        self, parts
    ):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        recorder._lock = _CancelOnFirstEnter(recorder)
        feed(processor, vad, False, count=2)
        assert started == []
        assert recorder.busy is False

    def test_a_chunk_is_taken_with_the_lock_held_and_a_callback_runs_without_it(
        self, parts
    ):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        held = {"take": [], "end": [], "start": [], "finish": []}
        take, end = recorder._take, recorder._end

        def spy_take(*args):
            held["take"].append(recorder._lock.locked())
            return take(*args)

        def spy_end(reason):
            held["end"].append(recorder._lock.locked())
            return end(reason)

        recorder._take = spy_take
        recorder._end = spy_end
        recorder._on_start = lambda: held["start"].append(recorder._lock.locked())
        recorder._on_finish = lambda rec: held["finish"].append(recorder._lock.locked())
        recorder.request()
        feed(processor, vad, True, count=2)
        feed(processor, vad, False, count=32)
        assert held["take"] and all(held["take"])
        assert held["end"] == [True]
        assert held["start"] == [False]
        assert held["finish"] == [False]
        # The end of a take by end_now() holds the lock as well.
        recorder.request()
        feed(processor, vad, True, count=2)
        processor.end_recording_now()
        assert held["end"] == [True, True]


class TestNoRecorderIsUnchanged:
    def test_a_processor_without_a_recorder_never_records(self):
        engine = Mock()
        engine.get_result = Mock(return_value="")
        engine.is_endpoint = Mock(return_value=False)
        engine.is_ready = Mock(return_value=True)
        engine.last_result = ""
        forwarder = Mock()
        processor = AudioProcessor(
            engine=engine, forwarder=forwarder, sample_rate=RATE)
        vad = Script()
        processor.vad = vad
        feed(processor, vad, True, count=2)
        forwarder.send_vad_start.assert_called_once()
        engine.process_audio.assert_called()


class TestEndRecordingNow:
    """The provider calls end_recording_now() where its audio loop skips
    process_chunk because transcription is off (push-to-talk release).

    Decision M1 of wh-first-sound-lost.23: a release must end the take, because
    the end-silence timer counts only audio that arrives.
    """

    def test_gate_opened_ends_the_capture_as_end_silence(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, False, count=2)
        feed(processor, vad, True, count=3)
        assert finished == []
        processor.end_recording_now()
        assert len(finished) == 1
        assert finished[0].end == END_SILENCE
        assert finished[0].samples is not None
        assert len(finished[0].samples) == 5 * CHUNK
        assert recorder.busy is False

    def test_gate_never_opened_ends_as_no_speech(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, False, count=4)
        processor.end_recording_now()
        assert len(finished) == 1
        assert finished[0].end == END_NO_SPEECH
        assert finished[0].samples is None
        assert finished[0].waited_samples == 4 * CHUNK

    def test_a_request_that_is_only_pending_stays_pending(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        processor.end_recording_now()
        assert finished == []
        assert started == []
        assert recorder.busy is True
        feed(processor, vad, False, count=1)
        assert started == [True]

    def test_idle_does_nothing(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        before = processor.current_utterance_id
        processor.end_recording_now()
        assert finished == []
        assert processor.current_utterance_id == before

    def test_the_end_resets_the_utterance_state_before_the_callback(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        seen = []
        recorder._on_finish = lambda rec: seen.append(
            (processor.current_utterance_id, len(processor.lead_in_buffer._chunks)))
        before = processor.current_utterance_id
        recorder.request()
        feed(processor, vad, True, count=2)
        processor.end_recording_now()
        assert seen == [(before + 1, 0)]

    def test_a_cancelled_capture_delivers_nothing(self, parts):
        processor, recorder, vad, engine, forwarder, agc, started, finished = parts
        recorder.request()
        feed(processor, vad, True, count=3)
        recorder.cancel()
        before = processor.current_utterance_id
        processor.end_recording_now()
        assert finished == []
        assert recorder.busy is False
        assert processor.current_utterance_id == before + 1

    def test_a_processor_without_a_recorder_ignores_the_call(self):
        engine = Mock()
        processor = AudioProcessor(
            engine=engine, forwarder=Mock(), sample_rate=RATE)
        processor.end_recording_now()  # does not raise
