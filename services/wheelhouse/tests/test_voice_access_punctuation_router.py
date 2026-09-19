"""Router-level Voice Access punctuation regressions.

Pipeline tests send production-catalog word events through SpeechProcessor and
inspect input commands. Direct router tests cover finalization decisions.
"""
import asyncio

import pytest

from speech.domain import Action, ProcessingMode
from speech.pattern_catalog import PatternCatalog
from speech.router import SpeechRouter
from speech.word_event import WordEvent
from tests.test_speech_pipeline import SpeechPipelineHarness


@pytest.fixture
async def production_punctuation_harness():
    harness = SpeechPipelineHarness()
    await harness.start()
    yield harness
    await harness.stop()


@pytest.fixture
def production_punctuation_router():
    return SpeechRouter(PatternCatalog("speech/config/patterns.toml"))


async def _send_phrase(harness, words, *, prefix=()):
    """Send one production word stream and finalize its utterance."""
    for index, word in enumerate((*prefix, *words)):
        await harness.send_word(
            word,
            start_of_utterance=index == 0,
            end_of_utterance=index == len(prefix) + len(words) - 1,
            delay_before_ms=50 if index else 0,
        )
    await harness.send_utterance_end_marker(harness._utterance_counter)
    await asyncio.sleep(0.1)


def _inserted_text(harness):
    return harness.get_dictation_texts()


class TestVoiceAccessPunctuationRouter:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("spoken_name", "character"),
        (
            (("close", "bracket"), "]"),
            (("close", "parentheses"), ")"),
            (("right", "parentheses"), ")"),
            (("number", "sign"), "#"),
            # "open" indexes a command too since the help-online entry gained the
            # alias "open voice access help" (wh-voice-access-parity.1.6).
            (("open", "bracket"), "["),
        ),
    )
    async def test_fresh_utterance_executes_replacement(
        self, production_punctuation_harness, spoken_name, character
    ):
        await _send_phrase(production_punctuation_harness, spoken_name)

        inserted = _inserted_text(production_punctuation_harness)
        assert character in inserted, (
            f"{spoken_name!r} must insert {character!r}; inserted={inserted!r}"
        )
        assert " ".join(spoken_name) not in " ".join(inserted)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("spoken_name", "character"),
        (
            (("close", "bracket"), "]"),
            (("close", "parentheses"), ")"),
            (("right", "parentheses"), ")"),
            (("number", "sign"), "#"),
            # "open" indexes a command too since the help-online entry gained the
            # alias "open voice access help" (wh-voice-access-parity.1.6).
            (("open", "bracket"), "["),
        ),
    )
    async def test_mid_utterance_executes_replacement(
        self, production_punctuation_harness, spoken_name, character
    ):
        await _send_phrase(
            production_punctuation_harness, spoken_name, prefix=("say",)
        )

        inserted = _inserted_text(production_punctuation_harness)
        assert "say" in inserted
        assert character in inserted, (
            f"mid-utterance {spoken_name!r} must insert {character!r}; "
            f"inserted={inserted!r}"
        )
        assert " ".join(spoken_name) not in " ".join(inserted)

    @pytest.mark.asyncio
    async def test_say_right_parentheses_preserves_only_say(
        self, production_punctuation_harness
    ):
        await _send_phrase(
            production_punctuation_harness,
            ("right", "parentheses"),
            prefix=("say",),
        )

        inserted = _inserted_text(production_punctuation_harness)
        assert inserted == ["say", ")"], f"unexpected insertion stream: {inserted!r}"
        assert "right" not in " ".join(inserted)
        assert "()" not in inserted

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "spoken_words",
        (
            ("back", "space"),
            ("right", "click"),
            ("number", "five"),
        ),
    )
    async def test_mid_utterance_collision_never_executes_a_command(
        self, production_punctuation_harness, spoken_words
    ):
        """A mid-utterance word that starts both a command and a replacement
        must still finalize as dictation when the replacement does not
        complete. It must never fall back to matching a command.

        Before the routing fix these words were dictated unconditionally
        mid-utterance. The fix sends them to replacement buffering, and a
        failed replacement finalizes through a path that tries commands
        first, so "say back space" could press Backspace in the middle of
        dictated text.
        """
        await _send_phrase(
            production_punctuation_harness, spoken_words, prefix=("say",)
        )

        actions = production_punctuation_harness.mock_app.get_all_actions()
        offending = [
            a for a in actions
            if a not in ("intelligent_insert_text", "end_utterance")
        ]
        assert not offending, (
            f"'say {' '.join(spoken_words)}' executed {offending!r}; "
            "a mid-utterance collision must finalize as dictation"
        )

        dictated = " ".join(_inserted_text(production_punctuation_harness))
        for word in ("say", *spoken_words):
            assert word in dictated, (
                f"{word!r} was lost from dictation; dictated={dictated!r}"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("joined_word", "character"),
        (
            ("eurosign", "€"),
            ("numbersign", "#"),
            ("yensign", "¥"),
            ("minussign", "-"),
        ),
    )
    async def test_joined_sign_name_inserts_the_character_mid_utterance(
        self, production_punctuation_harness, joined_word, character
    ):
        """Live dictation on 2026-08-17 typed "Eurosign" for the phrase "the
        price is euro sign fifty". The speech-to-text returned the two words
        joined into one. The two-word names now allow the joined form, and
        this test drives the whole pipeline the way the live utterance did.

        Only two-word names are covered here. The three-word names ("less
        than sign", "greater than sign", "pound sterling sign") accept the
        joined forms in the text parser, but the catalog does not list the
        fully joined word as a first word, so the router cannot recognize it.
        """
        await _send_phrase(
            production_punctuation_harness,
            (joined_word, "fifty"),
            prefix=("the", "price", "is"),
        )

        inserted = _inserted_text(production_punctuation_harness)
        assert character in inserted, (
            f"joined {joined_word!r} must insert {character!r}; "
            f"inserted={inserted!r}"
        )
        assert joined_word not in " ".join(inserted)

    @pytest.mark.asyncio
    async def test_mid_utterance_command_only_word_still_dictates(
        self, production_punctuation_harness
    ):
        await _send_phrase(
            production_punctuation_harness, ("delete",), prefix=("hello",)
        )

        inserted = _inserted_text(production_punctuation_harness)
        assert "delete" in inserted, (
            "a command-only first word must remain ordinary mid-utterance "
            f"dictation; inserted={inserted!r}"
        )


