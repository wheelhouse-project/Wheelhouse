"""Parakeet EOU adapter for STT benchmark harness.

Simulates streaming using the ring-buffer architecture that matches
the Rust parakeet-rs implementation:
- 4-second audio ring buffer
- Mel extracted from full buffer, last 25 frames fed to encoder
- Encoder cache persists across utterances (only decoder resets)
- 1 second silence prepended for cache warmup on first utterance
"""
import collections
import logging
import sys
import time
from pathlib import Path

import numpy as np

from .base import TranscriptionResult

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 2560  # 160ms at 16kHz
MIN_BUFFER_SAMPLES = SAMPLE_RATE  # 1 second before processing
BUFFER_SIZE_SAMPLES = SAMPLE_RATE * 4  # 4 second ring buffer
SLICE_LEN = 25  # PRE_ENCODE_CACHE(9) + FRAMES_PER_CHUNK(16)


class ParakeetAdapter:
    """Benchmark adapter for Parakeet EOU 120M."""

    def __init__(self, model_dir: str, name: str | None = None):
        self.name = name or "parakeet-eou-120m"
        model_path = Path(model_dir)

        # Add provider package to path for imports
        provider_dir = Path(__file__).resolve().parent.parent.parent / "parakeet_eou_stt_server"
        if str(provider_dir) not in sys.path:
            sys.path.insert(0, str(provider_dir))

        from mel_features import MelFeatureExtractor
        from onnx_encoder import ParakeetEncoder
        from onnx_decoder import ParakeetDecoder

        self._mel = MelFeatureExtractor()
        self._encoder = ParakeetEncoder(str(model_path / "encoder.onnx"))
        self._decoder = ParakeetDecoder(
            decoder_path=str(model_path / "decoder_joint.onnx"),
            tokenizer_path=str(model_path / "tokenizer.json"),
        )

        # Audio ring buffer cleared each utterance; encoder cache persists
        self._audio_ring: collections.deque = collections.deque(maxlen=BUFFER_SIZE_SAMPLES)

        log.info("Loaded Parakeet EOU model from %s", model_dir)

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """Feed audio and return transcription with timing and interims."""
        interim_results: list[str] = []

        # Hard reset everything for each utterance.
        # In a benchmark, each WAV file is independent -- encoder cache
        # from previous utterances would carry unrelated context that
        # confuses the conformer layers.
        self._decoder.reset()
        self._encoder.reset()
        self._audio_ring.clear()

        t0 = time.perf_counter()

        # Prepend 1s silence for encoder cache warmup.
        # Even with warm encoder cache, the ring buffer needs enough audio
        # before mel extraction can start (MIN_BUFFER_SAMPLES = 1s).
        warmup = np.zeros(SAMPLE_RATE, dtype=np.float32)
        full_audio = np.concatenate([warmup, samples])

        # Append 1s silence to flush delayed tokens through the pipeline
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
        full_audio = np.concatenate([full_audio, silence])

        # Process in 160ms chunks using ring-buffer mel extraction
        offset = 0
        last_text = ""
        while offset + CHUNK_SAMPLES <= len(full_audio):
            chunk = full_audio[offset:offset + CHUNK_SAMPLES]
            offset += CHUNK_SAMPLES

            # Add to ring buffer
            self._audio_ring.extend(chunk)

            # Wait for minimum buffer before processing
            if len(self._audio_ring) < MIN_BUFFER_SAMPLES:
                continue

            # Extract mel from full ring buffer
            buffer_arr = np.array(self._audio_ring, dtype=np.float32)
            full_mel = self._mel.extract(buffer_arr)
            total_frames = full_mel.shape[2]

            # Slice last SLICE_LEN frames for encoder
            start_frame = max(0, total_frames - SLICE_LEN)
            mel_slice = full_mel[:, :, start_frame:]

            # Encode with persistent cache
            enc_result = self._encoder.encode(mel_slice)

            # Decode each encoder output frame
            for t in range(enc_result.encoded_length):
                frame = enc_result.encoded[:, :, t:t + 1]
                self._decoder.decode_frame(frame)

            text = self._decoder.get_text().strip()
            if text and text != last_text:
                interim_results.append(text)
                last_text = text

        elapsed_ms = (time.perf_counter() - t0) * 1000
        final_text = self._decoder.get_text().strip()

        return TranscriptionResult(
            text=final_text,
            elapsed_ms=elapsed_ms,
            interim_results=interim_results,
        )

    def reset(self) -> None:
        """Soft reset for next utterance.

        Only resets decoder state. Encoder cache and audio ring buffer
        persist to maintain continuous context (matches Rust reset_states).
        """
        self._decoder.reset()
