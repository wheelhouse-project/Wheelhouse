"""Google Cloud STT adapter for the benchmark harness.

The adapter reads the production google_stt_server config.toml so the
benchmark exercises the same parameters that WheelHouse uses at runtime:
the same model id, the same enable_automatic_punctuation flag, the same
shared phrase hints (services/stt_providers/shared/hints.txt plus any
inline list in config.toml), the same hints_boost, and the same class
tokens. The benchmark calls the synchronous recognize() endpoint rather
than the streaming endpoint production uses, because the benchmark
processes pre-recorded WAV files one at a time and has no need for
streaming partials. RecognitionConfig accepts the same parameters
either way, so model behavior should match modulo streaming-only side
effects (interim_results, voice activity events, mid-utterance
restarts on is_final).

Reference: docs/design/local_stt_decision_framework.md (Section 4.4)
"""

import logging
import time
import tomllib
from pathlib import Path

import numpy as np
from google.cloud.speech_v1 import SpeechClient
from google.cloud.speech_v1.types import (
    RecognitionAudio,
    RecognitionConfig,
    SpeechContext,
)

from .base import TranscriptionResult

log = logging.getLogger(__name__)

# Locate the production google_stt_server config relative to this file.
# adapters/ -> evaluation/ -> stt_providers/ -> google_stt_server/
_HERE = Path(__file__).resolve().parent
_GSS_DIR = _HERE.parent.parent / "google_stt_server"
_SHARED_DIR = _HERE.parent.parent / "shared"


def _load_production_config() -> dict:
    """Load the production google_stt_server config and shared hints file.

    Mirrors the loader logic in google_stt_server/config_loader.py: hints
    come from services/stt_providers/shared/hints.txt, optionally augmented
    by an inline list in the [adaptation] section of config.toml. Class
    tokens, model, language, hints_boost, and enable_automatic_punctuation
    come from config.toml directly.
    """
    config_path = _GSS_DIR / "config.toml"
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)

    server = cfg.get("server", {})
    adap = cfg.get("adaptation", {})

    hints: set[str] = set()
    hints_path = _SHARED_DIR / "hints.txt"
    if hints_path.exists():
        for line in hints_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                hints.add(line)
    for h in adap.get("hints", []):
        if isinstance(h, str) and h.strip():
            hints.add(h.strip())

    return {
        "model": server.get("model", "latest_short"),
        "language_code": server.get("language_code", "en-US"),
        "enable_automatic_punctuation": bool(
            server.get("enable_automatic_punctuation", False)
        ),
        "phrase_hints": sorted(hints),
        "hints_boost": adap.get("hints_boost"),
        "class_tokens": [
            t for t in adap.get("class_tokens", [])
            if isinstance(t, str) and t.strip()
        ],
    }


class GoogleSTTAdapter:
    """Benchmark adapter for Google Cloud Speech-to-Text (synchronous).

    Reads the production google_stt_server config.toml on construction so
    the benchmark uses the same model, hints, boost, class tokens, and
    auto-punctuation flag that WheelHouse runs with. Pass an explicit
    model name to override.
    """

    def __init__(
        self,
        client: SpeechClient | None = None,
        model: str | None = None,
        name: str | None = None,
    ):
        prod = _load_production_config()
        self._model = model if model is not None else prod["model"]
        self._language_code = prod["language_code"]
        self._enable_auto_punct = prod["enable_automatic_punctuation"]
        self._phrase_hints = prod["phrase_hints"]
        self._hints_boost = prod["hints_boost"] or 0.0
        self._class_tokens = prod["class_tokens"]
        self.name = name or f"google-{self._model}"
        self._client = client or SpeechClient()
        log.info(
            "GoogleSTTAdapter aligned with production config: model=%s "
            "auto_punct=%s phrase_hints=%d hints_boost=%s class_tokens=%s",
            self._model, self._enable_auto_punct, len(self._phrase_hints),
            self._hints_boost, self._class_tokens,
        )

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """Send audio to Google Cloud STT and return the transcription.

        Converts float32 samples to int16 PCM bytes, calls the synchronous
        recognize() endpoint with the production-aligned RecognitionConfig
        (including SpeechContext for phrase hints + class tokens), and
        joins all result transcripts into a single string.
        """
        # Convert float32 [-1.0, 1.0] back to int16 PCM bytes.
        int16_samples = (samples * 32767).astype(np.int16)
        pcm_bytes = int16_samples.tobytes()

        config = RecognitionConfig(
            encoding=RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=sample_rate,
            language_code=self._language_code,
            model=self._model,
            enable_automatic_punctuation=self._enable_auto_punct,
        )

        all_phrases = list(self._phrase_hints) + list(self._class_tokens)
        if all_phrases:
            sc = SpeechContext(phrases=all_phrases, boost=self._hints_boost)
            config.speech_contexts.append(sc)

        audio = RecognitionAudio(content=pcm_bytes)

        t0 = time.perf_counter()
        response = self._client.recognize(config=config, audio=audio)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        # Extract transcript from all result segments.
        segments = []
        for result in response.results:
            if result.alternatives:
                segments.append(result.alternatives[0].transcript)

        text = "".join(segments).strip()

        return TranscriptionResult(
            text=text,
            elapsed_ms=elapsed_ms,
            interim_results=[],  # sync API has no interims
        )

    def reset(self) -> None:
        """No-op: each transcribe() is a standalone API call."""
        pass
