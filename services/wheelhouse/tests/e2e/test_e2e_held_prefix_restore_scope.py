"""The restore must put back only what the retract actually removed.

wh-spaced-punctuation-names-unresolved.3.1.2 (codex round 3). The
de2bb449 fix records the held words at ``end_utterance`` and puts them
back in front of a successful retract's replay. That record describes
what was HELD, not what is actually inside the next retractable span,
and three ordinary sequences separate the two.

The sibling module ``test_e2e_held_prefix_retract_scope.py`` measures
the same code without ever sending ``start_utterance``. Production
sends it from ``WebSocketManager`` before the first word of every new
utterance (integrations/websocket_manager.py:1292 from stable text,
1527 from a final-only utterance), and
``UIActionHandler.start_utterance`` resets the paste counter there
(ui/ui_action_handler.py:1126) exactly as ``end_utterance`` does at
1170. ``E2EPipelineHarness.send_word`` only queues a ``WordEvent``, so
the older module's measurements are taken in a world with one counter
reset where production has two. ``_start_utterance`` below sends the
IPC production sends, in production's position.

That gap is left in place on purpose (boss ruling, 2026-09-05):
teaching ``E2EPipelineHarness.send_word`` to send the command would
change the message stream every existing e2e test measures, so it
needs its own change with its own review. Anyone writing a new e2e
test whose subject is paste-counter accounting must send the command
themselves, as this module does.
"""
import asyncio
import time

import pytest

from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness
from services.wheelhouse.tests.test_screen_read_dictation_gate import (
    FakeController,
)


@pytest.fixture
async def harness(pattern_catalog):
    """A harness whose retract can actually reach send_backspaces."""
    h = E2EPipelineHarness(
        catalog=pattern_catalog,
        action_handler_foreground=True,
    )
    await h.start()
    yield h
    await h.stop()


async def _start_utterance(harness, utterance_id):
    """Send the start_utterance IPC production sends for a new utterance.

    Mirrors websocket_manager.py:1291-1294 and 1526-1529: the command
    goes out BEFORE the utterance's first word is queued, and its only
    parameter is the utterance id.
    """
    await harness.app.send_command({
        'action': 'start_utterance',
        'params': {'utterance_id': utterance_id},
    })


class _RetractWatcher:
    """Records how much text had been delivered when the retract went out."""

    def __init__(self, harness):
        self.harness = harness
        self.deliveries_at_retract = []
        self._original = harness.app.send_request

    def install(self):
        async def send_request(action, params=None):
            if action == "retract":
                self.deliveries_at_retract.append(
                    len(self.harness.recording.text_deliveries)
                )
            return await self._original(action, params)

        self.harness.app.send_request = send_request
        return self

    def screen_after_retract(self):
        """The text left on screen once the retract and the replay finish.

        The retract removes characters from the end of everything
        delivered before it, and the replay appends afterwards. The two
        recording lists have no shared ordering, so the split point is
        the delivery count captured at the retract IPC itself.
        """
        assert self.deliveries_at_retract, (
            "No retract IPC was sent, so there is nothing to measure. "
            "_handle_retraction returned before the request."
        )
        cut = self.deliveries_at_retract[0]
        deliveries = self.harness.recording.text_deliveries
        before = "".join(deliveries[:cut])
        removed = sum(self.harness.recording.backspace_sends)
        kept = before[:-removed] if removed else before
        return kept + "".join(deliveries[cut:])


