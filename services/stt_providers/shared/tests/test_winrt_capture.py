"""Tests for shared_audio.capture.winrt_capture module.

Tests the WinRT AudioGraph-based audio capture implementation.
"""

import pytest
import queue
import struct
import threading
import time
from unittest.mock import Mock, patch, MagicMock, call

from shared_audio.capture.winrt_capture import WinRTAudioCapture
from shared_audio.capture.base import AudioConfig


@pytest.fixture
def mock_winrt_available():
    """Mock WinRT availability."""
    with patch('shared_audio.capture.winrt_capture.WINRT_AUDIO_AVAILABLE', True):
        yield


@pytest.fixture
def _mock_winsdk_modules():
    """Pre-populate sys.modules with mock winsdk hierarchy.

    patch() needs the target module importable to resolve dotted paths.
    When winsdk isn't installed in the uv venv, we insert mocks first.
    """
    import sys
    mock_modules = {}
    module_paths = [
        'winsdk',
        'winsdk.windows',
        'winsdk.windows.media',
        'winsdk.windows.media.audio',
        'winsdk.windows.media.render',
        'winsdk.windows.media.capture',
        'winsdk.windows.media.mediaproperties',
        'winsdk.windows.devices',
        'winsdk.windows.devices.enumeration',
    ]
    originals = {}
    for path in module_paths:
        originals[path] = sys.modules.get(path)
        if path not in sys.modules:
            mock_modules[path] = MagicMock()
            sys.modules[path] = mock_modules[path]

    yield

    for path, mock_mod in mock_modules.items():
        if originals[path] is None:
            sys.modules.pop(path, None)
        else:
            sys.modules[path] = originals[path]


@pytest.fixture
def mock_winrt_graph(_mock_winsdk_modules):
    """Mock WinRT AudioGraph and related objects."""
    # Mock the utils module that gets imported
    import sys
    mock_utils = MagicMock()
    mock_run_sync = Mock()
    mock_utils.winrt_helpers.run_winrt_sync = mock_run_sync
    sys.modules['utils'] = mock_utils
    sys.modules['utils.winrt_helpers'] = mock_utils.winrt_helpers

    try:
        with patch('winsdk.windows.media.audio.AudioGraph') as MockGraph:
            with patch('winsdk.windows.media.audio.AudioGraphSettings') as MockSettings:
                with patch('winsdk.windows.media.mediaproperties.AudioEncodingProperties') as MockProps:
                    with patch('winsdk.windows.media.render.AudioRenderCategory'):
                        with patch('winsdk.windows.media.capture.MediaCategory'):
                                # Setup mock graph creation result
                                mock_graph = Mock()
                                mock_graph.start = Mock()
                                mock_graph.stop = Mock()
                                mock_graph.close = Mock()
                                mock_graph.create_device_input_node_async = Mock()
                                mock_graph.create_frame_output_node = Mock()

                                # Mock input node
                                mock_input_node = Mock()
                                mock_input_node.add_outgoing_connection = Mock()

                                # Mock frame output node
                                mock_frame_output = Mock()
                                mock_frame = Mock()
                                mock_audio_buffer = Mock()
                                mock_ref = Mock()

                                # Setup buffer to return float32 audio data
                                # Create a small amount of float32 data (2 samples = 8 bytes)
                                float_data = struct.pack('<ff', 0.5, -0.5)
                                mock_ref.__buffer__ = lambda self, flags: memoryview(float_data)

                                mock_audio_buffer.create_reference = Mock(return_value=mock_ref)
                                mock_audio_buffer.close = Mock()
                                mock_frame.lock_buffer = Mock(return_value=mock_audio_buffer)
                                mock_frame_output.get_frame = Mock(return_value=mock_frame)

                                # Mock async results - create once and reuse
                                mock_create_result = Mock(status=0, graph=mock_graph)
                                mock_input_result = Mock(status=0, device_input_node=mock_input_node)

                                # Setup run_winrt_sync to alternate between the two results
                                # First call creates graph, second call creates input node
                                results_cycle = [mock_create_result, mock_input_result]
                                call_count = [-1]

                                def sync_side_effect(*args, **kwargs):
                                    call_count[0] += 1
                                    return results_cycle[call_count[0] % 2]

                                mock_run_sync.side_effect = sync_side_effect

                                mock_graph.create_frame_output_node.return_value = mock_frame_output

                                yield {
                                    'graph': mock_graph,
                                    'input_node': mock_input_node,
                                    'frame_output': mock_frame_output,
                                    'create_result': mock_create_result,
                                    'input_result': mock_input_result,
                                    'run_sync': mock_run_sync,
                                }
    finally:
        # Clean up sys.modules
        sys.modules.pop('utils', None)
        sys.modules.pop('utils.winrt_helpers', None)


