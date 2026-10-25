"""Tests for shared AudioProcessor class.

These tests verify the AudioProcessor correctly:
- Processes audio through VAD/AGC/lead-in pipeline
- Sends vad_start when speech begins
- Calls recognition engine with processed audio
- Sends stable/final messages via WSForwarder
- Handles overflow detection
"""
import threading
import pytest
from unittest.mock import ANY, Mock, MagicMock, patch, call
from typing import Protocol, runtime_checkable


class TestRecognitionEngineProtocol:
    """Tests that the RecognitionEngine protocol is correctly defined."""

    def test_protocol_has_required_methods(self):
        """Verify RecognitionEngine protocol defines required methods."""
        from shared_stt.audio_processor import RecognitionEngine

        # RecognitionEngine should be a Protocol with these methods:
        # - process_audio(audio_bytes: bytes) -> None
        # - get_result() -> str
        # - is_endpoint() -> bool
        # - reset() -> None

        # Check it's a Protocol (runtime_checkable)
        assert hasattr(RecognitionEngine, '__protocol_attrs__') or \
               hasattr(RecognitionEngine, '_is_protocol')

    def test_mock_engine_satisfies_protocol(self):
        """Verify a mock with required methods satisfies the protocol."""
        from shared_stt.audio_processor import RecognitionEngine

        mock_engine = Mock()
        mock_engine.process_audio = Mock()
        mock_engine.is_ready = Mock(return_value=True)  # Optional method
        mock_engine.get_result = Mock(return_value="")
        mock_engine.is_endpoint = Mock(return_value=False)
        mock_engine.reset = Mock()
        mock_engine.last_result = ""  # Required attribute

        # Should be able to use as RecognitionEngine
        # (runtime_checkable Protocol check)
        assert isinstance(mock_engine, RecognitionEngine)


class TestAudioProcessorBasics:
    """Basic AudioProcessor construction and initialization tests."""

    def test_audio_processor_requires_engine(self):
        """AudioProcessor requires a RecognitionEngine."""
        from shared_stt.audio_processor import AudioProcessor

        with pytest.raises(TypeError):
            AudioProcessor()  # Missing required engine argument

    def test_audio_processor_accepts_engine(self):
        """AudioProcessor accepts a RecognitionEngine."""
        from shared_stt.audio_processor import AudioProcessor

        mock_engine = Mock()
        mock_engine.process_audio = Mock()
        mock_engine.get_result = Mock(return_value="")
        mock_engine.is_endpoint = Mock(return_value=False)
        mock_engine.reset = Mock()

        mock_forwarder = Mock()

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        assert processor.engine is mock_engine
        assert processor.forwarder is mock_forwarder


class TestAudioProcessorVADGate:
    """Tests for VAD gate behavior (opening/closing based on speech detection)."""

    @pytest.fixture
    def mock_engine(self):
        """Create a mock recognition engine."""
        engine = Mock()
        engine.process_audio = Mock()
        engine.get_result = Mock(return_value="")
        engine.is_endpoint = Mock(return_value=False)
        engine.reset = Mock()
        return engine

    @pytest.fixture
    def mock_forwarder(self):
        """Create a mock WebSocket forwarder."""
        forwarder = Mock()
        forwarder.send_vad_start = Mock()
        forwarder.send_stable = Mock()
        forwarder.send_final = Mock()
        return forwarder

    def test_vad_gate_starts_closed(self, mock_engine, mock_forwarder):
        """VAD gate should start closed."""
        from shared_stt.audio_processor import AudioProcessor

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        assert not processor.is_gate_open

    def test_vad_gate_opens_on_speech(self, mock_engine, mock_forwarder):
        """VAD gate should open when speech is detected."""
        from shared_stt.audio_processor import AudioProcessor

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Inject mock VAD that returns True (speech)
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()

        # Mock AGC to pass through
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00\x00' * 160)

        # Process audio
        processor.process_chunk(b'\x00\x00' * 160)

        assert processor.is_gate_open

    def test_sends_vad_start_when_gate_opens(self, mock_engine, mock_forwarder):
        """Should send vad_start message when VAD gate opens."""
        from shared_stt.audio_processor import AudioProcessor

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Inject mock VAD that returns True (speech)
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()

        # Mock AGC to pass through
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00\x00' * 160)

        # Mock lead-in buffer
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b'')
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()

        # Process audio
        processor.process_chunk(b'\x00\x00' * 160)

        # Should have sent vad_start
        mock_forwarder.send_vad_start.assert_called_once()


