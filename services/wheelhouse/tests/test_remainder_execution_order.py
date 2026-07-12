"""Tests for remainder execution order bug.

Bug: When user says "backspace period", the execution order is reversed:
1. "period" executes first (inserts ".")
2. "backspace" executes second (deletes the ".")

Expected: "backspace" should execute first, then "period".

Root cause: When a replacement pattern matches in the middle of buffered text,
the text BEFORE the match (which arrived first) ends up in `remainder` and
executes AFTER the matched pattern, instead of BEFORE.

These tests verify the correct execution order for various combinations of
commands and replacements.
"""
import sys
from pathlib import Path

# Add parent directories to path for imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))
# Add tests directory directly so sibling test modules are importable
sys.path.insert(0, str(Path(__file__).parent))

import asyncio
import pytest
from typing import List

# Import the test harness from the existing test file
from test_speech_pipeline import SpeechPipelineHarness, CapturedOutput


def get_action_sequence(outputs: List[CapturedOutput]) -> List[str]:
    """Extract a simplified action sequence for verification.

    Returns a list of action descriptions like:
    - 'key:backspace' for keypress actions
    - 'text:.' for text insertion actions
    - 'dictate:hello' for dictation
    """
    result = []
    for out in outputs:
        if out.action == 'keypress':
            key = out.params.get('key', 'unknown')
            repeat = out.params.get('repeat', 1)
            if repeat > 1:
                result.append(f'key:{key}x{repeat}')
            else:
                result.append(f'key:{key}')
        elif out.action == 'intelligent_insert_text':
            text = out.params.get('insertion_string', '')
            result.append(f'text:{text}')
        else:
            result.append(f'{out.action}:{out.params}')
    return result


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
# CORE BUG TESTS: Command followed by Replacement
# ============================================================================

