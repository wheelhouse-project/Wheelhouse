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
from collections import deque
from typing import Callable, Optional, Protocol, runtime_checkable

import numpy as np

from shared_audio import diagnostics
from shared_audio.silero_vad import SileroVAD
from shared_audio.agc import SmartAGC, AGCConfig
from shared_audio.lead_in_buffer import LeadInBuffer
from shared_stt.redact import redact_transcript
from shared_stt.pre_engine_metrics import PreEngineWork
from shared_stt.ws_forwarder import (
    DELIVERY_RECEIPT_ATTR,
    new_delivery_receipt,
    wheelhouse_can_receive_logs,
)

logger = logging.getLogger(__name__)

# The per-utterance [load-diag] line logs here rather than on `logger`
# (wh-stt-load-metrics). Providers silence this package wholesale -- the
# Parakeet server sets logging.getLogger("shared_stt").propagate = False and
# attaches its WebSocketLogHandler only to its own logger -- so a record from
# this module reaches no log at all. Measured, not assumed: grep -c
# "vad_utt_stats" over wheelhouse.log and its five rotated files returned 0
# in every one, while the provider's own lines were present.
#
# A named child lets a provider forward this one line by attaching a handler
# to this name: a record fires its own logger's handlers before propagation
# reaches the silenced parent. Nothing else under shared_stt changes
# visibility, which is why the [vad_utt_stats] line is deliberately left on
# `logger` above.
LOAD_METRICS_LOGGER_NAME = __name__ + ".loadmetrics"
load_metrics_logger = logging.getLogger(LOAD_METRICS_LOGGER_NAME)

#: How often the unreadable-capture reason may be logged, in seconds.
#: _read_capture_stats runs on two PER-CHUNK paths --
#: _sample_capture_history and _sample_capture_queue_depth -- so at 30 ms
#: chunks it is asked about 33 times a second. A line per failure would
#: bury the very band it exists to explain: the 2026-08-30 loaded run
#: lost capture readings for nearly five minutes. Nothing is discarded --
#: the suppressed calls are counted and the next line carries the count
#: (wh-capture-load-gaps).
CAPTURE_REASON_LOG_INTERVAL_S = 5.0


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


