"""Whole-utterance finalization (wh-whole-utterance-command-matching, Stage 3).

Stage 3 moves the command-prefix loop and the cannot-match fallback off
the word-by-word path and onto the utterance end. Two mechanical
changes:

- the impossible-pattern path in ``SpeechRouter._decide_buffering``
  DEFERS instead of finalizing mid-utterance: the buffer keeps
  accumulating with the mode's existing fixed timeout as the fallback
  (David's item-16 answer: current fixed timing, no per-provider
  latency measurement), and finalization runs at the utterance-end
  marker, the fixed timeout, the new-utterance auto-finalize, or the
  lifecycle-reset close;
- the finalization gains utterance context: a whole_utterance_only
  pattern (patterns.toml:18-36) fires on a whole-buffer match only when
  the buffer really spans the utterance, judged against the Stage-1
  per-utterance word list -- and on the auto-finalize path against the
  SNAPSHOT of the PREVIOUS utterance's list, because the Stage-1 reset
  at the top of process_word_event runs before the auto-finalize
  further down (the recorded read-before-reset ordering caveat).

The absorbed suites (test_e2e_replacement_prefix_after_command.py,
test_speech_processor_trailing_command.py,
test_speech_processor_bare_number.py, test_router_command_prefix.py)
stay green unmodified and are the behaviour-preservation proof.
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

import pytest

from speech.domain import Action, ProcessingMode
from speech.pattern_catalog import PatternCatalog
from speech.router import SpeechRouter
from speech.word_event import WordEvent

from test_speech_pipeline import SpeechPipelineHarness


@pytest.fixture(scope="module")
def router():
    return SpeechRouter(PatternCatalog("speech/config/patterns.toml"))


@pytest.fixture
async def running_harness():
    harness = SpeechPipelineHarness()
    await harness.start()
    yield harness
    await harness.stop()


def _word(word: str, *, start: bool = False) -> WordEvent:
    return WordEvent(
        word=word,
        start_of_utterance=start,
        end_of_utterance=False,
        utterance_id=1,
    )


# ============================================================================
# ROUTER -- the cannot-match fallback defers instead of finalizing
# ============================================================================

class TestImpossibleBufferDefers:

    def test_impossible_command_buffer_defers_to_the_end(self, router):
        """'backspace' + 'hello' used to finalize at word speed (the
        prefix loop fired backspace mid-utterance). Stage 3 keeps
        buffering so the whole utterance is matched at its end; the
        fixed command timeout stays the fallback."""
        decision = router.decide(
            _word("hello"),
            ProcessingMode.COMMAND_BUFFERING,
            ["backspace"],
            command_timeout_ms=1000,
            replacement_timeout_ms=400,
        )
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == 1000

    def test_disproved_command_buffer_defers_to_the_end(self, router):
        """The T-17877987532 shape: 'okay' opened a command buffer
        (^okay Google.*$) and 'question' disproved it. The old path
        finalized as dictation at word speed; Stage 3 defers."""
        decision = router.decide(
            _word("question"),
            ProcessingMode.COMMAND_BUFFERING,
            ["okay"],
            command_timeout_ms=1000,
            replacement_timeout_ms=400,
        )
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == 1000

    def test_timeout_finalization_still_resolves(self, router):
        """decide_timeout is the fixed fallback and must keep
        finalizing -- deferral applies only to the word-driven path."""
        decision = router.decide_timeout(["backspace", "hello"], False)
        assert decision.action == Action.EXECUTE
        assert decision.remainder == "hello"


# ============================================================================
# ROUTER -- whole_utterance_only judged against the utterance, not the buffer
# ============================================================================

class TestWholeUtteranceOnlyContext:

    def test_alias_fires_when_the_buffer_spans_the_utterance(self, router):
        decision = router.decide_timeout(
            ["save"], False, utterance_words=["save"]
        )
        assert decision.action == Action.EXECUTE

    def test_alias_suppressed_when_earlier_words_streamed(self, router):
        """The utterance was 'hello save'; only 'save' sits in the
        buffer. whole_utterance_only means the ENTIRE utterance
        (patterns.toml:18-36), so the alias must not fire."""
        decision = router.decide_timeout(
            ["save"], False, utterance_words=["hello", "save"]
        )
        assert decision.action == Action.DICTATE

    def test_hotword_head_still_counts_as_spanning(self, router):
        """An active wake word sits in the word list but never in the
        buffer (TRANSITION clears it). 'x-ray save' with hotword
        active is still 'save' as the whole utterance."""
        decision = router.decide_timeout(
            ["save"], True, utterance_words=["x-ray", "save"]
        )
        assert decision.action == Action.EXECUTE

    def test_missing_context_keeps_legacy_behavior(self, router):
        """Callers that pass no word list (direct decide_timeout uses in
        older tests) keep today's fire-on-buffer-match behavior."""
        decision = router.decide_timeout(["save"], False)
        assert decision.action == Action.EXECUTE

    def test_fused_hotword_head_still_counts_as_spanning(self, router):
        """STT engines can fuse the hyphenated wake word ('xray' for
        'x-ray'), and detection accepts that via the hyphen-insensitive
        _word_matches_hotword. The span judgment must accept the same
        head, or a fused wake word silently suppresses every
        whole_utterance_only alias it authorizes
        (wh-whole-utterance-command-matching.3.1.2)."""
        decision = router.decide_timeout(
            ["save"], True, utterance_words=["xray", "save"]
        )
        assert decision.action == Action.EXECUTE


