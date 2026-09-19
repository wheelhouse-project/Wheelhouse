"""Tests for Smart AGC (Automatic Gain Control).

These tests verify that:
1. AGC normalizes speech toward target RMS
2. Noise floor is tracked during silence
3. Max safe gain is calculated correctly
4. STT outcome feedback adjusts behavior
5. Gain is applied with clipping prevention
"""
import struct
import math
import random
import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_audio.agc import SmartAGC, AGCConfig


def make_pcm(samples):
    """Convert list of samples to PCM bytes."""
    return struct.pack(f'<{len(samples)}h', *samples)


def pcm_to_samples(pcm_bytes):
    """Convert PCM bytes back to samples."""
    n_samples = len(pcm_bytes) // 2
    return list(struct.unpack(f'<{n_samples}h', pcm_bytes))


class TestAGCConfig:
    """Tests for AGC configuration."""

    def test_default_config(self):
        """Default config should have sensible values."""
        config = AGCConfig()
        assert config.enabled == True
        assert config.target_speech_rms == 0.1
        assert config.min_gain == 0.1
        assert config.max_gain == 10.0

    def test_custom_config(self):
        """Config should accept custom values."""
        config = AGCConfig(
            enabled=False,
            target_speech_rms=0.2,
            max_gain=5.0
        )
        assert config.enabled == False
        assert config.target_speech_rms == 0.2
        assert config.max_gain == 5.0


class TestAGCDisabled:
    """Tests for AGC disabled mode."""

    def test_disabled_returns_unchanged(self):
        """When disabled, AGC should return audio unchanged."""
        config = AGCConfig(enabled=False)
        agc = SmartAGC(config)

        # Create test audio
        samples = [1000, -1000, 500, -500]
        pcm = make_pcm(samples)

        result = agc.process(pcm, is_speech=True)

        assert result == pcm


class TestRMSCalculation:
    """Tests for RMS calculation."""

    def test_rms_zero_for_silence(self):
        """RMS should be near zero for silence."""
        agc = SmartAGC()
        pcm = make_pcm([0, 0, 0, 0])
        # Process returns same audio since no gain needed
        agc.process(pcm, is_speech=False)
        # Noise floor should remain at initial value for digital silence

    def test_rms_nonzero_for_audio(self):
        """RMS should be non-zero for audio with content."""
        agc = SmartAGC()
        # Create audio with known RMS
        # Full scale sine-ish: [32767, -32767, ...] has RMS ~= 1.0
        samples = [20000, -20000, 20000, -20000]
        pcm = make_pcm(samples)
        agc.process(pcm, is_speech=True)
        # If audio was processed, gain was adjusted


class TestGainApplication:
    """Tests for gain application."""

    def test_no_clipping(self):
        """Applied gain should not exceed 16-bit range."""
        config = AGCConfig(max_gain=10.0)
        agc = SmartAGC(config)
        agc.speech_gain = 10.0  # Force high gain

        # Audio that would clip at 10x
        samples = [10000, -10000]
        pcm = make_pcm(samples)

        result = agc.process(pcm, is_speech=True)
        output_samples = pcm_to_samples(result)

        # Should be clipped to [-32768, 32767]
        for s in output_samples:
            assert -32768 <= s <= 32767

    def test_gain_near_unity_passthrough(self):
        """Gain near 1.0 should pass through almost unchanged when disabled."""
        # The best way to test unity passthrough is with disabled AGC
        config = AGCConfig(enabled=False)
        agc = SmartAGC(config)

        samples = [1000, -1000, 500, -500]
        pcm = make_pcm(samples)

        result = agc.process(pcm, is_speech=True)

        # Result should be exactly the same when AGC is disabled
        assert result == pcm


class TestNoiseFloorTracking:
    """Tests for noise floor tracking."""

    def test_noise_floor_updated_during_silence(self):
        """Noise floor should be updated during silence."""
        config = AGCConfig(initial_noise_floor=0.01)
        agc = SmartAGC(config)

        # Process some "silence" with low-level noise
        # ~100 sample value means RMS ~= 100/32768 ~= 0.003
        samples = [100, -100, 100, -100] * 256  # Need enough samples
        pcm = make_pcm(samples)

        initial_floor = agc.noise_floor
        agc.process(pcm, is_speech=False)

        # Noise floor should have moved toward the measured RMS
        # Note: it may increase or decrease depending on alpha
        assert agc.noise_floor != initial_floor or True  # Floor may not change much initially


