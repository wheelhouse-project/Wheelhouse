"""Tests for AudioCapture."""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import asyncio
from unittest.mock import MagicMock, patch
import pytest

from stt.audio_capture import AudioCapture, AudioCaptureError


class TestAudioCaptureInit:
    """Tests for AudioCapture initialization."""

    def test_default_init(self):
        """Test default initialization values."""
        capture = AudioCapture()
        assert capture.sample_rate == 16000
        assert capture.channels == 1
        assert capture.chunk_ms == 30
        assert capture.device is None
        assert capture._running is False

    def test_custom_init(self):
        """Test custom initialization values."""
        capture = AudioCapture(
            sample_rate=8000,
            channels=2,
            chunk_ms=20,
            device=1,
            queue_max_size=50,
        )
        assert capture.sample_rate == 8000
        assert capture.channels == 2
        assert capture.chunk_ms == 20
        assert capture.device == 1
        assert capture.queue_max_size == 50

    def test_chunk_size_calculation(self):
        """Test chunk size is calculated correctly."""
        # 16000 Hz * 30ms / 1000 = 480 samples
        capture = AudioCapture(sample_rate=16000, chunk_ms=30)
        assert capture.chunk_size == 480

        # 8000 Hz * 20ms / 1000 = 160 samples
        capture = AudioCapture(sample_rate=8000, chunk_ms=20)
        assert capture.chunk_size == 160


