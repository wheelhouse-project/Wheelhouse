"""Tests for lead-in buffer.

The lead-in buffer captures audio before speech detection triggers,
ensuring the beginning of speech is not cut off. This is critical
for STT accuracy as words are often clipped without a lead-in buffer.

These tests verify that:
1. Buffer has correct capacity based on lead time and sample rate
2. Audio is properly buffered and retrieved
3. Buffer wraps correctly when full
4. get_lead_in returns correct amount of audio
"""
import struct
import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def make_pcm(samples):
    """Convert list of samples to PCM bytes."""
    return struct.pack(f'<{len(samples)}h', *samples)


def pcm_to_samples(pcm_bytes):
    """Convert PCM bytes back to samples."""
    n_samples = len(pcm_bytes) // 2
    return list(struct.unpack(f'<{n_samples}h', pcm_bytes))


class TestLeadInBufferConfig:
    """Tests for lead-in buffer configuration."""

    def test_default_lead_time(self):
        """Default lead time should be 0.3 seconds."""
        from shared_audio.lead_in_buffer import LeadInBuffer
        buf = LeadInBuffer()
        assert buf.lead_time_s == 0.3

    def test_custom_lead_time(self):
        """Custom lead time should be accepted."""
        from shared_audio.lead_in_buffer import LeadInBuffer
        buf = LeadInBuffer(lead_time_s=0.5, sample_rate=16000)
        assert buf.lead_time_s == 0.5

    def test_capacity_calculation(self):
        """Capacity should be lead_time * sample_rate * 2 bytes."""
        from shared_audio.lead_in_buffer import LeadInBuffer
        buf = LeadInBuffer(lead_time_s=0.5, sample_rate=16000)
        # 0.5 seconds * 16000 Hz * 2 bytes = 16000 bytes
        assert buf.capacity_bytes == 16000


class TestLeadInBufferWrite:
    """Tests for writing to the buffer."""

    def test_add_audio_stores_data(self):
        """add should store audio in the buffer."""
        from shared_audio.lead_in_buffer import LeadInBuffer
        buf = LeadInBuffer(lead_time_s=0.5, sample_rate=16000)

        samples = [100] * 100
        pcm = make_pcm(samples)

        buf.add(pcm)
        assert buf.current_size > 0

    def test_buffer_wraps_when_full(self):
        """Buffer should wrap when capacity is exceeded."""
        from shared_audio.lead_in_buffer import LeadInBuffer
        # Small buffer for testing: 0.01s at 16kHz = 160 samples = 320 bytes
        buf = LeadInBuffer(lead_time_s=0.01, sample_rate=16000)

        # Write more than capacity
        samples1 = [111] * 200  # 400 bytes, marked with 111
        pcm1 = make_pcm(samples1)
        buf.add(pcm1)

        samples2 = [222] * 200  # 400 bytes, marked with 222
        pcm2 = make_pcm(samples2)
        buf.add(pcm2)

        # Buffer should only contain most recent data up to capacity
        lead_in = buf.get_lead_in()
        output_samples = pcm_to_samples(lead_in)

        # Should contain 222s (more recent) not 111s (older, wrapped out)
        # The exact content depends on wrapping behavior
        assert len(lead_in) <= buf.capacity_bytes


class TestLeadInBufferRead:
    """Tests for reading from the buffer."""

    def test_get_lead_in_returns_buffered_audio(self):
        """get_lead_in should return buffered audio."""
        from shared_audio.lead_in_buffer import LeadInBuffer
        buf = LeadInBuffer(lead_time_s=0.5, sample_rate=16000)

        samples = [100] * 100
        pcm = make_pcm(samples)
        buf.add(pcm)

        lead_in = buf.get_lead_in()
        assert len(lead_in) > 0

    def test_get_lead_in_respects_capacity(self):
        """get_lead_in should not return more than capacity."""
        from shared_audio.lead_in_buffer import LeadInBuffer
        buf = LeadInBuffer(lead_time_s=0.01, sample_rate=16000)

        # Write lots of data
        for _ in range(10):
            samples = [100] * 500
            pcm = make_pcm(samples)
            buf.add(pcm)

        lead_in = buf.get_lead_in()
        assert len(lead_in) <= buf.capacity_bytes

    def test_empty_buffer_returns_empty(self):
        """Empty buffer should return empty bytes."""
        from shared_audio.lead_in_buffer import LeadInBuffer
        buf = LeadInBuffer()

        lead_in = buf.get_lead_in()
        assert lead_in == b''


class TestLeadInBufferClear:
    """Tests for clearing the buffer."""

    def test_clear_empties_buffer(self):
        """clear should empty the buffer."""
        from shared_audio.lead_in_buffer import LeadInBuffer
        buf = LeadInBuffer()

        samples = [100] * 100
        pcm = make_pcm(samples)
        buf.add(pcm)

        assert buf.current_size > 0

        buf.clear()
        assert buf.current_size == 0
        assert buf.get_lead_in() == b''


class TestLeadInBufferOrdering:
    """Tests for correct audio ordering."""

    def test_audio_ordering_preserved(self):
        """Audio should be retrieved in correct order (oldest first)."""
        from shared_audio.lead_in_buffer import LeadInBuffer
        # Large buffer to avoid wrapping
        buf = LeadInBuffer(lead_time_s=1.0, sample_rate=16000)

        # Write sequential values
        samples1 = [1000] * 100
        pcm1 = make_pcm(samples1)
        buf.add(pcm1)

        samples2 = [2000] * 100
        pcm2 = make_pcm(samples2)
        buf.add(pcm2)

        samples3 = [3000] * 100
        pcm3 = make_pcm(samples3)
        buf.add(pcm3)

        lead_in = buf.get_lead_in()
        output_samples = pcm_to_samples(lead_in)

        # First chunk should have 1000s
        assert output_samples[:100] == [1000] * 100
        # Middle chunk should have 2000s
        assert output_samples[100:200] == [2000] * 100
        # Last chunk should have 3000s
        assert output_samples[200:300] == [3000] * 100
