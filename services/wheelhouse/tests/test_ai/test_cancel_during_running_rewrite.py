"""wh-cancel-fix-running-rewrite: "cancel fix" must stop a running rewrite.

Before this change the word-event loop was strictly serial: ``_processing_loop``
awaited ``process_word_event`` for one event before it read the next, and the
EXECUTE branch awaited the AI action, which awaited the model. The words of
"x-ray cancel fix" therefore sat in ``word_queue`` for the whole model call and
reached ``cancel_fix`` only after the replacement had been pasted, when
``ai.is_processing()`` was already False -- so the action set no flag and
showed no notice.

These tests drive the real ``SpeechProcessor``, the real ``TextParser``, the
real ``ActionFunctions`` and the real ``AIService``. Only the provider and the
Input-process round trips are stand-ins, so the cancellation checks that decide
the outcome (``AIService._transform_text`` after the provider call, and step 4
of ``_run_ai_text_transform`` before the paste) are the shipped ones.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai.providers.openai_compat import ChatResult, ChatStatus
from ai.service import AIService
from speech.command_engine import TextParser
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.word_event import WordEvent

PATTERNS_PATH = (
    Path(__file__).resolve().parents[2] / "speech" / "config" / "patterns.toml"
)

# Long enough that a genuine hang fails the test, short enough that a failing
# run does not sit for a minute.
_WAIT_S = 5.0


class _DelayingProvider:
    """A provider whose chat() blocks until the test releases it.

    ``in_flight`` is set the moment the model call starts, so a test can wait
    for the exact window the bead is about: the request is out and the answer
    has not come back.
    """

    def __init__(self, reply: str = "the rewritten text"):
        self.in_flight = asyncio.Event()
        self.release = asyncio.Event()
        self.reply = reply
        self.calls = 0

    async def chat(self, messages, max_tokens=None, **kwargs):
        self.calls += 1
        self.in_flight.set()
        await self.release.wait()
        return ChatResult(status=ChatStatus.OK, text=self.reply)

    async def is_available(self):
        return True


def _capture_result():
    return {
        "text": "the original text",
        "flush_failed": False,
        "select_all_fallback": False,
        "copy_failed": False,
        "capture_failed": False,
        "capture_token": "capture-token",
        "target_hwnd": 4242,
    }


class _Rig:
    """Everything the processor needs, plus the two records a test reads."""

    def __init__(self):
        self.requests = []          # (action, params) sent to the Input process
        self.gui_actions = []       # every dict sent to the GUI process

        self.app = MagicMock()
        self.app.send_request = AsyncMock(side_effect=self._route)
        self.app.send_command = AsyncMock()

        self.provider = _DelayingProvider()

        config = MagicMock()
        # ``AIService._ai_off`` reads all three of these, and an empty
        # base_url alone makes ``is_ready()`` False whatever ``_ready`` says.
        config.get = MagicMock(side_effect=lambda key, default=None: {
            "ai.enabled": True,
            "ai.server.enabled": True,
            "ai.server.base_url": "http://localhost:8781/v1",
            "ai.server.model": "stand-in-model",
            "ai.server.timeout_s": 60,
        }.get(key, default))
        self.service = AIService(config)
        self.service._provider = self.provider
        self.service._ready = True
        self.speech_handler = MagicMock()
        self.speech_handler.app = self.app
        self.speech_handler.logic_controller.service_manager.ai_service = (
            self.service
        )
        # Every AI status line reaches the user as a show_notification
        # action on state_to_gui_queue; ActionFunctions._send_gui_action
        # puts it there with put_nowait. AIService.speak and speak_brief
        # no longer exist, so this queue is the only record of what the
        # user is told.
        (
            self.speech_handler.logic_controller.state_manager
            .state_to_gui_queue.put_nowait
        ) = self.gui_actions.append

        self.catalog = PatternCatalog(str(PATTERNS_PATH))
        self.text_parser = TextParser(self.speech_handler, self.catalog)
        self.processor = SpeechProcessor(
            word_queue=asyncio.Queue(),
            catalog=self.catalog,
            text_parser=self.text_parser,
            app=self.app,
            replacement_timeout_ms=400,
            command_timeout_ms=1000,
        )
        self.speech_handler.speech_processor = self.processor

    async def _route(self, action, params=None, **kwargs):
        self.requests.append((action, params))
        if action == "capture_selected_text":
            return _capture_result()
        if action == "replace_selected_text":
            return {"success": True}
        if action == "press_key_verified":
            return {"success": True}
        return {}

    @property
    def actions_sent(self):
        return [action for action, _ in self.requests]

    @property
    def typed_text(self):
        """Every string the Input process was asked to type, in order."""
        return [
            params.get("insertion_string", "")
            for action, params in self.requests
            if action == "intelligent_insert_text"
        ]

    @property
    def text_typed_without_waiting(self):
        """Text typed through send_command instead of send_request.

        The shipped ``insert_text`` action -- what "type X", "dictate X" and
        "literal X" run -- does not await the Input process, so it never
        reaches ``send_request`` and ``typed_text`` above cannot see it.
        Ordinary dictation does await, which is why the two records are
        separate rather than merged.
        """
        return [
            call.args[0].get("params", {}).get("insertion_string", "")
            for call in self.app.send_command.call_args_list
            if call.args
            and call.args[0].get("action") == "intelligent_insert_text"
        ]

    @property
    def notices(self):
        """Every status message the user would see, in order."""
        return [
            action.get("message")
            for action in self.gui_actions
            if action.get("action") == "show_notification"
        ]

    async def say(self, *words, utterance_id=1, end_marker=True):
        """Put one utterance's words on the queue, then its end marker.

        The end marker is what the STT intake sends after the last word of an
        utterance, and it is what finalizes a buffer whose pattern could still
        have grown (``^fix`` has no closing anchor). Sending the words without
        it would leave the command waiting on the command timeout.
        """
        for index, word in enumerate(words):
            await self.processor.word_queue.put(
                WordEvent(
                    word,
                    start_of_utterance=(index == 0),
                    end_of_utterance=(index == len(words) - 1),
                    utterance_id=utterance_id,
                )
            )
        if end_marker:
            await self.processor.word_queue.put(
                WordEvent(
                    "",
                    start_of_utterance=False,
                    end_of_utterance=True,
                    utterance_id=utterance_id,
                    is_utterance_end_marker=True,
                )
            )

    async def wait_until(self, predicate, what):
        """Poll until predicate() is true, or fail naming what was awaited."""
        deadline = asyncio.get_running_loop().time() + _WAIT_S
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        pytest.fail(f"timed out after {_WAIT_S}s waiting for {what}")


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
    """A rig whose processing loop was never started.

    For the ask_ai test: no shipped pattern calls that action, so it is driven
    directly, and starting the loop as well would put a second reader on
    word_queue -- which is exactly what the lane's turn guard exists to
    prevent, and not the state a real AI call is in.
    """
    rig = _Rig()
    try:
        yield rig
    finally:
        rig.provider.release.set()
        rig.processor.end_ai_cancel_lane()


class TestCancelReachesTheActionWhileTheModelRuns:
    """A1: the cancel spoken during the model call stops the rewrite."""

    @pytest.mark.asyncio
    async def test_cancel_spoken_during_the_call_prevents_the_paste(self, rig):
        await rig.say("x-ray", "fix", utterance_id=1)
        await asyncio.wait_for(rig.provider.in_flight.wait(), timeout=_WAIT_S)

        await rig.say("x-ray", "cancel", "fix", utterance_id=2)

        # The whole defect: today these words wait in word_queue behind the
        # model call, so the cancel action never runs while it could matter.
        await rig.wait_until(
            lambda: "Cancelling." in rig.notices,
            'the cancel acknowledgement "Cancelling."',
        )

        rig.provider.release.set()
        await rig.wait_until(
            lambda: not rig.service.is_processing(),
            "the AI transform to finish",
        )

        assert "replace_selected_text" not in rig.actions_sent, (
            "the replacement was pasted although the rewrite was cancelled"
        )
        assert "Done." not in rig.notices, (
            'the transform spoke "Done." although the rewrite was cancelled'
        )
        assert "Cancelled." in rig.notices, (
            "the transform did not report the cancelled outcome"
        )


# The five shipped rewrite triggers, spelled the way a user says them.
# "make formal" and "translate to ..." require the hotword; the other three
# are whole_utterance_only and need none. Read from
# speech/config/patterns.toml, not recalled.
_REWRITE_TRIGGERS = [
    pytest.param(("simplify",), id="simplify"),
    pytest.param(("shorten",), id="shorten"),
    pytest.param(("x-ray", "make", "formal"), id="make-formal"),
    pytest.param(("pirate",), id="pirate"),
    pytest.param(("x-ray", "translate", "to", "french"), id="translate-to"),
]


class TestEveryRewriteTriggerSharesTheFix:
    """A2: all five shipped rewrite triggers reach the same helper."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trigger", _REWRITE_TRIGGERS)
    async def test_the_cancel_stops_this_trigger(self, rig, trigger):
        await rig.say(*trigger, utterance_id=1)
        await asyncio.wait_for(rig.provider.in_flight.wait(), timeout=_WAIT_S)

        await rig.say("x-ray", "cancel", "fix", utterance_id=2)
        await rig.wait_until(
            lambda: "Cancelling." in rig.notices,
            f'the cancel acknowledgement for {" ".join(trigger)!r}',
        )

        rig.provider.release.set()
        await rig.wait_until(
            lambda: not rig.service.is_processing(),
            "the AI transform to finish",
        )

        assert "replace_selected_text" not in rig.actions_sent
        assert "Done." not in rig.notices
        assert "Cancelled." in rig.notices


