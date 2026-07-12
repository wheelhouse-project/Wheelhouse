"""Integration tests for WhisperStreamingEngine + AudioProcessor.

Tests the full pipeline with real AudioProcessor and WhisperStreamingEngine
wired together, but with mocked audio capture and WhisperModel.

These tests verify:
- VAD gating prevents engine calls during silence
- Stable messages flow through AudioProcessor's N-1 holdback
- Final messages trigger on engine endpoint
- The holdback release fires on trailing silence

Reference: docs/design/chunked_streaming_engine_design.md Section 6.3
"""
import struct

import numpy as np
import pytest
from unittest.mock import ANY, Mock, patch


def make_pcm_speech(duration_ms: int = 30, sample_rate: int = 16000, rms: float = 0.3) -> bytes:
    """Create int16 PCM bytes with speech-like energy.

    AudioProcessor feeds int16 PCM to the engine (which converts to float32).
    We need the RMS to exceed both the VAD threshold and the engine's
    silence_rms_threshold after AGC processing.
    """
    n_samples = int(sample_rate * duration_ms / 1000)
    t = np.linspace(0, duration_ms / 1000, n_samples, dtype=np.float32)
    amplitude = rms * np.sqrt(2) * 32768.0
    samples = (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    return samples.tobytes()


def make_pcm_silence(duration_ms: int = 30, sample_rate: int = 16000) -> bytes:
    """Create int16 PCM silence bytes."""
    n_samples = int(sample_rate * duration_ms / 1000)
    return np.zeros(n_samples, dtype=np.int16).tobytes()


def make_mock_segment(text: str):
    """Create a mock segment like faster-whisper returns.

    Confidence attributes default to the 'real dictation' band so the
    wh-7ou.2 hallucination filter doesn't fire on these mocks. Arithmetic
    attributes (start/end) are floats so instrumentation log arithmetic
    (end - start) doesn't hit Mock subtraction errors.
    """
    segment = Mock()
    segment.text = text
    segment.avg_logprob = -0.2
    segment.no_speech_prob = 0.01
    segment.compression_ratio = 0.5
    segment.start = 0.0
    segment.end = 1.0
    return segment


@pytest.fixture
def mock_forwarder():
    """Create a mock WSForwarder."""
    fwd = Mock()
    fwd.send_vad_start = Mock()
    fwd.send_stable = Mock()
    fwd.send_final = Mock()
    return fwd


class TestVADGatesEngine:
    """Verify AudioProcessor's VAD gate prevents engine calls during silence."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_engine_not_called_during_silence(self, mock_model_class, mock_forwarder):
        """Engine should not receive audio when VAD gate is closed."""
        from shared_stt.audio_processor import AudioProcessor
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.01,
        )

        processor = AudioProcessor(
            engine=engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Feed silence -- VAD should keep gate closed
        for _ in range(20):
            processor.process_chunk(make_pcm_silence(30))

        # Engine's buffer should be empty (no audio forwarded)
        assert len(engine._audio_buffer) == 0
        mock_model.transcribe.assert_not_called()

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_engine_receives_audio_after_vad_opens(self, mock_model_class, mock_forwarder):
        """Engine should receive audio once VAD gate opens."""
        from shared_stt.audio_processor import AudioProcessor
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        processor = AudioProcessor(
            engine=engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
            vad_threshold=0.01,  # Very low so our synthetic speech triggers it
        )

        # Mock VAD to return True (speech detected)
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()

        # Mock AGC to pass through
        processor.agc = Mock()
        processor.agc.process = Mock(side_effect=lambda pcm, is_speech: pcm)
        processor.agc.on_stt_outcome = Mock()

        # Mock lead-in
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b'')
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()

        # Feed speech chunks
        for _ in range(5):
            processor.process_chunk(make_pcm_speech(30))

        # Engine should have received audio
        assert len(engine._audio_buffer) > 0


class TestStableMessageFlow:
    """Verify stable messages flow through AudioProcessor's holdback."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_stable_sent_with_holdback(self, mock_model_class, mock_forwarder):
        """Stable messages should use AudioProcessor's N-1 holdback on confirmed words.

        Scenario: engine confirms "hello world" via LocalAgreement-2.
        AudioProcessor should hold back the last word ("world") and send "hello".
        """
        from shared_stt.audio_processor import AudioProcessor
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            silence_rms_threshold=0.001,
        )

        processor = AudioProcessor(
            engine=engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Set up mocked pipeline
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(side_effect=lambda pcm, is_speech: pcm)
        processor.agc.on_stt_outcome = Mock()
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b'')
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()

        # Run 1: "hello" -- first inference, no confirmation yet
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())
        for _ in range(14):
            processor.process_chunk(make_pcm_speech(30))

        mock_forwarder.send_stable.assert_not_called()

        # Run 2: "hello world how" -- confirms "hello" via LCP
        mock_model.transcribe.return_value = ([make_mock_segment("hello world how")], Mock())
        for _ in range(14):
            processor.process_chunk(make_pcm_speech(30))

        # Engine confirms "hello" (LCP of ["hello"] and ["hello", "world", "how"])
        # AudioProcessor then applies N-1 holdback: won't send until > 1 word confirmed
        # With only "hello" confirmed, nothing is sent (single word held back)
        # This is correct -- single confirmed word is held back by AudioProcessor

        # Run 3: "hello world how are" -- confirms "hello world how"
        mock_model.transcribe.return_value = ([make_mock_segment("hello world how are")], Mock())
        for _ in range(14):
            processor.process_chunk(make_pcm_speech(30))

        # Engine confirms "hello world how"
        # AudioProcessor N-1 holdback sends "hello world" (holds "how")
        mock_forwarder.send_stable.assert_called_with("hello world", 0, trace_id=ANY)