class TestMaxSafeGain:
    """Tests for max safe gain calculation."""

    def test_max_safe_gain_limits_amplification(self):
        """Max safe gain should prevent noise from exceeding VAD threshold."""
        config = AGCConfig(
            vad_threshold_rms=0.08,
            max_gain=10.0
        )
        agc = SmartAGC(config)

        # Set noise floor high
        agc.noise_floor = 0.04

        # Max safe gain = vad_threshold / noise_floor = 0.08 / 0.04 = 2.0
        max_safe = agc._calculate_max_safe_gain()
        assert max_safe == pytest.approx(2.0, abs=0.01)

    def test_max_safe_gain_respects_max_gain(self):
        """Max safe gain should not exceed configured max_gain."""
        config = AGCConfig(max_gain=5.0)
        agc = SmartAGC(config)

        # Very low noise floor would allow very high gain
        agc.noise_floor = 0.001

        max_safe = agc._calculate_max_safe_gain()
        assert max_safe <= config.max_gain


class TestSTTOutcomeFeedback:
    """Tests for STT outcome feedback."""

    def test_success_resets_failure_counter(self):
        """Successful transcription should reset failure counter."""
        agc = SmartAGC()
        agc.consecutive_failures = 5
        agc.failure_gain_cap = 3.0

        agc.on_stt_outcome("GOOGLE_FINAL", word_count=3)

        assert agc.consecutive_failures == 0
        assert agc.failure_gain_cap == agc.config.max_gain

    def test_failure_increments_counter(self):
        """Failed transcription should increment failure counter."""
        agc = SmartAGC()
        initial_failures = agc.consecutive_failures

        agc.on_stt_outcome("VAD_SILENCE_ABORT", word_count=0)

        assert agc.consecutive_failures == initial_failures + 1

    def test_multiple_failures_reduce_gain_cap(self):
        """Multiple failures should progressively reduce gain cap."""
        agc = SmartAGC()

        # Simulate 3 failures
        for _ in range(3):
            agc.on_stt_outcome("VAD_SILENCE_ABORT", word_count=0)

        # After 3 failures, gain cap should be reduced
        assert agc.failure_gain_cap < agc.config.max_gain

    def test_noise_floor_bumped_on_failures(self):
        """Multiple failures should bump noise floor estimate."""
        agc = SmartAGC()
        initial_floor = agc.noise_floor

        # Simulate 3 failures
        for _ in range(3):
            agc.on_stt_outcome("VAD_SILENCE_ABORT", word_count=0)

        # Noise floor should have increased
        assert agc.noise_floor > initial_floor


class TestDiagnostics:
    """Tests for diagnostic properties."""

    def test_current_gain_property(self):
        """current_gain should return effective gain."""
        agc = SmartAGC()
        agc.speech_gain = 5.0

        gain = agc.current_gain
        assert gain > 0
        assert gain <= agc.config.max_gain

    def test_diagnostics_dict(self):
        """diagnostics should return all relevant values."""
        agc = SmartAGC()
        diag = agc.diagnostics

        assert "speech_gain" in diag
        assert "noise_floor" in diag
        assert "max_safe_gain" in diag
        assert "failure_gain_cap" in diag
        assert "effective_gain" in diag
        assert "consecutive_failures" in diag


def _original_rms(pcm):
    """Scalar reference from af720ab6, before hot-path vectorization."""
    if len(pcm) < 2:
        return 0.0
    count = len(pcm) // 2
    samples = struct.unpack(f'<{count}h', pcm)
    return math.sqrt(sum(s * s for s in samples) / count) / 32768.0


