"""Adversarial tests for STT input at the speech processing boundary.

These tests simulate the messy reality of speech-to-text output:
misheard words, partial commands, rapid utterances, and ambiguous input.
This is the system boundary where unexpected input actually enters.

Task R7 of the WheelHouse resilience testing plan.
"""
from typing import List

import pytest
from unittest.mock import Mock

from speech.actions import words_to_int
from speech.domain import ProcessingMode, Action, Decision
from speech.pattern_catalog import PatternCatalog
from speech.router import SpeechRouter
from speech.word_event import WordEvent


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def router():
    """Create a SpeechRouter with production patterns."""
    catalog = PatternCatalog("speech/config/patterns.toml")
    return SpeechRouter(catalog)


def _empty_context() -> dict:
    """Return a minimal context dict for the router."""
    return {"preceding_chars": "", "has_selection": False}


def _decide(
    router: SpeechRouter,
    word: str,
    mode: ProcessingMode = ProcessingMode.IDLE,
    buffer: List[str] | None = None,
    start: bool = False,
    end: bool = False,
) -> Decision:
    """Convenience wrapper around router.decide()."""
    event = WordEvent(
        word=word,
        start_of_utterance=start,
        end_of_utterance=end,
    )
    return router.decide(event, mode, buffer or [], _empty_context())


# =========================================================================
# TestAmbiguousCommands
# =========================================================================

class TestAmbiguousCommands:
    """Test words that could be either commands or dictation.

    At the STT boundary, the router must decide based on context.
    """

    def test_command_word_at_utterance_start(self, router):
        """'delete' at start of utterance should route to command path."""
        decision = _decide(router, "delete", start=True)
        # At utterance start, a known command word should BUFFER or EXECUTE
        assert decision.action in (Action.BUFFER, Action.EXECUTE), (
            f"'delete' at utterance start should be command, not {decision.action}"
        )

    def test_dictation_word_at_utterance_start(self, router):
        """A non-command word at utterance start should dictate."""
        decision = _decide(router, "hello", start=True)
        assert decision.action == Action.DICTATE, (
            f"'hello' should be dictated, not {decision.action}"
        )

    def test_enter_alone_is_dictation_not_command(self, router):
        """'enter' alone is not a standalone command -- it's part of
        'press enter'. Without the 'press' prefix, the router correctly
        treats it as dictation. This documents that partial command words
        don't accidentally trigger commands."""
        decision = _decide(router, "enter", start=True)
        assert decision.action == Action.DICTATE, (
            f"'enter' alone should DICTATE (not a standalone command), "
            f"got {decision.action}"
        )

    def test_number_alone_at_utterance_start(self, router):
        """A bare number at utterance start should dictate (not command)."""
        decision = _decide(router, "5", start=True)
        # Numbers by themselves are dictation, not commands
        assert decision.action == Action.DICTATE, (
            f"Bare '5' at utterance start should dictate, got {decision.action}"
        )


# =========================================================================
# TestPartialCommands
# =========================================================================

class TestPartialCommands:
    """Test incomplete commands at utterance boundaries."""

    def test_command_prefix_alone_buffers(self, router):
        """'press' alone buffers because it's a command first-word.

        The pattern catalog registers 'press' as a command prefix
        (for 'press <key>' patterns), so the router enters
        COMMAND_BUFFERING to wait for a potential second word.
        If no second word arrives, the timeout flushes it as dictation."""
        decision = _decide(router, "press", start=True, end=True)
        assert decision.action == Action.BUFFER, (
            f"'press' alone should BUFFER (command first-word, waiting for second word), "
            f"got {decision.action}"
        )

    def test_two_word_command_only_first_word(self, router):
        """First word of a multi-word command, no second word arrives.
        Router should buffer; timeout will finalize."""
        decision = _decide(router, "backspace", start=True)
        assert decision.action in (Action.BUFFER, Action.EXECUTE), (
            f"'backspace' should buffer or execute, got {decision.action}"
        )

    def test_timeout_finalizes_buffer(self, router):
        """When buffer has a valid command and timeout fires, should execute."""
        decision = router.decide_timeout(["backspace"], hotword_active=False)
        assert decision.action in (Action.EXECUTE, Action.DICTATE), (
            f"Timeout with ['backspace'] should finalize, got {decision.action}"
        )

    def test_empty_buffer_timeout(self, router):
        """Timeout with empty buffer should do nothing."""
        decision = router.decide_timeout([], hotword_active=False)
        assert decision.action == Action.IGNORE, (
            f"Empty buffer timeout should IGNORE, got {decision.action}"
        )


