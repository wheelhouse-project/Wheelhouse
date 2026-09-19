"""A replacement pair split by a command that came first.

wh-okay-prefix-splits-replacement. Two live traces, one rule.

T-17877987532: "okay question mark". "okay" opens a command buffer because
patterns.toml has ``^okay Google.*$``. "question" disproves that candidate, and
router.py finalizes the buffer as dictation at word speed -- no timeout is
involved. "mark" then arrives alone and types as a word, so the user sees
"okay question" and "mark" instead of "okay?".

T-17877991226: "backspace question mark". "backspace" fires as a command by the
wh-cmd-prefix-not-split design, the remainder "question" is dictated, and "mark"
types through mid-utterance passthrough. Same split, a different release path.

The ee8eba35 hold catches neither, because it arms only inside the timeout
sentinel and only in the replacement buffering modes. These tests pin the two
new arming sites and the two limits the sign-off placed on them: no hold once
the word event already carried end_of_utterance, and no hold for a
hotword-active buffer.
"""
import asyncio
import pytest

from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness


@pytest.fixture
async def harness(pattern_catalog):
    """Create and start an E2E pipeline harness."""
    h = E2EPipelineHarness(catalog=pattern_catalog)
    await h.start()
    yield h
    await h.stop()


def _joined(harness) -> str:
    """Everything the pipeline typed, lower case, single-spaced."""
    return " ".join(
        "".join(harness.recording.text_deliveries).lower().split()
    )