class TestMidUtteranceCollisionRouterDecisions:
    @pytest.mark.parametrize(
        "spoken_words",
        (
            ("back", "space"),
            ("right", "click"),
            ("number", "five"),
        ),
    )
    def test_incomplete_mid_utterance_replacement_dictates(
        self, production_punctuation_router, spoken_words
    ):
        """Removing the command-suppression finalization rule would execute these commands."""
        first, second = spoken_words
        first_decision = production_punctuation_router.decide(
            WordEvent(first, start_of_utterance=False, end_of_utterance=False),
            ProcessingMode.IDLE,
            [],
        )

        decision = production_punctuation_router.decide(
            WordEvent(second, start_of_utterance=False, end_of_utterance=False),
            first_decision.target_mode,
            [first],
        )

        assert decision.action is Action.DICTATE
        assert decision.payload == " ".join(spoken_words)

    def test_mid_utterance_collision_timeout_dictates(
        self, production_punctuation_router
    ):
        """Removing timeout-mode propagation would let a paused "back" execute as a command."""
        first_decision = production_punctuation_router.decide(
            WordEvent("back", start_of_utterance=False, end_of_utterance=False),
            ProcessingMode.IDLE,
            [],
        )

        decision = production_punctuation_router.decide_timeout(
            ["back"],
            hotword_active=False,
            mode=first_decision.target_mode,
        )

        assert decision.action is Action.DICTATE
        assert decision.payload == "back"

    def test_fresh_collision_word_still_starts_command_buffering(
        self, production_punctuation_router
    ):
        """Changing the fresh command branch to the mid-utterance mode would fail this test."""
        decision = production_punctuation_router.decide(
            WordEvent("back", start_of_utterance=True, end_of_utterance=False),
            ProcessingMode.IDLE,
            [],
        )

        assert decision.action is Action.BUFFER
        assert decision.target_mode is ProcessingMode.COMMAND_BUFFERING


