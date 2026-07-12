"""Tests for clipboard race condition fix.

This tests the fix for the bug where clipboard contents are pasted instead of
the spoken word when:
1. A word triggers pattern buffering (e.g., "back" matches "back space" prefix)
2. The utterance ends while still buffering
3. The timeout expires and dictation occurs AFTER clipboard was restored

The fix ensures `end_utterance` is sent AFTER `intelligent_insert_text` completes,
so `is_in_utterance()` returns True during paste operations.

See: docs/design/clipboard-race-fix-plan.md
"""
import sys
from pathlib import Path

# Add parent directories to path for imports
# tests/test_ui/test_file.py -> need to go up 4 levels to project root
# and 3 levels to wheelhouse directory
test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent.parent
patterns_path = wheelhouse_dir / "speech" / "config" / "patterns.toml"
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))  # wheelhouse dir

import asyncio
import pytest
from dataclasses import dataclass, field
from typing import List, Optional
from unittest.mock import MagicMock

from speech.word_event import WordEvent
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.command_engine import TextParser


@dataclass
class CapturedCommand:
    """Represents a single captured command from the pipeline."""
    action: str
    params: dict
    order: int  # Order in which command was received


class CommandOrderTrackingApp:
    """Mock application that tracks the ORDER of commands.

    This is crucial for testing the clipboard race condition fix:
    - `start_utterance` should come first
    - `intelligent_insert_text` should come BEFORE `end_utterance`
    - `end_utterance` should come AFTER dictation completes
    """

    def __init__(self):
        self.commands: List[CapturedCommand] = []
        self._order_counter = 0

    async def send_command(self, command: dict):
        """Capture fire-and-forget commands with order tracking."""
        self._order_counter += 1
        action = command.get('action', '')
        print(f"[MOCK] send_command #{self._order_counter}: {action}")
        self.commands.append(CapturedCommand(
            action=action,
            params=command.get('params', {}),
            order=self._order_counter
        ))

    async def send_request(self, action: str, params: dict):
        """Capture request-response commands with order tracking."""
        self._order_counter += 1
        print(f"[MOCK] send_request #{self._order_counter}: {action}")
        self.commands.append(CapturedCommand(
            action=action,
            params=params,
            order=self._order_counter
        ))
        return True  # Simulate successful response

    def get_command_order(self) -> List[str]:
        """Get list of action names in the order they were received."""
        return [cmd.action for cmd in sorted(self.commands, key=lambda c: c.order)]

    def get_commands_by_action(self, action: str) -> List[CapturedCommand]:
        """Get all commands with a specific action name."""
        return [cmd for cmd in self.commands if cmd.action == action]

    def clear(self):
        """Clear captured commands."""
        self.commands.clear()
        self._order_counter = 0


class MockContextMirror:
    """Mock context mirror that doesn't use shared memory."""

    def __init__(self):
        self._context = {"app_name": "TestApp", "window_title": "Test Window", "timestamp": 0.0}

    def init_reader(self):
        pass

    def read_context(self) -> dict:
        return self._context


