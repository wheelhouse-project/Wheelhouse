"""Pattern-catalog tests for a hyphenated first word.

The speech engine sends one token per word. A hum like "mm-hmm" arrives as
the single token "mm-hmm", hyphen included. The catalog builds its
first-word index by taking the leading run of letters from each literal
alternative, and that run stops at the hyphen. So a pattern whose
alternative is "mm-hmm" used to index the key "mm", the index lookup for
the real token "mm-hmm" missed, and the router passed the word through to
dictation with the reason "Not in catalog" -- the shipped filler-sound
filter never ran on the two hyphenated sounds.

The index key must be the whole token the engine sends. These tests pin
that, and they pin the consequence that matters for dictation: "mm" on its
own is the abbreviation for millimeter, it is not an alternative of any
pattern, and it must not be indexed at all.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

from speech.domain import Action, ProcessingMode
from speech.pattern_catalog import PatternCatalog, PatternType
from speech.router import SpeechRouter
from speech.word_event import WordEvent


_HEADER = 'COMMAND_HOTWORD = "x-ray"\n\n'

# The five sounds the shipped filter discards (doc_id filter-filler-sounds).
FILLER_SOUNDS = ["mm-hmm", "mm-mm", "mhm", "hmm", "uh"]


def _write_patterns(tmp_path: Path, body: str) -> str:
    p = tmp_path / "patterns.toml"
    p.write_text(_HEADER + body, encoding="utf-8")
    return str(p)


class TestHyphenatedFirstWordIndex:
    def test_hyphenated_alternative_indexes_the_whole_token(self, tmp_path):
        path = _write_patterns(tmp_path, """
[[pattern]]
pattern = '''\\b(?:mm-hmm|hmm)\\b'''
actions = [
    { function = "text", params = [""] }
]
""")
        catalog = PatternCatalog(path, user_patterns_file="")

        assert "mm-hmm" in catalog.first_words
        assert catalog.could_be_pattern_start("mm-hmm") is True
        assert catalog.get_pattern_type("mm-hmm") == PatternType.REPLACEMENT

    def test_the_truncated_stem_is_not_indexed(self, tmp_path):
        # "mm" is only the leading letters of "mm-hmm". It is not an
        # alternative of the pattern and never matches it, so indexing it
        # would send a dictated "mm" (millimeter) into replacement
        # buffering for no reason.
        path = _write_patterns(tmp_path, """
[[pattern]]
pattern = '''\\b(?:mm-hmm|hmm)\\b'''
actions = [
    { function = "text", params = [""] }
]
""")
        catalog = PatternCatalog(path, user_patterns_file="")

        assert "mm" not in catalog.first_words
        assert catalog.could_be_pattern_start("mm") is False

    def test_a_plain_word_still_indexes_unchanged(self, tmp_path):
        path = _write_patterns(tmp_path, """
[[pattern]]
pattern = '''\\b(?:period|full stop)\\b'''
actions = [
    { function = "text", params = ["."] }
]
""")
        catalog = PatternCatalog(path, user_patterns_file="")

        assert "period" in catalog.first_words
        assert "full" in catalog.first_words


class TestShippedFillerSoundsReachTheirPattern:
    """End-to-end guarantee against the shipped patterns file.

    match_complete() searches every replacement pattern regardless of the
    index, so it reports a match even when the index lookup misses. The
    router asks the index FIRST, so only these router-level checks prove
    the shipped filter actually runs on a spoken sound.
    """

    def test_every_filler_sound_resolves_in_the_catalog(self):
        catalog = PatternCatalog("speech/config/patterns.toml")
        for sound in FILLER_SOUNDS:
            assert catalog.could_be_pattern_start(sound) is True, (
                f"{sound!r} misses the first-word index, so the router "
                f"passes it through to dictation"
            )

    def test_no_filler_sound_is_passed_through_to_dictation(self):
        catalog = PatternCatalog("speech/config/patterns.toml")
        router = SpeechRouter(catalog)

        for sound in FILLER_SOUNDS:
            event = WordEvent(
                word=sound, start_of_utterance=True, end_of_utterance=True
            )
            decision = router.decide(event, ProcessingMode.IDLE, [])
            assert decision.action != Action.DICTATE, (
                f"{sound!r} went straight to dictation ({decision.reason}); "
                f"the filler-sound filter never ran"
            )

    def test_millimeter_still_dictates(self):
        # The unit abbreviation must keep its straight path to dictation.
        catalog = PatternCatalog("speech/config/patterns.toml")
        router = SpeechRouter(catalog)

        event = WordEvent(
            word="mm", start_of_utterance=True, end_of_utterance=True
        )
        decision = router.decide(event, ProcessingMode.IDLE, [])
        assert decision.action == Action.DICTATE
