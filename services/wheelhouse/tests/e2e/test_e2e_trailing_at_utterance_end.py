"""Trailing commands at whole-utterance finalization (Stage 3).

Boss ruling on wh-whole-utterance-command-matching.3 (2026-08-29,
overridable by David's item-19 answer): the Stage-3 deferral folds in
two SEPARABLE trailing-command pieces -- (A) at end-of-utterance
finalization, and only when the hotword is not active, a dictation
payload whose last word is a trailing command splits: the head
dictates, the trailing word is held; (B) the end-marker branch's
post-finalization consume covers the trailing kind as well as the
bare-number kind, so the held word fires in ITS OWN utterance instead
of leaking into the next one.

The parity tests pin TODAY's outcome for every reachable trailing
shape first, and the same tests must pass unchanged under the Stage-3
deferral -- a product-visible difference is David's call, not ours
(boss condition, recorded on the bead).
"""
import asyncio

import pytest

from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness


@pytest.fixture
async def harness(pattern_catalog):
    h = E2EPipelineHarness(catalog=pattern_catalog)
    await h.start()
    yield h
    await h.stop()


def _pressed(harness):
    return [call[0] for call in harness.recording.keystrokes]


def _typed(harness):
    return " ".join(harness.recording.text_deliveries).lower()


