"""Smart Automatic Gain Control with Noise Floor Tracking.

Multi-timescale AGC that normalizes speech while adapting to environmental
noise changes (furnace, fan, etc.) and uses STT outcomes as ground truth.

Key concept: Track two separate levels:
- Speech level: normalize to target RMS (fast, per-chunk)
- Noise floor: measure during silence, adapt in seconds

The safety constraint ensures amplified noise stays below VAD threshold:
    max_safe_gain = vad_threshold / noise_floor

Three Timescales:
- Fast (30ms): Normalize speech to target RMS
- Medium (seconds): Track noise floor, adjust max_safe_gain ceiling
- Slow (per utterance): Use STT outcomes as validation feedback
"""

import logging
import math
import struct
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AGCConfig:
    """Configuration for Smart AGC."""
    enabled: bool = True
    target_speech_rms: float = 0.1      # Target RMS level for normalized speech
    vad_threshold_rms: float = 0.08     # Approximate VAD trigger level
    noise_floor_alpha: float = 0.02     # Smoothing factor (lower = slower decay)
    min_gain: float = 0.1               # Minimum gain multiplier
    max_gain: float = 10.0              # Maximum gain multiplier
    initial_noise_floor: float = 0.01   # Starting noise floor estimate


class SmartAGC:
    """Multi-timescale AGC with noise floor tracking.

    Attributes:
        config: AGC configuration parameters
        noise_floor: Current noise floor estimate (updated during silence)
        speech_gain: Current speech normalization gain
        consecutive_failures: Count of consecutive STT failures for feedback
    """

    def __init__(self, config: Optional[AGCConfig] = None):
        """Initialize Smart AGC.

        Args:
            config: AGC configuration. Uses defaults if not provided.
        """
        self.config = config or AGCConfig()
        self.noise_floor = self.config.initial_noise_floor
        self.speech_gain = 1.0
        self.consecutive_failures = 0
        self.failure_gain_cap = self.config.max_gain  # Ratchet: reduced after failures
        self._last_speech_rms = 0.0

    def process(self, pcm_bytes: bytes, is_speech: bool) -> bytes:
        """Process audio chunk and return gain-adjusted audio.

        Fast loop (per chunk):
        - If speech: adjust gain toward target RMS
        - If silence: update noise floor estimate

        Args:
            pcm_bytes: Raw PCM audio bytes (16-bit signed, mono)
            is_speech: Whether VAD detected speech in this chunk

        Returns:
            Gain-adjusted PCM audio bytes
        """
        if not self.config.enabled:
            return pcm_bytes

        rms = self._calculate_rms(pcm_bytes)

        if is_speech and rms > 0.001:
            # Fast loop: adjust speech gain toward target
            self._adjust_speech_gain(rms)
            self._last_speech_rms = rms
        else:
            # Medium loop: update noise floor estimate during silence
            self._update_noise_floor(rms)

        # Calculate safe gain ceiling based on noise floor
        max_safe_gain = self._calculate_max_safe_gain()

        # Apply effective gain (speech gain clamped to safe maximum AND failure cap)
        effective_gain = max(
            self.config.min_gain,
            min(self.speech_gain, max_safe_gain, self.failure_gain_cap, self.config.max_gain)
        )

        return self._apply_gain(pcm_bytes, effective_gain)

    def on_stt_outcome(self, result_type: str, word_count: int) -> None:
        """Slow loop: feedback from STT outcomes.

        Uses actual transcription results as ground truth to validate
        and adjust noise floor estimate.

        Args:
            result_type: The STT result type (GOOGLE_FINAL, VAD_SILENCE_ABORT, etc.)
            word_count: Number of words in transcription (0 if no text)
        """
        if result_type == "GOOGLE_FINAL" and word_count > 0:
            # Success - reset failure counter and release gain cap
            if self.consecutive_failures > 0:
                logger.info(f"[agc] Success after {self.consecutive_failures} failures, releasing gain cap")
            self.consecutive_failures = 0
            self.failure_gain_cap = self.config.max_gain  # Release the ratchet
        elif result_type in ("VAD_SILENCE_ABORT", "NO_TEXT_TIMEOUT"):
            # Failure - noise floor might be underestimated
            self.consecutive_failures += 1

            # RATCHET: Progressive gain cap based on consecutive failures
            # This directly stops false positives by limiting amplification
            if self.consecutive_failures >= 10:
                self.failure_gain_cap = 2.0
            elif self.consecutive_failures >= 6:
                self.failure_gain_cap = 3.0
            elif self.consecutive_failures >= 3:
                self.failure_gain_cap = 5.0

            # Also bump noise floor estimate
            if self.consecutive_failures >= 3:
                adjustment = 1.3
                logger.info(f"[agc] Multiple failures ({self.consecutive_failures}), "
                     f"gain cap -> {self.failure_gain_cap:.1f}x, "
                     f"bumping noise floor from {self.noise_floor:.4f}")
            else:
                adjustment = 1.1

            self.noise_floor = min(self.noise_floor * adjustment, 0.5)
            logger.info(f"[agc] STT failure ({result_type}), noise floor -> {self.noise_floor:.4f}")

    def _calculate_rms(self, pcm_bytes: bytes) -> float:
        """Calculate RMS (root mean square) of PCM audio.

        Args:
            pcm_bytes: Raw PCM audio bytes (16-bit signed, mono)

        Returns:
            RMS value normalized to 0.0-1.0 range
        """
        if len(pcm_bytes) < 2:
            return 0.0

        # Unpack 16-bit signed samples
        n_samples = len(pcm_bytes) // 2
        samples = struct.unpack(f'<{n_samples}h', pcm_bytes)

        # Calculate RMS, normalize to 0.0-1.0
        sum_squares = sum(s * s for s in samples)
        rms = math.sqrt(sum_squares / n_samples) / 32768.0

        return rms

    def _adjust_speech_gain(self, current_rms: float) -> None:
        """Adjust speech gain toward target RMS.

        Uses smooth exponential approach to avoid sudden changes.

        Args:
            current_rms: RMS of current speech chunk
        """
        if current_rms < 0.001:
            return

        # Calculate desired gain to hit target
        desired_gain = self.config.target_speech_rms / current_rms

        # Smooth adjustment (attack/release behavior)
        # Faster attack (reduce gain quickly), slower release (increase slowly)
        if desired_gain < self.speech_gain:
            # Attack: reduce gain quickly to prevent clipping
            alpha = 0.3
        else:
            # Release: increase gain slowly to avoid amplifying transients
            alpha = 0.05

        self.speech_gain = self.speech_gain + alpha * (desired_gain - self.speech_gain)

        # Clamp to configured limits
        self.speech_gain = max(self.config.min_gain,
                               min(self.speech_gain, self.config.max_gain))

    def _update_noise_floor(self, silence_rms: float) -> None:
        """Update noise floor estimate during silence.

        Uses exponential moving average for smooth adaptation.

        Args:
            silence_rms: RMS of current silence chunk
        """
        if silence_rms < 0.0001:
            # Ignore near-zero readings (could be digital silence)
            return

        # Exponential moving average
        alpha = self.config.noise_floor_alpha
        self.noise_floor = alpha * silence_rms + (1 - alpha) * self.noise_floor

        # Clamp to reasonable range
        self.noise_floor = max(0.001, min(self.noise_floor, 0.5))

    def _calculate_max_safe_gain(self) -> float:
        """Calculate maximum safe gain based on noise floor.

        Ensures: noise_floor * max_safe_gain < vad_threshold

        Returns:
            Maximum gain that keeps amplified noise below VAD threshold
        """
        if self.noise_floor < 0.001:
            return self.config.max_gain

        max_safe = self.config.vad_threshold_rms / self.noise_floor
        return max(self.config.min_gain, min(max_safe, self.config.max_gain))

    def _apply_gain(self, pcm_bytes: bytes, gain: float) -> bytes:
        """Apply gain multiplier to PCM audio.

        Args:
            pcm_bytes: Raw PCM audio bytes (16-bit signed, mono)
            gain: Gain multiplier to apply

        Returns:
            Gain-adjusted PCM audio bytes with clipping prevention
        """
        if abs(gain - 1.0) < 0.01:
            # No significant gain change needed
            return pcm_bytes

        n_samples = len(pcm_bytes) // 2
        samples = struct.unpack(f'<{n_samples}h', pcm_bytes)

        # Apply gain with clipping prevention
        adjusted = []
        for s in samples:
            new_val = int(s * gain)
            # Clip to 16-bit range
            new_val = max(-32768, min(32767, new_val))
            adjusted.append(new_val)

        return struct.pack(f'<{n_samples}h', *adjusted)

    @property
    def current_gain(self) -> float:
        """Current effective gain for diagnostics."""
        max_safe = self._calculate_max_safe_gain()
        return min(self.speech_gain, max_safe)

    @property
    def diagnostics(self) -> dict:
        """Return diagnostic information for logging/debugging."""
        return {
            "speech_gain": round(self.speech_gain, 3),
            "noise_floor": round(self.noise_floor, 4),
            "max_safe_gain": round(self._calculate_max_safe_gain(), 3),
            "failure_gain_cap": round(self.failure_gain_cap, 1),
            "effective_gain": round(self.current_gain, 3),
            "consecutive_failures": self.consecutive_failures,
        }