# ============================================================================
# ROUTER -- the flagged last word carries the same utterance context
# ============================================================================

class TestFlaggedLastWordCarriesContext:
    """The in-process STT bridge (main.py _handle_stt_transcript) sets
    end_of_utterance=True on the utterance's last real WORD, and step 1
    of _decide_buffering finalizes on that flag. That finalization must
    receive the same utterance_words context the marker/timeout path
    gets, or the two provider families diverge on whole_utterance_only
    (wh-whole-utterance-command-matching.3.1.1)."""

    def _flagged(self, word: str) -> WordEvent:
        return WordEvent(
            word=word,
            start_of_utterance=False,
            end_of_utterance=True,
            utterance_id=1,
        )

    def test_flagged_finalization_suppresses_a_non_spanning_alias(self, router):
        decision = router.decide(
            self._flagged("save"),
            ProcessingMode.COMMAND_BUFFERING,
            [],
            command_timeout_ms=1000,
            replacement_timeout_ms=400,
            utterance_words=["hello", "save"],
        )
        assert decision.action == Action.DICTATE

    def test_flagged_finalization_fires_a_spanning_alias(self, router):
        decision = router.decide(
            self._flagged("save"),
            ProcessingMode.COMMAND_BUFFERING,
            [],
            command_timeout_ms=1000,
            replacement_timeout_ms=400,
            utterance_words=["save"],
        )
        assert decision.action == Action.EXECUTE


# ============================================================================
# PIPELINE -- deferral, the fixed fallback, and the cut-short cases
# ============================================================================

class TestDeferralAtThePipeline:

    @pytest.mark.asyncio
    async def test_impossible_buffer_waits_for_the_end_marker(
        self, running_harness
    ):
        """Nothing types at word speed once the buffer went impossible;
        the end marker resolves the whole utterance: backspace fires,
        'hello' dictates."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("hello", delay_before_ms=50)
        await asyncio.sleep(0.15)
        assert running_harness.get_dictation_texts() == []

        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(200)
        combined = " ".join(running_harness.get_dictation_texts())
        assert "hello" in combined
        assert "backspace" not in combined

    @pytest.mark.asyncio
    async def test_dropped_connection_resolves_on_the_fixed_timeout(
        self, running_harness
    ):
        """Cut-short case (dropped connection or device change): the
        end marker never arrives -- integrations/websocket_manager.py
        takes it down with the terminal FINAL message. The fixed
        command timeout (item 16: current fixed timing) finalizes the
        deferred words so nothing is lost."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("hello", delay_before_ms=50)
        await asyncio.sleep(0.3)
        assert running_harness.get_dictation_texts() == []

        await running_harness.wait_for_timeout(1100)
        combined = " ".join(running_harness.get_dictation_texts())
        assert "hello" in combined
        assert "backspace" not in combined

    @pytest.mark.asyncio
    async def test_stop_mid_deferral_discards_without_typing(
        self, running_harness
    ):
        """Cut-short case (stop): stopping the processor mid-buffer
        keeps its documented behavior -- the deferred words are
        discarded, not typed (the stop() crewcut on the held tail
        covers held words the same way). Changed only on David's
        word."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("hello", delay_before_ms=50)
        await running_harness.stop()
        assert running_harness.get_dictation_texts() == []
        # the fixture's stop() tolerates a second call
        await running_harness.start()

    @pytest.mark.asyncio
    async def test_lifecycle_reset_closes_the_deferred_buffer(
        self, running_harness
    ):
        """Cut-short case (Mode 1 fallback disagreement): the
        lifecycle-reset marker closes phrase 1, so the deferred buffer
        must finalize BEFORE the end_utterance/start_utterance pair --
        phrase 1's text may not land inside phrase 2's window."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("hello", delay_before_ms=50)
        await running_harness.send_word(
            "",
            is_lifecycle_reset_marker=True,
            utterance_id=running_harness._utterance_counter,
        )
        await running_harness.wait_for_timeout(100)
        combined = " ".join(running_harness.get_dictation_texts())
        assert "hello" in combined
        assert "backspace" not in combined

    @pytest.mark.asyncio
    async def test_lifecycle_reset_sends_the_pair_even_when_finalization_raises(
        self, running_harness
    ):
        """The finalize call the deferral added to the lifecycle branch
        sits BEFORE the end_utterance/start_utterance pair. A raise
        during that IPC must not skip the pair -- the Input process
        would keep phrase 1's window open and phrase 2's text would
        land inside it (wh-whole-utterance-command-matching.3.1.3
        instance 2)."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("hello", delay_before_ms=50)
        await asyncio.sleep(0.15)

        processor = running_harness.processor
        original = processor._execute_decision

        async def boom(decision, word_event=None):
            raise RuntimeError("finalization IPC failed (test)")

        processor._execute_decision = boom
        try:
            await running_harness.send_word(
                "",
                is_lifecycle_reset_marker=True,
                utterance_id=running_harness._utterance_counter,
            )
            await running_harness.wait_for_timeout(100)
        finally:
            processor._execute_decision = original

        actions = running_harness.mock_app.get_all_actions()
        assert 'end_utterance' in actions
        assert 'start_utterance' in actions

    @pytest.mark.asyncio
    async def test_lifecycle_reset_sends_the_pair_even_when_a_held_flush_raises(
        self, running_harness
    ):
        """The held-tail dispatch a lifecycle marker triggers is also
        pre-pair IPC: if it raises, the end_utterance/start_utterance
        pair must still send. Before the .3.1.4 fix the top-of-loop
        advance ran the held-tail flush outside the lifecycle branch's
        recovery guard, so the raise skipped the pair. Since the
        .3.1.8 fix the in-guard dispatch consumes (fires the trailing
        action) instead of flushing, so the raise is injected into the
        action-firing body the consume reaches."""
        await running_harness.send_word("submit", start_of_utterance=True)
        await asyncio.sleep(0.1)
        processor = running_harness.processor
        assert processor._pending_trailing_word == "submit"

        original = processor._fire_trailing_action_for_word

        async def boom(_word):
            raise RuntimeError("trailing-action IPC failed (test)")

        processor._fire_trailing_action_for_word = boom
        try:
            await running_harness.send_word(
                "",
                is_lifecycle_reset_marker=True,
                utterance_id=running_harness._utterance_counter,
            )
            await running_harness.wait_for_timeout(100)
        finally:
            processor._fire_trailing_action_for_word = original

        actions = running_harness.mock_app.get_all_actions()
        assert 'end_utterance' in actions
        assert 'start_utterance' in actions


# ============================================================================
# PIPELINE -- the armed trailing action fires while the utterance is open
# ============================================================================

def _enter_indexes(outputs):
    return [
        i for i, out in enumerate(outputs)
        if out.action == "hotkey_action"
        and "enter" in str(out.params).lower()
    ]


def _end_utterance_indexes(outputs):
    return [
        i for i, out in enumerate(outputs) if out.action == "end_utterance"
    ]


class TestMarkerFinalizationOrdering:
    """The end-marker path must run its work while the utterance is
    still open: the Input process restores the user's clipboard at
    end_utterance (the invariant test_ui/test_utterance_clipboard_race.py
    pins), so the trailing action the R1 split arms has to fire BEFORE
    the deferred end_utterance goes out
    (wh-whole-utterance-command-matching.3.1.5)."""

    @pytest.mark.asyncio
    async def test_remainder_split_fires_enter_before_end_utterance(
        self, running_harness
    ):
        """'backspace submit' at the end marker: the EXECUTE branch's
        remainder split arms submit. Enter must precede the single
        end_utterance. 'backspace' also presses a key, so the filter
        matches the enter params, not just the action name."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("submit", delay_before_ms=50)
        await asyncio.sleep(0.15)
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(200)

        outputs = running_harness.mock_app.outputs
        enters = _enter_indexes(outputs)
        ends = _end_utterance_indexes(outputs)
        assert enters, "the armed trailing action never fired"
        assert len(ends) == 1
        assert enters[-1] < ends[0]

    @pytest.mark.asyncio
    async def test_dictate_split_fires_enter_before_end_utterance(
        self, running_harness
    ):
        """'save submit' at the end marker: the whole_utterance_only
        suppression sends the payload down the DICTATE branch, whose
        split arms submit. Same ordering requirement as the remainder
        site."""
        await running_harness.send_word("save", start_of_utterance=True)
        await running_harness.send_word("submit", delay_before_ms=50)
        await asyncio.sleep(0.15)
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(200)

        outputs = running_harness.mock_app.outputs
        enters = _enter_indexes(outputs)
        ends = _end_utterance_indexes(outputs)
        assert enters, "the armed trailing action never fired"
        assert len(ends) == 1
        assert enters[-1] < ends[0]
        assert any(
            "save" in text
            for text in running_harness.get_dictation_texts()
        )