class TestAskAiSharesTheFix:
    """A2: ask_ai gets the lane too.

    No shipped pattern calls ask_ai -- ``grep -n "ask_ai"
    speech/config/patterns.toml`` returns nothing -- so the action is driven
    directly. The processing loop is deliberately NOT started: while an AI
    command runs, the lane is the only reader of word_queue, and that is the
    state this test reproduces. ``_in_word_event_turn`` is set because the
    real caller would be inside one.
    """

    @pytest.mark.asyncio
    async def test_cancel_during_the_question_cancels_it(self, rig_unstarted):
        rig = rig_unstarted
        rig.processor._in_word_event_turn = True
        actions = rig.text_parser.action_functions

        asking = asyncio.ensure_future(actions.ask_ai("what is the time"))
        await asyncio.wait_for(rig.provider.in_flight.wait(), timeout=_WAIT_S)

        await rig.say("x-ray", "cancel", "fix", utterance_id=1)
        await rig.wait_until(
            lambda: "Cancelling." in rig.notices,
            'the cancel acknowledgement "Cancelling." during ask_ai',
        )

        rig.provider.release.set()
        with pytest.raises(RuntimeError, match="request cancelled"):
            await asyncio.wait_for(asking, timeout=_WAIT_S)

        # The lane deferred the words rather than dropping them.
        assert [
            event.word for event in rig.processor._deferred_word_events
        ] == ["x-ray", "cancel", "fix", ""]