class TestAudioCaptureLifecycle:
    """Tests for AudioCapture start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_stream(self):
        """Test start creates and starts audio stream."""
        capture = AudioCapture()

        mock_stream = MagicMock()

        with patch("stt.audio_capture.sd") as mock_sd:
            mock_sd.InputStream.return_value = mock_stream

            await capture.start()

            assert capture._running is True
            mock_sd.InputStream.assert_called_once()
            mock_stream.start.assert_called_once()

            await capture.stop()

    @pytest.mark.asyncio
    async def test_start_already_running(self):
        """Test start when already running does nothing."""
        capture = AudioCapture()
        capture._running = True

        # Should return early without creating stream
        await capture.start()
        assert capture._stream is None

    @pytest.mark.asyncio
    async def test_start_raises_on_error(self):
        """Test start raises AudioCaptureError on failure."""
        capture = AudioCapture()

        with patch("stt.audio_capture.sd") as mock_sd:
            mock_sd.InputStream.side_effect = Exception("Device error")

            with pytest.raises(AudioCaptureError) as exc_info:
                await capture.start()

            assert "Failed to start" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_stop_cleans_up(self):
        """Test stop cleans up resources."""
        capture = AudioCapture()
        mock_stream = MagicMock()
        capture._stream = mock_stream
        capture._running = True

        await capture.stop()

        assert capture._running is False
        mock_stream.stop.assert_called_once()
        mock_stream.close.assert_called_once()
        assert capture._stream is None

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        """Test stop is safe without start."""
        capture = AudioCapture()
        await capture.stop()  # Should not raise


class TestAudioCaptureStats:
    """Tests for AudioCapture statistics."""

    def test_get_stats_initial(self):
        """Test stats after initialization."""
        capture = AudioCapture()
        stats = capture.get_stats()

        assert stats["frames_captured"] == 0
        assert stats["frames_dropped"] == 0
        assert stats["queue_size"] == 0
        assert stats["running"] is False

    def test_audio_callback_updates_stats(self):
        """Test audio callback updates frame counter."""
        capture = AudioCapture()
        capture._running = True

        # Simulate callback with mock data
        import numpy as np

        mock_data = np.zeros((480,), dtype=np.int16)
        capture._audio_callback(mock_data, 480, None, None)

        assert capture._frames_captured == 1

        # Another callback
        capture._audio_callback(mock_data, 480, None, None)
        assert capture._frames_captured == 2

    def test_audio_callback_drops_when_queue_full(self):
        """Test callback drops frames when queue is full."""
        capture = AudioCapture(queue_max_size=1)
        capture._running = True

        import numpy as np

        mock_data = np.zeros((480,), dtype=np.int16)

        # Fill queue
        capture._audio_callback(mock_data, 480, None, None)
        # This should be dropped
        capture._audio_callback(mock_data, 480, None, None)

        assert capture._frames_captured == 2
        assert capture._frames_dropped == 1


class TestAudioCaptureStreaming:
    """Tests for AudioCapture async streaming."""

    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self):
        """Test stream yields queued audio chunks."""
        capture = AudioCapture()
        capture._running = True

        # Pre-populate queue
        test_chunk = b"\x00" * 960
        capture._queue.put(test_chunk)

        # Collect one chunk
        chunks = []
        async for chunk in capture.stream():
            chunks.append(chunk)
            capture._running = False  # Stop after first chunk

        assert len(chunks) == 1
        assert chunks[0] == test_chunk

    @pytest.mark.asyncio
    async def test_stream_stops_when_not_running(self):
        """Test stream stops when _running becomes False."""
        capture = AudioCapture()
        capture._running = False

        chunks = []
        async for chunk in capture.stream():
            chunks.append(chunk)

        assert len(chunks) == 0


class TestAudioCaptureDeviceListing:
    """Tests for device listing."""

    def test_list_devices_returns_input_devices(self):
        """Test list_devices returns input devices only."""
        mock_devices = [
            {
                "name": "Microphone",
                "max_input_channels": 2,
                "max_output_channels": 0,
                "default_samplerate": 44100.0,
            },
            {
                "name": "Speakers",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
            },
            {
                "name": "Headset",
                "max_input_channels": 1,
                "max_output_channels": 2,
                "default_samplerate": 16000.0,
            },
        ]

        with patch("stt.audio_capture.sd") as mock_sd:
            mock_sd.query_devices.return_value = mock_devices

            devices = AudioCapture.list_devices()

            # Should only return input devices (2 of 3)
            assert len(devices) == 2
            assert devices[0]["name"] == "Microphone"
            assert devices[0]["channels"] == 2
            assert devices[1]["name"] == "Headset"

    def test_list_devices_handles_error(self):
        """Test list_devices returns empty list on error."""
        with patch("stt.audio_capture.sd") as mock_sd:
            mock_sd.query_devices.side_effect = Exception("Device error")

            devices = AudioCapture.list_devices()
            assert devices == []


class TestAudioCaptureVAD:
    """Tests for Voice Activity Detection filtering."""

    def test_vad_disabled_by_default(self):
        """Test VAD is disabled by default."""
        capture = AudioCapture()
        assert capture._vad_enabled is False
        assert capture._vad is None

    def test_vad_enabled_init(self):
        """Test VAD configuration from init."""
        capture = AudioCapture(
            vad_enabled=True,
            vad_threshold=0.7,
            vad_lead_in_chunks=5,
        )
        assert capture._vad_enabled is True
        assert capture._vad_threshold == 0.7
        assert capture._vad_lead_in_chunks == 5

    @pytest.mark.asyncio
    async def test_vad_disabled_passes_all_chunks(self):
        """Test all chunks pass through when VAD is disabled."""
        capture = AudioCapture(vad_enabled=False)
        capture._running = True

        # Pre-populate queue with test chunks
        test_chunks = [b"\x00" * 960 for _ in range(3)]
        for chunk in test_chunks:
            capture._queue.put(chunk)

        # Collect chunks
        collected = []
        async for chunk in capture.stream():
            collected.append(chunk)
            if len(collected) >= 3:
                capture._running = False

        assert len(collected) == 3

    @pytest.mark.asyncio
    async def test_vad_filters_silence(self):
        """Test VAD filters silent chunks."""
        capture = AudioCapture(vad_enabled=True, vad_lead_in_chunks=2)
        capture._running = True

        # Mock VAD that always returns False (silence)
        mock_vad = MagicMock()
        mock_vad.is_speech.return_value = False
        capture._vad = mock_vad

        # Add test chunks
        num_chunks = 5
        for _ in range(num_chunks):
            capture._queue.put(b"\x00" * 960)

        # Try to collect chunks with a timeout
        # Since all are silence, nothing should yield
        collected = []
        try:
            async with asyncio.timeout(0.5):
                async for chunk in capture.stream():
                    collected.append(chunk)
        except asyncio.TimeoutError:
            pass  # Expected - nothing yields so we hit timeout
        finally:
            capture._running = False

        # VAD should have called is_speech for each chunk
        assert mock_vad.is_speech.call_count == num_chunks
        # No chunks yielded (all filtered)
        assert len(collected) == 0
        # Frames that exceeded lead-in buffer capacity are counted as filtered
        assert capture._frames_vad_filtered >= num_chunks - capture._vad_lead_in_chunks

    @pytest.mark.asyncio
    async def test_vad_passes_speech(self):
        """Test VAD passes speech chunks."""
        capture = AudioCapture(vad_enabled=True)
        capture._running = True

        # Mock VAD that always returns True (speech)
        mock_vad = MagicMock()
        mock_vad.is_speech.return_value = True
        capture._vad = mock_vad

        # Add test chunks
        speech_chunk = b"\xff" * 960
        capture._queue.put(speech_chunk)

        # Collect one chunk
        collected = []
        async for chunk in capture.stream():
            collected.append(chunk)
            capture._running = False

        assert len(collected) == 1
        assert collected[0] == speech_chunk

    @pytest.mark.asyncio
    async def test_vad_lead_in_buffer(self):
        """Test lead-in buffer flushes on speech detection."""
        capture = AudioCapture(vad_enabled=True, vad_lead_in_chunks=2)
        capture._running = True

        # Mock VAD: first 2 calls silence, then speech
        mock_vad = MagicMock()
        mock_vad.is_speech.side_effect = [False, False, True, False]
        capture._vad = mock_vad

        # Add chunks: 2 silent (buffered), 1 speech (triggers flush)
        silence1 = b"\x00" * 960
        silence2 = b"\x01" * 960
        speech = b"\xff" * 960
        trailing = b"\x02" * 960
        capture._queue.put(silence1)
        capture._queue.put(silence2)
        capture._queue.put(speech)
        capture._queue.put(trailing)

        # Collect all chunks
        collected = []
        async for chunk in capture.stream():
            collected.append(chunk)
            if len(collected) >= 4:
                capture._running = False

        # Should get: lead-in (2) + speech (1) + trailing (1) = 4
        assert len(collected) == 4
        # Lead-in buffer should have been flushed first
        assert collected[0] == silence1
        assert collected[1] == silence2
        assert collected[2] == speech

    def test_vad_stats_tracking(self):
        """Test VAD filtered frames are tracked in stats."""
        capture = AudioCapture(vad_enabled=True)
        capture._frames_captured = 100
        capture._frames_dropped = 5
        capture._frames_vad_filtered = 50

        stats = capture.get_stats()

        assert stats["frames_captured"] == 100
        assert stats["frames_dropped"] == 5
        assert stats["frames_vad_filtered"] == 50
        assert stats["vad_enabled"] is True

