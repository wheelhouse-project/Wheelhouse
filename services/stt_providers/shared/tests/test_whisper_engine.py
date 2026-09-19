"""Tests for WhisperStreamingEngine.

Tests the chunked re-inference streaming engine with LocalAgreement-2
stability detection, audio buffer management, and endpoint detection.

Mock strategy: WhisperModel is mocked to avoid loading real model weights.
The mock returns predetermined transcription results.

Reference: docs/design/chunked_streaming_engine_design.md
"""
import numpy as np
import pytest
from typing import Any
from unittest.mock import Mock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_audio_chunk(duration_ms: int = 30, sample_rate: int = 16000, rms: float = 0.1) -> bytes:
    """Create float32 audio bytes with specified RMS level."""
    n_samples = int(sample_rate * duration_ms / 1000)
    t = np.linspace(0, duration_ms / 1000, n_samples, dtype=np.float32)
    amplitude = rms * np.sqrt(2)  # sine wave RMS = amplitude / sqrt(2)
    samples = (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return samples.tobytes()


def make_silence_chunk(duration_ms: int = 30, sample_rate: int = 16000) -> bytes:
    """Create float32 silent audio bytes (all zeros)."""
    n_samples = int(sample_rate * duration_ms / 1000)
    return np.zeros(n_samples, dtype=np.float32).tobytes()


def make_mock_segment(text: str):
    """Create a mock segment object like faster-whisper returns.

    Defaults match the 'real dictation' band (avg_logprob -0.2) so existing
    tests that don't exercise the wh-7ou.2 hallucination filter pass through
    naturally. Tests that need specific logprob values should use
    make_mock_segment_with_conf below.
    """
    segment = Mock()
    segment.text = text
    segment.avg_logprob = -0.2
    segment.no_speech_prob = 0.01
    segment.compression_ratio = 0.5
    segment.start = 0.0
    segment.end = 1.0
    # Real faster-whisper segments have words=None unless word_timestamps=True
    segment.words = None
    return segment


def feed_audio(engine, num_chunks: int, rms: float = 0.1, duration_ms: int = 30):
    """Feed multiple audio chunks to engine."""
    for _ in range(num_chunks):
        engine.process_audio(make_audio_chunk(duration_ms, rms=rms))


def feed_silence(engine, num_chunks: int, duration_ms: int = 30):
    """Feed multiple silence chunks to engine."""
    for _ in range(num_chunks):
        engine.process_audio(make_silence_chunk(duration_ms))


# At 16kHz with 30ms chunks:
# - 480 samples per chunk
# - 14 chunks = 6720 samples = 420ms (exceeds 400ms interval)
# - 17 chunks = 8160 samples = 510ms (exceeds 500ms endpoint threshold)
CHUNKS_FOR_INFERENCE = 14   # Enough to trigger 400ms inference interval
CHUNKS_FOR_ENDPOINT = 17    # Enough to trigger 500ms endpoint silence


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWhisperStreamingEngineInit:
    """Tests for engine initialization."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_creates_whisper_model(self, mock_model_class):
        """Engine should create a WhisperModel on init."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        WhisperStreamingEngine(
            model_size_or_path="large-v3-turbo",
            device="cuda",
            compute_type="float16",
        )

        mock_model_class.assert_called_once_with(
            "large-v3-turbo", device="cuda", compute_type="float16"
        )

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_last_result_starts_empty(self, mock_model_class):
        """last_result attribute should start as empty string."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine()
        assert engine.last_result == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_satisfies_recognition_engine_protocol(self, mock_model_class):
        """Engine should satisfy the RecognitionEngine protocol."""
        from shared_stt.whisper_engine import WhisperStreamingEngine
        from shared_stt.audio_processor import RecognitionEngine

        engine = WhisperStreamingEngine()
        assert isinstance(engine, RecognitionEngine)


class TestAudioBufferAccumulation:
    """Tests for audio buffer management."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_process_audio_accumulates(self, mock_model_class):
        """Audio bytes should be appended to the internal buffer."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine(re_inference_interval_ms=10000)

        chunk1 = make_audio_chunk(30)
        chunk2 = make_audio_chunk(30)
        engine.process_audio(chunk1)
        engine.process_audio(chunk2)

        assert len(engine._audio_buffer) == 2

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_buffer_cleared_on_reset(self, mock_model_class):
        """reset() should clear the audio buffer."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine(re_inference_interval_ms=10000)
        engine.process_audio(make_audio_chunk(30))
        engine.process_audio(make_audio_chunk(30))

        engine.reset()
        assert len(engine._audio_buffer) == 0