class TestAudioProcessorTranscription:
    """Tests for transcription handling (stable/final messages)."""

    @pytest.fixture
    def mock_engine(self):
        """Create a mock recognition engine."""
        engine = Mock()
        engine.process_audio = Mock()
        engine.get_result = Mock(return_value="")
        engine.is_endpoint = Mock(return_value=False)
        engine.reset = Mock()
        engine.last_result = ""
        return engine

    @pytest.fixture
    def mock_forwarder(self):
        """Create a mock WebSocket forwarder."""
        forwarder = Mock()
        forwarder.send_vad_start = Mock()
        forwarder.send_stable = Mock()
        forwarder.send_final = Mock()
        return forwarder

    def test_sends_stable_on_new_text(self, mock_engine, mock_forwarder):
        """Should send stable message when engine returns new text.

        The word-level stability logic holds back the last word (it may be
        partial), so we need at least 2 words for send_stable to fire.
        """
        from shared_stt.audio_processor import AudioProcessor

        mock_engine.get_result = Mock(return_value="hello world")

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Set gate open (speech in progress)
        processor._vad_gate_open = True

        # Inject mock components
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00\x00' * 160)

        # Process audio
        processor.process_chunk(b'\x00\x00' * 160)

        # Should have sent stable message (all words except last)
        mock_forwarder.send_stable.assert_called_once_with("hello", 0, trace_id=ANY)

    def test_sends_final_on_endpoint(self, mock_engine, mock_forwarder):
        """Should send final message when engine signals endpoint."""
        from shared_stt.audio_processor import AudioProcessor

        mock_engine.get_result = Mock(return_value="hello world")
        mock_engine.is_endpoint = Mock(return_value=True)

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Set gate open (speech in progress)
        processor._vad_gate_open = True

        # Inject mock components
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00\x00' * 160)
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.clear = Mock()

        # Process audio
        processor.process_chunk(b'\x00\x00' * 160)

        # Should have sent final message
        mock_forwarder.send_final.assert_called_once_with("hello world", 0, trace_id=ANY)

    def test_resets_after_endpoint(self, mock_engine, mock_forwarder):
        """Should reset engine and gate after endpoint."""
        from shared_stt.audio_processor import AudioProcessor

        mock_engine.get_result = Mock(return_value="hello")
        mock_engine.is_endpoint = Mock(return_value=True)

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Set gate open (speech in progress)
        processor._vad_gate_open = True

        # Inject mock components
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00\x00' * 160)
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.clear = Mock()

        # Process audio
        processor.process_chunk(b'\x00\x00' * 160)

        # Should have reset engine
        mock_engine.reset.assert_called_once()

        # Gate should be closed
        assert not processor.is_gate_open

        # Utterance ID should have incremented
        assert processor.current_utterance_id == 1


class TestAudioProcessorInterimResults:
    """Tests for interim results toggle."""

    @pytest.fixture
    def mock_engine(self):
        """Create a mock recognition engine."""
        engine = Mock()
        engine.process_audio = Mock()
        engine.get_result = Mock(return_value="hello")
        engine.is_endpoint = Mock(return_value=False)
        engine.reset = Mock()
        engine.last_result = ""
        return engine

    @pytest.fixture
    def mock_forwarder(self):
        """Create a mock WebSocket forwarder."""
        forwarder = Mock()
        forwarder.send_vad_start = Mock()
        forwarder.send_stable = Mock()
        forwarder.send_final = Mock()
        return forwarder

    def test_interim_results_enabled_by_default(self, mock_engine, mock_forwarder):
        """Interim results should be enabled by default."""
        from shared_stt.audio_processor import AudioProcessor

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        assert processor.send_interim_results is True

    def test_no_stable_when_interim_disabled(self, mock_engine, mock_forwarder):
        """Should not send stable messages when interim results disabled."""
        from shared_stt.audio_processor import AudioProcessor

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        processor.send_interim_results = False

        # Set gate open (speech in progress)
        processor._vad_gate_open = True

        # Inject mock components
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00\x00' * 160)

        # Process audio
        processor.process_chunk(b'\x00\x00' * 160)

        # Should NOT have sent stable message
        mock_forwarder.send_stable.assert_not_called()


class TestAudioProcessorLeadIn:
    """Tests for lead-in buffer integration."""

    @pytest.fixture
    def mock_engine(self):
        """Create a mock recognition engine."""
        engine = Mock()
        engine.process_audio = Mock()
        engine.get_result = Mock(return_value="")
        engine.is_endpoint = Mock(return_value=False)
        engine.reset = Mock()
        return engine

    @pytest.fixture
    def mock_forwarder(self):
        """Create a mock WebSocket forwarder."""
        forwarder = Mock()
        forwarder.send_vad_start = Mock()
        forwarder.send_stable = Mock()
        forwarder.send_final = Mock()
        return forwarder

    def test_buffers_silence(self, mock_engine, mock_forwarder):
        """Should buffer audio during silence."""
        from shared_stt.audio_processor import AudioProcessor

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Inject mock VAD that returns False (silence)
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=False)

        # Mock AGC to pass through
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00\x00' * 160)

        # Process audio
        processor.process_chunk(b'\x00\x00' * 160)

        # Engine should NOT have been called (audio buffered, not sent)
        mock_engine.process_audio.assert_not_called()

    def test_flushes_lead_in_on_speech(self, mock_engine, mock_forwarder):
        """Should flush lead-in buffer when speech starts."""
        from shared_stt.audio_processor import AudioProcessor

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # First, add some silence to buffer
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=False)
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00\x00' * 160)
        # wh-first-sound-lost.9: the lead-in goes through the AGC's
        # last gain. Pass it through unchanged here.
        processor.agc.apply_last_gain = Mock(side_effect=lambda pcm: pcm)

        processor.process_chunk(b'\x00\x00' * 160)

        # Now speech detected
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()

        # Mock lead-in to return buffered audio
        mock_lead_in = b'\x01\x00' * 160  # Different from current chunk
        processor.lead_in_buffer.get_lead_in = Mock(return_value=mock_lead_in)

        processor.process_chunk(b'\x02\x00' * 160)

        # Engine should have been called with combined audio
        # (lead-in + current chunk)
        assert mock_engine.process_audio.called
        processor.agc.apply_last_gain.assert_called_once_with(mock_lead_in)


