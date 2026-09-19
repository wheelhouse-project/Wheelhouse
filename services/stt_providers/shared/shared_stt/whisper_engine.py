"""Chunked re-inference streaming engine for faster-whisper.

Implements RecognitionEngine protocol. Accumulates audio in a buffer,
runs periodic inference via faster-whisper, and uses LocalAgreement-2
stability detection to determine which words are confirmed.

Reference: docs/design/chunked_streaming_engine_design.md
"""
import logging
import math
import re
import time

import numpy as np
from faster_whisper import WhisperModel

from shared_stt.redact import redact_transcript
from shared_stt.transcript_rules import has_capital_evidence

logger = logging.getLogger(__name__)


def _coerce_threshold(
    name: str, value, default: float, *, allow_neg_infinite: bool = False
) -> float:
    """wh-7ou.6.1.2: read a numeric tunable defensively.

    These thresholds are first USED mid-utterance (at final inference), so a
    malformed config value -- a quoted TOML number, a list, garbage text --
    must not survive to that comparison: a TypeError there loses the
    utterance and exits the provider's audio loop. A quoted number coerces
    cleanly; anything float() cannot read (or a bool, NaN, an int too large
    for float, or an infinity where the documented semantics do not use one)
    falls back to the default with a warning, matching the degrade-never-raise
    pattern config validation uses elsewhere in this project
    (ClickConfig.from_raw). Booleans are rejected before conversion because
    TOML true/false float() to 1.0/0.0, which would silently disable a
    criterion instead of surfacing the config mistake (wh-7ou.6.1.7). Only
    NEGATIVE infinity is a documented off-switch; TOML `inf` parses positive
    and would suppress every final (wh-7ou.6.1.6).
    """
    if isinstance(value, bool):
        logger.warning(
            "[engine_config] %s=%r is a boolean, not a number; using default %s",
            name, value, default,
        )
        return default
    try:
        coerced = float(value)
    except (TypeError, ValueError, OverflowError):
        logger.warning(
            "[engine_config] %s=%r is not a number; using default %s",
            name, value, default,
        )
        return default
    if math.isnan(coerced) or (
        math.isinf(coerced) and not (allow_neg_infinite and coerced < 0)
    ):
        logger.warning(
            "[engine_config] %s=%r is not usable; using default %s",
            name, value, default,
        )
        return default
    return coerced


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

    # wh-7ou.7.1.2: calibration mode auto-disables this long after the last
    # enable, so a crashed Logic process (whose disconnect the WSForwarder
    # reset may miss on a half-open connection) can never leave the
    # hallucination filter bypassed indefinitely.
    _CALIBRATION_MODE_TIMEOUT_S = 600.0

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
        single_word_min_probability: float = 0.6,
        single_word_max_no_speech_prob: float = 0.03,
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
        # allow_neg_infinite: -inf is that documented off-switch.
        self._hallucination_logprob_threshold = _coerce_threshold(
            "hallucination_logprob_threshold",
            hallucination_logprob_threshold,
            -0.5,
            allow_neg_infinite=True,
        )
        self._peak_avg_logprob: float = -float("inf")

        # wh-7ou.6 single-word rescue: avg_logprob is systematically lower on
        # one-word utterances than on phrases (field data 2026-08-06: real
        # 'comma' peaked -0.596..-0.643 against the -0.55 threshold, on the
        # same voice/mic the threshold was calibrated with), so a suppressed
        # single-word final gets a second chance judged on the word's OWN
        # decode probability plus the final segments' no_speech_prob.
        # min_probability=0.0 ignores the word-probability criterion;
        # max_no_speech_prob=1.0 ignores the no-speech criterion;
        # min_probability=2.0 disables the rescue entirely.
        self._single_word_min_probability = _coerce_threshold(
            "single_word_min_probability", single_word_min_probability, 0.6
        )
        self._single_word_max_no_speech_prob = _coerce_threshold(
            "single_word_max_no_speech_prob", single_word_max_no_speech_prob, 0.03
        )

        # wh-7ou.7.1.1: measurement block computed by _run_final_inference and
        # read by AudioProcessor via get_final_confidence(); None until a
        # final inference has run in the current utterance.
        self._final_confidence: dict | None = None

        # wh-7ou.7.1.2 calibration mode (spec Section 5.2): while enabled,
        # _run_final_inference delivers a final the hallucination filter
        # would suppress, so the voice-calibration session receives every
        # final with its text. Deliberately NOT cleared by reset() -- a
        # session spans many utterances. Cleared by set_calibration_mode
        # (False), by the WSForwarder disconnect reset, and by the timeout
        # above.
        self._calibration_mode: bool = False
        self._calibration_mode_enabled_at: float = 0.0

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

    def set_calibration_mode(self, enabled: bool) -> None:
        """wh-7ou.7.1.2: enable or disable the calibration suppression bypass.

        Driven by the Logic process's set_calibration_mode WebSocket command
        (and, with False, by the WSForwarder disconnect reset). Enabling
        while already enabled restarts the ten-minute safety window, so the
        Logic process can keep a long session alive by re-sending. Survives
        the per-utterance reset(); see _CALIBRATION_MODE_TIMEOUT_S for why
        it cannot outlive a crashed Logic process.
        """
        if enabled:
            self._calibration_mode = True
            self._calibration_mode_enabled_at = time.monotonic()
            logger.info(
                "[calibration_mode] enabled (auto-off after %.0fs)",
                self._CALIBRATION_MODE_TIMEOUT_S,
            )
        else:
            if self._calibration_mode:
                logger.info("[calibration_mode] disabled")
            self._calibration_mode = False

    def _calibration_mode_active(self) -> bool:
        """True while the bypass applies; lazily enforces the safety timeout.

        Checked at final inference (the only place the mode changes
        behavior), so no timer thread is needed: once the window has
        passed, the very next final is filtered normally and the stale
        flag is cleared.
        """
        if not self._calibration_mode:
            return False
        elapsed = time.monotonic() - self._calibration_mode_enabled_at
        if elapsed > self._CALIBRATION_MODE_TIMEOUT_S:
            self._calibration_mode = False
            logger.warning(
                "[calibration_mode] safety timeout after %.0fs; "
                "suppression restored",
                elapsed,
            )
            return False
        return True

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
        self._final_confidence = None

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
            # wh-7ou.6: per-word probabilities are read only here, by the
            # single-word rescue; interim passes skip the alignment cost.
            word_timestamps=True,
        )
        # Materialized because the rescue re-reads the segments after
        # _extract_text has consumed the faster-whisper generator.
        segments = list(segments)

        text = self._extract_text(segments)
        current_words = text.split() if text else []

        # wh-7ou.2 hallucination filter. Apply at final inference (not
        # interim) so flaky mid-utterance confidence drops don't discard
        # real speech -- a single high-confidence segment anywhere in the
        # inference history is enough to clear the threshold. Suppression
        # clears confirmed_words so get_result() returns empty; is_endpoint
        # still fires (self._finalized = True) so audio_processor can reset
        # and emit an AGC failure signal.
        # wh-7ou.6: a one-word transcript gets a second chance first (see
        # _single_word_rescue) -- the segment-average confidence this
        # threshold judges is systematically lower on single words.
        suppressed = False
        rescued = False
        if self._peak_avg_logprob < self._hallucination_logprob_threshold:
            if len(current_words) == 1 and self._single_word_rescue(segments):
                self._confirmed_words = current_words
                rescued = True
            elif self._calibration_mode_active():
                # wh-7ou.7.1.2: calibration bypass. The flag still reports
                # the filter's verdict (a suppressed=true final that
                # actually arrives is the noise sample calibration wants);
                # only the drop is skipped. Numbers-only log line, per the
                # calibration privacy discipline.
                suppressed = True
                self._confirmed_words = current_words
                logger.info(
                    "[calibration_mode] suppression bypassed: "
                    "peak_logprob=%.3f < threshold=%.3f word_count=%d",
                    self._peak_avg_logprob,
                    self._hallucination_logprob_threshold,
                    len(current_words),
                )
            else:
                suppressed = True
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

        # wh-7ou.7.1.1: measurement block for this final, attached to every
        # final (not only during calibration) via get_final_confidence().
        self._final_confidence = self._build_final_confidence(
            segments, len(current_words), suppressed, rescued
        )

        self._finalized = True
        self._has_new_result = True

    def _build_final_confidence(
        self, segments, word_count: int, suppressed: bool, rescued: bool
    ) -> dict:
        """wh-7ou.7.1.1: measurement block for the just-run final inference.

        Read by AudioProcessor (get_final_confidence) and attached to the
        final WebSocket message as the optional "confidence" object, so the
        Logic process can calibrate the single-word rescue thresholds without
        reading provider logs. A value that is missing or not a finite
        probability in [0, 1] reports None rather than a guess (the same
        refusal discipline as _single_word_rescue), and the -inf peak
        sentinel reports None because it is not JSON-serializable. The log
        line prints numbers only, never transcript text.
        """
        min_word_prob: float | None = None
        try:
            word_probs = [
                float(w.probability)
                for seg in segments
                for w in (getattr(seg, "words", None) or [])
            ]
            if word_probs and all(
                math.isfinite(p) and 0.0 <= p <= 1.0 for p in word_probs
            ):
                min_word_prob = min(word_probs)
        except Exception:
            min_word_prob = None
        max_no_speech: float | None = None
        try:
            no_speech_probs = [
                float(getattr(seg, "no_speech_prob", None)) for seg in segments
            ]
            if no_speech_probs and all(
                math.isfinite(p) and 0.0 <= p <= 1.0 for p in no_speech_probs
            ):
                max_no_speech = max(no_speech_probs)
        except Exception:
            max_no_speech = None
        peak_avg_logprob = (
            self._peak_avg_logprob
            if math.isfinite(self._peak_avg_logprob)
            else None
        )
        logger.info(
            "[final_confidence] min_word_prob=%s max_no_speech=%s "
            "peak_logprob=%s word_count=%d suppressed=%s rescued=%s",
            "none" if min_word_prob is None else format(min_word_prob, ".3f"),
            "none" if max_no_speech is None else format(max_no_speech, ".3f"),
            "none" if peak_avg_logprob is None else format(peak_avg_logprob, ".3f"),
            word_count,
            suppressed,
            rescued,
        )
        return {
            "min_word_probability": min_word_prob,
            "max_no_speech_prob": max_no_speech,
            "peak_avg_logprob": peak_avg_logprob,
            "word_count": word_count,
            "suppressed": suppressed,
            "rescued": rescued,
        }

    def get_final_confidence(self) -> dict | None:
        """wh-7ou.7.1.1: measurement block for the last final inference.

        None until a final inference has run in the current utterance;
        cleared by reset(). AudioProcessor reads this via getattr, so
        engines without the method (Parakeet, the cloud providers) need no
        change.
        """
        return self._final_confidence

    def _single_word_rescue(self, segments) -> bool:
        """wh-7ou.6: second chance for a suppressed one-word final transcript.

        Judged on the final inference only, using two signals:
        - every decoded word's probability must be at least
          single_word_min_probability (the word itself was heard clearly,
          regardless of the low segment average), and
        - every segment's no_speech_prob must be at most
          single_word_max_no_speech_prob (the audio was near-certainly
          speech; field data 2026-08-06: real one-word finals 0.008-0.017,
          cough-driven 'Thank you.' hallucinations 0.044-0.089).

        Missing, unreadable, or out-of-range word-level data keeps the
        suppression (wh-7ou.6.1.1). Every
        decision is logged so users can calibrate the two thresholds from
        live [single_word_rescue] lines.
        """
        try:
            words = [w for seg in segments for w in (getattr(seg, "words", None) or [])]
            if not words:
                logger.info(
                    "[single_word_rescue] no word-level data; keeping suppression"
                )
                return False
            word_probs = [float(w.probability) for w in words]
            no_speech_probs = [
                float(getattr(seg, "no_speech_prob", 1.0)) for seg in segments
            ]
            # wh-7ou.6.1.1: min()/max() skip a NaN that follows a finite
            # value, and an out-of-range score (an infinity, a negative)
            # can satisfy a one-sided comparison, so malformed data is
            # rejected before aggregation: every value must be a finite
            # probability in [0, 1] or the rescue refuses.
            if not all(
                math.isfinite(p) and 0.0 <= p <= 1.0
                for p in word_probs + no_speech_probs
            ):
                logger.warning(
                    "[single_word_rescue] non-finite or out-of-range "
                    "confidence value; keeping suppression"
                )
                return False
            min_prob = min(word_probs)
            max_no_speech = max(no_speech_probs)
            rescued = (
                min_prob >= self._single_word_min_probability
                and max_no_speech <= self._single_word_max_no_speech_prob
            )
        except Exception as e:
            logger.warning(
                "[single_word_rescue] could not read word data (%s); "
                "keeping suppression", e,
            )
            return False
        logger.info(
            "[single_word_rescue] %s: min_word_prob=%.3f (need >= %.2f) "
            "max_no_speech=%.3f (need <= %.2f) peak_logprob=%.3f",
            "RESCUED" if rescued else "not rescued",
            min_prob,
            self._single_word_min_probability,
            max_no_speech,
            self._single_word_max_no_speech_prob,
            self._peak_avg_logprob,
        )
        return rescued

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

        # Lowercase only the first character (sentence-start normalization).
        # Preserves proper noun capitalization within the text, and leaves
        # the first character alone when the utterance carries a capital
        # that is not merely positional (wh-first-char-lowercase). This is
        # the second copy of the rule; transcript_rules.py holds the first
        # and owns the shared condition.
        if text and not has_capital_evidence(text):
            text = text[0].lower() + text[1:]

        # Always capitalize the pronoun "I" and its contractions (I'm, I've, I'd, I'll)
        text = re.sub(r'\bi\b', 'I', text)

        # Whisper time/dollar quirk rules (wh-ocwbk). Applied last so the
        # inserted colon survives the punctuation pass above.
        text = apply_whisper_text_rules(text)

        return text
