"""cancel fix runs without the safety word (wh-safety-word-free-commands, stage S3).

"cancel fix" no longer needs the safety word, and neither does the cancel lane
that stops a running AI call while the model is still answering. The safety
word now decides what a "cancel fix" that has nothing to cancel does:

* no safety word: the whole utterance is typed as dictation and no notice
  appears, because the user may simply have been speaking;
* safety word spoken: a notice explains the failure and nothing is typed.

The lane fires without the safety word only at the END of the utterance, so
"cancel fix the typo" is ordinary words and cancels nothing. With the safety
word it keeps its early-firing behaviour. When the lane did cancel a job, the
command replayed for that same utterance must stay silent.

These tests drive the real SpeechProcessor, the real TextParser, the real
ActionFunctions and the real AIService (the rig of
tests/test_ai/test_cancel_during_running_rewrite.py); only the provider and
the Input-process round trips are stand-ins.
"""
import asyncio
import logging

import pytest

from speech.speech_processor import (
    _CancelCommandRecognizer,
    _cancel_command_patterns,
)
from speech.word_event import WordEvent

from tests.test_ai.test_cancel_during_running_rewrite import _Rig, _WAIT_S

NO_JOB = "No AI job is running."

WITHOUT = ()
WITH = ("x-ray",)
SAFETY = [
    pytest.param(WITHOUT, id="no-safety-word"),
    pytest.param(WITH, id="safety-word"),
]


@pytest.fixture
async def rig():
    rig = _Rig()
    await rig.processor.start()
    try:
        yield rig
    finally:
        rig.provider.release.set()
        await rig.processor.stop()


@pytest.fixture
async def rig_unstarted():
    rig = _Rig()
    try:
        yield rig
    finally:
        rig.provider.release.set()
        rig.processor.end_ai_cancel_lane()


def _typed(rig) -> str:
    """Everything typed, from either typing path, as one string."""
    return " ".join(rig.typed_text + rig.text_typed_without_waiting).strip()


async def _settle(rig):
    """Wait until every queued and deferred event has been processed."""
    await rig.wait_until(
        lambda: rig.processor.word_queue.empty()
        and not rig.processor._deferred_word_events
        and not rig.service.is_processing(),
        "the queue to drain",
    )
    await asyncio.sleep(0.25)


async def _start_a_fix(rig):
    await rig.say("x-ray", "fix", utterance_id=1)
    await asyncio.wait_for(rig.provider.in_flight.wait(), timeout=_WAIT_S)


class TestACancelFixCancelsARunningJob:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefix", SAFETY)
    async def test_the_job_is_cancelled_and_nothing_else_happens(
        self, rig, prefix,
    ):
        await _start_a_fix(rig)

        await rig.say(*prefix, "cancel", "fix", utterance_id=2)
        await rig.wait_until(
            lambda: "Cancelling." in rig.notices, "the cancel to be acknowledged"
        )

        rig.provider.release.set()
        await _settle(rig)

        assert "replace_selected_text" not in rig.actions_sent
        assert "Cancelled." in rig.notices
        assert rig.notices.count("Cancelling.") == 1, (
            "the replayed command cancelled a second time"
        )
        assert NO_JOB not in rig.notices, (
            "the replayed cancel fix told the user there was nothing to cancel"
        )
        assert _typed(rig) == "", f"the cancel words were typed: {_typed(rig)!r}"

    @pytest.mark.asyncio
    async def test_the_silence_belongs_to_that_utterance_only(self, rig):
        await _start_a_fix(rig)
        await rig.say("cancel", "fix", utterance_id=2)
        await rig.wait_until(
            lambda: "Cancelling." in rig.notices, "the cancel to be acknowledged"
        )
        rig.provider.release.set()
        await _settle(rig)
        assert _typed(rig) == ""

        # A later "cancel fix" with no job is an ordinary failed command.
        await rig.say("cancel", "fix", utterance_id=3)
        await _settle(rig)

        assert _typed(rig) == "cancel fix"
        assert NO_JOB not in rig.notices

    @pytest.mark.asyncio
    async def test_a_reused_id_after_the_replay_is_an_ordinary_failure(self, rig):
        """The replay consumes the recorded id. Ids restart when the STT
        server restarts, so the same id can come back at once; that later
        "cancel fix" with no job must type its words."""
        await _start_a_fix(rig)
        await rig.say("cancel", "fix", utterance_id=2)
        await rig.wait_until(
            lambda: "Cancelling." in rig.notices, "the cancel to be acknowledged"
        )
        rig.provider.release.set()
        await _settle(rig)
        assert _typed(rig) == ""

        await rig.say("cancel", "fix", utterance_id=2)
        await _settle(rig)

        assert _typed(rig) == "cancel fix"

    @pytest.mark.asyncio
    async def test_an_id_left_by_an_earlier_utterance_silences_nothing_later(
        self, rig,
    ):
        """An id the replay never consumed (the lane fired twice for one
        utterance, for example on a retraction) is forgotten when a
        different utterance starts, so a reused id is not swallowed after
        an STT server restart."""
        rig.processor._lane_cancelled_utterances.append(2)

        await rig.say("hello", utterance_id=3)
        await _settle(rig)
        await rig.say("cancel", "fix", utterance_id=2)
        await _settle(rig)

        assert _typed(rig) == "hello cancel fix"
        assert NO_JOB not in rig.notices