# The capture counters the per-utterance line reports as a difference
# between the start and the end of an utterance. The zero baseline in
# seed_capture_baseline_before_capture_starts covers exactly these, so a
# counter added to the line without being added here would silently lose its
# baseline (wh-stt-load-metrics.1.10).
#
# The tuple and the difference rule moved to shared_audio.diagnostics when a
# second provider began reporting the same three counters; they are bound
# here as well because this module is where they were written and still
# their first caller (wh-stt-load-metrics.4).
CAPTURE_DELTA_KEYS = diagnostics.CAPTURE_DELTA_KEYS


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
        forwarder,  # WSForwarder - left unannotated; see the note below
        sample_rate: int = 16000,
        vad_threshold: float = 0.5,
        vad_lead_in_ms: int = 300,
        agc_config: Optional[AGCConfig] = None,
        force_endpoint_silence_ms: Optional[float] = None,
        capture_stats: Optional[Callable[[], dict]] = None,
        log_load_diagnostics: bool = False,
    ):
        """Initialize the AudioProcessor.

        Args:
            engine: Recognition engine implementing the RecognitionEngine protocol.
            forwarder: WSForwarder instance for sending messages to WheelHouse.
                The parameter carries no type annotation. The reason given
                here used to be a circular import; that is not the case
                today -- this module imports wheelhouse_can_receive_logs
                from shared_stt.ws_forwarder at module level, and
                ws_forwarder imports nothing from here. The annotation is
                simply still absent.
            sample_rate: Audio sample rate in Hz (must be 16000 for Silero VAD).
            vad_threshold: Confidence threshold for VAD (0.0-1.0).
            vad_lead_in_ms: Duration of lead-in buffer in milliseconds.
            agc_config: Configuration for AGC (uses defaults if None).
            force_endpoint_silence_ms: When set, force utterance endpoint after
                this many ms of VAD silence. Uses Silero VAD (more reliable than
                engine-level RMS detection for post-AGC audio). None = disabled.
            capture_stats: Reader for the audio provider's get_stats(), used
                only to report load metrics (wh-stt-load-metrics). A provider
                that does not pass one still gets the engine half of the
                [load-diag] line; the capture fields then read "n/a".
            log_load_diagnostics: The provider's [debug] log_load_diagnostics
                setting, off by default. On, the [load-diag] work_scope=silence
                line is written every ten seconds of silence. Off, that window
                is discarded without a line, and only the one-per-event
                speech-onset and reset lines remain
                (wh-audit13-preengine-load-review.1).
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

        # wh-stt-load-metrics. Transcription degrades under heavy CPU load for
        # two different reasons that need two different fixes: the capture
        # callback loses frames (the words come out wrong), or the recognizer
        # falls behind (the words come out late but correct). These counters
        # separate the two, and _log_load_metrics reports them once per
        # utterance.
        #
        # Running totals rather than per-chunk lists: a long utterance would
        # grow a list without bound, and max plus mean answers the question a
        # list would. The capture snapshot is taken when the VAD gate opens and
        # subtracted at the endpoint, because the provider's counters run for
        # the life of the process -- reporting them raw would make every
        # utterance after the first look worse than it was.
        self._capture_stats = capture_stats
        self._load_capture_at_open: Optional[dict] = None
        # wh-stt-load-metrics.1.3: the utterance's audio opens with the
        # lead-in buffer, captured BEFORE the VAD gate opened, so a baseline
        # read at gate-open time hides every frame lost during the onset and
        # the line prints the reassuring zero the procedure maps to "no
        # capture loss". One snapshot per silent chunk is kept here, and the
        # oldest one still inside the lead-in window becomes the baseline.
        # wh-stt-load-metrics.1.5: the window is measured in audio bytes,
        # the same unit LeadInBuffer.add evicts by, so the kept snapshots
        # are exactly that buffer's chunks plus one older entry. Consumer
        # wall-clock time cannot measure it: the loop drains a backlog
        # faster than real time, which is the CPU-starvation state this
        # feature exists to diagnose, and every chunk of that burst then
        # carries the same timestamp. The byte rule is also the bound on
        # this deque, so no fixed entry cap can cut a long lead-in short.
        # crewcut: the snapshot is still taken on the consumer thread while
        # the counters advance on the capture thread, so the measured window
        # trails the audio by the current queue depth. Removing that needs the
        # capture layer to stamp each frame with the counter values it was
        # captured under, which is a change to the capture path this
        # measurement bead is not allowed to make.
        self._load_capture_history: deque = deque()
        self._load_capture_history_bytes: int = 0
        # Why these are NOT reset per utterance, unlike the _load_*
        # fields above: a capture reader that fails does so across
        # utterances, and resetting here would restart the rate limit on
        # every utterance boundary -- which under load is often enough to
        # bring back the flood the limit prevents.
        self._capture_reason_counts: dict = {}
        self._capture_reason_detail: Optional[str] = None
        self._capture_reason_logged_at: Optional[float] = None
        # Set once the "no reader" case has been reported. It is a fact
        # about how this processor was built, not a reading that failed,
        # so it is said once and never counted again
        # (wh-capture-load-gaps.1.1).
        self._capture_no_reader_reported = False
        # True only while __init__ takes its own seeding reading. Both
        # shipped providers construct the processor in their own __init__
        # and attach the handler that carries provider records to
        # wheelhouse.log later, in start(): distil_medium_en attaches to
        # shared_stt.audio_processor, the parent of the loadmetrics
        # logger, and the Parakeet server attaches to
        # LOAD_METRICS_LOGGER_NAME itself. A record written here therefore
        # reaches the provider's stdout and nothing else, so the one
        # no-reader report must not be spent on it
        # (wh-capture-load-gaps.2.1).
        self._capture_read_before_provider_handler = True
        self._lead_in_capacity_bytes = self.lead_in_buffer.capacity_bytes
        # wh-stt-load-metrics.1.6: True once the oldest kept entry is known
        # to predate every chunk the lead-in buffer still holds. Until then
        # there is no baseline to subtract and the line says n/a.
        self._load_capture_baseline_sealed = False
        self._seed_capture_history()
        self._capture_read_before_provider_handler = False
        self._load_q_max: int = 0
        self._load_q_sum: int = 0
        self._load_q_samples: int = 0
        self._load_engine_calls: int = 0
        self._load_engine_s_total: float = 0.0
        self._load_engine_s_max: float = 0.0
        self._load_engine_audio_samples: int = 0
        self._log_load_diagnostics = log_load_diagnostics
        self._pre_engine_work = PreEngineWork()
        self._pre_engine_idle = PreEngineWork()
        self._pre_engine_idle_started: Optional[float] = None
        self._pre_engine_history: deque[PreEngineWork] = deque()
        self._pre_engine_history_bytes = 0

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
        work_started = time.monotonic()
        is_speech = self.vad.is_speech(pcm_bytes)
        vad_ended = time.monotonic()

        # Step 2: Apply AGC
        agc_pcm = self.agc.process(pcm_bytes, is_speech)
        agc_ended = time.monotonic()
        work = PreEngineWork(len(pcm_bytes), vad_ended - work_started,
                             agc_ended - vad_ended)

        # Step 3: Lead-in buffer logic
        if self._vad_gate_open:
            self._pre_engine_work.add(work)
            # wh-stt-load-metrics: one queue-depth sample per chunk, taken on
            # the consumer thread. A deep capture queue means this loop is not
            # draining audio as fast as the microphone produces it, which is
            # the state that ends in dropped frames.
            self._sample_capture_queue_depth()

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

                # wh-stt-load-metrics: the utterance's measurement window opens
                # here, so the capture counters are read now and differenced at
                # the endpoint.
                baseline = self._capture_baseline()
                self._reset_load_metrics()
                self._load_capture_at_open = baseline
                self._flush_pre_engine_idle('speech')
                for previous_work in self._pre_engine_history:
                    self._pre_engine_work.add(previous_work)
                self._pre_engine_work.add(work)
                self._pre_engine_history.clear()
                self._pre_engine_history_bytes = 0

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

                # wh-stt-load-metrics.1.3: this chunk may become the opening
                # of the next utterance, so record where the capture counters
                # stood while it was being captured. The same bytes went into
                # the lead-in buffer on the line above, and the trim below
                # uses them to hold the two in step.
                self._sample_capture_history(pcm_bytes)

                # Keep engine cache warm during silence (for cache-aware
                # streaming engines like Parakeet that need continuous audio)
                keep_warm = getattr(self.engine, 'keep_warm', None)
                if keep_warm:
                    warm_started = time.monotonic()
                    keep_warm(self._pcm_to_float32(agc_pcm))
                    work.keep_warm_s = time.monotonic() - warm_started
                self._record_pre_engine_silence(work, work_started)

    def _flush_pre_engine_idle(self, end: str, log: bool = True) -> None:
        # The window resets either way, so an onset or reset line never
        # reports more than ten seconds of silence work
        # (wh-audit13-preengine-load-review.1).
        if log:
            self._pre_engine_idle.log(load_metrics_logger, 'silence', end, self.sample_rate)
        self._pre_engine_idle = PreEngineWork()
        self._pre_engine_idle_started = None

    def _record_pre_engine_silence(self, work: PreEngineWork, started: float) -> None:
        # Mirror LeadInBuffer's whole-chunk eviction, including zero capacity
        # and oversized chunks. Consumer time cannot bound captured audio.
        self._pre_engine_history.append(work)
        self._pre_engine_history_bytes += work.audio_bytes
        while (self._pre_engine_history
               and self._pre_engine_history_bytes > self._lead_in_capacity_bytes):
            self._pre_engine_history_bytes -= self._pre_engine_history.popleft().audio_bytes
        if self._pre_engine_idle_started is None:
            self._pre_engine_idle_started = started
        self._pre_engine_idle.add(work)
        if time.monotonic() - self._pre_engine_idle_started >= 10.0:
            self._flush_pre_engine_idle('interval', log=self._log_load_diagnostics)

    def _process_speech_audio(self, pcm_bytes: bytes) -> None:
        """Process audio through the recognition engine and handle results.

        Args:
            pcm_bytes: AGC-processed int16 PCM audio bytes.
        """
        # Convert to float32 for engine
        float32_audio = self._pcm_to_float32(pcm_bytes)

        # Feed to engine. Timed for wh-stt-load-metrics: this call is the
        # recognizer's whole cost per chunk, and comparing it against the
        # duration of the audio it was given is what separates inference lag
        # from capture loss.
        _load_t0 = time.perf_counter()
        self.engine.process_audio(float32_audio)
        _load_elapsed = time.perf_counter() - _load_t0
        self._load_engine_calls += 1
        self._load_engine_s_total += _load_elapsed
        if _load_elapsed > self._load_engine_s_max:
            self._load_engine_s_max = _load_elapsed
        self._load_engine_audio_samples += len(pcm_bytes) // 2

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
                        confidence = self._get_final_confidence()
                        if confidence is not None:
                            self.forwarder.send_final(
                                text, self.current_utterance_id,
                                trace_id=tid, confidence=confidence,
                            )
                        else:
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

    def _get_final_confidence(self) -> Optional[dict]:
        """wh-7ou.7.1.1: fetch the engine's per-final measurement block.

        Only Whisper-based engines expose get_final_confidence(); other
        engines (Parakeet, the cloud providers) do not, and their finals go
        out unchanged. Anything that is not a plain dict is discarded --
        the block goes straight into a JSON payload.
        """
        getter = getattr(self.engine, "get_final_confidence", None)
        if not callable(getter):
            return None
        try:
            block = getter()
        except Exception as e:
            logger.warning("Failed to read final confidence block: %s", e)
            return None
        return block if isinstance(block, dict) else None

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

        # Deliberately outside the try above, and last: a fault in the load
        # metrics must not suppress the [vad_utt_stats] line, and must not be
        # reported under that line's error message. _log_load_metrics carries
        # its own guard.
        self._log_load_metrics(endpoint_kind)

    def _reset_load_metrics(self) -> None:
        """Start a new per-utterance measurement window (wh-stt-load-metrics)."""
        self._load_capture_at_open = None
        self._load_q_max = 0
        self._load_q_sum = 0
        self._load_q_samples = 0
        self._load_engine_calls = 0
        self._load_engine_s_total = 0.0
        self._load_engine_s_max = 0.0
        self._load_engine_audio_samples = 0
        self._pre_engine_work = PreEngineWork()

    def _read_capture_stats(self) -> Optional[dict]:
        """The audio provider's stats, or None when they cannot be read.

        None rather than an empty dict: the reporting line prints "n/a" for a
        capture number it does not have, so a provider that was never given a
        capture_stats reader cannot be misread as one that saw no drops.
        """
        if self._capture_stats is None:
            self._note_capture_unreadable("no-reader")
            return None
        try:
            stats = self._capture_stats()
        except Exception as e:
            # Replaces a logger.debug on the module logger. That record
            # reached no log at all under a provider that silences this
            # package, which is why the 2026-08-30 band had no cause.
            self._note_capture_unreadable(
                "reader-raised", f"{type(e).__name__}: {e}")
            return None
        if not isinstance(stats, dict):
            self._note_capture_unreadable(
                "non-dict", type(stats).__name__)
            return None
        return stats

    def _a_no_reader_line_would_be_heard(self) -> bool:
        """Whether a no-reader line written now would reach wheelhouse.log.

        Two things must both be true, and they are separate questions that
        fail at different moments in provider startup.

        The provider must have attached the handler that carries these
        records off the console. Both shipped providers build this
        processor inside their own __init__ and attach that handler later,
        in start(), so the seeding reading __init__ takes for itself would
        otherwise spend the one allowed report on a record that reaches
        the provider's stdout and nothing else
        (wh-capture-load-gaps.2.1).

        The WebSocket behind that handler must also be up. Neither
        provider waits for it: start() calls forwarder.start(), which only
        starts a thread, and then attaches the handler, starts capture and
        enters the audio loop while WSForwarder.is_connected is still
        False. WebSocketLogHandler drops a record outright in that state,
        counting it and returning, so a report spent there is lost exactly
        as completely (wh-capture-load-gaps.2.2).

        Both failures end the same way, which is why one question covers
        them: a line written now reaches nobody.

        This answer decides only whether to WRITE the line. It does not
        spend the one-shot report, and must not: the connection can close
        between this read and the handler's own read of the same shared
        answer, on the forwarder's thread, so a latch closed on this
        answer can still close on a record the handler drops
        (wh-capture-load-gaps.2.3). What spends the report is the delivery
        receipt _note_capture_unreadable reads after the log call.

        Returns:
            True when the line should be written.
        """
        if self._capture_read_before_provider_handler:
            return False
        return wheelhouse_can_receive_logs(self.forwarder)

    def _note_capture_unreadable(self, reason: str,
                                 detail: Optional[str] = None) -> None:
        """Count one unreadable capture reading and report at a bounded rate.

        The line goes to load_metrics_logger, not to `logger`: providers
        silence this package wholesale -- Parakeet sets
        logging.getLogger("shared_stt").propagate = False with no handler
        -- so a record on the module logger reaches no log at all. The
        loadmetrics child already reaches wheelhouse.log, which is where
        the missing-window band is read (wh-capture-load-gaps).

        The constructor takes a reading of its own -- __init__ calls
        _seed_capture_history -- so a reader that is broken from the
        start says so at startup rather than waiting for the first
        utterance. A MISSING reader is the exception: that reading
        happens before the provider's start() attaches the handler that
        carries these records to wheelhouse.log, and the report below is
        allowed only once, so spending it there would leave the log with
        no line at all (wh-capture-load-gaps.2.1). The constructor
        therefore says nothing for that case and the first reading after
        start() says it. The other two reasons are unbounded, so their
        constructor line is a duplicate rather than the only one, and
        they keep it.

        "no-reader" is then reported once for the life of the processor
        and never counted again. self._capture_stats is bound in
        __init__ and never rebound, so that case can neither start nor
        stop being true, and a second line carries no information beyond
        the chunk rate. distil_medium_en builds its processor without a
        reader on purpose, so counting it on the two per-chunk paths
        wrote a line every CAPTURE_REASON_LOG_INTERVAL_S seconds into
        that provider's wheelhouse.log for the life of the process
        (wh-capture-load-gaps.1.1). The other two reasons are real
        failures that can start and stop, so they keep the timer below.

        At most one line per CAPTURE_REASON_LOG_INTERVAL_S. Every
        suppressed call is still counted, and the next line names each
        reason with its count, so a band that mixes two causes cannot be
        read as one. The counts are cleared only when a line is written,
        never on a suppressed call.

        Args:
            reason: which None case fired -- "no-reader", "reader-raised"
                or "non-dict".
            detail: the exception text, or the type that was not a dict.
                The newest one wins; only one is carried, because the
                line is a pointer to where to look next, not a record of
                every failure.
        """
        say_now = False
        receipt = None
        if reason == "no-reader":
            if not self._a_no_reader_line_would_be_heard():
                # Say nothing, count nothing, and leave the latch open.
                # A line written now would not reach wheelhouse.log, and
                # closing the latch on it would leave the log with no line
                # at all. A later reading says it instead
                # (wh-capture-load-gaps.2.1, .2.2).
                return
            if self._capture_no_reader_reported:
                return
            # The latch is NOT spent here. The check above decides only
            # whether to write the line; what spends the one-shot report
            # is the hand-over itself, reported back on this receipt
            # below. The two answers can differ inside one call, because
            # the forwarder's own thread can close the connection between
            # them, and the report was then lost for the life of the
            # process (wh-capture-load-gaps.2.3).
            receipt = new_delivery_receipt()
            # Past the timer, because one line for the whole process
            # cannot flood anything, and because a count left waiting on
            # a timer here would wait forever: the three reasons are
            # mutually exclusive per processor, so with no reader there
            # is no later reason to carry it out.
            say_now = True
        self._capture_reason_counts[reason] = (
            self._capture_reason_counts.get(reason, 0) + 1)
        if detail is not None:
            self._capture_reason_detail = detail
        now = time.monotonic()
        if (not say_now
                and self._capture_reason_logged_at is not None
                and now - self._capture_reason_logged_at
                < CAPTURE_REASON_LOG_INTERVAL_S):
            return
        self._capture_reason_logged_at = now
        counts = " ".join(
            f"{name}={count}" for name, count
            in sorted(self._capture_reason_counts.items()))
        suffix = (f" (last: {self._capture_reason_detail})"
                  if self._capture_reason_detail else "")
        # A prefix of its own, NOT the per-utterance "[load-diag]" one.
        # The two are different records -- that line is a measurement,
        # this one says a measurement could not be taken -- and a reader
        # grepping "load-diag" in wheelhouse.log still finds both.
        load_metrics_logger.info(
            "[load-diag-capture] unavailable: %s%s", counts, suffix,
            extra=(None if receipt is None
                   else {DELIVERY_RECEIPT_ATTR: receipt}))
        if receipt is not None and not receipt['dropped']:
            # Spent only now, and only because no handler reported that it
            # could not deliver this record. A handler that dropped it, or
            # one whose send raised, leaves the latch open so a later
            # reading says the line again (wh-capture-load-gaps.2.3).
            self._capture_no_reader_reported = True
        self._capture_reason_counts.clear()
        self._capture_reason_detail = None

    def _sample_capture_history(self, pcm_bytes: bytes) -> None:
        """Keep one capture snapshot per chunk the lead-in buffer keeps.

        Trimmed by the audio-byte rule LeadInBuffer.add evicts by, so the
        entries after the oldest are exactly that buffer's chunks and the
        oldest one is the baseline: the counters as they stood before the
        oldest retained chunk was captured.

        Args:
            pcm_bytes: the same raw chunk just added to the lead-in buffer.
                Its length is what pairs the two, and it is also the bound
                on this deque, so an empty chunk is refused (wh-stt-load-
                metrics.1.5).
        """
        if not pcm_bytes:
            return
        stats = self._read_capture_stats()
        if stats is None:
            return
        self._load_capture_history.append((len(pcm_bytes), stats))
        self._load_capture_history_bytes += len(pcm_bytes)
        if (self._load_capture_history_bytes
                > self._lead_in_capacity_bytes):
            # LeadInBuffer.add evicts while its total EXCEEDS the same
            # capacity, so the audio sampled here outgrowing it means that
            # buffer has already dropped its oldest chunk, and the entry at
            # the front of this deque was read before every chunk the buffer
            # still holds. That is a genuine baseline even when no seed
            # reading was available (wh-stt-load-metrics.1.6).
            #
            # The trim below cannot carry this: it keeps one entry MORE than
            # the lead-in holds, so it first pops a full chunk later than the
            # buffer's own first eviction, and the line printed n/a for that
            # extra chunk while the numbers were already there
            # (wh-stt-load-metrics.1.8). Sealing any earlier is equally wrong
            # -- a lead-in filled EXACTLY evicts nothing, so its first chunk
            # is still retained and no entry precedes it.
            self._load_capture_baseline_sealed = True
        while (len(self._load_capture_history) > 1
               and (self._load_capture_history_bytes
                    - self._load_capture_history[0][0])
               > self._lead_in_capacity_bytes):
            self._load_capture_history_bytes -= (
                self._load_capture_history.popleft()[0])

    def _seed_capture_history(self) -> None:
        """Open the history with a reading that predates the audio it holds.

        A snapshot taken while a chunk is processed already contains whatever
        that chunk lost, so until the lead-in buffer evicts something no entry
        is older than its oldest retained chunk. This entry is that older
        reading: taken at construction before the first chunk was captured,
        and again when an utterance ends before the next one's first chunk
        (wh-stt-load-metrics.1.6). It carries no audio, so the byte rule that
        trims the history neither charges it nor keeps it any longer than the
        chunk entries around it.

        A reader that cannot answer yet leaves the history unopened rather
        than guessing. The line then says n/a until the buffer's first
        eviction supplies a real predecessor.
        """
        stats = self._read_capture_stats()
        if stats is None:
            return
        self._load_capture_history.append((0, stats))
        self._load_capture_baseline_sealed = True

    def seed_capture_baseline_before_capture_starts(self) -> None:
        """Open the history with the zero every capture counter starts at.

        Call this from the provider immediately BEFORE it starts audio
        capture, and only there. Both other moments are wrong:

        At construction the capture object may have no stream yet, and
        then answers fewer keys than a started capture does.
        _log_load_metrics prints n/a for a key missing from EITHER end, so
        that reading withheld the first utterance's callback counters
        (wh-stt-load-metrics.1.9). The case was measured on
        SounddeviceAudioCapture, which answered four keys instead of six
        before its stream existed; that class is deleted
        (wh-portaudio-capture-removal).

        After capture start() returns is worse. The callback begins filling
        the queue the moment the stream opens, and under the CPU starvation
        this measurement exists to expose the provider thread can be
        preempted before it takes a reading. Nothing discards that queue, so
        those frames become the first utterance's audio while their loss
        already sits in the baseline -- overflow=0 for an utterance that lost
        frames, which is the single reading that rules capture loss out
        (wh-stt-load-metrics.1.10).

        Before the stream opens the counters are provably zero rather than
        merely unread: the capture builds its stream inside start(), so
        every counter it will later report starts at zero. So this records
        zero for every counter the line subtracts, including any the
        pre-stream reader cannot answer, and takes no reading at all.

        A provider that never counts a key is unaffected. The WinRT capture
        path has no PortAudio status flags at either end, and _log_load_metrics
        returns n/a when the key is missing from either side, so a zero here
        cannot turn that honest n/a into overflow=0.

        The caller is asserting that its capture source has counted nothing
        yet. A caller that is wrong about that over-reports rather than
        under-reports: the utterance is charged for loss that predates it,
        which is the direction that does not rule capture loss out.
        """
        self._load_capture_history.clear()
        self._load_capture_history_bytes = 0
        self._load_capture_history.append(
            (0, dict.fromkeys(CAPTURE_DELTA_KEYS, 0)))
        self._load_capture_baseline_sealed = True

    def _capture_baseline(self) -> Optional[dict]:
        """The capture counters as they stood before this utterance's oldest
        retained audio was captured, or None when no such reading exists.

        None makes the line print n/a for all three capture fields, which is
        the honest answer and the one that matters here: this measurement
        exists to rule capture loss in or out, so a zero delta computed from
        a baseline that already contained the loss would be a false all-clear
        (wh-stt-load-metrics.1.6).
        """
        if (not self._load_capture_baseline_sealed
                or not self._load_capture_history):
            return None
        return self._load_capture_history[0][1]

    def _sample_capture_queue_depth(self) -> None:
        """Record one capture-queue depth sample for this utterance."""
        stats = self._read_capture_stats()
        if stats is None:
            return
        depth = stats.get('qsize')
        if not isinstance(depth, int):
            return
        self._load_q_samples += 1
        self._load_q_sum += depth
        if depth > self._load_q_max:
            self._load_q_max = depth

    def _log_load_metrics(self, endpoint_kind: str) -> None:
        """Report this utterance's capture and inference load numbers.

        One INFO line per utterance, beside [vad_utt_stats], reading which of
        the two CPU-load failures happened (wh-stt-load-metrics):

          - overflow above zero means the capture callback lost input frames,
            so the recognizer was fed damaged audio.
          - engine_ratio above 1.0 means the recognizer spent more wall-clock
            time than the audio it consumed represents, so it cannot keep up
            with real time and the words arrive late.

        Both can be true at once, which is why both are printed rather than a
        single verdict.
        """
        try:
            deltas = diagnostics.capture_deltas(
                self._load_capture_at_open, self._read_capture_stats())
            overflow, status_flags, drops = (
                deltas[key] for key in CAPTURE_DELTA_KEYS)

            q_mean = (self._load_q_sum / self._load_q_samples
                      if self._load_q_samples else 0.0)
            engine_ms_total = self._load_engine_s_total * 1000.0
            engine_ms_max = self._load_engine_s_max * 1000.0
            audio_ms = (self._load_engine_audio_samples / self.sample_rate
                        * 1000.0) if self.sample_rate else 0.0
            ratio = (engine_ms_total / audio_ms) if audio_ms else 0.0

            load_metrics_logger.info(
                "[load-diag] utt=%d kind=%s overflow=%s status_flags=%s "
                "drops=%s q_max=%d q_mean=%.1f q_n=%d engine_calls=%d "
                "engine_ms_total=%.1f engine_ms_max=%.1f audio_ms=%.0f "
                "engine_ratio=%.2f %s",
                self.current_utterance_id,
                endpoint_kind,
                overflow,
                status_flags,
                drops,
                self._load_q_max,
                q_mean,
                self._load_q_samples,
                self._load_engine_calls,
                engine_ms_total,
                engine_ms_max,
                audio_ms,
                ratio,
                self._pre_engine_work.fields(self.sample_rate),
            )
        except Exception as e:
            logger.warning("Failed to log load metrics: %s", e)

    def _force_finalize_and_reset(self) -> None:
        """Force utterance finalization based on VAD silence.

        Called when VAD trailing silence exceeds force_endpoint_silence_ms.
        Asks the engine to finalize (if it supports it), then sends the
        final result and resets for the next utterance.
        """
        # Ask engine to run final inference (if it supports finalize()).
        # Timed for wh-stt-load-metrics, the same way _process_speech_audio
        # times process_audio: on the parakeet provider finalize() recognizes
        # the whole utterance buffer in one pass, so it is the single most
        # expensive engine call of a forced endpoint. Leaving it untimed made
        # engine_ratio read near zero on kind=force lines, which rules
        # inference lag out at exactly the moment it is happening.
        # The audio it consumes is NOT added to _load_engine_audio_samples:
        # those samples were already counted when the chunks were fed, and
        # counting them twice would inflate the denominator and understate
        # the ratio again by another route.
        finalize = getattr(self.engine, 'finalize', None)
        if finalize:
            _load_t0 = time.perf_counter()
            finalize()
            _load_elapsed = time.perf_counter() - _load_t0
            self._load_engine_calls += 1
            self._load_engine_s_total += _load_elapsed
            if _load_elapsed > self._load_engine_s_max:
                self._load_engine_s_max = _load_elapsed

        text = self.engine.get_result()

        if text:
            logger.info(f"FINAL [{self.current_utterance_id}] (forced): '{redact_transcript(text)}'")
            if self.forwarder:
                tid = getattr(self, '_current_trace_id', '')
                confidence = self._get_final_confidence()
                if confidence is not None:
                    self.forwarder.send_final(
                        text, self.current_utterance_id,
                        trace_id=tid, confidence=confidence,
                    )
                else:
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
        self._reset_load_metrics()  # wh-stt-load-metrics
        self._flush_pre_engine_idle('reset')
        self._pre_engine_history.clear()
        self._pre_engine_history_bytes = 0
        # wh-stt-load-metrics.1.3: this utterance's window is closed, so its
        # snapshots must not become the next utterance's baseline and make it
        # report the same loss a second time.
        self._load_capture_history.clear()
        self._load_capture_history_bytes = 0
        # wh-stt-load-metrics.1.6: the closing read for this utterance has
        # already been taken, so a reading now predates every chunk the next
        # utterance will keep, and nothing counted here is charged twice.
        self._load_capture_baseline_sealed = False
        self._seed_capture_history()
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