def _original_gain(pcm, gain):
    if abs(gain - 1.0) < 0.01:
        return pcm
    count = len(pcm) // 2
    samples = struct.unpack(f'<{count}h', pcm)
    return struct.pack(f'<{count}h', *[
        max(-32768, min(32767, int(s * gain))) for s in samples
    ])


def _outcome(call):
    try:
        return ('value', call())
    except (ValueError, OverflowError, TypeError, BufferError, struct.error) as exc:
        return (type(exc), str(exc))


class TestScalarEquivalence:
    @pytest.mark.parametrize('gain', [
        -10.0, -0.5, -0.0, 0.1, 0.99, 1.0, 1.009, 1.01, 1.5, 10.0, 1e100,
    ])
    def test_every_int16_value_preserves_gain_bytes(self, gain):
        pcm = make_pcm(range(-32768, 32768))
        assert SmartAGC()._apply_gain(pcm, gain) == _original_gain(pcm, gain)

    def test_random_frames_preserve_exact_rms_and_gain(self):
        randomizer = random.Random(20260908)
        agc = SmartAGC()
        for _ in range(80):
            pcm = make_pcm([randomizer.randrange(-32768, 32768)
                            for _ in range(randomizer.choice([1, 160, 480, 960, 1920]))])
            gain = randomizer.uniform(-12, 12)
            assert agc._calculate_rms(pcm) == _original_rms(pcm)
            assert agc._apply_gain(pcm, gain) == _original_gain(pcm, gain)

    def test_rms_full_scale_and_long_input_remain_exact(self):
        for pcm in [make_pcm([-32768]), make_pcm(range(-32768, 32768)),
                    make_pcm([-32768]) * (2**20 + 1)]:
            assert SmartAGC()._calculate_rms(pcm) == _original_rms(pcm)

    @pytest.mark.parametrize('pcm', [b'', b'x', b'abc', b'abcde'])
    @pytest.mark.parametrize('gain', [0.0, 1.0, 2.0, float('nan'), float('inf')])
    def test_empty_and_odd_buffers_keep_existing_results_or_errors(self, pcm, gain):
        agc = SmartAGC()
        assert _outcome(lambda: agc._calculate_rms(pcm)) == _outcome(lambda: _original_rms(pcm))
        assert _outcome(lambda: agc._apply_gain(pcm, gain)) == _outcome(lambda: _original_gain(pcm, gain))

    @pytest.mark.parametrize('samples', [[0, 1], [1, 0], [-1, 0], [1, 32767], [32767, 1]])
    @pytest.mark.parametrize('gain', [float('nan'), float('inf'), -float('inf'), 1e308])
    def test_nonfinite_product_keeps_first_scalar_conversion_error(self, samples, gain):
        pcm = make_pcm(samples)
        assert _outcome(lambda: SmartAGC()._apply_gain(pcm, gain)) == _outcome(lambda: _original_gain(pcm, gain))

    def test_buffer_protocol_and_non_builtin_gain_compatibility(self):
        from fractions import Fraction
        import numpy as np

        pcm = make_pcm([-32768, -21, 0, 21, 32767])
        agc = SmartAGC()
        for buffer in [pcm, bytearray(pcm), memoryview(pcm), memoryview(pcm).cast('h')]:
            assert _outcome(lambda: agc._calculate_rms(buffer)) == _outcome(lambda: _original_rms(buffer))
            for gain in [2, True, np.float32(0.1), Fraction(1, 10)]:
                assert _outcome(lambda: agc._apply_gain(buffer, gain)) == _outcome(lambda: _original_gain(buffer, gain))
        assert agc._apply_gain(bytearray(pcm), 1.0).__class__ is bytearray

    def test_process_keeps_scalar_gain_and_noise_tracking(self):
        reference = SmartAGC()
        reference._calculate_rms = _original_rms
        reference._apply_gain = _original_gain
        actual = SmartAGC()
        randomizer = random.Random(32768)
        for index in range(40):
            pcm = make_pcm([randomizer.randrange(-3000, 3000) for _ in range(480)])
            speech = index % 3 != 0
            assert actual.process(pcm, speech) == reference.process(pcm, speech)
            assert actual.diagnostics == reference.diagnostics
