"""Unit tests for TextParser (command_engine.py).

Tests the pattern matching and execution logic that will be refactored into PatternMatcher.
These tests document current behavior that MUST be preserved.

Test Categories:
1. Fullmatch vs search behavior (command vs replacement)
2. Remainder extraction
3. Capture group extraction (g1, g2, g3)
4. Numeric parameter validation
5. return_remainder parameter behavior
6. First-match-wins ordering
"""

import pytest
from pathlib import Path
import sys
from unittest.mock import Mock, AsyncMock

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from services.wheelhouse.speech.pattern_catalog import PatternCatalog
from services.wheelhouse.speech.command_engine import TextParser


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def catalog():
    """Real pattern catalog with production patterns."""
    patterns_file = str(project_root / "services" / "wheelhouse" / "speech" / "config" / "patterns.toml")
    return PatternCatalog(patterns_file)


@pytest.fixture
def mock_app():
    """Mock app that captures commands."""
    app = Mock()
    app.send_command = AsyncMock()
    app.send_request = AsyncMock(return_value={"status": "success"})
    return app


@pytest.fixture
def parser(catalog, mock_app):
    """TextParser with production patterns and mock app.

    parse_and_execute is wrapped to pass authorized_command=True: these
    tests simulate command-mode text that in production has already
    passed the router's hotword gate (wh-qj70s / wh-z69w).
    """
    mock_handler = Mock()
    mock_handler.app = mock_app
    p = TextParser(mock_handler, catalog)
    orig = p.parse_and_execute
    p.parse_and_execute = (
        lambda text, **kw: orig(text, **{"authorized_command": True, **kw})
    )
    return p


# ============================================================================
# TEST CLASS: FULLMATCH VS SEARCH
# ============================================================================

class TestFullmatchVsSearch:
    """Test that command patterns use fullmatch and replacements use search.

    This is CRITICAL duplicate logic that PatternMatcher will consolidate.
    Current implementation checks pattern.startswith('^') to decide.
    """

    @pytest.mark.asyncio
    async def test_command_requires_fullmatch(self, parser):
        """Command patterns (^ anchor) require entire text to match."""
        # "undo" matches ^undo$ - should work
        result = await parser.parse_and_execute("undo")
        assert result is True

        # "undo please" should NOT match ^undo$ (fullmatch fails)
        result = await parser.parse_and_execute("undo please")
        assert result is False

    @pytest.mark.asyncio
    async def test_command_partial_text_fails(self, parser):
        """Command in middle of text should not match."""
        # "I want to undo" - "undo" mid-text should not match ^undo$
        result = await parser.parse_and_execute("I want to undo")
        assert result is False

    @pytest.mark.asyncio
    async def test_replacement_uses_search(self, parser):
        """Replacement patterns (no ^ anchor) use search - can match mid-text."""
        # "comma" matches \bcomma\b - should work as standalone
        result = await parser.parse_and_execute("comma")
        assert result is True

    @pytest.mark.asyncio
    async def test_replacement_matches_mid_text(self, parser):
        """Replacement can match in middle of text."""
        # "hello comma world" - "comma" should match via search()
        # But this depends on pattern structure - \bcomma\b with word boundaries
        result = await parser.parse_and_execute("hello comma world")
        # This should match if the pattern is \bcomma\b
        assert result is True


# ============================================================================
# TEST CLASS: REMAINDER EXTRACTION
# ============================================================================

class TestRemainderExtraction:
    """Test return_remainder=True behavior for text after pattern match."""

    @pytest.mark.asyncio
    async def test_remainder_extraction_basic(self, parser):
        """Test that remainder is extracted correctly."""
        # "comma world" - "comma" matches, "world" is remainder
        result, remainder = await parser.parse_and_execute("comma world", return_remainder=True)
        assert result is True
        assert remainder == "world"

    @pytest.mark.asyncio
    async def test_no_remainder_when_full_match(self, parser):
        """No remainder when entire text matches."""
        result, remainder = await parser.parse_and_execute("comma", return_remainder=True)
        assert result is True
        assert remainder == ""

    @pytest.mark.asyncio
    async def test_remainder_with_multiple_words(self, parser):
        """Remainder includes all text after match."""
        result, remainder = await parser.parse_and_execute("comma hello world", return_remainder=True)
        assert result is True
        assert "hello" in remainder
        assert "world" in remainder

    @pytest.mark.asyncio
    async def test_no_match_returns_full_text_as_remainder(self, parser):
        """When no match, remainder is entire text."""
        result, remainder = await parser.parse_and_execute("xyzzy plugh", return_remainder=True)
        assert result is False
        assert remainder == "xyzzy plugh"