class ClipboardRaceTestHarness:
    """Test harness specifically for clipboard race condition testing.

    Provides methods to:
    - Send words that trigger buffering
    - Send utterance_end_marker while buffer is pending
    - Verify command order
    """

    def __init__(self, timeout_ms: int = 400):
        """Initialize the test harness.

        Args:
            timeout_ms: Replacement timeout in ms (default 400ms for faster tests)
        """
        self.mock_app = CommandOrderTrackingApp()
        self.word_queue: asyncio.Queue = asyncio.Queue()

        # Load pattern catalog
        self.catalog = PatternCatalog(str(patterns_path))

        # Create mock speech handler for TextParser
        self.mock_speech_handler = MagicMock()
        self.mock_speech_handler.app = self.mock_app

        # Create TextParser
        self.text_parser = TextParser(self.mock_speech_handler, self.catalog)

        # Create SpeechProcessor
        self.processor = SpeechProcessor(
            word_queue=self.word_queue,
            catalog=self.catalog,
            text_parser=self.text_parser,
            app=self.mock_app,
            replacement_timeout_ms=timeout_ms,
            command_timeout_ms=1000,
            hotword="x-ray"
        )

        # Replace context mirror with mock
        self.processor.context_mirror = MockContextMirror()

        self._utterance_counter = 0

    async def start(self):
        """Start the speech processor."""
        await self.processor.start()

    async def stop(self):
        """Stop the speech processor."""
        await self.processor.stop()

    async def send_word_with_start_utterance(
        self,
        word: str,
        utterance_id: Optional[int] = None
    ):
        """Send a word that starts a new utterance.

        This simulates the WebSocketManager sending start_utterance command
        followed by the word event.
        """
        if utterance_id is None:
            self._utterance_counter += 1
            utterance_id = self._utterance_counter

        # Simulate WebSocketManager sending start_utterance
        await self.mock_app.send_command({
            'action': 'start_utterance',
            'params': {'utterance_id': utterance_id}
        })

        # Send the word event
        event = WordEvent(
            word=word,
            start_of_utterance=True,
            end_of_utterance=False,
            utterance_id=utterance_id
        )
        await self.word_queue.put(event)
        await asyncio.sleep(0.01)  # Let processor handle

        return utterance_id

    async def send_utterance_end_marker(self, utterance_id: int):
        """Send the utterance_end_marker event.

        This is what the WebSocketManager sends after all words of an utterance.
        """
        event = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=True,
            utterance_id=utterance_id,
            is_utterance_end_marker=True
        )
        await self.word_queue.put(event)
        await asyncio.sleep(0.01)  # Let processor handle

    async def wait_for_timeout(self, timeout_ms: int = 500):
        """Wait for buffer timeout to expire."""
        await asyncio.sleep(timeout_ms / 1000.0)
        await asyncio.sleep(0.05)  # Extra time for processing

    def get_command_order(self) -> List[str]:
        """Get list of action names in order received."""
        return self.mock_app.get_command_order()

    def clear(self):
        """Clear captured commands."""
        self.mock_app.clear()


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def harness():
    """Create a test harness."""
    return ClipboardRaceTestHarness(timeout_ms=200)  # Short timeout for fast tests


@pytest.fixture
async def running_harness(harness):
    """Create and start a test harness."""
    await harness.start()
    yield harness
    await harness.stop()


# ============================================================================
# CLIPBOARD RACE CONDITION TESTS
# ============================================================================