class TestLifecycleCloseTakesTheSplit:
    """The lifecycle-reset marker IS phrase 1 closing (the Mode-1
    contract in integrations/websocket_manager.py), so the R1 split
    applies to the finalization it runs: the same spoken words must
    not behave differently by internal close path. Boss ruling on
    wh-whole-utterance-command-matching.3.1.6, option (a): both split
    sites, trailing action before the end/start pair."""

    @pytest.mark.asyncio
    async def test_lifecycle_remainder_split_fires_enter_before_the_pair(
        self, running_harness
    ):
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("submit", delay_before_ms=50)
        await asyncio.sleep(0.15)
        await running_harness.send_word(
            "",
            is_lifecycle_reset_marker=True,
            utterance_id=running_harness._utterance_counter,
        )
        await running_harness.wait_for_timeout(200)

        outputs = running_harness.mock_app.outputs
        enters = _enter_indexes(outputs)
        ends = _end_utterance_indexes(outputs)
        assert enters, "the trailing action never fired"
        assert ends
        assert enters[-1] < ends[0]
        combined = " ".join(running_harness.get_dictation_texts())
        assert "submit" not in combined

    @pytest.mark.asyncio
    async def test_lifecycle_dictate_split_fires_enter_before_the_pair(
        self, running_harness
    ):
        await running_harness.send_word("save", start_of_utterance=True)
        await running_harness.send_word("submit", delay_before_ms=50)
        await asyncio.sleep(0.15)
        await running_harness.send_word(
            "",
            is_lifecycle_reset_marker=True,
            utterance_id=running_harness._utterance_counter,
        )
        await running_harness.wait_for_timeout(200)

        outputs = running_harness.mock_app.outputs
        enters = _enter_indexes(outputs)
        ends = _end_utterance_indexes(outputs)
        assert enters, "the trailing action never fired"
        assert ends
        assert enters[-1] < ends[0]
        combined = " ".join(running_harness.get_dictation_texts())
        assert "save" in combined
        assert "submit" not in combined