def _hold_the_safety_timeout(harness):
    """Raise the watchdog bound for a test that leaves an utterance open.

    ``UtteranceClipboardManager._start_safety_timeout`` forces its own
    ``end_utterance`` one second after a ``start_utterance`` whose end
    never arrives. The method is at ui/utterance_clipboard_manager.py:633
    and the ``end_utterance`` call it forces is at :642. Cases G and
    H leave the third utterance open on purpose -- that is the whole
    sequence they measure -- so that timer would fire in the middle of
    every one of them.

    WHAT IT DOES NOT DO IS RESET THE PASTE COUNTER. This docstring said
    it did until 2026-09-06, and the claim was wrong. The forced end
    runs ``UtteranceClipboardManager.end_utterance``, which schedules a
    deferred clipboard restore and nothing else: a grep for
    ``reset_paste_counter`` over ui/utterance_clipboard_manager.py
    matches nothing, and the same grep over ui/ui_action_handler.py
    returns the only three callers, at :1126, :1170 and :1417. The
    manager is constructed with ``timeout_seconds`` alone
    (ui/ui_action_handler.py:786-790), so it holds no reference to the
    counter to reset. The timer therefore cannot empty the span these
    cases measure. What holding it off removes is the clipboard restore
    firing in the middle of the measurement, and the WARNING it logs
    reaching pytest's stderr.

    Called before the first command, while no timer is running:
    ``_start_safety_timeout`` reads this value each time it arms one.
    """
    harness.app.handler.utterance_manager.timeout_seconds = 30.0


class _TicketedGet:
    """Hands the processing loop one queue item per ticket.

    Case E parks the loop with a single event, which is enough when
    exactly one message has to wait. The .3.1.7 sequences below need
    the loop stopped between several messages, so this hands out one
    item at a time: every message is in the queue before the loop is
    allowed to take it, and no ordering depends on a sleep landing
    between two sends.

    Install it while the loop is inside its ORIGINAL ``get``, exactly
    as case E installs its gate: the item already being awaited is
    consumed normally, and the ticket holds the next iteration.
    """

    def __init__(self, harness):
        self.harness = harness
        self._original = harness.word_queue.get
        self._tickets = asyncio.Semaphore(0)

    def install(self):
        async def gated_get():
            await self._tickets.acquire()
            return await self._original()

        self.harness.word_queue.get = gated_get
        return self

    def release_one(self):
        """Let the loop take exactly one more item."""
        self._tickets.release()

    def remove(self):
        """Give the queue its own get back and wake the parked loop.

        The loop parks inside ``gated_get``'s ticket wait, and putting
        the attribute back does not wake a coroutine already waiting
        on the semaphore. Without this last ticket the loop never
        reaches the queue again and every later message -- the
        correction marker included -- sits there unprocessed.
        """
        del self.harness.word_queue.get
        self._tickets.release()