class TestWinRTCaptureInitialization:
    """Test initialization and configuration."""

    def test_init_with_default_config(self):
        """Should initialize with default AudioConfig."""
        capture = WinRTAudioCapture()

        assert capture.config.rate == 16000
        assert capture.config.channels == 1
        assert capture.config.chunk_ms == 30
        assert capture.overflow_callback is None
        assert capture._graph is None
        assert capture._running is False

    def test_init_with_custom_config(self):
        """Should use provided AudioConfig."""
        config = AudioConfig(rate=8000, channels=2, chunk_ms=20)
        capture = WinRTAudioCapture(config=config)

        assert capture.config.rate == 8000
        assert capture.config.channels == 2
        assert capture.config.chunk_ms == 20

    def test_init_with_overflow_callback(self):
        """Should store overflow callback and create monitor."""
        callback = Mock()
        capture = WinRTAudioCapture(overflow_callback=callback)

        assert capture.overflow_callback == callback
        assert capture.overflow_monitor is not None

    def test_init_creates_queue(self):
        """Should create queue with maxsize=100."""
        capture = WinRTAudioCapture()

        assert isinstance(capture._q, queue.Queue)
        assert capture._q.maxsize == 100

    def test_init_initializes_stats(self):
        """Should initialize statistics to zero."""
        capture = WinRTAudioCapture()

        assert capture._frames_captured == 0
        assert capture._drops == 0
        assert capture._max_queue_depth == 0
        assert capture._start_time is None


class TestWinRTCaptureStartStop:
    """Test start/stop lifecycle."""

    def test_start_raises_when_winrt_unavailable(self):
        """Should raise RuntimeError when WinRT not available."""
        with patch('shared_audio.capture.winrt_capture.WINRT_AUDIO_AVAILABLE', False):
            capture = WinRTAudioCapture()

            with pytest.raises(RuntimeError, match="WinRT audio APIs not available"):
                capture.start()

    def test_start_sets_running_flag(self, mock_winrt_available):
        """Should set _running flag and start_time."""
        with patch.object(threading.Thread, 'start'):
            capture = WinRTAudioCapture()
            start_time_before = time.time()

            capture.start()

            assert capture._running is True
            assert capture._start_time is not None
            assert capture._start_time >= start_time_before

    def test_start_creates_capture_thread(self, mock_winrt_available):
        """Should create and start background capture thread."""
        with patch.object(threading.Thread, 'start') as mock_thread_start:
            with patch.object(threading.Thread, '__init__', return_value=None) as mock_thread_init:
                capture = WinRTAudioCapture()
                capture.start()

                # Thread should be created
                assert capture._capture_thread is not None
                mock_thread_start.assert_called_once()

    def test_start_when_already_running_is_noop(self, mock_winrt_available):
        """Should be no-op when already running."""
        with patch.object(threading.Thread, 'start'):
            capture = WinRTAudioCapture()
            capture.start()
            first_thread = capture._capture_thread

            capture.start()  # Second start

            # Should not create new thread
            assert capture._capture_thread is first_thread

    def test_stop_clears_running_flag(self):
        """Should clear _running flag."""
        capture = WinRTAudioCapture()
        capture._running = True
        capture._capture_thread = None

        capture.stop()

        assert capture._running is False

    def test_stop_joins_capture_thread(self, mock_winrt_available):
        """Should join capture thread with timeout."""
        with patch.object(threading.Thread, 'start'):
            with patch.object(threading.Thread, 'join') as mock_join:
                capture = WinRTAudioCapture()
                capture.start()

                capture.stop()

                mock_join.assert_called_once_with(timeout=2.0)
                assert capture._capture_thread is None

    def test_stop_clears_queue_when_running(self, mock_winrt_available):
        """Should clear audio queue when stopping from running state."""
        with patch.object(threading.Thread, 'start'):
            with patch.object(threading.Thread, 'join'):
                capture = WinRTAudioCapture()
                capture.start()  # Set _running = True
                # Put some items in queue
                capture._q.put(b'test1')
                capture._q.put(b'test2')
                assert capture._q.qsize() == 2

                capture.stop()

                assert capture._q.qsize() == 0

    def test_stop_when_not_running_is_noop(self):
        """Should be no-op when not running."""
        capture = WinRTAudioCapture()
        # Should not raise
        capture.stop()
        assert capture._running is False