class _PaintedOverlayController:
    """Minimal stand-in for logic_controller with badges showing.

    _overlay_accepts_bare_number compares the state's VALUE string, so
    a plain attribute chain is enough -- no OverlayState import needed.
    """

    class _Machine:
        class _State:
            value = "painted"

        state = _State()

    click_overlay_state = _Machine()


class TestLifecycleCloseConsumesPreHeldTail:
    """A tail held BEFORE the lifecycle marker arrives (the processor
    already back to IDLE) must take the same end-of-utterance dispatch
    an ordinary end marker gives it: the trailing command fires, the
    bare number clicks -- both before the end/start pair. Boss ruling
    on wh-whole-utterance-command-matching.3.1.8, option (a): the
    Mode-1 marker is a confirmed utterance end, and hold timing
    (before vs during the close) cannot change what that end means."""

    @pytest.mark.asyncio
    async def test_lifecycle_preheld_trailing_word_fires_enter_before_the_pair(
        self, running_harness
    ):
        await running_harness.send_word("submit", start_of_utterance=True)
        await asyncio.sleep(0.15)
        processor = running_harness.processor
        # The mechanism claim: a pre-held tail exists only with the
        # processor idle, so the lifecycle branch's held-tail call is
        # the only path that can dispatch it.
        assert processor._pending_trailing_word == "submit"
        assert processor.mode == ProcessingMode.IDLE

        await running_harness.send_word(
            "",
            is_lifecycle_reset_marker=True,
            utterance_id=running_harness._utterance_counter,
        )
        await running_harness.wait_for_timeout(200)

        outputs = running_harness.mock_app.outputs
        enters = _enter_indexes(outputs)
        ends = _end_utterance_indexes(outputs)
        assert enters, "the trailing action never fired"
        assert ends
        assert enters[-1] < ends[0]
        combined = " ".join(running_harness.get_dictation_texts())
        assert "submit" not in combined

    @pytest.mark.asyncio
    async def test_lifecycle_preheld_bare_number_clicks_before_the_pair(
        self, running_harness
    ):
        processor = running_harness.processor
        processor.logic_controller = _PaintedOverlayController()

        clicks = []

        async def record_click(text, return_remainder=False,
                               authorized_command=False):
            clicks.append(text)
            await running_harness.mock_app.send_command({
                'action': 'bare_number_click', 'params': {'text': text},
            })
            if return_remainder:
                return True, ""
            return True

        processor.text_parser.parse_and_execute = record_click
        processor.text_parser.last_executed_pattern_type = "command"

        await running_harness.send_word("70", start_of_utterance=True)
        await asyncio.sleep(0.15)
        assert processor._pending_bare_number_words == ["70"]
        assert processor.mode == ProcessingMode.IDLE

        await running_harness.send_word(
            "",
            is_lifecycle_reset_marker=True,
            utterance_id=running_harness._utterance_counter,
        )
        await running_harness.wait_for_timeout(200)

        outputs = running_harness.mock_app.outputs
        click_idxs = [
            i for i, out in enumerate(outputs)
            if out.action == "bare_number_click"
        ]
        ends = _end_utterance_indexes(outputs)
        assert click_idxs, "the bare-number click never fired"
        assert clicks == ["click 70"]
        assert ends
        assert click_idxs[-1] < ends[0]
        combined = " ".join(running_harness.get_dictation_texts())
        assert "70" not in combined


class TestPunctuatedTrailingWord:
    """STT/ITN attaches terminal punctuation to finals ("submit."), and
    PatternCatalog.get_trailing_command deliberately normalizes it away
    at lookup. Every producer then stores the RAW token, and
    _fire_trailing_action_for_word re-matched the raw token against the
    anchored ^submit$ pattern -- the mismatch fallback dictated the
    token instead of firing Enter
    (wh-whole-utterance-command-matching.3.1.7, codex round 3)."""

    @pytest.mark.asyncio
    async def test_punctuated_ordinary_trailing_word_fires_enter(
        self, running_harness
    ):
        await running_harness.send_word("submit.", start_of_utterance=True)
        await asyncio.sleep(0.1)
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(200)

        outputs = running_harness.mock_app.outputs
        assert _enter_indexes(outputs), "the trailing action never fired"
        combined = " ".join(running_harness.get_dictation_texts())
        assert "submit" not in combined

    @pytest.mark.asyncio
    async def test_punctuated_remainder_split_fires_enter(
        self, running_harness
    ):
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("submit.", delay_before_ms=50)
        await asyncio.sleep(0.15)
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(200)

        outputs = running_harness.mock_app.outputs
        enters = _enter_indexes(outputs)
        ends = _end_utterance_indexes(outputs)
        assert enters, "the trailing action never fired"
        assert len(ends) == 1
        assert enters[-1] < ends[0]
        combined = " ".join(running_harness.get_dictation_texts())
        assert "submit" not in combined

    @pytest.mark.asyncio
    async def test_punctuated_dictate_split_fires_enter(
        self, running_harness
    ):
        await running_harness.send_word("save", start_of_utterance=True)
        await running_harness.send_word("submit.", delay_before_ms=50)
        await asyncio.sleep(0.15)
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(200)

        outputs = running_harness.mock_app.outputs
        assert _enter_indexes(outputs), "the trailing action never fired"
        combined = " ".join(running_harness.get_dictation_texts())
        assert "save" in combined
        assert "submit" not in combined


# ============================================================================
# PIPELINE -- whole_utterance_only across the auto-finalize boundary
# ============================================================================

