"""Chunked re-inference streaming engine for Sherpa-ONNX offline models.

Implements RecognitionEngine protocol for use with AudioProcessor.
Accumulates audio, runs periodic inference via sherpa-onnx OfflineRecognizer,
and uses LocalAgreement-2 stability detection for confirmed words.

Designed for NeMo Parakeet TDT models via sherpa-onnx.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path


# Add the venv's onnxruntime/capi/ directory to the DLL search path BEFORE
# importing sherpa_onnx. sherpa-onnx's native .pyd resolves "onnxruntime" via
# the standard Windows DLL search, which on machines with Windows ML installed
# finds C:\Windows\System32\onnxruntime.dll (1.17.x) ahead of the venv's modern
# 1.25+ DLL. The mismatch fails to load any model that uses ONNX IR/API 18+.
# This is the canonical Python 3.8+ pattern that the onnxruntime package itself
# uses internally; we replicate it here because sherpa-onnx does not.
def _add_onnxruntime_dll_directory() -> None:
    if sys.platform != "win32":
        return
    capi = Path(sys.executable).parent.parent / "Lib" / "site-packages" / "onnxruntime" / "capi"
    if capi.is_dir():
        os.add_dll_directory(str(capi))


_add_onnxruntime_dll_directory()


import numpy as np
import sherpa_onnx

logger = logging.getLogger(__name__)


# Time-reformat rule: Parakeet emits dotted-period AM/PM forms like
# "It is 8.17 p.m."; after the punctuation pass collapses "p.m." to "pm",
# this rule reshapes "8.17 pm" into "8:17 PM". The trailing \b keeps
# words that merely start with am/pm out of the rule ("0.75 amps" must
# not become "0:75 AMps" -- wh-251rh.1.2, same hole as the whisper
# engine's copy of the rule). The leading \b keeps longer digit runs
# from partially rewriting ("123.45 am" must not become "1" +
# "23:45 AM" -- wh-251rh.3).
_TIME_PERIOD = re.compile(
    r'\b(\d{1,2})\.(\d{2})\s*(am|pm)\b',
    re.IGNORECASE,
)

# Canonicalize "am" / "pm" next to an HH:MM time to uppercase AM / PM.
_AMPM_UPPERCASE = re.compile(r'(\d{1,2}:\d{2}\s+)(am|pm)\b', re.IGNORECASE)

# Phone-number hyphenation. A 10-digit block surrounded by word
# boundaries becomes NNN-NNN-NNNN. Parakeet emits the flat form for
# some voices ("7035551234") and the hyphenated form for others
# ("703-555-1234"), so this rule normalizes to the more readable
# written form. \b on both ends prevents matching 11-digit country-
# code blocks ("17035551234") or 9-digit ZIP+4 strings.
#
# Known false-positive surface: a genuinely non-phone 10-digit number
# in dictation (e.g., a 10-digit ID) will be hyphenated. This is rare
# in practice; users who need to dictate non-phone digit runs can
# pronounce them in smaller groups.
_HYPHENATE_PHONE = re.compile(r'\b(\d{3})(\d{3})(\d{4})\b')

# wh-parakeet-xray-hotword: a deliberate pause between the syllables of
# "x-ray" makes Parakeet emit two words (measured raw form: "X, Ray
# Boost"), which breaks the Logic-side wake-word match -- the wake word
# must arrive as ONE word. Rejoin the single letter x + the word "ray"
# into the standard English spelling, keeping the x's case. Runs after
# the punctuation pass, which has already collapsed "X, Ray" to "X Ray".
# The \b on both ends keeps longer words out ("Max ray", "x raymond").
_XRAY_JOIN = re.compile(r'\b([Xx])\s+[Rr]ay\b')


def _time_replace(match: re.Match) -> str:
    # wh-251rh.3.1: only values that read as a real 12-hour clock time may
    # rewrite ("0.75 am" / "13.99 pm" are measurements, not times). Same
    # validation as the whisper engine's copy.
    hour = match.group(1)
    minutes = match.group(2)
    if not (1 <= int(hour) <= 12 and 0 <= int(minutes) <= 59):
        return match.group(0)
    ampm = match.group(3).upper()
    return f"{hour}:{minutes} {ampm}"


class SherpaOfflineEngine:
    """Chunked re-inference engine using sherpa-onnx OfflineRecognizer.

    Implements RecognitionEngine protocol. Accumulates audio in a buffer,
    runs periodic inference, and uses LocalAgreement-2 stability detection
    to determine which words are confirmed.

    NeMo Parakeet TDT models use feature_dim=128 and model_type='nemo_transducer'.
    Models with external weights (encoder.weights) require loading from the
    model directory.
    """

    def __init__(
        self,
        model_path: str,
        use_gpu: bool = False,
        gpu_device_id: int = 0,
        re_inference_interval_ms: int = 600,
        endpoint_silence_ms: int = 800,
        silence_rms_threshold: float = 0.01,
        sample_rate: int = 16000,
        max_buffer_duration_s: float = 30.0,
        num_threads: int = 4,
        hotwords_file: str | None = None,
        hotwords_score: float = 2.0,
    ):
        self._sample_rate = sample_rate
        self._recognizer = None

        # Timing thresholds (converted to sample counts)
        self._re_inference_interval_samples = int(sample_rate * re_inference_interval_ms / 1000)
        self._endpoint_silence_samples = int(sample_rate * endpoint_silence_ms / 1000)
        self._silence_rms_threshold = silence_rms_threshold
        self._max_buffer_samples = int(sample_rate * max_buffer_duration_s)

        # Load model
        self._load_model(
            model_path, use_gpu, gpu_device_id, num_threads,
            hotwords_file, hotwords_score,
        )

        # Audio buffer
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

    def _load_model(
        self,
        model_path: str,
        use_gpu: bool,
        gpu_device_id: int,
        num_threads: int,
        hotwords_file: str | None = None,
        hotwords_score: float = 2.0,
    ) -> None:
        """Load sherpa-onnx OfflineRecognizer from model directory."""
        model_dir = Path(model_path)
        if not model_dir.exists():
            raise FileNotFoundError(f"Model directory not found: {model_dir}")

        # Detect model file naming (int8 quantized vs full precision)
        encoder = model_dir / "encoder.int8.onnx"
        decoder = model_dir / "decoder.int8.onnx"
        joiner = model_dir / "joiner.int8.onnx"
        if not encoder.exists():
            encoder = model_dir / "encoder.onnx"
            decoder = model_dir / "decoder.onnx"
            joiner = model_dir / "joiner.onnx"
        tokens = model_dir / "tokens.txt"

        for f in [encoder, decoder, joiner, tokens]:
            if not f.exists():
                raise FileNotFoundError(f"Missing model file: {f}")

        # NeMo Parakeet TDT models use feature_dim=128 and nemo_transducer type.
        # Models with external weights (encoder.weights) need onnxruntime to
        # resolve the weights relative to the .onnx file. We chdir into the
        # model directory so onnxruntime finds encoder.weights next to encoder.onnx.
        has_external_weights = (model_dir / "encoder.weights").exists()
        orig_cwd = os.getcwd()

        # Hotwords biasing (wh-afhfj). Contract from the wh-q3nrw spike:
        # plain-text hotwords work only with modeling_unit='bpe' AND
        # bpe_vocab=tokens.txt (omitting bpe_vocab access-violates sherpa
        # natively), and greedy_search silently ignores hotwords_file, so
        # enabling hotwords forces modified_beam_search.
        hotwords_kwargs = {}
        if hotwords_file:
            if Path(hotwords_file).exists():
                hotwords_kwargs = {
                    "hotwords_file": hotwords_file,
                    "hotwords_score": hotwords_score,
                    "modeling_unit": "bpe",
                    "bpe_vocab": str(tokens),
                    "decoding_method": "modified_beam_search",
                }
            else:
                logger.warning(
                    f"Hotwords file not found, constructing without hotwords: {hotwords_file}"
                )

        try:
            if has_external_weights:
                os.chdir(str(model_dir))
                logger.info("Changed to model directory for external weights resolution")

            provider = "cuda" if use_gpu else "cpu"
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                tokens=str(tokens),
                encoder=str(encoder),
                decoder=str(decoder),
                joiner=str(joiner),
                provider=provider,
                num_threads=num_threads,
                sample_rate=self._sample_rate,
                feature_dim=128,
                model_type="nemo_transducer",
                **hotwords_kwargs,
            )
            logger.info(
                f"Sherpa-ONNX recognizer loaded: {model_dir.name} "
                f"(provider={provider}, feature_dim=128, external_weights={has_external_weights}, "
                f"hotwords={'on' if hotwords_kwargs else 'off'})"
            )
        finally:
            if has_external_weights:
                os.chdir(orig_cwd)

    def process_audio(self, audio_bytes: bytes) -> None:
        """Process an audio chunk (float32 bytes)."""
        self._has_new_result = False

        samples = np.frombuffer(audio_bytes, dtype=np.float32)
        self._audio_buffer.append(samples)

        self._track_silence(samples)

        n_samples = len(samples)
        self._samples_since_last_inference += n_samples
        self._total_buffer_samples += n_samples

        # Safety cap
        if self._total_buffer_samples >= self._max_buffer_samples:
            if not self._finalized:
                logger.warning(
                    f"Audio buffer exceeded max duration "
                    f"({self._total_buffer_samples / self._sample_rate:.1f}s) - "
                    f"forcing finalization"
                )
                self._run_final_inference()
            return

        # Endpoint detection (trailing silence after speech)
        if self._speech_detected and self._trailing_silence_samples >= self._endpoint_silence_samples:
            if not self._finalized:
                self._run_final_inference()
            return

        # Periodic re-inference
        if (
            self._speech_detected
            and self._samples_since_last_inference >= self._re_inference_interval_samples
        ):
            self._run_inference()

    def is_ready(self) -> bool:
        """Check if results are available."""
        return self._has_new_result or bool(self._confirmed_words)

    def get_result(self) -> str:
        """Get current confirmed transcription text."""
        if not self._confirmed_words:
            return ""
        return " ".join(self._confirmed_words)

    def is_endpoint(self) -> bool:
        """Check if utterance endpoint has been reached."""
        return self._finalized

    def finalize(self) -> None:
        """Force finalization (called by AudioProcessor on VAD endpoint)."""
        if self._finalized:
            return
        if not self._audio_buffer:
            return
        self._run_final_inference()

    def cleanup(self) -> None:
        """Release the recognizer."""
        if self._recognizer is not None:
            del self._recognizer
            self._recognizer = None
            logger.info("Sherpa-ONNX recognizer released")

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

    def _recognize(self, audio: np.ndarray) -> str:
        """Run sherpa-onnx offline recognition on audio buffer."""
        assert self._recognizer is not None
        stream = self._recognizer.create_stream()
        stream.accept_waveform(self._sample_rate, audio)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def _run_inference(self) -> None:
        """Run periodic inference and update stability."""
        audio = np.concatenate(self._audio_buffer)
        self._samples_since_last_inference = 0

        text = self._recognize(audio)
        text = self._normalize_text(text)
        current_words = text.split() if text else []

        self._update_stability(current_words)
        self._has_new_result = True

    def _run_final_inference(self) -> None:
        """Run final inference and promote all words."""
        audio = np.concatenate(self._audio_buffer)

        text = self._recognize(audio)
        text = self._normalize_text(text)
        current_words = text.split() if text else []

        if current_words:
            self._confirmed_words = current_words

        self._finalized = True
        self._has_new_result = True

    def _update_stability(self, current_words: list[str]) -> None:
        """Update confirmed words using LocalAgreement-2."""
        if self._prev_words:
            lcp_len = 0
            for i in range(min(len(self._prev_words), len(current_words))):
                if self._prev_words[i] == current_words[i]:
                    lcp_len = i + 1
                else:
                    break

            if lcp_len > len(self._confirmed_words):
                self._confirmed_words = current_words[:lcp_len]

        self._prev_words = current_words

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize transcription output for WheelHouse.

        - Convert spelled-out letter sequences (V-O-X -> vox)
        - Remove punctuation (except periods between digits and colons in times)
        - Rejoin a split "x ray" / "X, Ray" into "x-ray" (wake word arrives
          as one word -- wh-parakeet-xray-hotword)
        - Dotted-period time form: '8.17 p.m.' -> '8:17 PM'
        - AM/PM uppercase beside an HH:MM time
        - Phone-number hyphenation: '7035551234' -> '703-555-1234'
        - Lowercase first character
        - Always capitalize pronoun 'I'
        """
        if not text:
            return ""

        # Convert spelled-out words (3+ letters).
        text = re.sub(
            r'\b([A-Za-z](?:-[A-Za-z]){2,})\b',
            lambda m: m.group(0).replace('-', '').lower(),
            text,
        )

        # Remove punctuation FIRST so the time rules see bare "am"/"pm"
        # instead of the dotted forms "a.m." / "p.m." that Parakeet can
        # emit. Keeps periods between digits (preserves "8.17" decimal
        # time form) and colons between digits (preserves HH:MM that a
        # time rule already produced).
        text = re.sub(r'(?<!\d)\.|\.(?!\d)|(?<!\d):|:(?!\d)|[,!?;]', '', text)

        # Rejoin a split "x ray" into "x-ray" (wh-parakeet-xray-hotword).
        text = _XRAY_JOIN.sub(lambda m: m.group(1) + '-ray', text)

        # Time-reformat rule: operates on text with dotted AM/PM already
        # collapsed to bare am/pm by the punctuation pass.
        text = _TIME_PERIOD.sub(_time_replace, text)

        # Uppercase am/pm when anchored to an HH:MM time.
        text = _AMPM_UPPERCASE.sub(
            lambda m: m.group(1) + m.group(2).upper(), text
        )

        # Hyphenate 10-digit blocks as phone numbers.
        text = _HYPHENATE_PHONE.sub(r'\1-\2-\3', text)

        # Lowercase first character
        if text:
            text = text[0].lower() + text[1:]

        # Always capitalize 'I' and contractions
        text = re.sub(r'\bi\b', 'I', text)

        return text