class TestAudioProcessorIsReady:
    """Tests for is_ready() handling."""

    @pytest.fixture
    def mock_forwarder(self):
        """Create a mock WebSocket forwarder."""
        forwarder = Mock()
        forwarder.send_vad_start = Mock()
        forwarder.send_stable = Mock()
        forwarder.send_final = Mock()
        return forwarder

    def test_skips_result_check_when_not_ready(self, mock_forwarder):
        """Should skip get_result/is_endpoint when is_ready returns False."""
        from shared_stt.audio_processor import AudioProcessor

        # Create engine with is_ready returning False
        mock_engine = Mock()
        mock_engine.process_audio = Mock()
        mock_engine.is_ready = Mock(return_value=False)
        mock_engine.get_result = Mock(return_value="hello")
        mock_engine.is_endpoint = Mock(return_value=False)
        mock_engine.reset = Mock()
        mock_engine.last_result = ""

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Set gate open (speech in progress)
        processor._vad_gate_open = True

        # Inject mock components
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00\x00' * 160)

        # Process audio
        processor.process_chunk(b'\x00\x00' * 160)

        # process_audio should be called (to feed audio)
        mock_engine.process_audio.assert_called_once()

        # is_ready should be called
        mock_engine.is_ready.assert_called_once()

        # get_result should NOT be called (because is_ready returned False)
        mock_engine.get_result.assert_not_called()

        # No messages should be sent
        mock_forwarder.send_stable.assert_not_called()
        mock_forwarder.send_final.assert_not_called()

    def test_processes_results_when_ready(self, mock_forwarder):
        """Should process results when is_ready returns True."""
        from shared_stt.audio_processor import AudioProcessor

        # Create engine with is_ready returning True
        # Use multi-word result because word-level stability holds back last word
        mock_engine = Mock()
        mock_engine.process_audio = Mock()
        mock_engine.is_ready = Mock(return_value=True)
        mock_engine.get_result = Mock(return_value="hello world")
        mock_engine.is_endpoint = Mock(return_value=False)
        mock_engine.reset = Mock()
        mock_engine.last_result = ""

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Set gate open (speech in progress)
        processor._vad_gate_open = True

        # Inject mock components
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00\x00' * 160)

        # Process audio
        processor.process_chunk(b'\x00\x00' * 160)

        # get_result should be called (because is_ready returned True)
        mock_engine.get_result.assert_called_once()

        # Should send stable message (all words except last)
        mock_forwarder.send_stable.assert_called_once_with("hello", 0, trace_id=ANY)

    def test_works_without_is_ready_method(self, mock_forwarder):
        """Should work with engines that don't implement is_ready (defaults to True)."""
        from shared_stt.audio_processor import AudioProcessor

        # Create engine WITHOUT is_ready method
        # Use multi-word result because word-level stability holds back last word
        mock_engine = Mock(spec=['process_audio', 'get_result', 'is_endpoint', 'reset', 'last_result'])
        mock_engine.process_audio = Mock()
        mock_engine.get_result = Mock(return_value="hello world")
        mock_engine.is_endpoint = Mock(return_value=False)
        mock_engine.reset = Mock()
        mock_engine.last_result = ""

        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )

        # Set gate open (speech in progress)
        processor._vad_gate_open = True

        # Inject mock components
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00\x00' * 160)

        # Process audio - should not raise even without is_ready
        processor.process_chunk(b'\x00\x00' * 160)

        # get_result should still be called (default is_ready returns True)
        mock_engine.get_result.assert_called_once()

        # Should send stable message (all words except last)
        mock_forwarder.send_stable.assert_called_once_with("hello", 0, trace_id=ANY)