def _hotkey_key_lists(harness):
    """Every hotkey combination the utterance sent to the Input process."""
    return [
        output.params.get("keys")
        for output in harness.get_outputs()
        if output.action == "hotkey_action"
    ]


class TestDeleteAllIsWholeUtteranceOnly:
    """delete-all runs Control+A then Delete, so it must never fire mid-utterance.

    Found by DeepSeek in the round-1 review of commit bc5b588e
    (wh-voice-access-parity.1.6.1.2). Before the fix, "delete all the files"
    selected the whole document and deleted it at the second word, then typed
    "the files" into the emptied document.
    """

    @pytest.mark.asyncio
    async def test_whole_utterance_delete_all_still_selects_then_deletes(
        self, production_punctuation_harness
    ):
        await _send_phrase(production_punctuation_harness, ("delete", "all"))

        assert ["ctrl", "a"] in _hotkey_key_lists(production_punctuation_harness)
        assert "del" in [
            output.params.get("key")
            for output in production_punctuation_harness.get_outputs()
            if output.action == "press_key_action"
        ]

    @pytest.mark.asyncio
    async def test_dictated_sentence_starting_with_delete_all_never_selects_all(
        self, production_punctuation_harness
    ):
        """Control+A must not reach the focused application.

        A single Delete press still happens here. That comes from the older
        one-word "delete" prefix behavior, which predates this branch and is
        not what this test guards.
        """
        await _send_phrase(
            production_punctuation_harness, ("delete", "all", "the", "files")
        )

        hotkeys = _hotkey_key_lists(production_punctuation_harness)
        assert ["ctrl", "a"] not in hotkeys, (
            f"delete-all fired mid-utterance and selected everything: {hotkeys!r}"
        )


class TestVoiceAccessAliasesStayOrdinaryDictation:
    """Sentences that contain a Voice Access alias must type as text.

    Finding wh-voice-access-parity.1.6.1.6 (DeepSeek, round 1 on commit
    bc5b588e). The rows that commit added resolve a spoken phrase with a
    plain regular-expression search over the pattern file, which models
    the parser layer only. Nothing drove the router, so a router
    regression would have kept every row green. These rows drive the
    whole pipeline instead.

    The three formatting names set whole_utterance_only, so their trigger
    words stay dictatable inside a longer sentence. The two remaining
    rows cover the words this branch changed most: "tap", which joined
    the greedy click trigger, and "dismiss", which briefly pressed the
    Escape key.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "spoken_words",
        (
            ("I", "need", "a", "cap", "that", "fits"),
            ("wear", "all", "caps", "that", "day"),
            ("there", "are", "no", "caps", "that", "fit"),
            ("tap", "water", "is", "safe", "to", "drink"),
            ("dismiss", "the", "alarm"),
        ),
    )
    async def test_sentence_is_typed_and_runs_no_command(
        self, production_punctuation_harness, spoken_words
    ):
        await _send_phrase(production_punctuation_harness, spoken_words)

        actions = production_punctuation_harness.mock_app.get_all_actions()
        offending = [
            action for action in actions
            if action not in ("intelligent_insert_text", "end_utterance")
        ]
        assert not offending, (
            f"{' '.join(spoken_words)!r} ran {offending!r}; the whole "
            "sentence must be ordinary dictation"
        )

        dictated = " ".join(_inserted_text(production_punctuation_harness))
        for word in spoken_words:
            assert word in dictated, (
                f"{word!r} was lost from dictation; dictated={dictated!r}"
            )
