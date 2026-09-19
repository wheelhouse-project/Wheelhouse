"""Bounded counters for inference outside the recognizer's engine_ratio.

Owned by the same consumer thread as the processing it measures. No worker,
lock, per-frame logging, or change to the recognizer's timing contract.
"""
import logging
from dataclasses import dataclass


@dataclass(slots=True)
class PreEngineWork:
    audio_bytes: int = 0
    vad_s: float = 0.0
    agc_s: float = 0.0
    keep_warm_s: float = 0.0
    wake_word_s: float = 0.0

    def add(self, other: 'PreEngineWork') -> None:
        self.audio_bytes += other.audio_bytes
        self.vad_s += other.vad_s
        self.agc_s += other.agc_s
        self.keep_warm_s += other.keep_warm_s
        self.wake_word_s += other.wake_word_s

    def fields(self, sample_rate: int) -> str:
        total_s = self.vad_s + self.agc_s + self.keep_warm_s + self.wake_word_s
        return (
            f'pre_engine_ms_total={total_s * 1000:.1f} '
            f'vad_ms={self.vad_s * 1000:.1f} agc_ms={self.agc_s * 1000:.1f} '
            f'keep_warm_ms={self.keep_warm_s * 1000:.1f} '
            f'wake_word_ms={self.wake_word_s * 1000:.1f} '
            f'pre_engine_audio_ms={self.audio_bytes / 2 / sample_rate * 1000:.0f}'
        )

    def log(self, logger: logging.Logger, scope: str, end: str, sample_rate: int) -> None:
        """An idle window is evidence of work, never a recognizer ratio."""
        if not self.audio_bytes:
            return
        try:
            logger.info('[load-diag] work_scope=%s work_end=%s engine_ratio=n/a %s',
                        scope, end, self.fields(sample_rate))
        except Exception:
            # Diagnostics cannot interrupt capture or a wake-word result.
            pass