class TestTrailingSilenceHoldbackRelease:
    """Tests for releasing held-back words when trailing silence is detected.

    Bug scenario: "new paragraph" spoken standalone with a streaming provider.
    The N-1 holdback sends stable "new" but holds back "paragraph".
    The final arrives after ~1300ms (VAD silence detection), but the speech
    processor's 700ms replacement timeout fires first, dictating "new"
    before "paragraph" arrives. Fix: detect trailing silence and release
    the held-back word early.
    """

    @pytest.fixture
    def mock_engine(self):
        engine = Mock()
        engine.process_audio = Mock()
        engine.get_result = Mock(return_value="")
        engine.is_endpoint = Mock(return_value=False)
        engine.reset = Mock()
        engine.last_result = ""
        return engine

    @pytest.fixture
    def mock_forwarder(self):
        forwarder = Mock()
        forwarder.send_vad_start = Mock()
        forwarder.send_stable = Mock()
        forwarder.send_final = Mock()
        return forwarder

    @pytest.fixture
    def processor(self, mock_engine, mock_forwarder):
        """Create an AudioProcessor with mocked components."""
        from shared_stt.audio_processor import AudioProcessor

        proc = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )
        # Mock VAD, AGC, lead-in so we control speech/silence
        proc.vad = Mock()
        proc.vad.is_speech = Mock(return_value=True)
        proc.vad.reset = Mock()
        proc.agc = Mock()
        proc.agc.process = Mock(side_effect=lambda pcm, is_speech: pcm)
        proc.lead_in_buffer = Mock()
        proc.lead_in_buffer.get_lead_in = Mock(return_value=b'')
        proc.lead_in_buffer.clear = Mock()
        proc.lead_in_buffer.add = Mock()
        return proc

    def _make_chunk(self, num_samples=512):
        """Create a silent PCM chunk (int16 zeros)."""
        return b'\x00\x00' * num_samples

    def test_holdback_released_after_trailing_silence(
        self, processor, mock_engine, mock_forwarder
    ):
        """Held-back word should be released after sufficient trailing silence.

        Simulates: user says "new paragraph" then stops speaking.
        - First chunk (speech): engine returns "new paragraph", stable sends "new" (N-1 holdback)
        - Subsequent chunks (silence): trailing silence accumulates
        - After threshold exceeded: stable "new paragraph" should be sent (holdback released)
        """
        chunk = self._make_chunk(512)  # 512 samples = 32ms at 16kHz

        # Step 1: Speech chunk - engine recognizes "new paragraph"
        mock_engine.get_result.return_value = "new paragraph"
        processor.vad.is_speech.return_value = True
        processor.process_chunk(chunk)

        # N-1 holdback should have sent stable "new" only
        mock_forwarder.send_stable.assert_called_once_with("new", 0, trace_id=ANY)
        mock_forwarder.send_stable.reset_mock()

        # Step 2: Silence chunks - user stopped speaking, engine text unchanged
        processor.vad.is_speech.return_value = False

        # Feed enough silence to exceed the trailing silence threshold (300ms)
        # 512 samples @ 16kHz = 32ms per chunk, need ~10 chunks for 320ms
        for _ in range(10):
            processor.process_chunk(chunk)

        # The held-back "paragraph" should now be released as stable "new paragraph"
        mock_forwarder.send_stable.assert_called_with("new paragraph", 0, trace_id=ANY)

    def test_holdback_not_released_during_active_speech(
        self, processor, mock_engine, mock_forwarder
    ):
        """Held-back word should NOT be released while speech is ongoing.

        Normal N-1 holdback must remain active during speech to prevent
        sending partial words that the recognizer hasn't committed yet.
        """
        chunk = self._make_chunk(512)

        # Speech chunk - engine returns "new paragraph"
        mock_engine.get_result.return_value = "new paragraph"
        processor.vad.is_speech.return_value = True
        processor.process_chunk(chunk)

        # N-1 holdback sends stable "new"
        mock_forwarder.send_stable.assert_called_once_with("new", 0, trace_id=ANY)
        mock_forwarder.send_stable.reset_mock()

        # More speech chunks - no silence at all
        for _ in range(10):
            processor.process_chunk(chunk)

        # Holdback should NOT release "paragraph" during active speech
        mock_forwarder.send_stable.assert_not_called()

    def test_holdback_not_released_with_brief_silence(
        self, processor, mock_engine, mock_forwarder
    ):
        """Brief silence (< threshold) should not trigger holdback release.

        Normal inter-word pauses (~100-200ms) should not release the holdback.
        """
        chunk = self._make_chunk(512)  # 32ms per chunk

        # Speech chunk
        mock_engine.get_result.return_value = "new paragraph"
        processor.vad.is_speech.return_value = True
        processor.process_chunk(chunk)
        mock_forwarder.send_stable.reset_mock()

        # Brief silence: 3 chunks = ~96ms (well under 300ms threshold)
        processor.vad.is_speech.return_value = False
        for _ in range(3):
            processor.process_chunk(chunk)

        # Should NOT release holdback yet
        mock_forwarder.send_stable.assert_not_called()

    def test_trailing_silence_counter_resets_on_new_speech(
        self, processor, mock_engine, mock_forwarder
    ):
        """Trailing silence counter should reset when speech resumes.

        If silence accumulates but then speech resumes (user was pausing
        between words), the counter resets and holdback stays active.
        """
        chunk = self._make_chunk(512)

        # Speech: engine returns "new paragraph"
        mock_engine.get_result.return_value = "new paragraph"
        processor.vad.is_speech.return_value = True
        processor.process_chunk(chunk)
        mock_forwarder.send_stable.reset_mock()

        # Partial silence (200ms = ~6 chunks) - close to but under threshold
        processor.vad.is_speech.return_value = False
        for _ in range(6):
            processor.process_chunk(chunk)

        # Speech resumes (user says another word)
        processor.vad.is_speech.return_value = True
        mock_engine.get_result.return_value = "new paragraph break"
        processor.process_chunk(chunk)

        # Should send stable "new paragraph" via normal N-1 holdback
        # (3 words, hold back "break", send first 2)
        mock_forwarder.send_stable.assert_called_with("new paragraph", 0, trace_id=ANY)
        mock_forwarder.send_stable.reset_mock()

        # More silence (but counter was reset) - 5 chunks = 160ms
        processor.vad.is_speech.return_value = False
        for _ in range(5):
            processor.process_chunk(chunk)

        # Should NOT release yet (counter was reset, only 160ms of new silence)
        mock_forwarder.send_stable.assert_not_called()

    def test_trailing_silence_resets_on_new_utterance(
        self, processor, mock_engine, mock_forwarder
    ):
        """Trailing silence counter should reset between utterances."""
        chunk = self._make_chunk(512)

        # Speech: engine returns "hello world" then endpoint
        mock_engine.get_result.return_value = "hello world"
        mock_engine.is_endpoint.return_value = True
        processor.vad.is_speech.return_value = True
        processor.process_chunk(chunk)

        # Verify final was sent and state reset
        mock_forwarder.send_final.assert_called_once()
        assert not processor.is_gate_open

        # New utterance starts - gate opens fresh
        mock_engine.get_result.return_value = "new paragraph"
        mock_engine.is_endpoint.return_value = False
        mock_engine.last_result = ""
        processor.vad.is_speech.return_value = True
        processor.process_chunk(chunk)  # Opens gate + processes

        mock_forwarder.send_stable.reset_mock()

        # Silence should start from zero for the new utterance
        processor.vad.is_speech.return_value = False
        for _ in range(5):  # ~160ms - under threshold
            processor.process_chunk(chunk)

        # Should NOT release (not enough silence in this utterance)
        mock_forwarder.send_stable.assert_not_called()


