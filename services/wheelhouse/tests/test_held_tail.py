"""The single held tail (wh-whole-utterance-command-matching, Stage 2).

Stage 2 replaces the three hold-slot attributes with ONE storage slot,
``SpeechProcessor._held_tail``: a kind-tagged record of the words held
back from dictation at the end of an utterance. The three old names
(``_pending_replacement_prefix``, ``_pending_trailing_word``,
``_pending_bare_number_words``) become properties over that slot, so
every existing white-box test and call site keeps working -- but the
storage, the top-of-loop advance, and the flush obligation for any NEW
event type are one thing, not three. Forgetting to flush one slot of
three on a new event type is the recurring bug class this design
removes (Option C, part 2 of the design proposal on the parent bead).

Behaviour is unchanged: the three absorbed suites
(test_speech_processor_trailing_command.py,
test_speech_processor_bare_number.py, and the two e2e
replacement-prefix files) are the proof of preservation. THESE tests
pin only the new structure:

- one storage slot, kind-tagged, None when nothing is held;
- each old attribute name maps onto the slot, read and write;
- a None-assignment clears only a SAME-KIND tail (the old attributes
  were independent: clearing one never touched the others);
- reading an old name while a different kind is held returns None;
- arming a kind while a different kind is held evicts it loudly (no
  live sequence does this; the log line is the tripwire);
- one unified flush dispatches per kind and preserves each kind's IPC
  shape (prefix: one joined dictation; trailing: the word; bare
  number: one dictation per word, in spoken order);
- stop() clears the single slot.
"""
import sys
from pathlib import Path

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))
sys.path.insert(0, str(test_file.parent))

import asyncio
import logging

import pytest
from unittest.mock import AsyncMock, MagicMock, call

from speech.word_event import WordEvent

from test_speech_pipeline import SpeechPipelineHarness
from test_utterance_word_list import make_retraction_processor


# ============================================================================
# TESTS -- storage and property mapping
# ============================================================================

class TestHeldTailStorage:

    def test_initial_tail_is_none(self):
        proc = make_retraction_processor()
        assert proc._held_tail is None

    def test_prefix_property_maps_to_tail(self):
        proc = make_retraction_processor()
        proc._pending_replacement_prefix = ["question"]
        assert proc._held_tail is not None
        assert proc._held_tail.kind.name == "REPLACEMENT_PREFIX"
        assert proc._held_tail.words == ["question"]
        assert proc._pending_replacement_prefix == ["question"]

    def test_trailing_property_maps_to_tail(self):
        proc = make_retraction_processor()
        proc._pending_trailing_word = "submit"
        assert proc._held_tail is not None
        assert proc._held_tail.kind.name == "TRAILING_COMMAND"
        assert proc._held_tail.words == ["submit"]
        assert proc._pending_trailing_word == "submit"

    def test_bare_number_property_maps_to_tail(self):
        proc = make_retraction_processor()
        proc._pending_bare_number_words = ["twenty"]
        assert proc._held_tail is not None
        assert proc._held_tail.kind.name == "BARE_NUMBER"
        assert proc._pending_bare_number_words == ["twenty"]
        # The extend path mutates the held list in place
        # (_extend_bare_number_hold does held.append(text)); the
        # property must hand back the live list, not a copy.
        proc._pending_bare_number_words.append("three")
        assert proc._held_tail.words == ["twenty", "three"]

    def test_cross_kind_read_is_none(self):
        proc = make_retraction_processor()
        proc._pending_trailing_word = "submit"
        assert proc._pending_replacement_prefix is None
        assert proc._pending_bare_number_words is None

    def test_same_kind_none_set_clears(self):
        proc = make_retraction_processor()
        proc._pending_trailing_word = "submit"
        proc._pending_trailing_word = None
        assert proc._held_tail is None

    def test_cross_kind_none_set_preserves_tail(self):
        """The old attributes were independent: the bare-number flush
        setting ITS slot to None must not clear a held trailing word.
        Every kind flush method ends with a None-assignment to its own
        name, so this is what keeps those methods kind-scoped."""
        proc = make_retraction_processor()
        proc._pending_trailing_word = "submit"
        proc._pending_bare_number_words = None
        proc._pending_replacement_prefix = None
        assert proc._held_tail is not None
        assert proc._pending_trailing_word == "submit"

    def test_arming_second_kind_evicts_and_logs(self, caplog):
        """No live sequence arms a kind while a different kind is held
        (trailing flushes on any non-marker event; a bare number arms
        only on the utterance-opening word; the prefix pass runs and
        flushes first at the top of the loop). If a future change
        breaks that, the eviction must be loud, not a silent word
        drop."""
        proc = make_retraction_processor()
        proc._pending_trailing_word = "submit"
        with caplog.at_level(
            logging.ERROR, logger="wheelhouse.pipeline"
        ):
            proc._pending_bare_number_words = ["three"]
        assert proc._held_tail.kind.name == "BARE_NUMBER"
        assert any(
            "held tail" in r.getMessage() for r in caplog.records
        )

    def test_eviction_log_redacts_the_held_words(self, caplog, monkeypatch):
        """The tripwire fires precisely when held words meet an
        unexpected arming sequence, so it follows the file's redaction
        discipline: no user speech in the pipeline log unless the
        launcher exported WHEELHOUSE_LOG_TRANSCRIPTS=1 (deepseek,
        wh-whole-utterance-command-matching.2.1)."""
        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        proc = make_retraction_processor()
        proc._pending_trailing_word = "submit"
        with caplog.at_level(
            logging.ERROR, logger="wheelhouse.pipeline"
        ):
            proc._pending_bare_number_words = ["three"]
        messages = [r.getMessage() for r in caplog.records]
        assert any("held tail" in m for m in messages)
        assert not any("submit" in m for m in messages)


