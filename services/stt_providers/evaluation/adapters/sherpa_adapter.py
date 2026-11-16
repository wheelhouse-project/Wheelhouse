"""Sherpa-ONNX adapter for STT benchmark harness.

Wraps sherpa_onnx recognizers directly (not SherpaRunner) to avoid importing
the full provider stack. Supports both streaming (OnlineRecognizer) and
full-utterance (OfflineRecognizer) transducer models.

Recognizer dispatch:
    recognizer_kind = "online"  -> sherpa_onnx.OnlineRecognizer.from_transducer
    recognizer_kind = "offline" -> sherpa_onnx.OfflineRecognizer.from_transducer
    recognizer_kind = None      -> auto-detect from the model directory name
                                   (csukuangfj_sherpa-onnx-streaming-* -> online,
                                    csukuangfj_sherpa-onnx-nemo-* -> offline,
                                    other -> offline as a safe default)

Offline models emit no interim results (they process the full waveform in one
pass), so interim_results is always an empty list on the offline path. This
is the same shape the google_adapter produces, and the harness already
handles it in the interim_stability aggregation (the metric reads None when
interim_count is zero).

ONNX external-weights gotcha: NVIDIA Parakeet TDT 0.6B v2/v3 ship as
encoder.onnx (small graph) + encoder.weights (2.4 GB external-data payload).
onnxruntime resolves the external weights relative to the process cwd rather
than the encoder file's directory. We work around this by temporarily cd'ing
into the model directory before loading, then restoring cwd. After load, the
recognizer holds everything in memory and the cwd switch is released.

Reference: docs/design/benchmark_harness_design.md (Section 2.2)
wh-htc: offline recognizer support for Parakeet TDT.
"""

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np
import sherpa_onnx

from .base import TranscriptionResult

log = logging.getLogger(__name__)

# The shipped Parakeet server's folder. Its modules import each other by
# their plain names, so the folder goes on sys.path (at the end, so that no
# server module hides an evaluation module of the same name).
_SERVER_DIR = Path(__file__).resolve().parents[2] / "sherpa_offline_parakeet_stt_server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.append(str(_SERVER_DIR))


def recognize_with_lead(
    decode_plain: Callable[[np.ndarray], str],
    audio: np.ndarray,
    lead: np.ndarray,
) -> tuple[str, bool]:
    """The lead-word join of the shipped server; returns (raw text, second decode).

    A copy of sherpa_engine.SherpaOfflineEngine._recognize with the join on,
    because that method does not say whether it decoded twice. It uses the
    server's own remove_lead_word and LEAD_WORD. The test
    test_recognize_with_lead_matches_the_server_recognize compares the two.
    """
    import sherpa_engine

    text, found = sherpa_engine.remove_lead_word(
        decode_plain(np.concatenate((lead, audio))), sherpa_engine.LEAD_WORD
    )
    if found:
        return text, False
    return decode_plain(audio), True


