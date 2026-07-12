"""Voxtral adapter for the benchmark harness.

Connects to a running vLLM instance serving Voxtral Mini via the HTTP
transcription API (/v1/audio/transcriptions). Sends pre-recorded audio
as a WAV file and receives the completed transcription.

Requires vLLM to be running separately:
    cd <your-vllm-dir> && .venv/Scripts/python -m vllm.entrypoints.openai.api_server \
        --model models/voxtral-mini-3b --tokenizer_mode mistral \
        --config_format mistral --load_format mistral ...

Reference: docs/design/local_stt_decision_framework.md (Section 4.4)
"""

import io
import logging
import time
import wave

import numpy as np
import requests

from .base import TranscriptionResult

log = logging.getLogger(__name__)


class VoxtralAdapter:
    """Benchmark adapter for Voxtral via vLLM HTTP transcription API."""

    def __init__(
        self,
        vllm_url: str = "http://localhost:8000",
        model_name: str = "models/voxtral-mini-3b",
        name: str | None = None,
    ):
        self.name = name or model_name.split("/")[-1]
        self._api_url = f"{vllm_url}/v1/audio/transcriptions"
        self._model_name = model_name
        self._session = requests.Session()

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """Send audio to vLLM and collect transcription."""
        # Convert float32 -> int16 PCM WAV in memory
        int16 = (samples * 32767).clip(-32768, 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(int16.tobytes())
        buf.seek(0)

        t0 = time.perf_counter()
        resp = self._session.post(
            self._api_url,
            files={"file": ("audio.wav", buf, "audio/wav")},
            data={
                "model": self._model_name,
                "language": "en",
                "temperature": "0.0",
            },
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        if resp.status_code != 200:
            log.warning("vLLM error %d: %s", resp.status_code, resp.text[:200])
            return TranscriptionResult(text="", elapsed_ms=elapsed_ms, interim_results=[])

        text = resp.json().get("text", "").strip()
        return TranscriptionResult(text=text, elapsed_ms=elapsed_ms, interim_results=[])

    def reset(self) -> None:
        """No persistent state to reset."""
        pass
