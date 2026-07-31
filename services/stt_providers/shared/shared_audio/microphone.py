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
from collections import deque
from typing import Optional, Callable

import sounddevice as sd
import numpy as np

from .diagnostics import LoopStallTracker
from .overflow_monitor import OverflowMonitor, OverflowConfig
from .thread_priority import (
    THREAD_PRIORITY_TIME_CRITICAL,
    elevate_current_thread,
    get_current_thread_priority,
)

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

        # Starvation defenses (wh-sounddevice-starvation-parity). PortAudio
        # owns the callback thread, so the priority check must run inside the
        # callback itself, once per stream start. Measured on the reference
        # machine (MME host API, 2026-07-28): PortAudio already runs this
        # thread at TIME_CRITICAL, so elevation is conditional -- it fires
        # only when the measured priority is below time-critical, i.e. on a
        # host API that leaves the thread unprotected
        # (wh-sounddevice-starvation-parity.3.1). The stall tracker measures
        # gaps between callback invocations -- a gap means PortAudio could not
        # deliver audio on time, the direct signature behind "input overflow".
        # The callback never logs; it appends log-ready lines here and read()
        # (the consumer thread) drains and emits them
        # (wh-sounddevice-starvation-parity.3.2). A bounded deque, not a
        # single slot (wh-sounddevice-starvation-parity.3.5): append and
        # popleft are atomic under the GIL and non-blocking, the one-by-one
        # drain cannot erase a concurrently appended entry, and maxlen bounds
        # memory if the consumer never reads. start() leaves it alone, so
        # diagnostics from a prior stream still get logged after a restart.
        self._callback_priority_checked = False
        self._callback_priority: Optional[int] = None
        self._callback_elevated: Optional[bool] = None
        self._pending_log_msgs: deque = deque(maxlen=8)
        self.stall_tracker = LoopStallTracker(label="capture callback")

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
        if not self._callback_priority_checked:
            # Elevate only when the host API leaves this thread below
            # time-critical (MME arrives pre-elevated; measured 2026-07-28).
            # An unreadable priority (None) also skips elevation -- never
            # blind-fire SetThreadPriority over unknown host scheduling --
            # and is reported as unavailable, not as host-managed
            # (wh-sounddevice-starvation-parity.3.4).
            self._callback_priority_checked = True
            prio = get_current_thread_priority()
            self._callback_priority = prio
            if prio is None:
                self._pending_log_msgs.append(
                    "[mic] Callback thread priority unavailable; "
                    "elevation skipped")
            elif prio < THREAD_PRIORITY_TIME_CRITICAL:
                self._callback_elevated = elevate_current_thread('time_critical')
                self._pending_log_msgs.append(
                    f"[mic] Callback thread priority was {prio}; "
                    f"elevated to time-critical: {self._callback_elevated}")
            else:
                self._pending_log_msgs.append(
                    f"[mic] Callback thread priority: {prio} "
                    f"(host-managed; no elevation needed)")

        # qsize is passed as a callable: it costs a mutex acquisition, so the
        # tracker evaluates it only when a rate-limited stall message forms.
        stall_msg = self.stall_tracker.record(self._q.qsize)
        if stall_msg:
            self._pending_log_msgs.append(stall_msg)

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

        # Each start makes PortAudio create a fresh callback thread, so the
        # priority check must run again from inside it. The stop()-to-start()
        # pause is intentional and must not be counted as a scheduling stall.
        # _pending_log_msgs is deliberately NOT cleared: unread diagnostics
        # from the prior stream still get logged on the next read()
        # (wh-sounddevice-starvation-parity.3.5).
        self._callback_priority_checked = False
        self._callback_priority = None
        self._callback_elevated = None
        self.stall_tracker.reset()

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
        # Log lines composed by the audio callback are emitted here, on the
        # consumer thread, so the callback never takes logging-handler locks
        # (wh-sounddevice-starvation-parity.3.2). Draining one entry at a
        # time means a line the callback appends mid-drain is logged on this
        # or the next pass, never erased.
        while True:
            try:
                msg = self._pending_log_msgs.popleft()
            except IndexError:
                break
            logger.info(msg)
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