# ============================================================================
# TESTS -- the unified flush
# ============================================================================

class TestUnifiedFlush:

    @pytest.mark.asyncio
    async def test_unified_flush_prefix_joins_words(self):
        proc = make_retraction_processor()
        proc._send_to_dictation = AsyncMock()
        proc._pending_replacement_prefix = ["question", "mark"]
        await proc._flush_held_tail_as_dictation()
        assert proc._send_to_dictation.await_args_list == [
            call("question mark"),
        ]
        assert proc._held_tail is None

    @pytest.mark.asyncio
    async def test_unified_flush_trailing_sends_word(self):
        proc = make_retraction_processor()
        proc._send_to_dictation = AsyncMock()
        proc._pending_trailing_word = "submit"
        await proc._flush_held_tail_as_dictation()
        assert proc._send_to_dictation.await_args_list == [
            call("submit"),
        ]
        assert proc._held_tail is None

    @pytest.mark.asyncio
    async def test_unified_flush_bare_number_one_ipc_per_word(self):
        """The bare-number flush sends one dictation per held word in
        spoken order -- joining them would change what a multi-word
        hold types. It also clears the deferred-word slot."""
        proc = make_retraction_processor()
        proc._send_to_dictation = AsyncMock()
        proc._pending_bare_number_words = ["twenty", "three"]
        proc._bare_number_deferred_word = "three"
        await proc._flush_held_tail_as_dictation()
        assert proc._send_to_dictation.await_args_list == [
            call("twenty"), call("three"),
        ]
        assert proc._held_tail is None
        assert proc._bare_number_deferred_word is None

    @pytest.mark.asyncio
    async def test_unified_flush_empty_is_noop(self):
        proc = make_retraction_processor()
        proc._send_to_dictation = AsyncMock()
        await proc._flush_held_tail_as_dictation()
        proc._send_to_dictation.assert_not_awaited()


# ============================================================================
# TESTS -- lifecycle
# ============================================================================

class TestHeldTailLifecycle:

    @pytest.mark.asyncio
    async def test_stop_clears_tail(self):
        """stop() sacrifices held words for deterministic shutdown
        (the crewcut comment in stop() names the trade-off); with one
        slot that is one clear, whatever the kind."""
        proc = make_retraction_processor()
        proc._pending_trailing_word = "submit"
        await proc.stop()
        assert proc._held_tail is None


