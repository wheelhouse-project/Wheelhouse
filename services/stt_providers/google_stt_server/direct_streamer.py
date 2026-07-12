"""Google Cloud Speech-to-Text streaming API wrapper.

This module provides a simplified, thread-safe interface to Google Cloud's
Speech-to-Text streaming API. It manages the complex streaming connection lifecycle,
handles audio data queuing, and provides synchronous access to asynchronous 
transcription responses. The streamer supports various configuration options
including custom models, phrase hints, and automatic punctuation.

Key Classes:
  - GoogleDirectStreamer: Main streaming client for Google STT API.

Key Methods:
  - send_audio: Queues audio data for transcription.
  - close: Gracefully closes the streaming connection.
  - responses: Generator that yields transcription responses.

Typical Usage:
  from direct_streamer import GoogleDirectStreamer
  
  streamer = GoogleDirectStreamer(
      language="en-US", 
      model="latest_short",
      sample_rate=16000
  )
  
  # Send audio data
  streamer.send_audio(audio_bytes)
  
  # Process responses
  for response in streamer.responses():
      # Handle transcription response
      process_response(response)
"""
import time
import queue
import threading
import inspect
import collections
from typing import Callable, Optional, TYPE_CHECKING
import importlib

import logging

from shared_stt.redact import redact_transcript

logger = logging.getLogger("GoogleSTT")


def _describe_response(resp) -> str:
    """Metadata-preserving description of a StreamingRecognizeResponse.

    Keeps is_final / stability / result_end_time -- the fields the
    log_stream_responses debug flag exists for -- and redacts only the
    transcript text (wh-797.17.3).
    """
    parts = []
    for r in resp.results:
        transcript = r.alternatives[0].transcript if r.alternatives else ""
        parts.append(
            f"(is_final={r.is_final} stability={r.stability:.2f} "
            f"end={r.result_end_time} transcript='{redact_transcript(transcript)}')"
        )
    return f"results=[{', '.join(parts)}]"

if TYPE_CHECKING:  # Hint types to the checker without importing at runtime
    from google.cloud.speech_v1 import SpeechClient  # type: ignore
    from google.cloud.speech_v1.types import RecognitionConfig, SpeechContext, StreamingRecognitionConfig, StreamingRecognizeRequest, StreamingRecognizeResponse  # type: ignore
    from .config_loader import DebugConfig