# =========================================================================
# TestNumericInputs
# =========================================================================

class TestNumericInputs:
    """Test numeric word/digit conversion at the STT boundary."""

    def test_spoken_number_three(self):
        assert words_to_int("three") == 3

    def test_digit_string_3(self):
        assert words_to_int("3") == 3

    def test_spoken_number_twenty(self):
        """'twenty' is not in the 0-10 map and should return None."""
        result = words_to_int("twenty")
        assert result is None

    def test_spoken_number_zero(self):
        assert words_to_int("zero") == 0

    def test_spoken_number_ten(self):
        assert words_to_int("ten") == 10

    def test_invalid_word_returns_none(self):
        assert words_to_int("banana") is None

    def test_empty_string_returns_none(self):
        assert words_to_int("") is None

    def test_none_returns_default_1(self):
        """None input (optional capture group not matched) returns 1."""
        assert words_to_int(None) == 1

    def test_stt_homophone_to_maps_to_2(self):
        """STT may produce 'to' instead of 'two' -- should handle."""
        assert words_to_int("to") == 2

    def test_stt_homophone_for_maps_to_4(self):
        """STT may produce 'for' instead of 'four'."""
        assert words_to_int("for") == 4

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace should be stripped."""
        assert words_to_int("  three  ") == 3


# =========================================================================
# TestRapidWordEvents
# =========================================================================

class TestRapidWordEvents:
    """Test processing under rapid word delivery."""

    def test_many_dictation_words_all_dictate(self, router):
        """A sequence of non-command words should all route to DICTATE."""
        words = ["the", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog"]

        for i, word in enumerate(words):
            decision = _decide(
                router,
                word,
                start=(i == 0),
                end=(i == len(words) - 1),
            )
            assert decision.action == Action.DICTATE, (
                f"Word '{word}' at index {i} should DICTATE, got {decision.action}. "
                f"Non-command words in sequence must not be misrouted."
            )

    def test_first_word_dictation_then_command_word_still_dictates(self, router):
        """When first word is dictation, subsequent command-like words should
        also route to DICTATE (because the utterance started as dictation).
        'the' starts dictation, then 'delete' mid-stream should not switch to command."""
        # First word: dictation
        first = _decide(router, "the", start=True)
        assert first.action == Action.DICTATE, (
            f"'the' should DICTATE, got {first.action}"
        )
        # Second word: command-like but mid-utterance after dictation start
        second = _decide(router, "delete", start=False)
        assert second.action == Action.DICTATE, (
            f"'delete' after dictation start should continue as DICTATE, "
            f"got {second.action}. Command words mid-dictation must not "
            f"switch modes."
        )

    def test_single_char_words_are_dictation(self, router):
        """Single-character words (common STT artifacts) should DICTATE."""
        for char in ["a", "I", "o"]:
            decision = _decide(router, char, start=True, end=True)
            assert decision.action == Action.DICTATE, (
                f"Single char '{char}' should DICTATE, got {decision.action}"
            )

    def test_empty_word_event_is_ignored_or_dictated(self, router):
        """Empty string word (STT artifact) should DICTATE or IGNORE, never EXECUTE."""
        decision = _decide(router, "", start=True, end=True)
        assert decision.action in (Action.DICTATE, Action.IGNORE), (
            f"Empty word should DICTATE or IGNORE, got {decision.action}. "
            f"An empty word must never trigger a command EXECUTE."
        )
