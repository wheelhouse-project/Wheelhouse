"""Shared AudioProcessor for STT providers.

This module provides a reusable audio processing class that handles the common
audio pipeline (VAD, AGC, lead-in buffer) and coordinates with a recognition
engine. Providers only need to implement the RecognitionEngine protocol.

Key Classes:
  - RecognitionEngine: Protocol defining the interface for recognition engines.
  - AudioProcessor: Processes audio and coordinates with recognition engine.

Typical Usage:
  from shared_stt.audio_processor import AudioProcessor, RecognitionEngine

  class MyEngine:
      def process_audio(self, audio_bytes: bytes) -> None:
          # Feed audio to your STT backend
          pass

      def get_result(self) -> str:
          return self.model.get_text()

      def is_endpoint(self) -> bool:
          return self.model.is_complete()

      def reset(self) -> None:
          self.model.reset()

  processor = AudioProcessor(
      engine=MyEngine(),
      forwarder=ws_forwarder,
      sample_rate=16000,
  )

  # In audio loop:
  processor.process_chunk(pcm_bytes)
"""
import logging
import struct
import time
from typing import Optional, Protocol, runtime_checkable

import numpy as np

from shared_audio.silero_vad import SileroVAD
from shared_audio.agc import SmartAGC, AGCConfig
from shared_audio.lead_in_buffer import LeadInBuffer
from shared_stt.redact import redact_transcript

logger = logging.getLogger(__name__)


@runtime_checkable
class RecognitionEngine(Protocol):
    """Protocol defining the interface for recognition engines.

    Providers implement this protocol to integrate with AudioProcessor.
    The processor handles audio capture, VAD, AGC, and WebSocket forwarding;
    the engine only needs to handle the actual speech recognition.

    Attributes:
        last_result: The last transcription result (used for change detection).

    Methods:
        process_audio: Feed audio to the recognition engine.
        is_ready: Check if results are available (optional, defaults to True).
        get_result: Get current transcription text.
        is_endpoint: Check if utterance is complete.
        reset: Reset for next utterance.
    """

    last_result: str

    def process_audio(self, audio_bytes: bytes) -> None:
        """Process an audio chunk.

        Args:
            audio_bytes: Audio in float32 format (after VAD/AGC processing).
        """
        ...

    def is_ready(self) -> bool:
        """Check if the engine has results ready.

        Some engines (like Sherpa-ONNX) need to accumulate audio before
        producing results. This method allows checking if get_result()
        will return meaningful data.

        Returns:
            True if results are available. Default implementation returns True.
        """
        ...

    def get_result(self) -> str:
        """Get current transcription result.

        Returns:
            Current transcription text (may be partial or final).
        """
        ...

    def is_endpoint(self) -> bool:
        """Check if current utterance is complete.

        Returns:
            True if the engine detected end of utterance.
        """
        ...

    def reset(self) -> None:
        """Reset state for next utterance."""
        ...