class TestCancelAroundThePaste:
    """A3: after the answer but before the paste, and after the paste."""

    @pytest.mark.asyncio
    async def test_a_cancel_after_the_answer_still_stops_the_paste(self, rig):
        actions = rig.text_parser.action_functions

        async def answer_then_cancel(ai, captured):
            # The model answered; the cancel lands before the paste is sent.
            ai.cancel_requested = True
            return ChatResult(status=ChatStatus.OK, text="the rewritten text")

        await actions._run_ai_text_transform(
            send=answer_then_cancel,
            working_word="Rewriting",
            no_text_message="No text to rewrite.",
            failed_message="Rewrite failed. Original text preserved.",
        )

        assert "replace_selected_text" not in rig.actions_sent
        assert "Done." not in rig.notices
        assert "Cancelled." in rig.notices
        assert rig.service.cancel_requested is False, (
            "the unconsumed cancel flag would cancel the next fix"
        )

    @pytest.mark.asyncio
    async def test_a_cancel_after_the_paste_behaves_as_before(self, rig):
        rig.provider.release.set()
        await rig.say("x-ray", "fix", utterance_id=1)
        await rig.wait_until(
            lambda: "replace_selected_text" in rig.actions_sent,
            "the replacement paste",
        )
        await rig.wait_until(
            lambda: not rig.service.is_processing(),
            "the AI transform to finish",
        )
        assert "Done." in rig.notices

        notices_before = list(rig.notices)
        await rig.say("x-ray", "cancel", "fix", utterance_id=2)
        await rig.wait_until(
            lambda: rig.processor.word_queue.empty(),
            "the cancel utterance to be taken off the queue",
        )
        await asyncio.sleep(0.05)

        # Nothing to cancel: the action returns without showing a notice,
        # exactly as it did before this change.
        assert rig.notices == notices_before
        assert rig.service.cancel_requested is False
        assert rig.actions_sent.count("replace_selected_text") == 1