class TestAudioProcessorAGCFeedback:
    """Tests for AGC STT-outcome feedback from non-Google providers (wh-7ou.1).

    Before this bead, only google_stt_server emitted failure outcome events
    (NO_TEXT_TIMEOUT / VAD_SILENCE_ABORT) to the AGC. The removed
    Distil-Whisper provider and sherpa_offline_parakeet used the shared AudioProcessor + engine.finalize()
    path via _force_finalize_and_reset, which only emitted a success event
    when the engine returned text. When a force-endpoint fired with no text
    (the non-Google equivalent of a silence abort -- the hallucination-prone
    case), the AGC received no signal at all and its failure ratchet never
    engaged.
    """

    @pytest.fixture
    def mock_engine(self):
        engine = Mock()
        engine.process_audio = Mock()
        engine.get_result = Mock(return_value="")
        engine.is_endpoint = Mock(return_value=False)
        engine.reset = Mock()
        engine.last_result = ""
        return engine

    @pytest.fixture
    def mock_forwarder(self):
        forwarder = Mock()
        forwarder.send_vad_start = Mock()
        forwarder.send_stable = Mock()
        forwarder.send_final = Mock()
        return forwarder

    def _make_processor(self, mock_engine, mock_forwarder):
        from shared_stt.audio_processor import AudioProcessor
        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
            force_endpoint_silence_ms=500,
        )
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b'\x00' * 512)
        processor.agc.on_stt_outcome = Mock()
        return processor

    def test_force_endpoint_with_text_emits_success_to_agc(
        self, mock_engine, mock_forwarder
    ):
        """Force-finalize with engine text should emit success outcome to AGC."""
        processor = self._make_processor(mock_engine, mock_forwarder)
        mock_engine.get_result.return_value = "hello world"

        processor._force_finalize_and_reset()

        # Success event fired (word_count > 0)
        processor.agc.on_stt_outcome.assert_called_once()
        call_args = processor.agc.on_stt_outcome.call_args
        assert call_args[0][1] == 2  # word_count

    def test_force_endpoint_without_text_emits_failure_to_agc(
        self, mock_engine, mock_forwarder
    ):
        """Force-finalize with no engine text should emit failure outcome to AGC.

        This is the core fix for wh-7ou.1: when VAD trailing silence forces an
        endpoint but the engine produced nothing (likely non-speech audio got
        through VAD / AGC over-amplified noise), the AGC must learn from this
        so its failure ratchet can engage.
        """
        processor = self._make_processor(mock_engine, mock_forwarder)
        mock_engine.get_result.return_value = ""  # No text produced

        processor._force_finalize_and_reset()

        # Failure event must fire with word_count=0
        processor.agc.on_stt_outcome.assert_called_once()
        call_args = processor.agc.on_stt_outcome.call_args
        result_type = call_args[0][0]
        word_count = call_args[0][1]
        assert word_count == 0
        # Result type should be one of the AGC-recognized failure signals
        assert result_type in ("VAD_SILENCE_ABORT", "NO_TEXT_TIMEOUT", "SILENCE_ABORT")

    def test_repeated_empty_force_endpoints_engage_ratchet(
        self, mock_engine, mock_forwarder
    ):
        """Repeated empty force-endpoints should engage the AGC failure ratchet.

        This is the end-to-end confirmation: without the fix, the ratchet never
        engages for non-Google providers no matter how many hallucination events
        occur. With the fix, 3+ empty force-endpoints in a row drop the gain cap.
        """
        from shared_stt.audio_processor import AudioProcessor
        from shared_audio.agc import SmartAGC

        # Use a real AGC so the ratchet logic runs end-to-end
        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
            force_endpoint_silence_ms=500,
        )
        assert isinstance(processor.agc, SmartAGC)
        initial_cap = processor.agc.failure_gain_cap
        mock_engine.get_result.return_value = ""

        # Simulate 3 consecutive empty force-endpoints (hallucination / noise trigger)
        for _ in range(3):
            processor._force_finalize_and_reset()

        # Ratchet must have dropped the failure gain cap
        assert processor.agc.failure_gain_cap < initial_cap
        assert processor.agc.consecutive_failures >= 3


