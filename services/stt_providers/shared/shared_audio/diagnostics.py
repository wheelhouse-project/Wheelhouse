"""Microphone diagnostic tools for testing audio capture.

This module provides utilities for testing and validating microphone functionality
before starting the main speech recognition process. It includes tools for
recording test audio, checking audio levels, and optionally saving diagnostic
recordings to disk for troubleshooting purposes.

Key Functions:
  - run_mic_check: Tests microphone capture for a specified duration.

Typical Usage:
  from shared_audio.diagnostics import run_mic_check
  from shared_audio.capture import get_audio_provider, AudioConfig

  config = AudioConfig(rate=16000, chunk_ms=20)
  provider = get_audio_provider(config)

  # Test microphone for 5 seconds
  result = run_mic_check(provider, duration_seconds=5.0, rate=16000, chunk_ms=20)
  if result != 0:
      print("Microphone test failed")
"""
import array
import logging
import math
import time
import wave
from typing import Optional, Protocol, TYPE_CHECKING

logger = logging.getLogger(__name__)


class AudioProviderProtocol(Protocol):
    """Protocol for audio provider interface."""
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def read(self, timeout: float = 1.0) -> Optional[bytes]: ...


def run_mic_check(
    mic: AudioProviderProtocol,
    duration_seconds: float = 5.0,
    rate: int = 16000,
    chunk_ms: int = 20,
    write_wav_path: Optional[str] = None,
    device_index: Optional[int] = None
) -> int:
    """Runs a diagnostic test on the audio provider, printing stats and optionally saving a WAV file.

    Args:
        mic: Audio provider instance (must implement start/stop/read)
        duration_seconds: How long to run the test
        rate: Sample rate in Hz
        chunk_ms: Chunk duration in milliseconds
        write_wav_path: Optional path to write WAV file for debugging
        device_index: Device index for logging (informational only)

    Returns:
        0 on success, 2 if no frames captured
    """
    mic.start()
    expected_samples = int(rate * chunk_ms / 1000)
    expected_bytes = expected_samples * 2  # int16 mono (2 bytes per sample)
    frames = []
    zero_frames = 0
    total_frames = 0
    start = time.time()
    last_log = start
    rms = 0.0

    logger.info(
        f"[mic-check] Starting diagnostics for {duration_seconds:.2f}s | rate={rate} "
        f"chunk_ms={chunk_ms} expected_frame_bytes={expected_bytes} device_index={device_index}"
    )

    while (time.time() - start) < duration_seconds:
        frame = mic.read(timeout=0.5)
        now = time.time()
        if frame is None:
            continue
        total_frames += 1
        if len(frame) != expected_bytes:
            logger.warning(f"[mic-check] Unexpected frame size {len(frame)} (expected {expected_bytes})")
        arr = array.array('h')
        arr.frombytes(frame)
        if not arr:
            zero_frames += 1
            rms = 0.0
        else:
            s = 0
            z = True
            for sample in arr:
                if sample != 0:
                    z = False
                s += sample * sample
            if z:
                zero_frames += 1
            rms = math.sqrt(s / len(arr)) if arr else 0.0
        if (now - last_log) >= 1.0:
            silence_ratio = (zero_frames / total_frames) if total_frames else 0.0
            logger.info(
                f"[mic-check] frames={total_frames} silence_frames={zero_frames} "
                f"silence_ratio={silence_ratio:.2%} last_rms={rms:.1f}"
            )
            last_log = now
        if write_wav_path:
            frames.append(frame)

    duration = time.time() - start
    silence_ratio = (zero_frames / total_frames) if total_frames else 0.0
    logger.info(
        f"[mic-check][DONE] duration={duration:.2f}s frames={total_frames} frame_ms={chunk_ms} "
        f"silence_ratio={silence_ratio:.2%}"
    )

    if write_wav_path and frames:
        try:
            with wave.open(write_wav_path, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                wf.writeframes(b''.join(frames))
            logger.info(f"[mic-check] Wrote WAV: {write_wav_path}")
        except Exception as e:
            logger.warning(f"[mic-check] Failed to write WAV: {e}")

    mic.stop()

    if total_frames == 0:
        logger.error("[mic-check][RESULT] FAIL: No frames captured. Check device index / permissions.")
        return 2
    if silence_ratio > 0.95:
        logger.warning("[mic-check][RESULT] WARN: >95% frames are digital silence (mic muted / wrong source?)")
    else:
        logger.info("[mic-check][RESULT] PASS: Audio frames captured.")
    return 0