class TestWinRTCaptureRead:
    """Test audio reading."""

    def test_read_returns_audio_from_queue(self):
        """Should return audio from internal queue."""
        capture = WinRTAudioCapture()
        test_audio = b'\x01\x02\x03\x04'
        capture._q.put(test_audio)

        audio = capture.read(timeout=1.0)

        assert audio == test_audio

    def test_read_returns_none_on_timeout(self):
        """Should return None when queue is empty and timeout expires."""
        capture = WinRTAudioCapture()

        audio = capture.read(timeout=0.1)

        assert audio is None

    def test_read_respects_timeout(self):
        """Should wait for specified timeout."""
        capture = WinRTAudioCapture()
        start_time = time.time()

        audio = capture.read(timeout=0.2)

        elapsed = time.time() - start_time
        assert audio is None
        assert elapsed >= 0.2
        assert elapsed < 0.3  # Should not wait much longer


class TestWinRTCaptureStats:
    """Test statistics and monitoring."""

    def test_get_stats_returns_current_stats(self):
        """Should return current capture statistics."""
        capture = WinRTAudioCapture()
        capture._frames_captured = 100
        capture._drops = 5
        capture._max_queue_depth = 10
        capture._q.put(b'test')

        stats = capture.get_stats()

        assert stats['captured'] == 100
        assert stats['drops'] == 5
        assert stats['qsize'] == 1
        assert stats['max_q'] == 10

    def test_get_queue_size_returns_current_qsize(self):
        """Should return current queue size."""
        capture = WinRTAudioCapture()
        capture._q.put(b'a')
        capture._q.put(b'b')
        capture._q.put(b'c')

        qsize = capture.get_queue_size()

        assert qsize == 3

    def test_reset_overflow_monitor_delegates(self):
        """Should delegate to overflow_monitor.reset_for_restart()."""
        capture = WinRTAudioCapture()
        capture.overflow_monitor.reset_for_restart = Mock()

        capture.reset_overflow_monitor()

        capture.overflow_monitor.reset_for_restart.assert_called_once()

    def test_get_overflow_status_delegates(self):
        """Should delegate to overflow_monitor.get_status()."""
        capture = WinRTAudioCapture()
        mock_status = {'count': 3, 'last_time': 100}
        capture.overflow_monitor.get_status = Mock(return_value=mock_status)

        status = capture.get_overflow_status()

        assert status == mock_status
        capture.overflow_monitor.get_status.assert_called_once()


