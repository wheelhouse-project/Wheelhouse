"""Tests for SpeechOutput -- pyttsx3 TTS with toast fallback.

Verifies:
- Lazy engine initialization (not created until first speak)
- Engine reuse across calls
- Dedicated single-thread executor for pyttsx3 thread affinity
- Toast fallback when pyttsx3 init fails
- speak_brief is non-blocking (fire-and-forget)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai.speech_output import SpeechOutput


@pytest.fixture
def speech():
    """Create a SpeechOutput instance with mocked pyttsx3."""
    return SpeechOutput()


class TestLazyInit:
    """Verify engine is lazily initialized on first use."""

    def test_lazy_engine_init(self, speech):
        """Engine is NOT created in __init__, only on first speak()."""
        assert speech._engine is None

    @pytest.mark.asyncio
    async def test_engine_created_once(self, speech):
        """Repeated speak() calls reuse the same engine instance."""
        mock_engine = MagicMock()
        with patch("ai.speech_output.pyttsx3") as mock_pyttsx3:
            mock_pyttsx3.init.return_value = mock_engine

            await speech.speak("hello")
            await speech.speak("world")

            # pyttsx3.init() called exactly once -- engine is reused
            mock_pyttsx3.init.assert_called_once()


class TestSpeak:
    """Verify speak() calls pyttsx3 correctly via executor."""

    @pytest.mark.asyncio
    async def test_speak_calls_pyttsx3(self, speech):
        """speak() calls engine.say() and engine.runAndWait()."""
        mock_engine = MagicMock()
        with patch("ai.speech_output.pyttsx3") as mock_pyttsx3:
            mock_pyttsx3.init.return_value = mock_engine

            await speech.speak("test message")

            mock_engine.say.assert_called_once_with("test message")
            mock_engine.runAndWait.assert_called_once()

    @pytest.mark.asyncio
    async def test_speak_uses_dedicated_executor(self, speech):
        """Verify run_in_executor is called with the single-thread executor."""
        with patch("ai.speech_output.pyttsx3"):
            with patch.object(
                asyncio.get_event_loop(), "run_in_executor"
            ) as mock_exec:
                mock_exec.return_value = asyncio.Future()
                mock_exec.return_value.set_result(None)

                await speech.speak("test")

                # First arg should be our dedicated executor, not None (default pool)
                call_args = mock_exec.call_args
                assert call_args[0][0] is speech._executor


class TestFallback:
    """Verify toast fallback when pyttsx3 fails."""

    @pytest.mark.asyncio
    async def test_speak_fallback_to_toast(self, speech):
        """When pyttsx3 init raises, fall back to toast notification."""
        with patch("ai.speech_output.pyttsx3") as mock_pyttsx3:
            mock_pyttsx3.init.side_effect = Exception("COM error")

            with patch.object(speech, "_toast") as mock_toast:
                await speech.speak("fallback message")
                mock_toast.assert_called_once_with("WheelHouse", "fallback message")


class TestSpeakBrief:
    """Verify speak_brief is fire-and-forget (non-blocking)."""

    @pytest.mark.asyncio
    async def test_speak_brief_is_nonblocking(self, speech):
        """speak_brief creates a task but doesn't await it to completion."""
        mock_engine = MagicMock()
        with patch("ai.speech_output.pyttsx3") as mock_pyttsx3:
            mock_pyttsx3.init.return_value = mock_engine

            # speak_brief should return quickly (fire-and-forget)
            await speech.speak_brief("quick status")

            # Allow the background task to complete
            await asyncio.sleep(0.05)

            mock_engine.say.assert_called_once_with("quick status")


class TestShutdown:
    """Verify executor cleanup."""

    @pytest.mark.asyncio
    async def test_shutdown_stops_executor(self, speech):
        """shutdown() shuts down the thread executor."""
        with patch.object(speech._executor, "shutdown") as mock_shutdown:
            await speech.shutdown()
            mock_shutdown.assert_called_once_with(wait=False)
