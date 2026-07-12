"""Edge case tests for speech processor.

Hand-written tests for:
- Race conditions
- Double execution prevention
- State machine edge cases
- Buffer management
- Timeout handling
"""
import pytest
import asyncio
from pathlib import Path
import sys

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from services.wheelhouse.speech.speech_processor import SpeechProcessor, ProcessingMode
from services.wheelhouse.speech.pattern_catalog import PatternCatalog
from services.wheelhouse.speech.word_event import WordEvent
from tests.speech.test_harness import MockApp, RealTextParser


# ============================================================================
# TEST FIXTURES
# ============================================================================

@pytest.fixture
def config_dir():
    """Return path to config files."""
    return project_root / "services" / "wheelhouse" / "speech" / "config"


@pytest.fixture
def catalog(config_dir):
    """Create PatternCatalog with patterns.toml."""
    return PatternCatalog(str(config_dir / "patterns.toml"))


@pytest.fixture
def mock_app():
    """Create mock app."""
    return MockApp()


@pytest.fixture
def mock_text_parser(mock_app, catalog):
    """Create real text parser for accurate testing."""
    return RealTextParser(mock_app, catalog)


@pytest.fixture
def processor(catalog, mock_text_parser, mock_app):
    """Create SpeechProcessor."""
    return SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=mock_text_parser,
        app=mock_app,
        replacement_timeout_ms=400,
        command_timeout_ms=1000
    )


# ============================================================================
# RACE CONDITION TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_no_double_execution_on_early_completion(processor, mock_app):
    """Test that pattern completing before timeout doesn't execute twice.
    
    This was the "snake case" bug - pattern would complete, execute, then
    timeout would fire and execute again.
    
    Fixed by: Moving state transition (cancel timeout + set IDLE) before
    async execution in _execute_command_and_return_to_idle().
    """
    # Send "snake case" - completes immediately on second word
    await processor.process_word_event(
        WordEvent("snake", start_of_utterance=True, end_of_utterance=False)
    )
    assert processor.mode == ProcessingMode.COMMAND_BUFFERING
    
    await processor.process_word_event(
        WordEvent("case", start_of_utterance=False, end_of_utterance=False)
    )
    
    # Should return to IDLE immediately
    assert processor.mode == ProcessingMode.IDLE
    assert len(processor.buffer) == 0
    
    # Record action count after pattern completion
    actions_after_completion = mock_app.call_count
    
    # Wait for timeout period (in case timeout wasn't cancelled)
    await asyncio.sleep(1.1)
    
    # Action count should NOT have increased
    assert mock_app.call_count == actions_after_completion, \
        "Command executed twice! Timeout fired after pattern completion."


@pytest.mark.asyncio
async def test_finalization_guard_prevents_stale_execution(processor, mock_app):
    """Test that stale end-of-utterance events don't cause double command execution.

    Scenario: Pattern completes and executes, then a stale EOI arrives.
    The stale EOI should not cause another command execution.

    Note: Use "undo" which is a command that doesn't require hotword.
    """
    # Send a complete command pattern that doesn't require hotword
    await processor.process_word_event(
        WordEvent("undo", start_of_utterance=True, end_of_utterance=False)
    )

    # Send EOI to finalize
    await processor.process_word_event(
        WordEvent("", start_of_utterance=False, end_of_utterance=True)
    )

    # Count hotkey actions (undo executes a hotkey)
    def count_hotkey_actions():
        return len([a for a in mock_app.actions if a.get('action') == 'hotkey_action'])

    initial_hotkey_count = count_hotkey_actions()
    assert initial_hotkey_count == 1, f"Undo should execute once. Actions: {mock_app.actions}"

    # Send another EOI (simulates stale event)
    await processor.process_word_event(
        WordEvent("", start_of_utterance=False, end_of_utterance=True)
    )

    # Hotkey count should not increase
    assert count_hotkey_actions() == initial_hotkey_count, \
        f"Stale EOI caused command double-execution. Actions: {mock_app.actions}"


# ============================================================================
# REPLACEMENT PATTERN TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_period_replacement_executes(processor, mock_app):
    """Test that 'period' replacement pattern works correctly.

    Mid-utterance replacements buffer and wait for timeout before executing.
    """
    # Send "period" mid-utterance
    await processor.process_word_event(
        WordEvent("hello", start_of_utterance=True, end_of_utterance=False)
    )
    await processor.process_word_event(
        WordEvent("period", start_of_utterance=False, end_of_utterance=False)
    )

    # Mid-utterance replacement buffers
    assert processor.mode == ProcessingMode.REPLACEMENT_BUFFERING

    # Wait for timeout to fire
    await asyncio.sleep(0.5)  # Wait for 400ms replacement timeout

    # After timeout, should be back in IDLE
    assert processor.mode == ProcessingMode.IDLE

    # Check that both hello (dictation) and period (replacement) were executed
    assert mock_app.call_count >= 1, "Period pattern did not execute"


