"""
Audio capture with async streaming interface and optional VAD filtering.

:flow: AudioCapture -> VAD filter -> async audio_stream -> STTProvider
:flow: Wraps sounddevice for real-time microphone input.

This module provides a unified audio capture interface for all STT providers.
Audio is captured via sounddevice (PortAudio backend) and exposed as an async
iterator of audio chunks. When VAD is enabled, silent chunks are filtered.
"""

import asyncio
import logging
import queue
import time
from collections import deque
from typing import AsyncIterator, TYPE_CHECKING

import sounddevice as sd
import numpy as np

if TYPE_CHECKING:
    from .vad import SileroVAD

logger = logging.getLogger(__name__)


class AudioCapture:
    """Async audio capture from microphone.

    Provides an async iterator interface for streaming audio chunks to
    STT providers. Uses sounddevice for cross-platform audio capture.

    :flow: Captures audio via sounddevice callback.
    :flow: Queues chunks for async consumption.
    :flow: STTProvider.transcribe_stream() consumes via stream().

    Example:
        capture = AudioCapture()
        await capture.start()

        async for chunk in capture.stream():
            # Process audio chunk (16-bit PCM, 16kHz)
            ...

        await capture.stop()
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        chunk_ms: int = 30,
        device: int | str | None = None,
        queue_max_size: int = 100,
        vad_enabled: bool = False,
        vad_threshold: float = 0.5,
        vad_lead_in_chunks: int = 10,  # ~300ms buffer before speech detection
    ):
        """Initialize audio capture.

        Args:
            sample_rate: Sample rate in Hz (default 16000 for STT).
            channels: Number of audio channels (default 1 for mono).
            chunk_ms: Chunk duration in milliseconds (default 30ms).
            device: Audio device index, name, or None for default.
            queue_max_size: Max chunks to buffer before dropping.
            vad_enabled: Enable Voice Activity Detection filtering.
            vad_threshold: VAD confidence threshold (0.0-1.0).
            vad_lead_in_chunks: Number of chunks to buffer before speech.
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_ms = chunk_ms
        self.chunk_size = int(sample_rate * chunk_ms / 1000)
        self.device = device
        self.queue_max_size = queue_max_size
        
        # VAD configuration
        self._vad_enabled = vad_enabled
        self._vad_threshold = vad_threshold
        self._vad_lead_in_chunks = vad_lead_in_chunks
        # Hold-over: continue yielding N chunks after VAD reports silence
        # This prevents cutting off during natural pauses between phrases
        self._vad_speech_hold_chunks = 25  # ~750ms at 30ms/chunk
        self._vad: "SileroVAD | None" = None

        self._stream: sd.InputStream | None = None
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=queue_max_size)
        self._running = False

        # Stats
        self._frames_captured = 0
        self._frames_dropped = 0
        self._frames_vad_filtered = 0
        self._start_time: float | None = None

    async def start(self) -> None:
        """Start audio capture.

        Raises:
            AudioCaptureError: If audio device cannot be opened.
        """
        if self._running:
            return

        # Initialize VAD if enabled
        if self._vad_enabled and self._vad is None:
            try:
                from .vad import SileroVAD
                self._vad = SileroVAD(
                    threshold=self._vad_threshold,
                    sample_rate=self.sample_rate,
                )
                logger.info(f"VAD enabled with threshold={self._vad_threshold}")
            except ImportError as e:
                logger.warning(f"VAD requested but pysilero-vad not available: {e}")
                self._vad_enabled = False

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                blocksize=self.chunk_size,
                device=self.device,
                dtype="int16",
                callback=self._audio_callback,
            )
            self._stream.start()
            self._running = True
            self._start_time = time.time()
            self._frames_captured = 0
            self._frames_dropped = 0
            self._frames_vad_filtered = 0

            logger.info(
                f"AudioCapture started: {self.sample_rate}Hz, "
                f"{self.chunk_ms}ms chunks, device={self.device or 'default'}, "
                f"vad={self._vad_enabled}"
            )

        except Exception as e:
            raise AudioCaptureError(f"Failed to start audio capture: {e}") from e

    async def stop(self) -> None:
        """Stop audio capture and release resources."""
        self._running = False

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        # Reset VAD state
        if self._vad:
            self._vad.reset()

        # Clear any remaining queued audio
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        elapsed = time.time() - self._start_time if self._start_time else 0
        vad_info = f", {self._frames_vad_filtered} VAD-filtered" if self._vad_enabled else ""
        logger.info(
            f"AudioCapture stopped: {self._frames_captured} frames captured, "
            f"{self._frames_dropped} dropped{vad_info}, {elapsed:.1f}s elapsed"
        )

    async def stream(self) -> AsyncIterator[bytes]:
        """Async iterator yielding audio chunks.

        Yields:
            bytes: Audio chunks as 16-bit PCM data.

        When VAD is enabled, only yields chunks containing speech.
        Lead-in buffer captures audio context before speech is detected.
        
        The iterator runs until stop() is called.
        """
        # Lead-in buffer holds recent chunks before speech detection
        lead_in_buffer: deque[bytes] = deque(maxlen=self._vad_lead_in_chunks)
        in_speech = False
        
        # Diagnostic counters
        chunks_received = 0
        chunks_yielded = 0
        speech_transitions = 0
        silence_count = 0  # Tracks consecutive silent chunks during speech hold-over
        
        logger.debug(f"AudioCapture.stream started, vad_enabled={self._vad_enabled}")
        
        while self._running:
            try:
                # Use small timeout to allow checking _running flag
                chunk = await asyncio.to_thread(
                    self._queue.get, timeout=0.1
                )
                chunks_received += 1
                
                # If VAD disabled, yield all chunks
                if not self._vad_enabled or self._vad is None:
                    chunks_yielded += 1
                    yield chunk
                    continue
                
                # Check if chunk contains speech
                is_speech = self._vad.is_speech(chunk)
                
                if is_speech:
                    # Reset hold-over counter on any speech
                    silence_count = 0
                    
                    if not in_speech:
                        # Transition to speech - flush lead-in buffer
                        in_speech = True
                        speech_transitions += 1
                        logger.debug(f"VAD: Speech detected (transition #{speech_transitions}), flushing {len(lead_in_buffer)} lead-in chunks")
                        while lead_in_buffer:
                            chunks_yielded += 1
                            yield lead_in_buffer.popleft()
                    chunks_yielded += 1
                    yield chunk
                else:
                    if in_speech:
                        # Silence detected while in speech - use hold-over
                        silence_count += 1
                        chunks_yielded += 1
                        yield chunk  # Always yield during hold-over
                        
                        if silence_count >= self._vad_speech_hold_chunks:
                            # Hold-over expired - end speech
                            in_speech = False
                            logger.debug(f"VAD: Speech ended after {silence_count} silent chunks (total yielded: {chunks_yielded})")
                    else:
                        # No speech - buffer for lead-in, track filtered
                        lead_in_buffer.append(chunk)
                        self._frames_vad_filtered += 1
                
                # Periodic stats
                if chunks_received % 500 == 0:
                    logger.debug(f"AudioCapture stats: {chunks_received} received, {chunks_yielded} yielded, {self._frames_vad_filtered} filtered")
                
            except queue.Empty:
                continue
        
        logger.debug(f"AudioCapture.stream ended: {chunks_received} received, {chunks_yielded} yielded, {speech_transitions} speech transitions")

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        """Sounddevice callback (runs in separate thread).

        :flow: PortAudio thread -> queue -> async stream()
        """
        if status:
            logger.warning(f"Audio status: {status}")

        self._frames_captured += 1

        try:
            self._queue.put_nowait(indata.tobytes())
        except queue.Full:
            self._frames_dropped += 1

    def get_stats(self) -> dict:
        """Get capture statistics."""
        return {
            "frames_captured": self._frames_captured,
            "frames_dropped": self._frames_dropped,
            "frames_vad_filtered": self._frames_vad_filtered,
            "vad_enabled": self._vad_enabled,
            "queue_size": self._queue.qsize(),
            "running": self._running,
        }

    @staticmethod
    def list_devices() -> list[dict]:
        """List available audio input devices.

        Returns:
            List of dicts with device info (index, name, channels, sample_rate).
        """
        devices = []
        try:
            all_devices = sd.query_devices()
            if isinstance(all_devices, dict):
                all_devices = [all_devices]

            for i, dev in enumerate(all_devices):
                if dev.get("max_input_channels", 0) > 0:
                    devices.append({
                        "index": i,
                        "name": dev.get("name", "Unknown"),
                        "channels": dev.get("max_input_channels", 0),
                        "sample_rate": int(dev.get("default_samplerate", 0)),
                    })
        except Exception as e:
            logger.error(f"Error listing audio devices: {e}")

        return devices


class AudioCaptureError(Exception):
    """Raised when audio capture fails."""