class TestACommandPrefixNoLongerSplitsTheReplacement:
    """The two live traces recorded on wh-okay-prefix-splits-replacement."""

    @pytest.mark.asyncio
    async def test_okay_question_mark_types_a_question_mark(self, harness):
        """T-17877987532: 'okay question mark' -> 'okay' then '?'.

        Every word carries end_of_utterance=False and the separate empty end
        marker closes the utterance, which is the shape production sends.
        """
        await harness.send_word("okay", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("question", utterance_id=utterance_id)
        await harness.send_word("mark", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        delivered = harness.recording.text_deliveries
        assert len(delivered) == 2, f"Expected 2 deliveries, got {delivered}"
        assert delivered[0].strip().lower() == "okay", (
            "A command prefix in front must not split 'question mark'; got "
            f"{delivered}"
        )
        assert delivered[1] == "?", (
            "A command prefix in front must not split 'question mark'; got "
            f"{delivered}"
        )

    @pytest.mark.asyncio
    async def test_backspace_remainder_question_mark_converts(self, harness):
        """T-17877991226: the Backspace command fires, then the pair converts.

        The command itself is unaffected -- it still deletes -- and only the
        remainder the command released is judged again.
        """
        await harness.send_word("backspace", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("question", utterance_id=utterance_id)
        await harness.send_word("mark", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        pressed = [call[0] for call in harness.recording.keystrokes]
        assert ("backspace",) in pressed, (
            f"The Backspace command must still fire; pressed {pressed}"
        )
        assert _joined(harness) == "?", (
            "The remainder 'question' plus a late 'mark' must convert; got "
            f"{harness.recording.text_deliveries}"
        )


class TestTheTwoLimitsOnTheNewArmingSites:
    """Conditions under which the new sites must NOT hold."""

    @pytest.mark.asyncio
    async def test_a_word_carrying_end_of_utterance_is_not_held(self, harness):
        """Narrowing 2: no completing word can follow, so typing waits for nothing.

        The harness replacement timeout is 400 ms. Reading the delivery after
        150 ms proves the words went out immediately rather than waiting for
        the release deadline.
        """
        await harness.send_word("okay", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word(
            "question", utterance_id=utterance_id, end_of_utterance=True
        )
        await asyncio.sleep(0.15)

        assert _joined(harness) == "okay question", (
            "A word already marked end_of_utterance must type at once, not "
            f"wait out the release deadline; got "
            f"{harness.recording.text_deliveries}"
        )

    @pytest.mark.asyncio
    async def test_a_hotword_active_buffer_is_never_held(self, harness):
        """Amendment 1, asked of the gate directly.

        A scenario test cannot pin this. No end-to-end case reaches the gate
        with hotword_active True AND a holdable word: a hotword buffer holding
        "question" does not finalize on that word at all, it waits for the end
        marker. One path does reach site 3 with hotword_active True -- a
        word-speed EXECUTE through the matcher's punctuation-tail retry -- but
        its remainder is punctuation the gate refuses anyway. The wording here
        said "no end-to-end case reaches the gate with hotword_active True",
        which is literally false; reviewer_0 round 1 caught it (.1.2).

        The reason for the exclusion is in the gate's own docstring, and it
        belongs to the timeout site: that site holds the RAW buffer, while the
        router rebuilds a hotword buffer's dictation payload with the wake
        word in front, so holding the raw buffer would drop it.
        """
        gate = harness.processor._should_hold_replacement_prefix

        assert gate(
            ["question"], hotword_active=False, end_of_utterance=False
        ) is True, "Without the hotword, a viable prefix must be held"

        assert gate(
            ["question"], hotword_active=True, end_of_utterance=False
        ) is False, "A hotword-authorized buffer must never be held"

    @pytest.mark.asyncio
    async def test_the_gate_refuses_words_that_cannot_grow(self, harness):
        """A word that is already a replacement, or can never be one, types."""
        gate = harness.processor._should_hold_replacement_prefix

        assert gate(
            [], hotword_active=False, end_of_utterance=False
        ) is False, "No words means nothing to hold"

        assert gate(
            ["elephant"], hotword_active=False, end_of_utterance=False
        ) is False, "A word with no replacement patterns must not be held"

        assert gate(
            ["hello", "question"], hotword_active=False, end_of_utterance=False
        ) is True, "The LAST word decides, not the first"

        assert gate(
            ["period"], hotword_active=False, end_of_utterance=False
        ) is False, (
            "A word that is ALREADY a complete replacement has nothing to "
            "wait for and must not be held"
        )


class TestOnlyTheAfterRemainderCanHold:
    """The BEFORE remainder must never arm the hold.

    reviewer_0 round 1, finding .1.1. ``_execute_decision`` passes a word
    event only to the AFTER remainder; the BEFORE call at the same site
    passes nothing. Text before the matched pattern arrived earlier in
    spoken order, so a word arriving next does not belong to it. Without
    this test the exclusion was unpinned: passing the event to the BEFORE
    call survived the whole mutation gate.
    """

    @pytest.mark.asyncio
    async def test_a_before_remainder_prefix_is_typed_in_spoken_order(
        self, harness
    ):
        """'question period mark' keeps its order and does not convert.

        "question period" finalizes as the replacement "period" with the
        before remainder "question". If that remainder were held, the later
        "mark" would complete "question mark" AFTER the period had already
        typed, so the utterance would come out reversed as "." then "?".
        """
        await harness.send_word("question", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("period", utterance_id=utterance_id)
        await harness.send_word("mark", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        delivered = harness.recording.text_deliveries
        assert len(delivered) == 3, f"Expected 3 deliveries, got {delivered}"
        assert delivered[0].strip().lower() == "question", (
            "The before remainder must type as its own word, in spoken "
            f"order; got {delivered}"
        )
        assert delivered[1] == ".", (
            f"The matched replacement must type second; got {delivered}"
        )
        assert delivered[2].strip().lower() == "mark", (
            "A later 'mark' must type as a word, not complete a held "
            f"'question'; got {delivered}"
        )


class TestTheNewSitesAlwaysReleaseWithoutFurtherSpeech:
    """Held words must type even when no further event ever arrives.

    The same hazard codex filed against the timeout site as
    wh-trailing-question-mark-words.2.1, now reachable through two more
    sites. integrations/websocket_manager.py can deliver stable text and then
    never send the terminal FINAL message, taking the utterance-end marker
    with it. If the user stops speaking, the release deadline each site arms
    is the only producer left.
    """

    @pytest.mark.asyncio
    async def test_a_command_prefix_hold_types_without_a_marker(self, harness):
        """'okay question', then silence: both words still type."""
        await harness.send_word("okay", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("question", utterance_id=utterance_id)

        # No end marker and no further word ever arrive. Wait well past
        # the resolution deadline. wh-whole-utterance-command-matching.3
        # (timing-only edit): the deferral resolves this buffer on the
        # fixed command timeout (1000 ms) instead of the word-speed
        # hold's release deadline, so the old 1.0 s sleep sat exactly on
        # the fire time. The pinned behavior -- both words type without
        # any marker -- is unchanged.
        await asyncio.sleep(1.6)

        assert _joined(harness) == "okay question", (
            "Held words must be typed once even when the utterance-end "
            f"marker never arrives; got {harness.recording.text_deliveries}"
        )

    @pytest.mark.asyncio
    async def test_a_command_remainder_hold_types_without_a_marker(self, harness):
        """'backspace question', then silence: the remainder still types."""
        await harness.send_word("backspace", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("question", utterance_id=utterance_id)
        # wh-whole-utterance-command-matching.3 (timing-only edit): the
        # deferral resolves on the fixed command timeout; see the sleep
        # note in the test above.
        await asyncio.sleep(1.6)

        assert _joined(harness) == "question", (
            "A held command remainder must be typed once even when the "
            f"utterance-end marker never arrives; got "
            f"{harness.recording.text_deliveries}"
        )
