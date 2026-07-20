"""wh-parakeet-xray-hotword: the wake-word match must ignore hyphens.

STT engines disagree on the hyphen in a hyphenated wake word: Parakeet
usually emits "X-ray" but can fuse or split it, and a user hotword
override may be written with or without a hyphen. The router's fresh
wake-word detection (router.py, decide step 2) compares one spoken word
against the configured wake word; that comparison is case-insensitive
and, since this change, hyphen-insensitive in BOTH directions
("xray" matches wake word "x-ray"; "x-ray" matches wake word "xray").

The split two-word form ("x" then "ray") is NOT handled here -- the
Parakeet engine rejoins it into one word before words are sent (see
sherpa_engine._normalize_text and its TestNormalizeTextXrayJoin tests).
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.router import SpeechRouter, _word_matches_hotword
from speech.pattern_catalog import PatternCatalog
from speech.word_event import WordEvent
from speech.domain import ProcessingMode, Action


@pytest.fixture
def catalog():
    return PatternCatalog("speech/config/patterns.toml")


def _decide_fresh_word(router, word):
    """Run one word through the fresh-utterance decision path."""
    ev = WordEvent(word=word, start_of_utterance=True, end_of_utterance=False)
    return router.decide(ev, ProcessingMode.IDLE, [], {})


def _is_hotword_detection(decision):
    return (
        decision.action == Action.TRANSITION
        and decision.target_mode == ProcessingMode.HOTWORD_BUFFERING
    )


class TestHyphenInsensitiveHotword:
    @pytest.fixture
    def router(self, catalog):
        return SpeechRouter(catalog, hotword="x-ray")

    def test_exact_hyphenated_form_matches(self, router):
        assert _is_hotword_detection(_decide_fresh_word(router, "x-ray"))

    def test_fused_form_matches(self, router):
        # An engine that drops the hyphen must still trigger the wake word.
        assert _is_hotword_detection(_decide_fresh_word(router, "xray"))

    def test_fused_form_case_insensitive(self, router):
        assert _is_hotword_detection(_decide_fresh_word(router, "Xray"))
        assert _is_hotword_detection(_decide_fresh_word(router, "X-Ray"))

    def test_partial_words_do_not_match(self, router):
        # Neither syllable alone is the wake word.
        assert not _is_hotword_detection(_decide_fresh_word(router, "x"))
        assert not _is_hotword_detection(_decide_fresh_word(router, "ray"))

    def test_similar_word_does_not_match(self, router):
        assert not _is_hotword_detection(_decide_fresh_word(router, "gray"))

    def test_snapshot_reconstructs_canonical_form(self, router):
        # A fused-form detection still snapshots the CONFIGURED wake word,
        # so the dictation fallback reconstructs a well-formed prefix.
        decision = _decide_fresh_word(router, "xray")
        assert _is_hotword_detection(decision)
        timeout_decision = router.decide_timeout(
            ["hello", "world"], hotword_active=True
        )
        assert timeout_decision.action == Action.DICTATE
        assert timeout_decision.payload == "x-ray hello world"


class TestWordMatchesHotwordHelper:
    """Direct unit tests of the module-level helper (deepseek review,
    wh-parakeet-xray-hotword.1.2): the helper must not depend on callers
    lowercasing the wake word before passing it in. SpeechRouter and
    SpeechProcessor both lowercase today, but the helper is module-level
    and a future caller that skips that step must not get a silent
    false-negative on the wake-word match."""

    def test_mixed_case_hotword_argument_matches(self):
        assert _word_matches_hotword("x-ray", "X-Ray")

    def test_mixed_case_hotword_hyphen_stripped_matches(self):
        assert _word_matches_hotword("xray", "X-RAY")

    def test_non_matching_word_still_rejected(self):
        assert not _word_matches_hotword("gray", "X-Ray")


class TestReverseDirection:
    def test_hyphenated_word_matches_unhyphenated_hotword(self, catalog):
        # A user override written WITHOUT the hyphen must still accept an
        # engine that emits the hyphenated spelling.
        router = SpeechRouter(catalog, hotword="xray")
        assert _is_hotword_detection(_decide_fresh_word(router, "x-ray"))

    def test_unhyphenated_hotword_unaffected_words(self, catalog):
        router = SpeechRouter(catalog, hotword="computer")
        assert not _is_hotword_detection(_decide_fresh_word(router, "compute"))
        assert _is_hotword_detection(_decide_fresh_word(router, "computer"))
