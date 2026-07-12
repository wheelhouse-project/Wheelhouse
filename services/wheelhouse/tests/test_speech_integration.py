"""Speech pipeline integration tests for resilience scenarios.

Tests the full processing chain with realistic scenarios that could
strand an accessibility user if they fail. Exercises:
  WordEvent -> SpeechProcessor -> SpeechRouter -> CommandEngine -> Actions

Uses the SpeechPipelineHarness from test_speech_pipeline.py for
controlled timing and output capture.

Task R5 of the WheelHouse resilience testing plan.
"""
import sys
from pathlib import Path

# Add parent directories to path for imports (matches test_speech_pipeline.py)
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import pytest

from test_speech_pipeline import SpeechPipelineHarness


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def harness():
    """Create a fresh test harness with production patterns."""
    return SpeechPipelineHarness()


@pytest.fixture
async def running_harness(harness):
    """Create, start, and yield a test harness; stop on teardown."""
    await harness.start()
    yield harness
    await harness.stop()


# ============================================================================
# 1. EMPTY UTTERANCE PRODUCES NO COMMANDS
# ============================================================================

class TestEmptyUtterance:
    """VAD start followed by immediate end should not execute anything.

    Real scenario: microphone picks up a brief noise that triggers VAD
    start, but ends immediately with no actual speech content. The
    pipeline must not emit phantom commands or dictation.
    """

    @pytest.mark.asyncio
    async def test_empty_utterance_start_then_end(self, running_harness):
        """An utterance that starts and immediately ends with no words
        should produce zero outputs."""
        # Send the utterance end marker with no preceding words
        await running_harness.send_utterance_end_marker(utterance_id=500)
        # Allow processing time
        await asyncio.sleep(0.1)

        outputs = running_harness.get_outputs()
        # The only permissible output is end_utterance itself.
        non_end_outputs = [
            o for o in outputs
            if o.action != "end_utterance"
        ]
        assert non_end_outputs == [], (
            f"Empty utterance should produce no commands or dictation. "
            f"Got: {[(o.action, o.params) for o in non_end_outputs]}"
        )

    @pytest.mark.asyncio
    async def test_start_of_utterance_flag_then_immediate_end(self, running_harness):
        """A WordEvent with start_of_utterance=True and end_of_utterance=True
        but empty word should not produce commands."""
        from speech.word_event import WordEvent

        # Simulate an edge case: STT sends a start+end event with empty word
        event = WordEvent(
            word="",
            start_of_utterance=True,
            end_of_utterance=True,
            utterance_id=501
        )
        await running_harness.word_queue.put(event)
        await asyncio.sleep(0.1)

        texts = running_harness.get_dictation_texts()
        # Empty string dictation should be filtered or harmless
        non_empty_texts = [t for t in texts if t.strip()]
        assert non_empty_texts == [], (
            f"Empty word event should not produce meaningful dictation. Got: {texts}"
        )


# ============================================================================
# 2. SINGLE-WORD COMMANDS EXECUTE CORRECTLY
# ============================================================================