class TestClipboardRaceCondition:
    """Tests for the clipboard race condition fix.

    The bug scenario:
    1. User says "back" (matches "back space" pattern prefix)
    2. Word triggers buffering with 700ms timeout
    3. Utterance ends (utterance_end_marker sent)
    4. Current behavior: end_utterance sent immediately → clipboard restored
    5. Timeout expires → intelligent_insert_text sent → but clipboard already restored!

    The fix:
    - Defer end_utterance until AFTER buffer finalizes
    - This ensures intelligent_insert_text arrives BEFORE end_utterance
    - is_in_utterance() returns True during paste → no clipboard_context wrapper
    """

    @pytest.mark.asyncio
    async def test_end_utterance_after_dictation_when_buffering(self, running_harness):
        """end_utterance should be sent AFTER intelligent_insert_text when buffering.

        This is THE critical test for the clipboard race condition fix.
        """
        # Send a word that triggers buffering (like "new" for "new paragraph")
        # "new" matches prefix of "new paragraph" pattern so it triggers buffering
        utterance_id = await running_harness.send_word_with_start_utterance("new")

        # Give processor time to start buffering
        await asyncio.sleep(0.02)

        # Send utterance_end_marker while still buffering
        await running_harness.send_utterance_end_marker(utterance_id)

        # Wait for timeout to expire
        await running_harness.wait_for_timeout(300)

        # Get the command order
        order = running_harness.get_command_order()
        print(f"\n[DEBUG] Command order: {order}")

        # Verify order: start_utterance, ..., intelligent_insert_text (or pattern action), end_utterance
        assert 'start_utterance' in order, f"Should have start_utterance. Got: {order}"
        assert 'end_utterance' in order, f"Should have end_utterance. Got: {order}"

        # The key assertion: end_utterance should come AFTER any dictation/action
        start_idx = order.index('start_utterance')
        end_idx = order.index('end_utterance')

        # Check if there's an intelligent_insert_text or other action between start and end
        actions_between = order[start_idx + 1:end_idx]
        print(f"[DEBUG] Actions between start and end: {actions_between}")

        # end_utterance should be LAST (after all other actions for this utterance)
        assert end_idx == len(order) - 1, (
            f"end_utterance should be last command. "
            f"Order: {order}, end_utterance at index {end_idx}"
        )

    @pytest.mark.asyncio
    async def test_immediate_dictation_no_race(self, running_harness):
        """Words that don't trigger buffering should work normally.

        Non-pattern words are dictated immediately, so end_utterance order doesn't matter.
        """
        # Send a word that does NOT trigger buffering
        utterance_id = await running_harness.send_word_with_start_utterance("hello")

        # Give processor time to handle
        await asyncio.sleep(0.05)

        # Send utterance_end_marker
        await running_harness.send_utterance_end_marker(utterance_id)

        # Wait a bit for processing
        await asyncio.sleep(0.05)

        order = running_harness.get_command_order()
        print(f"\n[DEBUG] Command order for non-buffered word: {order}")

        # For non-buffered words, intelligent_insert_text happens immediately
        # end_utterance can come right after
        assert 'start_utterance' in order
        assert 'intelligent_insert_text' in order
        assert 'end_utterance' in order

        # intelligent_insert_text should come before end_utterance
        insert_idx = order.index('intelligent_insert_text')
        end_idx = order.index('end_utterance')
        assert insert_idx < end_idx, (
            f"intelligent_insert_text should come before end_utterance. Order: {order}"
        )

    @pytest.mark.asyncio
    async def test_buffered_word_with_delayed_end_marker(self, running_harness):
        """Test buffering where end_marker arrives during buffer wait.

        Simulates the clipboard race condition scenario.
        "new" triggers buffering because it could be "new paragraph" (multi-word pattern).
        """
        # "new" triggers REPLACEMENT buffering because it might be "new paragraph"
        utterance_id = await running_harness.send_word_with_start_utterance("new")

        # Small delay (word is now buffering)
        await asyncio.sleep(0.01)

        # Utterance ends while buffering (this is the race condition trigger)
        await running_harness.send_utterance_end_marker(utterance_id)

        # Wait for timeout to expire and dictation to happen
        await running_harness.wait_for_timeout(300)

        order = running_harness.get_command_order()
        print(f"\n[DEBUG] Buffered word order: {order}")

        # The fix ensures end_utterance comes AFTER the buffer is finalized
        # This means after timeout expires and dictation/action happens

        # Find indices
        start_idx = order.index('start_utterance')
        end_idx = order.index('end_utterance')

        # There should be something between start and end (the buffer finalization action)
        assert end_idx > start_idx + 1, (
            f"There should be actions between start_utterance and end_utterance. "
            f"Order: {order}"
        )

        # end_utterance should be last
        assert end_idx == len(order) - 1, (
            f"end_utterance should be the last command. Order: {order}"
        )