class TestAudioProcessorHallucinationSuppression:
    """wh-7ou.2: when the engine suppresses a final (is_endpoint=True with
    empty text) because its hallucination filter fired, AudioProcessor must
    still reset state, log stats, and feed the AGC failure ratchet. Without
    this path the processor would leave the gate 'open' on the next utterance.
    """

    @pytest.fixture
    def mock_engine(self):
        engine = Mock()
        engine.process_audio = Mock()
        engine.get_result = Mock(return_value="")
        engine.is_endpoint = Mock(return_value=True)
        engine.is_ready = Mock(return_value=True)
        engine.reset = Mock()
        engine.last_result = ""
        return engine

    @pytest.fixture
    def mock_forwarder(self):
        forwarder = Mock()
        forwarder.send_vad_start = Mock()
        forwarder.send_stable = Mock()
        forwarder.send_final = Mock()
        return forwarder

    def _make_processor(self, mock_engine, mock_forwarder):
        from shared_stt.audio_processor import AudioProcessor
        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
        )
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b"\x00" * 512)
        processor.agc.on_stt_outcome = Mock()
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b"")
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()
        return processor

    def test_suppressed_final_emits_failure_to_agc(self, mock_engine, mock_forwarder):
        """is_endpoint=True with empty text should fire the AGC failure signal."""
        processor = self._make_processor(mock_engine, mock_forwarder)
        processor._vad_gate_open = True

        processor._process_speech_audio(b"\x00" * 512)

        processor.agc.on_stt_outcome.assert_called_once()
        call_args = processor.agc.on_stt_outcome.call_args
        assert call_args[0][0] == "VAD_SILENCE_ABORT"
        assert call_args[0][1] == 0

    def test_suppressed_final_resets_state(self, mock_engine, mock_forwarder):
        """Suppressed endpoint must close the gate and advance the utterance id."""
        processor = self._make_processor(mock_engine, mock_forwarder)
        processor._vad_gate_open = True
        starting_id = processor.current_utterance_id

        processor._process_speech_audio(b"\x00" * 512)

        assert processor.is_gate_open is False
        assert processor.current_utterance_id == starting_id + 1
        mock_engine.reset.assert_called_once()

    def test_suppressed_final_does_not_send_final_message(
        self, mock_engine, mock_forwarder
    ):
        """Suppressed endpoint must NOT send a FINAL over the WebSocket.
        Otherwise WheelHouse would dictate an empty string, which manifests
        as a flicker in the UI and still advances the utterance routing.
        """
        processor = self._make_processor(mock_engine, mock_forwarder)
        processor._vad_gate_open = True

        processor._process_speech_audio(b"\x00" * 512)

        mock_forwarder.send_final.assert_not_called()
        mock_forwarder.send_stable.assert_not_called()


class TestFinalCallShape:
    """The final goes to the forwarder as text, utterance id and trace id,
    and nothing more. The confidence block of the removed Distil-Whisper
    engine (wh-7ou.7.1.1) is gone, so an engine that still carries a
    get_final_confidence method (a bare Mock creates one) must not change
    the call."""

    @pytest.fixture
    def mock_forwarder(self):
        forwarder = Mock()
        forwarder.send_vad_start = Mock()
        forwarder.send_stable = Mock()
        forwarder.send_final = Mock()
        return forwarder

    def _make_engine(self):
        engine = Mock()
        engine.process_audio = Mock()
        engine.is_ready = Mock(return_value=True)
        engine.get_result = Mock(return_value="hello world")
        engine.is_endpoint = Mock(return_value=True)
        engine.reset = Mock()
        engine.last_result = ""
        engine.get_final_confidence = Mock(return_value={"word_count": 1})
        return engine

    def _make_processor(self, mock_engine, mock_forwarder, **kwargs):
        from shared_stt.audio_processor import AudioProcessor
        processor = AudioProcessor(
            engine=mock_engine,
            forwarder=mock_forwarder,
            sample_rate=16000,
            **kwargs,
        )
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b"\x00" * 512)
        processor.agc.on_stt_outcome = Mock()
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b"")
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()
        return processor

    def test_endpoint_final_carries_no_confidence_block(self, mock_forwarder):
        engine = self._make_engine()
        processor = self._make_processor(engine, mock_forwarder)
        processor._vad_gate_open = True

        processor._process_speech_audio(b"\x00" * 512)

        mock_forwarder.send_final.assert_called_once_with(
            "hello world", 0, trace_id=ANY
        )

    def test_forced_final_carries_no_confidence_block(self, mock_forwarder):
        engine = self._make_engine()
        engine.is_endpoint = Mock(return_value=False)
        engine.finalize = Mock()
        processor = self._make_processor(
            engine, mock_forwarder, force_endpoint_silence_ms=500
        )

        processor._force_finalize_and_reset()

        mock_forwarder.send_final.assert_called_once_with(
            "hello world", 0, trace_id=ANY
        )


# ---------------------------------------------------------------------------
# wh-first-sound-lost.1: the hand-off to the engine-audio saver
# ---------------------------------------------------------------------------

class _RecordingSaver:
    """Stands in for EngineAudioSaver; records submit() into a shared list."""

    def __init__(self, events, raises=False):
        self.events = events
        self.raises = raises
        self.audios = []

    def submit(self, audio, utterance_id, final_sent):
        self.events.append(("submit", utterance_id, final_sent))
        self.audios.append(audio)
        if self.raises:
            raise RuntimeError("the saver broke")


