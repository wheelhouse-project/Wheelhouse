"""Test harness infrastructure for comprehensive pattern testing.

Provides mock components and helpers for testing speech patterns without
actual WebSocket/STT connections or UI interactions.

Components:
- MockApp: Records all action executions with full details
- MockWebSocketFeed: Generates WordEvent sequences with timing control
- TestScenario: DSL for defining test cases
- Assertion helpers: Validate state, actions, timing
"""
import asyncio
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, Mock
from pathlib import Path

# Add project root to path
import sys
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from services.wheelhouse.speech.word_event import WordEvent
from services.wheelhouse.speech.speech_processor import ProcessingMode


# ============================================================================
# MOCK APP - Records all action executions
# ============================================================================

class MockApp:
    """Mock WheelHouse app that records all send_command() and send_request() calls.
    
    Records:
    - All actions executed
    - Action parameters
    - Execution order
    - Timing information
    
    Usage:
        app = MockApp()
        await app.send_command({"action": "text", "text": "hello"})
        assert app.actions == [{"action": "text", "text": "hello"}]
    """
    
    def __init__(self):
        self.actions: List[Dict[str, Any]] = []
        self.call_count = 0
        
    async def send_command(self, payload: Dict[str, Any]):
        """Record action execution.
        
        Args:
            payload: Action payload (action, params, etc.)
        """
        self.actions.append(payload)
        self.call_count += 1
    
    async def send_request(self, action: str, params: Dict[str, Any]):
        """Record action execution that awaits completion.
        
        Args:
            action: Action name
            params: Action parameters dictionary
        """
        payload = {"action": action, "params": params}
        self.actions.append(payload)
        self.call_count += 1
        # Return mock response
        return {"status": "success"}
        
    def reset(self):
        """Clear recorded actions."""
        self.actions.clear()
        self.call_count = 0
        
    def get_actions_by_type(self, action_type: str) -> List[Dict[str, Any]]:
        """Get all actions of specific type.
        
        Args:
            action_type: Action type to filter by (e.g., "text", "hk")
            
        Returns:
            List of actions matching the type
        """
        return [a for a in self.actions if a.get("action") == action_type]
    
    def get_text_output(self) -> str:
        """Get concatenated text from all 'text' actions.
        
        Returns:
            Combined text output from all text actions
        """
        text_actions = self.get_actions_by_type("text")
        return "".join(a.get("text", "") for a in text_actions)
    
    @property
    def last_action(self) -> Optional[Dict[str, Any]]:
        """Get most recent action."""
        return self.actions[-1] if self.actions else None


# ============================================================================
# REAL TEXT PARSER - Uses actual pattern matching and execution
# ============================================================================

class RealTextParser:
    """Real TextParser wrapper for testing.

    Uses the actual TextParser from command_engine.py to execute real pattern
    matching and action execution. This catches bugs in:
    - Pattern matching logic
    - Parameter substitution
    - Capture group handling
    - Action execution

    IMPORTANT: Side-effect functions (run, gs, add_hint_to_stt) are mocked to
    prevent tests from actually opening browsers, running programs, etc.
    """

    def __init__(self, mock_app: MockApp, pattern_catalog):
        """Initialize with real TextParser.

        Args:
            mock_app: MockApp to receive action calls
            pattern_catalog: PatternCatalog with loaded patterns
        """
        from services.wheelhouse.speech.command_engine import TextParser

        # Create mock speech handler with just the app reference
        self.mock_speech_handler = Mock()
        self.mock_speech_handler.app = mock_app

        # Create real TextParser
        self.parser = TextParser(self.mock_speech_handler, pattern_catalog)

        # Mock side-effect functions to prevent actual execution during tests
        # These functions execute directly (open browser, run programs, etc.)
        self._mock_side_effect_functions()

    def _mock_side_effect_functions(self):
        """Replace side-effect functions with safe mocks.

        Functions that execute directly (not via IPC) need to be mocked to
        prevent tests from actually opening browsers, running programs, etc.
        """
        action_funcs = self.parser.action_functions

        # Mock run_program (run) - would execute programs
        async def mock_run_program(path):
            return None
        action_funcs._functions["run"] = mock_run_program

        # Mock GSearch (gs) - would open browser
        async def mock_gsearch(query=None):
            return None
        action_funcs._functions["gs"] = mock_gsearch

        # Mock add_hint_to_stt - would access clipboard and websocket
        async def mock_add_hint():
            return None
        action_funcs._functions["add_hint_to_stt"] = mock_add_hint

        # Mock async_sleep (sleep) - would delay tests
        async def mock_sleep(duration):
            return None
        action_funcs._functions["sleep"] = mock_sleep

    async def parse_and_execute(self, text: str) -> bool:
        """Execute real pattern matching and action execution.

        Passes authorized_command=True: this harness simulates command-mode
        text that in production has already passed the router's hotword
        gate (wh-qj70s). Without it every requires_hotword command pattern
        is refused and the suite fails wholesale (wh-z69w).

        Args:
            text: Text to match against patterns

        Returns:
            True if pattern matched and executed, False otherwise
        """
        return await self.parser.parse_and_execute(
            text, authorized_command=True
        )


