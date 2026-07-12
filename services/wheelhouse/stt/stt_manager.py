"""
STT Manager - orchestrates STT providers and audio capture.

:flow: LogicProcess -> STTManager -> AudioCapture + STTProvider -> TranscriptEvent
:flow: Publishes transcripts to EventBus for command processing.

The STTManager is the central coordinator for speech-to-text functionality:
- Manages provider lifecycle (start, stop, switch)
- Coordinates audio capture with active provider
- Publishes transcript events to the EventBus
- Handles provider failures and fallback
"""

import asyncio
import logging
from typing import Callable, Awaitable

from stt.base import (
    STTProvider,
    TranscriptEvent,
    ProviderCapabilities,
    STTProviderError,
)
from stt.audio_capture import AudioCapture

logger = logging.getLogger(__name__)


# Type alias for transcript handlers
TranscriptHandler = Callable[[TranscriptEvent], Awaitable[None]]


class STTManager:
    """Manages STT provider lifecycle and audio capture.

    The STTManager is the main entry point for in-process STT functionality.
    It coordinates between audio capture and the active STT provider.

    :flow: Created by ServiceManager during Logic process startup.
    :flow: Starts AudioCapture and configured STTProvider.
    :flow: Routes transcripts to registered handlers (e.g., SpeechProcessor).

    Example:
        manager = STTManager()
        manager.on_transcript(handle_transcript)

        await manager.start("google")

        # Transcription runs automatically...

        await manager.stop()
    """

    def __init__(
        self,
        vad_enabled: bool = True,
        vad_threshold: float = 0.5,
        vad_lead_in_chunks: int = 10,  # ~300ms buffer before speech detection
    ):
        """Initialize STT Manager.
        
        Args:
            vad_enabled: Enable Voice Activity Detection filtering.
            vad_threshold: VAD confidence threshold (0.0-1.0).
            vad_lead_in_chunks: Audio context chunks before speech.
        """
        # Store VAD config - AudioCapture created in start() after checking provider
        self._vad_enabled = vad_enabled
        self._vad_threshold = vad_threshold
        self._vad_lead_in_chunks = vad_lead_in_chunks
        
        self.audio_capture: AudioCapture | None = None
        self.provider: STTProvider | None = None
        self._handlers: list[TranscriptHandler] = []
        self._running = False
        self._transcription_task: asyncio.Task | None = None

    def on_transcript(self, handler: TranscriptHandler) -> None:
        """Register a handler for transcript events.

        Args:
            handler: Async function called with each TranscriptEvent.
        """
        self._handlers.append(handler)

    async def start(
        self,
        provider_type: str,
        **provider_kwargs,
    ) -> None:
        """Start STT with the specified provider.

        Args:
            provider_type: Provider name ('google', 'azure').
            **provider_kwargs: Provider-specific configuration.

        Raises:
            STTProviderError: If provider fails to start.
        """
        if self._running:
            logger.warning("STTManager already running, stopping first")
            await self.stop()

        # Create provider first to check its capabilities
        self.provider = self._create_provider(provider_type, **provider_kwargs)
        
        # Check if provider has internal VAD - if so, disable our VAD
        capabilities = self.provider.get_capabilities()
        use_external_vad = self._vad_enabled and not capabilities.has_internal_vad
        
        logger.debug(
            f"VAD config: provider_has_internal={capabilities.has_internal_vad}, "
            f"config_enabled={self._vad_enabled}, using_external={use_external_vad}"
        )
        
        # Create AudioCapture with appropriate VAD setting
        self.audio_capture = AudioCapture(
            vad_enabled=use_external_vad,
            vad_threshold=self._vad_threshold,
            vad_lead_in_chunks=self._vad_lead_in_chunks,
        )

        # Start components
        await self.provider.start()
        await self.audio_capture.start()

        self._running = True

        # Start transcription loop
        self._transcription_task = asyncio.create_task(
            self._transcription_loop(),
            name="stt_transcription_loop",
        )

        logger.info(f"STTManager started with provider: {provider_type}")

    async def stop(self) -> None:
        """Stop STT and release resources."""
        self._running = False

        # Cancel transcription task
        if self._transcription_task:
            self._transcription_task.cancel()
            try:
                await self._transcription_task
            except asyncio.CancelledError:
                pass
            self._transcription_task = None

        # Stop components
        if self.audio_capture:
            await self.audio_capture.stop()

        if self.provider:
            await self.provider.stop()
            self.provider = None

        logger.info("STTManager stopped")

    async def switch_provider(
        self,
        provider_type: str,
        **provider_kwargs,
    ) -> None:
        """Switch to a different STT provider at runtime.

        Args:
            provider_type: New provider name.
            **provider_kwargs: Provider-specific configuration.

        This performs a hot-swap: stops the current provider,
        starts the new one, and resumes transcription.
        AudioCapture is recreated if VAD settings need to change.
        """
        logger.info(f"Switching STT provider to: {provider_type}")

        was_running = self._running

        # Stop current provider
        if self.provider:
            await self.provider.stop()

        # Cancel transcription task
        if self._transcription_task:
            self._transcription_task.cancel()
            try:
                await self._transcription_task
            except asyncio.CancelledError:
                pass

        # Stop current AudioCapture (VAD settings might change)
        if self.audio_capture:
            await self.audio_capture.stop()

        # Create new provider and check its VAD capabilities
        self.provider = self._create_provider(provider_type, **provider_kwargs)
        
        capabilities = self.provider.get_capabilities()
        use_external_vad = self._vad_enabled and not capabilities.has_internal_vad
        
        logger.debug(
            f"VAD config: provider_has_internal={capabilities.has_internal_vad}, "
            f"config_enabled={self._vad_enabled}, using_external={use_external_vad}"
        )
        
        # Create new AudioCapture with appropriate VAD setting
        self.audio_capture = AudioCapture(
            vad_enabled=use_external_vad,
            vad_threshold=self._vad_threshold,
            vad_lead_in_chunks=self._vad_lead_in_chunks,
        )
        
        await self.provider.start()
        await self.audio_capture.start()

        # Resume transcription if was running
        if was_running:
            self._transcription_task = asyncio.create_task(
                self._transcription_loop(),
                name="stt_transcription_loop",
            )

        logger.info(f"Switched to provider: {provider_type}")

    def get_current_provider(self) -> str | None:
        """Get the name of the current provider."""
        return self.provider.name if self.provider else None

    def get_capabilities(self) -> ProviderCapabilities | None:
        """Get capabilities of the current provider."""
        return self.provider.get_capabilities() if self.provider else None

    def get_available_providers(self) -> list[str]:
        """Get list of available (installed) providers.

        Returns:
            List of provider names that are ready to use.
        """
        available = []

        # Check Google Cloud Speech
        try:
            from google.cloud import speech_v1  # noqa: F401
            available.append("google")
        except ImportError:
            pass

        # Check Azure Speech
        try:
            import azure.cognitiveservices.speech  # noqa: F401
            available.append("azure")
        except ImportError:
            pass

        return available

    async def _transcription_loop(self) -> None:
        """Main transcription loop - streams audio to provider.

        :flow: AudioCapture.stream() -> STTProvider.transcribe_stream()
        :flow: TranscriptEvents -> registered handlers
        """
        logger.debug(f"Transcription loop starting for provider: {self.provider.name if self.provider else 'None'}")
        logger.debug(f"Audio capture running: {self.audio_capture._running}")
        try:
            await self.provider.transcribe_stream(
                self.audio_capture.stream(),
                self._dispatch_transcript,
            )
            logger.debug("Transcription loop completed normally")
        except asyncio.CancelledError:
            logger.debug("Transcription loop cancelled")
            raise
        except STTProviderError as e:
            logger.error(f"STT provider error: {e}")
            # TODO: Implement fallback logic
        except Exception as e:
            logger.exception(f"Unexpected error in transcription loop: {e}")

    async def _dispatch_transcript(self, event: TranscriptEvent) -> None:
        """Dispatch transcript event to all registered handlers."""
        for handler in self._handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.exception(f"Error in transcript handler: {e}")

    def _create_provider(
        self,
        provider_type: str,
        **kwargs,
    ) -> STTProvider:
        """Create an STT provider instance.

        Args:
            provider_type: Provider name ('google', 'azure').
            **kwargs: Provider-specific arguments.

        Returns:
            Configured STTProvider instance.

        Raises:
            ValueError: If provider type is unknown.
        """
        if provider_type == "google":
            from stt.providers.google_provider import GoogleSTTProvider

            return GoogleSTTProvider(
                language=kwargs.get("language", "en-US"),
                model=kwargs.get("model", "latest_long"),
                enable_punctuation=kwargs.get("enable_punctuation", True),
                boost_words=kwargs.get("boost_words"),
                boost_value=kwargs.get("boost_value", 10.0),
            )

        if provider_type == "azure":
            from stt.providers.azure_provider import AzureSTTProvider

            return AzureSTTProvider(
                subscription_key=kwargs.get("subscription_key"),
                region=kwargs.get("region", "eastus"),
                language=kwargs.get("language", "en-US"),
                enable_punctuation=kwargs.get("enable_punctuation", True),
                boost_words=kwargs.get("boost_words"),
            )

        raise ValueError(f"Unknown STT provider: {provider_type}")

