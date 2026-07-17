"""Integration tests for the speech processing pipeline.

Tests the flow from WordEvent intake through to output actions,
with timing simulation to test buffer timeouts.

Test Scope:
- Entry: WordEvents (simulating WebSocket messages)
- Exit: Captured commands/dictation sent to input process
- Components: PatternCatalog, SpeechProcessor, Router, CommandEngine
"""
import sys
from pathlib import Path

# Add parent directories to path for imports
# This handles the `services.wheelhouse.shared` import in speech_processor.py
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import pytest
from dataclasses import dataclass, field
from typing import List, Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from speech.word_event import WordEvent
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.command_engine import TextParser
from speech.domain import ProcessingMode
from utils.trace_context import get_trace_id


@dataclass
class CapturedOutput:
    """Represents a single captured output from the pipeline."""
    action: str
    params: dict
    timestamp: float = 0.0
    trace_id: str = ""


class MockApp:
    """Mock application that captures send_command and send_request calls.

    Used to verify what the speech pipeline sends to the input process.
    """

    def __init__(self):
        self.outputs: List[CapturedOutput] = []
        self._current_time = 0.0

    def set_time(self, t: float):
        """Set the current timestamp for captured outputs."""
        self._current_time = t

    async def send_command(self, command: dict):
        """Capture fire-and-forget commands."""
        self.outputs.append(CapturedOutput(
            action=command.get('action', ''),
            params=command.get('params', {}),
            timestamp=self._current_time,
            trace_id=get_trace_id(),
        ))

    async def send_request(self, action: str, params: dict):
        """Capture request-response commands (awaited)."""
        self.outputs.append(CapturedOutput(
            action=action,
            params=params,
            timestamp=self._current_time,
            trace_id=get_trace_id(),
        ))
        return True  # Simulate successful response

    def get_dictation_texts(self) -> List[str]:
        """Get all dictated text in order."""
        return [
            out.params.get('insertion_string', '')
            for out in self.outputs
            if out.action == 'intelligent_insert_text'
        ]

    def get_all_actions(self) -> List[str]:
        """Get all action names in order."""
        return [out.action for out in self.outputs]

    def clear(self):
        """Clear captured outputs."""
        self.outputs.clear()


class MockContextMirror:
    """Mock context mirror that doesn't use shared memory."""

    def __init__(self):
        self._context = {"app_name": "TestApp", "window_title": "Test Window", "timestamp": 0.0}

    def init_reader(self):
        pass

    def read_context(self) -> dict:
        return self._context

    def set_context(self, ctx: dict):
        self._context = ctx


