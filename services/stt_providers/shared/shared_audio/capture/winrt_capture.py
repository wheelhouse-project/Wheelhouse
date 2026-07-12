"""WinRT-based audio capture for STT services.

This module provides audio capture using Windows Runtime (WinRT) AudioGraph,
replacing the PortAudio/sounddevice dependency with native Windows APIs.

GLOSSARY
--------
- **AudioGraph** - WinRT audio processing graph connecting inputs to outputs
- **AudioDeviceInputNode** - Microphone capture node in the graph
- **AudioFrameOutputNode** - Node providing raw PCM frame access
- **MediaCategory.SPEECH** - Audio category optimized for voice capture

OVERVIEW
--------
WinRT AudioGraph provides native Windows audio capture with:

1. **Lower latency** - Direct Windows audio stack integration
2. **Better async** - Native async operations compatible with asyncio
3. **No external deps** - No PortAudio/compiled library needed
4. **Auto-format** - Handles sample rate conversion in hardware

KEY INSIGHTS
------------
1. **Graph lifecycle** - Graph must be created, started, and closed properly.
   Use as context manager or call close() explicitly.

2. **Encoding properties** - AudioEncodingProperties set on AudioGraphSettings
   affect DEVICE format, not internal processing. The AudioGraph always uses
   float32 internally regardless of encoding_properties settings.

3. **Frame format** - AudioFrameOutputNode.get_frame() returns FLOAT32 audio
   samples in range [-1.0, 1.0], NOT the int16 format you might expect.
   CRITICAL: You must convert float32 to int16 before sending to STT services:
       int16_sample = max(-32768, min(32767, int(float32_sample * 32767)))

4. **Buffer extraction** - IMemoryBufferReference requires buffer protocol
   access via memoryview(ref), NOT bytes(ref). The winsdk package implements
   __buffer__ which calls IMemoryBufferByteAccess::GetBuffer internally.

5. **Thread safety** - Graph runs in WinRT thread, frame data safe to read
   from Python thread. Queue provides producer-consumer decoupling.
"""

import logging
import queue
import struct
import threading
import time
from typing import Optional, Callable

from .base import AudioConfig, AudioStats
from ..overflow_monitor import OverflowMonitor, OverflowConfig

logger = logging.getLogger(__name__)


def _is_winrt_available() -> bool:
    """Check if WinRT audio APIs are available."""
    try:
        from winsdk.windows.media.audio import AudioGraph  # noqa: F401
        return True
    except ImportError:
        return False


WINRT_AUDIO_AVAILABLE = _is_winrt_available()


