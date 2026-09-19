r"""Router decisions for a Pattern Manager phrase of three or more words.

wh-multiword-phrase-replacement, found as wh-codex-merge-audit.15.1.2.

Pattern Manager simple mode saves a phrase through
speech/phrase_expression.py, which passes the phrase through ``re.escape``
and so writes each internal space as an escaped space: the phrase
"paste my signature" is saved as ``\b(?:paste\ my\ signature)\b``. The
router decides whether a partial buffer can still grow into a replacement
by testing the buffer against the ``literal_body_matchers`` the catalog
compiles for the pattern at load time.

The fixture builds the user file through the real producer and writer
(``PatternManager.create_pattern`` with ``phrases=[...]``) and loads it
beside the shipped patterns.toml. The shipped command
``^paste(?: that| here)?$`` (whole_utterance_only) therefore collides with
the first word of "paste my signature" exactly as it does for a user.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.router import SpeechRouter
from speech.pattern_catalog import PatternCatalog
from speech.pattern_manager import PatternManager
from speech.word_event import WordEvent
from speech.domain import ProcessingMode, Action


GREEDY_TIMEOUT_MS = 5000
COMMAND_TIMEOUT_MS = 700
REPLACEMENT_TIMEOUT_MS = 400

SHIPPED_PATTERNS = (
    Path(__file__).resolve().parent.parent / "speech" / "config" / "patterns.toml"
)

# The smallest system file PatternManager accepts, in the shape
# tests/test_pattern_phrases.py uses. The manager only writes the user
# file; the catalog below loads the SHIPPED system file instead.
SYSTEM_CONTENT = (
    'COMMAND_HOTWORD = "x-ray"\n'
    '\n'
    '[[pattern]]\n'
    "pattern = '''^save$'''\n"
    'requires_hotword = true\n'
    'actions = [\n'
    '    { function = "hk", params = ["ctrl", "s"] }\n'
    ']\n'
)

# Each saved phrase and the text its replacement types. Three words, three
# words, two words and one word: the last two are the shapes that must keep
# firing (acceptance criterion 4).
PHRASE_OUTPUTS = {
    "paste my signature": "Best regards, Sam Rivera",
    "here we go": "Liftoff!",
    "paste date": "2026-09-13",
    "sincerely": "Yours truly,",
}


@pytest.fixture(scope="module")
def phrase_catalog(tmp_path_factory):
    """The shipped catalog plus a user file saved by Pattern Manager."""
    directory = tmp_path_factory.mktemp("multiword_phrase_router")
    system_file = directory / "system_patterns.toml"
    system_file.write_text(SYSTEM_CONTENT, encoding="utf-8")
    user_file = directory / "user_patterns.toml"
    manager = PatternManager(str(system_file), str(user_file))
    for phrase, output in PHRASE_OUTPUTS.items():
        result = manager.create_pattern(
            trigger="",
            pattern_type="replacement",
            action_type="text",
            action_params={"output": output},
            phrases=[phrase],
        )
        assert result["success"], (phrase, result)
    return PatternCatalog(str(SHIPPED_PATTERNS), user_patterns_file=str(user_file))


@pytest.fixture
def router(phrase_catalog):
    return SpeechRouter(phrase_catalog, hotword="x-ray")


def _decide(router, word, mode, buffer):
    """Route one mid-utterance word with the timeouts this suite uses."""
    return router.decide(
        WordEvent(word, start_of_utterance=False, end_of_utterance=False),
        mode,
        buffer,
        hotword_active=False,
        command_timeout_ms=COMMAND_TIMEOUT_MS,
        replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
        greedy_timeout_ms=GREEDY_TIMEOUT_MS,
    )


class TestAPhraseOpeningKeepsBuffering:
    """The buffer ["paste", "my"] opens the saved phrase and must stay open.

    Sequence 1 of wh-codex-merge-audit.15.1.2: "please paste my signature
    now" said in one breath. "paste" also opens the shipped command, so
    the router buffers it in MID_REPLACEMENT_BUFFERING. In that mode the
    router finalizes an impossible buffer at once (router.py, the step 3
    branch that skips the utterance-end deferral for MID mode), so a wrong
    "impossible" answer at "my" types the words.
    """

    def test_multiword_phrase_command_collision_keeps_buffering_at_word_two(
            self, router):
        first = _decide(router, "paste", ProcessingMode.IDLE, [])
        assert first.action == Action.BUFFER, first
        assert first.target_mode == ProcessingMode.MID_REPLACEMENT_BUFFERING, first

        second = _decide(
            router, "my", ProcessingMode.MID_REPLACEMENT_BUFFERING, ["paste"])
        assert second.action == Action.BUFFER, (
            "'paste my' opens the saved phrase 'paste my signature' and "
            f"must keep buffering; got {second.action} "
            f"payload={second.payload!r} reason={second.reason!r}"
        )

        third = _decide(
            router, "signature", ProcessingMode.MID_REPLACEMENT_BUFFERING,
            ["paste", "my"])
        assert third.action == Action.EXECUTE, third
        assert third.payload == "paste my signature", third

    def test_multiword_phrase_catalog_stores_matchers_for_each_opening(
            self, phrase_catalog):
        """The catalog's load-time matchers are what the router's probe
        reads, so the opening must be accepted there, not only by a fresh
        build_literal_prefix_matchers call."""
        entries = [
            data
            for compiled, pattern_type, data
            in phrase_catalog.get_matching_patterns("paste")
            if pattern_type == "replacement"
            and compiled.fullmatch("paste my signature")
        ]
        assert len(entries) == 1, entries
        matchers = entries[0].get("literal_body_matchers")
        assert matchers is not None, entries[0]
        patterns = [m.pattern for m in matchers]
        assert any(m.match("paste my") for m in matchers), patterns
        assert any(m.match("paste") for m in matchers), patterns


class TestTheIncompleteNameCheck:
    """``is_incomplete_replacement_name`` decides the hold at an utterance end.

    Sequence 2 of wh-codex-merge-audit.15.1.2: the user says "here we",
    pauses past the STT endpoint, then says "go". At the end marker the
    processor holds the words only when this check answers True
    (speech_processor.py, ``_should_hold_replacement_prefix``), so a False
    answer flushes "here we" as dictation before "go" arrives.
    """

    def test_multiword_phrase_two_word_opening_is_an_incomplete_name(
            self, router):
        assert router.is_incomplete_replacement_name(["here", "we"]) is True

    @pytest.mark.parametrize("words", [
        pytest.param(["here", "we", "go"], id="complete"),
        pytest.param(["here", "they"], id="departed"),
    ])
    def test_multiword_phrase_name_check_refuses_complete_and_departed_buffers(
            self, router, words):
        """A complete phrase is not unfinished, and a buffer that has left
        the phrase cannot grow into it. Either answer True would hold
        ordinary words back for the release deadline."""
        assert router.is_incomplete_replacement_name(words) is False