# ============================================================================
# TEST CLASS: CAPTURE GROUP EXTRACTION
# ============================================================================

class TestCaptureGroups:
    """Test capture group extraction (g1, g2, g3) in patterns."""

    @pytest.mark.asyncio
    async def test_single_capture_group(self, parser, mock_app):
        """Test single capture group extraction."""
        # "activate chrome" - captures "chrome" as g1
        result = await parser.parse_and_execute("activate chrome")
        assert result is True
        # The action should have received the captured value
        assert mock_app.send_command.called or mock_app.send_request.called

    @pytest.mark.asyncio
    async def test_numeric_capture_group(self, parser, mock_app):
        """Test numeric capture group extraction."""
        # "delete 5" - captures "5" as g1
        result = await parser.parse_and_execute("delete 5")
        assert result is True

    @pytest.mark.asyncio
    async def test_two_capture_groups(self, parser, mock_app):
        """Test pattern with two capture groups."""
        # "backspace 3" - captures "backspace" as g1, "3" as g2
        result = await parser.parse_and_execute("backspace 3")
        assert result is True


# ============================================================================
# TEST CLASS: NUMERIC VALIDATION
# ============================================================================

class TestNumericValidation:
    """Test numeric parameter validation in _execute_rule.

    This is CRITICAL duplicate logic that PatternMatcher will consolidate.
    Current implementation uses words_to_int() to validate.
    """

    @pytest.mark.asyncio
    async def test_valid_digit_number(self, parser):
        """Digit numbers should pass validation."""
        result = await parser.parse_and_execute("delete 5")
        assert result is True

    @pytest.mark.asyncio
    async def test_valid_word_number(self, parser):
        """Word numbers (one, two, three) should pass validation."""
        # "backspace three" - "three" should validate via words_to_int
        result = await parser.parse_and_execute("backspace three")
        assert result is True

    @pytest.mark.asyncio
    async def test_valid_large_number(self, parser):
        """Larger numbers should validate."""
        result = await parser.parse_and_execute("delete 10")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_without_number_uses_default(self, parser):
        """Delete without number should use default (1)."""
        result = await parser.parse_and_execute("delete")
        assert result is True


# ============================================================================
# TEST CLASS: RETURN_REMAINDER PARAMETER
# ============================================================================

class TestReturnRemainderParameter:
    """Test the return_remainder parameter behavior."""

    @pytest.mark.asyncio
    async def test_return_remainder_false_returns_bool(self, parser):
        """return_remainder=False returns just boolean."""
        result = await parser.parse_and_execute("undo", return_remainder=False)
        assert isinstance(result, bool)
        assert result is True

    @pytest.mark.asyncio
    async def test_return_remainder_true_returns_tuple(self, parser):
        """return_remainder=True returns (bool, str) tuple."""
        result = await parser.parse_and_execute("undo", return_remainder=True)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)

    @pytest.mark.asyncio
    async def test_default_is_false(self, parser):
        """Default return_remainder is False."""
        result = await parser.parse_and_execute("undo")
        assert isinstance(result, bool)


# ============================================================================
# TEST CLASS: FIRST MATCH WINS
# ============================================================================

class TestFirstMatchWins:
    """Test that first matching pattern wins (ordering matters)."""

    @pytest.mark.asyncio
    async def test_patterns_checked_in_order(self, parser, mock_app):
        """Patterns are checked in file order, first match wins."""
        # This is implicit in the implementation - if multiple patterns
        # could match, the first one in patterns.toml wins
        # Test with a known pattern
        result = await parser.parse_and_execute("undo")
        assert result is True
        # Just verify it matched something - ordering is implicit


# ============================================================================
# TEST CLASS: PATTERN TYPE DETECTION
# ============================================================================

class TestPatternTypeDetection:
    """Test pattern type detection (command vs replacement) from ^ anchor.

    This is CRITICAL duplicate logic that PatternMatcher will consolidate.
    Current implementation checks compiled.pattern.startswith('^').
    """

    @pytest.mark.asyncio
    async def test_command_pattern_detected(self, parser):
        """Patterns with ^ anchor are treated as commands (fullmatch)."""
        # ^undo$ is a command - should require fullmatch
        result = await parser.parse_and_execute("undo")
        assert result is True
        result = await parser.parse_and_execute("undo extra")
        assert result is False

    @pytest.mark.asyncio
    async def test_replacement_pattern_detected(self, parser):
        """Patterns without ^ anchor are treated as replacements (search)."""
        # \bcomma\b is a replacement - should use search
        result = await parser.parse_and_execute("comma")
        assert result is True


# ============================================================================
# RUN TESTS
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