class TestEveryOtherEventKeepsItsOrder:
    """A4: the deferred events keep the order the queue would have given."""

    @pytest.mark.asyncio
    async def test_words_spoken_during_the_call_are_typed_after_the_paste(
        self, rig,
    ):
        await rig.say("x-ray", "fix", utterance_id=1)
        await asyncio.wait_for(rig.provider.in_flight.wait(), timeout=_WAIT_S)

        await rig.say("hello", "world", utterance_id=2)
        await rig.wait_until(
            lambda: rig.processor.word_queue.empty(),
            "the lane to take the dictated words",
        )
        assert "intelligent_insert_text" not in rig.actions_sent, (
            "a word spoken during the model call was typed before the paste"
        )

        rig.provider.release.set()
        await rig.wait_until(
            lambda: not rig.service.is_processing(),
            "the AI transform to finish",
        )
        await rig.wait_until(
            lambda: "intelligent_insert_text" in rig.actions_sent,
            "the deferred words to be typed",
        )

        order = rig.actions_sent
        assert order.index("replace_selected_text") < order.index(
            "intelligent_insert_text"
        ), "the deferred dictation was typed before the paste"

        typed = rig.typed_text
        joined = " ".join(typed)
        assert "hello" in joined and "world" in joined, typed
        assert joined.index("hello") < joined.index("world"), (
            f"the deferred words were typed out of spoken order: {typed}"
        )


class TestTheLaneOnlyOpensInsideAWordEventTurn:
    """The lane is a SECOND reader of word_queue, so it must not outlive the
    one turn that opened it, and closing it must not lose what it took.

    Both properties are asserted directly on the processor rather than through
    a status notice: outside a word-event turn there is no outcome to watch,
    because the correct behaviour is that nothing happens at all.
    """

    @pytest.mark.asyncio
    async def test_an_ai_call_outside_the_word_loop_opens_no_lane(
        self, rig_unstarted,
    ):
        rig = rig_unstarted
        assert rig.processor._in_word_event_turn is False

        rig.processor.begin_ai_cancel_lane()
        await rig.processor.word_queue.put(
            WordEvent("hello", start_of_utterance=True, end_of_utterance=True)
        )
        await asyncio.sleep(0.05)

        assert rig.processor._ai_cancel_lane_task is None
        assert rig.processor.word_queue.qsize() == 1, (
            "a lane opened outside a word-event turn took an event the "
            "processing loop was going to read"
        )
        assert rig.processor._deferred_word_events == []

    @pytest.mark.asyncio
    async def test_closing_the_lane_keeps_the_events_it_deferred(
        self, rig_unstarted,
    ):
        rig = rig_unstarted
        rig.processor._in_word_event_turn = True
        rig.processor.begin_ai_cancel_lane()

        await rig.processor.word_queue.put(
            WordEvent("hello", start_of_utterance=True, end_of_utterance=True)
        )
        await rig.wait_until(
            lambda: rig.processor._deferred_word_events,
            "the lane to defer the dictated word",
        )

        rig.processor.end_ai_cancel_lane()

        assert [
            event.word for event in rig.processor._deferred_word_events
        ] == ["hello"], (
            "closing the lane threw away the events it had taken off "
            "word_queue; nothing else would ever process them"
        )
        assert rig.processor._ai_cancel_lane_task is None


class TestTheCancelStillNeedsTheHotword:
    """The lane recognises a hotword-gated command, so it needs the hotword.

    ``cancel fix`` carries ``requires_hotword = true`` in
    speech/config/patterns.toml. Dictating those two words during a model call
    is ordinary text, and text must not cancel the call.
    """

    @pytest.mark.asyncio
    async def test_the_trigger_words_alone_do_not_cancel(self, rig):
        await rig.say("x-ray", "fix", utterance_id=1)
        await asyncio.wait_for(rig.provider.in_flight.wait(), timeout=_WAIT_S)

        await rig.say("cancel", "fix", utterance_id=2)
        await rig.wait_until(
            lambda: rig.processor.word_queue.empty(),
            "the lane to take the dictated words",
        )
        assert "Cancelling." not in rig.notices, (
            "words dictated without the hotword cancelled the running call"
        )

        rig.provider.release.set()
        await rig.wait_until(
            lambda: "Done." in rig.notices,
            "the rewrite to finish uncancelled",
        )
        assert rig.actions_sent.count("replace_selected_text") == 1
        assert "Cancelled." not in rig.notices