class TestFinalMessageFlow:
    """Verify final messages trigger on engine endpoint."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_final_sent_on_endpoint(self, mock_model_class, mock_forwarder):
        """Final message should be sent when engine detects endpoint (trailing silence)."""
        from shared_stt.audio_processor import AudioProcessor
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            endpoint_silence_ms=500,
            silence_rms_threshold=0.01,
        )

        processor = AudioProcessor(
            engine=engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Set up mocked pipeline
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(side_effect=lambda pcm, is_speech: pcm)
        processor.agc.on_stt_outcome = Mock()
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b'')
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()

        # Speech: inference runs, "delete" recognized
        mock_model.transcribe.return_value = ([make_mock_segment("delete")], Mock())
        for _ in range(14):
            processor.process_chunk(make_pcm_speech(30))

        # Now silence to trigger endpoint (VAD still reports speech=True
        # because AudioProcessor gate is open, but engine tracks its own RMS)
        processor.vad.is_speech = Mock(return_value=False)

        # Feed 510ms+ of silence to trigger endpoint
        mock_model.transcribe.return_value = ([make_mock_segment("delete")], Mock())
        for _ in range(17):
            processor.process_chunk(make_pcm_silence(30))

        # Final should have been sent
        mock_forwarder.send_final.assert_called_once_with("delete", 0, trace_id=ANY)

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_reset_after_final(self, mock_model_class, mock_forwarder):
        """AudioProcessor should reset engine and gate after final."""
        from shared_stt.audio_processor import AudioProcessor
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            endpoint_silence_ms=500,
            silence_rms_threshold=0.01,
        )

        processor = AudioProcessor(
            engine=engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Set up mocked pipeline
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(side_effect=lambda pcm, is_speech: pcm)
        processor.agc.on_stt_outcome = Mock()
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b'')
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()

        # Speech + endpoint
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())
        for _ in range(14):
            processor.process_chunk(make_pcm_speech(30))

        processor.vad.is_speech = Mock(return_value=False)
        for _ in range(17):
            processor.process_chunk(make_pcm_silence(30))

        # Gate should be closed, engine reset
        assert not processor.is_gate_open
        assert engine._audio_buffer == []
        assert processor.current_utterance_id == 1


class TestHoldbackRelease:
    """Verify AudioProcessor releases held-back words on trailing silence."""

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_holdback_releases_on_silence(self, mock_model_class, mock_forwarder):
        """Held-back word should be released when trailing silence exceeds threshold.

        The AudioProcessor's N-1 holdback holds the last confirmed word.
        When trailing silence is detected (VAD reports no speech for 300ms),
        the held-back word is released as a stable message.
        """
        from shared_stt.audio_processor import AudioProcessor
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            endpoint_silence_ms=500,
            silence_rms_threshold=0.01,
        )

        processor = AudioProcessor(
            engine=engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Set up mocked pipeline
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(side_effect=lambda pcm, is_speech: pcm)
        processor.agc.on_stt_outcome = Mock()
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b'')
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()

        # Speech: 2 runs to confirm "hello world"
        mock_model.transcribe.return_value = ([make_mock_segment("hello world")], Mock())
        for _ in range(14):
            processor.process_chunk(make_pcm_speech(30))

        mock_model.transcribe.return_value = ([make_mock_segment("hello world")], Mock())
        for _ in range(14):
            processor.process_chunk(make_pcm_speech(30))

        # At this point, engine confirmed "hello world"
        # AudioProcessor N-1 holdback sent stable "hello", holds "world"
        mock_forwarder.send_stable.reset_mock()

        # Silence (VAD reports no speech) for 300ms+ to trigger holdback release
        # But BEFORE engine endpoint (500ms)
        processor.vad.is_speech = Mock(return_value=False)
        for _ in range(10):  # 10 * 30ms = 300ms
            processor.process_chunk(make_pcm_silence(30))

        # Holdback should release "hello world" as stable
        mock_forwarder.send_stable.assert_called_with("hello world", 0, trace_id=ANY)


class TestForceEndpointOnVADSilence:
    """Verify AudioProcessor forces endpoint when VAD silence exceeds threshold.

    This tests the fix for the production bug where the engine's RMS-based
    endpoint detection fails because AGC amplifies ambient noise above the
    silence threshold, causing the audio buffer to grow unboundedly.
    """

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_force_endpoint_fires_on_vad_silence(self, mock_model_class, mock_forwarder):
        """Force endpoint should fire when VAD silence exceeds force_endpoint_silence_ms."""
        from shared_stt.audio_processor import AudioProcessor
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            endpoint_silence_ms=500,
            silence_rms_threshold=0.001,
        )

        processor = AudioProcessor(
            engine=engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
            force_endpoint_silence_ms=500,
        )

        # Set up mocked pipeline
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(side_effect=lambda pcm, is_speech: pcm)
        processor.agc.on_stt_outcome = Mock()
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b'')
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()

        # Speech
        for _ in range(14):
            processor.process_chunk(make_pcm_speech(30))

        # Switch to silence
        processor.vad.is_speech = Mock(return_value=False)
        for _ in range(17):  # 510ms > 500ms
            processor.process_chunk(make_pcm_silence(30))

        # Should have sent final and reset
        mock_forwarder.send_final.assert_called_once()
        assert not processor.is_gate_open

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_force_endpoint_when_engine_rms_detection_broken(self, mock_model_class, mock_forwarder):
        """Force endpoint works even when engine's RMS-based detection fails.

        Simulates the production bug: AGC amplifies ambient noise above the
        engine's silence_rms_threshold, preventing the engine from detecting
        endpoint. AudioProcessor's VAD-based force endpoint catches it.
        """
        from shared_stt.audio_processor import AudioProcessor
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("delete")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            endpoint_silence_ms=500,
            silence_rms_threshold=0.01,
        )

        processor = AudioProcessor(
            engine=engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
            force_endpoint_silence_ms=500,
        )

        # Set up mocked pipeline
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(side_effect=lambda pcm, is_speech: pcm)
        processor.agc.on_stt_outcome = Mock()
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b'')
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()

        # Speech
        for _ in range(14):
            processor.process_chunk(make_pcm_speech(30))

        # Switch to silence via VAD, but AGC returns noisy audio
        # that fools engine's RMS-based detection (RMS 0.02 > threshold 0.01)
        processor.vad.is_speech = Mock(return_value=False)
        noisy_silence = make_pcm_speech(30, rms=0.02)
        processor.agc.process = Mock(return_value=noisy_silence)

        for _ in range(17):  # 510ms
            processor.process_chunk(make_pcm_silence(30))

        # Despite engine's silence detection failing, force endpoint fires
        mock_forwarder.send_final.assert_called_once()
        assert not processor.is_gate_open
        assert engine._audio_buffer == []  # Reset happened

    @patch("shared_stt.whisper_engine.WhisperModel")
    def test_force_endpoint_disabled_by_default(self, mock_model_class, mock_forwarder):
        """When force_endpoint_silence_ms is None (default), no forced endpoint."""
        from shared_stt.audio_processor import AudioProcessor
        from shared_stt.whisper_engine import WhisperStreamingEngine

        mock_model = mock_model_class.return_value
        mock_model.transcribe.return_value = ([make_mock_segment("hello")], Mock())

        engine = WhisperStreamingEngine(
            re_inference_interval_ms=400,
            endpoint_silence_ms=10000,  # Very high so engine never endpoints
            silence_rms_threshold=0.001,
        )

        processor = AudioProcessor(
            engine=engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
            # force_endpoint_silence_ms not set (default None)
        )

        # Set up mocked pipeline
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(side_effect=lambda pcm, is_speech: pcm)
        processor.agc.on_stt_outcome = Mock()
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b'')
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()

        # Speech then silence
        for _ in range(14):
            processor.process_chunk(make_pcm_speech(30))

        processor.vad.is_speech = Mock(return_value=False)
        for _ in range(20):  # 600ms of silence
            processor.process_chunk(make_pcm_silence(30))

        # Should NOT have sent final (force endpoint disabled, engine endpoint very high)
        mock_forwarder.send_final.assert_not_called()
        assert processor.is_gate_open  # Gate still open
