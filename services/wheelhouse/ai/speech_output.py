"""SpeechOutput -- Text-to-speech via Windows SAPI (pyttsx3) with toast fallback.

Uses a dedicated single-thread executor to ensure pyttsx3 COM engine
is always created and used on the same thread (thread affinity requirement).
Falls back to toast notifications if pyttsx3 initialization fails.
"""

import asyncio
import concurrent.futures
import logging

import pyttsx3

log = logging.getLogger(__name__)


class SpeechOutput:
    """Text-to-speech output with toast fallback."""

    def __init__(self):
        self._engine = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    async def speak(self, text: str) -> None:
        """Speak text aloud. Falls back to toast notification on failure."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(self._executor, self._speak_sync, text)
        except Exception as e:
            log.warning("pyttsx3 speak failed, falling back to toast: %s", e)
            self._toast("Wheelhouse", text)

    async def speak_brief(self, text: str) -> None:
        """Speak a short status message. Non-blocking -- fire and forget."""
        loop = asyncio.get_event_loop()
        loop.create_task(self.speak(text))

    def _speak_sync(self, text: str) -> None:
        """Synchronous speak -- runs on the dedicated executor thread."""
        self._init_engine()
        self._engine.say(text)
        self._engine.runAndWait()

    def _init_engine(self) -> None:
        """Lazy-initialize pyttsx3 engine on the executor thread."""
        if self._engine is None:
            self._engine = pyttsx3.init()

    def _toast(self, title: str, message: str) -> None:
        """Fallback: log the message (toast integration deferred to GUI wiring)."""
        log.info("Toast [%s]: %s", title, message)

    async def shutdown(self) -> None:
        """Shut down the thread executor."""
        self._executor.shutdown(wait=False)