class TestAutoFinalizeReadsThePreviousUtterance:

    @pytest.mark.asyncio
    async def test_save_alone_fires_at_the_end_marker(self, running_harness):
        await running_harness.send_word("save", start_of_utterance=True)
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(200)
        assert "save" not in " ".join(running_harness.get_dictation_texts())

    @pytest.mark.asyncio
    async def test_cut_short_save_still_fires_on_auto_finalize(
        self, running_harness
    ):
        """The Stage-1 read-before-reset ordering caveat, as behavior.
        Utterance A is exactly 'save' (a whole_utterance_only command)
        but its end marker never arrives; utterance B starts and the
        auto-finalize closes A. A's buffer spanned A's utterance, so
        save must still FIRE -- which requires the auto-finalize to
        judge the buffer against A's word list, snapshotted before the
        Stage-1 reset replaced it with B's. A naive read of the live
        list sees B's words and wrongly suppresses the command."""
        await running_harness.send_word("save", start_of_utterance=True)
        await asyncio.sleep(0.05)
        assert running_harness.get_dictation_texts() == []

        await running_harness.send_word(
            "hello", start_of_utterance=True, delay_before_ms=50
        )
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(200)
        combined = " ".join(running_harness.get_dictation_texts())
        assert "save" not in combined
        assert "hello" in combined


# ============================================================================
# PIPELINE -- a raise mid-finalization cannot end the previous utterance late
# ============================================================================