class WinRTAudioCapture:
    """Audio capture using WinRT AudioGraph.

    Provides the same interface as MicrophoneStream for drop-in replacement.
    Uses Windows native audio APIs for microphone capture.

    Args:
        config: Audio configuration (rate, channels, chunk_ms).
        overflow_callback: Optional callback when queue overflows.

    Example:
        ```python
        config = AudioConfig(rate=16000, chunk_ms=30)
        capture = WinRTAudioCapture(config)
        capture.start()

        while running:
            audio = capture.read(timeout=1.0)
            if audio:
                process(audio)

        capture.stop()
        ```
    """

    def __init__(
        self,
        config: Optional[AudioConfig] = None,
        overflow_callback: Optional[Callable] = None
    ):
        """Initialize WinRT audio capture.

        Args:
            config: Audio configuration. Defaults to 16kHz mono 30ms.
            overflow_callback: Called when audio queue overflows.
        """
        self.config = config or AudioConfig()
        self.overflow_callback = overflow_callback

        # WinRT objects (created on start)
        self._graph = None
        self._mic_node = None
        self._frame_output = None

        # Threading
        self._capture_thread: Optional[threading.Thread] = None
        self._running = False
        self._q: queue.Queue = queue.Queue(maxsize=100)

        # Statistics
        self._frames_captured = 0
        self._drops = 0
        self._max_queue_depth = 0
        self._start_time: Optional[float] = None

        # Initialize overflow monitoring (matches MicrophoneStream interface)
        overflow_config = OverflowConfig(
            overflow_threshold=5,
            window_seconds=30.0,
            restart_cooldown_seconds=60.0,
            max_restart_attempts=3,
            stable_reset_seconds=300.0
        )
        self.overflow_monitor = OverflowMonitor(overflow_config, self.overflow_callback)

    def start(self) -> None:
        """Start audio capture.

        Creates WinRT AudioGraph and begins capturing to internal queue.
        """
        if self._running:
            return

        if not WINRT_AUDIO_AVAILABLE:
            raise RuntimeError("WinRT audio APIs not available")

        self._running = True
        self._start_time = time.time()

        # Start capture in background thread
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name="WinRTAudioCapture"
        )
        self._capture_thread.start()

        logger.debug(
            f"WinRT audio started: {self.config.rate}Hz, "
            f"{self.config.channels}ch, {self.config.chunk_ms}ms chunks"
        )

    def stop(self) -> None:
        """Stop audio capture and release resources."""
        if not self._running:
            return

        self._running = False

        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
            self._capture_thread = None

        # Clear queue
        with self._q.mutex:
            self._q.queue.clear()

        logger.debug("WinRT audio stopped")

    def read(self, timeout: float = 1.0) -> Optional[bytes]:
        """Read audio chunk from capture queue.

        Args:
            timeout: Maximum seconds to wait for audio.

        Returns:
            Audio bytes (int16 PCM) or None if timeout.
        """
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_stats(self) -> AudioStats:
        """Get capture statistics."""
        return {
            'captured': self._frames_captured,
            'drops': self._drops,
            'qsize': self._q.qsize(),
            'max_q': self._max_queue_depth,
        }

    def get_queue_size(self) -> int:
        """Get current queue depth."""
        return self._q.qsize()

    def reset_overflow_monitor(self) -> None:
        """Reset overflow monitoring state after restart."""
        self.overflow_monitor.reset_for_restart()

    def get_overflow_status(self) -> dict:
        """Get current overflow monitoring status for debugging."""
        return self.overflow_monitor.get_status()

    def list_audio_devices(self) -> list:
        """List available audio input devices.

        Uses WinRT device enumeration to find audio capture devices.

        Returns:
            List of dicts with device info: index, name, rate, channels
        """
        if not WINRT_AUDIO_AVAILABLE:
            return []

        try:
            from winsdk.windows.devices.enumeration import DeviceInformation, DeviceClass

            # Try to import winrt_helpers from wheelhouse
            # This is optional - if not available, we'll skip device enumeration
            try:
                import sys
                import os
                # Look for wheelhouse in common locations
                possible_paths = [
                    os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'wheelhouse')),
                    os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'wheelhouse')),
                ]
                for path in possible_paths:
                    if os.path.exists(path) and path not in sys.path:
                        sys.path.insert(0, path)

                from utils.winrt_helpers import run_winrt_sync

                # Find audio capture devices
                devices = run_winrt_sync(
                    DeviceInformation.find_all_async(DeviceClass.AUDIO_CAPTURE),
                    timeout=5.0
                )

                result = []
                for i, device in enumerate(devices):
                    result.append({
                        'index': i,
                        'name': device.name,
                        'rate': 16000,  # Default, WinRT handles conversion
                        'channels': 1,
                    })
                return result

            except ImportError:
                logger.warning("winrt_helpers not available, cannot enumerate devices")
                return []

        except Exception as e:
            logger.warning(f"Failed to enumerate audio devices: {e}")
            return []

    def _capture_loop(self) -> None:
        """Background thread for audio capture.

        Creates WinRT graph and polls for frames, converting to bytes
        and queuing for consumption by read().
        """
        try:
            self._setup_graph()
            self._poll_frames()
        except Exception as e:
            logger.error(f"WinRT capture error: {e}")
        finally:
            self._cleanup_graph()

    def _setup_graph(self) -> None:
        """Create and configure WinRT AudioGraph."""
        # Import here to avoid import errors when WinRT not available
        from winsdk.windows.media.audio import AudioGraph, AudioGraphSettings
        from winsdk.windows.media.render import AudioRenderCategory
        from winsdk.windows.media.mediaproperties import AudioEncodingProperties
        from winsdk.windows.media.capture import MediaCategory

        # Try to import winrt_helpers
        import sys
        import os
        possible_paths = [
            os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'wheelhouse')),
            os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'wheelhouse')),
        ]
        for path in possible_paths:
            if os.path.exists(path) and path not in sys.path:
                sys.path.insert(0, path)

        from utils.winrt_helpers import run_winrt_sync

        # Create encoding properties for STT
        props = AudioEncodingProperties.create_pcm(
            self.config.rate,
            self.config.channels,
            16  # bits per sample
        )

        # Create graph settings
        settings = AudioGraphSettings(AudioRenderCategory.SPEECH)
        settings.encoding_properties = props

        # Create graph
        result = run_winrt_sync(AudioGraph.create_async(settings), timeout=5.0)
        if result.status != 0:
            raise RuntimeError(f"AudioGraph creation failed: status={result.status}")

        self._graph = result.graph

        # Create microphone input
        input_result = run_winrt_sync(
            self._graph.create_device_input_node_async(MediaCategory.SPEECH),
            timeout=5.0
        )
        if input_result.status != 0:
            raise RuntimeError(f"Microphone node failed: status={input_result.status}")

        self._mic_node = input_result.device_input_node

        # Create frame output
        self._frame_output = self._graph.create_frame_output_node(props)

        # Connect mic to output
        self._mic_node.add_outgoing_connection(self._frame_output)

        # Start the graph
        self._graph.start()

        logger.debug("WinRT AudioGraph started")

    def _poll_frames(self) -> None:
        """Poll for audio frames and queue them.

        KEY INSIGHT: AudioGraph ALWAYS outputs float32 audio internally, regardless of
        the encoding properties set on AudioGraphSettings. The encoding_properties setting
        affects device/render format, not internal processing. We must convert to int16.
        """
        from winsdk.windows.media import AudioBufferAccessMode

        # Calculate poll interval based on chunk size
        # Poll at 2x the chunk rate to avoid missing data
        poll_interval = self.config.chunk_ms / 1000.0 / 2
        target_bytes = self.config.bytes_per_chunk

        buffer_accumulator = bytearray()

        while self._running:
            try:
                frame = self._frame_output.get_frame()
                if frame:
                    # Extract bytes from frame using buffer protocol
                    audio_buffer = frame.lock_buffer(AudioBufferAccessMode.READ)
                    try:
                        ref = audio_buffer.create_reference()
                        # IMemoryBufferReference supports Python buffer protocol
                        # Use memoryview to access the underlying byte data
                        float32_data = bytes(memoryview(ref))

                        # AudioGraph outputs float32 (-1.0 to 1.0), convert to int16
                        # Each float32 is 4 bytes, each int16 is 2 bytes
                        if len(float32_data) >= 4:
                            num_samples = len(float32_data) // 4
                            float_samples = struct.unpack('<' + 'f' * num_samples, float32_data[:num_samples * 4])
                            int16_samples = tuple(
                                max(-32768, min(32767, int(s * 32767)))
                                for s in float_samples
                            )
                            int16_data = struct.pack('<' + 'h' * len(int16_samples), *int16_samples)
                            buffer_accumulator.extend(int16_data)
                    finally:
                        audio_buffer.close()

                    # Yield chunks of target size (in int16 bytes)
                    while len(buffer_accumulator) >= target_bytes:
                        chunk = bytes(buffer_accumulator[:target_bytes])
                        buffer_accumulator = buffer_accumulator[target_bytes:]

                        self._frames_captured += 1

                        try:
                            self._q.put_nowait(chunk)
                            qsize = self._q.qsize()
                            if qsize > self._max_queue_depth:
                                self._max_queue_depth = qsize
                        except queue.Full:
                            self._drops += 1
                            # Report overflow to monitor (may trigger restart)
                            self.overflow_monitor.report_overflow()

                time.sleep(poll_interval)

            except Exception as e:
                if self._running:
                    logger.warning(f"Frame poll error: {e}")
                    time.sleep(0.1)

    def _cleanup_graph(self) -> None:
        """Clean up WinRT resources."""
        try:
            if self._graph:
                self._graph.stop()
                self._graph.close()
                self._graph = None
                self._mic_node = None
                self._frame_output = None
        except Exception as e:
            logger.warning(f"Graph cleanup error: {e}")