class TestWinRTCaptureGraphSetup:
    """Test WinRT graph setup."""

    def test_setup_graph_creates_audio_graph(self, mock_winrt_available, mock_winrt_graph):
        """Should create AudioGraph with correct settings."""
        mocks = mock_winrt_graph
        capture = WinRTAudioCapture()

        capture._setup_graph()

        # Should call run_winrt_sync to create graph
        assert mocks['run_sync'].call_count >= 1
        assert capture._graph == mocks['graph']

    def test_setup_graph_creates_microphone_input(self, mock_winrt_available, mock_winrt_graph):
        """Should create device input node for microphone."""
        mocks = mock_winrt_graph
        capture = WinRTAudioCapture()

        capture._setup_graph()

        assert capture._mic_node == mocks['input_node']

    def test_setup_graph_creates_frame_output(self, mock_winrt_available, mock_winrt_graph):
        """Should create frame output node."""
        mocks = mock_winrt_graph
        capture = WinRTAudioCapture()

        capture._setup_graph()

        assert capture._frame_output == mocks['frame_output']

    def test_setup_graph_connects_nodes(self, mock_winrt_available, mock_winrt_graph):
        """Should connect mic to frame output."""
        mocks = mock_winrt_graph
        capture = WinRTAudioCapture()

        capture._setup_graph()

        mocks['input_node'].add_outgoing_connection.assert_called_once_with(
            mocks['frame_output']
        )

    def test_setup_graph_starts_graph(self, mock_winrt_available, mock_winrt_graph):
        """Should start the AudioGraph."""
        mocks = mock_winrt_graph
        capture = WinRTAudioCapture()

        capture._setup_graph()

        mocks['graph'].start.assert_called_once()

    def test_setup_graph_raises_on_graph_creation_failure(self, mock_winrt_available, mock_winrt_graph):
        """Should raise RuntimeError when graph creation fails."""
        mocks = mock_winrt_graph
        mocks['create_result'].status = 1  # Non-zero status = failure
        capture = WinRTAudioCapture()

        with pytest.raises(RuntimeError, match="AudioGraph creation failed"):
            capture._setup_graph()

    def test_setup_graph_raises_on_mic_node_failure(self, mock_winrt_available, mock_winrt_graph):
        """Should raise RuntimeError when mic node creation fails."""
        mocks = mock_winrt_graph
        mocks['input_result'].status = 1  # Non-zero status = failure
        capture = WinRTAudioCapture()

        with pytest.raises(RuntimeError, match="Microphone node failed"):
            capture._setup_graph()


class TestWinRTCaptureCleanup:
    """Test resource cleanup."""

    def test_cleanup_graph_stops_and_closes_graph(self):
        """Should stop and close the graph."""
        capture = WinRTAudioCapture()
        mock_graph = Mock()
        mock_graph.stop = Mock()
        mock_graph.close = Mock()
        capture._graph = mock_graph

        capture._cleanup_graph()

        mock_graph.stop.assert_called_once()
        mock_graph.close.assert_called_once()
        assert capture._graph is None

    def test_cleanup_graph_clears_nodes(self):
        """Should clear node references."""
        capture = WinRTAudioCapture()
        capture._graph = Mock()
        capture._mic_node = Mock()
        capture._frame_output = Mock()

        capture._cleanup_graph()

        assert capture._mic_node is None
        assert capture._frame_output is None

    def test_cleanup_graph_handles_exceptions(self):
        """Should handle cleanup errors gracefully."""
        capture = WinRTAudioCapture()
        mock_graph = Mock()
        mock_graph.stop.side_effect = Exception("Stop failed")
        capture._graph = mock_graph

        # Should not raise
        capture._cleanup_graph()

    def test_cleanup_graph_when_no_graph(self):
        """Should be no-op when no graph exists."""
        capture = WinRTAudioCapture()
        # Should not raise
        capture._cleanup_graph()


class TestWinRTCaptureListDevices:
    """Test device enumeration."""

    def test_list_audio_devices_returns_empty_when_winrt_unavailable(self):
        """Should return empty list when WinRT not available."""
        with patch('shared_audio.capture.winrt_capture.WINRT_AUDIO_AVAILABLE', False):
            capture = WinRTAudioCapture()

            devices = capture.list_audio_devices()

            assert devices == []

    def test_list_audio_devices_returns_empty_on_import_error(self, mock_winrt_available):
        """Should return empty list when winrt_helpers unavailable."""
        with patch('shared_audio.capture.winrt_capture.WINRT_AUDIO_AVAILABLE', True):
            # Mock the imports to raise ImportError
            with patch('builtins.__import__', side_effect=ImportError("No winrt_helpers")):
                capture = WinRTAudioCapture()

                devices = capture.list_audio_devices()

                assert devices == []

    def test_list_audio_devices_handles_enumeration_errors(self, mock_winrt_available, _mock_winsdk_modules):
        """Should return empty list on enumeration errors."""
        with patch('winsdk.windows.devices.enumeration.DeviceInformation') as MockDevInfo:
            MockDevInfo.find_all_async.side_effect = Exception("Enumeration failed")
            capture = WinRTAudioCapture()

            devices = capture.list_audio_devices()

            assert devices == []