class TestEngineAudioSaverHandOff:
    """AudioProcessor hands the audio of the final decode to the saver at the
    four places an utterance can end, after the final transcript was sent
    (so the file write can never delay the transcript) and before the engine
    is reset (reset throws the audio away).

      engine_text   engine endpoint, with text   -> final_sent=True
      engine_empty  engine endpoint, no text     -> final_sent=False
      forced_text   forced endpoint, with text   -> final_sent=True
      forced_empty  forced endpoint, no text     -> final_sent=False
    """

    PATHS = ["engine_text", "engine_empty", "forced_text", "forced_empty"]
    TEXT = "hello world"

    @staticmethod
    def _audio(seed=0):
        import numpy as np

        rng = np.random.default_rng(seed)
        return (rng.standard_normal(1600) * 0.1).astype(np.float32)

    def _engine(self, events, path):
        engine = Mock()
        engine.is_ready = Mock(return_value=True)
        has_text = path.endswith("text")
        engine.get_result = Mock(return_value=self.TEXT if has_text else "")
        engine.is_endpoint = Mock(return_value=path.startswith("engine"))
        engine.last_result = ""
        # The real engines set last_final_audio inside the decode call
        # (process_audio on an endpoint, finalize on a forced endpoint), so
        # the value must not exist before that call. `pending_audio` is what
        # the next decode call publishes. A processor that read
        # last_final_audio before the decode would read None.
        engine.last_final_audio = None
        engine.pending_audio = None

        def decode(*_args, **_kwargs):
            engine.last_final_audio = engine.pending_audio

        engine.process_audio = Mock(side_effect=decode)
        engine.finalize = Mock(side_effect=decode)

        def reset():
            events.append(("reset",))
            engine.last_final_audio = None      # the real engines do this

        engine.reset = Mock(side_effect=reset)
        return engine

    def _forwarder(self, events):
        forwarder = Mock()
        forwarder.send_vad_start = Mock()
        forwarder.send_stable = Mock()
        forwarder.send_final = Mock(
            side_effect=lambda text, utt, **kw: events.append(
                ("send_final", text, utt)))
        return forwarder

    def _processor(self, engine, forwarder, **kwargs):
        from shared_stt.audio_processor import AudioProcessor

        processor = AudioProcessor(
            engine=engine,
            forwarder=forwarder,
            sample_rate=16000,
            force_endpoint_silence_ms=500,
            **kwargs,
        )
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b"\x00" * 512)
        processor.agc.on_stt_outcome = Mock()
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b"")
        processor.lead_in_buffer.clear = Mock()
        processor.lead_in_buffer.add = Mock()
        return processor

    @staticmethod
    def _end_one_utterance(processor, engine, path, audio):
        """Drive one utterance to its end by the given path.

        The audio becomes visible as engine.last_final_audio only when the
        decode call runs (see _engine)."""
        engine.last_final_audio = None
        engine.pending_audio = audio
        if path.startswith("engine"):
            processor._vad_gate_open = True
            processor._process_speech_audio(b"\x00" * 512)
        else:
            processor._force_finalize_and_reset()

    def _expected(self, path, utt):
        reset = ("reset",)
        if path.endswith("text"):
            return [("send_final", self.TEXT, utt), ("submit", utt, True), reset]
        return [("submit", utt, False), reset]

    @pytest.mark.parametrize("path", PATHS)
    def test_the_saver_is_called_after_send_final_and_before_reset(self, path):
        events = []
        engine = self._engine(events, path)
        saver = _RecordingSaver(events)
        processor = self._processor(
            engine, self._forwarder(events), engine_audio_saver=saver)

        self._end_one_utterance(processor, engine, path, self._audio(0))
        self._end_one_utterance(processor, engine, path, self._audio(1))

        assert events == self._expected(path, 0) + self._expected(path, 1)

    @pytest.mark.parametrize("path", PATHS)
    def test_the_saver_gets_the_engines_final_audio(self, path):
        import numpy as np

        events = []
        engine = self._engine(events, path)
        saver = _RecordingSaver(events)
        processor = self._processor(
            engine, self._forwarder(events), engine_audio_saver=saver)

        first, second = self._audio(0), self._audio(1)
        self._end_one_utterance(processor, engine, path, first)
        self._end_one_utterance(processor, engine, path, second)

        assert len(saver.audios) == 2
        assert np.array_equal(saver.audios[0], first)
        assert np.array_equal(saver.audios[1], second)

    @pytest.mark.parametrize("path", PATHS)
    def test_without_a_saver_recognition_is_unchanged(self, path):
        events = []
        engine = self._engine(events, path)
        forwarder = self._forwarder(events)
        processor = self._processor(engine, forwarder, engine_audio_saver=None)

        self._end_one_utterance(processor, engine, path, self._audio(0))

        sent = [e for e in events if e[0] == "send_final"]
        assert sent == ([("send_final", self.TEXT, 0)]
                        if path.endswith("text") else [])
        assert processor.current_utterance_id == 1

    @pytest.mark.parametrize("path", PATHS)
    def test_the_saver_is_optional_by_default(self, path):
        """No engine_audio_saver keyword at all: the pre-feature call shape
        every provider and test already uses must keep working."""
        from shared_stt.audio_processor import AudioProcessor

        events = []
        engine = self._engine(events, path)
        processor = AudioProcessor(
            engine=engine, forwarder=self._forwarder(events),
            sample_rate=16000, force_endpoint_silence_ms=500)
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=True)
        processor.vad.reset = Mock()
        processor.agc = Mock()
        processor.agc.process = Mock(return_value=b"\x00" * 512)
        processor.lead_in_buffer = Mock()
        processor.lead_in_buffer.get_lead_in = Mock(return_value=b"")

        self._end_one_utterance(processor, engine, path, self._audio(0))

        assert processor.current_utterance_id == 1

    @pytest.mark.parametrize("path", PATHS)
    def test_no_audio_means_no_call(self, path):
        events = []
        engine = self._engine(events, path)
        saver = _RecordingSaver(events)
        processor = self._processor(
            engine, self._forwarder(events), engine_audio_saver=saver)

        self._end_one_utterance(processor, engine, path, None)

        assert not [e for e in events if e[0] == "submit"]
        assert processor.current_utterance_id == 1

    @pytest.mark.parametrize("path", PATHS)
    def test_a_saver_that_raises_does_not_stop_recognition(self, path):
        events = []
        engine = self._engine(events, path)
        saver = _RecordingSaver(events, raises=True)
        processor = self._processor(
            engine, self._forwarder(events), engine_audio_saver=saver)

        self._end_one_utterance(processor, engine, path, self._audio(0))
        self._end_one_utterance(processor, engine, path, self._audio(1))

        assert events == self._expected(path, 0) + self._expected(path, 1)
        assert processor.current_utterance_id == 2
        assert engine.reset.call_count == 2
        # The AGC feedback that follows the hand-off still ran, twice.
        assert processor.agc.on_stt_outcome.call_count == 2

    @pytest.mark.parametrize("path", ["engine_text", "forced_text"])
    def test_a_partial_decode_before_the_endpoint_is_not_saved(self, path):
        """Only the final decode is saved: a non-final chunk (no endpoint)
        that comes first must add no submit, even when the engine already
        holds audio at that moment."""
        import numpy as np

        events = []
        engine = self._engine(events, path)
        partial_text = "hello"
        engine.get_result = Mock(side_effect=[partial_text, self.TEXT])
        if path.startswith("engine"):
            engine.is_endpoint = Mock(side_effect=[False, True])
        saver = _RecordingSaver(events)
        processor = self._processor(
            engine, self._forwarder(events), engine_audio_saver=saver)
        processor._vad_gate_open = True

        # The partial decode: no endpoint, and the engine holds other audio.
        engine.pending_audio = self._audio(7)
        processor._process_speech_audio(b"\x00" * 512)
        assert not [e for e in events if e[0] == "submit"]

        # The final decode.
        final_audio = self._audio(8)
        engine.pending_audio = final_audio
        if path.startswith("engine"):
            processor._process_speech_audio(b"\x00" * 512)
        else:
            processor._force_finalize_and_reset()

        assert events == self._expected(path, 0)
        assert len(saver.audios) == 1
        assert np.array_equal(saver.audios[0], final_audio)