class TestAnEventQueuedAfterTheLaneClosesRunsLast:
    """A4, second half: the drain happens before the next read of word_queue.

    ``_processing_loop`` empties ``_deferred_word_events`` at the top of every
    iteration, before it reads word_queue at all, so an event that reaches the
    queue after the lane closed cannot overtake an event the lane deferred --
    however small the gap between the two is. This test creates the smallest
    gap there is: it queues the late word from inside ``end_ai_cancel_lane``,
    which runs while the loop is still inside the word event that opened the
    lane and has not yet looked at either list.
    """

    @pytest.mark.asyncio
    async def test_a_word_queued_at_the_close_is_typed_after_the_deferred_ones(
        self, rig,
    ):
        original_end = rig.processor.end_ai_cancel_lane
        queued = []

        def end_and_queue_a_late_word():
            # end_ai_cancel_lane runs at the end of EVERY word-event turn, so
            # the late word is queued only on the close that actually shuts a
            # lane -- the one inside the AI action's own finally.
            lane_was_open = rig.processor._ai_cancel_lane_task is not None
            original_end()
            if not lane_was_open or queued:
                return
            queued.append(True)
            rig.processor.word_queue.put_nowait(
                WordEvent(
                    "later",
                    start_of_utterance=True,
                    end_of_utterance=True,
                    utterance_id=3,
                )
            )
            rig.processor.word_queue.put_nowait(
                WordEvent(
                    "",
                    start_of_utterance=False,
                    end_of_utterance=True,
                    utterance_id=3,
                    is_utterance_end_marker=True,
                )
            )

        rig.processor.end_ai_cancel_lane = end_and_queue_a_late_word

        await rig.say("x-ray", "fix", utterance_id=1)
        await asyncio.wait_for(rig.provider.in_flight.wait(), timeout=_WAIT_S)

        await rig.say("hello", "world", utterance_id=2)
        await rig.wait_until(
            lambda: rig.processor.word_queue.empty(),
            "the lane to take the dictated words",
        )

        rig.provider.release.set()
        await rig.wait_until(
            lambda: "later" in " ".join(rig.typed_text),
            "the word queued at the moment the lane closed to be typed",
        )

        assert queued, "no lane was ever closed, so nothing was queued late"
        joined = " ".join(rig.typed_text)
        # Asserted before the two comparisons below because index() would
        # raise ValueError on a missing word, and a test that ends in an
        # unrelated exception has proved nothing about the order.
        assert "hello" in joined and "world" in joined, (
            f"the deferred words never reached the typing path: {joined!r}"
        )
        assert joined.index("hello") < joined.index("later"), joined
        assert joined.index("world") < joined.index("later"), joined


