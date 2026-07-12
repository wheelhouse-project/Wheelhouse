"""End-to-end pipeline harness wiring WordEvents through to UIActionHandler.

Extends the SpeechPipelineHarness pattern but replaces MockApp with
AppAdapter -> UIActionHandler, giving full pipeline coverage with OS
calls mocked at the Windows API boundary.
"""
import asyncio
import sys
from pathlib import Path
from typing import Optional, List
from unittest.mock import MagicMock

# Path setup (matches existing test conventions)
project_root = Path(__file__).parent.parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from speech.word_event import WordEvent
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.command_engine import TextParser

from services.wheelhouse.tests.e2e.os_mocks import Recording
from services.wheelhouse.tests.e2e.app_adapter import AppAdapter
from services.wheelhouse.tests.test_speech_pipeline import MockContextMirror


class E2EPipelineHarness:
    """Full pipeline harness: WordEvent -> SpeechProcessor -> UIActionHandler.

    Usage:
        harness = E2EPipelineHarness()
        await harness.start()
        await harness.send_word("hello", start_of_utterance=True)
        # Check harness.recording for OS-level effects
        await harness.stop()
    """

    def __init__(self, catalog=None, context_kwargs: Optional[dict] = None,
                 greedy_timeout_ms: Optional[int] = None):
        self.word_queue: asyncio.Queue = asyncio.Queue()
        self._utterance_counter = 0
        self._elapsed = 0.0

        # AppAdapter creates real UIActionHandler and owns OS-level patches
        self.app = AppAdapter(Recording(), context_kwargs=context_kwargs)
        self.recording = self.app.recording

        # Use provided catalog or load fresh (for standalone usage)
        if catalog is not None:
            self.catalog = catalog
        else:
            wheelhouse_root = Path(__file__).parent.parent.parent
            patterns_path = str(wheelhouse_root / "speech" / "config" / "patterns.toml")
            self.catalog = PatternCatalog(patterns_path)

        # Read hotword from catalog -- don't hardcode it
        self.hotword = self.catalog.command_hotword or "x-ray"

        # Create mock speech handler for TextParser
        self.mock_speech_handler = MagicMock()
        self.mock_speech_handler.app = self.app

        # Create TextParser
        self.text_parser = TextParser(self.mock_speech_handler, self.catalog)

        # Create SpeechProcessor. greedy_timeout_ms stays at the
        # production default (5000 ms) unless a test overrides it --
        # the timer-expiry e2e tests pass a small value so the REAL
        # greedy buffer timer fires within test time
        # (wh-greedy-e2e-timer-coverage).
        processor_kwargs = dict(
            word_queue=self.word_queue,
            catalog=self.catalog,
            text_parser=self.text_parser,
            app=self.app,
            replacement_timeout_ms=400,
            command_timeout_ms=1000,
            hotword=self.hotword,
        )
        if greedy_timeout_ms is not None:
            processor_kwargs["greedy_timeout_ms"] = greedy_timeout_ms
        self.processor = SpeechProcessor(**processor_kwargs)

        # Replace context mirror with mock (avoids shared memory access)
        self.processor.context_mirror = MockContextMirror()

    async def start(self):
        """Start the speech processor."""
        await self.processor.start()

    async def stop(self):
        """Stop the speech processor and clean up patches."""
        await self.processor.stop()
        self.app.stop_patches()

    async def send_word(self, word: str, start_of_utterance: bool = False,
                        end_of_utterance: bool = False, delay_before_ms: int = 0,
                        utterance_id: Optional[int] = None,
                        allow_processing: bool = True):
        """Send a word event to the pipeline."""
        if delay_before_ms > 0:
            await asyncio.sleep(delay_before_ms / 1000.0)
            self._elapsed += delay_before_ms

        if utterance_id is None:
            if start_of_utterance:
                self._utterance_counter += 1
            utterance_id = self._utterance_counter

        event = WordEvent(
            word=word,
            start_of_utterance=start_of_utterance,
            end_of_utterance=end_of_utterance,
            utterance_id=utterance_id,
        )
        await self.word_queue.put(event)
        if allow_processing:
            await asyncio.sleep(0.01)

    async def send_utterance(self, words: List[str], inter_word_delay_ms: int = 50):
        """Send a complete utterance (sequence of words)."""
        for i, word in enumerate(words):
            await self.send_word(
                word=word,
                start_of_utterance=(i == 0),
                end_of_utterance=(i == len(words) - 1),
                delay_before_ms=0 if i == 0 else inter_word_delay_ms,
            )

    async def send_word_batch(self, words: List[str]):
        """Send multiple words in rapid succession without yielding.

        Simulates STT batching where multiple words arrive from a single
        'stable' message and are queued before the processor can handle them.
        """
        self._utterance_counter += 1
        utterance_id = self._utterance_counter

        for i, word in enumerate(words):
            event = WordEvent(
                word=word,
                start_of_utterance=(i == 0),
                end_of_utterance=False,
                utterance_id=utterance_id,
            )
            await self.word_queue.put(event)

        # Now yield once to let processor start handling
        await asyncio.sleep(0.01)

    async def send_utterance_end_marker(self, utterance_id: int):
        """Send an utterance end marker (signals no more words for this utterance)."""
        event = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=True,
            utterance_id=utterance_id,
            is_utterance_end_marker=True,
        )
        await self.word_queue.put(event)
        await asyncio.sleep(0.01)

    async def wait_for_timeout(self, timeout_ms: int = 500):
        """Wait for timeout to expire (lets processor buffer timeout trigger)."""
        await asyncio.sleep(timeout_ms / 1000.0)
        self._elapsed += timeout_ms
        # Extra time for processing
        await asyncio.sleep(0.05)