class TestCommandBeforeReplacement:
    """Test that commands execute BEFORE replacements when spoken in that order.

    Bug scenario: "backspace period" should execute:
    1. backspace (delete a character)
    2. period (insert ".")

    But currently executes in reverse order.
    """

    @pytest.mark.asyncio
    async def test_backspace_period_order(self, running_harness):
        """'backspace period' - backspace MUST execute before period.

        This is the primary bug test. The user says "backspace period" expecting:
        1. Delete one character (backspace)
        2. Insert a period

        Current bug: period inserts first, then backspace deletes it.
        """
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("period", start_of_utterance=False, delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        outputs = running_harness.get_outputs()
        actions = get_action_sequence(outputs)

        print(f"\n[DEBUG] Actions: {actions}")
        print(f"[DEBUG] Raw outputs: {[(o.action, o.params) for o in outputs]}")

        # Find the backspace and period actions
        backspace_idx = None
        period_idx = None
        for i, action in enumerate(actions):
            if 'backspace' in action.lower():
                backspace_idx = i
            if action.startswith('text:.') or 'period' in action.lower():
                period_idx = i

        assert backspace_idx is not None, f"Backspace action not found in: {actions}"
        assert period_idx is not None, f"Period action not found in: {actions}"
        assert backspace_idx < period_idx, (
            f"Backspace (idx={backspace_idx}) must execute BEFORE period (idx={period_idx}). "
            f"Actions: {actions}"
        )

    @pytest.mark.asyncio
    async def test_delete_comma_order(self, running_harness):
        """'delete comma' - delete MUST execute before comma."""
        await running_harness.send_word("delete", start_of_utterance=True)
        await running_harness.send_word("comma", start_of_utterance=False, delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        outputs = running_harness.get_outputs()
        actions = get_action_sequence(outputs)

        print(f"\n[DEBUG] Actions: {actions}")

        # Find the delete and comma actions
        # Note: 'delete' command sends 'del' key
        delete_idx = None
        comma_idx = None
        for i, action in enumerate(actions):
            if 'del' in action.lower():
                delete_idx = i
            if action == 'text:,' or ('text:' in action and ',' in action):
                comma_idx = i

        assert delete_idx is not None, f"Delete (del) action not found in: {actions}"
        assert comma_idx is not None, f"Comma action not found in: {actions}"
        assert delete_idx < comma_idx, (
            f"Delete (idx={delete_idx}) must execute BEFORE comma (idx={comma_idx}). "
            f"Actions: {actions}"
        )

    @pytest.mark.asyncio
    async def test_enter_period_order(self, running_harness):
        """'enter period' - enter MUST execute before period."""
        await running_harness.send_word("enter", start_of_utterance=True)
        await running_harness.send_word("period", start_of_utterance=False, delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        outputs = running_harness.get_outputs()
        actions = get_action_sequence(outputs)

        print(f"\n[DEBUG] Actions: {actions}")

        # Find the enter and period actions
        enter_idx = None
        period_idx = None
        for i, action in enumerate(actions):
            if 'enter' in action.lower() or 'return' in action.lower():
                enter_idx = i
            if action.startswith('text:.') or 'period' in action.lower():
                period_idx = i

        assert enter_idx is not None, f"Enter action not found in: {actions}"
        assert period_idx is not None, f"Period action not found in: {actions}"
        assert enter_idx < period_idx, (
            f"Enter (idx={enter_idx}) must execute BEFORE period (idx={period_idx}). "
            f"Actions: {actions}"
        )


# ============================================================================
# REVERSE ORDER: Replacement followed by Command
# ============================================================================

class TestReplacementBeforeCommand:
    """Test that replacements execute BEFORE commands when spoken in that order."""

    @pytest.mark.asyncio
    async def test_period_backspace_order(self, running_harness):
        """'period backspace' - period MUST execute before backspace."""
        await running_harness.send_word("period", start_of_utterance=True)
        await running_harness.send_word("backspace", start_of_utterance=False, delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        outputs = running_harness.get_outputs()
        actions = get_action_sequence(outputs)

        print(f"\n[DEBUG] Actions: {actions}")

        # Find the period and backspace actions
        period_idx = None
        backspace_idx = None
        for i, action in enumerate(actions):
            if action.startswith('text:.') or 'period' in action.lower():
                period_idx = i
            if 'backspace' in action.lower():
                backspace_idx = i

        assert period_idx is not None, f"Period action not found in: {actions}"
        assert backspace_idx is not None, f"Backspace action not found in: {actions}"
        assert period_idx < backspace_idx, (
            f"Period (idx={period_idx}) must execute BEFORE backspace (idx={backspace_idx}). "
            f"Actions: {actions}"
        )


# ============================================================================
# MIXED: Text + Replacement + Text
# ============================================================================

class TestMixedTextAndReplacement:
    """Test that text before and after replacements is handled correctly."""

    @pytest.mark.asyncio
    async def test_hello_period_world(self, running_harness):
        """'hello period world' - order: hello dictated, period executes, world dictated."""
        await running_harness.send_word("hello", start_of_utterance=True)
        await running_harness.send_word("period", start_of_utterance=False, delay_before_ms=50)
        await running_harness.send_word("world", start_of_utterance=False, delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        outputs = running_harness.get_outputs()
        actions = get_action_sequence(outputs)

        print(f"\n[DEBUG] Actions: {actions}")

        # Find indices for hello, period, world
        hello_idx = None
        period_idx = None
        world_idx = None
        for i, action in enumerate(actions):
            if 'hello' in action.lower():
                hello_idx = i
            if action.startswith('text:.') or ('text:' in action and '.' in action):
                period_idx = i
            if 'world' in action.lower():
                world_idx = i

        assert hello_idx is not None, f"'hello' not found in: {actions}"
        assert period_idx is not None, f"Period not found in: {actions}"
        assert world_idx is not None, f"'world' not found in: {actions}"

        assert hello_idx < period_idx, (
            f"'hello' (idx={hello_idx}) must come BEFORE period (idx={period_idx})"
        )
        assert period_idx < world_idx, (
            f"Period (idx={period_idx}) must come BEFORE 'world' (idx={world_idx})"
        )


# ============================================================================
# BATCH ARRIVAL: Simulates real STT behavior
# ============================================================================

class TestBatchArrival:
    """Test correct ordering when words arrive in a batch (real STT behavior)."""

    @pytest.mark.asyncio
    async def test_backspace_period_batch(self, running_harness):
        """'backspace period' arriving as batch - same order requirement."""
        await running_harness.send_word_batch(["backspace", "period"])
        await running_harness.wait_for_timeout(500)

        outputs = running_harness.get_outputs()
        actions = get_action_sequence(outputs)

        print(f"\n[DEBUG BATCH] Actions: {actions}")

        # Find the backspace and period actions
        backspace_idx = None
        period_idx = None
        for i, action in enumerate(actions):
            if 'backspace' in action.lower():
                backspace_idx = i
            if action.startswith('text:.') or 'period' in action.lower():
                period_idx = i

        assert backspace_idx is not None, f"Backspace action not found in: {actions}"
        assert period_idx is not None, f"Period action not found in: {actions}"
        assert backspace_idx < period_idx, (
            f"Backspace (idx={backspace_idx}) must execute BEFORE period (idx={period_idx}). "
            f"Actions: {actions}"
        )


# ============================================================================
# RUN TESTS
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