class TestLeadInGain:
    """wh-first-sound-lost.9: the 300 ms lead-in reaches the engine at the
    gain that SmartAGC applied to the first speech chunk after it.

    The lead-in buffer holds the raw chunks, so before the fix the lead-in
    reached the engine at gain 1.0 and the first speech chunk at the AGC
    gain. The volume step at the join is one cause of the lost first sound.
    These tests use the real SmartAGC and the real lead-in buffer; only the
    VAD and the engine are mocks.
    """

    SILENCE = 500     # sample value of each lead-in chunk
    SPEECH = 1000     # sample value of the first speech chunk
    SAMPLES = 512     # samples per chunk, one Silero VAD frame
    LEAD_IN_CHUNKS = 3

    def _run(self, agc_config=None):
        """Feed three silence chunks, then one speech chunk. Return the
        engine's two calls as int16 sample arrays: (lead-in, speech)."""
        import numpy as np
        from shared_stt.audio_processor import AudioProcessor

        engine = Mock()
        engine.get_result = Mock(return_value="")
        engine.is_endpoint = Mock(return_value=False)
        processor = AudioProcessor(engine=engine, forwarder=Mock(),
                                   sample_rate=16000, agc_config=agc_config)
        processor.vad = Mock()
        processor.vad.is_speech = Mock(return_value=False)

        silence = np.full(self.SAMPLES, self.SILENCE, dtype='<i2').tobytes()
        speech = np.full(self.SAMPLES, self.SPEECH, dtype='<i2').tobytes()
        for _ in range(self.LEAD_IN_CHUNKS):
            processor.process_chunk(silence)
        assert not engine.process_audio.called
        processor.vad.is_speech = Mock(return_value=True)
        processor.process_chunk(speech)

        calls = [np.frombuffer(c.args[0], dtype=np.float32)
                 for c in engine.process_audio.call_args_list]
        assert len(calls) == 2, "expected one lead-in call and one speech call"
        lead_in, first = (np.round(c * 32768.0).astype(np.int64) for c in calls)
        assert len(lead_in) == self.SAMPLES * self.LEAD_IN_CHUNKS
        return lead_in, first

    def test_the_lead_in_gets_the_gain_of_the_first_speech_chunk(self):
        from shared_audio.agc import AGCConfig

        lead_in, first = self._run(AGCConfig(enabled=True))

        gain = first[0] / self.SPEECH
        # The precondition: the AGC really changed the speech chunk, so a
        # lead-in left at gain 1.0 cannot pass by accident.
        assert gain > 1.05, f"the AGC did not change the speech chunk: {gain}"
        assert set(first.tolist()) == {first[0]}
        # Every lead-in sample got the same gain as the speech chunk. The
        # gain step truncates toward zero, so the two can differ by one count.
        assert set(lead_in.tolist()) == {lead_in[0]}
        assert abs(lead_in[0] - self.SILENCE * gain) <= 1, (
            f"lead-in sample {lead_in[0]}, expected about "
            f"{self.SILENCE * gain:.1f} (gain {gain:.4f})")

    def test_with_agc_off_the_engine_gets_the_raw_audio(self):
        """Acceptance item 2: with [agc] enabled = false the engine gets the
        same bytes as before the fix: the raw lead-in, then the raw chunk."""
        import numpy as np
        from shared_audio.agc import AGCConfig

        lead_in, first = self._run(AGCConfig(enabled=False))

        assert np.array_equal(lead_in, np.full(
            self.SAMPLES * self.LEAD_IN_CHUNKS, self.SILENCE))
        assert np.array_equal(first, np.full(self.SAMPLES, self.SPEECH))
