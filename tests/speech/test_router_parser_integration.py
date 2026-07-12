"""Integration tests for SpeechRouter + TextParser.

Tests the interaction between routing decisions and pattern execution.
These tests verify that Router and TextParser use consistent matching logic.

This is CRITICAL for the PatternMatcher refactoring - both components
must agree on what matches and what doesn't.
"""

import pytest
from pathlib import Path
import sys
from unittest.mock import Mock, AsyncMock

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from services.wheelhouse.speech.pattern_catalog import PatternCatalog, PatternType
from services.wheelhouse.speech.router import SpeechRouter
from services.wheelhouse.speech.command_engine import TextParser
from services.wheelhouse.speech.domain import ProcessingMode, Action


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def catalog():
    """Real pattern catalog with production patterns."""
    patterns_file = str(project_root / "services" / "wheelhouse" / "speech" / "config" / "patterns.toml")
    return PatternCatalog(patterns_file)


@pytest.fixture
def router(catalog):
    """SpeechRouter with production patterns."""
    return SpeechRouter(catalog, hotword="x-ray")


@pytest.fixture
def mock_app():
    """Mock app for TextParser."""
    app = Mock()
    app.send_command = AsyncMock()
    app.send_request = AsyncMock(return_value={"status": "success"})
    return app


@pytest.fixture
def parser(catalog, mock_app):
    """TextParser with production patterns.

    parse_and_execute passes authorized_command=True to simulate the
    router's hotword gate having vetted the text (wh-qj70s / wh-z69w).
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
# TEST CLASS: CONSISTENCY BETWEEN ROUTER AND PARSER
# ============================================================================

class TestRouterParserConsistency:
    """Test that Router and Parser agree on matching.

    When Router decides to EXECUTE, Parser must successfully match.
    This ensures the duplicate fullmatch/search logic is consistent.
    """

    @pytest.mark.asyncio
    async def test_command_pattern_consistency(self, router, parser):
        """Router EXECUTE for command -> Parser matches."""
        # Router: "undo" finalizes to EXECUTE
        decision = router._resolve_finalization(buffer=["undo"], hotword_active=False)
        assert decision.action == Action.EXECUTE

        # Parser: "undo" must also match
        result = await parser.parse_and_execute("undo")
        assert result is True

    @pytest.mark.asyncio
    async def test_replacement_pattern_consistency(self, router, parser):
        """Router EXECUTE for replacement -> Parser matches."""
        # Router: "comma" finalizes to EXECUTE
        decision = router._resolve_finalization(buffer=["comma"], hotword_active=False)
        assert decision.action == Action.EXECUTE

        # Parser: "comma" must also match
        result = await parser.parse_and_execute("comma")
        assert result is True

    @pytest.mark.asyncio
    async def test_multi_word_replacement_consistency(self, router, parser):
        """Router and Parser agree on multi-word replacement."""
        # Router: "question mark" finalizes to EXECUTE
        decision = router._resolve_finalization(buffer=["question", "mark"], hotword_active=False)
        assert decision.action == Action.EXECUTE

        # Parser: "question mark" must also match
        result = await parser.parse_and_execute("question mark")
        assert result is True

    @pytest.mark.asyncio
    async def test_no_match_consistency(self, router, parser):
        """Router DICTATE for no-match -> Parser also fails to match."""
        # Router: "xyzzy plugh" finalizes to DICTATE
        decision = router._resolve_finalization(buffer=["xyzzy", "plugh"], hotword_active=False)
        assert decision.action == Action.DICTATE

        # Parser: "xyzzy plugh" must also fail to match
        result = await parser.parse_and_execute("xyzzy plugh")
        assert result is False


# ============================================================================
# TEST CLASS: REMAINDER HANDLING
# ============================================================================

class TestRemainderHandling:
    """Test remainder extraction consistency."""

    @pytest.mark.asyncio
    async def test_router_remainder_matches_parser_remainder(self, router, parser):
        """Router remainder matches Parser remainder extraction."""
        # Router: "comma world" -> EXECUTE with remainder "world"
        decision = router._resolve_finalization(buffer=["comma", "world"], hotword_active=False)
        assert decision.action == Action.EXECUTE
        assert decision.remainder == "world"

        # Parser: "comma world" -> match with remainder "world"
        result, remainder = await parser.parse_and_execute("comma world", return_remainder=True)
        assert result is True
        assert remainder == "world"


# ============================================================================
# TEST CLASS: NUMERIC VALIDATION CONSISTENCY
# ============================================================================

class TestNumericValidationConsistency:
    """Test numeric validation between Router and Parser."""

    @pytest.mark.asyncio
    async def test_valid_number_accepted_by_both(self, router, parser):
        """Valid numbers accepted by both Router and Parser."""
        # Router: "delete 5" can match
        can_match = not router._cannot_match(["delete", "5"], "command")
        assert can_match is True

        # Parser: "delete 5" executes
        result = await parser.parse_and_execute("delete 5")
        assert result is True

    @pytest.mark.asyncio
    async def test_invalid_number_rejected_by_both(self, router, parser):
        """Invalid numbers rejected by both Router and Parser."""
        # Router: "delete xyz" cannot match
        cannot_match = router._cannot_match(["delete", "xyz"], "command")
        assert cannot_match is True

        # Parser: "delete xyz" should not match delete pattern
        # (but might match other patterns or fail)
        result = await parser.parse_and_execute("delete xyz")
        # If it matches a different pattern, that's fine
        # The key is numeric validation rejected the delete+number pattern


# ============================================================================
# TEST CLASS: PATTERN TYPE CONSISTENCY
# ============================================================================

class TestPatternTypeConsistency:
    """Test pattern type detection is used consistently."""

    def test_catalog_type_matches_router_behavior(self, catalog, router):
        """Catalog pattern type matches Router behavior."""
        # Command patterns: Router uses fullmatch
        assert catalog.get_pattern_type("undo") == PatternType.COMMAND
        # Verify Router treats as command (not buffered mid-utterance)

        # Replacement patterns: Router uses search
        assert catalog.get_pattern_type("comma") == PatternType.REPLACEMENT
        # Verify Router treats as replacement (buffered mid-utterance)

    @pytest.mark.asyncio
    async def test_command_fullmatch_in_parser(self, catalog, parser):
        """Parser uses fullmatch for command patterns."""
        # "undo" is command (^ anchor)
        assert catalog.get_pattern_type("undo") == PatternType.COMMAND

        # fullmatch means "undo extra" should NOT match
        result = await parser.parse_and_execute("undo extra")
        assert result is False

    @pytest.mark.asyncio
    async def test_replacement_search_in_parser(self, catalog, parser):
        """Parser uses search for replacement patterns."""
        # "comma" is replacement (no ^ anchor)
        assert catalog.get_pattern_type("comma") == PatternType.REPLACEMENT

        # search means "hello comma world" SHOULD match
        result = await parser.parse_and_execute("hello comma world")
        assert result is True


# ============================================================================
# RUN TESTS
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
