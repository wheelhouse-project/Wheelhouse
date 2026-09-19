"""A correction of the new utterance must not delete the held prefix.

wh-spaced-punctuation-names-unresolved.3.1.2 (codex round 2). Stage B
lets an exact punctuation-name opening stay held across a confirmed
utterance end, so the words of utterance 1 are typed only after
utterance 1's ``end_utterance`` has already gone out. The Input side
credits every insert to one running counter and resets it on
``start_utterance``, ``end_utterance``, and a successful retract
(ui/clipboard_operations.py:341-348, ui/ui_action_handler.py:1126,
1170, 1309-1317, 1405, 1417) -- there is no utterance concept in that
accounting at all. So those words land inside utterance 2's retractable
span, and an ordinary STT correction of utterance 2 deletes them and
replays only utterance 2's corrected final.

These tests are end to end on purpose. At processor level the delivered
text is the same in the broken and the fixed case; only the retract and
what follows it separate them, and that needs the real
``UIActionHandler.retract`` and the real paste counters.

Two e2e seams had to be opened before the retract path could run at all
(both recorded on the bead, both mirroring production rather than
inventing behaviour):

* ``AppAdapter.send_request`` returned ``True`` for every action, so
  ``_handle_retraction``'s ``response.get('status')`` raised
  AttributeError. input_proc.py:2032-2053 answers ``retract`` with the
  handler's own dict; the adapter now does the same.
* ``ui_action_handler``'s win32gui was the real one, so the retract
  focus gate compared the test's stand-in HWND with whatever window
  held focus on the machine and answered ``focus_drifted`` every time.
  The harness's ``action_handler_foreground`` flag supplies the same
  same-window stand-in the other modules already get. It is opt-in
  because it also changes the insert focus proof at
  ui_action_handler.py:1645, which every existing e2e test was written
  against.
"""
import asyncio
import pytest

from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness


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


@pytest.fixture
async def drifted_harness(pattern_catalog):
    """A harness whose retract refuses: the focus proof cannot pass.

    This is the default e2e wiring. It is named here so the fail-safe
    test says what it is exercising rather than relying on a default.
    """
    h = E2EPipelineHarness(catalog=pattern_catalog)
    await h.start()
    yield h
    await h.stop()


async def _hold_then_correct(harness, first_word, second_word, corrected):
    """Speak ``first_word``, close its utterance, then open a new one.

    Returns the deliveries made after the retract IPC went out -- the
    text left on screen once the retract has removed the whole credited
    span. The cut is taken AT the retract rather than at the retraction
    marker on purpose: the marker itself flushes the held words (the
    empty-word guard in ``_resolve_pending_replacement_prefix``), and
    that delivery is inside the span the retract then removes.
    """
    seen_at_retract = []
    original_send_request = harness.app.send_request

    async def send_request(action, params=None):
        if action == "retract":
            seen_at_retract.append(len(harness.recording.text_deliveries))
        return await original_send_request(action, params)

    harness.app.send_request = send_request

    await harness.send_word(first_word, start_of_utterance=True)
    utterance_one = harness._utterance_counter
    # The remote shape: a real word never carries end_of_utterance, and
    # a separate empty marker closes the utterance.
    await harness.send_utterance_end_marker(utterance_one)

    # Utterance 2 opens while the release window is still open.
    await harness.send_word(second_word, start_of_utterance=True)
    utterance_two = harness._utterance_counter
    await asyncio.sleep(0.05)

    await harness.send_retraction_marker(utterance_two, corrected)
    await asyncio.sleep(0.15)
    assert seen_at_retract, (
        "No retract IPC was sent, so there is nothing to measure. "
        "_handle_retraction returned before the request."
    )
    return harness.recording.text_deliveries[seen_at_retract[0]:]


def _joined(deliveries):
    return "".join(deliveries).strip().lower()


class TestACorrectionKeepsTheEarlierUtterancesWords:
    """The three held states the finding enumerates, one test each."""

    @pytest.mark.asyncio
    async def test_a_completed_name_waiting_for_its_end_keeps_its_first_word(
        self, harness
    ):
        """"question" then "mark", corrected to "Marcus".

        The hold is complete-and-waiting: "question mark" spells a name
        but its second word did not end its utterance, so the pair sits
        under the one release deadline. The retraction marker flushes
        it, and the retract then owns all of it.
        """
        after = await _hold_then_correct(
            harness, "question", "mark", "Marcus",
        )
        assert harness.recording.backspace_sends, (
            "The retract never ran, so this test proves nothing about "
            "its scope. Check the adapter's retract route and the "
            "action_handler_foreground flag."
        )
        assert _joined(after) == "question marcus", (
            "The correction of utterance 2 deleted utterance 1's "
            f"'question'. Replayed: {after}"
        )

    @pytest.mark.asyncio
    async def test_an_incomplete_opening_keeps_its_first_word(self, harness):
        """"open" then "single", corrected to "simple".

        The hold is incomplete: "open single" is still only an opening
        of "open single quote", so the words stay held and grow.
        """
        after = await _hold_then_correct(harness, "open", "single", "simple")
        assert harness.recording.backspace_sends, "The retract never ran"
        assert _joined(after) == "open simple", (
            f"utterance 1's 'open' was lost. Replayed: {after}"
        )

    @pytest.mark.asyncio
    async def test_a_mismatched_continuation_keeps_the_flushed_word(
        self, harness
    ):
        """"open" then "the", corrected to "this".

        The new-start guard flushes the held word before routing the
        mismatch, so the two words reach the screen as separate
        inserts. Both are still inside utterance 2's span, which is why
        fixing only the empty-marker guard would not be enough.
        """
        after = await _hold_then_correct(harness, "open", "the", "this")
        assert harness.recording.backspace_sends, "The retract never ran"
        assert _joined(after) == "open this", (
            f"utterance 1's 'open' was lost. Replayed: {after}"
        )


