"""'submit' at the end of an utterance (wh-remove-trailing-submit).

These tests first pinned the trailing-position submit at whole-utterance
finalization (wh-whole-utterance-command-matching.3). The trailing
position was removed: 'submit' now presses Enter only as the whole
utterance, and 'submit' after other words is typed as text. Each shape
below keeps its old input and now pins the new outcome.
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


class TestSubmitAfterOtherWordsIsText:
    """Every shape that used to fire the trailing submit now types it."""

    @pytest.mark.asyncio
    async def test_submit_after_an_impossible_buffer_is_typed(self, harness):
        """'backspace hello submit': backspace fires, 'hello submit'
        types, and no Enter fires."""
        await harness.send_word("backspace", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("hello", utterance_id=utterance_id)
        await harness.send_word("submit", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        assert ("backspace",) in _pressed(harness)
        assert ("enter",) not in _pressed(harness)
        typed = _typed(harness)
        assert "hello" in typed
        assert "submit" in typed

    @pytest.mark.asyncio
    async def test_hotword_dictation_keeps_typing_the_trailing_word(
        self, harness
    ):
        """'x-ray foo submit' (hotword active): the hotword dictation
        fallback types the words -- 'submit' is text and NO Enter
        fires."""
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
    async def test_comma_submit_types_the_word(self, harness):
        """'comma submit': the fresh 'comma' executes as the
        spoken-punctuation replacement at word speed. 'submit' is then
        not the whole utterance, so it is typed and no Enter fires."""
        await harness.send_word("comma", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("submit", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        assert ("enter",) not in _pressed(harness)
        typed = _typed(harness)
        assert "," in typed
        assert "submit" in typed

    @pytest.mark.asyncio
    async def test_backspace_submit_types_the_word(self, harness):
        """'backspace submit': backspace fires and 'submit' is typed.
        The R1 split (David, item 20, 2026-08-29) that made this shape
        press Enter was removed with the trailing position."""
        await harness.send_word("backspace", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("submit", utterance_id=utterance_id)
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        assert ("backspace",) in _pressed(harness)
        assert ("enter",) not in _pressed(harness)
        assert "submit" in _typed(harness)


class TestSubmitAsTheWholeUtterance:

    @pytest.mark.asyncio
    async def test_submit_alone_presses_enter_without_leaking(self, harness):
        """'submit' as the entire utterance presses Enter in its own
        utterance, types nothing, and leaks nothing into the next
        utterance."""
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
    the end marker behind it. That path must give the same outcome as
    the marker path: 'submit' after other words is typed."""

    @pytest.mark.asyncio
    async def test_flagged_backspace_submit_types_the_word(self, harness):
        await harness.send_word("backspace", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word(
            "submit", utterance_id=utterance_id, end_of_utterance=True
        )
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        assert ("backspace",) in _pressed(harness)
        assert ("enter",) not in _pressed(harness)
        assert "submit" in _typed(harness)

    @pytest.mark.asyncio
    async def test_flagged_backspace_hello_submit_types_the_word(
        self, harness
    ):
        await harness.send_word("backspace", start_of_utterance=True)
        utterance_id = harness._utterance_counter
        await harness.send_word("hello", utterance_id=utterance_id)
        await harness.send_word(
            "submit", utterance_id=utterance_id, end_of_utterance=True
        )
        await harness.send_utterance_end_marker(utterance_id)
        await asyncio.sleep(0.3)

        assert ("backspace",) in _pressed(harness)
        assert ("enter",) not in _pressed(harness)
        typed = _typed(harness)
        assert "hello" in typed
        assert "submit" in typed


class TestStalePendingEndDoesNotSplit:

    @pytest.mark.asyncio
    async def test_timeout_finalization_ignores_the_stale_slot(self, harness):
        """A stale _pending_utterance_end, injected directly, must not
        change a TIMEOUT finalization: the whole payload dictates and no
        Enter fires (wh-whole-utterance-command-matching.3.1.3 instance
        4, wh-pending-utterance-end-stale-slot)."""
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
