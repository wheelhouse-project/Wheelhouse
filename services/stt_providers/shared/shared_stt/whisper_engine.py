"""Chunked re-inference streaming engine for faster-whisper.

Implements RecognitionEngine protocol. Accumulates audio in a buffer,
runs periodic inference via faster-whisper, and uses LocalAgreement-2
stability detection to determine which words are confirmed.

Reference: docs/design/chunked_streaming_engine_design.md
"""
import logging
import re

import numpy as np
from faster_whisper import WhisperModel

from shared_stt.redact import redact_transcript

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Whisper output text rules (wh-ocwbk, migrated from the Logic process's
# TextNumerizer as part of retiring it -- epic wh-251rh). These rules encode
# Whisper-specific output quirks, so they belong in the engine that produces
# them, not at the WebSocket boundary in the Logic process.
# ---------------------------------------------------------------------------

# Rule 1: Time formatting. Matches "9.45 am", "9.45am", "9 45 am", "945 am",
# "1230 pm", etc. Requires the AM/PM anchor to avoid false positives on
# plain decimals like "9.45". The trailing \b keeps words that merely
# START with am/pm out of the rule ("230 amps" must not become
# "2:30 AMps" -- wh-251rh.1.2, an inherited TextNumerizer hole fixed
# post-migration). The LEADING \b keeps longer digit runs from partially
# rewriting ("123.45 am" must not become "1" + "23:45 AM" -- wh-251rh.3,
# same inheritance).
_TIME_PERIOD = re.compile(
    r'\b(\d{1,2})\.(\d{2})\s*(am|pm)\b',
    re.IGNORECASE,
)
_TIME_SPACE = re.compile(
    r'\b(\d{1,2})\s(\d{2})\s+(am|pm)\b',
    re.IGNORECASE,
)
_TIME_CONCAT = re.compile(
    r'\b(\d{1,2})(\d{2})\b\s+(am|pm)\b',
    re.IGNORECASE,
)

# Rule 2: Redundant dollar word. Matches "$200 dollars", "$12.50 dollars".
_REDUNDANT_DOLLAR = re.compile(
    r'(\$[\d,]+(?:\.\d+)?)\s+dollars\b',
    re.IGNORECASE,
)


def _time_replace(match: re.Match) -> str:
    """Format time match as H:MM AM/PM; leave non-clock values alone.

    wh-251rh.3.1: only values that read as a real 12-hour clock time may
    rewrite. An AM-radio frequency ("980 am" -> minutes 80), a decimal
    measurement ("0.75 am" -> hour 0), or an out-of-range pair
    ("13.99 pm") is returned unchanged. A genuinely ambiguous valid pair
    ("1010 am" radio vs 10:10 AM) still rewrites -- indistinguishable
    without context, and the retired TextNumerizer behaved the same.
    """
    hour = match.group(1)
    minutes = match.group(2)
    if not (1 <= int(hour) <= 12 and 0 <= int(minutes) <= 59):
        return match.group(0)
    ampm = match.group(3).upper()
    return f"{hour}:{minutes} {ampm}"


def apply_whisper_text_rules(text: str) -> str:
    """Apply the Whisper output cleanup rules to extracted text.

    Pure function, stateless. Called at the end of
    :meth:`WhisperStreamingEngine._extract_text`, after the existing
    punctuation/case normalization, so the inserted colon is not stripped
    by the punctuation pass. Because _extract_text runs on EVERY inference
    pass (interim stability re-inferences as well as the endpoint final),
    the rules apply to interim text too -- unlike the retired Logic-side
    TextNumerizer, which saw only final transcripts (wh-251rh.1.3).
    """
    if not text:
        return text

    # Rule 1: Time formatting (period, then space, then concat)
    text = _TIME_PERIOD.sub(_time_replace, text)
    text = _TIME_SPACE.sub(_time_replace, text)
    text = _TIME_CONCAT.sub(_time_replace, text)

    # Rule 2: Redundant dollar word
    text = _REDUNDANT_DOLLAR.sub(r'\1', text)

    return text


class WhisperStreamingEngine:
    """Chunked re-inference streaming engine for faster-whisper.

    Implements RecognitionEngine protocol. Accumulates audio in a buffer,
    runs periodic inference via faster-whisper, and uses LocalAgreement-2
    stability detection to determine which words are confirmed.

    The engine operates in three phases per utterance:
    1. Accumulate audio chunks into a growing buffer
    2. Periodically re-run Whisper on the full buffer (at re_inference_interval_ms)
    3. Compare consecutive outputs via LocalAgreement-2 to confirm stable words

    On endpoint (trailing silence), a final inference promotes all words.
    """

    def __init__(
        self,
        model_size_or_path: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "float16",
        re_inference_interval_ms: int = 400,
        endpoint_silence_ms: int = 500,
        silence_rms_threshold: float = 0.01,
        beam_size: int = 5,
        language: str = "en",
        sample_rate: int = 16000,
        max_buffer_duration_s: float = 30.0,
        hallucination_logprob_threshold: float = -0.5,
        hotwords: str | None = None,
    ):
        self._model = WhisperModel(model_size_or_path, device=device, compute_type=compute_type)
        self._beam_size = beam_size
        self._language = language
        self._sample_rate = sample_rate

        # wh-apmg: faster-whisper's decoder-bias string ("word, word, ...").
        # This is the true boost mechanism for user hints, distinct from
        # initial_prompt (which conditions style and burns the 224-token
        # prompt window). Hotwords share that same window; callers cap the
        # string length (see distil_medium_en build_hotwords_string).
        self._hotwords = hotwords or None
        if self._hotwords:
            # wh-apmg.1.3: the hallucination-filter calibration predates
            # hotwords; log the bias once so field reports of resumed
            # hallucination leakage can be correlated with it.
            logger.info(f"hotwords active: {len(self._hotwords)} chars")

        # Timing thresholds (converted to sample counts)
        self._re_inference_interval_samples = int(sample_rate * re_inference_interval_ms / 1000)
        self._endpoint_silence_samples = int(sample_rate * endpoint_silence_ms / 1000)
        self._silence_rms_threshold = silence_rms_threshold

        # Max buffer cap (safety net against unbounded growth)
        self._max_buffer_samples = int(sample_rate * max_buffer_duration_s)

        # Audio buffer (list of float32 numpy arrays)
        self._audio_buffer: list[np.ndarray] = []

        # Sample counters
        self._samples_since_last_inference: int = 0
        self._trailing_silence_samples: int = 0
        self._total_buffer_samples: int = 0

        # Speech detection gate
        self._speech_detected: bool = False

        # LocalAgreement-2 state
        self._prev_words: list[str] = []
        self._confirmed_words: list[str] = []

        # Result state
        self._has_new_result: bool = False
        self._finalized: bool = False
        self.last_result: str = ""

        # wh-7ou.2 hallucination filter: track peak avg_logprob across all
        # inference runs in an utterance. At final inference, if the peak
        # never reached the threshold, suppress the transcript. Hallucinations
        # on non-speech audio (throat clears, coughs) never achieve Whisper's
        # confidence band for articulated speech (~-0.2 to -0.5); training-data
        # priors fire at ~-0.6 to -0.9. Set to -inf to disable.
        self._hallucination_logprob_threshold = hallucination_logprob_threshold
        self._peak_avg_logprob: float = -float("inf")

    def process_audio(self, audio_bytes: bytes) -> None:
        """Process an audio chunk (float32 bytes).

        Appends to the buffer, tracks silence/speech, and triggers
        inference when the re-inference interval elapses or endpoint
        is reached.
        """
        self._has_new_result = False

        samples = np.frombuffer(audio_bytes, dtype=np.float32)
        self._audio_buffer.append(samples)

        # Track silence/speech via RMS energy
        self._track_silence(samples)

        n_samples = len(samples)
        self._samples_since_last_inference += n_samples
        self._total_buffer_samples += n_samples

        # Check max buffer cap (safety net against unbounded growth)
        if self._total_buffer_samples >= self._max_buffer_samples:
            if not self._finalized:
                logger.warning(
                    f"Audio buffer exceeded max duration "
                    f"({self._total_buffer_samples / self._sample_rate:.1f}s) - "
                    f"forcing finalization"
                )
                self._run_final_inference()
            return

        # Check endpoint first (trailing silence after speech)
        if self._speech_detected and self._trailing_silence_samples >= self._endpoint_silence_samples:
            if not self._finalized:
                self._run_final_inference()
            return

        # Check if we should run periodic inference
        if (
            self._speech_detected
            and self._samples_since_last_inference >= self._re_inference_interval_samples
        ):
            self._run_inference()

    def is_ready(self) -> bool:
        """Check if results are available.

        Returns True when get_result() has meaningful data: either a new
        inference just ran, or confirmed words exist from a prior inference.
        This allows AudioProcessor to check for holdback release during
        silence even when no new inference triggered.
        """
        return self._has_new_result or bool(self._confirmed_words)

    def get_result(self) -> str:
        """Get current confirmed transcription text.

        During speech: returns confirmed words (stable across 2+ runs).
        After endpoint: returns full final text.
        """
        if not self._confirmed_words:
            return ""
        return " ".join(self._confirmed_words)

    def is_endpoint(self) -> bool:
        """Check if utterance endpoint has been reached (trailing silence)."""
        return self._finalized

    def finalize(self) -> None:
        """Force finalization: run final inference and promote all words.

        Called externally when AudioProcessor detects endpoint via VAD.
        No-op if already finalized or if no audio has been buffered.
        """
        if self._finalized:
            return
        if not self._audio_buffer:
            return
        self._run_final_inference()

    def cleanup(self) -> None:
        """Explicitly release the WhisperModel and its CUDA resources.

        Must be called before process exit to avoid unordered GC cleanup
        of CTranslate2 CUDA resources, which can crash on Windows
        (STATUS_STACK_BUFFER_OVERRUN / 0xC0000409).
        """
        if self._model is not None:
            del self._model
            self._model = None
            logger.info("WhisperModel released")

    def reset(self) -> None:
        """Reset all state for a new utterance."""
        self._audio_buffer = []
        self._samples_since_last_inference = 0
        self._trailing_silence_samples = 0
        self._total_buffer_samples = 0
        self._speech_detected = False
        self._prev_words = []
        self._confirmed_words = []
        self._has_new_result = False
        self._finalized = False
        self.last_result = ""
        self._peak_avg_logprob = -float("inf")

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    def _track_silence(self, samples: np.ndarray) -> None:
        """Track trailing silence via RMS energy."""
        rms = np.sqrt(np.mean(samples ** 2))
        if rms > self._silence_rms_threshold:
            self._trailing_silence_samples = 0
            self._speech_detected = True
        else:
            self._trailing_silence_samples += len(samples)

    def _run_inference(self) -> None:
        """Run Whisper inference on the full audio buffer and update stability."""
        audio = np.concatenate(self._audio_buffer)
        self._samples_since_last_inference = 0

        segments, _ = self._model.transcribe(
            audio,
            language=self._language,
            beam_size=self._beam_size,
            hotwords=self._hotwords,
        )

        text = self._extract_text(segments)
        current_words = text.split() if text else []

        self._update_stability(current_words)
        self._has_new_result = True

    def _run_final_inference(self) -> None:
        """Run final inference on endpoint and promote all words."""
        audio = np.concatenate(self._audio_buffer)

        segments, _ = self._model.transcribe(
            audio,
            language=self._language,
            beam_size=self._beam_size,
            hotwords=self._hotwords,
        )

        text = self._extract_text(segments)
        current_words = text.split() if text else []

        # wh-7ou.2 hallucination filter. Apply at final inference (not
        # interim) so flaky mid-utterance confidence drops don't discard
        # real speech -- a single high-confidence segment anywhere in the
        # inference history is enough to clear the threshold. Suppression
        # clears confirmed_words so get_result() returns empty; is_endpoint
        # still fires (self._finalized = True) so audio_processor can reset
        # and emit an AGC failure signal.
        if self._peak_avg_logprob < self._hallucination_logprob_threshold:
            logger.info(
                "[hallucination_suppressed] peak_logprob=%.3f < threshold=%.3f "
                "suppressed_text=%r",
                self._peak_avg_logprob,
                self._hallucination_logprob_threshold,
                redact_transcript(text),
            )
            self._confirmed_words = []
        elif current_words:
            # Promote all words (bypass 2-run requirement)
            self._confirmed_words = current_words

        self._finalized = True
        self._has_new_result = True

    def _update_stability(self, current_words: list[str]) -> None:
        """Update confirmed words using LocalAgreement-2.

        Compares current inference output with the previous run's output.
        The longest common prefix (LCP) of the two word lists is computed.
        If the LCP extends beyond the current confirmed count, new words
        are confirmed. Confirmed words never shrink (monotonicity invariant).
        """
        # wh-7ou.2: hold back confirmed-word promotion until the utterance
        # has shown at least one inference with avg_logprob >= threshold.
        # Without this guard, STABLE interim messages leak hallucinated
        # transcripts ("thank you") to WheelHouse during re-inference
        # BEFORE the final filter in _run_final_inference can suppress
        # them. Keep _prev_words fresh so that when peak eventually does
        # cross the threshold, LocalAgreement-2 has a reference point.
        if self._peak_avg_logprob < self._hallucination_logprob_threshold:
            self._prev_words = current_words
            return

        if self._prev_words:
            # Find longest common prefix
            lcp_len = 0
            for i in range(min(len(self._prev_words), len(current_words))):
                if self._prev_words[i] == current_words[i]:
                    lcp_len = i + 1
                else:
                    break

            # Extend confirmed (never shrink)
            if lcp_len > len(self._confirmed_words):
                self._confirmed_words = current_words[:lcp_len]

        self._prev_words = current_words

    def _extract_text(self, segments) -> str:
        """Extract and normalize text from Whisper segments.

        Concatenates text from all segments, strips whitespace, then:
        - Converts spelled-out letter sequences (V-O-X-T-R-A-L -> voxtral)
        - Removes punctuation (except periods preceded by a digit)
        - Lowercases only the first character (preserves proper nouns)
        """
        parts = []
        for segment in segments:
            parts.append(segment.text)
            # wh-7ou.2: track peak avg_logprob across all inference runs in
            # this utterance -- used by the hallucination filter in
            # _run_final_inference.
            avg_lp = getattr(segment, "avg_logprob", None)
            if isinstance(avg_lp, float) and avg_lp == avg_lp:  # not NaN
                if avg_lp > self._peak_avg_logprob:
                    self._peak_avg_logprob = avg_lp
            logger.info(
                "[whisper_seg] text=%r no_speech_prob=%.3f avg_logprob=%.3f "
                "compression=%.3f duration=%.2fs",
                redact_transcript(segment.text),
                getattr(segment, "no_speech_prob", float("nan")),
                avg_lp if avg_lp is not None else float("nan"),
                getattr(segment, "compression_ratio", float("nan")),
                segment.end - segment.start,
            )
        text = "".join(parts).strip()

        # Convert spelled-out words (e.g., "V-O-X-T-R-A-L" -> "voxtral")
        # Requires 3+ letters to avoid false positives on patterns like "A-1"
        text = re.sub(
            r'\b([A-Za-z](?:-[A-Za-z]){2,})\b',
            lambda m: m.group(0).replace('-', '').lower(),
            text,
        )

        # Remove punctuation, keeping periods between digits (e.g., "3.14", "2.0")
        text = re.sub(r'(?<!\d)\.|\.(?!\d)|[,!?;:]', '', text)

        # Lowercase only the first character (sentence-start normalization)
        # Preserves proper noun capitalization within the text
        if text:
            text = text[0].lower() + text[1:]

        # Always capitalize the pronoun "I" and its contractions (I'm, I've, I'd, I'll)
        text = re.sub(r'\bi\b', 'I', text)

        # Whisper time/dollar quirk rules (wh-ocwbk). Applied last so the
        # inserted colon survives the punctuation pass above.
        text = apply_whisper_text_rules(text)

        return text