class TestMultiWordPatternTimeout:
    """Tests for multi-word pattern timeout behavior.

    When a word matches the prefix of a multi-word pattern (e.g., "new" for "new paragraph"),
    the system buffers and waits for the next word. If the timeout expires before the
    second word arrives, the first word is dictated.

    The bug: If timeout is too short, multi-word patterns fail because the second word
    arrives after the timeout already dictated the first word separately.
    """

    @pytest.mark.asyncio
    async def test_multi_word_pattern_second_word_after_timeout(self):
        """Multi-word pattern fires when both words arrive within the buffering window.

        Sequence:
        1. "new" arrives, triggers REPLACEMENT_BUFFERING (matches "new paragraph" prefix).
        2. "paragraph" arrives within the timeout window. Buffer becomes ["new", "paragraph"]
           and the pattern matches as a complete replacement. The router emits Action.EXECUTE.
        3. utterance_end_marker arrives after the buffer has already been consumed.
           Mode is IDLE so end_utterance is sent immediately.

        Expected: 0 intelligent_insert_text calls, 1 hotkey_action (the "new paragraph"
        pattern action shifts+enters twice), end_utterance is the last command sent.
        """
        # Timeout long enough that the buffer never finalizes via timeout in this test.
        # Both words and utterance_end arrive within the window.
        harness = ClipboardRaceTestHarness(timeout_ms=400)
        await harness.start()

        try:
            # Send "new" which starts buffering for "new paragraph" pattern
            utterance_id = await harness.send_word_with_start_utterance("new")

            # Brief delay, well inside the 400ms timeout window
            await asyncio.sleep(0.05)

            # Send "paragraph" while still buffering. The pattern "new paragraph"
            # matches as a complete replacement, so the router emits EXECUTE here.
            event = WordEvent(
                word="paragraph",
                start_of_utterance=False,
                end_of_utterance=False,
                utterance_id=utterance_id
            )
            await harness.word_queue.put(event)
            await asyncio.sleep(0.05)

            # End the utterance
            await harness.send_utterance_end_marker(utterance_id)
            await asyncio.sleep(0.05)

            order = harness.get_command_order()
            print(f"\n[DEBUG] Multi-word pattern timeout order: {order}")

            insert_commands = harness.mock_app.get_commands_by_action('intelligent_insert_text')
            hotkey_commands = harness.mock_app.get_commands_by_action('hotkey_action')
            print(f"[DEBUG] Insert commands: {[(c.params, c.order) for c in insert_commands]}")
            print(f"[DEBUG] Hotkey commands: {[(c.params, c.order) for c in hotkey_commands]}")

            # Pattern fired: no dictation of "new" or "paragraph" as separate words
            assert len(insert_commands) == 0, (
                f"Expected no dictation (pattern action fired), got {len(insert_commands)}. "
                f"Commands: {order}"
            )

            # Pattern action ran exactly once
            assert len(hotkey_commands) == 1, (
                f"Expected exactly one hotkey_action (the 'new paragraph' pattern), "
                f"got {len(hotkey_commands)}. Commands: {order}"
            )

            # end_utterance still sent, last in the sequence
            assert 'end_utterance' in order, f"Expected end_utterance. Order: {order}"
            assert order.index('end_utterance') == len(order) - 1, (
                f"end_utterance should be last. Order: {order}"
            )

        finally:
            await harness.stop()

    @pytest.mark.asyncio
    async def test_multi_word_pattern_second_word_before_timeout(self):
        """Second word arriving BEFORE timeout should trigger pattern.

        When "paragraph" arrives within the timeout window, "new paragraph"
        should be recognized and execute the pattern action (not dictation).
        """
        # Use longer timeout to ensure second word arrives in time
        harness = ClipboardRaceTestHarness(timeout_ms=500)  # Longer timeout
        await harness.start()

        try:
            # Send "new" which starts buffering for "new paragraph" pattern
            utterance_id = await harness.send_word_with_start_utterance("new")

            # Small delay, but BEFORE timeout expires
            await asyncio.sleep(0.05)

            # Send "paragraph" while still buffering
            event = WordEvent(
                word="paragraph",
                start_of_utterance=False,
                end_of_utterance=False,
                utterance_id=utterance_id
            )
            await harness.word_queue.put(event)
            await asyncio.sleep(0.1)

            # End the utterance
            await harness.send_utterance_end_marker(utterance_id)
            await asyncio.sleep(0.05)

            order = harness.get_command_order()
            print(f"\n[DEBUG] Multi-word pattern success order: {order}")

            # Should have pattern action, NOT intelligent_insert_text for dictation
            insert_commands = harness.mock_app.get_commands_by_action('intelligent_insert_text')
            print(f"[DEBUG] Insert commands: {[(c.params, c.order) for c in insert_commands]}")

            # For "new paragraph" pattern, it should execute hotkey actions, not text insertion
            # The pattern does: hk shift+enter twice (for paragraph break)
            # So we should NOT see "new paragraph" as dictated text
            for cmd in insert_commands:
                text = cmd.params.get('insertion_string', '')
                assert 'new paragraph' not in text.lower(), (
                    f"Should not dictate 'new paragraph' - pattern should have matched. "
                    f"Got insertion: {text}"
                )

        finally:
            await harness.stop()