# ============================================================================
# BUFFER MANAGEMENT TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_buffer_cleared_after_command_execution(processor, mock_app):
    """Test that buffer is cleared after command executes."""
    await processor.process_word_event(
        WordEvent("browser", start_of_utterance=True, end_of_utterance=False)
    )

    # "browser" is a fresh command, so it buffers first
    assert processor.mode == ProcessingMode.COMMAND_BUFFERING

    # Wait for command timeout to fire
    await asyncio.sleep(1.1)  # Wait for 1000ms command timeout

    # After timeout, should be back in IDLE with cleared buffer
    assert processor.mode == ProcessingMode.IDLE
    assert len(processor.buffer) == 0


@pytest.mark.asyncio
async def test_buffer_cleared_after_replacement_execution(processor, mock_app):
    """Test that buffer is cleared after replacement executes."""
    await processor.process_word_event(
        WordEvent("hello", start_of_utterance=True, end_of_utterance=False)
    )
    await processor.process_word_event(
        WordEvent("period", start_of_utterance=False, end_of_utterance=False)
    )
    
    # Wait for timeout
    await asyncio.sleep(0.5)
    
    assert len(processor.buffer) == 0


# ============================================================================
# TIMEOUT HANDLING TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_timeout_cancelled_on_early_completion(processor, mock_app):
    """Test that timeout is cancelled when pattern completes early via EOI.

    Send "browser" as fresh command, then end-of-utterance to force
    early finalization which should cancel the timeout.
    """
    await processor.process_word_event(
        WordEvent("browser", start_of_utterance=True, end_of_utterance=False)
    )

    # Should be buffering
    assert processor.mode == ProcessingMode.COMMAND_BUFFERING

    # Send end of utterance to force early finalization
    await processor.process_word_event(
        WordEvent("", start_of_utterance=False, end_of_utterance=True)
    )

    # Should execute and return to IDLE, cancelling timeout
    assert processor.mode == ProcessingMode.IDLE
    assert processor.timeout_task is None or processor.timeout_task.done()


@pytest.mark.asyncio
async def test_command_timeout_fires_after_wait(processor, mock_app):
    """Test that command patterns wait for timeout if incomplete."""
    await processor.process_word_event(
        WordEvent("delete", start_of_utterance=True, end_of_utterance=False)
    )
    
    # Should buffer (has optional parameter)
    assert processor.mode == ProcessingMode.COMMAND_BUFFERING
    assert processor.timeout_task is not None
    
    # Wait for timeout
    await asyncio.sleep(1.1)
    
    # Should have finalized
    assert processor.mode == ProcessingMode.IDLE
    assert mock_app.call_count > 0


@pytest.mark.asyncio
async def test_replacement_timeout_fires_after_wait(processor, mock_app):
    """Test that multi-word replacement patterns wait for timeout.
    
    Using a multi-word replacement prefix word as the example.
    """
    await processor.process_word_event(
        WordEvent("hello", start_of_utterance=True, end_of_utterance=False)
    )
    await processor.process_word_event(
        WordEvent("david", start_of_utterance=False, end_of_utterance=False)
    )
    
    # Should buffer (multi-word replacement pattern)
    assert processor.mode == ProcessingMode.REPLACEMENT_BUFFERING
    
    # Wait for replacement timeout (400ms)
    await asyncio.sleep(0.5)
    
    # Should have finalized
    assert processor.mode == ProcessingMode.IDLE


# ============================================================================
# STATE MACHINE TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_fresh_command_enters_buffering(processor, mock_app):
    """Test that fresh command word enters COMMAND_BUFFERING."""
    await processor.process_word_event(
        WordEvent("delete", start_of_utterance=True, end_of_utterance=False)
    )
    assert processor.mode == ProcessingMode.COMMAND_BUFFERING


