"""Tests for WhisperStreamingEngine.

Tests the chunked re-inference streaming engine with LocalAgreement-2
stability detection, audio buffer management, and endpoint detection.

Mock strategy: WhisperModel is mocked to avoid loading real model weights.
The mock returns predetermined transcription results.

Reference: docs/design/chunked_streaming_engine_design.md
"""
import numpy as np
import pytest
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

        # "Hello" lowercased (sentence start), "World" stays as-is
        # But both happen to be non-proper-nouns, so Whisper's output after
        # first-char lowercasing gives "hello World"
        assert engine.get_result() == "hello World"

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

        assert engine.get_result() == "open Google Chrome"

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
):
    """Mock segment with per-segment confidence attributes used by wh-7ou.2."""
    segment = Mock()
    segment.text = text
    segment.avg_logprob = avg_logprob
    segment.no_speech_prob = no_speech_prob
    segment.compression_ratio = compression_ratio
    segment.start = start
    segment.end = end
    return segment


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