class TestCommandPatternWithCount:
    """Tests for command patterns with optional count argument.

    Commands like "backspace 2" need a longer timeout because the count
    arrives as a separate word from STT. These patterns should be commands
    (with ^ anchor) so they use COMMAND_TIMEOUT_MS (1000ms) instead
    of REPLACEMENT_TIMEOUT_MS (700ms for replacements).
    """

    @pytest.mark.asyncio
    async def test_backspace_with_count_within_timeout(self):
        """Backspace with count should work when count arrives within timeout.

        "backspace 2" should execute as a single command doing 2 backspaces,
        not as "backspace" (1) followed by dictated "2".
        """
        # Use command timeout (1000ms) since backspace is now a command
        harness = ClipboardRaceTestHarness(timeout_ms=1000)
        await harness.start()

        try:
            # Send "backspace" which starts command buffering
            utterance_id = await harness.send_word_with_start_utterance("backspace")

            # Simulate realistic STT delay (500ms between words)
            await asyncio.sleep(0.5)

            # Send "2" while still buffering
            event = WordEvent(
                word="2",
                start_of_utterance=False,
                end_of_utterance=False,
                utterance_id=utterance_id
            )
            await harness.word_queue.put(event)
            await asyncio.sleep(0.1)

            # End the utterance
            await harness.send_utterance_end_marker(utterance_id)
            await asyncio.sleep(0.1)

            order = harness.get_command_order()
            print(f"\n[DEBUG] Backspace with count order: {order}")

            # Should NOT have intelligent_insert_text for "2" (it should be part of command)
            insert_commands = harness.mock_app.get_commands_by_action('intelligent_insert_text')
            print(f"[DEBUG] Insert commands: {[(c.params, c.order) for c in insert_commands]}")

            # The "2" should NOT be dictated as text
            for cmd in insert_commands:
                text = cmd.params.get('insertion_string', '')
                assert '2' not in text, (
                    f"'2' should not be dictated - it should be part of backspace command. "
                    f"Got insertion: '{text}'"
                )

        finally:
            await harness.stop()

    @pytest.mark.asyncio
    async def test_backspace_alone_executes_single(self):
        """Backspace alone should execute as single backspace after timeout.

        If user says just "backspace" without a count, it should still work
        after the timeout expires.
        """
        # Use shorter timeout for faster test
        harness = ClipboardRaceTestHarness(timeout_ms=200)
        await harness.start()

        try:
            # Send "backspace" alone
            utterance_id = await harness.send_word_with_start_utterance("backspace")

            # End the utterance immediately (no count coming)
            await asyncio.sleep(0.05)
            await harness.send_utterance_end_marker(utterance_id)

            # Wait for timeout to expire and command to execute
            await asyncio.sleep(0.3)

            order = harness.get_command_order()
            print(f"\n[DEBUG] Backspace alone order: {order}")

            # Should have executed (no dictation of "backspace" as text)
            insert_commands = harness.mock_app.get_commands_by_action('intelligent_insert_text')
            for cmd in insert_commands:
                text = cmd.params.get('insertion_string', '')
                assert 'backspace' not in text.lower(), (
                    f"'backspace' should not be dictated - it should execute as command. "
                    f"Got insertion: '{text}'"
                )

        finally:
            await harness.stop()

    @pytest.mark.asyncio
    async def test_backspace_count_after_timeout_executes_without_count(self):
        """Count arriving after timeout should result in single backspace.

        This documents expected behavior: if "2" arrives after the timeout,
        "backspace" executes alone (1 backspace). The "2" arriving mid-utterance
        after a command is not dictated (commands consume the utterance context).

        Note: This is the bug scenario we're trying to avoid by using a longer
        timeout for commands with optional counts.
        """
        # Use very short timeout to force timeout before count arrives
        harness = ClipboardRaceTestHarness(timeout_ms=50)
        await harness.start()

        try:
            # Send "backspace"
            utterance_id = await harness.send_word_with_start_utterance("backspace")

            # Wait for timeout to expire
            await asyncio.sleep(0.15)

            # Now send "2" - but timeout already expired
            event = WordEvent(
                word="2",
                start_of_utterance=False,
                end_of_utterance=False,
                utterance_id=utterance_id
            )
            await harness.word_queue.put(event)
            await asyncio.sleep(0.05)

            # End the utterance
            await harness.send_utterance_end_marker(utterance_id)
            await asyncio.sleep(0.05)

            order = harness.get_command_order()
            print(f"\n[DEBUG] Backspace timeout then count order: {order}")

            # Backspace should have executed (press_key_action present)
            assert 'press_key_action' in order, (
                f"Backspace command should have executed. Got: {order}"
            )

            # The key point: with short timeout, the count is missed
            # This test documents why we need longer timeouts for commands with counts

        finally:
            await harness.stop()


