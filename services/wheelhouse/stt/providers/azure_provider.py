"""
Azure Speech-to-Text Provider - cloud-based, high-quality speech recognition.

:flow: STTManager -> AzureSTTProvider -> Azure Speech SDK -> TranscriptEvent
:flow: Uses continuous recognition for real-time transcription.

Azure Speech provides:
- High accuracy with neural network models
- Real-time streaming transcription
- Phrase list support for domain-specific vocabulary
- Multiple language support
- Automatic punctuation

Requires Azure Cognitive Services Speech subscription.
"""

import asyncio
import logging

from utils.redact import redact_transcript
import os
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

# Check for Azure Speech SDK at import time
try:
    import azure.cognitiveservices.speech as speechsdk
    AZURE_SPEECH_AVAILABLE = True
except ImportError:
    AZURE_SPEECH_AVAILABLE = False
    speechsdk = None


class AzureSTTProvider(STTProvider):
    """Azure Cognitive Services Speech-to-Text provider.

    Uses the Azure Speech SDK for real-time transcription with
    push stream audio input.

    :flow: Receives audio chunks from AudioCapture.
    :flow: Pushes to Azure Speech SDK via PushAudioInputStream.
    :flow: Emits TranscriptEvent for recognizing and recognized events.
    """

    def __init__(
        self,
        subscription_key: str | None = None,
        region: str = "eastus",
        language: str = "en-US",
        enable_punctuation: bool = True,
        boost_words: list[str] | None = None,
    ):
        """Initialize Azure STT provider.

        Args:
            subscription_key: Azure subscription key (or from env AZURE_SPEECH_KEY).
            region: Azure region (e.g., "eastus", "westus2").
            language: Language code (e.g., "en-US").
            enable_punctuation: Enable automatic punctuation.
            boost_words: Initial list of words to boost recognition for.
        """
        self.subscription_key = subscription_key or os.environ.get("AZURE_SPEECH_KEY", "")
        self.region = region
        self.language = language
        self.enable_punctuation = enable_punctuation
        self._boost_words = list(boost_words or [])

        # Runtime state
        self._speech_config = None
        self._audio_stream = None
        self._audio_config = None
        self._recognizer = None
        self._phrase_list = None
        self._running = False
        self._utterance_id = 0

        # Async event signaling
        self._transcript_queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def name(self) -> str:
        return "azure"

    @property
    def is_available(self) -> bool:
        """Check if Azure Speech SDK is available and configured."""
        if not AZURE_SPEECH_AVAILABLE:
            return False
        return bool(self.subscription_key)

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            boost_list=True,  # Supports phrase list
            offline=False,  # Requires internet
            hot_reload_config=True,  # Can update phrase list dynamically
            languages=["en-US", "en-GB", "es-ES", "fr-FR", "de-DE", "auto"],
        )

    async def start(self) -> None:
        """Initialize Azure Speech recognizer."""
        if not AZURE_SPEECH_AVAILABLE:
            raise STTProviderNotAvailableError(
                "azure-cognitiveservices-speech not installed. "
                "Run: pip install azure-cognitiveservices-speech"
            )

        if not self.subscription_key:
            raise STTProviderNotAvailableError(
                "Azure subscription key not set. Set AZURE_SPEECH_KEY env var "
                "or pass subscription_key parameter."
            )

        try:
            # Create speech config
            self._speech_config = speechsdk.SpeechConfig(
                subscription=self.subscription_key,
                region=self.region,
            )
            self._speech_config.speech_recognition_language = self.language

            if self.enable_punctuation:
                self._speech_config.enable_dictation()

            # Create push stream for audio input
            self._audio_stream = speechsdk.audio.PushAudioInputStream(
                stream_format=speechsdk.audio.AudioStreamFormat(
                    samples_per_second=16000,
                    bits_per_sample=16,
                    channels=1,
                )
            )
            self._audio_config = speechsdk.audio.AudioConfig(
                stream=self._audio_stream
            )

            # Create recognizer
            self._recognizer = speechsdk.SpeechRecognizer(
                speech_config=self._speech_config,
                audio_config=self._audio_config,
            )

            # Set up phrase list
            self._phrase_list = speechsdk.PhraseListGrammar.from_recognizer(
                self._recognizer
            )
            for word in self._boost_words:
                self._phrase_list.addPhrase(word)

            # Create async queue for transcript events
            self._transcript_queue = asyncio.Queue()
            self._loop = asyncio.get_event_loop()

            self._running = True
            logger.info("Azure STT provider started")

        except Exception as e:
            raise STTProviderStartError(f"Failed to start Azure STT: {e}") from e

    async def stop(self) -> None:
        """Stop the Azure STT provider."""
        self._running = False

        if self._recognizer:
            try:
                self._recognizer.stop_continuous_recognition()
            except Exception:
                pass

        if self._audio_stream:
            try:
                self._audio_stream.close()
            except Exception:
                pass

        self._recognizer = None
        self._audio_stream = None
        self._audio_config = None
        self._speech_config = None
        self._phrase_list = None

        logger.info("Azure STT provider stopped")

    async def transcribe_stream(
        self,
        audio_stream: AsyncIterator[bytes],
        callback: TranscriptCallback,
    ) -> None:
        """Transcribe audio stream using Azure Speech.

        Args:
            audio_stream: Async iterator yielding 16-bit PCM audio chunks.
            callback: Called with each TranscriptEvent.
        """
        if not self._recognizer or not self._running:
            raise STTProviderError("Azure STT provider not started")

        # Set up event handlers
        def handle_recognizing(evt):
            """Handle interim (recognizing) results."""
            if evt.result.reason == speechsdk.ResultReason.RecognizingSpeech:
                text = evt.result.text.strip()
                if text and self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._transcript_queue.put(
                            TranscriptEvent(
                                text=text,
                                is_final=False,
                                utterance_id=self._utterance_id + 1,
                                confidence=0.8,  # Interim results don't have confidence
                            )
                        ),
                        self._loop,
                    )

        def handle_recognized(evt):
            """Handle final (recognized) results."""
            if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
                text = evt.result.text.strip()
                if text and self._loop:
                    self._utterance_id += 1
                    asyncio.run_coroutine_threadsafe(
                        self._transcript_queue.put(
                            TranscriptEvent(
                                text=text,
                                is_final=True,
                                utterance_id=self._utterance_id,
                                confidence=0.95,  # Azure doesn't always provide confidence
                            )
                        ),
                        self._loop,
                    )

        def handle_canceled(evt):
            """Handle cancellation/errors."""
            if evt.cancellation_details.reason == speechsdk.CancellationReason.Error:
                logger.error(
                    f"Azure STT error: {evt.cancellation_details.error_details}"
                )

        # Connect handlers
        self._recognizer.recognizing.connect(handle_recognizing)
        self._recognizer.recognized.connect(handle_recognized)
        self._recognizer.canceled.connect(handle_canceled)

        # Start continuous recognition
        self._recognizer.start_continuous_recognition()

        try:
            # Feed audio and process results
            async for audio_chunk in audio_stream:
                if not self._running:
                    break

                # Push audio to Azure
                self._audio_stream.write(audio_chunk)

                # Process any pending transcript events
                while not self._transcript_queue.empty():
                    try:
                        event = self._transcript_queue.get_nowait()
                        await callback(event)
                    except asyncio.QueueEmpty:
                        break

                # Small yield to prevent blocking
                await asyncio.sleep(0)

            # Signal end of audio
            self._audio_stream.close()

            # Process remaining events
            await asyncio.sleep(0.5)  # Allow final results to arrive
            while not self._transcript_queue.empty():
                try:
                    event = self._transcript_queue.get_nowait()
                    await callback(event)
                except asyncio.QueueEmpty:
                    break

        finally:
            self._recognizer.stop_continuous_recognition()

    # Boost word (phrase list) management

    async def add_boost_word(self, word: str, boost: float = 10.0) -> bool:
        """Add a word to the phrase list.

        Note: Azure doesn't support boost values, just presence in phrase list.
        """
        if word not in self._boost_words:
            self._boost_words.append(word)
            if self._phrase_list:
                self._phrase_list.addPhrase(word)
            logger.info(f"Added phrase: {redact_transcript(word)}")
        return True

    async def remove_boost_word(self, word: str) -> bool:
        """Remove a word from the phrase list.

        Note: Azure SDK doesn't support removing individual phrases.
        The phrase list will be rebuilt on next start().
        """
        if word in self._boost_words:
            self._boost_words.remove(word)
            logger.info(f"Removed phrase: {redact_transcript(word)} (takes effect on restart)")
            return True
        return False

    async def clear_boost_list(self) -> None:
        """Clear the phrase list.

        Note: Clears internal list. Takes full effect on next start().
        """
        self._boost_words.clear()
        if self._phrase_list:
            self._phrase_list.clear()
        logger.info("Cleared phrase list")

    def get_boost_words(self) -> list[str]:
        """Get current phrase list."""
        return list(self._boost_words)