class TestInferenceTriggering:
    """Tests for re-inference interval and gating logic."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_is_ready_false_before_inference(self, mock_model_class):
        """is_ready() should return False before any inference has run."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine(re_inference_interval_ms=10000)
        engine.process_audio(make_audio_chunk(30))

        assert engine.is_ready() is False

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_inference_triggers_at_interval(self, mock_model_class):
        """Inference should trigger after re_inference_interval_ms of audio."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        mock_model.transcribe.assert_called_once()
        assert engine.is_ready() is True

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_no_inference_before_interval(self, mock_model_class):
        """Inference should NOT trigger before interval elapses."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Only feed ~200ms of audio (under 400ms interval)
        feed_audio(engine, 7)

        mock_model.transcribe.assert_not_called()

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_no_inference_before_speech(self, mock_model_class):
        """Engine should not run inference until speech energy is detected."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.01,
        )

        # Feed 510ms of silence (exceeds interval but no speech energy)
        feed_silence(engine, CHUNKS_FOR_ENDPOINT)

        mock_model.transcribe.assert_not_called()

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_is_ready_resets_between_process_audio_calls(self, mock_model_class):
        """is_ready() should be False when process_audio didn't trigger inference."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Trigger first inference
        feed_audio(engine, CHUNKS_FOR_INFERENCE)
        assert engine.is_ready() is True

        # Next chunk won't trigger inference (interval resets)
        engine.process_audio(make_audio_chunk(30, rms=0.1))
        assert engine.is_ready() is False

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_interval_resets_after_inference(self, mock_model_class):
        """Sample counter should reset after inference, requiring another full interval."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # First inference at ~420ms
        feed_audio(engine, CHUNKS_FOR_INFERENCE)
        assert mock_model.transcribe.call_count == 1

        # Feed only 7 more chunks (~210ms) -- not enough for second inference
        feed_audio(engine, 7)
        assert mock_model.transcribe.call_count == 1  # Still 1

        # Feed 7 more (total ~420ms since last inference)
        feed_audio(engine, 7)
        assert mock_model.transcribe.call_count == 2


class TestLocalAgreement2Stability:
    """Tests for LocalAgreement-2 stability detection."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_first_inference_no_confirmation(self, mock_model_class):
        """First inference should set prev_words but not confirm anything."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Only 1 run -- no confirmation possible
        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_two_run_agreement_confirms_lcp(self, mock_model_class):
        """Words confirmed by LocalAgreement-2 when LCP of 2 consecutive runs matches."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Run 1: "hello"
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Run 2: "hello world" -- LCP with run 1 = ["hello"]
        mock_model.transcribe.return_value = ([make_mock_segment("hello world")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "hello"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_stability_never_shrinks(self, mock_model_class):
        """Confirmed words monotonically increase -- never shrink even if model revises."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Run 1: "hello world"
        mock_model.transcribe.return_value = ([make_mock_segment("hello world")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Run 2: "hello world how" -- confirms ["hello", "world"]
        mock_model.transcribe.return_value = ([make_mock_segment("hello world how")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "hello world"

        # Run 3: "goodbye" -- completely different, LCP = 0
        mock_model.transcribe.return_value = ([make_mock_segment("goodbye")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Confirmed should still be "hello world" (never shrinks)
        assert engine.get_result() == "hello world"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_stability_grows_incrementally(self, mock_model_class):
        """Confirmed words grow as more consecutive runs agree on the prefix."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Run 1: "hello"
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)
        assert engine.get_result() == ""  # No confirmation yet

        # Run 2: "hello world" -- confirms "hello"
        mock_model.transcribe.return_value = ([make_mock_segment("hello world")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)
        assert engine.get_result() == "hello"

        # Run 3: "hello world how" -- confirms "hello world"
        mock_model.transcribe.return_value = ([make_mock_segment("hello world how")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)
        assert engine.get_result() == "hello world"

        # Run 4: "hello world how are" -- confirms "hello world how"
        mock_model.transcribe.return_value = ([make_mock_segment("hello world how are")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)
        assert engine.get_result() == "hello world how"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_two_runs_fully_disagree_nothing_confirmed(self, mock_model_class):
        """When two consecutive runs fully disagree, LCP = 0, nothing new confirmed."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Run 1: "hello"
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Run 2: "goodbye" -- LCP = 0
        mock_model.transcribe.return_value = ([make_mock_segment("goodbye")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == ""


class TestEndpointDetection:
    """Tests for silence-based endpoint detection."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_endpoint_on_trailing_silence(self, mock_model_class):
        """is_endpoint() should fire after trailing silence exceeds threshold."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            endpoint_silence_ms=500,
            silence_rms_threshold=0.01,
        )

        # Feed speech to trigger inference and set _speech_detected
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Feed 510ms of silence (exceeds 500ms threshold)
        feed_silence(engine, CHUNKS_FOR_ENDPOINT)

        assert engine.is_endpoint() is True

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_no_endpoint_without_speech(self, mock_model_class):
        """is_endpoint() should NOT fire if no speech was ever detected."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            endpoint_silence_ms=500,
            silence_rms_threshold=0.01,
        )

        # Feed only silence (no speech)
        feed_silence(engine, 20)

        assert engine.is_endpoint() is False

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_no_endpoint_before_threshold(self, mock_model_class):
        """is_endpoint() should NOT fire before silence threshold."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            endpoint_silence_ms=500,
            silence_rms_threshold=0.01,
        )

        # Feed speech
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Only 200ms of silence (under 500ms threshold)
        feed_silence(engine, 7)

        assert engine.is_endpoint() is False

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_endpoint_final_inference_promotes_all_words(self, mock_model_class):
        """On endpoint, final inference should promote all words to confirmed."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            endpoint_silence_ms=500,
            silence_rms_threshold=0.01,
        )

        # Run 1: "hello" (sets prev_words, no confirmation)
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)
        assert engine.get_result() == ""

        # Endpoint silence triggers final inference
        mock_model.transcribe.return_value = ([make_mock_segment("hello world")], Mock())
        feed_silence(engine, CHUNKS_FOR_ENDPOINT)

        assert engine.is_endpoint() is True
        # Final inference promotes ALL words (bypasses 2-run requirement)
        assert engine.get_result() == "hello world"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_silence_counter_resets_on_speech(self, mock_model_class):
        """Trailing silence counter should reset when speech resumes."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            endpoint_silence_ms=500,
            silence_rms_threshold=0.01,
        )

        # Speech to set _speech_detected
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Partial silence (300ms) -- under threshold
        feed_silence(engine, 10)

        # Speech resumes -- counter should reset
        feed_audio(engine, 5, rms=0.1)

        # More silence (300ms) -- under threshold (counter was reset)
        feed_silence(engine, 10)

        assert engine.is_endpoint() is False


class TestResetBehavior:
    """Tests for state reset."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_reset_clears_all_state(self, mock_model_class):
        """reset() should clear all internal state."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Build up state
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        engine.reset()

        assert engine._audio_buffer == []
        assert engine._confirmed_words == []
        assert engine._prev_words == []
        assert engine._speech_detected is False
        assert engine._trailing_silence_samples == 0
        assert engine._has_new_result is False
        assert engine._finalized is False
        assert engine.last_result == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_reset_allows_new_utterance(self, mock_model_class):
        """After reset(), engine should process a fresh utterance."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # First utterance
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        engine.reset()

        # Second utterance -- fresh start
        mock_model.transcribe.return_value = ([make_mock_segment("world")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # First run in new utterance: prev_words set but no confirmation
        assert engine.get_result() == ""
        assert engine.is_ready() is True


class TestMultiSegmentTranscription:
    """Tests for handling multi-segment output from faster-whisper."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_concatenates_multiple_segments(self, mock_model_class):
        """Engine should concatenate text from multiple segments."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Run 1: two segments
        segments = [make_mock_segment(" hello"), make_mock_segment(" world")]
        mock_model.transcribe.return_value = (segments, Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Run 2: same result (confirms)
        segments = [make_mock_segment(" hello"), make_mock_segment(" world")]
        mock_model.transcribe.return_value = (segments, Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "hello world"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_strips_whitespace_from_segments(self, mock_model_class):
        """Whisper segments often have leading spaces -- these should be handled."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Run 1: leading/trailing whitespace
        mock_model.transcribe.return_value = ([make_mock_segment("  hello  ")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Run 2: same
        mock_model.transcribe.return_value = ([make_mock_segment("  hello  ")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "hello"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_empty_segments_handled(self, mock_model_class):
        """Empty or whitespace-only segments should not produce spurious results."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Empty segment list
        mock_model.transcribe.return_value = ([], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_sentence_start_lowercased(self, mock_model_class):
        """First character of output should be lowercased (sentence-start normalization)."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Run 1
        mock_model.transcribe.return_value = ([make_mock_segment("Hello World")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Run 2
        mock_model.transcribe.return_value = ([make_mock_segment("Hello World")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # wh-first-char-lowercase changed this row from "hello World".
        # The capital on "World" is evidence that "Hello" was not
        # capitalized merely because it opens the utterance, so the H
        # survives. The unconditional lowercasing this row used to pin is
        # still pinned, by
        # TestCapitalEvidenceInExtractText::
        # test_a_lowercase_second_word_still_lowercases_the_first in
        # test_whisper_engine_text_rules.py, whose second word is
        # lowercase and therefore supplies no evidence.
        assert engine.get_result() == "Hello World"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_proper_nouns_preserved(self, mock_model_class):
        """Proper noun capitalization should be preserved (only first char lowercased)."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        mock_model.transcribe.return_value = ([make_mock_segment("Open Google Chrome")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        mock_model.transcribe.return_value = ([make_mock_segment("Open Google Chrome")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # wh-first-char-lowercase changed this row from
        # "open Google Chrome". "Google" is capitalized, so the condition
        # keeps the O as the model wrote it. The row's own subject --
        # proper nouns surviving -- holds more completely than before.
        assert engine.get_result() == "Open Google Chrome"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_punctuation_stripped(self, mock_model_class):
        """Punctuation should be stripped from output."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        mock_model.transcribe.return_value = ([make_mock_segment("Hello, world!")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        mock_model.transcribe.return_value = ([make_mock_segment("Hello, world!")], Mock())
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "hello world"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_decimal_periods_preserved(self, mock_model_class):
        """Periods after digits should be preserved (decimal numbers)."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        mock_model.transcribe.return_value = (
            [make_mock_segment("Set volume to 3.14 please.")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        mock_model.transcribe.return_value = (
            [make_mock_segment("Set volume to 3.14 please.")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # 3.14 period kept (digit before it), trailing period removed
        assert engine.get_result() == "set volume to 3.14 please"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_trailing_period_after_digit_stripped(self, mock_model_class):
        """Trailing period after a digit should be stripped (sentence-ending, not decimal)."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        mock_model.transcribe.return_value = (
            [make_mock_segment("Item 7.")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        mock_model.transcribe.return_value = (
            [make_mock_segment("Item 7.")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "item 7"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_pronoun_i_always_capitalized(self, mock_model_class):
        """The pronoun 'I' and its contractions should always be capitalized."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # "I'm" at sentence start -- first-char lowercasing would make it "i'm"
        # but the I-capitalization rule should restore it to "I'm"
        mock_model.transcribe.return_value = (
            [make_mock_segment("I'm going to the store")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        mock_model.transcribe.return_value = (
            [make_mock_segment("I'm going to the store")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "I'm going to the store"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_pronoun_i_contractions_capitalized(self, mock_model_class):
        """I've, I'd, I'll should all be capitalized."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        mock_model.transcribe.return_value = (
            [make_mock_segment("I've been told I'd like it and I'll try")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        mock_model.transcribe.return_value = (
            [make_mock_segment("I've been told I'd like it and I'll try")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "I've been told I'd like it and I'll try"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_i_in_words_not_capitalized(self, mock_model_class):
        """The letter 'i' inside words should NOT be capitalized."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        mock_model.transcribe.return_value = (
            [make_mock_segment("Time is big")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        mock_model.transcribe.return_value = (
            [make_mock_segment("Time is big")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "time is big"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_spelled_out_word_converted(self, mock_model_class):
        """V-O-X-T-R-A-L should become 'voxtral'."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        mock_model.transcribe.return_value = (
            [make_mock_segment("The word is V-O-X-T-R-A-L")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        mock_model.transcribe.return_value = (
            [make_mock_segment("The word is V-O-X-T-R-A-L")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "the word is voxtral"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_spelled_out_three_letters_minimum(self, mock_model_class):
        """A-B-C (3 letters) should be converted, A-B (2 letters) should not."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        mock_model.transcribe.return_value = (
            [make_mock_segment("Type A-B-C not A-B")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        mock_model.transcribe.return_value = (
            [make_mock_segment("Type A-B-C not A-B")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "type abc not A-B"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_spelled_out_word_in_sentence(self, mock_model_class):
        """Spelled-out word at sentence start should still be lowercased."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        mock_model.transcribe.return_value = (
            [make_mock_segment("S-O-N-O-S is great")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        mock_model.transcribe.return_value = (
            [make_mock_segment("S-O-N-O-S is great")], Mock()
        )
        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        assert engine.get_result() == "sonos is great"


class TestTranscribeCallParameters:
    """Tests for the parameters passed to WhisperModel.transcribe()."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_transcribe_receives_concatenated_buffer(self, mock_model_class):
        """transcribe() should receive the full concatenated audio buffer."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            beam_size=5,
            language="en",
        )

        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        # Verify transcribe was called with a numpy array
        args, kwargs = mock_model.transcribe.call_args
        audio_arg = args[0]
        assert isinstance(audio_arg, np.ndarray)
        assert audio_arg.dtype == np.float32

        # Total samples should match all chunks fed
        expected_samples = CHUNKS_FOR_INFERENCE * int(16000 * 30 / 1000)
        assert len(audio_arg) == expected_samples

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_transcribe_passes_language_and_beam_size(self, mock_model_class):
        """transcribe() should pass configured language and beam_size."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            beam_size=3,
            language="en",
        )

        feed_audio(engine, CHUNKS_FOR_INFERENCE)

        _, kwargs = mock_model.transcribe.call_args
        assert kwargs["language"] == "en"
        assert kwargs["beam_size"] == 3


class TestMaxBufferCap:
    """Tests for max buffer duration safety cap."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_max_buffer_forces_finalization(self, mock_model_class):
        """When buffer exceeds max duration, engine should force finalization."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello world")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            max_buffer_duration_s=1.0,
        )

        # Feed 1.1 seconds of speech (37 * 30ms = 1110ms > 1000ms max)
        feed_audio(engine, 37, rms=0.1)

        assert engine.is_endpoint() is True
        assert engine.get_result() == "hello world"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_buffer_within_max_not_finalized(self, mock_model_class):
        """Buffer under max duration should not trigger forced finalization."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            max_buffer_duration_s=30.0,
        )

        feed_audio(engine, CHUNKS_FOR_INFERENCE, rms=0.1)

        assert engine.is_endpoint() is False


class TestWhisperEngineCleanup:
    """Tests for explicit CUDA resource cleanup."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_cleanup_deletes_model(self, mock_model_class):
        """cleanup() should explicitly delete the WhisperModel.

        CTranslate2 CUDA cleanup can crash (0xC0000409) when left to Python GC
        during process exit. Explicit cleanup ensures orderly CUDA teardown.
        """
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine(
            model_size_or_path="tiny",
            device="cpu",
        )

        assert engine._model is not None
        engine.cleanup()
        assert engine._model is None

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_cleanup_is_idempotent(self, mock_model_class):
        """cleanup() can be called multiple times safely."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine(
            model_size_or_path="tiny",
            device="cpu",
        )

        engine.cleanup()
        engine.cleanup()  # Should not raise
        assert engine._model is None


class TestFinalize:
    """Tests for public finalize() method."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_finalize_promotes_all_words(self, mock_model_class):
        """finalize() should run final inference and promote all words."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello world")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        # Feed speech and run one inference
        feed_audio(engine, CHUNKS_FOR_INFERENCE, rms=0.1)
        # Only 1 run, no confirmation yet
        assert engine.get_result() == ""

        engine.finalize()

        assert engine.is_endpoint() is True
        assert engine.get_result() == "hello world"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_finalize_noop_if_already_finalized(self, mock_model_class):
        """finalize() should not call transcribe again if already finalized."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        feed_audio(engine, CHUNKS_FOR_INFERENCE, rms=0.1)
        engine.finalize()
        call_count = mock_model.transcribe.call_count

        engine.finalize()  # Should be no-op

        assert mock_model.transcribe.call_count == call_count

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_finalize_noop_if_no_audio(self, mock_model_class):
        """finalize() should be a no-op if no audio has been buffered."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        engine.finalize()

        mock_model.transcribe.assert_not_called()
        assert engine.is_endpoint() is False


def make_mock_segment_with_conf(
    text: str,
    avg_logprob: float = -0.2,
    no_speech_prob: float = 0.01,
    compression_ratio: float = 0.5,
    start: float = 0.0,
    end: float = 1.0,
    words=None,
):
    """Mock segment with per-segment confidence attributes used by wh-7ou.2.

    words: list of word mocks (see make_mock_word) as returned by
    faster-whisper when word_timestamps=True; None otherwise (the real
    library's default).
    """
    segment = Mock()
    segment.text = text
    segment.avg_logprob = avg_logprob
    segment.no_speech_prob = no_speech_prob
    segment.compression_ratio = compression_ratio
    segment.start = start
    segment.end = end
    segment.words = words
    return segment


def make_mock_word(word: str, probability: float, start: float = 0.0, end: float = 0.5):
    """Mock faster-whisper Word (word_timestamps=True output) for wh-7ou.6."""
    w = Mock()
    w.word = word
    w.probability = probability
    w.start = start
    w.end = end
    return w


class TestHallucinationFilter:
    """wh-7ou.2: peak-avg_logprob-based filter that suppresses final transcripts
    when Whisper's confidence never reached the threshold across the utterance.

    Calibration data (2026-04-19, 11 utterances on distil_medium_en):
    - Hallucinations: peak avg_logprob in [-0.86, -0.63]
    - Dictated speech: peak avg_logprob in [-0.40, -0.17]
    Default threshold -0.5 sits in the clean gap.
    """

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_peak_starts_at_neg_infinity(self, mock_model_class):
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine()
        assert engine._peak_avg_logprob == -float("inf")

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_peak_updates_to_max_over_segments(self, mock_model_class):
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine()
        engine._extract_text([
            make_mock_segment_with_conf("hello", avg_logprob=-0.8),
            make_mock_segment_with_conf(" world", avg_logprob=-0.3),
            make_mock_segment_with_conf("!", avg_logprob=-1.1),
        ])

        # Peak is the LEAST negative (highest) avg_logprob seen
        assert engine._peak_avg_logprob == -0.3

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_peak_resets_on_reset(self, mock_model_class):
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine()
        engine._extract_text([make_mock_segment_with_conf("hi", avg_logprob=-0.1)])
        assert engine._peak_avg_logprob == -0.1

        engine.reset()
        assert engine._peak_avg_logprob == -float("inf")

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_filter_suppresses_final_when_peak_below_threshold(self, mock_model_class):
        """Hallucination path: every segment scored below -0.5 threshold."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        # Simulate a final inference returning a hallucinated transcript with
        # avg_logprob matching the real-world distribution (-0.8 is textbook
        # "thank you" hallucination territory).
        hallucinated_seg = make_mock_segment_with_conf("Thank you.", avg_logprob=-0.8)
        mock_model.transcribe.return_value = ([hallucinated_seg], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            hallucination_logprob_threshold=-0.5,
        )
        # Seed buffer and force a final inference
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine._finalized is True
        assert engine._confirmed_words == []
        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_filter_passes_through_when_peak_above_threshold(self, mock_model_class):
        """Dictation path: a high-confidence segment clears the threshold."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        # Real dictated "thank you" typically hits ~-0.25
        real_seg = make_mock_segment_with_conf("Thank you.", avg_logprob=-0.25)
        mock_model.transcribe.return_value = ([real_seg], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            hallucination_logprob_threshold=-0.5,
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine._finalized is True
        assert engine._confirmed_words == ["thank", "you"]
        assert engine.get_result() == "thank you"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_interim_high_confidence_saves_utterance(self, mock_model_class):
        """An earlier interim inference reached high confidence; the final's
        own segments drifted low (tail-of-utterance effect). The peak tracker
        ensures the utterance is still kept because ANY earlier run was
        confident. This mirrors the real-world 'yes' case from calibration
        (UTT-5: early seg -0.342, final seg -0.968).
        """
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            hallucination_logprob_threshold=-0.5,
        )
        # Simulate peak from an earlier interim
        engine._extract_text([make_mock_segment_with_conf(" Yes.", avg_logprob=-0.342)])
        assert engine._peak_avg_logprob == -0.342

        # Final inference returns a low-confidence segment for the same word
        mock_model.transcribe.return_value = (
            [make_mock_segment_with_conf(" Yes", avg_logprob=-0.968)],
            Mock(),
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        # Kept because the peak across the utterance was -0.342 (> -0.5)
        assert engine.get_result() == "yes"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_threshold_is_configurable(self, mock_model_class):
        """A more permissive threshold should let borderline transcripts through."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf("Okay.", avg_logprob=-0.7)
        mock_model.transcribe.return_value = ([seg], Mock())

        # With a very permissive threshold (-2.0), even -0.7 peak clears it
        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            hallucination_logprob_threshold=-2.0,
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == "okay"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_filter_disabled_at_negative_infinity(self, mock_model_class):
        """Sentinel value to disable the filter entirely."""
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf("mhm.", avg_logprob=-9.9)
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            hallucination_logprob_threshold=-float("inf"),
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        # Nothing can be below -inf, so filter never fires
        assert engine.get_result() == "mhm"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_interim_stability_held_back_below_threshold(self, mock_model_class):
        """STABLE interim promotion must be gated on peak_avg_logprob >= threshold.

        Without this guard, _update_stability would promote LocalAgreement-2
        confirmed words during re-inference, and the STABLE WS message would
        fire BEFORE _run_final_inference can apply the hallucination filter.
        This was observed in live testing (2026-04-19 17:49:01-29): three
        hallucinated throat-clears leaked 'thank you', 'a', 'thank you' via
        interim STABLE before their finals were suppressed.
        """
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine(
            hallucination_logprob_threshold=-0.5,
        )

        # Simulate two consecutive low-confidence inferences returning the
        # same hallucinated text. LocalAgreement-2 would normally promote
        # "thank you" to _confirmed_words on the 2nd run.
        engine._extract_text([make_mock_segment_with_conf(" Thank you.", avg_logprob=-0.85)])
        engine._update_stability(["thank", "you"])
        engine._extract_text([make_mock_segment_with_conf(" Thank you.", avg_logprob=-0.85)])
        engine._update_stability(["thank", "you"])

        # Peak is -0.85 (below -0.5 threshold) -> no confirmed-word leak
        assert engine._confirmed_words == []
        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_interim_stability_promotes_after_threshold_crossed(self, mock_model_class):
        """Once peak crosses threshold mid-utterance, LocalAgreement-2 resumes.

        Real-world case: user starts quiet, ramps up. Early interims stay
        below threshold; once a confident inference lands, subsequent
        interims promote normally.
        """
        from shared_stt.whisper_engine import WhisperStreamingEngine

        engine = WhisperStreamingEngine(
            hallucination_logprob_threshold=-0.5,
        )

        # Early low-confidence inference: no promotion
        engine._extract_text([make_mock_segment_with_conf(" Hello", avg_logprob=-0.85)])
        engine._update_stability(["hello"])
        assert engine._confirmed_words == []

        # Confidence jumps above threshold; _prev_words was tracked during
        # the low-confidence run so LCP has a reference for the next run.
        engine._extract_text([make_mock_segment_with_conf(" Hello world", avg_logprob=-0.20)])
        engine._update_stability(["hello", "world"])

        # "hello" appears in both inferences -> promoted
        assert engine._confirmed_words == ["hello"]


class TestHotwords:
    """wh-apmg: the hints feature was a silent no-op for this engine --
    hints.txt was appended to and never read. The engine now accepts a
    hotwords string and must pass it to every model.transcribe call so
    faster-whisper biases the decoder toward the user's words."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_hotwords_passed_to_interim_inference(self, mock_model_class):
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment(" zwicky")], Mock())
        engine = WhisperStreamingEngine(hotwords="Zwicky, WheelHouse")
        engine.process_audio(make_audio_chunk(30))
        engine._run_inference()
        assert mock_model.transcribe.call_args.kwargs["hotwords"] == "Zwicky, WheelHouse"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_hotwords_passed_to_final_inference(self, mock_model_class):
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment(" zwicky")], Mock())
        engine = WhisperStreamingEngine(hotwords="Zwicky")
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()
        assert mock_model.transcribe.call_args.kwargs["hotwords"] == "Zwicky"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_default_is_no_hotwords(self, mock_model_class):
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment(" hi")], Mock())
        engine = WhisperStreamingEngine()
        engine.process_audio(make_audio_chunk(30))
        engine._run_inference()
        assert mock_model.transcribe.call_args.kwargs["hotwords"] is None

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_empty_string_normalized_to_none(self, mock_model_class):
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment(" hi")], Mock())
        engine = WhisperStreamingEngine(hotwords="")
        engine.process_audio(make_audio_chunk(30))
        engine._run_inference()
        assert mock_model.transcribe.call_args.kwargs["hotwords"] is None

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_active_hotwords_logged_at_init(self, mock_model_class, caplog):
        # wh-apmg.1.3: the wh-7ou.2 hallucination-filter calibration
        # predates hotwords. Until recalibrated, a field report of
        # resumed hallucination leakage must be correlatable with
        # hotwords being active -- so init logs the fact once.
        import logging
        from shared_stt.whisper_engine import WhisperStreamingEngine

        hotwords = "Zwicky, WheelHouse"
        with caplog.at_level(logging.INFO):
            WhisperStreamingEngine(hotwords=hotwords)
        assert any(
            "hotwords active" in r.message and str(len(hotwords)) in r.message
            for r in caplog.records
        )

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_no_hotwords_no_init_log(self, mock_model_class, caplog):
        import logging
        from shared_stt.whisper_engine import WhisperStreamingEngine

        with caplog.at_level(logging.INFO):
            WhisperStreamingEngine()
        assert not any("hotwords active" in r.message for r in caplog.records)


class TestSingleWordRescue:
    """wh-7ou.6: the peak-avg_logprob filter suppresses real single-word
    utterances ('comma' -0.596/-0.630/-0.609 vs the -0.55 threshold, field
    data 2026-08-06) because the segment-average confidence is systematically
    lower on one-word utterances than on phrases, even for the voice the
    threshold was calibrated on.

    Fix: when the filter would suppress AND the cleaned transcript is exactly
    one word, give a second chance using two signals from the FINAL inference:
    - min per-word probability (faster-whisper word_timestamps=True) must be
      >= single_word_min_probability (default 0.6)
    - max segment no_speech_prob must be <= single_word_max_no_speech_prob
      (default 0.03; field data: real one-word finals 0.008-0.017,
      cough-driven 'Thank you.' hallucinations 0.044-0.089)
    Missing word data keeps the suppression (default to refusing).
    """

    def _engine(self, mock_model_class, **kwargs: Any):
        from shared_stt.whisper_engine import WhisperStreamingEngine

        defaults: dict[str, Any] = dict(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            hallucination_logprob_threshold=-0.55,
        )
        defaults.update(kwargs)
        return WhisperStreamingEngine(**defaults)

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_single_word_confident_low_no_speech_rescued(self, mock_model_class):
        """The 'comma' case: low segment average, but the word itself decoded
        confidently on near-certain speech audio -> transcript is kept."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[make_mock_word(" Comma.", probability=0.85)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine._finalized is True
        assert engine.get_result() == "comma"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_single_word_high_no_speech_stays_suppressed(self, mock_model_class):
        """The 'hmm' case: confident word but the audio was not clearly
        speech (no_speech_prob in the hallucination range) -> suppressed."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " hmm.",
            avg_logprob=-0.95,
            no_speech_prob=0.048,
            words=[make_mock_word(" hmm.", probability=0.9)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_single_word_low_word_probability_stays_suppressed(self, mock_model_class):
        """Clear speech audio but the model was unsure of the word -> suppressed."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Kamo.",
            avg_logprob=-0.58,
            no_speech_prob=0.010,
            words=[make_mock_word(" Kamo.", probability=0.3)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_multi_word_never_rescued(self, mock_model_class):
        """'Thank you.' with confident words and low no_speech must STAY
        suppressed: the rescue applies to single-word transcripts only."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Thank you.",
            avg_logprob=-0.70,
            no_speech_prob=0.010,
            words=[
                make_mock_word(" Thank", probability=0.95),
                make_mock_word(" you.", probability=0.95),
            ],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_no_word_data_keeps_suppression(self, mock_model_class):
        """words=None (word timestamps unavailable) -> no rescue, no crash."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=None,
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_rescue_disabled_by_min_probability_above_one(self, mock_model_class):
        """single_word_min_probability=2.0 disables the rescue entirely
        (word probabilities never exceed 1.0)."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.001,
            words=[make_mock_word(" Comma.", probability=0.99)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class, single_word_min_probability=2.0)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_thresholds_configurable(self, mock_model_class):
        """Both rescue thresholds are constructor/config parameters."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.045,
            words=[make_mock_word(" Comma.", probability=0.55)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        # Defaults would refuse (0.55 < 0.6, 0.045 > 0.03); widened
        # thresholds accept.
        engine = self._engine(
            mock_model_class,
            single_word_min_probability=0.5,
            single_word_max_no_speech_prob=0.05,
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == "comma"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_word_timestamps_only_on_final_inference(self, mock_model_class):
        """Interim passes must stay cheap: word_timestamps is requested only
        by the final inference, where the rescue reads per-word data."""
        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))

        engine._run_inference()
        interim_kwargs = mock_model.transcribe.call_args.kwargs
        assert "word_timestamps" not in interim_kwargs

        engine._run_final_inference()
        final_kwargs = mock_model.transcribe.call_args.kwargs
        assert final_kwargs["word_timestamps"] is True

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_accept_path_does_not_need_word_data(self, mock_model_class):
        """A confident single word (peak above threshold) is accepted without
        ever consulting word-level data."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.20,
            no_speech_prob=0.010,
            words=None,
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == "comma"

    # -- wh-7ou.6.1.1: non-finite / out-of-range confidence values ----------

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_nan_word_probability_after_finite_stays_suppressed(self, mock_model_class):
        """min() skips a NaN that follows a finite value, so without explicit
        validation a NaN word probability would inherit the finite word's
        pass. Malformed data must keep the suppression."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[
                make_mock_word(" Comma.", probability=0.9),
                make_mock_word(".", probability=float("nan")),
            ],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine._finalized is True
        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_nan_word_probability_first_stays_suppressed(self, mock_model_class):
        """NaN in first position: min() returns the NaN and the comparison
        refuses, but the validation must refuse explicitly either way."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[
                make_mock_word(" Comma.", probability=float("nan")),
                make_mock_word(".", probability=0.9),
            ],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_nan_no_speech_prob_after_finite_stays_suppressed(self, mock_model_class):
        """max() skips a NaN that follows a finite value, so a NaN
        no_speech_prob on a later segment would inherit the earlier
        segment's pass. Malformed data must keep the suppression."""
        mock_model = mock_model_class.return_value
        seg1 = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[make_mock_word(" Comma.", probability=0.9)],
        )
        seg2 = make_mock_segment_with_conf(
            ".",
            avg_logprob=-0.63,
            no_speech_prob=float("nan"),
            words=None,
        )
        mock_model.transcribe.return_value = ([seg1, seg2], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_infinite_word_probability_stays_suppressed(self, mock_model_class):
        """A positive-infinite word probability satisfies min_prob >= 0.6 but
        is not a probability; it must keep the suppression."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[make_mock_word(" Comma.", probability=float("inf"))],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_negative_no_speech_prob_stays_suppressed(self, mock_model_class):
        """A negative no_speech_prob satisfies max_no_speech <= 0.03 but is
        not a probability; it must keep the suppression."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=-1.0,
            words=[make_mock_word(" Comma.", probability=0.9)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    # -- wh-7ou.6.1.2: malformed threshold config must not crash ------------

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_quoted_toml_threshold_string_is_coerced(self, mock_model_class):
        """A quoted TOML number ('0.5' instead of 0.5) reaches the
        constructor as a string; it must be read as the number, not crash
        the final inference."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.045,
            words=[make_mock_word(" Comma.", probability=0.55)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(
            mock_model_class,
            single_word_min_probability="0.5",
            single_word_max_no_speech_prob="0.05",
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine._finalized is True
        assert engine.get_result() == "comma"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_unreadable_threshold_falls_back_to_default(self, mock_model_class):
        """A config value float() cannot read (a list, garbage text) falls
        back to the documented default with a warning instead of raising a
        TypeError mid-utterance. Defaults 0.6/0.03 rescue the calibrated
        'comma' shape."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[make_mock_word(" Comma.", probability=0.85)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(
            mock_model_class,
            single_word_min_probability=[0.6],
            single_word_max_no_speech_prob="not-a-number",
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine._finalized is True
        assert engine.get_result() == "comma"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_unreadable_hallucination_threshold_falls_back(self, mock_model_class):
        """Same defect family: hallucination_logprob_threshold is also
        compared mid-utterance, so an unreadable value must fall back to its
        default (-0.5), not raise at final inference. Peak -0.20 clears the
        default threshold, so the transcript is accepted."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.20,
            no_speech_prob=0.010,
            words=None,
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(
            mock_model_class, hallucination_logprob_threshold={"bad": 1}
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine._finalized is True
        assert engine.get_result() == "comma"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_neg_infinite_hallucination_threshold_still_disables_filter(
        self, mock_model_class
    ):
        """-inf is the documented off-switch for the hallucination filter and
        must survive the defensive coercion."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-5.0,
            no_speech_prob=0.010,
            words=None,
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(
            mock_model_class, hallucination_logprob_threshold=-float("inf")
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == "comma"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_rescue_comparison_contained_against_bad_attribute(self, mock_model_class):
        """Belt and suspenders: even if a non-numeric threshold reaches the
        comparison (attribute poked after construction), the rescue refuses
        instead of crashing the final inference."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[make_mock_word(" Comma.", probability=0.85)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine._single_word_min_probability = "0.6"  # type: ignore[assignment]  # bypasses coercion
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine._finalized is True
        assert engine.get_result() == ""

    # -- wh-7ou.6.1.7: TOML booleans must not become thresholds -------------

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_boolean_rescue_thresholds_fall_back_to_defaults(self, mock_model_class):
        """TOML false/true reach the constructor as bool; float() would turn
        them into 0.0/1.0, silently turning OFF both rescue criteria. They
        must fall back to the defaults instead: word prob 0.3 refuses under
        the default 0.6 but would pass a bool-derived 0.0."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Kamo.",
            avg_logprob=-0.58,
            no_speech_prob=0.010,
            words=[make_mock_word(" Kamo.", probability=0.3)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(
            mock_model_class,
            single_word_min_probability=False,
            single_word_max_no_speech_prob=True,
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_boolean_hallucination_threshold_falls_back(self, mock_model_class):
        """hallucination_logprob_threshold=false would float to 0.0 and
        suppress every ordinary negative-logprob final. It must fall back to
        the default -0.5, which accepts a peak of -0.20."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.20,
            no_speech_prob=0.010,
            words=None,
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(
            mock_model_class, hallucination_logprob_threshold=False
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == "comma"

    # -- wh-7ou.6.1.6: only NEGATIVE infinity is the documented off-switch --

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_positive_infinity_hallucination_threshold_falls_back(
        self, mock_model_class
    ):
        """TOML inf parses POSITIVE; peak < +inf is always true, which would
        silently suppress every final. Only -inf (the documented off-switch)
        may pass; +inf falls back to the default -0.5, which accepts -0.20."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.20,
            no_speech_prob=0.010,
            words=None,
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(
            mock_model_class, hallucination_logprob_threshold=float("inf")
        )
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == "comma"

    # -- wh-7ou.6.1.8: oversized TOML integers must not crash construction --

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_oversized_toml_integers_fall_back_to_defaults(self, mock_model_class):
        """A valid 400-digit TOML integer parses as int; float() raises
        OverflowError, which must be treated like any other unreadable value
        (fall back with a warning), not crash provider startup."""
        engine = self._engine(
            mock_model_class,
            single_word_min_probability=10**400,
            single_word_max_no_speech_prob=10**400,
            hallucination_logprob_threshold=-(10**400),
        )

        assert engine._single_word_min_probability == 0.6
        assert engine._single_word_max_no_speech_prob == 0.03
        assert engine._hallucination_logprob_threshold == -0.5

    # -- wh-7ou.6.1.4: real transcribe returns a one-shot iterable ----------

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_rescue_works_when_transcribe_returns_one_shot_iterator(
        self, mock_model_class
    ):
        """faster-whisper's transcribe returns a generator, not a list. The
        rescue re-reads segments after _extract_text consumed them, so the
        final path must materialize the iterable first. A replayable-list
        mock cannot catch losing that materialization; this one can."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[make_mock_word(" Comma.", probability=0.85)],
        )
        mock_model.transcribe.return_value = (iter([seg]), Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine._finalized is True
        assert engine.get_result() == "comma"


class TestFinalConfidenceBlock:
    """wh-7ou.7.1.1: every final inference computes a measurement block --
    lowest word probability, highest segment no_speech_prob, peak avg_logprob,
    word count, and the suppressed/rescued outcome -- exposed via
    get_final_confidence() so AudioProcessor can attach it to the final
    WebSocket message as the optional "confidence" object. Fields are null
    when the underlying data is missing or malformed (the same refusal
    discipline as the single-word rescue)."""

    EXPECTED_KEYS = {
        "min_word_probability",
        "max_no_speech_prob",
        "peak_avg_logprob",
        "word_count",
        "suppressed",
        "rescued",
    }

    def _engine(self, mock_model_class, **kwargs: Any):
        from shared_stt.whisper_engine import WhisperStreamingEngine

        defaults: dict[str, Any] = dict(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            hallucination_logprob_threshold=-0.55,
        )
        defaults.update(kwargs)
        return WhisperStreamingEngine(**defaults)

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_none_before_any_final(self, mock_model_class):
        """No block exists until a final inference has run."""
        engine = self._engine(mock_model_class)
        assert engine.get_final_confidence() is None

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_accepted_final_reports_measurements(self, mock_model_class):
        """An ordinary accepted final carries the full block: it is attached
        to every final, not only suppressed or rescued ones."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Hello world.",
            avg_logprob=-0.2,
            no_speech_prob=0.012,
            words=[
                make_mock_word(" Hello", probability=0.91),
                make_mock_word(" world.", probability=0.85),
            ],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        block = engine.get_final_confidence()
        assert block is not None
        assert set(block.keys()) == self.EXPECTED_KEYS
        assert block["min_word_probability"] == pytest.approx(0.85)
        assert block["max_no_speech_prob"] == pytest.approx(0.012)
        assert block["peak_avg_logprob"] == pytest.approx(-0.2)
        assert block["word_count"] == 2
        assert block["suppressed"] is False
        assert block["rescued"] is False

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_measurements_span_all_segments(self, mock_model_class):
        """min word probability and max no_speech_prob aggregate across all
        segments of the final inference, not just the first."""
        mock_model = mock_model_class.return_value
        seg1 = make_mock_segment_with_conf(
            " Hello",
            avg_logprob=-0.3,
            no_speech_prob=0.01,
            words=[make_mock_word(" Hello", probability=0.91)],
        )
        seg2 = make_mock_segment_with_conf(
            " world.",
            avg_logprob=-0.2,
            no_speech_prob=0.04,
            words=[make_mock_word(" world.", probability=0.62)],
        )
        mock_model.transcribe.return_value = ([seg1, seg2], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        block = engine.get_final_confidence()
        assert block["min_word_probability"] == pytest.approx(0.62)
        assert block["max_no_speech_prob"] == pytest.approx(0.04)
        assert block["peak_avg_logprob"] == pytest.approx(-0.2)

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_rescued_single_word_reports_rescued(self, mock_model_class):
        """The 'comma' shape: rescued finals say rescued=True, suppressed=False."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[make_mock_word(" Comma.", probability=0.85)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        block = engine.get_final_confidence()
        assert block["suppressed"] is False
        assert block["rescued"] is True
        assert block["min_word_probability"] == pytest.approx(0.85)
        assert block["max_no_speech_prob"] == pytest.approx(0.014)
        assert block["peak_avg_logprob"] == pytest.approx(-0.63)
        assert block["word_count"] == 1

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_suppressed_final_reports_suppressed(self, mock_model_class):
        """A hallucination-filtered final says suppressed=True, rescued=False,
        and still carries the measurement numbers."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Thank you.",
            avg_logprob=-0.8,
            no_speech_prob=0.05,
            words=[
                make_mock_word(" Thank", probability=0.95),
                make_mock_word(" you.", probability=0.95),
            ],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        block = engine.get_final_confidence()
        assert block["suppressed"] is True
        assert block["rescued"] is False
        assert block["min_word_probability"] == pytest.approx(0.95)
        assert block["max_no_speech_prob"] == pytest.approx(0.05)
        assert block["word_count"] == 2

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_missing_word_data_reports_null_min_word_probability(
        self, mock_model_class
    ):
        """words=None (no word-level data) -> min_word_probability is None;
        the segment-level fields are still reported."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Hello world.",
            avg_logprob=-0.2,
            no_speech_prob=0.012,
            words=None,
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        block = engine.get_final_confidence()
        assert block["min_word_probability"] is None
        assert block["max_no_speech_prob"] == pytest.approx(0.012)
        assert block["peak_avg_logprob"] == pytest.approx(-0.2)
        assert block["word_count"] == 2

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_malformed_word_probability_reports_null(self, mock_model_class):
        """A NaN word probability is malformed data: report null rather than
        a min() that silently skipped the NaN."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[
                make_mock_word(" Comma.", probability=0.9),
                make_mock_word(".", probability=float("nan")),
            ],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        block = engine.get_final_confidence()
        assert block["min_word_probability"] is None
        assert block["max_no_speech_prob"] == pytest.approx(0.014)

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_malformed_no_speech_prob_reports_null(self, mock_model_class):
        """An out-of-range no_speech_prob (negative) is malformed: null."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=-1.0,
            words=[make_mock_word(" Comma.", probability=0.9)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        block = engine.get_final_confidence()
        assert block["max_no_speech_prob"] is None
        assert block["min_word_probability"] == pytest.approx(0.9)

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_empty_final_reports_nulls_and_zero_word_count(self, mock_model_class):
        """An empty final (no segments) still produces a block: all nulls,
        word_count 0, and peak_avg_logprob null because the peak tracker
        never left its -inf sentinel (which is not JSON-serializable)."""
        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        block = engine.get_final_confidence()
        assert block is not None
        assert block["min_word_probability"] is None
        assert block["max_no_speech_prob"] is None
        assert block["peak_avg_logprob"] is None
        assert block["word_count"] == 0
        assert block["rescued"] is False

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_reset_clears_block(self, mock_model_class):
        """reset() clears the block so a stale utterance's numbers can never
        be attached to the next utterance's final."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Hello world.", avg_logprob=-0.2, no_speech_prob=0.012, words=None
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()
        assert engine.get_final_confidence() is not None

        engine.reset()
        assert engine.get_final_confidence() is None

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_block_recomputed_for_each_final(self, mock_model_class):
        """Every final gets a fresh block reflecting its own inference."""
        mock_model = mock_model_class.return_value
        seg1 = make_mock_segment_with_conf(
            " Hello world.", avg_logprob=-0.2, no_speech_prob=0.012, words=None
        )
        mock_model.transcribe.return_value = ([seg1], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()
        assert engine.get_final_confidence()["word_count"] == 2

        engine.reset()

        seg2 = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[make_mock_word(" Comma.", probability=0.85)],
        )
        mock_model.transcribe.return_value = ([seg2], Mock())
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        block = engine.get_final_confidence()
        assert block["word_count"] == 1
        assert block["rescued"] is True


class TestCalibrationMode:
    """wh-7ou.7.1.2 (spec Section 5.2): while calibration mode is enabled the
    engine skips hallucination suppression at final inference, so every final
    arrives with its text -- a cough's invented text is exactly the noise
    sample calibration wants. The confidence block still reports the filter's
    verdict (suppressed=True) so the Logic process can see what would have
    happened. Safety: the mode resets to off after a ten-minute internal
    timeout (the WebSocket-disconnect reset lives in WSForwarder), so a
    crashed Logic process can never leave the filter disabled."""

    def _engine(self, mock_model_class, **kwargs: Any):
        from shared_stt.whisper_engine import WhisperStreamingEngine

        defaults: dict[str, Any] = dict(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
            hallucination_logprob_threshold=-0.55,
        )
        defaults.update(kwargs)
        return WhisperStreamingEngine(**defaults)

    def _hallucinated_segment(self):
        """A cough-driven 'Thank you.' final: peak below the threshold, high
        no_speech_prob, multi-word (so the single-word rescue never applies).
        Without calibration mode this is always suppressed."""
        return make_mock_segment_with_conf(
            " Thank you.",
            avg_logprob=-0.8,
            no_speech_prob=0.05,
            words=[
                make_mock_word(" Thank", probability=0.95),
                make_mock_word(" you.", probability=0.95),
            ],
        )

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_bypass_delivers_would_be_suppressed_final(self, mock_model_class):
        """While enabled, a final the filter would suppress keeps its text,
        and the confidence block still reports the filter's verdict with the
        measurement numbers the noise stage needs."""
        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([self._hallucinated_segment()], Mock())

        engine = self._engine(mock_model_class)
        engine.set_calibration_mode(True)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine._finalized is True
        assert engine.get_result() == "thank you"
        block = engine.get_final_confidence()
        assert block["suppressed"] is True
        assert block["rescued"] is False
        assert block["min_word_probability"] == pytest.approx(0.95)
        assert block["max_no_speech_prob"] == pytest.approx(0.05)
        assert block["word_count"] == 2

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_off_by_default_suppression_unchanged(self, mock_model_class):
        """An engine that never saw set_calibration_mode suppresses exactly
        as before."""
        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([self._hallucinated_segment()], Mock())

        engine = self._engine(mock_model_class)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_disable_restores_suppression(self, mock_model_class):
        """set_calibration_mode(False) turns the bypass off again (the path
        the WSForwarder disconnect reset drives)."""
        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([self._hallucinated_segment()], Mock())

        engine = self._engine(mock_model_class)
        engine.set_calibration_mode(True)
        engine.set_calibration_mode(False)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == ""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_confident_final_unaffected(self, mock_model_class):
        """A final that clears the threshold behaves identically with the
        mode on: delivered, suppressed=False."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Hello world.", avg_logprob=-0.2, no_speech_prob=0.012, words=None
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.set_calibration_mode(True)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == "hello world"
        block = engine.get_final_confidence()
        assert block["suppressed"] is False
        assert block["rescued"] is False

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_rescued_single_word_still_reports_rescued(self, mock_model_class):
        """The single-word rescue still runs first, so a rescued 'comma'
        reports rescued=True (not suppressed=True) during calibration --
        the flags stay mutually exclusive and honest."""
        mock_model = mock_model_class.return_value
        seg = make_mock_segment_with_conf(
            " Comma.",
            avg_logprob=-0.63,
            no_speech_prob=0.014,
            words=[make_mock_word(" Comma.", probability=0.85)],
        )
        mock_model.transcribe.return_value = ([seg], Mock())

        engine = self._engine(mock_model_class)
        engine.set_calibration_mode(True)
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == "comma"
        block = engine.get_final_confidence()
        assert block["rescued"] is True
        assert block["suppressed"] is False

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_mode_survives_per_utterance_reset(self, mock_model_class):
        """reset() runs between every utterance; the calibration session
        spans many utterances, so the mode must survive it."""
        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([self._hallucinated_segment()], Mock())

        engine = self._engine(mock_model_class)
        engine.set_calibration_mode(True)
        engine.reset()
        engine.process_audio(make_audio_chunk(30))
        engine._run_final_inference()

        assert engine.get_result() == "thank you"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_timeout_resets_mode(self, mock_model_class):
        """Ten minutes after enable, the bypass is gone and the mode is off:
        a crashed Logic process can never leave the filter disabled."""
        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([self._hallucinated_segment()], Mock())

        engine = self._engine(mock_model_class)
        with patch("shared_stt.whisper_engine.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            engine.set_calibration_mode(True)
            mock_time.monotonic.return_value = 1000.0 + 601.0
            engine.process_audio(make_audio_chunk(30))
            engine._run_final_inference()

        assert engine.get_result() == ""
        assert engine._calibration_mode is False

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_within_timeout_still_bypasses(self, mock_model_class):
        """Just under ten minutes the mode is still active."""
        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([self._hallucinated_segment()], Mock())

        engine = self._engine(mock_model_class)
        with patch("shared_stt.whisper_engine.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            engine.set_calibration_mode(True)
            mock_time.monotonic.return_value = 1000.0 + 599.0
            engine.process_audio(make_audio_chunk(30))
            engine._run_final_inference()

        assert engine.get_result() == "thank you"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_reenable_restarts_timeout_window(self, mock_model_class):
        """Enabling again while already on restarts the ten-minute window,
        so the Logic process can keep a long session alive by re-sending."""
        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([self._hallucinated_segment()], Mock())

        engine = self._engine(mock_model_class)
        with patch("shared_stt.whisper_engine.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            engine.set_calibration_mode(True)
            mock_time.monotonic.return_value = 500.0
            engine.set_calibration_mode(True)
            # 700s after the first enable, 200s after the second
            mock_time.monotonic.return_value = 700.0
            engine.process_audio(make_audio_chunk(30))
            engine._run_final_inference()

        assert engine.get_result() == "thank you"

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_bypass_log_prints_numbers_only(self, mock_model_class, caplog):
        """The bypass log line follows the numbers-only privacy discipline:
        no transcript text, and no [hallucination_suppressed] line either
        (nothing was suppressed)."""
        import logging

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([self._hallucinated_segment()], Mock())

        engine = self._engine(mock_model_class)
        engine.set_calibration_mode(True)
        engine.process_audio(make_audio_chunk(30))
        with caplog.at_level(logging.INFO):
            engine._run_final_inference()

        bypass_lines = [
            r.getMessage() for r in caplog.records
            if "[calibration_mode]" in r.getMessage()
        ]
        assert bypass_lines, "expected a [calibration_mode] bypass log line"
        assert all("thank" not in line.lower() for line in bypass_lines)
        assert not any(
            "[hallucination_suppressed]" in r.getMessage() for r in caplog.records
        )