class TestNothingToCancel:
    @pytest.mark.asyncio
    async def test_without_the_safety_word_the_words_are_typed_with_no_notice(
        self, rig,
    ):
        await rig.say("cancel", "fix", utterance_id=1)
        await _settle(rig)

        assert _typed(rig) == "cancel fix"
        assert rig.notices == []

    @pytest.mark.asyncio
    async def test_with_the_safety_word_a_notice_is_shown_and_nothing_is_typed(
        self, rig,
    ):
        await rig.say("x-ray", "cancel", "fix", utterance_id=1)
        await _settle(rig)

        assert rig.notices == [NO_JOB]
        assert _typed(rig) == ""


class TestNoAiService:
    @pytest.fixture
    async def rig(self, rig):
        rig.speech_handler.logic_controller.service_manager.ai_service = None
        return rig

    @pytest.mark.asyncio
    async def test_without_the_safety_word_the_words_are_typed_with_no_notice(
        self, rig,
    ):
        await rig.say("cancel", "fix", utterance_id=1)
        await _settle(rig)

        assert _typed(rig) == "cancel fix"
        assert rig.notices == []

    @pytest.mark.asyncio
    async def test_with_the_safety_word_a_notice_is_shown_and_nothing_is_typed(
        self, rig,
    ):
        await rig.say("x-ray", "cancel", "fix", utterance_id=1)
        await _settle(rig)

        assert rig.notices == [NO_JOB]
        assert _typed(rig) == ""


class TestALongerUtteranceIsOrdinaryWords:
    @pytest.mark.asyncio
    async def test_cancel_fix_the_typo_does_not_cancel_a_running_job(self, rig):
        await _start_a_fix(rig)

        await rig.say("cancel", "fix", "the", "typo", utterance_id=2)
        await rig.wait_until(
            lambda: rig.processor.word_queue.empty(),
            "the lane to take the words",
        )
        await asyncio.sleep(0.1)
        assert "Cancelling." not in rig.notices, (
            "a longer utterance that starts with cancel fix cancelled the job"
        )
        assert rig.service.cancel_requested is False

        rig.provider.release.set()
        await _settle(rig)

        assert rig.actions_sent.count("replace_selected_text") == 1
        assert "Cancelled." not in rig.notices
        assert _typed(rig) == "cancel fix the typo"