class TestPendingUtteranceEndSurvivesARaise:
    """The end-marker branch sets _pending_utterance_end before it
    finalizes the buffer. A raise between the set and the send used to
    leave the slot standing, because the only clears were a successful
    _send_pending_utterance_end and stop(); the NEXT utterance then sent
    end_utterance carrying the PREVIOUS utterance's id. These tests pin the
    third clear, in the per-word exception handler
    (wh-pending-utterance-end-stale-slot).

    Why the stale send did harm: ui_action_handler.end_utterance flushes
    the live letter buffer before it calls the manager, and resets the live
    retraction tracking after that call returns. The id guard sits inside
    utterance_clipboard_manager._end_utterance_locked and refuses by
    returning, so neither effect is inside the guard and both landed on the
    live utterance."""

    @staticmethod
    def _stale_ends(outputs, stale_id):
        return [
            i for i, out in enumerate(outputs)
            if out.action == "end_utterance"
            and out.params.get("utterance_id") == stale_id
        ]

    @staticmethod
    async def _defer_a_buffer(running_harness):
        """Leave the processor mid-buffer so the end marker takes the
        mode != IDLE branch, which is the only place the slot is set."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("hello", delay_before_ms=50)
        return running_harness._utterance_counter

    @pytest.mark.asyncio
    async def test_a_raise_in_decide_timeout_leaves_no_stale_end(
        self, running_harness
    ):
        """decide_timeout runs one line after the slot is set. The
        catalog-reload race is the documented candidate for its raise."""
        stale_id = await self._defer_a_buffer(running_harness)
        processor = running_harness.processor
        original = processor.router.decide_timeout
        raised = []

        def boom(*args, **kwargs):
            raised.append(True)
            raise RuntimeError("catalog reload raced the finalization (test)")

        processor.router.decide_timeout = boom
        try:
            await running_harness.send_utterance_end_marker(stale_id)
            await running_harness.wait_for_timeout(100)
        finally:
            processor.router.decide_timeout = original

        assert raised, "the finalization never raised"
        assert processor._pending_utterance_end is None

        await running_harness.send_word("world", start_of_utterance=True)
        await running_harness.wait_for_timeout(100)
        assert running_harness._utterance_counter != stale_id
        outputs = running_harness.get_outputs()
        assert self._stale_ends(outputs, stale_id) == []

    @pytest.mark.asyncio
    async def test_a_raise_in_execute_decision_leaves_no_stale_end(
        self, running_harness
    ):
        """_execute_decision runs inside a try/finally that resets
        _marker_finalization_active but not this slot, so a transport
        failure inside it lands in the per-word handler with the slot
        still set."""
        stale_id = await self._defer_a_buffer(running_harness)
        processor = running_harness.processor
        original = processor._execute_decision
        raised = []

        async def boom(decision, word_event=None):
            raised.append(True)
            raise RuntimeError("finalization IPC failed (test)")

        processor._execute_decision = boom
        try:
            await running_harness.send_utterance_end_marker(stale_id)
            await running_harness.wait_for_timeout(100)
        finally:
            processor._execute_decision = original

        assert raised, "the finalization never raised"
        assert processor._pending_utterance_end is None

        await running_harness.send_word("world", start_of_utterance=True)
        await running_harness.wait_for_timeout(100)
        assert running_harness._utterance_counter != stale_id
        outputs = running_harness.get_outputs()
        assert self._stale_ends(outputs, stale_id) == []

    @pytest.mark.asyncio
    async def test_no_stale_end_lands_between_the_words_of_a_replay(
        self, running_harness
    ):
        """A retraction in the next utterance replays its words one at a
        time. A stale end landing between them closes an utterance that is
        still speaking."""
        stale_id = await self._defer_a_buffer(running_harness)
        processor = running_harness.processor
        original = processor.router.decide_timeout
        raised = []

        def boom(*args, **kwargs):
            raised.append(True)
            raise RuntimeError("catalog reload raced the finalization (test)")

        processor.router.decide_timeout = boom
        try:
            await running_harness.send_utterance_end_marker(stale_id)
            await running_harness.wait_for_timeout(100)
        finally:
            processor.router.decide_timeout = original
        assert raised, "the finalization never raised"

        # The pipeline harness's MockApp answers every send_request
        # with True, and _handle_retraction reads response.get. Give
        # the retract its real answer shape so the replay actually
        # runs; the capture stays, so outputs are unaffected.
        original_request = running_harness.mock_app.send_request

        async def answered_retract(action, params):
            await original_request(action, params)
            if action == 'retract':
                return {'status': 'retracted'}
            return {'status': 'ok'}

        running_harness.mock_app.send_request = answered_retract

        # Under the mutant that drops the handler's clear, utterance 2
        # reaches the retraction with the stale slot STILL set, and that
        # is the state this test measures. Its words therefore have to
        # buffer rather than dictate: every dictation ends in
        # _send_pending_utterance_end, which would fire the stale end
        # BEFORE the replay and leave the replay region trivially clean.
        # 'backspace' plus a word is the impossible buffer
        # TestImpossibleBufferDefers uses.
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("hello", delay_before_ms=50)
        before_replay = len(running_harness.get_outputs())
        assert self._stale_ends(
            running_harness.get_outputs(), stale_id
        ) == [], (
            'utterance 2 dictated before the replay, so the replay '
            'region is no longer the window this test measures'
        )
        await running_harness.send_word(
            "",
            is_retraction_marker=True,
            retraction_full_text="hello world",
            utterance_id=running_harness._utterance_counter,
        )
        await running_harness.wait_for_timeout(150)

        outputs = running_harness.get_outputs()
        replay_region = outputs[before_replay:]
        assert [
            out for out in replay_region
            if out.action == "intelligent_insert_text"
        ], "the retraction never replayed any words"
        assert self._stale_ends(replay_region, stale_id) == []
        assert self._stale_ends(outputs, stale_id) == []


# ============================================================================
# A SPOKEN COUNT WAITS FOR THE WHOLE NUMBER
# (wh-whole-utterance-command-matching.4)
# ============================================================================


def _presses_of(outputs, key: str):
    """Every press action in ``outputs`` that pressed ``key``."""
    return [
        out for out in outputs
        if out.action == "press_key_action" and out.params.get("key") == key
    ]


class TestASpokenCountWaitsForTheWholeNumber:
    """'backspace twenty three' pressed backspace 20 times and typed
    'three'.

    The count capture is not digits-only: speech/pattern_transform.py
    widens patterns.toml:475 from speech/number_word_parser.py's
    vocabulary, so the regex captures 'twenty three' as one group and
    parses it to 23. The defect is timing. router.py:534 keeps buffering
    only while the count is UNFILLED, and 'twenty' fills it, so the
    router executed with repeat 20 and 'three' arrived as fresh
    dictation. Delete escapes this only through whole_utterance_only
    (patterns.toml:467), which router.py:775 also uses to refuse a
    prefix -- so backspace cannot use that flag without losing
    'backspace hello' (C5).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("words", [
        pytest.param(["backspace", "twenty", "three"], id="one-word-backspace"),
        pytest.param(["back", "space", "twenty", "three"], id="two-word-back-space"),
        pytest.param(["backspace", "23"], id="digits"),
    ])
    async def test_the_whole_count_reaches_one_press(
        self, running_harness, words
    ):
        """C1. Each spelling presses backspace 23 times and dictates
        nothing."""
        await running_harness.send_word(words[0], start_of_utterance=True)
        for word in words[1:]:
            await running_harness.send_word(word, delay_before_ms=50)
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(600)

        presses = _presses_of(running_harness.get_outputs(), "backspace")
        assert len(presses) == 1, (
            f"expected exactly one backspace press, got {presses}"
        )
        assert presses[0].params.get("repeat") == 23
        assert running_harness.get_dictation_texts() == []

    @pytest.mark.asyncio
    async def test_an_incomplete_count_fires_nothing_yet(
        self, running_harness
    ):
        """C3. 'backspace twenty' with no end marker and no timeout does
        nothing at all -- neither the press nor any dictation."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("twenty", delay_before_ms=50)
        await asyncio.sleep(0.15)

        assert _presses_of(running_harness.get_outputs(), "backspace") == []
        assert running_harness.get_dictation_texts() == []

    @pytest.mark.asyncio
    async def test_the_command_timeout_finalizes_a_count_that_never_grows(
        self, running_harness
    ):
        """C4. 'backspace twenty' then silence presses 20 times once the
        command timeout fires. The harness runs with
        command_timeout_ms=1000, so 1400ms is past it."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("twenty", delay_before_ms=50)
        await running_harness.wait_for_timeout(1400)

        presses = _presses_of(running_harness.get_outputs(), "backspace")
        assert len(presses) == 1, (
            f"expected exactly one backspace press, got {presses}"
        )
        assert presses[0].params.get("repeat") == 20
        assert running_harness.get_dictation_texts() == []

    @pytest.mark.asyncio
    async def test_a_count_the_utterance_ends_on_presses_that_many(
        self, running_harness
    ):
        """C2. 'backspace twenty' with the end marker right after
        'twenty' presses 20 times."""
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("twenty", delay_before_ms=50)
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(600)

        presses = _presses_of(running_harness.get_outputs(), "backspace")
        assert len(presses) == 1, (
            f"expected exactly one backspace press, got {presses}"
        )
        assert presses[0].params.get("repeat") == 20
        assert running_harness.get_dictation_texts() == []

    @pytest.mark.asyncio
    async def test_delete_keeps_its_whole_count(self, running_harness):
        """C7. 'delete twenty three' already worked through
        whole_utterance_only; it must keep working."""
        for index, word in enumerate(["delete", "twenty", "three"]):
            await running_harness.send_word(
                word,
                start_of_utterance=(index == 0),
                delay_before_ms=0 if index == 0 else 50,
            )
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(600)

        presses = _presses_of(running_harness.get_outputs(), "del")
        assert len(presses) == 1, (
            f"expected exactly one del press, got {presses}"
        )
        assert presses[0].params.get("repeat") == 23
        assert running_harness.get_dictation_texts() == []

    @pytest.mark.asyncio
    async def test_a_word_that_cannot_extend_the_count_still_presses_and_dictates(
        self, running_harness
    ):
        """C5. 'backspace three hello' still presses three times and
        dictates 'hello'.

        The count wait must not swallow ordinary dictation after a
        command. 'hello' cannot extend 'three', so the prefix loop in
        _resolve_finalization executes the command and dictates the rest,
        exactly as it did before the count could wait at all.

        This test says nothing about WHEN that happens: it sends the end
        marker before it looks. The sibling below is the one that pins
        the timing, and the timing is not what the name of this test
        used to claim (wh-whole-utterance-command-matching.4.1.3).
        """
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("three", delay_before_ms=50)
        await running_harness.send_word("hello", delay_before_ms=50)
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(600)

        presses = _presses_of(running_harness.get_outputs(), "backspace")
        assert len(presses) == 1, (
            f"expected exactly one backspace press, got {presses}"
        )
        assert presses[0].params.get("repeat") == 3
        assert running_harness.get_dictation_texts() == ["hello"]

    @pytest.mark.asyncio
    async def test_a_disproving_word_does_not_press_where_it_arrives(
        self, running_harness
    ):
        """'backspace three hello' presses nothing at 'hello'.

        The word that cannot extend the count does not release the
        command where it arrives. It makes the buffer impossible, and
        step 3 of _decide_buffering defers an impossible buffer to the
        utterance end (wh-whole-utterance-command-matching.3), so the
        press waits for the end marker or the command timeout.

        This is not a defect the router can remove
        (wh-whole-utterance-command-matching.4.1.3). 'question' reaches
        that same branch at the same moment as 'hello', so pressing at
        'hello' would also press at 'question' and split 'question
        mark'. The test exists because the sibling above sends the end
        marker before it looks, so it cannot tell which event released
        the wait, and its name once claimed the wrong one.
        """
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("three", delay_before_ms=50)
        await running_harness.send_word("hello", delay_before_ms=50)
        await running_harness.wait_for_timeout(400)

        assert _presses_of(running_harness.get_outputs(), "backspace") == [], (
            "the command pressed at 'hello'; the impossible-buffer "
            "deferral is what keeps 'question mark' whole"
        )
        assert running_harness.get_dictation_texts() == []

        await running_harness.wait_for_timeout(1400)

        presses = _presses_of(running_harness.get_outputs(), "backspace")
        assert len(presses) == 1, (
            f"expected exactly one backspace press, got {presses}"
        )
        assert presses[0].params.get("repeat") == 3
        assert running_harness.get_dictation_texts() == ["hello"]

    @pytest.mark.asyncio
    async def test_a_command_still_ends_an_utterance_that_holds_punctuation(
        self, running_harness
    ):
        """'backspace three question mark' presses three times and types
        '?'.

        The companion of the test above: it is the sequence that the
        impossible-buffer deferral protects, and it is why the wait
        cannot end at the disproving word
        (wh-whole-utterance-command-matching.4.1.3). 'question' reaches
        the deferral exactly as 'hello' does, so a router that pressed
        at 'hello' would press here too and dictate 'question', leaving
        'mark' alone and losing the '?'.
        """
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("three", delay_before_ms=50)
        await running_harness.send_word("question", delay_before_ms=50)
        await running_harness.send_word("mark", delay_before_ms=50)
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(600)

        presses = _presses_of(running_harness.get_outputs(), "backspace")
        assert len(presses) == 1, (
            f"expected exactly one backspace press, got {presses}"
        )
        assert presses[0].params.get("repeat") == 3
        assert running_harness.get_dictation_texts() == ["?"]

    @pytest.mark.asyncio
    async def test_an_extendable_count_waits_for_the_end_of_the_utterance(
        self, running_harness
    ):
        """'backspace three' alone presses three times, and not before the
        end marker.

        'three' is a complete count and a prefix of 'three hundred', so it
        now waits the way 'delete three' already did. This pins the
        user-visible timing change the fix makes: the outcome is the same
        three presses, but they arrive at the end of the utterance rather
        than the instant 'three' is heard.
        """
        await running_harness.send_word("backspace", start_of_utterance=True)
        await running_harness.send_word("three", delay_before_ms=50)
        await asyncio.sleep(0.15)

        assert _presses_of(running_harness.get_outputs(), "backspace") == [], (
            "the press fired before the utterance ended"
        )

        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(600)

        presses = _presses_of(running_harness.get_outputs(), "backspace")
        assert len(presses) == 1, (
            f"expected exactly one backspace press, got {presses}"
        )
        assert presses[0].params.get("repeat") == 3
        assert running_harness.get_dictation_texts() == []

    @pytest.mark.asyncio
    async def test_a_homophone_count_waits_the_same_way(self, running_harness):
        """The STT homophone "to" for "two" must be able to grow as well.

        speech/actions.py:words_to_int parses this count with
        aliases=True, so "back space to hundred" is 200. The wait has to
        ask the same question with the same options, or a count the
        command engine WOULD accept fires at "to" and dictates "hundred".

        The press repeat is 50, not 200: speech/actions.py:789 clamps a
        key repeat at 50 after the conversion. That clamp is existing
        behaviour and is not what this test is about -- what it pins is
        that the whole phrase reached one press and nothing dictated.
        """
        for index, word in enumerate(["back", "space", "to", "hundred"]):
            await running_harness.send_word(
                word,
                start_of_utterance=(index == 0),
                delay_before_ms=0 if index == 0 else 50,
            )
        await running_harness.send_utterance_end_marker(
            running_harness._utterance_counter
        )
        await running_harness.wait_for_timeout(600)

        presses = _presses_of(running_harness.get_outputs(), "backspace")
        assert len(presses) == 1, (
            f"expected exactly one backspace press, got {presses}"
        )
        assert presses[0].params.get("repeat") == 50
        assert running_harness.get_dictation_texts() == []


