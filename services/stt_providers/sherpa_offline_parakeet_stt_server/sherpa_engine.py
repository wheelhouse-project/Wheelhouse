"""Chunked re-inference streaming engine for Sherpa-ONNX offline models.

Implements RecognitionEngine protocol for use with AudioProcessor.
Accumulates audio, runs periodic inference via sherpa-onnx OfflineRecognizer,
and uses LocalAgreement-2 stability detection for confirmed words.

Designed for NeMo Parakeet TDT models via sherpa-onnx.
"""
from __future__ import annotations

import logging
import math
import os
import re
import struct
import sys
from dataclasses import dataclass
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

from shared_stt.transcript_rules import apply_itn, normalize_transcript

logger = logging.getLogger(__name__)

# The sentencepiece vocabulary sherpa needs to turn a written hotword into
# the model's own tokens. It sits beside the ONNX files in the model
# directory and is NOT tokens.txt (wh-parakeet-hotword-vocab).
BPE_VOCAB_FILENAME = "bpe.vocab"


@dataclass(frozen=True)
class HotwordsStatus:
    """What actually happened to hint boosting at load time.

    'requested' means the caller asked for hotwords. 'active' means the
    recognizer was built with them. The two differ whenever the
    vocabulary or the hotwords file could not be used, and the caller
    needs that difference to avoid telling the user that boosting is on
    when it is off."""

    requested: bool = False
    active: bool = False
    detail: str = ""


# What a C++ stream extraction skips over: the whitespace of the C
# locale, and nothing else. Python's own str.split also separates on
# U+00A0, U+2028 and the rest of the Unicode whitespace set, which the
# loader reads as ordinary bytes inside a token
# (wh-parakeet-hotword-vocab.2.3).
_LOADER_WHITESPACE = " \t\n\v\f\r"


def _loader_items(line: str) -> list[str]:
    """The items `stream >> item` would read out of one vocabulary line."""
    items: list[str] = []
    current: list[str] = []
    for character in line:
        if character in _LOADER_WHITESPACE:
            if current:
                items.append("".join(current))
                current = []
        else:
            current.append(character)
    if current:
        items.append("".join(current))
    return items


def _loader_text(path: Path) -> str:
    """The text sherpa's loaders read out of this file.

    Both loaders open the file as a text stream (std::ifstream with no
    ios::binary), and on Windows the C runtime ends a text-mode file at
    the first 0x1A byte, so nothing after it reaches std::getline. Read
    whole, a marker a text tool appended after the last row was a token
    with no score, and a vocabulary the loader accepts lost boosting;
    a marker inside a piece cut the loader's copy of that row to a token
    with no score, which ended the provider, while the row read whole
    here had both (wh-parakeet-hotword-vocab.2.8, measured against the
    real model). The cut comes before decoding, because bytes after the
    marker never reach the loader whether or not they are UTF-8.

    Text mode also turns \\r\\n into \\n, which changes nothing here:
    _loader_items drops a \\r at the end of a line as whitespace, which
    is what both loaders do with one that reaches them. The bytes are
    decoded here rather than read with read_text, because read_text
    turns a lone \\r into a \\n and that changes the verdict: the row
    'piece\\r\\t-2.5' is one good line to the loader, which treats the
    \\r as whitespace inside the line, and arrived as two broken ones
    (wh-parakeet-hotword-vocab.2.4, measured against the real model)."""
    data = path.read_bytes()
    if os.name == "nt":
        data = data.split(b"\x1a", 1)[0]
    return data.decode("utf-8")