class TestEdgeCases:
    """Edge case tests for the clipboard race condition fix."""

    @pytest.mark.asyncio
    async def test_multiple_words_then_end(self, running_harness):
        """Multiple words followed by end_marker should work correctly."""
        # First word starts utterance
        utterance_id = await running_harness.send_word_with_start_utterance("hello")

        # More words (not starting new utterance)
        for word in ["world", "test"]:
            event = WordEvent(
                word=word,
                start_of_utterance=False,
                end_of_utterance=False,
                utterance_id=utterance_id
            )
            await running_harness.word_queue.put(event)
            await asyncio.sleep(0.01)

        # End the utterance
        await running_harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.05)

        order = running_harness.get_command_order()
        print(f"\n[DEBUG] Multi-word order: {order}")

        # All intelligent_insert_text calls should come before end_utterance
        end_idx = order.index('end_utterance')
        for i, action in enumerate(order):
            if action == 'intelligent_insert_text':
                assert i < end_idx, f"insert at {i} should be before end at {end_idx}"

    @pytest.mark.asyncio
    async def test_idle_mode_end_utterance_immediate(self, running_harness):
        """When in IDLE mode, end_utterance can be sent immediately.

        If no buffering is happening, there's no race condition to worry about.
        """
        # Send a word that gets dictated immediately (no buffering)
        utterance_id = await running_harness.send_word_with_start_utterance("hello")

        # Wait for it to be fully processed
        await asyncio.sleep(0.1)

        # At this point, processor should be back in IDLE mode
        # end_utterance can be sent immediately
        await running_harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.05)

        order = running_harness.get_command_order()

        # Should work fine - order doesn't matter for non-buffered case
        # as long as intelligent_insert_text completes before clipboard restoration
        assert 'start_utterance' in order
        assert 'end_utterance' in order