class AudioProcessor:
    """Processes audio through VAD/AGC pipeline and coordinates with recognition engine.

    This class handles:
    - VAD gating (only sends audio when speech detected)
    - AGC (automatic gain control)
    - Lead-in buffering (captures pre-speech audio)
    - vad_start signaling (for instant GUI feedback)
    - Stable/final message sending via WebSocket
    - Utterance ID management
    - Interim results toggle

    Providers only need to implement RecognitionEngine and pass it here.
    """

    def __init__(
        self,
        engine: RecognitionEngine,
        forwarder,  # WSForwarder - not typed to avoid circular import
        sample_rate: int = 16000,
        vad_threshold: float = 0.5,
        vad_lead_in_ms: int = 300,
        agc_config: Optional[AGCConfig] = None,
        force_endpoint_silence_ms: Optional[float] = None,
    ):
        """Initialize the AudioProcessor.

        Args:
            engine: Recognition engine implementing the RecognitionEngine protocol.
            forwarder: WSForwarder instance for sending messages to WheelHouse.
            sample_rate: Audio sample rate in Hz (must be 16000 for Silero VAD).
            vad_threshold: Confidence threshold for VAD (0.0-1.0).
            vad_lead_in_ms: Duration of lead-in buffer in milliseconds.
            agc_config: Configuration for AGC (uses defaults if None).
            force_endpoint_silence_ms: When set, force utterance endpoint after
                this many ms of VAD silence. Uses Silero VAD (more reliable than
                engine-level RMS detection for post-AGC audio). None = disabled.
        """
        self.engine = engine
        self.forwarder = forwarder
        self.sample_rate = sample_rate

        # Initialize VAD
        self.vad = SileroVAD(threshold=vad_threshold, sample_rate=sample_rate)

        # Initialize AGC
        self.agc = SmartAGC(agc_config or AGCConfig())

        # Initialize lead-in buffer
        self.lead_in_buffer = LeadInBuffer(
            lead_time_s=vad_lead_in_ms / 1000.0,
            sample_rate=sample_rate,
            bytes_per_sample=2  # int16 PCM
        )

        # VAD gate state
        self._vad_gate_open = False

        # Utterance tracking
        self.current_utterance_id = 0

        # Interim results control
        self.send_interim_results = True

        # Word-level stable tracking (prevents partial word fragments)
        # Only send words that are followed by another word (confirming they're complete)
        self._last_sent_stable_words: list[str] = []

        # Trailing silence tracking for holdback release
        # When the user stops speaking, the last held-back word won't change,
        # so we release it early to avoid downstream timeout issues
        self._trailing_silence_samples: int = 0
        self._trailing_silence_release_ms: float = 300.0

        # Force endpoint on VAD silence (for engines with broken RMS detection)
        self._force_endpoint_silence_ms = force_endpoint_silence_ms

        # wh-7ou.2 instrumentation: VAD sustained-speech ratio per utterance.
        # Captured at gate-open, compared at endpoint/force-endpoint to detect
        # transient false-positives (throat clears, coughs) where the gate opens
        # on a brief voiced onset but the utterance has low aggregate speech
        # density. Uses counters already tracked by SileroVAD -- zero added cost.
        self._vad_speech_at_open: int = 0
        self._vad_inferences_at_open: int = 0
        self._gate_open_monotonic: float = 0.0

    @property
    def is_gate_open(self) -> bool:
        """Whether the VAD gate is currently open (speech in progress)."""
        return self._vad_gate_open

    def process_chunk(self, pcm_bytes: bytes) -> None:
        """Process an audio chunk through the full pipeline.

        This method:
        1. Runs VAD on raw audio
        2. Applies AGC
        3. Manages lead-in buffer
        4. Opens VAD gate and sends vad_start when speech starts
        5. Feeds audio to recognition engine
        6. Sends stable/final messages based on engine output

        Args:
            pcm_bytes: Raw int16 PCM audio bytes.
        """
        if not pcm_bytes:
            return

        # Step 1: Run VAD (BEFORE AGC - on raw audio)
        is_speech = self.vad.is_speech(pcm_bytes)

        # Step 2: Apply AGC
        agc_pcm = self.agc.process(pcm_bytes, is_speech)

        # Step 3: Lead-in buffer logic
        if self._vad_gate_open:
            # Track trailing silence while gate is open
            num_samples = len(pcm_bytes) // 2
            if is_speech:
                self._trailing_silence_samples = 0
            else:
                self._trailing_silence_samples += num_samples

            # Force endpoint if VAD silence exceeds threshold
            # (more reliable than engine-level RMS on post-AGC audio)
            if self._force_endpoint_silence_ms is not None:
                trailing_ms = self._trailing_silence_samples / self.sample_rate * 1000
                if trailing_ms >= self._force_endpoint_silence_ms:
                    self._force_finalize_and_reset()
                    return

            # Gate already open - process audio
            self._process_speech_audio(agc_pcm)
        else:
            if is_speech:
                # Speech detected! Open gate
                self._vad_gate_open = True
                logger.debug("VAD gate opened - speech detected")

                # wh-7ou.2: snapshot VAD counters so we can compute the
                # sustained-speech ratio across this utterance at endpoint.
                self._vad_speech_at_open = getattr(self.vad, "_speech_count", 0)
                self._vad_inferences_at_open = getattr(self.vad, "_inference_count", 0)
                self._gate_open_monotonic = time.monotonic()

                # Generate trace_id at utterance birth
                from shared_stt.ws_forwarder import generate_trace_id
                self._current_trace_id = generate_trace_id()

                # Send vad_start for instant GUI feedback
                if self.forwarder:
                    self.forwarder.send_vad_start(self.current_utterance_id, trace_id=self._current_trace_id)

                # Flush lead-in buffer
                lead_in = self.lead_in_buffer.get_lead_in()
                self.lead_in_buffer.clear()

                # Process lead-in + current chunk
                if lead_in:
                    self._process_speech_audio(lead_in)
                self._process_speech_audio(agc_pcm)
            else:
                # Still silence - add to lead-in buffer
                self.lead_in_buffer.add(pcm_bytes)

                # Keep engine cache warm during silence (for cache-aware
                # streaming engines like Parakeet that need continuous audio)
                keep_warm = getattr(self.engine, 'keep_warm', None)
                if keep_warm:
                    keep_warm(self._pcm_to_float32(agc_pcm))

    def _process_speech_audio(self, pcm_bytes: bytes) -> None:
        """Process audio through the recognition engine and handle results.

        Args:
            pcm_bytes: AGC-processed int16 PCM audio bytes.
        """
        # Convert to float32 for engine
        float32_audio = self._pcm_to_float32(pcm_bytes)

        # Feed to engine
        self.engine.process_audio(float32_audio)

        # Check if engine has results ready (some engines need to accumulate audio)
        # Use getattr with default True for engines that don't implement is_ready()
        is_ready = getattr(self.engine, 'is_ready', lambda: True)()
        if not is_ready:
            return

        # Check for results
        text = self.engine.get_result()
        is_endpoint = self.engine.is_endpoint()

        # wh-7ou.2: engine may suppress a final transcript when its
        # hallucination filter decides the utterance was non-speech. The
        # endpoint still fires (is_endpoint=True) but get_result() returns
        # empty. Treat this like a VAD silence abort: feed the AGC failure
        # ratchet, log utterance stats, reset cleanly. Without this branch
        # the suppressed endpoint would skip the `if text:` block and leave
        # the processor gated-open on the next utterance.
        if is_endpoint and not text:
            logger.info(
                f"FINAL [{self.current_utterance_id}]: "
                f"<suppressed by hallucination filter>"
            )
            self.agc.on_stt_outcome("VAD_SILENCE_ABORT", 0)
            self._log_vad_utterance_stats("endpoint_suppressed", "")
            self._reset_for_new_utterance()
            return

        if text:
            # Check if text changed or finalized
            if text != self.engine.last_result or is_endpoint:
                if is_endpoint:
                    # Final result
                    logger.info(f"FINAL [{self.current_utterance_id}]: '{redact_transcript(text)}'")
                    if self.forwarder:
                        tid = getattr(self, '_current_trace_id', '')
                        self.forwarder.send_final(text, self.current_utterance_id, trace_id=tid)

                    # AGC feedback
                    word_count = len(text.split())
                    self.agc.on_stt_outcome("GOOGLE_FINAL", word_count)
                elif self.send_interim_results:
                    # Partial result - use word-level stability to prevent fragments
                    # Only send words that are "committed" (followed by another word)
                    current_words = text.split() if text else []
                    sent_count = len(self._last_sent_stable_words)

                    # Check if current words still match what we've sent (no revision)
                    if sent_count > 0 and sent_count <= len(current_words):
                        if current_words[:sent_count] != self._last_sent_stable_words:
                            # Revision detected - log but continue (final will sort it out)
                            # Redact the joined text, not the list repr, so the
                            # placeholder's word count is the real word count
                            # (wh-797.17.3).
                            logger.warning(f"[REVISION] UTT-{self.current_utterance_id}: "
                                         f"sent='{redact_transcript(' '.join(self._last_sent_stable_words))}', "
                                         f"current='{redact_transcript(' '.join(current_words[:sent_count]))}'")

                    # Commit all words except the last (it may be partial)
                    if len(current_words) > sent_count + 1:
                        # New complete words available - send them
                        words_to_send = current_words[:-1]  # All except last
                        stable_text = " ".join(words_to_send)
                        self._last_sent_stable_words = words_to_send[:]
                        logger.info(f"STABLE [{self.current_utterance_id}]: '{redact_transcript(stable_text)}'")
                        if self.forwarder:
                            tid = getattr(self, '_current_trace_id', '')
                            self.forwarder.send_stable(stable_text, self.current_utterance_id, trace_id=tid)
                        # Reset trailing silence so _maybe_release_holdback doesn't
                        # immediately undo the holdback on this same chunk.
                        self._trailing_silence_samples = 0

                self.engine.last_result = text

            # Check trailing silence release (runs even when text hasn't changed)
            if not is_endpoint and self.send_interim_results:
                self._maybe_release_holdback(text)

            if is_endpoint:
                self._log_vad_utterance_stats("endpoint", text)
                self._reset_for_new_utterance()

    def _maybe_release_holdback(self, text: str) -> None:
        """Release held-back words when trailing silence exceeds threshold.

        During active speech, the N-1 holdback prevents sending partial words.
        Once the user stops speaking (trailing silence), the last word won't
        change, so we release it to avoid downstream timeout issues.

        Args:
            text: Current engine transcription text.
        """
        trailing_ms = self._trailing_silence_samples / self.sample_rate * 1000
        if trailing_ms < self._trailing_silence_release_ms:
            return

        current_words = text.split()
        sent_words = self._last_sent_stable_words

        # Only release if there are unsent words
        if len(current_words) <= len(sent_words):
            return

        # Verify consistency (current text starts with what we sent)
        if sent_words and current_words[:len(sent_words)] != sent_words:
            return  # Revision - don't release, let final handle it

        # Release all words (no holdback)
        stable_text = " ".join(current_words)
        self._last_sent_stable_words = current_words[:]
        logger.info(f"STABLE [{self.current_utterance_id}] (silence release): '{redact_transcript(stable_text)}'")
        if self.forwarder:
            tid = getattr(self, '_current_trace_id', '')
            self.forwarder.send_stable(stable_text, self.current_utterance_id, trace_id=tid)

    def _log_vad_utterance_stats(self, endpoint_kind: str, text: str) -> None:
        """wh-7ou.2: emit sustained-speech ratio for the just-closed utterance.

        Computes speech_chunks / total_chunks across the gate-open window using
        SileroVAD's running counters. Hypothesis: throat clears and other
        transient non-speech events have low ratios (~0.1-0.3) because the gate
        opens on a brief voiced onset but most subsequent chunks fall below
        threshold. Real speech has high ratios (~0.6-0.9).

        No filtering is applied yet -- this is observation only. Once the
        threshold is calibrated from data, we can gate final inference on this
        ratio in _force_finalize_and_reset and is_endpoint paths.
        """
        try:
            speech_now = getattr(self.vad, "_speech_count", 0)
            inferences_now = getattr(self.vad, "_inference_count", 0)
            speech_delta = speech_now - self._vad_speech_at_open
            inference_delta = inferences_now - self._vad_inferences_at_open
            ratio = (speech_delta / inference_delta) if inference_delta > 0 else 0.0
            gate_ms = (time.monotonic() - self._gate_open_monotonic) * 1000.0
            logger.info(
                "[vad_utt_stats] utt=%d kind=%s speech=%d total=%d ratio=%.3f "
                "gate_ms=%.0f text=%r",
                self.current_utterance_id,
                endpoint_kind,
                speech_delta,
                inference_delta,
                ratio,
                gate_ms,
                redact_transcript(text),
            )
        except Exception as e:
            logger.warning("Failed to log VAD utterance stats: %s", e)

    def _force_finalize_and_reset(self) -> None:
        """Force utterance finalization based on VAD silence.

        Called when VAD trailing silence exceeds force_endpoint_silence_ms.
        Asks the engine to finalize (if it supports it), then sends the
        final result and resets for the next utterance.
        """
        # Ask engine to run final inference (if it supports finalize())
        finalize = getattr(self.engine, 'finalize', None)
        if finalize:
            finalize()

        text = self.engine.get_result()

        if text:
            logger.info(f"FINAL [{self.current_utterance_id}] (forced): '{redact_transcript(text)}'")
            if self.forwarder:
                tid = getattr(self, '_current_trace_id', '')
                self.forwarder.send_final(text, self.current_utterance_id, trace_id=tid)
            word_count = len(text.split())
            self.agc.on_stt_outcome("GOOGLE_FINAL", word_count)
        else:
            # VAD opened the gate but the engine produced nothing before the
            # trailing-silence timeout. Likely cause: AGC over-amplified noise
            # into VAD trigger range, or Silero false-positived on non-speech
            # (throat clearing, cough, HVAC). Feed this back to AGC so its
            # failure ratchet engages and caps effective gain (wh-7ou.1).
            logger.debug("Force endpoint with no text - resetting")
            self.agc.on_stt_outcome("VAD_SILENCE_ABORT", 0)

        self._log_vad_utterance_stats("force", text)
        self._reset_for_new_utterance()

    def _reset_for_new_utterance(self) -> None:
        """Reset state for the next utterance."""
        self.engine.reset()
        self._vad_gate_open = False
        self.lead_in_buffer.clear()
        self.vad.reset()
        self._last_sent_stable_words = []  # Reset word-level tracking
        self._trailing_silence_samples = 0  # Reset trailing silence
        self.current_utterance_id += 1
        logger.debug(f"Reset for new utterance {self.current_utterance_id}")

    def reset_utterance(self) -> None:
        """Manually reset for a new utterance (called externally if needed)."""
        self._reset_for_new_utterance()

    def on_stt_outcome(self, result_type: str, word_count: int) -> None:
        """Forward STT outcome to AGC for feedback loop.

        Args:
            result_type: The STT result type (e.g., "GOOGLE_FINAL", "NO_TEXT_TIMEOUT").
            word_count: Number of words in transcription (0 if no text).
        """
        self.agc.on_stt_outcome(result_type, word_count)

    def _pcm_to_float32(self, pcm_bytes: bytes) -> bytes:
        """Convert int16 PCM bytes to float32 audio bytes.

        Args:
            pcm_bytes: Audio samples as int16 signed PCM bytes.

        Returns:
            Audio samples as float32 bytes.
        """
        n_samples = len(pcm_bytes) // 2
        samples = struct.unpack(f'<{n_samples}h', pcm_bytes)
        float_samples = [s / 32768.0 for s in samples]
        return np.array(float_samples, dtype=np.float32).tobytes()