class SpeechPipelineHarness:
    """Test harness for the speech processing pipeline.

    Provides controlled timing and output capture for testing
    the speech processor's behavior with various word sequences.
    """

    def __init__(self, patterns_path: Optional[str] = None):
        """Initialize the test harness.

        Args:
            patterns_path: Path to patterns.toml file. If None, uses default.
        """
        self.mock_app = MockApp()
        self.word_queue: asyncio.Queue = asyncio.Queue()

        # Load pattern catalog
        if patterns_path is None:
            patterns_path = "speech/config/patterns.toml"
        self.catalog = PatternCatalog(patterns_path)

        # Create mock speech handler for TextParser
        self.mock_speech_handler = MagicMock()
        self.mock_speech_handler.app = self.mock_app

        # Create TextParser
        self.text_parser = TextParser(self.mock_speech_handler, self.catalog)

        # Create SpeechProcessor with mocked context mirror
        self.processor = SpeechProcessor(
            word_queue=self.word_queue,
            catalog=self.catalog,
            text_parser=self.text_parser,
            app=self.mock_app,
            replacement_timeout_ms=400,
            command_timeout_ms=1000,
            hotword="x-ray"
        )

        # Replace context mirror with mock
        self.processor.context_mirror = MockContextMirror()

        self._elapsed = 0.0
        self._utterance_counter = 0

    async def start(self):
        """Start the speech processor."""
        await self.processor.start()

    async def stop(self):
        """Stop the speech processor."""
        await self.processor.stop()

    async def send_word(
        self,
        word: str,
        start_of_utterance: bool = False,
        end_of_utterance: bool = False,
        delay_before_ms: int = 0,
        utterance_id: Optional[int] = None,
        allow_processing: bool = True,
        **word_event_kwargs,
    ):
        """Send a word event to the pipeline.

        Args:
            word: The word to send
            start_of_utterance: Whether this is the first word of an utterance
            end_of_utterance: Whether this is the last word of an utterance
            delay_before_ms: Delay in ms before sending this word
            utterance_id: Optional utterance ID (auto-generated if not provided)
            allow_processing: If True, yield to processor after queueing (default).
                             If False, queue without yielding (simulates rapid batching).
            **word_event_kwargs: Extra fields passed through to WordEvent constructor
                (e.g. trace_id, is_utterance_end_marker, is_retraction_marker).
        """
        if delay_before_ms > 0:
            await asyncio.sleep(delay_before_ms / 1000.0)
            self._elapsed += delay_before_ms

        if utterance_id is None:
            if start_of_utterance:
                self._utterance_counter += 1
            utterance_id = self._utterance_counter

        self.mock_app.set_time(self._elapsed)

        event = WordEvent(
            word=word,
            start_of_utterance=start_of_utterance,
            end_of_utterance=end_of_utterance,
            utterance_id=utterance_id,
            **word_event_kwargs,
        )
        await self.word_queue.put(event)

        # Give processor time to handle the event (unless batching)
        if allow_processing:
            await asyncio.sleep(0.01)

    async def send_utterance(self, words: List[str], inter_word_delay_ms: int = 50):
        """Send a complete utterance (sequence of words).

        Args:
            words: List of words in the utterance
            inter_word_delay_ms: Delay between words in ms
        """
        for i, word in enumerate(words):
            is_first = (i == 0)
            is_last = (i == len(words) - 1)
            delay = 0 if i == 0 else inter_word_delay_ms

            await self.send_word(
                word=word,
                start_of_utterance=is_first,
                end_of_utterance=is_last,
                delay_before_ms=delay
            )

    async def send_word_batch(self, words: List[str]):
        """Send multiple words in rapid succession without yielding.

        This simulates STT batching where multiple words arrive from a single
        'stable' message and are queued before the processor can handle them.

        Args:
            words: List of words to queue rapidly
        """
        self._utterance_counter += 1
        utterance_id = self._utterance_counter

        for i, word in enumerate(words):
            is_first = (i == 0)
            event = WordEvent(
                word=word,
                start_of_utterance=is_first,
                end_of_utterance=False,
                utterance_id=utterance_id
            )
            await self.word_queue.put(event)

        # Now yield once to let processor start handling
        await asyncio.sleep(0.01)

    async def wait_for_timeout(self, timeout_ms: int = 500):
        """Wait for timeout to expire.

        Args:
            timeout_ms: Time to wait in ms
        """
        await asyncio.sleep(timeout_ms / 1000.0)
        self._elapsed += timeout_ms
        self.mock_app.set_time(self._elapsed)
        # Extra time for processing
        await asyncio.sleep(0.05)

    async def send_utterance_end_marker(self, utterance_id: int):
        """Send an utterance end marker (signals no more words for this utterance).

        This simulates what happens when Google STT sends a FINAL result -
        an empty WordEvent with is_utterance_end_marker=True is queued.

        Args:
            utterance_id: The utterance ID to end
        """
        event = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=True,
            utterance_id=utterance_id,
            is_utterance_end_marker=True
        )
        await self.word_queue.put(event)
        await asyncio.sleep(0.01)

    def get_outputs(self) -> List[CapturedOutput]:
        """Get all captured outputs."""
        return self.mock_app.outputs

    def get_dictation_texts(self) -> List[str]:
        """Get all dictated text strings."""
        return self.mock_app.get_dictation_texts()

    def clear_outputs(self):
        """Clear captured outputs."""
        self.mock_app.clear()


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def harness():
    """Create a test harness with production patterns."""
    return SpeechPipelineHarness()


@pytest.fixture
async def running_harness(harness):
    """Create and start a test harness."""
    await harness.start()
    yield harness
    await harness.stop()


# ============================================================================
# BASIC FUNCTIONALITY TESTS
# ============================================================================

class TestBasicDictation:
    """Test basic word passthrough to dictation."""

    @pytest.mark.asyncio
    async def test_single_word_passthrough(self, running_harness):
        """Non-pattern words should pass through immediately."""
        await running_harness.send_word("hello", start_of_utterance=True)
        await asyncio.sleep(0.05)

        texts = running_harness.get_dictation_texts()
        assert texts == ["hello"]

    @pytest.mark.asyncio
    async def test_multi_word_passthrough(self, running_harness):
        """Multiple non-pattern words should all pass through."""
        await running_harness.send_utterance(["hello", "world"])
        await asyncio.sleep(0.05)

        texts = running_harness.get_dictation_texts()
        assert texts == ["hello", "world"]


# ============================================================================
# PUNCTUATION TESTS - THE BUG SCENARIO
# ============================================================================

