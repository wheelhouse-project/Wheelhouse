"""
Google Cloud Speech-to-Text Provider - cloud-based, high-quality speech recognition.

:flow: STTManager -> GoogleSTTProvider -> Google Cloud Speech API -> TranscriptEvent
:flow: Uses gRPC streaming for real-time transcription.

Google Cloud STT provides:
- High accuracy with neural network models
- Real-time streaming transcription
- Phrase hints/boost words for domain-specific vocabulary
- Multiple language support
- Automatic punctuation

This is a simpler in-process implementation. For the full-featured server
with stability processing and overlay mode, see google_stt_server/.
"""

import asyncio
import logging

from utils.redact import redact_transcript
import queue
import threading
from typing import AsyncIterator

from stt.base import (
    STTProvider,
    TranscriptEvent,
    ProviderCapabilities,
    TranscriptCallback,
    STTProviderNotAvailableError,
    STTProviderStartError,
    STTProviderError,
)

logger = logging.getLogger(__name__)

# Check for google-cloud-speech at import time
try:
    from google.cloud import speech_v1
    from google.cloud.speech_v1 import types as speech_types
    GOOGLE_SPEECH_AVAILABLE = True
except ImportError:
    GOOGLE_SPEECH_AVAILABLE = False
    speech_v1 = None
    speech_types = None