class TestTheOrdinaryCasesAreUnchanged:
    """Controls. A restore that fires when it should not is data loss too."""

    @pytest.mark.asyncio
    async def test_a_word_typed_before_its_end_utterance_is_not_replayed(
        self, harness
    ):
        """"hello" then "the", corrected to "this": no hold, no restore.

        "hello" opens no name, so it is dictated inside utterance 1 and
        its ``end_utterance`` resets the counter before utterance 2
        starts. The retract can only reach " the". Replaying "hello"
        here would type it a second time.
        """
        after = await _hold_then_correct(harness, "hello", "the", "this")
        assert harness.recording.backspace_sends == [4], (
            "Only ' the' was inside utterance 2's span; got "
            f"{harness.recording.backspace_sends}"
        )
        assert _joined(after) == "this", (
            f"'hello' was replayed on top of itself. Replayed: {after}"
        )

    @pytest.mark.asyncio
    async def test_the_record_does_not_outlive_the_span_it_describes(
        self, harness
    ):
        """A correction after the end marker restores nothing.

        "open" is held across utterance 1's end, and utterance 2's
        first word spells no punctuation name, so the hold is dictated
        inside utterance 2's span and goes on the record. But utterance
        2 then ends, and that ``end_utterance`` resets the Input paste
        counter -- those characters can never be retracted again. A
        correction after that point must replay its own text and
        nothing else; restoring here would type words that are already
        on screen.

        NO WORD AFTER THE END MARKER CARRIES ``start_of_utterance``,
        AND THAT IS THE WHOLE POINT OF THIS TEST. The processor holds
        the same invariant in two places: ``_send_end_utterance`` empties
        the record (speech_processor.py:3284), and a flagged word empties
        it again on the way into the loop
        (speech_processor.py:1154). An earlier version of this test
        opened a third utterance with ``start_of_utterance=True``, so
        the second clear covered the first and the mutation
        ``the-record-outlives-the-span-it-describes`` SURVIVED the full
        sweep at 8f24d514. The mutation-gate skill names the failure --
        a later defence-in-depth fix masks an older mutation's catchers
        -- and the repair is to feed the catcher an input only the
        mutated layer handles. Here that input is a word with no flag,
        so line 3284 is the only clear that can run.

        The unflagged word is also what gives the retract characters to
        remove. Without it the credited span is empty, the retract
        answers ``not_retracted``, and the restore is never reached at
        all -- the test would then pass for a reason that says nothing
        about the record.
        """
        await harness.send_word("open", start_of_utterance=True)
        first = harness._utterance_counter
        await harness.send_utterance_end_marker(first)

        # "open the" spells no punctuation name, so utterance 2's first
        # word flushes the hold and "open" is DICTATED inside utterance
        # 2's span. Only a delivery puts words on the record
        # (_record_delivered_earlier_utterance_words), so a hold that
        # resolves into a mark instead would leave the record empty and
        # this test would prove nothing.
        await harness.send_word("the", start_of_utterance=True)
        second = harness._utterance_counter
        await asyncio.sleep(0.05)
        assert "open" in _joined(harness.recording.text_deliveries), (
            "The held word was never dictated, so it is not on the "
            "record and there is nothing for the correction to "
            f"restore: {harness.recording.text_deliveries}"
        )

        await harness.send_utterance_end_marker(second)
        await asyncio.sleep(0.05)

        seen_at_retract = []
        original_send_request = harness.app.send_request

        async def send_request(action, params=None):
            if action == "retract":
                seen_at_retract.append(
                    len(harness.recording.text_deliveries)
                )
            return await original_send_request(action, params)

        harness.app.send_request = send_request

        await harness.send_word("cat", start_of_utterance=False)
        await asyncio.sleep(0.05)

        await harness.send_retraction_marker(second, "hallo")
        await asyncio.sleep(0.15)

        assert seen_at_retract, "No retract IPC was sent"
        after = harness.recording.text_deliveries[seen_at_retract[0]:]
        assert _joined(after) == "hallo", (
            "A record from before the end marker was replayed into "
            f"this correction: {after}"
        )

    @pytest.mark.asyncio
    async def test_a_refused_retraction_still_replays_nothing(
        self, drifted_harness
    ):
        """A retract that fails must not authorize a replay.

        The existing fail-safe: every ``not_retracted`` reason except
        the command-buffered one is terminal, because text may still be
        on screen and replaying would double it. The restore must ride
        on the same rule rather than around it.
        """
        after = await _hold_then_correct(
            drifted_harness, "question", "mark", "Marcus",
        )
        assert drifted_harness.recording.backspace_sends == [], (
            "This harness's retract is supposed to refuse; got "
            f"{drifted_harness.recording.backspace_sends}"
        )
        assert after == [], (
            "A refused retract replayed text on top of what is still on "
            f"screen: {after}"
        )