class TestTrailingParityAcrossTheDeferral:
    """Boss condition: pin today's outcome per reachable shape; the same
    outcome must hold under the Stage-3 deferral."""

    @pytest.mark.asyncio
    async def test_trailing_word_after_an_impossible_buffer_still_fires(
        self, harness
    ):
        """'backspace hello submit': backspace fires, 'hello' types,
        Enter fires, 'submit' is never typed (probed 2026-08-29).
        Today 'hello' causes the mid-utterance split, so 'submit'
        arrives afterward as its own fresh word, arms the trailing
        hold, and the end marker fires it. Under the deferral the
        end-of-utterance split (piece A, David's item-20 R1 ruling)
        plus the consume guard (piece B, the boss's 19(a)
        regression-guard ruling) deliver the SAME outcome -- without
        them, 'submit' rides the multi-word remainder through the
        replacement-only _process_remainder path as literal text and
        the Enter action is lost."""
        await harness.send_word("backspace", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("hello", utterance_id=utterance_id)
        await harness.send_word("submit", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        assert ("backspace",) in _pressed(harness)
        assert ("enter",) in _pressed(harness)
        typed = _typed(harness)
        assert "hello" in typed
        assert "submit" not in typed

    @pytest.mark.asyncio
    async def test_hotword_dictation_keeps_typing_the_trailing_word(
        self, harness
    ):
        """'x-ray foo submit' (hotword active): the hotword dictation
        fallback types the words -- 'submit' stays literal text and NO
        Enter fires. This is the reachable-today multi-word DICTATE
        finalization at the end marker, and it is exactly why piece A
        gates on not-hotword_active: the split must not change this
        outcome."""
        await harness.send_word("x-ray", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("foo", utterance_id=utterance_id)
        await harness.send_word("submit", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        assert ("enter",) not in _pressed(harness)
        typed = _typed(harness)
        assert "foo" in typed
        assert "submit" in typed

    @pytest.mark.asyncio
    async def test_comma_submit_pins_the_current_outcome(self, harness):
        """'comma submit' -- the trailing doc's own example family.
        The whole_utterance_only doc reads as if the alias stays text
        inside a longer utterance; probed 2026-08-29, the pipeline
        does something simpler: a fresh 'comma' executes as the
        spoken-punctuation REPLACEMENT at word speed (the IDLE
        immediate path, which the deferral deliberately keeps), then
        'submit' arrives as its own fresh word, arms the single-word
        trailing hold, and the end marker fires Enter. Observed
        deliveries: ',' typed, Enter pressed -- identical before and
        after the deferral, because neither word ever enters a
        deferred buffer."""
        await harness.send_word("comma", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("submit", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        assert ("enter",) in _pressed(harness)
        typed = _typed(harness)
        assert "," in typed
        assert "submit" not in typed

    @pytest.mark.asyncio
    async def test_backspace_submit_fires_enter_under_r1(self, harness):
        """'backspace submit' -- the shape where the trailing word used
        to ride the mid-utterance split's remainder as literal text
        ('Submit' typed, no Enter). The deferral erased the
        arrived-before/after-the-split distinction, so exact parity
        for both backspace shapes at once was impossible; David ruled
        R1 (item 20, QUESTIONS-2026-08-29.md, 2026-08-29): a
        dictation-bound payload ending in a trailing command word
        splits at end-of-utterance finalization -- the head dictates,
        the trailing word fires. This is the ONE accepted
        product-visible change: backspace fires, Enter fires, and
        'submit' is no longer typed, matching patterns.toml's
        documented trailing intent and the 'backspace hello submit'
        outcome."""
        await harness.send_word("backspace", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("submit", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        assert ("backspace",) in _pressed(harness)
        assert ("enter",) in _pressed(harness)
        assert "submit" not in _typed(harness)


class TestSingleWordTrailingCorner:

    @pytest.mark.asyncio
    async def test_submit_alone_presses_enter_without_leaking(self, harness):
        """'submit' as the entire utterance presses Enter in its own
        utterance, types nothing, and leaks nothing into the next
        utterance. Probed 2026-08-29: this ALREADY works today --
        the end-marker branch consumes the mid-utterance-armed hold
        via _consume_held_tail_at_utterance_end (speech_processor.py
        ~1056) before finalization, so the leak described in
        observation (a) / QUESTIONS item 19(a) does not reproduce.
        This is a parity pin, not a behavior change: under the
        deferral the buffer path that produces this outcome moves
        into the end-marker finalization, where only the ruled
        consume guard (boss ruling on
        wh-whole-utterance-command-matching.3, 2026-08-29) keeps the
        hold from surviving into the next utterance."""
        await harness.send_word("submit", start_of_utterance=True)
        await harness.send_utterance_end_marker(harness._utterance_counter)
        await asyncio.sleep(0.3)

        assert ("enter",) in _pressed(harness)
        assert "submit" not in _typed(harness)

        # Nothing may flush into the next utterance.
        await harness.send_word("hello", start_of_utterance=True)
        await harness.send_utterance_end_marker(harness._utterance_counter)
        await asyncio.sleep(0.3)
        typed = _typed(harness)
        assert "hello" in typed
        assert "submit" not in typed
        assert _pressed(harness).count(("enter",)) == 1


class TestFlaggedLastWordParity:
    """The in-process STT bridge (main.py _handle_stt_transcript) sets
    end_of_utterance=True on the utterance's last real word AND queues
    the end marker behind it. The flagged word finalizes a deferred
    buffer through router step 1 at word speed, so that finalization
    must apply the same R1 trailing split the marker path applies --
    otherwise the in-process provider misses the ruled R1 outcome
    while remote providers deliver it
    (wh-whole-utterance-command-matching.3.1.1)."""

    @pytest.mark.asyncio
    async def test_flagged_backspace_submit_fires_enter(self, harness):
        await harness.send_word("backspace", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word(
            "submit", utterance_id=utterance_id, end_of_utterance=True
        )
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        assert ("backspace",) in _pressed(harness)
        assert ("enter",) in _pressed(harness)
        assert "submit" not in _typed(harness)

    @pytest.mark.asyncio
    async def test_flagged_backspace_hello_submit_fires_enter(self, harness):
        await harness.send_word("backspace", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("hello", utterance_id=utterance_id)
        await harness.send_word(
            "submit", utterance_id=utterance_id, end_of_utterance=True
        )
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        assert ("backspace",) in _pressed(harness)
        assert ("enter",) in _pressed(harness)
        typed = _typed(harness)
        assert "hello" in typed
        assert "submit" not in typed


class TestStalePendingEndDoesNotSplit:

    @pytest.mark.asyncio
    async def test_timeout_finalization_ignores_the_stale_slot(self, harness):
        """The R1 split fires only when the utterance end is confirmed
        (the end marker or the flagged last word). A stale
        _pending_utterance_end must not make a TIMEOUT finalization
        split its trailing word: the timeout means the end was never
        confirmed, so the whole payload dictates
        (wh-whole-utterance-command-matching.3.1.3 instance 4 -- the
        split gate was a new reader of the stale slot).

        An earlier utterance's raise used to leave that slot set; the
        per-word handler now clears it
        (wh-pending-utterance-end-stale-slot). This test injects the
        stale id directly, so it still pins the split gate against any
        future producer."""
        harness.processor._pending_utterance_end = 999
        await harness.send_word("backspace", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("hello", utterance_id=utterance_id)
        await harness.send_word("submit", utterance_id=utterance_id)
        await harness.wait_for_timeout(1100)

        assert ("enter",) not in _pressed(harness)
        typed = _typed(harness)
        assert "hello" in typed
        assert "submit" in typed