def _loader_lines(text: str) -> list[str]:
    """The lines std::getline reads out of a file's text.

    A line ends at a newline and at nothing else, and the empty remainder
    after a final newline is not a line. str.splitlines also ends one at
    \\v, \\f, \\x1c-\\x1e, U+0085, U+2028 and U+2029, which the loader
    reads as ordinary bytes inside a token (wh-parakeet-hotword-vocab.2.3
    for the vocabulary, .2.6 for tokens.txt). A \\r that ends a line in a
    CRLF file survives into the line here and is dropped by _loader_items,
    which is what the loader does with it."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def read_token_pieces(path: Path) -> list[str]:
    """The token column of tokens.txt, read the way sherpa reads it.

    sherpa loads tokens.txt through SymbolTable::ReadTokens: std::getline
    ends a line at a newline, the line is trimmed of C whitespace, and
    `stream >> item` reads the token and then the id, on C whitespace.
    A line holding one item is the space symbol, whose id is that item.
    str.splitlines and str.rsplit(None, 1) read Unicode instead: they
    ended a line at U+2028 and separated a token from its id at U+00A0,
    so a token the loader keeps whole came out of here in fragments,
    a vocabulary made of such tokens was refused as built for another
    model, and a hint holding such a character was dropped as outside
    the model (wh-parakeet-hotword-vocab.2.6, measured against the real
    model: the loader builds the recognizer from a tokens.txt whose
    token holds U+2028 or U+0085, or ends in U+00A0).

    The loader ends the provider on a line with more than two items,
    and the NeMo transducer's own line count ends it on a blank one, so
    no file with either can start the provider whatever this reads out
    of it; both are read as the loader reads them before it stops. The
    file is read only as far as the loader reads it (_loader_text), so
    a line after a text-mode end of file is not a line here either."""
    pieces: list[str] = []
    for line in _loader_lines(_loader_text(path)):
        items = _loader_items(line)
        pieces.append(items[0] if len(items) >= 2 else " ")
    return pieces


# The number sherpa's vocabulary loader collects for a score: a sign,
# digits with at most one point, an exponent with digits. ASCII digits
# only: \d would also match the Unicode digits float() accepts.
_SCORE_SYNTAX = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _loader_score(value_text: str) -> float | None:
    """The score sherpa's vocabulary loader reads from this item, or None
    when that loader cannot read one and ends the provider.

    The loader reads the score with `stream >> float`: it collects an
    ASCII number, converts it with strtof, and refuses the item when
    nothing was collected or the conversion overflowed or underflowed
    to zero. float() reads more than that: nan and inf in any spelling,
    Unicode digits, Unicode whitespace, a digit-group underscore, and
    any magnitude a double holds. Each of those passed here and ended
    the provider (wh-parakeet-hotword-vocab.2.7, measured against the
    real model: nan, inf, -1e40, -3.4028236e38, -1e999, -1e-50, a
    Unicode digit, a leading U+00A0 and an exponent holding a Unicode
    digit all end it; -3.4028235e38, which rounds to the largest
    float32, -1e-40, a float32 denormal, and the smallest normal
    float32 all load).

    The range is the float32 range after rounding, taken from the
    packed value rather than from constants, so that the boundary is
    the one the C library's own rounding produces. The rounding here
    goes through a double first, which can differ from strtof's direct
    rounding only for a number written to about seventeen significant
    digits that lands within half a double step of a float32 boundary;
    sentencepiece writes far fewer digits than that.

    The digit-group underscore is refused although the loader reads
    -1 out of -1_000 and runs on: that is the safe direction the
    docstring of bpe_vocab_rejection_reason records for '-2.5x'."""
    if _SCORE_SYNTAX.fullmatch(value_text) is None:
        return None
    value = float(value_text)
    try:
        as_float32 = struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return None
    if math.isinf(as_float32):
        return None
    mantissa = re.split("[eE]", value_text, maxsplit=1)[0]
    if as_float32 == 0.0 and mantissa.strip("+-.0"):
        return None
    return value


def hotwords_file_rejection_reason(hotwords_path: Path) -> str | None:
    """Why this hotwords file cannot be used, or None when it can be.

    sherpa reads the file itself, so this only asks whether it is there.
    The question is asked inside a try because Path.exists calls stat and
    pathlib re-raises a stat failure that is not a missing-path error: a
    file the account cannot stat raised out of startup and killed the
    provider, where an unusable hotwords file should only turn boosting
    off with a reason (wh-parakeet-hotword-vocab.2.5)."""
    try:
        if not hotwords_path.exists():
            return f"the hotwords file is missing: {hotwords_path}"
    except OSError as e:
        return f"the hotwords file could not be read: {e}"
    return None


def bpe_vocab_rejection_reason(vocab_path: Path, tokens_path: Path) -> str | None:
    """Why this vocabulary cannot be used, or None when it can be.

    sherpa's own contract for the bpe_vocab argument, from the installed
    package: "The vocabulary generated by google's sentencepiece program.
    It is a file has two columns, one is the token, the other is the log
    probability." tokens.txt has two columns as well, but its second
    column is an integer id, so it passes a shape check and still
    produces wrong tokenization. The score test below is what separates
    them, and it rests on one property: a usable vocabulary carries at
    least one value that is written negative or is not whole, and a
    column of ids carries none.

    The shape test is stricter than that contract, because the native
    loader behind the argument is stricter: it reads every physical
    line and ends the process on one it cannot read as a token and a
    score. It reads a line and its items the way that loader does,
    with C whitespace over bytes rather than Python's Unicode split,
    so that it refuses what the loader refuses and accepts what the
    loader accepts (wh-parakeet-hotword-vocab.2.3).

    The score test reads each item the way that loader reads a float
    (_loader_score): an ASCII number within float32 range. float()
    alone accepted nan, inf, Unicode digits and any magnitude, and the
    loader ended the provider on every one of them
    (wh-parakeet-hotword-vocab.2.7).

    One difference is left in place, in the safe direction. A C++
    stream reads a score as far as it parses, so it takes -2.5 out of
    '-2.5x' and -1 out of '-1_000'; _loader_score refuses both whole
    items. A file like that loses boosting and keeps a reason, where
    the loader would run on with a score it read out of half an item.
    sentencepiece writes plain floats, so no file it generates reaches
    the difference."""
    try:
        # One read answers both questions, and asking Path.exists first
        # could not: it calls stat, and pathlib re-raises a stat failure
        # that is not a missing-path error, so a vocabulary the account
        # cannot read ended startup instead of turning boosting off
        # (wh-parakeet-hotword-vocab.2.5, measured on Python 3.12.10).
        text = _loader_text(vocab_path)
    except FileNotFoundError:
        return f"the vocabulary file is missing: {vocab_path}"
    except (OSError, UnicodeDecodeError) as e:
        return f"the vocabulary file could not be read: {e}"

    # Split into lines the way std::getline does; _loader_lines says why
    # str.splitlines cannot (wh-parakeet-hotword-vocab.2.3).
    raw_lines = _loader_lines(text)
    if not any(line.strip() for line in raw_lines):
        return f"the vocabulary file is empty: {vocab_path}"

    pieces: list[str] = []
    every_value_is_an_id = True
    for index, line in enumerate(raw_lines):
        # sherpa hands this file to simple-sentencepiece, which reads
        # every physical line and then requires one token and one score
        # out of it, separated by whitespace. On a line it cannot read
        # that way it ends the process; it does not raise, so no except
        # around the recognizer can turn that into degraded boosting
        # (wh-parakeet-hotword-vocab.2.2, measured against the real
        # model). Blank lines therefore count, and so does a piece with
        # a space or a tab inside it: splitting on the last tab used to
        # keep such a piece whole and read the score after it, which
        # passed every check here and still ended the provider.
        #
        # The loader reads two items and ignores whatever follows them,
        # so a third column is not a reason to refuse a file: doing so
        # turned boosting off for a vocabulary the loader accepts
        # (wh-parakeet-hotword-vocab.2.3, measured against the real
        # model).
        items = _loader_items(line)
        if not items:
            return (
                f"line {index + 1} is blank, and sherpa's vocabulary "
                f"loader ends the provider on a line it cannot read as "
                f"a token and a score"
            )
        if len(items) < 2:
            return (
                f"line {index + 1} carries no score after its token, "
                f"because nothing on it is separated by the whitespace "
                f"sherpa's vocabulary loader reads, so that loader "
                f"would end the provider on it"
            )
        piece, value_text = items[0], items[1]
        value = _loader_score(value_text)
        if value is None:
            return (
                f"line {index + 1} has '{value_text}' where a sentencepiece "
                f"score belongs, and sherpa's vocabulary loader would end "
                f"the provider on it"
            )
        pieces.append(piece)
        # A token id is a whole number of zero or more, and an id column
        # never carries a minus sign. A score column carries at least one
        # value that is not one of those -- negative, fractional, or
        # both. Reading the column this way, rather than comparing each
        # value with its line number, rejects tokens.txt whatever base
        # its ids start from and whatever order the lines are in
        # (wh-parakeet-hotword-vocab.1.1).
        #
        # The sign has to come from the text. float("-0") is -0.0, which
        # is whole and is not less than zero, so a score written -0 looks
        # exactly like the id 0 to the number alone
        # (wh-parakeet-hotword-vocab.2.1). Reading the sign from the text
        # also makes a numeric `value >= 0` test redundant, because the
        # only way float() returns a negative number is a written minus.
        written_negative = value_text.strip().startswith("-")
        if every_value_is_an_id and (
            written_negative or not value.is_integer()
        ):
            every_value_is_an_id = False

    if every_value_is_an_id:
        return (
            "every value in the second column is a whole number of zero or "
            "more, so this file holds token ids rather than sentencepiece "
            "scores; tokens.txt is not a vocabulary"
        )

    try:
        model_pieces = set(read_token_pieces(tokens_path))
    except (OSError, UnicodeDecodeError) as e:
        return f"the model's tokens.txt could not be read: {e}"
    if model_pieces:
        shared = sum(1 for piece in pieces if piece in model_pieces)
        # crewcut: half is a deliberately loose floor, chosen without a
        # real bpe.vocab to measure against. It separates a vocabulary
        # built for another language or another tokenizer from this
        # model's own, and it cannot prove the scores came from this
        # model's tokenizer. Once the real file exists, compare it with
        # tokens.txt and tighten this to the relationship the two
        # actually have.
        if shared * 2 < len(pieces):
            return (
                f"only {shared} of its {len(pieces)} tokens belong to this "
                f"model, so the vocabulary was built for another model"
            )
    return None


class SherpaOfflineEngine:
    """Chunked re-inference engine using sherpa-onnx OfflineRecognizer.

    Implements RecognitionEngine protocol. Accumulates audio in a buffer,
    runs periodic inference, and uses LocalAgreement-2 stability detection
    to determine which words are confirmed.

    NeMo Parakeet TDT models use feature_dim=128 and model_type='nemo_transducer'.
    Models with external weights (encoder.weights) require loading from the
    model directory.
    """

    #: Set by _load_model on every path, so a caller can always read it.
    hotwords_status: HotwordsStatus

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
        # plain-text hotwords work only with modeling_unit='bpe' AND a
        # bpe_vocab (omitting bpe_vocab access-violates sherpa natively),
        # and greedy_search silently ignores hotwords_file, so enabling
        # hotwords forces modified_beam_search.
        #
        # wh-parakeet-hotword-vocab: that spike passed tokens.txt as the
        # bpe_vocab. tokens.txt is the model's id table, not a
        # sentencepiece vocabulary, so every hotword was tokenized
        # wrongly. The real file is bpe.vocab beside the ONNX files, and
        # it is checked before use: an unusable vocabulary drops boosting
        # instead of biasing on nonsense, and the caller can read
        # hotwords_status to tell the user the truth.
        bpe_vocab = model_dir / BPE_VOCAB_FILENAME
        hotwords_kwargs = {}
        if not hotwords_file:
            self.hotwords_status = HotwordsStatus()
        else:
            reason = hotwords_file_rejection_reason(Path(hotwords_file))
            if reason is None:
                reason = bpe_vocab_rejection_reason(bpe_vocab, tokens)
            if reason is None:
                hotwords_kwargs = {
                    "hotwords_file": hotwords_file,
                    "hotwords_score": hotwords_score,
                    "modeling_unit": "bpe",
                    "bpe_vocab": str(bpe_vocab),
                    "decoding_method": "modified_beam_search",
                }
                self.hotwords_status = HotwordsStatus(
                    requested=True, active=True
                )
                logger.info(f"Hotwords initialized from {bpe_vocab}")
            else:
                self.hotwords_status = HotwordsStatus(
                    requested=True, active=False, detail=reason
                )
                logger.warning(
                    "Hotwords requested but not initialized; continuing "
                    f"without boosting: {reason}"
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
            status = self.hotwords_status
            if status.active:
                hotwords_state = "on"
            elif status.requested:
                hotwords_state = f"requested but off ({status.detail})"
            else:
                hotwords_state = "off"
            logger.info(
                f"Sherpa-ONNX recognizer loaded: {model_dir.name} "
                f"(provider={provider}, feature_dim=128, external_weights={has_external_weights}, "
                f"hotwords={hotwords_state})"
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
        # normalize_transcript first, apply_itn second: normalization
        # strips punctuation and collapses "p.m." to "pm", which is the
        # shape the ITN clock rule reads. Parakeet already emits digits,
        # so apply_itn also has to leave existing numerals alone
        # (wh-shared-itn-parakeet).
        text = apply_itn(normalize_transcript(text))
        current_words = text.split() if text else []

        self._update_stability(current_words)
        self._has_new_result = True

    def _run_final_inference(self) -> None:
        """Run final inference and promote all words."""
        audio = np.concatenate(self._audio_buffer)

        text = self._recognize(audio)
        # Same stage order as _run_inference above.
        text = apply_itn(normalize_transcript(text))
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