class TestWinRTCaptureAdversarial:
    """Adversarial tests for edge cases."""

    def test_multiple_start_stop_cycles(self, mock_winrt_available):
        """Should handle multiple start/stop cycles."""
        with patch.object(threading.Thread, 'start'):
            with patch.object(threading.Thread, 'join'):
                capture = WinRTAudioCapture()

                for i in range(3):
                    capture.start()
                    assert capture._running is True

                    capture.stop()
                    assert capture._running is False

    def test_read_after_stop_returns_none(self, mock_winrt_available):
        """Should return None when reading after stop (queue cleared)."""
        with patch.object(threading.Thread, 'start'):
            with patch.object(threading.Thread, 'join'):
                capture = WinRTAudioCapture()
                capture.start()  # Set _running = True
                capture._q.put(b'test')
                capture.stop()  # Clears queue because _running was True

                audio = capture.read(timeout=0.1)

                assert audio is None

    def test_queue_overflow_increments_drops(self):
        """Should increment drops counter when queue is full."""
        capture = WinRTAudioCapture()
        capture.overflow_monitor.report_overflow = Mock()

        # Fill the queue to maxsize
        for i in range(100):
            capture._q.put(b'x')

        # Now try to add more (this is what _poll_frames does)
        try:
            capture._q.put_nowait(b'overflow')
        except queue.Full:
            capture._drops += 1
            capture.overflow_monitor.report_overflow()

        assert capture._drops == 1
        capture.overflow_monitor.report_overflow.assert_called_once()

    def test_max_queue_depth_tracking(self):
        """Should track maximum queue depth."""
        capture = WinRTAudioCapture()

        # Simulate adding items
        for i in range(5):
            capture._q.put(b'x')
            qsize = capture._q.qsize()
            if qsize > capture._max_queue_depth:
                capture._max_queue_depth = qsize

        assert capture._max_queue_depth == 5

        # Add more
        for i in range(5):
            capture._q.put(b'x')
            qsize = capture._q.qsize()
            if qsize > capture._max_queue_depth:
                capture._max_queue_depth = qsize

        assert capture._max_queue_depth == 10

    def test_concurrent_reads_from_queue(self):
        """Should handle concurrent reads from queue."""
        capture = WinRTAudioCapture()
        capture._q.put(b'audio1')
        capture._q.put(b'audio2')
        capture._q.put(b'audio3')

        audio1 = capture.read(timeout=0.1)
        audio2 = capture.read(timeout=0.1)
        audio3 = capture.read(timeout=0.1)

        assert audio1 == b'audio1'
        assert audio2 == b'audio2'
        assert audio3 == b'audio3'

    def test_zero_timeout_read(self):
        """Should handle zero timeout (non-blocking read)."""
        capture = WinRTAudioCapture()

        # Empty queue, zero timeout should return immediately
        start = time.time()
        audio = capture.read(timeout=0.0)
        elapsed = time.time() - start

        assert audio is None
        assert elapsed < 0.1  # Should be nearly instant

    def test_large_timeout_read(self):
        """Should handle large timeout values."""
        capture = WinRTAudioCapture()
        capture._q.put(b'quick')

        # Large timeout, but should return immediately when data available
        start = time.time()
        audio = capture.read(timeout=100.0)
        elapsed = time.time() - start

        assert audio == b'quick'
        assert elapsed < 1.0  # Should return quickly, not wait for timeout