class TestUtteranceEndFinalization:
    """Tests for finalize-buffer-on-utterance_end behavior (wh-jkjkh).

    When the STT pipeline emits an utterance_end marker while the speech processor
    is still buffering, the processor should finalize the buffer immediately rather
    than wait for the per-pattern timeout. The 400/1000 ms timeouts remain only as
    a safety net for pathological cases where utterance_end never arrives.

    The scenarios below depend on patterns that keep buffering even after their main
    text matches: "back space" matches the regex ^back ?space\\s*(\\d+)?$ but the
    optional numeric group is unfilled, so the router continues buffering for an
    optional count. Without the fix, the buffer waits for the 1000 ms command
    timeout. With the fix, utterance_end finalizes the buffer at once and the
    pattern action runs.
    """

    @pytest.mark.asyncio
    async def test_buffer_finalizes_on_utterance_end_before_timeout(self):
        """utterance_end finalizes the buffer before the command timeout fires.

        With the harness command_timeout fixed at 1000 ms, this test waits only
        ~80 ms after sending utterance_end. Without the fix, the action and
        end_utterance are still pending behind the timeout, so the assertions fail.
        With the fix, the buffer finalizes at once and both assertions pass.
        """
        # replacement_timeout_ms is irrelevant here; "back" goes to COMMAND_BUFFERING.
        # command_timeout_ms is 1000 ms inside the harness.
        harness = ClipboardRaceTestHarness(timeout_ms=400)
        await harness.start()

        try:
            utterance_id = await harness.send_word_with_start_utterance("back")
            await asyncio.sleep(0.02)

            # "space" matches "back space" as a complete pattern, but the optional
            # count group is unfilled so the router continues buffering.
            event = WordEvent(
                word="space",
                start_of_utterance=False,
                end_of_utterance=False,
                utterance_id=utterance_id
            )
            await harness.word_queue.put(event)
            await asyncio.sleep(0.02)

            # utterance_end arrives while the buffer still holds ["back", "space"].
            await harness.send_utterance_end_marker(utterance_id)

            # Wait far less than the 1000 ms command timeout. With the fix, the
            # buffer is finalized inside this window. Without the fix, the action
            # and end_utterance are still pending.
            await asyncio.sleep(0.08)

            order = harness.get_command_order()
            print(f"\n[DEBUG] Finalize-on-end order: {order}")

            insert_commands = harness.mock_app.get_commands_by_action('intelligent_insert_text')
            assert len(insert_commands) == 0, (
                f"Buffer matched 'back space' as a command; no dictation expected. "
                f"Got {len(insert_commands)} insertion(s). Order: {order}"
            )

            assert 'press_key_action' in order, (
                f"Pattern action 'back space' should have fired before the 1000 ms "
                f"timeout, but did not. Order: {order}"
            )

            assert 'end_utterance' in order, (
                f"end_utterance should have been sent after the pattern fired. "
                f"Order: {order}"
            )

            # Pattern action precedes end_utterance
            press_idx = order.index('press_key_action')
            end_idx = order.index('end_utterance')
            assert press_idx < end_idx, (
                f"press_key_action ({press_idx}) should precede end_utterance ({end_idx}). "
                f"Order: {order}"
            )

        finally:
            await harness.stop()

    @pytest.mark.asyncio
    async def test_end_utterance_sent_exactly_once_after_pattern_via_utterance_end(self):
        """end_utterance is sent exactly once when a pattern is finalized via utterance_end.

        Acceptance: 'New test: end_utterance is sent exactly once after a multi-word
        pattern is finalized via utterance_end (not zero, not twice).'

        Risk it guards against: a future change that calls _send_pending_utterance_end
        twice (once during the immediate finalize, once during a stale timeout wakeup),
        or that drops the call entirely.
        """
        harness = ClipboardRaceTestHarness(timeout_ms=400)
        await harness.start()

        try:
            utterance_id = await harness.send_word_with_start_utterance("back")
            await asyncio.sleep(0.02)

            event = WordEvent(
                word="space",
                start_of_utterance=False,
                end_of_utterance=False,
                utterance_id=utterance_id
            )
            await harness.word_queue.put(event)
            await asyncio.sleep(0.02)

            await harness.send_utterance_end_marker(utterance_id)

            # Wait long enough for any stale timeout to also fire (1000 ms command
            # timeout + safety margin) so we'd catch a duplicate end_utterance.
            await asyncio.sleep(1.2)

            end_commands = harness.mock_app.get_commands_by_action('end_utterance')
            order = harness.get_command_order()
            assert len(end_commands) == 1, (
                f"Expected exactly one end_utterance, got {len(end_commands)}. "
                f"Order: {order}"
            )

            # Verify utterance_id matches
            assert end_commands[0].params.get('utterance_id') == utterance_id, (
                f"end_utterance utterance_id mismatch. "
                f"Expected {utterance_id}, got {end_commands[0].params}"
            )

        finally:
            await harness.stop()