# ============================================================================
# TESTS -- the prefix hold across an utterance boundary
# (wh-spaced-punctuation-names-unresolved.3)
# ============================================================================
#
# Stage B lets a punctuation name whose words arrive in SEPARATE
# utterances reach its mark. The tests below pin the four properties
# that make that safe, at processor level; the user-visible sequences
# live in tests/test_pipeline_punctuation_names.py.
#
# These use SpeechPipelineHarness rather than make_retraction_processor
# because every one of them asks the REAL router a real catalog
# question: whether a word list is an exact opening of a replacement
# name. A MagicMock catalog cannot answer that.


class _NoRedirectDecision:
    """A focus-redirect answer that declines, with a target hwnd."""

    open_editor = False
    target_terminal_hwnd = 0


class _RecordingFocusPolicy:
    """Focus-redirect policy stand-in that records the HWNDs it saw."""

    def __init__(self):
        self.seen_hwnds = []

    async def should_redirect(self, focused_hwnd):
        self.seen_hwnds.append(focused_hwnd)
        return _NoRedirectDecision()

    def on_utterance_end(self):
        pass


class TestTheHoldArmsAtAnUtteranceEnd:
    """The gate that used to refuse every hold at an utterance end."""

    def test_a_name_opening_holds(self):
        """A whole utterance that opens a name is held, not typed."""
        proc = SpeechPipelineHarness().processor
        proc._words_this_utterance = ["open"]
        assert proc._should_hold_replacement_prefix(
            ["open"], hotword_active=False, end_of_utterance=True,
        ) is True

    def test_a_word_that_is_not_a_name_opening_still_refuses(self):
        """The refusal the exception was carved out of is intact."""
        proc = SpeechPipelineHarness().processor
        proc._words_this_utterance = ["hello"]
        assert proc._should_hold_replacement_prefix(
            ["hello"], hotword_active=False, end_of_utterance=True,
        ) is False

    def test_a_tail_that_merely_ends_in_a_name_word_refuses(self):
        """"hold it open" ends on a name's first word and is not one.

        The sentence must type at once. Holding it would delay an
        ordinary sentence for the whole release deadline.
        """
        proc = SpeechPipelineHarness().processor
        proc._words_this_utterance = ["hold", "it", "open"]
        assert proc._should_hold_replacement_prefix(
            ["hold", "it", "open"],
            hotword_active=False,
            end_of_utterance=True,
        ) is False

    def test_a_name_opening_that_is_only_part_of_the_utterance_refuses(self):
        """The same words, spoken after others, are a tail not a name.

        This is what keeps "I said back space" prompt: "space" opens
        ``space bar``, but the user plainly did not begin a name.
        """
        proc = SpeechPipelineHarness().processor
        proc._words_this_utterance = ["I", "said", "back", "space"]
        assert proc._should_hold_replacement_prefix(
            ["space"], hotword_active=False, end_of_utterance=True,
        ) is False


class TestTheHoldNeverOutlivesItsBound:

    @pytest.mark.asyncio
    async def test_a_growing_hold_arms_one_deadline_and_no_more(self):
        """One release deadline, armed once, for the whole hold.

        "open" / "single" / "quote" said as three utterances grows the
        hold twice before the mark fires. Neither growth may arm a
        second timer: the hold must expire ``replacement_timeout_ms``
        after it was armed, not after its last word.
        """
        harness = SpeechPipelineHarness()
        proc = harness.processor
        armed = []
        real_start = proc._start_timeout

        def spy(duration_ms):
            armed.append(
                (duration_ms, list(proc._pending_replacement_prefix or []))
            )
            real_start(duration_ms)

        proc._start_timeout = spy
        await harness.start()
        try:
            for index, words in enumerate([["open"], ["single"], ["quote"]]):
                if index:
                    await asyncio.sleep(0.05)
                await harness.send_utterance(words)
                await harness.send_utterance_end_marker(
                    harness._utterance_counter
                )
            await asyncio.sleep(0.1)
            insertions = harness.mock_app.get_dictation_texts()
        finally:
            await harness.stop()

        # Every timer armed WHILE words were held, in order. Exactly one,
        # for replacement_timeout_ms, armed when the hold was created --
        # the two later growths ("open single", then the completion)
        # armed nothing.
        while_held = [entry for entry in armed if entry[1]]
        assert while_held == [
            (proc.replacement_timeout_ms, ["open"]),
        ], armed
        # And the sequence really did complete, so the assertion above
        # is about a hold that grew rather than one that never formed.
        assert insertions == ["'"], insertions

    @pytest.mark.asyncio
    async def test_the_deadline_uses_the_replacement_timeout(self):
        """The bound is replacement_timeout_ms, not a new setting."""
        harness = SpeechPipelineHarness()
        proc = harness.processor
        armed = []
        real_start = proc._start_timeout

        def spy(duration_ms):
            armed.append(
                (duration_ms, list(proc._pending_replacement_prefix or []))
            )
            real_start(duration_ms)

        proc._start_timeout = spy
        await harness.start()
        try:
            await harness.send_utterance(["question"])
            await harness.send_utterance_end_marker(
                harness._utterance_counter
            )
            await asyncio.sleep(0.05)
            held_arming = [entry for entry in armed if entry[1]]
        finally:
            await harness.stop()

        assert held_arming == [
            (proc.replacement_timeout_ms, ["question"]),
        ], armed