class TestTheLaneNeverLogsAnError:
    """Every ERROR log raises a Windows popup (main.py), so a cancel with
    nothing left to cancel must be logged below ERROR and leave the lane open.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefix", SAFETY)
    async def test_a_cancel_with_no_job_logs_no_error(
        self, rig_unstarted, caplog, prefix,
    ):
        rig = rig_unstarted
        rig.processor._in_word_event_turn = True
        with caplog.at_level(logging.DEBUG):
            rig.processor.begin_ai_cancel_lane()
            await rig.say(*prefix, "cancel", "fix", utterance_id=1)
            await rig.wait_until(
                lambda: rig.processor._deferred_word_events
                and rig.processor.word_queue.empty(),
                "the lane to take the words",
            )
            await asyncio.sleep(0.1)

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [], [r.getMessage() for r in errors]
        assert not rig.processor._ai_cancel_lane_task.done(), (
            "the lane stopped after a cancel that had nothing to cancel"
        )
        # Nothing was cancelled, so the utterance must NOT be recorded as
        # one the lane answered: the replay has to report the failure
        # (a notice or the typed words) instead of staying silent.
        assert list(rig.processor._lane_cancelled_utterances) == []


def _event(word, *, start=False, end=False, marker=False, utterance_id=1):
    return WordEvent(
        word,
        start_of_utterance=start,
        end_of_utterance=end,
        utterance_id=utterance_id,
        is_utterance_end_marker=marker,
    )


class TestTheRecognizer:
    @pytest.fixture
    def recognizer(self, rig_unstarted):
        rig = rig_unstarted
        return _CancelCommandRecognizer(
            _cancel_command_patterns(rig.catalog), rig.text_parser.matcher
        )

    def _feed(self, recognizer, words):
        fired = []
        for index, word in enumerate(words):
            fired.append(
                recognizer.observe(
                    _event(
                        word,
                        start=index == 0,
                        end=index == len(words) - 1,
                    ),
                    hotword="x-ray",
                )
            )
        return fired

    def test_without_the_safety_word_it_fires_on_the_last_word_only(
        self, recognizer,
    ):
        assert self._feed(recognizer, ["cancel", "fix"]) == [False, True]

    def test_without_the_safety_word_a_longer_utterance_never_fires(
        self, recognizer,
    ):
        assert self._feed(recognizer, ["cancel", "fix", "the", "typo"]) == [
            False, False, False, False,
        ]

    def test_without_the_safety_word_a_mid_utterance_cancel_never_fires(
        self, recognizer,
    ):
        assert self._feed(recognizer, ["please", "cancel", "fix"]) == [
            False, False, False,
        ]

    def test_with_the_safety_word_it_still_fires_on_the_word_that_completes_it(
        self, recognizer,
    ):
        fired = []
        for index, word in enumerate(["x-ray", "cancel", "fix", "later"]):
            fired.append(
                recognizer.observe(
                    _event(word, start=index == 0), hotword="x-ray"
                )
            )
        assert fired == [False, False, True, False]

    def test_the_end_marker_fires_when_the_last_word_carried_no_end_flag(
        self, recognizer,
    ):
        fired = [
            recognizer.observe(_event("cancel", start=True), hotword="x-ray"),
            recognizer.observe(_event("fix"), hotword="x-ray"),
            recognizer.observe(_event("", marker=True), hotword="x-ray"),
        ]
        assert fired == [False, False, True]

    def test_the_hotword_of_a_later_utterance_arms_after_a_bare_utterance(
        self, recognizer,
    ):
        # A bare utterance that is not a cancel must leave nothing behind:
        # the next utterance opens with the hotword and is judged as one.
        first = recognizer.observe(
            _event("hello", start=True, end=True, utterance_id=1),
            hotword="x-ray",
        )
        fired = [
            recognizer.observe(
                _event(word, start=index == 0, utterance_id=2),
                hotword="x-ray",
            )
            for index, word in enumerate(["x-ray", "cancel", "fix"])
        ]
        assert first is False
        assert fired == [False, False, True]

    @pytest.fixture
    def gated(self, rig_unstarted):
        """A cancel pattern that still carries requires_hotword (a user's own)."""
        import re

        rig = rig_unstarted
        shipped = _cancel_command_patterns(rig.catalog)
        assert shipped, "the catalog has no cancel pattern to copy"
        patterns = [dict(shipped[0], requires_hotword=True)]
        patterns.append(dict(
            shipped[0], requires_hotword=True,
            compiled_pattern=re.compile(r"^stop$", re.IGNORECASE),
        ))
        return _CancelCommandRecognizer(patterns, rig.text_parser.matcher)

    def test_a_hotword_gated_cancel_pattern_never_fires_without_the_hotword(
        self, gated,
    ):
        # Each line below reaches a different place that judges bare words.
        # The last word of a bare utterance:
        assert [
            gated.observe(_event("cancel", start=True), hotword="x-ray"),
            gated.observe(_event("fix", end=True), hotword="x-ray"),
        ] == [False, False]
        # A bare utterance of one word, judged on its first word:
        assert gated.observe(
            _event("stop", start=True, end=True), hotword="x-ray"
        ) is False
        # A bare utterance ended by the end marker:
        assert [
            gated.observe(_event("cancel", start=True), hotword="x-ray"),
            gated.observe(_event("fix"), hotword="x-ray"),
            gated.observe(_event("", marker=True), hotword="x-ray"),
        ] == [False, False, False]
        # A retraction whose corrected text is bare:
        marker = WordEvent(
            "", start_of_utterance=False, end_of_utterance=False,
            utterance_id=1, is_retraction_marker=True,
            retraction_full_text="cancel fix",
        )
        assert gated.observe(marker, hotword="x-ray") is False

    def test_a_hotword_gated_cancel_pattern_still_fires_after_the_hotword(
        self, gated,
    ):
        fired = [
            gated.observe(
                _event(word, start=index == 0), hotword="x-ray"
            )
            for index, word in enumerate(["x-ray", "cancel", "fix"])
        ]
        assert fired == [False, False, True]

    @pytest.mark.parametrize(
        ("corrected", "expected"),
        [
            ("cancel fix", True),
            ("Cancel fix.", True),
            ("cancel fix the typo", False),
            ("please cancel fix", False),
        ],
    )
    def test_a_retraction_without_the_safety_word_fires_only_on_the_whole_command(
        self, recognizer, corrected, expected,
    ):
        marker = WordEvent(
            "",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text=corrected,
        )
        assert recognizer.observe(marker, hotword="x-ray") is expected