class TestTheRecognisedCancelReplaysAsACommand:
    """The lane recognises the cancel; it does not consume it.

    ``_ai_cancel_lane_loop`` appends every event to ``_deferred_word_events``
    BEFORE it asks the recognizer, and nothing removes the ones that matched.
    The cancel utterance therefore reaches the truth table again when the lane
    closes, and ``cancel_fix`` runs a second time.

    That second run must stay a no-op, and it is one:
    ``AIService.is_processing()`` returns ``self._processing_lock.locked()``,
    and ``_run_ai_text_transform`` leaves ``async with ai._processing_lock``
    before the word-event turn ends. The replayed command therefore finds
    nothing in flight, sets no flag and shows no notice.

    Removing the recognised words from the deferred list instead was
    considered and rejected. The lane would then be routing rather than
    recognising, and it would drop words in every case where the real truth
    table would have used them differently -- a greedy capture, a replacement
    prefix, a longer command that only looks like the cancel so far.
    """

    @pytest.mark.asyncio
    async def test_the_replayed_command_shows_nothing_and_sets_nothing(
        self, rig,
    ):
        await rig.say("x-ray", "fix", utterance_id=1)
        await asyncio.wait_for(rig.provider.in_flight.wait(), timeout=_WAIT_S)

        await rig.say("x-ray", "cancel", "fix", utterance_id=2)
        await rig.wait_until(
            lambda: "Cancelling." in rig.notices,
            'the cancel acknowledgement "Cancelling."',
        )

        rig.provider.release.set()
        await rig.wait_until(
            lambda: not rig.service.is_processing(),
            "the AI transform to finish",
        )
        await rig.wait_until(
            lambda: not rig.processor._deferred_word_events
            and rig.processor.word_queue.empty(),
            "the deferred cancel utterance to be replayed",
        )
        await asyncio.sleep(0.05)

        assert rig.notices.count("Cancelling.") == 1, (
            f"the replayed cancel command showed a second notice: "
            f"{rig.notices}"
        )
        assert rig.service.cancel_requested is False, (
            "the replayed cancel command left a flag set, which would cancel "
            "the next rewrite"
        )
        assert "intelligent_insert_text" not in rig.actions_sent, (
            "the replayed cancel utterance was typed as dictation instead of "
            "running as the command it is"
        )


class TestAFailedCallDoesNotLeakTheCancelFlag:
    """Finding wh-cancel-fix-running-rewrite.1.1, filed by deepseek.

    The lane created a window this code had never had: ``cancel_fix`` can now
    run while ``_run_ai_text_transform`` holds ``ai._processing_lock``, so it
    sets ``ai.cancel_requested`` mid-call. Every exit that ends the call
    normally consumes that flag -- step 3a for a CANCELLED result, step 4
    before the paste -- but the not-ok exits do not, and the real one that
    matters awaits ``ai.recheck_ready()`` inside the lane, which takes two
    five-second probes with the "Rewriting..." dialog still showing. That is
    exactly when a user says "cancel fix".

    A flag left set is not a cosmetic leak. ``AIService._transform_text``
    checks it BEFORE the provider call, so the user's next rewrite returns
    CANCELLED with no model call at all: it silently does nothing and they
    have to say it again.
    """

    @pytest.mark.asyncio
    async def test_a_cancel_during_a_failed_call_does_not_cancel_the_next_one(
        self, rig,
    ):
        actions = rig.text_parser.action_functions

        async def fail_after_a_cancel(ai, captured):
            # What the lane does while this call is still in flight.
            ai.cancel_requested = True
            return ChatResult(status=ChatStatus.TRANSPORT_ERROR)

        await actions._run_ai_text_transform(
            send=fail_after_a_cancel,
            working_word="Rewriting",
            no_text_message="No text to rewrite.",
            failed_message="Rewrite failed. Original text preserved.",
        )

        assert rig.service.cancel_requested is False, (
            "the failed call left the cancel flag set, and the next rewrite "
            "will consume it and do nothing"
        )

        # The harm the flag does, spelled out: the next rewrite must reach the
        # model and paste, not return CANCELLED before the call.
        rig.provider.release.set()
        await rig.say("x-ray", "fix", utterance_id=1)
        await rig.wait_until(
            lambda: "replace_selected_text" in rig.actions_sent,
            "the next rewrite to paste its replacement",
        )
        assert rig.provider.calls == 1, (
            "the next rewrite never reached the model; the stale cancel flag "
            "cancelled it before the call"
        )