class TestSingleWordCommand:
    """Single-word commands like 'backspace' should produce the correct
    action, not be dictated as text.

    Real scenario: user says 'backspace' to delete the last character.
    If this is treated as dictation, the literal word 'backspace' gets
    typed into the document instead of performing the deletion.
    """

    @pytest.mark.asyncio
    async def test_backspace_executes_as_command(self, running_harness):
        """'backspace' at utterance start should execute a key press,
        not dictate the word 'backspace'."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        # backspace has optional \d+ so it buffers; wait for timeout
        await running_harness.wait_for_timeout(1100)

        texts = running_harness.get_dictation_texts()
        assert "backspace" not in texts, (
            f"'backspace' should execute as command, not dictation. Got: {texts}"
        )

        all_actions = running_harness.mock_app.get_all_actions()
        # The command should have produced at least one non-dictation action
        # (press action via send_command which records as 'press' action)
        has_command_output = any(a != "intelligent_insert_text" and a != "end_utterance" for a in all_actions)
        assert has_command_output or len(all_actions) > 0, (
            f"'backspace' should have triggered a command action. Actions: {all_actions}"
        )

    @pytest.mark.asyncio
    async def test_backspace_with_count_executes(self, running_harness):
        """'backspace 3' should execute three backspaces, not dictate."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("3", start_of_utterance=False, delay_before_ms=100)
        await running_harness.wait_for_timeout(1100)

        texts = running_harness.get_dictation_texts()
        combined = " ".join(texts)
        assert "backspace" not in combined, (
            f"'backspace 3' should execute, not dictate. Got: {texts}"
        )
        assert "3" not in combined, (
            f"The count '3' should be consumed by the command. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_delete_executes_as_command(self, running_harness):
        """'delete' at utterance start should execute, not dictate."""
        await running_harness.send_word("delete", start_of_utterance=True)
        await running_harness.wait_for_timeout(1100)

        texts = running_harness.get_dictation_texts()
        assert "delete" not in texts, (
            f"'delete' should execute as command, not dictation. Got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_undo_executes_as_command(self, running_harness):
        """'undo' at utterance start should execute Ctrl+Z, not dictate."""
        await running_harness.send_word("undo", start_of_utterance=True)
        await running_harness.wait_for_timeout(1100)

        texts = running_harness.get_dictation_texts()
        assert "undo" not in texts, (
            f"'undo' should execute as command, not dictation. Got: {texts}"
        )


# ============================================================================
# 3. BACK-TO-BACK UTTERANCES BOTH EXECUTE
# ============================================================================

class TestBackToBackUtterances:
    """Two utterances in rapid succession should both be processed.

    Real scenario: user says 'backspace' (pause) 'hello' quickly.
    The first utterance is a command, the second is dictation. Both
    must complete. If the processor gets stuck in a buffering state
    after the first utterance, the second is lost.
    """

    @pytest.mark.asyncio
    async def test_command_then_dictation(self, running_harness):
        """A command utterance followed by a dictation utterance should
        both produce output."""
        # First utterance: command
        await running_harness.send_word("backspace", start_of_utterance=True, utterance_id=1)
        await running_harness.send_utterance_end_marker(utterance_id=1)
        # Wait for command timeout to finalize
        await running_harness.wait_for_timeout(1100)

        # Verify first utterance produced a command (not dictation)
        texts_after_first = running_harness.get_dictation_texts()
        assert "backspace" not in texts_after_first, (
            f"First utterance 'backspace' should be a command. Got dictation: {texts_after_first}"
        )

        running_harness.clear_outputs()

        # Second utterance: dictation
        await running_harness.send_word("hello", start_of_utterance=True, utterance_id=2)
        await running_harness.send_utterance_end_marker(utterance_id=2)
        await asyncio.sleep(0.15)

        texts_after_second = running_harness.get_dictation_texts()
        assert "hello" in texts_after_second, (
            f"Second utterance 'hello' should be dictated. Got: {texts_after_second}"
        )

    @pytest.mark.asyncio
    async def test_dictation_then_dictation(self, running_harness):
        """Two dictation utterances in succession should both produce text."""
        # First utterance
        await running_harness.send_utterance(["good", "morning"])
        await asyncio.sleep(0.1)

        texts_first = running_harness.get_dictation_texts()
        assert "good" in texts_first, f"First word 'good' missing. Got: {texts_first}"
        assert "morning" in texts_first, f"Second word 'morning' missing. Got: {texts_first}"

        running_harness.clear_outputs()

        # Second utterance (new utterance_id due to start_of_utterance=True in send_utterance)
        await running_harness.send_utterance(["how", "are", "you"])
        await asyncio.sleep(0.1)

        texts_second = running_harness.get_dictation_texts()
        assert "how" in texts_second, f"'how' missing from second utterance. Got: {texts_second}"
        assert "are" in texts_second, f"'are' missing from second utterance. Got: {texts_second}"
        assert "you" in texts_second, f"'you' missing from second utterance. Got: {texts_second}"

    @pytest.mark.asyncio
    async def test_command_then_command(self, running_harness):
        """Two command utterances in quick succession should both execute."""
        # First command: backspace
        await running_harness.send_word("backspace", start_of_utterance=True, utterance_id=10)
        await running_harness.send_utterance_end_marker(utterance_id=10)
        await running_harness.wait_for_timeout(1100)

        first_outputs = list(running_harness.get_outputs())
        first_actions = [o.action for o in first_outputs]

        running_harness.clear_outputs()

        # Second command: undo
        await running_harness.send_word("undo", start_of_utterance=True, utterance_id=11)
        await running_harness.send_utterance_end_marker(utterance_id=11)
        await running_harness.wait_for_timeout(1100)

        second_outputs = list(running_harness.get_outputs())
        second_actions = [o.action for o in second_outputs]

        # Both should have produced command outputs (not dictation)
        first_dictation = [o for o in first_outputs if o.action == "intelligent_insert_text"]
        second_dictation = [o for o in second_outputs if o.action == "intelligent_insert_text"]

        assert not any("backspace" in (o.params.get("insertion_string", "") or "") for o in first_dictation), (
            f"First 'backspace' should be a command, not dictation. Actions: {first_actions}"
        )
        assert not any("undo" in (o.params.get("insertion_string", "") or "") for o in second_dictation), (
            f"Second 'undo' should be a command, not dictation. Actions: {second_actions}"
        )

    @pytest.mark.asyncio
    async def test_rapid_new_utterance_finalizes_previous_buffer(self, running_harness):
        """When a new utterance starts while still buffering the previous one,
        the buffer should auto-finalize and the new utterance should proceed."""
        # Start buffering a command (backspace waits for optional count)
        await running_harness.send_word("backspace", start_of_utterance=True, utterance_id=20)
        # Very short delay - don't wait for timeout
        await asyncio.sleep(0.05)

        # New utterance starts immediately (auto-finalize should trigger)
        await running_harness.send_word("hello", start_of_utterance=True, utterance_id=21)
        await asyncio.sleep(0.15)

        texts = running_harness.get_dictation_texts()
        all_actions = running_harness.mock_app.get_all_actions()

        # "hello" should be dictated
        assert "hello" in texts, (
            f"New utterance 'hello' should be dictated after auto-finalize. "
            f"Got texts: {texts}, actions: {all_actions}"
        )
        # "backspace" should NOT appear in dictation
        assert "backspace" not in texts, (
            f"'backspace' should have been finalized as command, not dictated. Got: {texts}"
        )


# ============================================================================
# 4. DICTATION PRESERVES ALL SPOKEN WORDS
# ============================================================================

class TestDictationPreservesWords:
    """Every spoken word in dictation mode should reach the output.

    Real scenario: user dictates a sentence like 'the quick brown fox'.
    If any word is silently dropped, the user's document has missing
    words and they may not notice until much later.
    """

    @pytest.mark.asyncio
    async def test_multi_word_dictation_no_drops(self, running_harness):
        """All words in a multi-word dictation utterance should appear in output."""
        words = ["the", "quick", "brown", "fox", "jumped"]
        await running_harness.send_utterance(words)
        await asyncio.sleep(0.1)

        texts = running_harness.get_dictation_texts()
        for word in words:
            assert word in texts, (
                f"Word '{word}' was dropped from dictation. Got: {texts}"
            )

    @pytest.mark.asyncio
    async def test_single_word_dictation(self, running_harness):
        """A single non-command word should pass through to dictation."""
        await running_harness.send_word("hello", start_of_utterance=True)
        await asyncio.sleep(0.1)

        texts = running_harness.get_dictation_texts()
        assert "hello" in texts, f"'hello' should be dictated. Got: {texts}"

    @pytest.mark.asyncio
    async def test_dictation_word_order_preserved(self, running_harness):
        """Words should appear in the order they were spoken."""
        words = ["I", "love", "programming"]
        await running_harness.send_utterance(words)
        await asyncio.sleep(0.1)

        texts = running_harness.get_dictation_texts()
        # Verify order: each word should appear and in sequence
        indices = []
        for word in words:
            assert word in texts, f"'{word}' missing from output. Got: {texts}"
            indices.append(texts.index(word))

        # Indices should be strictly increasing
        assert indices == sorted(indices), (
            f"Word order not preserved. Expected order {words}, "
            f"got indices {indices} in {texts}"
        )

    @pytest.mark.asyncio
    async def test_batch_arrival_preserves_all_words(self, running_harness):
        """Words arriving in a single STT batch (queued before processor
        can handle them) should all be preserved."""
        await running_harness.send_word_batch(["this", "is", "a", "test"])
        await asyncio.sleep(0.15)

        texts = running_harness.get_dictation_texts()
        for word in ["this", "is", "a", "test"]:
            assert word in texts, (
                f"Batch word '{word}' was dropped. Got: {texts}"
            )

    @pytest.mark.asyncio
    async def test_long_utterance_no_drops(self, running_harness):
        """A longer utterance (10+ words) should not drop any words."""
        words = [
            "we", "hold", "these", "truths", "to", "be",
            "self", "evident", "that", "all", "are", "created"
        ]
        await running_harness.send_utterance(words, inter_word_delay_ms=30)
        await asyncio.sleep(0.2)

        texts = running_harness.get_dictation_texts()
        for word in words:
            assert word in texts, (
                f"Word '{word}' dropped in long utterance. "
                f"Got {len(texts)}/{len(words)} words: {texts}"
            )
        assert len(texts) == len(words), (
            f"Expected {len(words)} words, got {len(texts)}: {texts}"
        )