class TestAHoldLosesNoWords:

    @pytest.mark.asyncio
    async def test_expiry_after_the_focus_answer_changed_types_it_all(self):
        """Every held word reaches _send_to_dictation at the deadline.

        The world can move under a hold: the release deadline fires up
        to ``replacement_timeout_ms`` after the words were held, and the
        focused window may be a different one by then. The flush still
        goes through ``_send_to_dictation``, which consults the
        focus-redirect policy with the CURRENT window, and every held
        word is typed.
        """
        harness = SpeechPipelineHarness()
        proc = harness.processor
        policy = _RecordingFocusPolicy()
        # maybe_route_to_editor returns before the policy consult when no
        # logic_controller is wired, so wire one. The policy declines
        # every redirect, so the editor IPC on it is never called and the
        # text falls through to the legacy dictation path.
        proc.logic_controller = MagicMock()
        proc.focus_redirect_policy = policy
        hwnd = [111]
        proc._focused_hwnd_provider = lambda: hwnd[0]
        sent = []
        real_send = proc._send_to_dictation

        async def spy(text, *args, **kwargs):
            sent.append(text)
            return await real_send(text, *args, **kwargs)

        proc._send_to_dictation = spy
        await harness.start()
        try:
            await harness.send_word(
                "open", start_of_utterance=True, end_of_utterance=True,
            )
            await harness.send_utterance_end_marker(
                harness._utterance_counter
            )
            await harness.send_word(
                "single", start_of_utterance=True, end_of_utterance=True,
            )
            await harness.send_utterance_end_marker(
                harness._utterance_counter
            )
            assert proc._pending_replacement_prefix == ["open", "single"]
            # The user moved to another window while the hold stood.
            hwnd[0] = 222
            # Past the release deadline.
            await asyncio.sleep(proc.replacement_timeout_ms / 1000.0 + 0.2)
            insertions = harness.mock_app.get_dictation_texts()
        finally:
            await harness.stop()

        assert sent == ["open single"], sent
        assert insertions == ["open single"], insertions
        assert proc._held_tail is None
        # The flush really did run through _send_to_dictation's focus
        # path, and saw the window the user had moved to.
        assert policy.seen_hwnds == [222], policy.seen_hwnds

    @pytest.mark.asyncio
    async def test_a_lifecycle_reset_marker_flushes_the_hold(self):
        """The lifecycle backstop still empties the slot, losing nothing.

        The marker pairs an end_utterance with a start_utterance for the
        same utterance, so held words must never survive it and land in
        phrase 2. A hold that is an exact name opening is no exception:
        that shape now survives an ordinary utterance-end MARKER, and
        this test is what keeps the exception out of the lifecycle path.
        The pipeline harness has no provider-change helper, so the
        marker event is built here.
        """
        harness = SpeechPipelineHarness()
        proc = harness.processor
        await harness.start()
        try:
            proc._pending_replacement_prefix = ["open", "single"]
            await proc.process_word_event(WordEvent(
                word="",
                start_of_utterance=False,
                end_of_utterance=False,
                utterance_id=7,
                is_lifecycle_reset_marker=True,
            ))
            actions = harness.mock_app.get_all_actions()
            insertions = harness.mock_app.get_dictation_texts()
        finally:
            await harness.stop()

        assert proc._held_tail is None
        assert insertions == ["open single"], insertions
        # The words landed BEFORE the pair that closes phrase 1.
        assert actions.index("intelligent_insert_text") < actions.index(
            "end_utterance"
        ), actions

