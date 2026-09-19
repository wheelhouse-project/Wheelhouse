"""End-of-utterance replacement completion (wh-trailing-question-mark-words).

Production never marks end-of-utterance on the last real word. Every word
event carries ``end_of_utterance=False`` and a separate empty
``is_utterance_end_marker`` event closes the utterance
(integrations/websocket_manager.py:484-501). The two speech harnesses do the
opposite: ``send_utterance`` sets ``end_of_utterance=True`` on the last word,
so every test written on it exercises the buffering router's end-marker
branch, which production cannot reach.

These tests use the production shape on purpose: real words with
``end_of_utterance=False``, then ``send_utterance_end_marker``.

The failure they pin: the STT server always holds back the last confirmed
word (services/stt_providers/.../audio_processor.py:362-366), and when
sherpa's own endpoint wins the race against the 300 ms silence release, the
held word first appears in the FINAL message. The gap in front of it then
exceeds REPLACEMENT_TIMEOUT_MS, the replacement buffer finalizes as
dictation, and a spoken "question mark" at the end of a sentence types as
two words.
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


class TestTrailingReplacementAcrossTheTimeout:
    """A replacement whose completing word arrives after the timer expires."""

    @pytest.mark.asyncio
    async def test_late_mark_still_types_a_question_mark(self, harness):
        """'question' ... (over the timeout) ... 'mark' -> '?', not two words.

        The harness replacement timeout is 400 ms; the 500 ms wait puts the
        completing word past it, which is exactly what the sherpa endpoint
        does to the held-back last word in production.
        """
        await harness.send_word("question", start_of_utterance=True)
        utterance_id = harness._utterance_counter

        # The replacement timer expires with only "question" buffered.
        await harness.wait_for_timeout(500)

        # "mark" arrives in the FINAL message: a real word, never marked
        # end-of-utterance, followed by the separate empty end marker.
        await harness.send_word("mark", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.1)

        assert harness.recording.text_deliveries == ["?"], (
            "A trailing 'question mark' must type '?' even when the "
            "completing word arrives after the timeout; got "
            f"{harness.recording.text_deliveries}"
        )

    @pytest.mark.asyncio
    async def test_late_stop_still_types_a_period(self, harness):
        """The same race on another two-word replacement: 'full' + 'stop'."""
        await harness.send_word("full", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.wait_for_timeout(500)
        await harness.send_word("stop", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.1)

        assert harness.recording.text_deliveries == ["."], (
            "A trailing 'full stop' must type '.' even when the completing "
            "word arrives after the timeout; got "
            f"{harness.recording.text_deliveries}"
        )


class TestProductionMarkerShapeStillWorks:
    """Guards for behaviour that must NOT change (acceptance criterion 3).

    These use the production marker shape too, which is what closes the
    harness gap: before this module, no send_utterance-style test drove the
    pipeline the way the websocket manager does.
    """

    @pytest.mark.asyncio
    async def test_question_mark_arriving_together_is_unchanged(self, harness):
        """No gap: 'test question mark' -> first word + '?'."""
        await harness.send_word("test", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("question", utterance_id=utterance_id)
        await harness.send_word("mark", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        delivered = harness.recording.text_deliveries
        assert len(delivered) == 2, f"Expected 2 deliveries, got {delivered}"
        assert delivered[0].strip().lower() == "test", f"Got {delivered}"
        assert delivered[1] == "?", f"Got {delivered}"

    @pytest.mark.asyncio
    async def test_lone_question_after_the_timeout_is_still_dictated(self, harness):
        """'question' with nothing completing it stays the spoken word."""
        await harness.send_word("question", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.wait_for_timeout(500)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.1)

        delivered = harness.recording.text_deliveries
        assert len(delivered) == 1, f"Expected 1 delivery, got {delivered}"
        assert delivered[0].strip().lower() == "question", f"Got {delivered}"

    @pytest.mark.asyncio
    async def test_unrelated_late_word_is_dictated_not_swallowed(self, harness):
        """'question' then a late 'everything' types both words."""
        await harness.send_word("question", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.wait_for_timeout(500)
        await harness.send_word("everything", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.1)

        delivered = harness.recording.text_deliveries
        joined = "".join(delivered).strip().lower()
        assert joined == "question everything", f"Got {delivered}"

    @pytest.mark.asyncio
    async def test_held_words_do_not_leak_into_the_next_utterance(self, harness):
        """A missing end marker must not carry held words into utterance 2.

        integrations/websocket_manager.py drops a FINAL message whose
        text disagrees with the stable text, and the utterance-end
        marker can be lost with it. The held words belong to the
        utterance that spoke them, so the first word of the next
        utterance flushes them as text instead of completing a
        replacement across the boundary.
        """
        await harness.send_word("question", start_of_utterance=True)

        # The replacement timer expires, and no end marker ever arrives
        # for this utterance.
        await harness.wait_for_timeout(500)

        await harness.send_word("hello", start_of_utterance=True)
        second_utterance_id = harness._utterance_counter
        await harness.send_utterance_end_marker(second_utterance_id)
        await asyncio.sleep(0.1)

        delivered = harness.recording.text_deliveries
        assert len(delivered) == 2, f"Expected 2 deliveries, got {delivered}"
        assert delivered[0].strip().lower() == "question", f"Got {delivered}"
        assert delivered[1].strip().lower() == "hello", f"Got {delivered}"

    @pytest.mark.asyncio
    async def test_a_new_utterance_never_completes_the_old_replacement(self, harness):
        """The completing word must not reach back across an utterance.

        Same lost-end-marker shape as the test above, except the next
        utterance opens with the word that WOULD have completed the
        replacement. Joining them would type one '?' for two separate
        utterances and silently swallow both spoken words.
        """
        await harness.send_word("question", start_of_utterance=True)
        await harness.wait_for_timeout(500)

        await harness.send_word("mark", start_of_utterance=True)
        second_utterance_id = harness._utterance_counter
        await harness.send_utterance_end_marker(second_utterance_id)
        await asyncio.sleep(0.1)

        delivered = harness.recording.text_deliveries
        assert len(delivered) == 2, f"Expected 2 deliveries, got {delivered}"
        assert delivered[0].strip().lower() == "question", f"Got {delivered}"
        assert delivered[1].strip().lower() == "mark", f"Got {delivered}"


class TestTheHoldAlwaysReleasesWithoutFurtherSpeech:
    """Held words must type even when no further event ever arrives.

    Filed by codex as wh-trailing-question-mark-words.2.1. Holding the
    buffer instead of typing it introduced a way to lose words that the
    pre-fix code did not have: the timer typed the buffer immediately,
    so a dropped FINAL message cost nothing.

    integrations/websocket_manager.py can receive stable text and then
    never deliver the terminal FINAL message, taking the utterance-end
    marker with it. Its idle watchdog, ``_fire_idle_watchdog``, only
    writes the GUI activity state; it does not send an utterance-end
    marker. So if the user then stops speaking, no producer is left to
    release the held words and they are never typed.
    """

    @pytest.mark.asyncio
    async def test_held_words_type_when_no_marker_and_no_word_follow(
        self, harness
    ):
        """'question', the timer, then silence: the word still types once."""
        await harness.send_word("question", start_of_utterance=True)

        # The replacement timer expires and the fix holds "question".
        await harness.wait_for_timeout(500)

        # No end marker and no further word ever arrive. Wait well past
        # the release deadline.
        await asyncio.sleep(1.0)

        delivered = harness.recording.text_deliveries
        assert len(delivered) == 1, (
            "The held word must be typed exactly once even when the "
            f"utterance-end marker never arrives; got {delivered}"
        )
        assert delivered[0].strip().lower() == "question", f"Got {delivered}"


class TestTheSecondPassNeverExecutesACommand:
    """The held words plus one more word must never run a command.

    The second pass calls ``router.decide_timeout`` with
    ``ProcessingMode.MID_REPLACEMENT_BUFFERING``, which is what passes
    ``allow_commands=False`` into ``router._resolve_finalization``. That
    mode argument is load-bearing, not decoration: ``decide_timeout``
    takes ``mode=None`` by default, and ``None`` means commands ARE
    allowed. Dropping the argument, or copying the sentinel branch's
    ``mode=self.mode``, silently turns the retry into a command path.

    Three shipped patterns collide this way. Each is a command whose
    first word also opens a replacement, and none of them needs the
    hotword:

        "back" + "space"   -> ^back ?space\\s*(\\d+)?$      presses backspace
        "right" + "click"  -> ^((right|double)[\\s-]+click[.!?]?)$
        "number" + "one"   -> ^((number|numbers)\\s+(one|...))$

    Measured against the shipped catalog: ``decide_timeout(["back",
    "space"], mode=MID_REPLACEMENT_BUFFERING)`` returns DICTATE, and the
    same call with the mode widened or omitted returns EXECUTE
    "Finalized as command". So a user dictating "I said back space" with
    the STT holdback race in play would lose a character to a backspace
    the widened path ran.

    Filed by deepseek as wh-trailing-question-mark-words.1.1 after the
    mutation gate's own docstring named this guard as uncovered.
    """

    @pytest.mark.asyncio
    async def test_a_late_command_collision_pair_types_instead_of_executing(
        self, harness
    ):
        """'I said back' ... (over the timeout) ... 'space' types four words.

        "back" arrives mid-utterance, so the router buffers it in
        MID_REPLACEMENT_BUFFERING (it opens the ``\\bback ?tick\\b``
        replacement). The timer then finalizes that buffer to dictation
        and the fix holds it. "space" arrives after the timeout and
        drives the second pass, which must decline the backspace command
        and type both words.
        """
        await harness.send_word("I", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("said", utterance_id=utterance_id)
        await harness.send_word("back", utterance_id=utterance_id)

        await harness.wait_for_timeout(500)

        await harness.send_word("space", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.1)

        delivered = harness.recording.text_deliveries
        joined = "".join(delivered).strip().lower()
        assert joined == "i said back space", f"Got {delivered}"
        assert harness.recording.backspace_sends == [], (
            "The second pass ran the backspace command; the held words "
            "must never become a command. Got "
            f"{harness.recording.backspace_sends}"
        )
        assert harness.recording.keystrokes == [], (
            "The second pass pressed a key; the held words must type as "
            f"text only. Got {harness.recording.keystrokes}"
        )