class SherpaAdapter:
    """Benchmark adapter for sherpa-onnx transducer models.

    Supports both OnlineRecognizer (streaming transducers) and OfflineRecognizer
    (NeMo Parakeet TDT/CTC, other full-utterance transducers). Call-site can
    pass recognizer_kind explicitly; otherwise we infer from the model
    directory name using the csukuangfj/ naming convention.
    """

    def __init__(
        self,
        model_path: str,
        provider: str = "cpu",
        name: str | None = None,
        recognizer_kind: str | None = None,
        language: str | None = None,
        num_threads: int | None = None,
        shipped_text: bool = False,
        lead_word_wav: str | None = None,
    ):
        model_dir = Path(model_path)
        # None keeps the sherpa-onnx defaults (2 threads online, 1 offline),
        # which every result before this option used.
        if num_threads is not None and num_threads < 1:
            raise ValueError(f"num_threads must be 1 or more, not {num_threads}")
        thread_kwargs = {} if num_threads is None else {"num_threads": num_threads}
        self.recognizer_kind = recognizer_kind or self._detect_recognizer_kind(model_dir)
        # Multilingual streaming models (Nemotron 3.5 ASR) read the language
        # from a per-stream option; without it they detect the language.
        # Only the online path can set it, so refuse it elsewhere instead
        # of silently benchmarking auto-detection.
        if language and self.recognizer_kind != "online":
            raise ValueError(
                f"language={language!r} needs an online (streaming) model; "
                f"{model_dir.name} loads as {self.recognizer_kind}"
            )
        self.language = language
        # shipped_text reproduces the shipped Parakeet server's transcript
        # (sherpa_engine.py _decode_plain + _run_final_inference). Only the
        # offline path has that server, so refuse it elsewhere.
        if shipped_text and self.recognizer_kind != "offline":
            raise ValueError(
                f"shipped_text needs an offline model; "
                f"{model_dir.name} loads as {self.recognizer_kind}"
            )
        self.shipped_text = shipped_text
        # lead_word_wav reproduces the server's lead-word join (lead_word_path
        # set). The join is part of the shipped text path, so it needs it.
        if lead_word_wav and not shipped_text:
            raise ValueError("lead_word_wav needs shipped_text")
        self.lead_audio: np.ndarray | None = None
        if lead_word_wav:
            import sherpa_engine

            # The server's loader: 16 kHz mono 16-bit only, used as it is.
            self.lead_audio = sherpa_engine.load_lead_word_audio(lead_word_wav, 16000)
        # True or False after each transcribe() with the lead word; None without it.
        self.last_second_decode: bool | None = None
        default_name = f"{model_dir.name}_{language}" if language else model_dir.name
        if num_threads is not None:
            default_name = f"{default_name}_t{num_threads}"
        self.name = name or default_name
        # Typed as Any because the branch below binds either OnlineRecognizer
        # or OfflineRecognizer; pyright otherwise narrows to the first branch's
        # return type and flags cross-branch attribute access.
        self.recognizer: Any

        # Auto-detect model files.
        tokens = str(model_dir / "tokens.txt")
        encoder = self._find_model_file(model_dir, "encoder")
        decoder = self._find_model_file(model_dir, "decoder")
        joiner = self._find_model_file(model_dir, "joiner")

        log.info("Loading sherpa-onnx model: %s (%s)", self.name, self.recognizer_kind)
        log.info("  encoder: %s", Path(encoder).name)
        log.info("  decoder: %s", Path(decoder).name)
        log.info("  joiner:  %s", Path(joiner).name)
        log.info("  provider: %s", provider)

        if self.recognizer_kind == "offline":
            self.recognizer = self._load_offline_recognizer(
                model_dir=model_dir,
                encoder=encoder,
                decoder=decoder,
                joiner=joiner,
                tokens=tokens,
                provider=provider,
                thread_kwargs=thread_kwargs,
            )
        else:
            # Streaming online recognizer. Decoding method depends on the
            # model family: streaming transducers support modified_beam_search
            # (better accuracy), NeMo streaming transducers support only
            # greedy_search (sherpa-onnx's NeMo online impl explicitly rejects
            # modified_beam_search at load time).
            decoding_method = (
                "greedy_search" if "nemo" in model_dir.name.lower()
                else "modified_beam_search"
            )
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=tokens,
                encoder=encoder,
                decoder=decoder,
                joiner=joiner,
                decoding_method=decoding_method,
                sample_rate=16000,
                provider=provider,
                enable_endpoint_detection=True,
                rule1_min_trailing_silence=2.4,
                rule2_min_trailing_silence=0.5,
                rule3_min_utterance_length=30.0,
                **thread_kwargs,
            )

    @staticmethod
    def _detect_recognizer_kind(model_dir: Path) -> str:
        """Heuristic: detect online vs offline from directory/file naming.

        Sherpa-onnx models ship under directories whose names encode the
        recognizer kind:
        - csukuangfj_sherpa-onnx-streaming-*   -> online (streaming)
        - csukuangfj_sherpa-onnx-nemo-*        -> offline (NeMo .nemo exports)
        - other                                -> offline (safer default;
                                                    most non-streaming exports
                                                    in the sherpa-onnx catalog
                                                    are full-utterance)

        Call sites that know the kind should pass it explicitly to avoid
        guessing. The Stage B benchmark runner reads the kind from the
        triage's sherpa_recognizer_kind field and forwards it.
        """
        name = model_dir.name.lower()
        if "sherpa-onnx-streaming" in name or "streaming" in name:
            return "online"
        if "sherpa-onnx-nemo" in name or "nemo" in name:
            return "offline"
        return "offline"

    @staticmethod
    def _load_offline_recognizer(
        *,
        model_dir: Path,
        encoder: str,
        decoder: str,
        joiner: str,
        tokens: str,
        provider: str,
        thread_kwargs: dict[str, int],
    ) -> "sherpa_onnx.OfflineRecognizer":
        """Construct an OfflineRecognizer with NeMo-aware defaults.

        NeMo Parakeet TDT exports use feature_dim=128 (standard NeMo mel
        dimension) and model_type='nemo_transducer'. Non-NeMo offline
        transducers use feature_dim=80 and model_type='transducer'.

        External weights: cd into the model directory before load so that
        onnxruntime resolves encoder.weights relative to encoder.onnx.
        """
        name = model_dir.name.lower()
        if "nemo" in name or "parakeet" in name:
            feature_dim = 128
            model_type = "nemo_transducer"
        else:
            feature_dim = 80
            model_type = "transducer"

        orig_cwd = os.getcwd()
        try:
            os.chdir(str(model_dir))
            # Use just the basenames so onnxruntime's external-data resolver
            # finds encoder.weights next to encoder.onnx in the new cwd.
            return sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=Path(encoder).name,
                decoder=Path(decoder).name,
                joiner=Path(joiner).name,
                tokens=Path(tokens).name,
                feature_dim=feature_dim,
                model_type=model_type,
                decoding_method="greedy_search",
                provider=provider,
                **thread_kwargs,
            )
        finally:
            os.chdir(orig_cwd)

    @staticmethod
    def _find_model_file(model_dir: Path, prefix: str) -> str:
        """Find the ONNX model file for a given component (encoder/decoder/joiner).

        Prefers int8 quantized models if available, falls back to fp32.
        """
        int8_files = list(model_dir.glob(f"{prefix}*int8*.onnx"))
        if int8_files:
            return str(int8_files[0])

        all_files = list(model_dir.glob(f"{prefix}*.onnx"))
        if all_files:
            return str(all_files[0])

        raise FileNotFoundError(
            f"No ONNX file found for '{prefix}' in {model_dir}"
        )

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """Feed audio samples and return transcription.

        Dispatches to the online or offline transcription path based on
        self.recognizer_kind.
        """
        if self.recognizer_kind == "offline":
            return self._transcribe_offline(samples, sample_rate)
        return self._transcribe_online(samples, sample_rate)

    def _transcribe_online(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """Streaming transducer path with interim-result harvesting.

        Uses OnlineRecognizer API: is_ready/decode_stream/get_result loop
        to harvest intermediate text as chunks arrive. Pyright can't narrow
        self.recognizer to OnlineRecognizer here because the instance
        attribute is bound in two branches; ignoring the type check is safe.
        """
        recognizer: Any = self.recognizer
        stream = recognizer.create_stream()
        if self.language:
            stream.set_option("language", self.language)
        interim_results: list[str] = []

        t0 = time.perf_counter()

        stream.accept_waveform(sample_rate, samples)

        # Append silence to trigger endpoint detection and flush buffered tokens.
        silence = np.zeros(int(sample_rate * 1.0), dtype=np.float32)
        stream.accept_waveform(sample_rate, silence)

        stream.input_finished()

        last_text = ""
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
            partial = recognizer.get_result(stream)
            partial_text = partial.text if hasattr(partial, "text") else str(partial)
            partial_text = partial_text.strip()
            if partial_text and partial_text != last_text:
                interim_results.append(partial_text)
                last_text = partial_text

        elapsed_ms = (time.perf_counter() - t0) * 1000

        final = recognizer.get_result(stream)
        final_text = final.text if hasattr(final, "text") else str(final)

        return TranscriptionResult(
            text=final_text.strip(),
            elapsed_ms=elapsed_ms,
            interim_results=interim_results,
        )

    def _transcribe_offline(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """Full-utterance transducer path. No interim results.

        Feeds the entire waveform at once, decodes in a single call, reads
        the final text from stream.result.text. OfflineRecognizer does not
        expose intermediate decoder states, so interim_results is empty.
        """
        if self.lead_audio is not None:
            return self._transcribe_with_lead(samples, sample_rate)
        t0 = time.perf_counter()
        stream = self.recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self.recognizer.decode_stream(stream)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        result = stream.result
        if self.shipped_text:
            # Same text as the shipped server: tokens, not result.text, then
            # normalize_transcript first and apply_itn second.
            shipped = "".join(result.tokens).strip()
            return self._shipped_result(shipped, elapsed_ms)
        text = result.text if hasattr(result, "text") else str(result)

        return TranscriptionResult(
            text=text.strip(),
            elapsed_ms=elapsed_ms,
            interim_results=[],
        )

    def _decode_tokens(self, audio: np.ndarray) -> str:
        """sherpa_engine._decode_plain: decode `audio` and return the raw token text."""
        stream = self.recognizer.create_stream()
        stream.accept_waveform(16000, audio)
        self.recognizer.decode_stream(stream)
        return "".join(stream.result.tokens).strip()

    def _transcribe_with_lead(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """The server path with the lead-word join on; elapsed_ms covers both decodes."""
        if sample_rate != 16000:
            raise ValueError(f"the lead-word join needs 16000 Hz audio, not {sample_rate}")
        assert self.lead_audio is not None
        t0 = time.perf_counter()
        raw, second = recognize_with_lead(self._decode_tokens, samples, self.lead_audio)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.last_second_decode = second
        return self._shipped_result(raw, elapsed_ms)

    @staticmethod
    def _shipped_result(raw: str, elapsed_ms: float) -> TranscriptionResult:
        from shared_stt.transcript_rules import apply_itn, normalize_transcript

        text = apply_itn(normalize_transcript(raw))
        # The server keeps the final text as a word list and joins it with
        # one space (sherpa_engine.py _run_final_inference, get_result).
        return TranscriptionResult(
            text=" ".join(text.split()),
            elapsed_ms=elapsed_ms,
            interim_results=[],
        )

    def reset(self) -> None:
        """No-op: each transcribe() creates a fresh stream."""
        pass
