"""Tests for shared_audio.capture.sounddevice_capture module.

Tests the sounddevice wrapper that adapts MicrophoneStream to the
AudioProvider interface.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from shared_audio.capture.sounddevice_capture import SounddeviceAudioCapture
from shared_audio.capture.base import AudioConfig


@pytest.fixture
def mock_microphone_stream():
    """Mock MicrophoneStream for testing."""
    with patch('shared_audio.capture.sounddevice_capture.SOUNDDEVICE_AVAILABLE', True):
        with patch('shared_audio.microphone.MicrophoneStream') as MockStream:
            mock_instance = Mock()
            mock_instance.start = Mock()
            mock_instance.stop = Mock()
            mock_instance.read = Mock(return_value=b'\x00' * 960)
            mock_instance.get_stats_snapshot = Mock(return_value={
                'captured': 100,
                'drops': 0,
                'qsize': 5,
                'max_q': 10
            })
            mock_instance.get_queue_size = Mock(return_value=5)
            mock_instance.overflow_monitor = Mock()
            mock_instance.reset_overflow_monitor = Mock()
            mock_instance.get_overflow_status = Mock(return_value={'count': 0})
            mock_instance.list_audio_devices = Mock(return_value=[])

            MockStream.return_value = mock_instance
            yield MockStream, mock_instance


class TestSounddeviceCaptureInitialization:
    """Test initialization and configuration."""

    def test_init_with_default_config(self):
        """Should initialize with default AudioConfig."""
        capture = SounddeviceAudioCapture()

        assert capture.config.rate == 16000
        assert capture.config.channels == 1
        assert capture.config.chunk_ms == 30
        assert capture.overflow_callback is None
        assert capture._stream is None

    def test_init_with_custom_config(self):
        """Should use provided AudioConfig."""
        config = AudioConfig(rate=8000, channels=2, chunk_ms=20)
        capture = SounddeviceAudioCapture(config=config)

        assert capture.config.rate == 8000
        assert capture.config.channels == 2
        assert capture.config.chunk_ms == 20

    def test_init_with_overflow_callback(self):
        """Should store overflow callback."""
        callback = Mock()
        capture = SounddeviceAudioCapture(overflow_callback=callback)

        assert capture.overflow_callback == callback


class TestSounddeviceCaptureStartStop:
    """Test start/stop lifecycle."""

    def test_start_creates_microphone_stream(self, mock_microphone_stream):
        """Should create and start MicrophoneStream."""
        MockStream, mock_instance = mock_microphone_stream
        config = AudioConfig(rate=16000, channels=1, chunk_ms=30)
        capture = SounddeviceAudioCapture(config=config)

        capture.start()

        MockStream.assert_called_once_with(
            rate=16000,
            channels=1,
            chunk_ms=30,
            device_index=None,
            overflow_callback=None
        )
        mock_instance.start.assert_called_once()
        assert capture._stream == mock_instance

    def test_start_with_device_index(self, mock_microphone_stream):
        """Should pass device_index to MicrophoneStream."""
        MockStream, _ = mock_microphone_stream
        config = AudioConfig(rate=16000, channels=1, device_index=2)
        capture = SounddeviceAudioCapture(config=config)

        capture.start()

        MockStream.assert_called_once_with(
            rate=16000,
            channels=1,
            chunk_ms=30,
            device_index=2,
            overflow_callback=None
        )

    def test_start_with_overflow_callback(self, mock_microphone_stream):
        """Should pass overflow_callback to MicrophoneStream."""
        MockStream, _ = mock_microphone_stream
        callback = Mock()
        capture = SounddeviceAudioCapture(overflow_callback=callback)

        capture.start()

        MockStream.assert_called_once()
        args, kwargs = MockStream.call_args
        assert kwargs['overflow_callback'] == callback

    def test_start_when_already_started_is_noop(self, mock_microphone_stream):
        """Should be no-op when already started."""
        MockStream, mock_instance = mock_microphone_stream
        capture = SounddeviceAudioCapture()

        capture.start()
        first_stream = capture._stream
        mock_instance.start.reset_mock()

        capture.start()  # Second start

        # Should not create new stream or call start again
        assert capture._stream is first_stream
        mock_instance.start.assert_not_called()

    def test_start_raises_when_sounddevice_unavailable(self):
        """Should raise RuntimeError when sounddevice not available."""
        with patch('shared_audio.capture.sounddevice_capture.SOUNDDEVICE_AVAILABLE', False):
            capture = SounddeviceAudioCapture()

            with pytest.raises(RuntimeError, match="sounddevice not available"):
                capture.start()

    def test_stop_calls_stream_stop(self, mock_microphone_stream):
        """Should call stop on MicrophoneStream."""
        _, mock_instance = mock_microphone_stream
        capture = SounddeviceAudioCapture()
        capture.start()

        capture.stop()

        mock_instance.stop.assert_called_once()
        assert capture._stream is None

    def test_stop_when_not_started_is_noop(self):
        """Should be no-op when not started."""
        capture = SounddeviceAudioCapture()
        # Should not raise
        capture.stop()
        assert capture._stream is None


class TestSounddeviceCaptureRead:
    """Test audio reading."""

    def test_read_returns_audio_from_stream(self, mock_microphone_stream):
        """Should return audio bytes from MicrophoneStream.read()."""
        _, mock_instance = mock_microphone_stream
        mock_instance.read.return_value = b'\x01\x02\x03\x04'
        capture = SounddeviceAudioCapture()
        capture.start()

        audio = capture.read(timeout=1.0)

        mock_instance.read.assert_called_once_with(timeout=1.0)
        assert audio == b'\x01\x02\x03\x04'

    def test_read_with_custom_timeout(self, mock_microphone_stream):
        """Should pass timeout to stream.read()."""
        _, mock_instance = mock_microphone_stream
        capture = SounddeviceAudioCapture()
        capture.start()

        capture.read(timeout=2.5)

        mock_instance.read.assert_called_once_with(timeout=2.5)

    def test_read_returns_none_when_not_started(self):
        """Should return None when stream not started."""
        capture = SounddeviceAudioCapture()

        audio = capture.read()

        assert audio is None

    def test_read_returns_none_on_timeout(self, mock_microphone_stream):
        """Should return None when stream returns None."""
        _, mock_instance = mock_microphone_stream
        mock_instance.read.return_value = None
        capture = SounddeviceAudioCapture()
        capture.start()

        audio = capture.read(timeout=0.1)

        assert audio is None


class TestSounddeviceCaptureStats:
    """Test statistics and monitoring."""

    def test_get_stats_returns_stream_stats(self, mock_microphone_stream):
        """Should return stats from MicrophoneStream."""
        _, mock_instance = mock_microphone_stream
        mock_instance.get_stats_snapshot.return_value = {
            'captured': 200,
            'drops': 5,
            'qsize': 3,
            'max_q': 15
        }
        capture = SounddeviceAudioCapture()
        capture.start()

        stats = capture.get_stats()

        assert stats['captured'] == 200
        assert stats['drops'] == 5
        assert stats['qsize'] == 3
        assert stats['max_q'] == 15

    def test_get_stats_when_not_started(self):
        """Should return zeros when stream not started."""
        capture = SounddeviceAudioCapture()

        stats = capture.get_stats()

        assert stats['captured'] == 0
        assert stats['drops'] == 0
        assert stats['qsize'] == 0
        assert stats['max_q'] == 0

    def test_get_queue_size_returns_stream_qsize(self, mock_microphone_stream):
        """Should return queue size from stream."""
        _, mock_instance = mock_microphone_stream
        mock_instance.get_queue_size.return_value = 7
        capture = SounddeviceAudioCapture()
        capture.start()

        qsize = capture.get_queue_size()

        assert qsize == 7

    def test_get_queue_size_when_not_started(self):
        """Should return 0 when not started."""
        capture = SounddeviceAudioCapture()

        qsize = capture.get_queue_size()

        assert qsize == 0


class TestSounddeviceCaptureOverflowMonitor:
    """Test overflow monitoring integration."""

    def test_overflow_monitor_property_returns_stream_monitor(self, mock_microphone_stream):
        """Should return overflow_monitor from stream."""
        _, mock_instance = mock_microphone_stream
        mock_monitor = Mock()
        mock_instance.overflow_monitor = mock_monitor
        capture = SounddeviceAudioCapture()
        capture.start()

        monitor = capture.overflow_monitor

        assert monitor is mock_monitor

    def test_overflow_monitor_when_not_started(self):
        """Should return None when stream not started."""
        capture = SounddeviceAudioCapture()

        monitor = capture.overflow_monitor

        assert monitor is None

    def test_reset_overflow_monitor_delegates_to_stream(self, mock_microphone_stream):
        """Should call reset_overflow_monitor on stream."""
        _, mock_instance = mock_microphone_stream
        capture = SounddeviceAudioCapture()
        capture.start()

        capture.reset_overflow_monitor()

        mock_instance.reset_overflow_monitor.assert_called_once()

    def test_reset_overflow_monitor_when_not_started(self):
        """Should be no-op when not started."""
        capture = SounddeviceAudioCapture()
        # Should not raise
        capture.reset_overflow_monitor()

    def test_get_overflow_status_returns_stream_status(self, mock_microphone_stream):
        """Should return overflow status from stream."""
        _, mock_instance = mock_microphone_stream
        mock_instance.get_overflow_status.return_value = {'count': 3, 'last_time': 100}
        capture = SounddeviceAudioCapture()
        capture.start()

        status = capture.get_overflow_status()

        assert status['count'] == 3
        assert status['last_time'] == 100

    def test_get_overflow_status_when_not_started(self):
        """Should return empty dict when not started."""
        capture = SounddeviceAudioCapture()

        status = capture.get_overflow_status()

        assert status == {}


class TestSounddeviceCaptureListDevices:
    """Test device enumeration."""

    def test_list_audio_devices_when_started(self, mock_microphone_stream):
        """Should delegate to stream when started."""
        _, mock_instance = mock_microphone_stream
        mock_devices = [
            {'index': 0, 'name': 'Device 1', 'rate': 16000, 'channels': 1},
            {'index': 1, 'name': 'Device 2', 'rate': 48000, 'channels': 2},
        ]
        mock_instance.list_audio_devices.return_value = mock_devices
        capture = SounddeviceAudioCapture()
        capture.start()

        devices = capture.list_audio_devices()

        mock_instance.list_audio_devices.assert_called_once()
        assert devices == mock_devices

    def test_list_audio_devices_when_not_started(self):
        """Should create temp stream to list devices when not started."""
        with patch('shared_audio.capture.sounddevice_capture.SOUNDDEVICE_AVAILABLE', True):
            with patch('shared_audio.microphone.MicrophoneStream') as MockStream:
                mock_temp = Mock()
                mock_devices = [{'index': 0, 'name': 'Test Device'}]
                mock_temp.list_audio_devices.return_value = mock_devices
                MockStream.return_value = mock_temp

                capture = SounddeviceAudioCapture()
                devices = capture.list_audio_devices()

                # Should create temporary stream
                MockStream.assert_called_once()
                mock_temp.list_audio_devices.assert_called_once()
                assert devices == mock_devices

    def test_list_audio_devices_when_sounddevice_unavailable(self):
        """Should return empty list when sounddevice unavailable."""
        with patch('shared_audio.capture.sounddevice_capture.SOUNDDEVICE_AVAILABLE', False):
            capture = SounddeviceAudioCapture()

            devices = capture.list_audio_devices()

            assert devices == []


class TestSounddeviceCaptureAdversarial:
    """Adversarial tests for edge cases."""

    def test_multiple_start_stop_cycles(self, mock_microphone_stream):
        """Should handle multiple start/stop cycles."""
        MockStream, mock_instance = mock_microphone_stream
        capture = SounddeviceAudioCapture()

        for i in range(3):
            capture.start()
            assert capture._stream is not None
            mock_instance.start.assert_called()

            capture.stop()
            assert capture._stream is None
            mock_instance.stop.assert_called()

            mock_instance.start.reset_mock()
            mock_instance.stop.reset_mock()

    def test_read_after_stop(self, mock_microphone_stream):
        """Should return None when reading after stop."""
        _, mock_instance = mock_microphone_stream
        capture = SounddeviceAudioCapture()
        capture.start()
        capture.stop()

        audio = capture.read()

        assert audio is None

    def test_concurrent_reads_share_stream(self, mock_microphone_stream):
        """Should use same stream for concurrent reads."""
        _, mock_instance = mock_microphone_stream
        mock_instance.read.side_effect = [b'\x01', b'\x02', b'\x03']
        capture = SounddeviceAudioCapture()
        capture.start()

        audio1 = capture.read()
        audio2 = capture.read()
        audio3 = capture.read()

        assert audio1 == b'\x01'
        assert audio2 == b'\x02'
        assert audio3 == b'\x03'
        assert mock_instance.read.call_count == 3