class TestPunctuationPatterns:
    """Test punctuation replacement patterns.

    These tests verify the bug fix for punctuation words followed by speech.
    """

    @pytest.mark.asyncio
    async def test_comma_alone_with_timeout(self, running_harness):
        """'comma' alone should insert ',' after timeout."""
        await running_harness.send_word("comma", start_of_utterance=True)
        await running_harness.wait_for_timeout(500)

        # Should have executed the comma pattern
        actions = running_harness.mock_app.get_all_actions()
        # The 'text' action inserts via the command engine
        # Check that we got punctuation, not the word "comma"
        texts = running_harness.get_dictation_texts()
        # If comma pattern executed, we won't have dictation of "comma"
        assert "comma" not in texts, "Should not dictate literal 'comma'"

    @pytest.mark.asyncio
    async def test_comma_followed_by_word_fast(self, running_harness):
        """'comma hello' with fast timing should insert ', hello'.

        THIS IS THE BUG TEST: Currently fails because router uses fullmatch()
        instead of search() for replacement patterns.
        """
        # Words arrive quickly (before timeout)
        await running_harness.send_word("comma", start_of_utterance=True)
        await running_harness.send_word("hello", start_of_utterance=False, delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        texts = running_harness.get_dictation_texts()
        all_outputs = running_harness.get_outputs()

        # Debug output
        print(f"\n[DEBUG] Dictation texts: {texts}")
        print(f"[DEBUG] All outputs: {[(o.action, o.params) for o in all_outputs]}")

        # Expected: comma pattern executes, then "hello" is dictated
        # The comma should NOT appear as literal text
        assert "comma" not in " ".join(texts), (
            f"Should not contain literal 'comma'. Got: {texts}"
        )
        # "hello" should be dictated
        assert "hello" in " ".join(texts), (
            f"Should contain 'hello'. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_comma_followed_by_word_slow(self, running_harness):
        """'comma' then pause then 'hello' should insert ',' then 'hello' separately."""
        await running_harness.send_word("comma", start_of_utterance=True)
        await running_harness.wait_for_timeout(500)  # Timeout fires

        running_harness.clear_outputs()

        await running_harness.send_word("hello", start_of_utterance=True)
        await asyncio.sleep(0.05)

        texts = running_harness.get_dictation_texts()
        assert texts == ["hello"]

    @pytest.mark.asyncio
    async def test_period_alone(self, running_harness):
        """'period' alone should insert '.'"""
        await running_harness.send_word("period", start_of_utterance=True)
        await running_harness.wait_for_timeout(500)

        texts = running_harness.get_dictation_texts()
        assert "period" not in texts, "Should not dictate literal 'period'"

    @pytest.mark.asyncio
    async def test_period_followed_by_word(self, running_harness):
        """'period test' should insert '. test'.

        THIS IS THE BUG TEST.
        """
        await running_harness.send_word("period", start_of_utterance=True)
        await running_harness.send_word("test", start_of_utterance=False, delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        texts = running_harness.get_dictation_texts()
        assert "period" not in " ".join(texts), (
            f"Should not contain literal 'period'. Got: {texts}"
        )
        assert "test" in " ".join(texts), (
            f"Should contain 'test'. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_question_mark_two_words(self, running_harness):
        """'question mark' (two-word pattern) should insert '?'"""
        await running_harness.send_word("question", start_of_utterance=True)
        await running_harness.send_word("mark", start_of_utterance=False, delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        texts = running_harness.get_dictation_texts()
        # Should not contain the literal words
        combined = " ".join(texts)
        assert "question" not in combined, f"Should not contain 'question'. Got: {texts}"
        assert "mark" not in combined, f"Should not contain 'mark'. Got: {texts}"

    @pytest.mark.asyncio
    async def test_comma_followed_by_word_batch(self, running_harness):
        """'comma hello' arriving in same STT batch (queued before processor runs).

        This simulates real STT behavior where Google sends 'comma hello' as
        a single stable message, causing both words to be queued before the
        processor has a chance to handle the first word.

        THIS IS THE MOST REALISTIC BUG TEST.
        """
        # Simulate batch arrival - words queued without yielding to processor
        await running_harness.send_word_batch(["comma", "hello"])
        await running_harness.wait_for_timeout(500)

        texts = running_harness.get_dictation_texts()
        all_outputs = running_harness.get_outputs()

        # Debug output
        print(f"\n[DEBUG BATCH] Dictation texts: {texts}")
        print(f"[DEBUG BATCH] All outputs: {[(o.action, o.params) for o in all_outputs]}")

        # Expected: comma pattern executes, then "hello" is dictated
        assert "comma" not in " ".join(texts), (
            f"Should not contain literal 'comma'. Got: {texts}"
        )
        assert "hello" in " ".join(texts), (
            f"Should contain 'hello'. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_comma_mid_utterance(self, running_harness):
        """'hello comma world' - comma mid-utterance should still insert punctuation.

        This tests the MID_REPLACEMENT_BUFFER case where replacement patterns
        appearing mid-utterance should still be buffered and executed.
        """
        await running_harness.send_word("hello", start_of_utterance=True)
        await running_harness.send_word("comma", start_of_utterance=False, delay_before_ms=50)
        await running_harness.send_word("world", start_of_utterance=False, delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        texts = running_harness.get_dictation_texts()
        all_outputs = running_harness.get_outputs()

        # Debug output
        print(f"\n[DEBUG MID-UTTERANCE] Dictation texts: {texts}")
        print(f"[DEBUG MID-UTTERANCE] All outputs: {[(o.action, o.params) for o in all_outputs]}")

        # Expected: "hello", then comma executes (,), then "world"
        combined = " ".join(texts)
        assert "comma" not in combined, (
            f"Should not contain literal 'comma'. Got: {texts}"
        )
        assert "hello" in combined, f"Should contain 'hello'. Got: {texts}"
        assert "world" in combined, f"Should contain 'world'. Got: {texts}"

    @pytest.mark.asyncio
    async def test_multiple_punctuation_in_remainder(self, running_harness):
        """'hello comma comma world' - multiple punctuation patterns in sequence.

        Tests that the remainder processing loop handles multiple patterns.
        """
        await running_harness.send_word("hello", start_of_utterance=True)
        await running_harness.send_word("comma", start_of_utterance=False, delay_before_ms=50)
        await running_harness.send_word("comma", start_of_utterance=False, delay_before_ms=50)
        await running_harness.send_word("world", start_of_utterance=False, delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        texts = running_harness.get_dictation_texts()
        all_outputs = running_harness.get_outputs()

        # Debug output
        print(f"\n[DEBUG MULTI-PUNCT] Dictation texts: {texts}")
        print(f"[DEBUG MULTI-PUNCT] All outputs: {[(o.action, o.params) for o in all_outputs]}")

        # Expected: "hello", then two commas (,,), then "world"
        combined = " ".join(texts)
        assert "comma" not in combined, (
            f"Should not contain literal 'comma'. Got: {texts}"
        )
        assert "hello" in combined, f"Should contain 'hello'. Got: {texts}"
        assert "world" in combined, f"Should contain 'world'. Got: {texts}"
        # Count the commas - should have 2
        comma_count = sum(1 for t in texts if t.strip() == ',')
        assert comma_count == 2, f"Should have 2 commas, got {comma_count}. Texts: {texts}"

    @pytest.mark.asyncio
    async def test_router_pattern_matching_debug(self, running_harness):
        """Debug test to verify pattern matching behavior.

        This test directly examines what the router decides for various inputs.
        """
        from speech.router import SpeechRouter
        from speech.pattern_catalog import PatternCatalog, PatternType
        from speech.domain import ProcessingMode, Action

        catalog = running_harness.catalog
        router = SpeechRouter(catalog, "x-ray")

        # Test: Is "comma" recognized as a replacement pattern?
        pattern_type = catalog.get_pattern_type("comma")
        print(f"\n[DEBUG] Pattern type for 'comma': {pattern_type}")
        assert pattern_type == PatternType.REPLACEMENT, f"Expected REPLACEMENT, got {pattern_type}"

        # Test: Is single-word "comma" complete?
        is_complete = router._is_single_word_complete("comma", "replacement")
        print(f"[DEBUG] Is 'comma' single-word complete: {is_complete}")

        # Test: Can "comma" match with next word?
        can_continue = not router._cannot_match_with_next_word(["comma"], "replacement")
        print(f"[DEBUG] Can 'comma' continue with next word: {can_continue}")

        # Test: What patterns match "comma"?
        patterns = catalog.get_matching_patterns("comma")
        print(f"[DEBUG] Patterns matching 'comma': {len(patterns)}")
        for compiled, ptype, data in patterns:
            print(f"  - Pattern: {compiled.pattern}, Type: {ptype}")
            # Test fullmatch vs search
            fullmatch_result = compiled.fullmatch("comma")
            search_result = compiled.search("comma")
            print(f"    fullmatch('comma'): {fullmatch_result}")
            print(f"    search('comma'): {search_result}")
            fullmatch_multi = compiled.fullmatch("comma hello")
            search_multi = compiled.search("comma hello")
            print(f"    fullmatch('comma hello'): {fullmatch_multi}")
            print(f"    search('comma hello'): {search_multi}")


# ============================================================================
# SOUND-ALIKE MISHEAR ALIAS TESTS (wh-int8-punctuation-mishears)
# ============================================================================

class TestMishearAliases:
    """Known sound-alike mishears of punctuation words map to punctuation.

    The shipped default STT engine (Parakeet TDT int8) consistently
    transcribes spoken "comma" as come/kama/commer (or "come on") and
    spoken "colon" as colin, so dictating those punctuation marks is
    broken out of the box. The aliases accept the mishears ONLY when they
    arrive as the complete utterance on their own (^...$ anchors), so the
    real words stay dictatable inside normal sentences.

    The ^ anchor classifies these as command-type patterns, which buffer
    on the command timeout. Both STT paths emit is_utterance_end_marker
    after every utterance, and the processor finalizes a pending buffer
    immediately on that marker, so these tests send the marker the way
    real STT finals do instead of waiting out the wall-clock timeout.
    """

    async def _end_utterance(self, harness):
        """Send the utterance-end marker for the current utterance."""
        await harness.send_utterance_end_marker(harness._utterance_counter)

    @pytest.mark.asyncio
    async def test_colin_alone_inserts_colon(self, running_harness):
        """'colin' as a whole utterance is the known mishear of 'colon'."""
        await running_harness.send_word("colin", start_of_utterance=True)
        await self._end_utterance(running_harness)
        await running_harness.wait_for_timeout(200)

        texts = running_harness.get_dictation_texts()
        assert "colin" not in " ".join(texts), (
            f"Should not dictate literal 'colin'. Got: {texts}"
        )
        assert any(t.strip() == ":" for t in texts), (
            f"Should insert ':'. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_colin_capitalized_inserts_colon(self, running_harness):
        """Parakeet capitalizes the name form ('Colin'); match is case-insensitive."""
        await running_harness.send_word("Colin", start_of_utterance=True)
        await self._end_utterance(running_harness)
        await running_harness.wait_for_timeout(200)

        texts = running_harness.get_dictation_texts()
        assert "Colin" not in " ".join(texts), (
            f"Should not dictate literal 'Colin'. Got: {texts}"
        )
        assert any(t.strip() == ":" for t in texts), (
            f"Should insert ':'. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_come_alone_inserts_comma(self, running_harness):
        """'come' as a whole utterance is the known mishear of 'comma'."""
        await running_harness.send_word("come", start_of_utterance=True)
        await self._end_utterance(running_harness)
        await running_harness.wait_for_timeout(200)

        texts = running_harness.get_dictation_texts()
        assert "come" not in " ".join(texts), (
            f"Should not dictate literal 'come'. Got: {texts}"
        )
        assert any(t.strip() == "," for t in texts), (
            f"Should insert ','. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_kama_alone_inserts_comma(self, running_harness):
        """'kama' as a whole utterance is the known mishear of 'comma'."""
        await running_harness.send_word("kama", start_of_utterance=True)
        await self._end_utterance(running_harness)
        await running_harness.wait_for_timeout(200)

        texts = running_harness.get_dictation_texts()
        assert "kama" not in " ".join(texts), (
            f"Should not dictate literal 'kama'. Got: {texts}"
        )
        assert any(t.strip() == "," for t in texts), (
            f"Should insert ','. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_commer_alone_inserts_comma(self, running_harness):
        """'commer' as a whole utterance is the known mishear of 'comma'."""
        await running_harness.send_word("commer", start_of_utterance=True)
        await self._end_utterance(running_harness)
        await running_harness.wait_for_timeout(200)

        texts = running_harness.get_dictation_texts()
        assert "commer" not in " ".join(texts), (
            f"Should not dictate literal 'commer'. Got: {texts}"
        )
        assert any(t.strip() == "," for t in texts), (
            f"Should insert ','. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_come_on_utterance_inserts_comma(self, running_harness):
        """'come on' as a whole utterance is the known two-word mishear of 'comma'."""
        await running_harness.send_word("come", start_of_utterance=True)
        await running_harness.send_word("on", start_of_utterance=False, delay_before_ms=50)
        await self._end_utterance(running_harness)
        await running_harness.wait_for_timeout(200)

        texts = running_harness.get_dictation_texts()
        combined = " ".join(texts)
        assert "come" not in combined, (
            f"Should not dictate literal 'come'. Got: {texts}"
        )
        assert "on" not in texts, (
            f"Should not dictate literal 'on'. Got: {texts}"
        )
        assert any(t.strip() == "," for t in texts), (
            f"Should insert ','. Got: {texts}"
        )

    # ------------------------------------------------------------------
    # Anchor guards: the aliases must NOT fire inside normal sentences.
    # These protect against loosening ^...$ to \b...\b (mutation gate).
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_colin_mid_utterance_stays_text(self, running_harness):
        """'ask colin tomorrow' keeps 'colin' as a dictated word."""
        await running_harness.send_utterance(["ask", "colin", "tomorrow"])
        await self._end_utterance(running_harness)
        await running_harness.wait_for_timeout(200)

        texts = running_harness.get_dictation_texts()
        combined = " ".join(texts)
        assert "colin" in combined, (
            f"'colin' inside a sentence must stay text. Got: {texts}"
        )
        assert not any(t.strip() == ":" for t in texts), (
            f"No ':' should be inserted mid-sentence. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_come_mid_utterance_stays_text(self, running_harness):
        """'please come home' keeps 'come' as a dictated word."""
        await running_harness.send_utterance(["please", "come", "home"])
        await self._end_utterance(running_harness)
        await running_harness.wait_for_timeout(200)

        texts = running_harness.get_dictation_texts()
        combined = " ".join(texts)
        assert "come" in combined, (
            f"'come' inside a sentence must stay text. Got: {texts}"
        )
        assert not any(t.strip() == "," for t in texts), (
            f"No ',' should be inserted mid-sentence. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_come_followed_by_other_word_stays_text(self, running_harness):
        """'come home' (utterance continues past 'come') dictates both words.

        Exercises the buffering path: after 'come' the router may wait for
        a possible 'come on', but 'home' breaks the match and both words
        must fall back to dictation.
        """
        await running_harness.send_word("come", start_of_utterance=True)
        await running_harness.send_word("home", start_of_utterance=False, delay_before_ms=50)
        await self._end_utterance(running_harness)
        await running_harness.wait_for_timeout(200)

        texts = running_harness.get_dictation_texts()
        combined = " ".join(texts)
        assert "come" in combined, (
            f"'come home' must dictate 'come'. Got: {texts}"
        )
        assert "home" in combined, (
            f"'come home' must dictate 'home'. Got: {texts}"
        )
        assert not any(t.strip() == "," for t in texts), (
            f"No ',' should be inserted for 'come home'. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_colin_followed_by_word_stays_text(self, running_harness):
        """'colin tomorrow' dictates both words.

        Guards the immediate-execute path: 'colin' alone is a complete
        pattern with no possible extension, but a whole-utterance-only
        alias must wait for the utterance to actually end before firing.
        """
        await running_harness.send_word("colin", start_of_utterance=True)
        await running_harness.send_word("tomorrow", start_of_utterance=False, delay_before_ms=50)
        await self._end_utterance(running_harness)
        await running_harness.wait_for_timeout(200)

        texts = running_harness.get_dictation_texts()
        combined = " ".join(texts)
        assert "colin" in combined, (
            f"'colin tomorrow' must dictate 'colin'. Got: {texts}"
        )
        assert "tomorrow" in combined, (
            f"'colin tomorrow' must dictate 'tomorrow'. Got: {texts}"
        )
        assert not any(t.strip() == ":" for t in texts), (
            f"No ':' should be inserted for 'colin tomorrow'. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_come_on_followed_by_word_stays_text(self, running_harness):
        """'come on over' dictates all three words.

        Guards the buffering execute-on-complete path: 'come on' matches
        the alias, but the utterance continues, so it must stay text.
        """
        await running_harness.send_word("come", start_of_utterance=True)
        await running_harness.send_word("on", start_of_utterance=False, delay_before_ms=50)
        await running_harness.send_word("over", start_of_utterance=False, delay_before_ms=50)
        await self._end_utterance(running_harness)
        await running_harness.wait_for_timeout(200)

        texts = running_harness.get_dictation_texts()
        combined = " ".join(texts)
        for expected in ("come", "on", "over"):
            assert expected in combined, (
                f"'come on over' must dictate '{expected}'. Got: {texts}"
            )
        assert not any(t.strip() == "," for t in texts), (
            f"No ',' should be inserted for 'come on over'. Got: {texts}"
        )


# ============================================================================
# MULTI-WORD PATTERN PROTECTION TESTS
# ============================================================================

class TestMultiWordPatterns:
    """Test that multi-word patterns with optional parts still work.

    These tests ensure the fix doesn't break patterns like 'backspace three'.
    """

    @pytest.mark.asyncio
    async def test_backspace_alone(self, running_harness):
        """'backspace' alone should execute single backspace."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.wait_for_timeout(500)

        texts = running_harness.get_dictation_texts()
        # Should NOT dictate "backspace" - it should execute as command
        assert "backspace" not in texts

    @pytest.mark.asyncio
    async def test_backspace_with_count(self, running_harness):
        """'backspace three' should execute three backspaces."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("three", start_of_utterance=False, delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        texts = running_harness.get_dictation_texts()
        # Should NOT dictate the words - command should execute
        combined = " ".join(texts)
        assert "backspace" not in combined
        assert "three" not in combined


# ============================================================================
# COMMAND PATTERN TESTS
# ============================================================================

class TestCommandPatterns:
    """Test command patterns (with ^ anchor)."""

    @pytest.mark.asyncio
    async def test_command_mid_utterance_is_dictation(self, running_harness):
        """Commands mid-utterance should be treated as dictation."""
        # "I want to delete something" - "delete" should be dictated
        await running_harness.send_utterance(["I", "want", "to", "delete", "something"])
        await asyncio.sleep(0.05)

        texts = running_harness.get_dictation_texts()
        combined = " ".join(texts)
        assert "delete" in combined, "Mid-utterance 'delete' should be dictated"


# ============================================================================
# TIMING TESTS
# ============================================================================

class TestTimingBehavior:
    """Test timeout and timing behavior."""

    @pytest.mark.asyncio
    async def test_replacement_timeout_400ms(self, running_harness):
        """Replacement patterns should timeout after ~400ms."""
        await running_harness.send_word("comma", start_of_utterance=True)

        # Wait less than timeout
        await asyncio.sleep(0.2)

        # Should still be buffering (no output yet or still in buffer)
        # Note: This is hard to test precisely due to async timing

        # Wait for full timeout
        await running_harness.wait_for_timeout(300)

        # Now should have processed
        texts = running_harness.get_dictation_texts()
        assert "comma" not in texts


# ============================================================================
# PROCESSING LOOP RESILIENCE TESTS
# ============================================================================

class TestProcessingLoopResilience:
    """Test that the processing loop survives individual word processing failures.

    The processing loop should catch and log errors for individual words
    without crashing the entire loop. This prevents a single timeout or
    error from breaking the entire speech pipeline.
    """

    @pytest.mark.asyncio
    async def test_loop_survives_timeout_error(self, harness):
        """Processing loop should continue after a TimeoutError on one word.

        Simulates the real bug: send_request times out, raising TimeoutError.
        The loop should log the error and continue processing subsequent words.
        """
        # Create a mock app that raises TimeoutError on first call, then works
        call_count = 0
        original_send_request = harness.mock_app.send_request

        async def flaky_send_request(action: str, params: dict):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call times out (simulates the real bug scenario)
                raise TimeoutError("Request timed out after 5.0s")
            return await original_send_request(action, params)

        harness.mock_app.send_request = flaky_send_request

        await harness.start()
        try:
            # Send first word - this will timeout
            await harness.send_word("first", start_of_utterance=True)
            await asyncio.sleep(0.1)  # Give time for error to propagate

            # Send second word - this should still work if loop survived
            await harness.send_word("second", start_of_utterance=True)
            await asyncio.sleep(0.1)

            # The loop should have survived and processed "second"
            texts = harness.get_dictation_texts()
            assert "second" in texts, (
                f"Processing loop should have survived timeout and processed 'second'. "
                f"Got: {texts}"
            )
        finally:
            await harness.stop()

    @pytest.mark.asyncio
    async def test_loop_survives_generic_exception(self, harness):
        """Processing loop should continue after a generic exception.

        Any exception during word processing should be caught and logged,
        not crash the loop.
        """
        call_count = 0
        original_send_request = harness.mock_app.send_request

        async def flaky_send_request(action: str, params: dict):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Simulated unexpected error")
            return await original_send_request(action, params)

        harness.mock_app.send_request = flaky_send_request

        await harness.start()
        try:
            # Send first word - this will raise RuntimeError
            await harness.send_word("first", start_of_utterance=True)
            await asyncio.sleep(0.1)

            # Send second word - should still work
            await harness.send_word("second", start_of_utterance=True)
            await asyncio.sleep(0.1)

            texts = harness.get_dictation_texts()
            assert "second" in texts, (
                f"Processing loop should have survived exception and processed 'second'. "
                f"Got: {texts}"
            )
        finally:
            await harness.stop()

    @pytest.mark.asyncio
    async def test_loop_processes_remaining_queue_after_error(self, harness):
        """After an error, all remaining queued words should still be processed.

        This tests the real scenario: multiple words queued, first one fails,
        the rest should still be processed.
        """
        call_count = 0
        original_send_request = harness.mock_app.send_request

        async def flaky_send_request(action: str, params: dict):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("Simulated timeout")
            return await original_send_request(action, params)

        harness.mock_app.send_request = flaky_send_request

        await harness.start()
        try:
            # Queue multiple words rapidly (simulates real STT batch)
            await harness.send_word_batch(["first", "second", "third"])
            await asyncio.sleep(0.2)  # Give time to process all

            texts = harness.get_dictation_texts()
            # First word failed, but second and third should succeed
            assert "second" in texts, f"Should have processed 'second'. Got: {texts}"
            assert "third" in texts, f"Should have processed 'third'. Got: {texts}"
        finally:
            await harness.stop()


# ============================================================================
# TIMEOUT HANDLER RESILIENCE TESTS
# ============================================================================

class TestTimeoutHandlerResilience:
    """Test that _timeout_handler survives errors during execute_decision.

    Bug: _timeout_handler only caught CancelledError. A TimeoutError from
    send_request propagated to the global asyncio exception handler, crashing
    the entire Logic process. The _processing_loop had this protection but
    _timeout_handler did not.
    """

    @pytest.mark.asyncio
    async def test_timeout_handler_does_not_leak_exceptions(self, harness):
        """_timeout_handler must catch exceptions, not let them propagate.

        In production, unhandled exceptions from asyncio tasks reach the
        global exception handler in main.py, which triggers a full process
        shutdown. This test lets a word enter buffering normally, then
        patches _execute_decision before the timeout fires.
        """
        await harness.start()
        try:
            # Send "question" which enters REPLACEMENT_BUFFERING (waiting for
            # possible "mark" to complete "question mark" pattern) and starts
            # a 400ms timeout task. Let the word be processed normally.
            await harness.send_word("question", start_of_utterance=True)

            # Capture the timeout task reference
            timeout_task = harness.processor.timeout_task
            assert timeout_task is not None, (
                "Processor should have created a timeout task for buffered pattern"
            )

            # NOW patch _execute_decision to raise TimeoutError.
            # This simulates the real crash: timeout fires -> _execute_decision
            # -> _send_to_dictation -> send_request -> TimeoutError
            async def failing_execute(decision):
                raise TimeoutError("Request timed out after 5.0s")

            harness.processor._execute_decision = failing_execute

            # Wait for the timeout task to complete (it will call our patch)
            try:
                await asyncio.wait_for(asyncio.shield(timeout_task), timeout=2.0)
            except TimeoutError as e:
                if "Request timed out" in str(e):
                    pytest.fail(
                        f"_timeout_handler leaked TimeoutError: {e}. "
                        f"In production this crashes the Logic process."
                    )
                raise

            # Verify the task completed without storing an exception
            assert timeout_task.done()
            if not timeout_task.cancelled():
                exc = timeout_task.exception()
                assert exc is None, (
                    f"_timeout_handler stored exception {type(exc).__name__}: {exc}. "
                    f"In production this crashes via global exception handler."
                )
        finally:
            await harness.stop()


# ============================================================================
# UTTERANCE END MARKER TESTS
# ============================================================================

class TestUtteranceEndMarker:
    """Test that end_utterance is always sent when utterance completes.

    Bug: When IGNORE decision path was taken (e.g., empty buffer timeout),
    the deferred end_utterance was never sent, causing a 60s timeout in
    UtteranceClipboardManager.

    Fix: The IGNORE decision path now calls _send_pending_utterance_end().
    """

    @pytest.mark.asyncio
    async def test_utterance_end_sent_after_dictation(self, harness):
        """end_utterance should be sent after dictation completes."""
        await harness.start()
        try:
            # Send a word with utterance end marker
            await harness.send_word("hello", start_of_utterance=True, utterance_id=99)
            await harness.send_utterance_end_marker(utterance_id=99)

            # Wait for processing
            await asyncio.sleep(0.1)

            actions = harness.mock_app.get_all_actions()
            assert "end_utterance" in actions, (
                f"Should have sent end_utterance after dictation. Got: {actions}"
            )

        finally:
            await harness.stop()

    @pytest.mark.asyncio
    async def test_utterance_end_sent_after_command(self, harness):
        """end_utterance should be sent after command execution."""
        await harness.start()
        try:
            # Send "backspace" command with utterance end marker
            await harness.send_word("backspace", start_of_utterance=True, utterance_id=100)
            await harness.send_utterance_end_marker(utterance_id=100)

            # Wait for command timeout (1000ms) + buffer
            await harness.wait_for_timeout(1100)

            actions = harness.mock_app.get_all_actions()
            assert "end_utterance" in actions, (
                f"Should have sent end_utterance after command. Got: {actions}"
            )

        finally:
            await harness.stop()

    @pytest.mark.asyncio
    async def test_utterance_end_sent_on_ignore_path(self, harness):
        """end_utterance should be sent even when IGNORE decision is taken.

        This tests the specific bug fix: when a pattern times out with an
        empty buffer (e.g., hotword alone), end_utterance must still be sent.
        """
        await harness.start()
        try:
            # Send a replacement pattern word that will timeout
            # "comma" is a replacement pattern - send it alone, let it timeout
            await harness.send_word("comma", start_of_utterance=True, utterance_id=101)
            await harness.send_utterance_end_marker(utterance_id=101)

            # Wait for replacement timeout (400ms + buffer)
            await harness.wait_for_timeout(500)

            actions = harness.mock_app.get_all_actions()
            assert "end_utterance" in actions, (
                f"end_utterance should be sent even on IGNORE path. Got: {actions}"
            )

        finally:
            await harness.stop()