class GoogleDirectStreamer:
    """
    A simplified, thread-safe wrapper that manages a streaming connection to
    the Google STT API. It acts as a conduit, queueing raw responses from the
    API to be consumed by a synchronous client.
    """

    def __init__(self,
                 language: str = "en-US",
                 model: str = "latest_long",
                 sample_rate: int = 16000,
                 enable_auto_punct: bool = True,
                 phrase_hints: list[str] | None = None,
                 phrase_hints_boost: float | None = None,
                 class_tokens: list[str] | None = None,
                 client: Optional["SpeechClient"] = None,
                 debug_cfg: Optional["DebugConfig"] = None,
                 keepalive_gap: float = 1.2,
                 single_utterance: bool = False,
                 transcription_enabled_event: Optional[threading.Event] = None) -> None:
        # Configuration
        self.language = language
        self.model = model
        self.sample_rate = sample_rate
        self.enable_auto_punct = enable_auto_punct
        self.phrase_hints = phrase_hints or []
        self.phrase_hints_boost = phrase_hints_boost
        self.class_tokens = class_tokens or []
        
        if client is None:
            _mod = importlib.import_module('google.cloud.speech_v1')
            _SpeechClient = getattr(_mod, 'SpeechClient')
            self._client: "SpeechClient" = _SpeechClient()
        else:
            self._client = client
        
        if debug_cfg is None:
            from .config_loader import DebugConfig
            self._debug = DebugConfig()
        else:
            self._debug = debug_cfg

        self._keepalive_gap = keepalive_gap
        self._single_utterance = single_utterance
        self.transcription_enabled_event = transcription_enabled_event

        # Runtime state
        self._response_q: queue.Queue = queue.Queue(maxsize=400)
        self._audio_q: queue.Queue = queue.Queue(maxsize=800)
        self._stream_thread: Optional[threading.Thread] = None
        self._closed = True
        self._start_time: Optional[float] = None
        self._frames_enqueued = 0
        self._frames_sent = 0
        self._last_frame_time = 0.0
        self._restart_request = threading.Event()

    def start(self):
        """Starts the streamer, creating the background threads for I/O."""
        if not self._closed:
            return
        self._closed = False
        self._start_time = time.time()
        self._last_frame_time = self._start_time
        if self._debug.log_lifecycle:
            pass
        
        self._stream_thread = threading.Thread(target=self._stream_runner, daemon=True)
        self._stream_thread.start()

    def _stream_runner(self):
        """The main loop for the stream thread, handles reconnection."""
        while not self._closed:
            # If transcription is disabled, wait here before attempting to connect.
            if self.transcription_enabled_event and not self.transcription_enabled_event.is_set():
                if self._debug.log_lifecycle: pass
                self.transcription_enabled_event.wait()
                if self._debug.log_lifecycle: pass
                # After being re-enabled, just loop back to the top to re-evaluate.
                continue

            self._restart_request.clear()
            self._run_recognize_session()
            
            # If we are here, the stream ended.
            # If it was an unexpected end (not a planned restart) and we are not closing,
            # add a small delay to prevent rapid-fire reconnection attempts.
            if not self._closed and not self._restart_request.is_set():
                if self._debug.log_lifecycle: pass
                time.sleep(0.5)

    def _run_recognize_session(self):
        """
        :flow: STT Transcription
        :step: 5
        :description: This function makes the actual `streaming_recognize` call to the Google Cloud Speech-to-Text API. It uses a generator to feed audio data and receives transcription responses.
        :data_in: An iterator of `StreamingRecognizeRequest` objects containing audio data.
        :data_out: An iterator of `StreamingRecognizeResponse` objects from the API.
        """
        if self._closed:
            return
        
        _types = importlib.import_module('google.cloud.speech_v1.types')
        RecognitionConfig = getattr(_types, 'RecognitionConfig')
        SpeechContext = getattr(_types, 'SpeechContext')
        StreamingRecognitionConfig = getattr(_types, 'StreamingRecognitionConfig')
        StreamingRecognizeRequest = getattr(_types, 'StreamingRecognizeRequest')
        
        config = RecognitionConfig(
            encoding=RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=self.sample_rate,
            language_code=self.language,
            model=self.model,
            enable_automatic_punctuation=self.enable_auto_punct,
        )
        all_phrases = self.phrase_hints + self.class_tokens
        if all_phrases:
            sc = SpeechContext(phrases=all_phrases, boost=self.phrase_hints_boost or 0)
            config.speech_contexts.append(sc)
            
        streaming_config = StreamingRecognitionConfig(
            config=config,
            interim_results=True,
            single_utterance=self._single_utterance,
            enable_voice_activity_events=True,
        )

        def _iter_bridge():
            """A generator that bridges the audio queue to the gRPC request iterator."""
            keepalive_silence = b"\x00" * int(self.sample_rate * 0.03) * 2
            while not self._closed and not self._restart_request.is_set():
                # If transcription is disabled, block here until it's re-enabled.
                if self.transcription_enabled_event and not self.transcription_enabled_event.is_set():
                    if self._debug.log_lifecycle: pass
                    self.transcription_enabled_event.wait() # Block until enabled
                    if self._debug.log_lifecycle: pass

                try:
                    # Now that we know we're enabled, try to get a chunk.
                    chunk = self._audio_q.get(timeout=0.4)
                    if chunk is None: # Sentinel value
                        if self._debug.log_lifecycle: pass
                        break
                    self._frames_sent += 1
                    self._last_frame_time = time.time()
                    yield StreamingRecognizeRequest(audio_content=chunk)
                except queue.Empty:
                    # If the queue is empty, send a keepalive to prevent the stream from closing.
                    if (time.time() - self._last_frame_time) >= self._keepalive_gap:
                        self._frames_sent += 1
                        self._last_frame_time = time.time()
                        if self._debug.log_lifecycle: pass
                        yield StreamingRecognizeRequest(audio_content=keepalive_silence)
                except Exception as e:
                    if self._debug.log_lifecycle: pass
                    break
            if self._restart_request.is_set() and self._debug.log_lifecycle:
                pass

        try:
            if self._debug.log_lifecycle: pass
            
            responses = self._client.streaming_recognize(streaming_config, _iter_bridge())
            
            if self._debug.log_lifecycle: pass
            
            for resp in responses:
                if self._debug.log_stream_responses:
                    logger.debug("[g-stream-raw] Received response from Google: %s", _describe_response(resp))
                
                if self._closed:
                    break
                
                try:
                    self._response_q.put(resp, timeout=0.2)
                    # Check if this response is final and trigger a restart
                    if any(r.is_final for r in resp.results):
                        if self._debug.log_lifecycle: pass
                        self._restart_request.set()
                        # The iterator bridge will see this and stop, ending the `responses` loop.
                except queue.Full:
                    if self._debug.log_lifecycle: pass
                    break # Exit if the consumer is stuck
        
        except Exception as e:
            if self._debug.log_lifecycle: pass
        
        finally:
            # This session is over. The outer loop in _stream_runner will decide whether to restart.
            if not self._restart_request.is_set():
                # If not a planned restart, maybe an error, so signal end to consumer.
                try: self._response_q.put(None, timeout=0.2)
                except Exception: pass

    def send_audio(self, pcm_bytes: bytes):
        """
        :flow: STT Transcription
        :step: 3
        :description: Receives a chunk of audio data confirmed to be speech and places it into an internal queue for the streaming thread to process.
        :data_in: A bytes object containing a chunk of speech audio.
        :data_out: The same bytes object, placed into the `_audio_q` queue.
        """
        if self._closed: return

        # Do not enqueue audio if transcription is disabled
        if self.transcription_enabled_event and not self.transcription_enabled_event.is_set():
            return

        try:
            self._audio_q.put_nowait(pcm_bytes)
            self._frames_enqueued += 1
        except queue.Full:
            if self._debug.log_lifecycle: pass

    def restart_stream(self):
        """Externally signals the stream to restart its recognition session."""
        if not self._closed and not self._restart_request.is_set():
            if self._debug.log_lifecycle: pass
            self._restart_request.set()

    def get_response_non_blocking(self):
        """Pulls a single raw response from the queue without blocking."""
        if self._response_q is None:
            raise queue.Empty()
        return self._response_q.get_nowait()

    def purge_responses(self):
        """Clears all pending responses from the queue."""
        if self._response_q is None: return
        count = 0
        while not self._response_q.empty():
            try:
                self._response_q.get_nowait()
                count += 1
            except queue.Empty:
                break
        if self._debug.log_lifecycle and count > 0:
            pass

    def finish(self):
        """Signals the streamer to shut down gracefully and non-blockingly."""
        if self._closed:
            return
        
        self._closed = True
        self._restart_request.set() # Ensure the stream runner loop exits
        try:
            # Send sentinel to unblock the audio queue if it's waiting
            self._audio_q.put(None, timeout=0.1)
        except Exception:
            pass
        
        if self._stream_thread:
            self._stream_thread.join(timeout=0.5)

        # Clear queues
        self._audio_q = queue.Queue()
        self._response_q = queue.Queue()

    @property
    def elapsed(self) -> float:
        if not self._start_time: return 0.0
        return time.time() - self._start_time

    @property
    def active(self) -> bool:
        return not self._closed

    @property
    def diagnostics(self) -> dict:
        """Return diagnostic information for overflow analysis."""
        return {
            'audio_q_size': self._audio_q.qsize(),
            'response_q_size': self._response_q.qsize(),
            'frames_enqueued': self._frames_enqueued,
            'frames_sent': self._frames_sent,
        }

    def _audio_generator(self):
        """
        :flow: STT Transcription
        :step: 4
        :description: A generator that yields audio chunks from the internal queue to be sent to the Google STT API. This bridges the gap between the synchronous `send_audio` method and the asynchronous streaming API.
        :data_in: Audio chunks from the `_audio_q` queue.
        :data_out: A generator yielding audio chunks.
        """
        # Use a deque to buffer audio chunks. This is more efficient than
        # a list for appends and pops from the left.
        audio_buffer = collections.deque()
        
        while not self._closed:
            # If transcription is disabled, clear the buffer and wait.
            if self.transcription_enabled_event and not self.transcription_enabled_event.is_set():
                if audio_buffer:
                    audio_buffer.clear()
                # Clear the queue as well to prevent buildup
                while not self._audio_q.empty():
                    try:
                        self._audio_q.get_nowait()
                    except queue.Empty:
                        break
                time.sleep(0.1) # Wait before checking again
                continue

            try:
                # Non-blocking get from the queue
                chunk = self._audio_q.get_nowait()
                audio_buffer.append(chunk)
            except queue.Empty:
                # If the buffer is empty and the queue is empty, we might need to wait.
                if not audio_buffer:
                    time.sleep(0.05)
                    continue

            if audio_buffer:
                yield audio_buffer.popleft()