class TestALifecycleResetDrainsWhatItArms:
    """wh-spaced-punctuation-names-unresolved.3.1.4, codex round 2.

    The class above pins the reset against a hold that already stood on
    entry. These pin it against one the reset's OWN buffer finalization
    creates, which is a different path: the entry drain
    (_consume_held_tail_at_utterance_end) has already run by then, and
    _consume_finalization_armed_tail returns early for the replacement
    kind on purpose -- the ordinary end-marker branch wants that hold to
    survive so a name can finish across an utterance boundary.

    A lifecycle reset is not that boundary. It pairs an end_utterance
    with a start_utterance for the same utterance id, so phrase 1 must
    be delivered before the pair whatever armed it.

    The reset marker is built through the harness rather than by hand so
    the words really pass through the processing loop and leave the
    buffer live, which is the state the finding needs and the state
    setting _pending_replacement_prefix directly cannot produce.
    """

    @pytest.mark.asyncio
    async def test_the_reset_types_a_name_opening_its_own_finalization_held(
        self,
    ):
        """"open", still buffered, must type before the reset's pair."""
        harness = SpeechPipelineHarness()
        proc = harness.processor
        await harness.start()
        try:
            await harness.send_word("open", start_of_utterance=True)
            # The state the finding needs: a live buffer, nothing held.
            assert proc._held_tail is None
            await harness.send_word(
                "", utterance_id=1, is_lifecycle_reset_marker=True,
            )
            actions = harness.mock_app.get_all_actions()
            insertions = harness.mock_app.get_dictation_texts()
        finally:
            await harness.stop()

        assert proc._held_tail is None, proc._held_tail
        assert insertions == ["open"], (insertions, actions)
        assert actions.index("intelligent_insert_text") < actions.index(
            "end_utterance"
        ), actions

    @pytest.mark.asyncio
    async def test_the_phrase_after_the_reset_cannot_finish_the_name(self):
        """The whole harm: phrase 2 completing phrase 1's name.

        This is the sequence the shipped Mode 1 producer builds when a
        fallback final does not extend the prior stable text --
        integrations/websocket_manager.py _handle_mode1_fresh_content
        queues the reset and then the fresh phrase. "open" and
        "bracket" belong to different phrases and must never make "[".
        """
        harness = SpeechPipelineHarness()
        proc = harness.processor
        await harness.start()
        try:
            await harness.send_word("open", start_of_utterance=True)
            await harness.send_word(
                "", utterance_id=1, is_lifecycle_reset_marker=True,
            )
            await harness.send_word("bracket", start_of_utterance=True)
            await harness.wait_for_timeout()
            actions = harness.mock_app.get_all_actions()
            insertions = harness.mock_app.get_dictation_texts()
        finally:
            await harness.stop()

        assert "[" not in insertions, (insertions, actions)
        assert " ".join(insertions) == "open bracket", (insertions, actions)

    @pytest.mark.asyncio
    async def test_a_complete_name_still_reaches_its_mark_at_an_end_marker(
        self,
    ):
        """Control: the ordinary end marker keeps the .3 behaviour.

        The fix is lifecycle-only. An utterance-end MARKER that
        finalizes the same live buffer must still arm the hold, because
        that is what lets the next utterance finish the name -- the
        whole of Stage B.
        """
        harness = SpeechPipelineHarness()
        proc = harness.processor
        await harness.start()
        try:
            await harness.send_word("open", start_of_utterance=True)
            await harness.send_utterance_end_marker(1)
            held = proc._pending_replacement_prefix
            insertions = harness.mock_app.get_dictation_texts()
        finally:
            await harness.stop()

        assert held == ["open"], (held, insertions)
        assert insertions == [], insertions