class TestTheLaneRecognisesWhatTheRouterWouldRecognise:
    """Codex round 2 findings .1.2, .1.3 and .1.4.

    The lane runs its own recognizer rather than the command engine, because
    the engine is not re-entrant while a word-event turn is open. Every place
    that recognizer differs from the shipped routing is a place the lane
    either fires on something the user did not mean as a command, or misses
    something the user did. These three tests pin the three differences codex
    measured, each against the shipped patterns.
    """

    @pytest.mark.asyncio
    async def test_an_escaped_cancel_phrase_does_not_cancel(self, rig):
        """Finding .1.4: "type x-ray cancel fix" is text, not a command.

        The shipped ``^(?:type|dictate) (.+)$`` and ``literal (.+)$`` patterns
        exist so a user can dictate words that would otherwise be commands.
        The router honours that escape because it arms the hotword only on a
        fresh utterance in IDLE (speech/router.py:113-125). A lane that arms
        on a hotword anywhere in the utterance takes the escape away for as
        long as an AI call is running.
        """
        await rig.say("x-ray", "fix", utterance_id=1)
        await asyncio.wait_for(rig.provider.in_flight.wait(), timeout=_WAIT_S)

        await rig.say("type", "x-ray", "cancel", "fix", utterance_id=2)

        # Wait until the lane has taken all four words, so the recognizer has
        # already seen the last one and made its decision. Four, not five: the
        # end marker arrives after that decision, and on the branch this test
        # exists to catch the lane is already closed by the time it does.
        await rig.wait_until(
            lambda: len(rig.processor._deferred_word_events) >= 4,
            "the lane to defer all four words of the escaped utterance",
        )

        assert "Cancelling." not in rig.notices, (
            "an escaped 'type x-ray cancel fix' cancelled the rewrite; the "
            "text escape must protect its contents from acting as a command"
        )
        assert rig.service.cancel_requested is False, (
            "the escaped utterance set the cancel flag"
        )

        rig.provider.release.set()
        await rig.wait_until(
            lambda: "replace_selected_text" in rig.actions_sent,
            "the rewrite the user asked for to paste its replacement",
        )
        # The deferred events replay only after the action returns, so this
        # is a wait rather than a bare assertion.
        await rig.wait_until(
            lambda: "x-ray cancel fix" in rig.text_typed_without_waiting,
            "the escaped words to be typed as text once the rewrite finished",
        )

    @pytest.mark.asyncio
    async def test_a_punctuated_cancel_still_cancels(self, rig):
        """Finding .1.2: "cancel fix." must cancel, exactly as "cancel fix".

        Google speech recognition adds sentence punctuation when the supported
        ``enable_automatic_punctuation`` setting is on
        (services/stt_providers/google_stt_server/config.toml), and the words
        reach the processor with that punctuation attached. The command engine
        handles it in ``PatternMatcher._match_command_with_punct_retry``; a
        lane that matches the raw joined words does not.
        """
        await rig.say("x-ray", "fix", utterance_id=1)
        await asyncio.wait_for(rig.provider.in_flight.wait(), timeout=_WAIT_S)

        await rig.say("x-ray", "cancel", "fix.", utterance_id=2)

        await rig.wait_until(
            lambda: "Cancelling." in rig.notices,
            'the cancel acknowledgement "Cancelling." for a punctuated cancel',
        )

        rig.provider.release.set()
        await rig.wait_until(
            lambda: not rig.service.is_processing(),
            "the AI transform to finish",
        )
        assert "replace_selected_text" not in rig.actions_sent, (
            "the punctuated cancel was recognised but the paste still happened"
        )

    @pytest.mark.asyncio
    async def test_a_corrected_final_transcript_cancels(self, rig):
        """Finding .1.3: the cancel can arrive only in the corrected final.

        ``WebSocketManager._handle_mode3_retract`` queues ONE retraction
        marker carrying the whole corrected transcript in
        ``retraction_full_text``, then an end marker; the corrected words
        never arrive as word events
        (integrations/websocket_manager.py:426-464). A lane that resets on
        the marker and reads nothing from it cannot see a cancel that only
        the correction got right.
        """
        await rig.say("x-ray", "fix", utterance_id=1)
        await asyncio.wait_for(rig.provider.in_flight.wait(), timeout=_WAIT_S)

        # What the user said was heard as "x-ray cancel six" and corrected.
        await rig.processor.word_queue.put(
            WordEvent(
                "",
                start_of_utterance=False,
                end_of_utterance=False,
                utterance_id=2,
                is_retraction_marker=True,
                retraction_full_text="x-ray cancel fix",
            )
        )
        await rig.processor.word_queue.put(
            WordEvent(
                "",
                start_of_utterance=False,
                end_of_utterance=True,
                utterance_id=2,
                is_utterance_end_marker=True,
            )
        )

        await rig.wait_until(
            lambda: "Cancelling." in rig.notices,
            'the cancel acknowledgement "Cancelling." for a corrected final',
        )

        rig.provider.release.set()
        await rig.wait_until(
            lambda: not rig.service.is_processing(),
            "the AI transform to finish",
        )
        assert "replace_selected_text" not in rig.actions_sent, (
            "the corrected final said cancel and the paste still happened"
        )