class TestTheRestorePutsBackOnlyWhatTheRetractRemoved:
    """One test per counterexample codex measured in round 3."""

    @pytest.mark.asyncio
    async def test_a_prefix_that_expired_before_the_next_start_is_not_doubled(
        self, harness
    ):
        """Case A: the hold expires in the gap between the two IPC sends.

        "open" is held across utterance 1's end, its 400 ms release
        deadline expires, and the word is typed. Utterance 2's
        ``start_utterance`` then resets the paste counter, so that
        "Open" has left the retractable span for good. A correction of
        utterance 2 must not type it a second time.
        """
        watcher = _RetractWatcher(harness).install()

        await _start_utterance(harness, 1)
        await harness.send_word("open", start_of_utterance=True)
        utterance_one = harness._utterance_counter
        await harness.send_utterance_end_marker(utterance_one)

        # Past the 400 ms release deadline: the hold flushes as
        # dictation and the word is typed while no utterance is open.
        await asyncio.sleep(0.65)
        assert harness.recording.text_deliveries, (
            "The held prefix never expired, so this test measures "
            "nothing about an expired prefix."
        )

        await _start_utterance(harness, 2)
        await harness.send_word("the", start_of_utterance=True)
        utterance_two = harness._utterance_counter
        await asyncio.sleep(0.05)

        await harness.send_retraction_marker(utterance_two, "this")
        await asyncio.sleep(0.15)

        screen = watcher.screen_after_retract()
        assert screen.lower().count("open") == 1, (
            "The expired prefix was typed before utterance 2's "
            "start_utterance reset the paste counter, so the retract "
            "could not have removed it. Restoring it puts a second "
            f"copy on screen. Screen: {screen!r}"
        )

    @pytest.mark.asyncio
    async def test_c_a_prefix_refused_by_the_screen_read_gate_is_never_typed(
        self, harness
    ):
        """Case C: the flush was refused, so nothing was ever displayed.

        ``_flush_pending_replacement_prefix_as_dictation`` clears the
        hold before it calls ``_send_to_dictation``, and that method
        returns without typing while a screen read is in flight
        (speech_processor.py:3841-3847). The record set at
        ``end_utterance`` still names the words, so a later correction
        inserts text the user never saw.

        THE ORDER BELOW IS THE WHOLE POINT OF THIS TEST. The refusal
        must happen AFTER utterance 2's start-of-utterance clear
        (speech_processor.py:1154), because that clear would erase a
        false record on its own. An earlier version of this test let
        the hold expire during a sleep before utterance 2 began, and
        the mutation gate proved the consequence: the mutation
        an-undelivered-prefix-is-recorded-anyway, which drops the
        ``surface is None`` half of the guard, SURVIVED. Utterance 2's
        own first word is what flushes the hold
        (speech_processor.py:3074-3081 and 3172), and the clear sits
        above the advance that reaches those lines, so holding the
        screen read across that word is what puts the refusal on the
        far side of the clear.
        """
        watcher = _RetractWatcher(harness).install()

        await _start_utterance(harness, 1)
        await harness.send_word("open", start_of_utterance=True)
        utterance_one = harness._utterance_counter
        await harness.send_utterance_end_marker(utterance_one)

        # The screen read starts while the prefix is still held and is
        # still running when utterance 2's first word arrives. "open
        # the" spells no name, so that word flushes the hold, and the
        # flush is refused.
        harness.processor.logic_controller = FakeController(
            since=time.monotonic(),
        )
        await _start_utterance(harness, 2)
        await harness.send_word("the", start_of_utterance=True)
        utterance_two = harness._utterance_counter
        await asyncio.sleep(0.05)
        assert not harness.recording.text_deliveries, (
            "The refused flush typed something, so this test is not "
            "measuring a refused delivery at all."
        )

        # The read ends, and the next word is typed. Without it the
        # retractable span is empty, the retract answers
        # not_retracted, and the restore never runs at all -- the test
        # would then pass for a reason that says nothing about the
        # record.
        harness.processor.logic_controller = None
        await harness.send_word("cat", delay_before_ms=40)
        await asyncio.sleep(0.05)
        assert harness.recording.text_deliveries, (
            "Nothing was typed after the screen read ended, so the "
            "retract has nothing to remove and the restore is never "
            "reached."
        )

        await harness.send_retraction_marker(utterance_two, "this")
        await asyncio.sleep(0.15)

        screen = watcher.screen_after_retract()
        assert "open" not in screen.lower(), (
            "The prefix was refused by the screen-read gate and never "
            "reached the screen, so the retract removed nothing of it. "
            f"The restore typed it anyway. Screen: {screen!r}"
        )

    @pytest.mark.asyncio
    async def test_b_words_dictated_because_of_a_boundary_stay_dictated(
        self, harness
    ):
        """Case B: the replay must not join across the utterance end.

        Utterance 1 says "question" and ends. Utterance 2's continuing
        word rejects the tentative join, so "Question mark" is typed
        literally. A correction of utterance 2 then replays the earlier
        words and the corrected final as ONE utterance
        (speech_processor.py:3776-3788), and the in-utterance matcher
        turns the literal pair into "?".
        """
        watcher = _RetractWatcher(harness).install()

        await _start_utterance(harness, 1)
        await harness.send_word("question", start_of_utterance=True)
        utterance_one = harness._utterance_counter
        await harness.send_utterance_end_marker(utterance_one)

        await _start_utterance(harness, 2)
        await harness.send_word("mark", start_of_utterance=True)
        utterance_two = harness._utterance_counter
        await harness.send_word("my", delay_before_ms=40)
        await harness.send_word("wards", delay_before_ms=40)
        await asyncio.sleep(0.05)

        await harness.send_retraction_marker(utterance_two, "mark my words")
        await asyncio.sleep(0.15)

        screen = watcher.screen_after_retract()
        # wh-spaced-punctuation-names-unresolved.3.1.10, ruled
        # 2026-09-06 after the full sweep at 23598702. THIS POSITIVE
        # ASSERTION COMES FIRST AND IT IS THE LOAD-BEARING ONE. The
        # negative assertion below cannot tell "the earlier word came
        # back literally" from "the earlier word never came back at
        # all": both leave no "?" on screen. A mutation that sent the
        # restore through the editor arm made the surface guard drop
        # the words entirely, and the negative assertion passed
        # vacuously while the mutation read as a survivor.
        assert "question" in screen.lower(), (
            "Utterance 1's own word is gone from the screen. A "
            "correction of utterance 2 must leave it standing, so "
            "losing it is a worse failure than joining it into a "
            f"mark. Screen: {screen!r}"
        )
        assert "?" not in screen, (
            "The earlier utterance's words were dictated literally "
            "because utterance 1 had ended. The correction replayed "
            "them inside utterance 2, so the name matcher joined them "
            f"into a mark. Screen: {screen!r}"
        )

    @pytest.mark.asyncio
    async def test_d_a_held_name_that_fires_its_mark_keeps_the_earlier_word(
        self, harness
    ):
        """Case D: the held name fires its mark inside the new span.

        wh-spaced-punctuation-names-unresolved.3.1.6, codex round 4.
        "question" is held across utterance 1's end. Utterance 2's
        first word "mark" completes the name without ending its own
        utterance, so the hold grows instead of firing
        (speech_processor.py:3222-3230), and the 400 ms release
        deadline fires the complete name through
        _fire_held_replacement_if_complete.

        That is a DELIVERY of the earlier utterance's word: the "?" on
        screen was spelled half by utterance 1. Before the fix, only
        _flush_pending_replacement_prefix_as_dictation promoted the
        boundary candidate, so a hold that resolved into a MARK
        recorded nothing at all, and the correction below removed the
        mark and replayed only its own final. The measured screen was
        'Marcus'.
        """
        watcher = _RetractWatcher(harness).install()

        await _start_utterance(harness, 1)
        await harness.send_word("question", start_of_utterance=True)
        utterance_one = harness._utterance_counter
        await harness.send_utterance_end_marker(utterance_one)

        await _start_utterance(harness, 2)
        await harness.send_word("mark", start_of_utterance=True)
        utterance_two = harness._utterance_counter

        await asyncio.sleep(0.65)
        typed = "".join(harness.recording.text_deliveries)
        assert "?" in typed, (
            f"The held name never fired its mark: {typed!r}"
        )

        await harness.send_retraction_marker(utterance_two, "Marcus")
        await asyncio.sleep(0.15)

        screen = watcher.screen_after_retract()
        assert "question" in screen.lower(), (
            "The mark was spelled partly by utterance 1's word, and "
            "the correction removed the mark, so that word must come "
            f"back. Screen: {screen!r}"
        )

    @pytest.mark.asyncio
    async def test_e_a_prefix_flushed_after_the_next_start_is_restored(
        self, harness
    ):
        """Case E: the release sentinel outlives the next start_utterance.

        wh-spaced-punctuation-names-unresolved.3.1.5, codex round 4.
        The release deadline queues a sentinel onto ``word_queue``
        (speech_processor.py:4450) and the processing loop does the
        flush when it reaches it. Nothing makes that loop reach the
        sentinel before the next utterance's ``start_utterance``
        command: WebSocketManager awaits the command and only then
        queues the utterance's first word
        (integrations/websocket_manager.py:1295), so a sentinel already
        sitting in the queue is flushed INSIDE the span that command
        opened.

        Those characters are therefore inside what a correction of this
        utterance retracts, and the correction must put them back. Case
        A above is the same two events in the opposite order and needs
        the opposite answer, so the flagged word alone cannot decide
        it; the start_utterance generation is what separates the two.
        Before the fix, the flagged word cleared the record
        unconditionally and the measured screen was 'This'.

        The ordering is made exact rather than raced: the loop is
        parked at its next ``word_queue.get`` before the end marker
        goes in, so the deadline expires with the loop asleep and the
        sentinel simply waits.
        """
        watcher = _RetractWatcher(harness).install()

        await _start_utterance(harness, 1)
        await harness.send_word("open", start_of_utterance=True)
        utterance_one = harness._utterance_counter

        # Installed BEFORE the end marker on purpose: the loop is still
        # inside the ORIGINAL get at this moment, so the marker is
        # consumed normally and the gate holds the very next iteration
        # -- the one that would otherwise take the sentinel.
        released = asyncio.Event()
        original_get = harness.word_queue.get

        async def gated_get():
            await released.wait()
            return await original_get()

        harness.word_queue.get = gated_get
        try:
            await harness.send_utterance_end_marker(utterance_one)
            await asyncio.sleep(0.65)

            assert not harness.recording.text_deliveries, (
                "The loop was supposed to be parked at word_queue.get, "
                "so nothing can have been typed yet: "
                f"{harness.recording.text_deliveries!r}"
            )
            assert harness.word_queue.qsize() >= 1, (
                "The release deadline never queued its sentinel, so "
                "this test would measure the wrong ordering."
            )

            # Only now does the new utterance start. The sentinel is
            # already in the queue, and the flagged word goes in behind
            # it, exactly as production orders them.
            await _start_utterance(harness, 2)
            await harness.send_word(
                "the", start_of_utterance=True, allow_processing=False,
            )
            utterance_two = harness._utterance_counter
        finally:
            released.set()
            del harness.word_queue.get

        await asyncio.sleep(0.25)
        typed = "".join(harness.recording.text_deliveries)
        assert "open" in typed.lower(), (
            f"The held prefix never flushed: {typed!r}"
        )

        await harness.send_retraction_marker(utterance_two, "this")
        await asyncio.sleep(0.15)

        screen = watcher.screen_after_retract()
        assert screen.lower().count("open") == 1, (
            "The prefix was typed after utterance 2's start_utterance, "
            "so the retract removed it and the replay must put it back "
            f"exactly once. Screen: {screen!r}"
        )

    @pytest.mark.asyncio
    async def test_f_a_gate_that_turns_refusing_mid_step_records_nothing(
        self, harness
    ):
        """Case F: the screen-read gate changes answer inside the step.

        wh-spaced-punctuation-names-unresolved.3.1.6, boss ruling
        2026-09-06. The withdrawn fix read the gate itself before
        running the decision, and command_engine consults the same gate
        again at command_engine.py:463-472. An await sits between those
        two moments -- the editor consult at :419-421 -- so the answer
        can change in both directions:

          allowed, then refusing: the pre-check records a delivery for
          a mark the rule then dropped, and the correction replays a
          word the user never saw. That is the doubling codex round 3
          case C already ruled out.

          refusing, then typed: the pre-check records nothing for text
          the rule really typed, and the correction loses the earlier
          utterance's words.

        This test measures the first direction, because it is the one
        an ordinary screen read produces: the read starts while the
        replacement is already in flight. The gate answers "allowed"
        when the step begins and "refusing" by the time the rule asks,
        and the flip happens inside the editor await itself, which is
        exactly the gap. The mark is never typed, so nothing may be
        recorded for it.

        THE READ THEN ENDS AND ONE ORDINARY WORD IS TYPED, for the
        reason case C states above: with nothing delivered, the retract
        answers nothing_to_retract and the restore never runs, so the
        test would pass while proving nothing. Measured 2026-09-06 --
        without that word the correction produced no delivery at all
        (ui_action_handler.py:1244 "Retraction blocked: no characters
        pasted", then speech_processor.py:4026 "Retraction not
        performed: nothing_to_retract").
        """
        watcher = _RetractWatcher(harness).install()
        processor = harness.processor
        original_route = processor.maybe_route_to_editor
        refusing = {"now": False}
        flipped = {"done": False}

        async def route_then_refuse(text):
            # The flip lands inside command_engine's editor await, so
            # the gate has one answer before it and another after it.
            # It arms ONCE: the correction below consults the editor
            # too, and a second flip would refuse the correction's own
            # final, which is the delivery this test measures.
            routed = await original_route(text)
            if not flipped["done"]:
                flipped["done"] = True
                refusing["now"] = True
            return routed

        processor.maybe_route_to_editor = route_then_refuse
        processor._screen_read_refuses_dictation = lambda: refusing["now"]

        await _start_utterance(harness, 1)
        await harness.send_word("question", start_of_utterance=True)
        utterance_one = harness._utterance_counter
        await harness.send_utterance_end_marker(utterance_one)

        await _start_utterance(harness, 2)
        await harness.send_word("mark", start_of_utterance=True)
        utterance_two = harness._utterance_counter

        await asyncio.sleep(0.65)
        typed = "".join(harness.recording.text_deliveries)
        assert "?" not in typed, (
            "The gate was refusing by the time the rule asked, so the "
            f"rule must have dropped its text: {typed!r}"
        )

        # The read ends, and the next word is typed, for the reason
        # case C gives above: with nothing delivered, the retract
        # answers nothing_to_retract, the restore never runs, and the
        # test would pass for a reason that says nothing about the
        # record. Measured here: without this word the correction
        # produced no delivery at all and the case F guard fired.
        refusing["now"] = False
        await harness.send_word("cat", delay_before_ms=40)
        await asyncio.sleep(0.05)
        assert harness.recording.text_deliveries, (
            "Nothing was typed after the screen read ended, so the "
            "retract has nothing to remove and the restore is never "
            "reached."
        )

        await harness.send_retraction_marker(utterance_two, "this")
        await asyncio.sleep(0.15)

        screen = watcher.screen_after_retract()
        assert "question" not in screen.lower(), (
            "The rule was refused, so the earlier utterance's word "
            "never reached the screen and was never inside the "
            "retractable span. The restore typed it anyway. Screen: "
            f"{screen!r}"
        )
    @pytest.mark.asyncio
    async def test_g_a_flush_a_later_start_outran_is_not_restored(
        self, harness
    ):
        """Case G: a third start_utterance outruns utterance 2's word.

        wh-spaced-punctuation-names-unresolved.3.1.7, codex round 5.
        Case E's answer -- keep the record when the flagged word's
        number equals the record's -- reads a number the word has
        CARRIED since it was queued. WebSocketManager stamps it at the
        enqueue (integrations/websocket_manager.py:1318 and :1555), so
        a start_utterance sent afterwards, while the word still waits
        in word_queue, is invisible to that comparison
        (speech_processor.py:3475). Its reset took the flushed text out
        of the live span, and the correction below removes only what
        was typed after it, so the restore must put nothing back.

        Every message here is in the queue before the loop is allowed
        to take it, so the ordering is exact rather than raced.
        """
        _hold_the_safety_timeout(harness)
        watcher = _RetractWatcher(harness).install()

        await _start_utterance(harness, 1)
        await harness.send_word("open", start_of_utterance=True)
        utterance_one = harness._utterance_counter

        gate = _TicketedGet(harness).install()
        try:
            # The loop is inside its original get, so it takes this
            # marker normally and parks on the ticket afterwards.
            await harness.send_utterance_end_marker(utterance_one)
            await asyncio.sleep(0.65)
            assert not harness.recording.text_deliveries, (
                "The loop was supposed to park before the release "
                "sentinel, so nothing can have been typed yet: "
                f"{harness.recording.text_deliveries!r}"
            )

            # Case E's moment: the sentinel flushes INSIDE the span
            # this command opens, which is what earns the record.
            await _start_utterance(harness, 2)
            gate.release_one()
            await asyncio.sleep(0.25)
            typed = "".join(harness.recording.text_deliveries)
            assert "open" in typed.lower(), (
                f"The held prefix never flushed: {typed!r}"
            )

            # Utterance 2's own first word is queued, and stamped, here
            # -- and then a third start_utterance goes out before the
            # parked loop can take it.
            await harness.send_word(
                "the", start_of_utterance=True, allow_processing=False,
            )
            utterance_two = harness._utterance_counter
            await _start_utterance(harness, 3)
            gate.release_one()
            await asyncio.sleep(0.25)
        finally:
            gate.remove()

        typed = "".join(harness.recording.text_deliveries)
        assert "the" in typed.lower(), (
            f"Utterance 2's own word never reached the screen: {typed!r}"
        )

        await harness.send_retraction_marker(utterance_two, "this")
        await asyncio.sleep(0.15)

        screen = watcher.screen_after_retract()
        assert screen.lower().count("open") == 1, (
            "The third start_utterance reset the paste counter, so the "
            "flushed prefix had already left the retractable span and "
            "the correction did not remove it. Restoring it types it a "
            f"second time. Screen: {screen!r}"
        )

    @pytest.mark.asyncio
    async def test_h_a_mark_a_later_start_outran_is_not_respelled(
        self, harness
    ):
        """Case H: the same defect on the path that fires a mark.

        wh-spaced-punctuation-names-unresolved.3.1.7, codex round 5,
        second reproduction. Case D's promotion records the earlier
        word that helped spell a mark. Nothing here consumes a
        start-of-utterance word after that promotion, so
        _clear_record_delivered_before (its one caller is
        speech_processor.py:1173) never runs at all, and a repair made
        only inside that comparison cannot reach this sequence. The
        mark itself is still on screen -- the correction removed only
        the word typed after the later start -- so respelling it is a
        second insertion of a word the user already sees.
        """
        _hold_the_safety_timeout(harness)
        watcher = _RetractWatcher(harness).install()

        await _start_utterance(harness, 1)
        await harness.send_word("question", start_of_utterance=True)
        utterance_one = harness._utterance_counter
        await harness.send_utterance_end_marker(utterance_one)

        await _start_utterance(harness, 2)
        await harness.send_word("mark", start_of_utterance=True)
        utterance_two = harness._utterance_counter

        # Installed while the loop waits inside its original get: the
        # release sentinel is taken normally and fires the mark, and
        # the loop parks on the ticket straight afterwards.
        gate = _TicketedGet(harness).install()
        try:
            await asyncio.sleep(0.65)
            typed = "".join(harness.recording.text_deliveries)
            assert "?" in typed, (
                f"The held name never fired its mark: {typed!r}"
            )

            # A stable extension of utterance 2, queued before the next
            # utterance starts and delivered after it.
            await harness.send_word(
                "cat", utterance_id=utterance_two, allow_processing=False,
            )
            await _start_utterance(harness, 3)
            gate.release_one()
            await asyncio.sleep(0.25)
        finally:
            gate.remove()

        typed = "".join(harness.recording.text_deliveries)
        assert "cat" in typed.lower(), (
            f"The extension never reached the screen: {typed!r}"
        )

        await harness.send_retraction_marker(utterance_two, "hat")
        await asyncio.sleep(0.15)

        screen = watcher.screen_after_retract()
        assert "?" in screen, (
            "The retract could only reach the text typed after the "
            "third start_utterance, so the mark must still be on "
            f"screen for this test to measure anything. Screen: {screen!r}"
        )
        assert "question" not in screen.lower(), (
            "The mark the earlier word helped spell is still on screen, "
            "so the correction never removed that word's contribution "
            f"and the restore must not spell it again. Screen: {screen!r}"
        )

    @pytest.mark.asyncio
    async def test_i_a_start_during_the_retract_wait_still_restores(
        self, harness
    ):
        """Case I: the next start goes out while the retract is in flight.

        wh-spaced-punctuation-names-unresolved.3.1.8, codex round 6.
        Case G's answer -- drop the record when a later start took the
        words out of the span -- was read from the count AFTER the
        retract's response came back (speech_processor.py:4134, after
        the await at :4068). That is the wrong moment. Both messages
        travel on the one outbound queue, and a start enqueued AFTER
        the retract sits BEHIND it: the Input side processes the
        retract first, while its paste counter still holds the earlier
        words, and removes them. Reading the count afterwards sees the
        later start and suppresses a restoration the screen needs. A
        newer start cannot undo a deletion that already happened.

        This is case E's sequence exactly, with one message added: a
        third start_utterance sent while the retract waits for its
        response. Case E's answer must not change, because nothing the
        Input side did changed.
        """
        _hold_the_safety_timeout(harness)
        watcher = _RetractWatcher(harness).install()

        # The start goes out AFTER the retract is on the queue and
        # BEFORE its response arrives. Wrapping the watcher rather than
        # replacing it keeps the delivery-count measurement it takes.
        wrapped = harness.app.send_request
        started_during_the_wait = asyncio.Event()

        async def send_request(action, params=None):
            if action != "retract":
                return await wrapped(action, params)
            in_flight = asyncio.create_task(wrapped(action, params))
            # One loop iteration is enough to reach the enqueue:
            # nothing between send_request's entry (app.py:1165) and
            # its put_nowait (app.py:1358) awaits, so the task runs
            # straight through the enqueue and parks on the response.
            await asyncio.sleep(0)
            assert watcher.deliveries_at_retract, (
                "The retract had not been enqueued yet, so a start "
                "sent here would sit AHEAD of it and this test would "
                "measure case G's ordering instead of its own."
            )
            await _start_utterance(harness, 3)
            started_during_the_wait.set()
            return await in_flight

        harness.app.send_request = send_request

        await _start_utterance(harness, 1)
        await harness.send_word("open", start_of_utterance=True)
        utterance_one = harness._utterance_counter

        released = asyncio.Event()
        original_get = harness.word_queue.get

        async def gated_get():
            await released.wait()
            return await original_get()

        harness.word_queue.get = gated_get
        try:
            await harness.send_utterance_end_marker(utterance_one)
            await asyncio.sleep(0.65)
            assert not harness.recording.text_deliveries, (
                "The loop was supposed to be parked at word_queue.get, "
                "so nothing can have been typed yet: "
                f"{harness.recording.text_deliveries!r}"
            )
            await _start_utterance(harness, 2)
            await harness.send_word(
                "the", start_of_utterance=True, allow_processing=False,
            )
            utterance_two = harness._utterance_counter
        finally:
            released.set()
            del harness.word_queue.get

        await asyncio.sleep(0.25)
        typed = "".join(harness.recording.text_deliveries)
        assert "open" in typed.lower(), (
            f"The held prefix never flushed: {typed!r}"
        )

        await harness.send_retraction_marker(utterance_two, "this")
        await asyncio.sleep(0.15)

        assert started_during_the_wait.is_set(), (
            "The third start_utterance never went out, so this test "
            "measured case E and proves nothing about .3.1.8."
        )
        screen = watcher.screen_after_retract()
        assert screen.lower().count("open") == 1, (
            "The retract was already on the queue when the third "
            "start_utterance was sent, so the Input side removed the "
            "prefix before resetting its counter. The replay must put "
            f"it back exactly once. Screen: {screen!r}"
        )