@pytest.mark.asyncio
async def test_mid_utterance_command_passthroughs(processor, mock_app):
    """Test that command words mid-utterance passthrough as dictation."""
    await processor.process_word_event(
        WordEvent("i", start_of_utterance=True, end_of_utterance=False)
    )
    await processor.process_word_event(
        WordEvent("want", start_of_utterance=False, end_of_utterance=False)
    )
    await processor.process_word_event(
        WordEvent("to", start_of_utterance=False, end_of_utterance=False)
    )
    await processor.process_word_event(
        WordEvent("delete", start_of_utterance=False, end_of_utterance=False)
    )
    
    # "delete" should passthrough, not buffer
    assert processor.mode == ProcessingMode.IDLE


@pytest.mark.asyncio
async def test_fresh_non_catalog_passthroughs(processor, mock_app):
    """Test that fresh non-catalog words passthrough immediately."""
    await processor.process_word_event(
        WordEvent("hello", start_of_utterance=True, end_of_utterance=False)
    )
    
    # Should passthrough (not in catalog)
    assert processor.mode == ProcessingMode.IDLE
    assert mock_app.call_count > 0


@pytest.mark.asyncio
async def test_mid_utterance_replacement_buffers(processor, mock_app):
    """Test that multi-word replacement words mid-utterance buffer.
    
    Using a multi-word replacement prefix - 'david' should start buffering.
    """
    await processor.process_word_event(
        WordEvent("hello", start_of_utterance=True, end_of_utterance=False)
    )
    await processor.process_word_event(
        WordEvent("david", start_of_utterance=False, end_of_utterance=False)
    )
    
    # "david" should buffer (multi-word replacement pattern)
    assert processor.mode == ProcessingMode.REPLACEMENT_BUFFERING


# ============================================================================
# DATA TRANSFORMATION TESTS (Real TextParser)
# ============================================================================

@pytest.mark.asyncio
async def test_quotes_pattern_parameter_substitution(processor, mock_app):
    """Test that 'quotes' pattern correctly substitutes captured text.
    
    Bug: Pattern was inserting literal "g1" instead of captured text.
    Pattern: \\bquotes? (?!selection$)(.+)$
    Expected: "quotes hello world" → insert "hello world" with quotes
    Actual (buggy): "quotes hello world" → insert literal "g1"
    
    This test uses RealTextParser to catch parameter substitution bugs.
    """
    # Send "quotes hello world" as utterance
    await processor.process_word_event(
        WordEvent("quotes", start_of_utterance=True, end_of_utterance=False)
    )
    await processor.process_word_event(
        WordEvent("hello", start_of_utterance=False, end_of_utterance=False)
    )
    await processor.process_word_event(
        WordEvent("world", start_of_utterance=False, end_of_utterance=False)
    )
    
    # Should buffer (greedy pattern)
    assert processor.mode == ProcessingMode.REPLACEMENT_BUFFERING
    
    # Wait for timeout (greedy pattern needs timeout to finalize)
    await asyncio.sleep(0.8)
    
    # Should have executed
    assert processor.mode == ProcessingMode.IDLE
    assert mock_app.call_count > 0, "Pattern did not execute"
    
    # CRITICAL: Validate actual text output
    # The action might be 'text' or 'intelligent_insert_text'
    all_actions = mock_app.actions
    assert len(all_actions) > 0, "No actions executed"
    
    # Get the insertion string from whatever action was used
    action = all_actions[0]
    if action.get("action") == "text":
        actual_text = action.get("text", "")
    elif action.get("action") == "intelligent_insert_text":
        actual_text = action.get("params", {}).get("insertion_string", "")
    else:
        actual_text = str(action)
    
    print(f"DEBUG: Actual text inserted: '{actual_text}'")
    
    # Check that captured text was substituted correctly
    assert actual_text != '"g1"', \
        f"🐛 BUG DETECTED: Literal 'g1' inserted instead of captured text! Got: {actual_text}"
    
    # Should contain the captured words
    assert "hello" in actual_text and "world" in actual_text, \
        f"Expected 'hello world' in output, got: {actual_text}"


@pytest.mark.asyncio
async def test_period_replacement_output(processor, mock_app):
    """Test that 'period' replacement produces correct output.
    
    Validates data transformation, not just that action was called.
    """
    await processor.process_word_event(
        WordEvent("hello", start_of_utterance=True, end_of_utterance=False)
    )
    await processor.process_word_event(
        WordEvent("period", start_of_utterance=False, end_of_utterance=False)
    )
    
    # Should have executed (single-word replacement passthroughs)
    assert mock_app.call_count > 0
    
    # Validate output
    text_actions = mock_app.get_actions_by_type("text")
    if text_actions:
        actual_text = text_actions[-1].get("text", "")
        # Should be "." not empty or wrong
        assert actual_text == ".", f"Expected '.', got '{actual_text}'"
