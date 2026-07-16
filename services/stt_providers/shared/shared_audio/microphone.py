"""Audio capture and streaming from system microphone using sounddevice.

This module handles real-time audio capture from the system's default or specified
microphone device. It provides a queue-based streaming interface that captures
audio in configurable chunks and maintains statistics for monitoring performance.
The audio stream is designed to work with voice activity detection and speech
recognition systems.

Key Classes:
  - MicrophoneStream: Main class for audio capture with configurable parameters.

Key Methods:
  - start: Begins audio capture and streaming.
  - stop: Stops the audio stream and releases resources.
  - list_audio_devices: Enumerates available audio input devices.
  - read: Retrieves captured audio data from the internal queue.

Typical Usage:
  from shared_audio.microphone import MicrophoneStream

  mic = MicrophoneStream(rate=16000, chunk_ms=20)
  mic.start()

  while running:
      audio_data = mic.read()
      # Process audio_data...

  mic.stop()
"""
import logging
import queue
import time
from typing import Optional, Callable

import sounddevice as sd
import numpy as np

from .overflow_monitor import OverflowMonitor, OverflowConfig

logger = logging.getLogger(__name__)


class MicrophoneStream:
    def __init__(self, rate=16000, channels=1, chunk_ms=30, device_index=None, debug_frame_stats: bool = False, overflow_callback: Optional[Callable] = None):
        self.rate = rate
        self.channels = channels
        self.chunk_ms = chunk_ms
        self.chunk_size = int(rate * chunk_ms / 1000)
        self.device_index = device_index
        self.debug_frame_stats = debug_frame_stats
        self.overflow_callback = overflow_callback

        self._stream = None
        # ~10s of audio at 30ms chunks -- matches WinRTAudioCapture: survives
        # consumer stalls from whole-machine CPU saturation without dropping
        # (wh-stt-audio-consumer-behind-realtime).
        self._q = queue.Queue(maxsize=333)

        # Initialize overflow monitoring
        overflow_config = OverflowConfig(
            overflow_threshold=5,  # 5 overflows in window triggers restart
            window_seconds=30.0,   # 30-second tracking window
            restart_cooldown_seconds=60.0,  # 1 minute between restart attempts
            max_restart_attempts=3,  # Max 3 restart attempts
            stable_reset_seconds=300.0  # Reset counter after 5 minutes stable
        )
        self.overflow_monitor = OverflowMonitor(overflow_config, self.overflow_callback)

        # Instrumentation
        self._frames_captured = 0
        self._drops = 0
        self._max_queue_depth = 0
        self._last_stats_log = 0.0
        self._start_time = None

    def list_audio_devices(self):
        try:
            devices_info = sd.query_devices()
            input_devices = []
            # devices_info can be a list or a single dict, normalize to list
            if isinstance(devices_info, dict):
                devices_info = [devices_info]

            for i, device in enumerate(devices_info):
                # The device object is a dictionary
                if device.get('max_input_channels', 0) > 0:
                    input_devices.append({
                        'index': i,
                        'name': device.get('name'),
                        'rate': int(device.get('default_samplerate', 0)),
                        'channels': int(device.get('max_input_channels', 0)),
                    })
            return input_devices
        except Exception as e:
            logger.error(f"[mic] Error listing audio devices: {e}")
            return []

    def _callback(self, indata, frames, time_info, status):
        """
        This is called (from a separate thread) for each audio block.

        :flow: STT Transcription
        :step: 1
        :description: Captures raw microphone audio chunks via sounddevice callback
        :data_in: Raw audio buffer from PortAudio (sounddevice library)
        :data_out: Audio bytes object queued for processing
        :notes: Low-level audio capture callback running in separate PortAudio thread. Converts NumPy audio buffer to bytes and queues for main thread consumption. Monitors for input overflow errors - if overflow threshold exceeded, triggers automatic microphone restart. Frame counter maintained for diagnostics. Queue consumed by read() method which feeds VAD and STT streaming.
        """
        if status:
            logger.warning(f"[mic] PortAudio status: {status}")

            # Check for input overflow and trigger restart if needed
            if "input overflow" in str(status).lower():
                context = {'qsize': self._q.qsize(), 'drops': self._drops}
                if self.overflow_monitor.report_overflow(context):
                    logger.warning("[mic] Overflow threshold exceeded - restart will be triggered")

        self._frames_captured += 1
        if self.debug_frame_stats and self._frames_captured <= 5:
            logger.debug(f"[mic-debug] frame#{self._frames_captured} bytes={len(indata.tobytes())}")

        try:
            # The output of indata is a NumPy array. We convert it to bytes.
            self._q.put_nowait(indata.tobytes())
            qsize = self._q.qsize()
            if qsize > self._max_queue_depth:
                self._max_queue_depth = qsize
        except queue.Full:
            if self.debug_frame_stats:
                logger.debug("[mic-debug] queue_full_drop")
            self._drops += 1

        if self.debug_frame_stats:
            now = time.time()
            if (now - self._last_stats_log) >= 1.0:
                elapsed = now - (self._start_time or now)
                logger.debug(f"[mic-debug] stats captured={self._frames_captured} drops={self._drops} qsize={self._q.qsize()} max_q={self._max_queue_depth} elapsed={elapsed:.1f}s")
                self._last_stats_log = now

    def start(self):
        if self._stream and self._stream.active:
            return

        if self.debug_frame_stats:
            dname = 'default'
            if self.device_index is not None:
                try:
                    dev = sd.query_devices(self.device_index)
                    # query_devices with an index returns a dict
                    if isinstance(dev, dict):
                        dname = dev.get('name', 'unknown')
                except Exception:
                    dname = 'unknown'
            logger.debug(f"[mic-debug] opening device name={dname} idx={self.device_index} rate={self.rate} chunk_size={self.chunk_size}")

        try:
            self._stream = sd.InputStream(
                samplerate=self.rate,
                channels=self.channels,
                blocksize=self.chunk_size,
                device=self.device_index,
                dtype='int16',  # Corresponds to pyaudio.paInt16
                callback=self._callback
            )
            self._stream.start()
            self._start_time = time.time()
        except Exception as e:
            logger.error(f"[mic] Error starting audio stream: {e}")
            self._stream = None

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        # Clear the queue after stopping to avoid processing stale data on restart
        with self._q.mutex:
            self._q.queue.clear()

    def reset_overflow_monitor(self):
        """Reset overflow monitoring state after restart."""
        self.overflow_monitor.reset_for_restart()

    def get_overflow_status(self) -> dict:
        """Get current overflow monitoring status for debugging."""
        return self.overflow_monitor.get_status()

    def read(self, timeout=1.0):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_queue_size(self):
        return self._q.qsize()

    def get_stats_snapshot(self):
        return {
            'captured': self._frames_captured,
            'drops': self._drops,
            'qsize': self._q.qsize(),
            'max_q': self._max_queue_depth,
        }