# ============================================================================
# A COUNT THE PATTERN ITSELF ALREADY BOUNDS FIRES AT ONCE
# (wh-whole-utterance-command-matching.4.1.1)
# ============================================================================

_BOUNDED_COUNT_PATTERNS = """
COMMAND_HOTWORD = "x-ray"

[[pattern]]
pattern = '''^tab (\\d+) times$'''
doc_id = "tab_times"
actions = [ { function = "press", params = ["tab", "g1"] } ]

[[pattern]]
pattern = '''^back ?space\\s*(\\d+)?$'''
doc_id = "backspace"
actions = [ { function = "press", params = ["backspace", "g1"] } ]
"""


@pytest.fixture(scope="module")
def bounded_count_router(tmp_path_factory):
    """A router whose catalog holds a count with a literal word after it.

    The shipped catalog cannot exercise this shape. Of its 318 patterns,
    113 carry a validation_group; 112 of those set whole_utterance_only,
    and the step-2 whole-utterance branch returns before the count check
    ever runs, so exactly one shipped pattern reaches that check --
    backspace, whose count ends the match. The Advanced pattern editor
    lets a user write '^tab (\\d+) times$' and stores no
    whole_utterance_only flag for a new entry, so a user pattern does
    reach the check with a literal after the count. This catalog is
    written here to stand in for that user pattern.
    """
    path = tmp_path_factory.mktemp("bounded_count") / "patterns.toml"
    path.write_bytes(_BOUNDED_COUNT_PATTERNS.encode("utf-8"))
    return SpeechRouter(PatternCatalog(str(path)))