# ============================================================================
# TEST SCENARIO - DSL for defining test cases
# ============================================================================

@dataclass
class WordSpec:
    """Specification for a word in a test scenario.
    
    Attributes:
        word: The word text
        fresh: True if start_of_utterance (default: False)
        utterance_end: True if this is end of utterance (default: False)
        delay_ms: Delay before sending this word (default: 0)
    """
    word: str
    fresh: bool = False
    utterance_end: bool = False
    delay_ms: int = 0


@dataclass
class TestScenario:
    """Test scenario definition.
    
    Defines a sequence of words to send through the processor and
    expected outcomes.
    
    Attributes:
        name: Scenario description
        words: List of word specifications
        expected_mode: Expected final processing mode
        expected_actions: Expected actions executed (optional)
        expected_text: Expected text output (optional)
        should_timeout: Whether scenario should trigger timeout
    """
    name: str
    words: List[WordSpec]
    expected_mode: Optional[ProcessingMode] = None
    expected_actions: Optional[List[Dict[str, Any]]] = None
    expected_text: Optional[str] = None
    should_timeout: bool = False


def words(*specs: Tuple[str, bool]) -> List[WordSpec]:
    """Helper to create word specs from tuples.
    
    Args:
        specs: Tuples of (word, is_fresh)
        
    Returns:
        List of WordSpec objects
        
    Example:
        words(("delete", True), ("five", False))
    """
    return [WordSpec(word=w, fresh=f) for w, f in specs]


async def run_scenario(processor, scenario: TestScenario, mock_app: MockApp) -> Dict[str, Any]:
    """Run a test scenario through the processor.
    
    Args:
        processor: SpeechProcessor instance
        scenario: Test scenario to run
        mock_app: Mock app to record actions
        
    Returns:
        Results dictionary with:
        - final_mode: Processor mode after scenario
        - actions: List of actions executed
        - buffer: Final buffer contents
        - text_output: Combined text from text actions
    """
    mock_app.reset()
    
    for word_spec in scenario.words:
        # Apply delay if specified
        if word_spec.delay_ms > 0:
            await asyncio.sleep(word_spec.delay_ms / 1000.0)
            
        # Create and process word event
        event = WordEvent(
            word=word_spec.word,
            start_of_utterance=word_spec.fresh,
            end_of_utterance=word_spec.utterance_end
        )
        await processor.process_word_event(event)
    
    # Wait for any pending timeouts if expected
    if scenario.should_timeout:
        timeout_ms = processor.command_completion_wait_ms
        await asyncio.sleep(timeout_ms / 1000.0 + 0.1)
    
    return {
        "final_mode": processor.mode,
        "actions": mock_app.actions.copy(),
        "buffer": processor.buffer.copy(),
        "text_output": mock_app.get_text_output(),
        "action_count": mock_app.call_count
    }


# ============================================================================
# ASSERTION HELPERS
# ============================================================================

def assert_mode(result: Dict[str, Any], expected: ProcessingMode, msg: str = ""):
    """Assert processor ended in expected mode."""
    actual = result["final_mode"]
    assert actual == expected, f"{msg}\nExpected mode {expected}, got {actual}"


def assert_actions_count(result: Dict[str, Any], expected: int, msg: str = ""):
    """Assert specific number of actions executed."""
    actual = result["action_count"]
    assert actual == expected, f"{msg}\nExpected {expected} actions, got {actual}"


def assert_text_output(result: Dict[str, Any], expected: str, msg: str = ""):
    """Assert text output matches expected."""
    actual = result["text_output"]
    assert actual == expected, f"{msg}\nExpected text '{expected}', got '{actual}'"


def assert_buffer_empty(result: Dict[str, Any], msg: str = ""):
    """Assert buffer was cleared."""
    buffer = result["buffer"]
    assert len(buffer) == 0, f"{msg}\nExpected empty buffer, got {buffer}"


def assert_action_type(result: Dict[str, Any], action_type: str, msg: str = ""):
    """Assert at least one action of specified type was executed."""
    actions = result["actions"]
    found = any(a.get("action") == action_type for a in actions)
    assert found, f"{msg}\nNo action of type '{action_type}' found in {actions}"


# ============================================================================
# PATTERN HELPERS
# ============================================================================

def is_command_pattern(pattern: str) -> bool:
    """Check if pattern is a command (starts with ^).
    
    Args:
        pattern: Regex pattern string
        
    Returns:
        True if command pattern, False if replacement
    """
    return pattern.startswith("^")


def has_optional_params(pattern: str) -> bool:
    """Check if pattern has optional parameters.
    
    Args:
        pattern: Regex pattern string
        
    Returns:
        True if pattern has (?:...)? optional groups
    """
    return "(?:" in pattern and ")?" in pattern


def extract_first_word(pattern: str) -> str:
    """Extract first word from pattern for test naming.
    
    Args:
        pattern: Regex pattern string
        
    Returns:
        First word in pattern
    """
    # Remove anchors and extract first literal word
    pattern = pattern.lstrip("^").rstrip("$")
    pattern = pattern.replace("\\b", "")
    
    # Extract first word before any regex metacharacters
    for i, char in enumerate(pattern):
        if char in "(?[\\|":
            return pattern[:i].strip()
    return pattern.strip()