class GoogleSTTProvider(STTProvider):
    """Google Cloud Speech-to-Text provider.

    Uses the Google Cloud Speech streaming API for real-time transcription.
    Requires GOOGLE_APPLICATION_CREDENTIALS environment variable to be set.

    :flow: Receives audio chunks from AudioCapture.
    :flow: Streams to Google Cloud Speech API via gRPC.
    :flow: Emits TranscriptEvent for interim and final results.
    """

    def __init__(
        self,
        language: str = "en-US",
        model: str = "latest_long",
        sample_rate: int = 16000,
        enable_punctuation: bool = True,
        boost_words: list[str] | None = None,
        boost_value: float = 10.0,
    ):
        """Initialize Google STT provider.

        Args:
            language: Language code (e.g., "en-US", "es-ES").
            model: Recognition model ("latest_long", "latest_short", etc.).
            sample_rate: Audio sample rate in Hz (default 16000).
            enable_punctuation: Enable automatic punctuation.
            boost_words: Initial list of words to boost recognition for.
            boost_value: Boost strength for phrase hints (0-20).
        """
        self.language = language
        self.model = model
        self.sample_rate = sample_rate
        self.enable_punctuation = enable_punctuation
        self._boost_words = list(boost_words or [])
        self._boost_value = boost_value

        # Runtime state
        self._client = None
        self._running = False
        self._utterance_id = 0

        # Thread communication for streaming
        self._audio_queue: queue.Queue | None = None
        self._response_queue: queue.Queue | None = None
        self._stream_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

    @property
    def name(self) -> str:
        return "google"

    @property
    def is_available(self) -> bool:
        """Check if Google Cloud Speech is available."""
        if not GOOGLE_SPEECH_AVAILABLE:
            return False
        # Check for credentials
        import os
        return bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            boost_list=True,  # Supports phrase hints
            offline=False,  # Requires internet
            hot_reload_config=True,  # Can update boost words dynamically
            languages=["en-US", "en-GB", "es-ES", "fr-FR", "de-DE", "auto"],
        )

    async def start(self) -> None:
        """Initialize Google Cloud Speech client."""
        if not GOOGLE_SPEECH_AVAILABLE:
            raise STTProviderNotAvailableError(
                "google-cloud-speech not installed. Run: pip install google-cloud-speech"
            )

        import os
        if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            raise STTProviderNotAvailableError(
                "GOOGLE_APPLICATION_CREDENTIALS environment variable not set"
            )

        try:
            self._client = speech_v1.SpeechClient()
            self._audio_queue = queue.Queue(maxsize=500)
            self._response_queue = queue.Queue(maxsize=100)
            self._stop_event = threading.Event()
            self._running = True

            logger.info("Google STT provider started")

        except Exception as e:
            raise STTProviderStartError(f"Failed to start Google STT: {e}") from e

    async def stop(self) -> None:
        """Stop the Google STT provider."""
        self._running = False

        if self._stop_event:
            self._stop_event.set()

        if self._audio_queue:
            # Signal stream thread to stop
            try:
                self._audio_queue.put(None, timeout=0.5)
            except queue.Full:
                pass

        if self._stream_thread and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=5.0)

        self._client = None
        self._audio_queue = None
        self._response_queue = None
        self._stream_thread = None

        logger.info("Google STT provider stopped")

    async def transcribe_stream(
        self,
        audio_stream: AsyncIterator[bytes],
        callback: TranscriptCallback,
    ) -> None:
        """Transcribe audio stream using Google Cloud Speech.

        Args:
            audio_stream: Async iterator yielding 16-bit PCM audio chunks.
            callback: Called with each TranscriptEvent.
        """
        if not self._client or not self._running:
            raise STTProviderError("Google STT provider not started")

        # Start streaming thread
        self._stream_thread = threading.Thread(
            target=self._streaming_recognize_thread,
            name="google_stt_stream",
            daemon=True,
        )
        self._stream_thread.start()

        # Feed audio to the stream thread
        async for audio_chunk in audio_stream:
            if not self._running:
                break

            try:
                self._audio_queue.put(audio_chunk, timeout=0.5)
            except queue.Full:
                logger.warning("Audio queue full, dropping chunk")

            # Check for responses
            await self._process_responses(callback)

        # Signal end of audio
        try:
            self._audio_queue.put(None, timeout=0.5)
        except queue.Full:
            pass

        # Process remaining responses
        self._stop_event.set()
        await self._process_responses(callback, drain=True)

    async def _process_responses(
        self,
        callback: TranscriptCallback,
        drain: bool = False,
    ) -> None:
        """Process responses from the streaming thread."""
        timeout = 0.5 if drain else 0.01

        while True:
            try:
                response = self._response_queue.get(timeout=timeout)
                if response is None:
                    break

                # Process the response
                for result in response.results:
                    if not result.alternatives:
                        continue

                    text = result.alternatives[0].transcript.strip()
                    confidence = result.alternatives[0].confidence or 0.9

                    if text:
                        if result.is_final:
                            self._utterance_id += 1

                        await callback(
                            TranscriptEvent(
                                text=text,
                                is_final=result.is_final,
                                utterance_id=self._utterance_id + (0 if result.is_final else 1),
                                confidence=confidence,
                            )
                        )

            except queue.Empty:
                if drain:
                    break
                return

    def _streaming_recognize_thread(self) -> None:
        """Background thread that runs the streaming recognition."""
        try:
            # Build config
            config = speech_types.RecognitionConfig(
                encoding=speech_types.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=self.sample_rate,
                language_code=self.language,
                model=self.model,
                enable_automatic_punctuation=self.enable_punctuation,
            )

            # Add phrase hints if any
            if self._boost_words:
                speech_context = speech_types.SpeechContext(
                    phrases=self._boost_words,
                    boost=self._boost_value,
                )
                config.speech_contexts.append(speech_context)

            streaming_config = speech_types.StreamingRecognitionConfig(
                config=config,
                interim_results=True,
            )

            # Create request generator
            def request_generator():
                # First request with config
                yield speech_types.StreamingRecognizeRequest(
                    streaming_config=streaming_config
                )

                # Subsequent requests with audio
                while not self._stop_event.is_set():
                    try:
                        chunk = self._audio_queue.get(timeout=0.5)
                        if chunk is None:
                            break
                        yield speech_types.StreamingRecognizeRequest(
                            audio_content=chunk
                        )
                    except queue.Empty:
                        continue

            # Make the streaming call
            responses = self._client.streaming_recognize(request_generator())

            for response in responses:
                if self._stop_event.is_set():
                    break

                try:
                    self._response_queue.put(response, timeout=0.5)
                except queue.Full:
                    logger.warning("Response queue full, dropping response")

        except Exception as e:
            logger.exception(f"Error in streaming recognition: {e}")

        finally:
            # Signal end of responses
            try:
                self._response_queue.put(None, timeout=0.5)
            except queue.Full:
                pass

    # Boost word management

    async def add_boost_word(self, word: str, boost: float = 10.0) -> bool:
        """Add a word to the boost list.

        Note: Changes take effect on the next streaming session.
        """
        if word not in self._boost_words:
            self._boost_words.append(word)
            logger.info(f"Added boost word: {redact_transcript(word)}")
        return True

    async def remove_boost_word(self, word: str) -> bool:
        """Remove a word from the boost list."""
        if word in self._boost_words:
            self._boost_words.remove(word)
            logger.info(f"Removed boost word: {redact_transcript(word)}")
            return True
        return False

    async def clear_boost_list(self) -> None:
        """Clear all boost words."""
        self._boost_words.clear()
        logger.info("Cleared boost word list")

    def get_boost_words(self) -> list[str]:
        """Get current boost word list."""
        return list(self._boost_words)