def _decisions_word_by_word(router, words):
    """Feed ``words`` to the router one at a time; return each Decision."""
    decisions = []
    buffer = []
    for index, word in enumerate(words):
        event = WordEvent(
            word=word,
            start_of_utterance=(index == 0),
            end_of_utterance=False,
            utterance_id=1,
        )
        decisions.append(
            router._decide_buffering(
                word_event=event,
                mode=ProcessingMode.COMMAND_BUFFERING,
                buffer=list(buffer),
                hotword_active=False,
                command_timeout_ms=1000,
                replacement_timeout_ms=400,
                utterance_words=buffer + [word],
            )
        )
        buffer.append(word)
    return decisions


class TestACountThePatternAlreadyBoundsFiresAtOnce:
    """A count can only grow while it sits at the end of what matched.

    The count wait added for wh-whole-utterance-command-matching.4 asked
    only whether another number word could extend the captured phrase.
    It never asked whether the MATCH left room for one. When the pattern
    puts literal text after the capture, the word after the count is
    already spoken by the time the pattern matches, so the count is
    final and the wait can protect nothing -- it only delays the command
    to the end marker or the command timeout
    (wh-whole-utterance-command-matching.4.1.1).
    """

    def test_a_count_followed_by_literal_text_executes_at_once(
        self, bounded_count_router
    ):
        """'tab twenty times' presses at 'times', not at the end.

        'twenty' is extendable on its own, so the growth check alone
        holds the buffer here. The literal 'times' has already been
        spoken, so nothing can extend the count any more.
        """
        decisions = _decisions_word_by_word(
            bounded_count_router, ["tab", "twenty", "times"]
        )
        assert decisions[-1].action is Action.EXECUTE, (
            "the command waited although 'times' already bounded the "
            f"count; router said {decisions[-1].action} "
            f"({decisions[-1].reason})"
        )

    def test_the_bounded_count_still_carries_the_whole_number(
        self, bounded_count_router
    ):
        """'tab twenty three times' executes with 23, not 20.

        The buffer keeps growing while the pattern does not match, so
        the two-word count still arrives whole. This is the guard that
        the fix did not buy its speed by cutting the number short.
        """
        decisions = _decisions_word_by_word(
            bounded_count_router, ["tab", "twenty", "three", "times"]
        )
        assert decisions[-1].action is Action.EXECUTE
        result = bounded_count_router.matcher.match_for_routing(
            ["tab", "twenty", "three", "times"], "command", False
        )
        assert result is not None and result.match_object is not None
        assert result.match_object.group(1) == "twenty three"

    def test_a_count_that_ends_the_match_still_waits(
        self, bounded_count_router
    ):
        """'back space twenty' keeps buffering.

        The regression guard for the fix above: backspace is the one
        shipped pattern whose count ends the match, and it is the whole
        reason the wait exists. Narrowing the wait must not reach it.
        """
        decisions = _decisions_word_by_word(
            bounded_count_router, ["back", "space", "twenty"]
        )
        assert decisions[-1].action is Action.BUFFER
        assert "could still grow" in decisions[-1].reason
