"""Tests for Silero VAD wrapper.

These tests verify that:
1. SileroVAD can be instantiated with correct config
2. is_speech returns bool
3. Buffer handles various chunk sizes
4. Reset clears internal state
"""
import struct
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def make_pcm(samples):
    """Convert list of samples to PCM bytes."""
    return struct.pack(f'<{len(samples)}h', *samples)


class TestSileroVADConfig:
    """Tests for SileroVAD configuration."""

    def test_default_threshold(self):
        """Default threshold should be 0.5."""
        # Mock the Silero detector to avoid loading model
        with patch('shared_audio.silero_vad._get_silero_detector') as mock_detector:
            mock_detector.return_value = MagicMock(return_value=0.0)
            from shared_audio.silero_vad import SileroVAD
            vad = SileroVAD()
            assert vad.threshold == 0.5

    def test_custom_threshold(self):
        """Custom threshold should be accepted."""
        with patch('shared_audio.silero_vad._get_silero_detector') as mock_detector:
            mock_detector.return_value = MagicMock(return_value=0.0)
            from shared_audio.silero_vad import SileroVAD
            vad = SileroVAD(threshold=0.7)
            assert vad.threshold == 0.7

    def test_invalid_sample_rate_raises(self):
        """Non-16000 Hz sample rate should raise ValueError."""
        with patch('shared_audio.silero_vad._get_silero_detector') as mock_detector:
            mock_detector.return_value = MagicMock(return_value=0.0)
            from shared_audio.silero_vad import SileroVAD
            with pytest.raises(ValueError):
                SileroVAD(sample_rate=8000)


class TestSileroVADSpeechDetection:
    """Tests for speech detection."""

    def test_is_speech_returns_bool(self):
        """is_speech should return a boolean."""
        with patch('shared_audio.silero_vad._get_silero_detector') as mock_detector:
            mock_detector.return_value = MagicMock(return_value=0.6)
            from shared_audio.silero_vad import SileroVAD
            vad = SileroVAD(threshold=0.5)

            # Create 1024 bytes (512 samples) of audio
            samples = [100] * 512
            pcm = make_pcm(samples)

            result = vad.is_speech(pcm)
            assert isinstance(result, bool)

    def test_high_confidence_is_speech(self):
        """High confidence should return True."""
        with patch('shared_audio.silero_vad._get_silero_detector') as mock_detector:
            mock_detector.return_value = MagicMock(return_value=0.8)
            from shared_audio.silero_vad import SileroVAD
            vad = SileroVAD(threshold=0.5)

            samples = [100] * 512
            pcm = make_pcm(samples)

            result = vad.is_speech(pcm)
            assert result is True

    def test_low_confidence_not_speech(self):
        """Low confidence should return False."""
        with patch('shared_audio.silero_vad._get_silero_detector') as mock_detector:
            mock_detector.return_value = MagicMock(return_value=0.2)
            from shared_audio.silero_vad import SileroVAD
            vad = SileroVAD(threshold=0.5)

            samples = [100] * 512
            pcm = make_pcm(samples)

            result = vad.is_speech(pcm)
            assert result is False


class TestSileroVADBuffering:
    """Tests for audio buffering."""

    def test_small_chunks_buffered(self):
        """Small chunks should be buffered until 1024 bytes."""
        with patch('shared_audio.silero_vad._get_silero_detector') as mock_detector:
            mock_detector.return_value = MagicMock(return_value=0.0)
            from shared_audio.silero_vad import SileroVAD
            vad = SileroVAD()

            # Send small chunk (less than 1024 bytes)
            samples = [100] * 100  # 200 bytes
            pcm = make_pcm(samples)

            # First call - buffer not full, uses last confidence (0.0)
            vad.is_speech(pcm)
            assert len(vad._buffer) == 200

    def test_buffer_cleared_after_processing(self):
        """Buffer should be cleared after processing full chunk."""
        with patch('shared_audio.silero_vad._get_silero_detector') as mock_detector:
            mock_detector.return_value = MagicMock(return_value=0.0)
            from shared_audio.silero_vad import SileroVAD
            vad = SileroVAD()

            # Send exactly 1024 bytes
            samples = [100] * 512
            pcm = make_pcm(samples)

            vad.is_speech(pcm)
            # Buffer should be empty after processing
            assert len(vad._buffer) == 0


class TestSileroVADConfidence:
    """Tests for confidence retrieval."""

    def test_get_confidence(self):
        """get_confidence should return last computed value."""
        with patch('shared_audio.silero_vad._get_silero_detector') as mock_detector:
            mock_detector.return_value = MagicMock(return_value=0.75)
            from shared_audio.silero_vad import SileroVAD
            vad = SileroVAD()

            samples = [100] * 512
            pcm = make_pcm(samples)

            vad.is_speech(pcm)
            assert vad.get_confidence() == 0.75


class TestSileroVADReset:
    """Tests for reset functionality."""

    def test_reset_clears_buffer(self):
        """reset should clear the audio buffer."""
        with patch('shared_audio.silero_vad._get_silero_detector') as mock_detector:
            mock_detector.return_value = MagicMock(return_value=0.0)
            from shared_audio.silero_vad import SileroVAD
            vad = SileroVAD()

            # Add some data to buffer
            samples = [100] * 100
            pcm = make_pcm(samples)
            vad.is_speech(pcm)

            assert len(vad._buffer) > 0

            vad.reset()
            assert len(vad._buffer) == 0
            assert vad._last_confidence == 0.0
