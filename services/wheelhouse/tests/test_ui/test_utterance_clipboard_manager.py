"""Tests for UtteranceClipboardManager - clipboard save/restore for utterances.

Covers:
- Lifecycle (start_utterance saves clipboard text, end_utterance schedules
  PendingRestore; deferred restore fires on timer or manual hook)
- Utterance ID validation (mismatched IDs ignored, overlapping forces cleanup)
- Skip restore flag (for copy/cut commands)
- Text accumulation (tracks text inserted during utterance)
- Safety timeout (timer created on start, cancelled on end)
- Not in utterance (end_utterance when not in utterance is noop)
- Adversarial: rapid start/end, timeout edge cases

wh-d0lr1: end_utterance schedules a deferred PendingRestore instead of
restoring synchronously. Tests use ``fire_pending_restore_now()`` to
simulate the timer firing on demand.

Clipboard save/restore uses pyperclip directly (not win32clipboard) to avoid
native heap corruption from SetClipboardData.
"""
import threading
import pytest
from unittest.mock import MagicMock, patch

_MOD = "ui.utterance_clipboard_manager"


def _make_manager(timeout_seconds=1.0):
    """Create an UtteranceClipboardManager."""
    from ui.utterance_clipboard_manager import UtteranceClipboardManager
    return UtteranceClipboardManager(timeout_seconds=timeout_seconds)


@pytest.fixture
def mock_deps():
    """Mock pyperclip, threading, and clipboard_sequence for utterance tests.

    threading.Timer returns a MagicMock so the deferred-restore timer never
    actually fires under its own clock; tests drive the timer manually via
    ``fire_pending_restore_now``. clipboard_sequence.get_sequence_number is
    mocked to a fixed value so the ownership check passes by default.
    Tests that need to simulate an external clipboard write override the
    return value or side_effect.

    Note: ``threading.Lock`` is patched too, so a real lock is substituted
    in via ``Lock`` so context-manager semantics still work. Without this,
    every ``with self._lock:`` would acquire a MagicMock and the test
    would hang or behave unpredictably.
    """
    real_lock = threading.Lock
    with patch(f"{_MOD}.threading") as mock_threading, \
         patch(f"{_MOD}.pyperclip") as mock_pp, \
         patch(f"{_MOD}.clipboard_sequence") as mock_seq:
        mock_threading.Timer.return_value = MagicMock()
        mock_threading.current_thread.return_value = MagicMock()
        mock_threading.main_thread.return_value = MagicMock()
        mock_threading.Lock = real_lock  # real lock for `with self._lock:` to work
        mock_pp.paste.return_value = "original clipboard"
        mock_pp.copy = MagicMock()
        mock_seq.get_sequence_number.return_value = 1
        yield mock_pp, mock_threading, mock_seq


# ===========================================================================
# Initial State
# ===========================================================================

class TestInitialState:

    def test_not_in_utterance_on_creation(self):
        mgr = _make_manager()
        assert mgr.is_in_utterance() is False

    def test_no_utterance_id_on_creation(self):
        mgr = _make_manager()
        assert mgr._utterance_id is None

    def test_saved_text_none_on_creation(self):
        mgr = _make_manager()
        assert mgr._saved_text is None

    def test_no_timeout_task_on_creation(self):
        mgr = _make_manager()
        assert mgr._timeout_task is None

    def test_custom_timeout(self):
        mgr = _make_manager(timeout_seconds=5.0)
        assert mgr.timeout_seconds == 5.0

    def test_skip_restore_false_on_creation(self):
        mgr = _make_manager()
        assert mgr._skip_restore is False

    def test_empty_accumulated_text_on_creation(self):
        mgr = _make_manager()
        assert mgr._accumulated_text == ""

    def test_clipboard_dirty_false_on_creation(self):
        mgr = _make_manager()
        assert mgr.is_clipboard_dirty() is False


# ===========================================================================
# Clipboard Dirty Tracking (wh-4z4g9)
# ===========================================================================

class TestClipboardDirtyTracking:
    """The dirty flag is set only via mark_clipboard_dirty(), not by
    start_utterance, accumulate_text, or any other lifecycle call.
    """

    def test_mark_clipboard_dirty_sets_flag(self):
        mgr = _make_manager()
        mgr.mark_clipboard_dirty()
        assert mgr.is_clipboard_dirty() is True

    def test_start_utterance_resets_dirty_flag(self, mock_deps):
        mgr = _make_manager()
        mgr.mark_clipboard_dirty()
        mgr.start_utterance(100)
        assert mgr.is_clipboard_dirty() is False

    def test_end_utterance_resets_dirty_flag(self, mock_deps):
        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.mark_clipboard_dirty()
        mgr.end_utterance(100)
        assert mgr.is_clipboard_dirty() is False

    def test_accumulate_text_does_not_set_dirty(self, mock_deps):
        """Tracking accumulated text is independent of clipboard writes."""
        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.accumulate_text("hello")
        assert mgr.is_clipboard_dirty() is False

    def test_unicode_only_utterance_skips_restore(self, mock_deps):
        """wh-606yk acceptance: a Unicode-only utterance ends without
        restoring the clipboard.

        Simulates the production path where intelligent_insert_text routes
        to VerifiedUnicodeStrategy (which never marks the clipboard dirty),
        and end_utterance is then called. pyperclip.copy must not run.
        """
        mock_pp, _, _ = mock_deps
        mock_pp.paste.return_value = "user clipboard contents"

        mgr = _make_manager()
        mgr.start_utterance(100)
        # Simulate VerifiedUnicodeStrategy execution: shadow buffer and
        # accumulator may advance, but the clipboard is never written.
        mgr.accumulate_text("hello")
        # No mark_clipboard_dirty() call.
        mgr.end_utterance(100)

        mock_pp.copy.assert_not_called()
        assert mgr.is_in_utterance() is False

    def test_clipboard_paste_utterance_restores(self, mock_deps):
        """A clipboard-paste utterance ends with clipboard_dirty=True. The
        restore is deferred (wh-d0lr1); fire the timer manually to verify
        the saved clipboard is then copied back.
        """
        mock_pp, _, _ = mock_deps
        mock_pp.paste.return_value = "user clipboard contents"

        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.mark_clipboard_dirty()  # StandardStrategy clipboard path.
        mgr.end_utterance(100)

        # Deferred -- not fired yet.
        mock_pp.copy.assert_not_called()
        assert mgr._pending_restore is not None

        mgr.fire_pending_restore_now()

        mock_pp.copy.assert_called_once_with("user clipboard contents")
        assert mgr.is_in_utterance() is False
        assert mgr._pending_restore is None

    def test_mixed_utterance_restores(self, mock_deps):
        """An utterance that mixes Unicode and clipboard inserts ends
        with dirty=True (the bead's mixed-utterance acceptance case).
        Restore is deferred; fire the timer manually."""
        mock_pp, _, _ = mock_deps
        mock_pp.paste.return_value = "user clipboard contents"

        mgr = _make_manager()
        mgr.start_utterance(100)
        # First word: Unicode -- no dirty mark.
        mgr.accumulate_text("hello")
        # Second word: clipboard paste -- dirty.
        mgr.mark_clipboard_dirty()
        mgr.accumulate_text("world")
        mgr.end_utterance(100)
        mgr.fire_pending_restore_now()

        mock_pp.copy.assert_called_once_with("user clipboard contents")


# ===========================================================================
# Lifecycle
# ===========================================================================

class TestLifecycle:

    def test_start_utterance_saves_clipboard_text(self, mock_deps):
        mock_pp, _, _ = mock_deps
        mock_pp.paste.return_value = "my clipboard"

        mgr = _make_manager()
        mgr.start_utterance(100)

        assert mgr._saved_text == "my clipboard"
        assert mgr.is_in_utterance() is True
        assert mgr._utterance_id == 100

    def test_end_utterance_restores_clipboard_when_dirty(self, mock_deps):
        """Restore is deferred; fire the timer manually."""
        mock_pp, _, _ = mock_deps
        mock_pp.paste.return_value = "original"

        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.mark_clipboard_dirty()
        mgr.end_utterance(100)
        mgr.fire_pending_restore_now()

        mock_pp.copy.assert_called_with("original")
        assert mgr.is_in_utterance() is False

    def test_end_utterance_skips_restore_when_clean(self, mock_deps):
        mock_pp, _, _ = mock_deps

        mgr = _make_manager()
        mgr.start_utterance(100)
        # _clipboard_dirty remains False (Unicode-only or terminal-only utterance)
        mgr.end_utterance(100)

        mock_pp.copy.assert_not_called()
        assert mgr.is_in_utterance() is False

    def test_end_utterance_resets_state(self, mock_deps):
        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.end_utterance(100)

        assert mgr.is_in_utterance() is False
        assert mgr._utterance_id is None
        assert mgr._saved_text is None


# ===========================================================================
# Utterance ID Validation
# ===========================================================================

class TestUtteranceIdValidation:

    def test_mismatched_id_ignored(self, mock_deps):
        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.end_utterance(999)  # Wrong ID

        assert mgr.is_in_utterance() is True
        assert mgr._utterance_id == 100

    def test_none_utterance_id_matches_any(self, mock_deps):
        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.end_utterance(None)

        assert mgr.is_in_utterance() is False

    def test_overlapping_utterance_forces_cleanup_no_paste(self, mock_deps):
        mock_pp, _, _ = mock_deps

        mgr = _make_manager()
        mgr.start_utterance(100)
        # No paste in first utterance
        mgr.start_utterance(200)

        assert mgr._utterance_id == 200
        assert mgr.is_in_utterance() is True
        # No restore since no paste happened
        mock_pp.copy.assert_not_called()

    def test_overlapping_utterance_chains_dirty_baseline(self, mock_deps):
        """When start(200) fires before end_utterance(100) with the prior
        utterance dirty, the prior is force-ended (which schedules a
        PendingRestore) and then the new utterance chains: it reuses the
        prior saved baseline as its own saved_text instead of capturing
        a fresh pyperclip.paste (which would capture WheelHouse's
        transient dictated text)."""
        mock_pp, _, _ = mock_deps
        mock_pp.paste.return_value = "first clipboard"

        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.mark_clipboard_dirty()
        mgr.start_utterance(200)  # Force-ends 100, then chains.

        assert mgr._utterance_id == 200
        # Chain reused the prior saved baseline; no immediate copy fired
        # because the chain path keeps saved_text alive in the manager.
        mock_pp.copy.assert_not_called()
        assert mgr._saved_text == "first clipboard"
        assert mgr._pending_restore is None  # cancelled by the chain

    def test_correct_id_ends_utterance(self, mock_deps):
        mgr = _make_manager()
        mgr.start_utterance(42)
        mgr.end_utterance(42)

        assert mgr.is_in_utterance() is False


# ===========================================================================
# Skip Restore Flag
# ===========================================================================

class TestSkipRestore:

    def test_skip_flag_prevents_restore(self, mock_deps):
        mock_pp, _, _ = mock_deps

        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.skip_clipboard_restore()
        mgr.end_utterance(100)

        mock_pp.copy.assert_not_called()
        assert mgr.is_in_utterance() is False

    def test_skip_flag_cleared_after_use(self, mock_deps):
        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.skip_clipboard_restore()
        mgr.end_utterance(100)

        assert mgr._skip_restore is False

    def test_clear_skip_flag(self):
        mgr = _make_manager()
        mgr._skip_restore = True
        mgr.clear_skip_flag()
        assert mgr._skip_restore is False

    def test_skip_clipboard_restore_sets_flag(self):
        mgr = _make_manager()
        mgr.skip_clipboard_restore()
        assert mgr._skip_restore is True

    def test_skip_flag_resets_state_fully(self, mock_deps):
        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.skip_clipboard_restore()
        mgr.end_utterance(100)

        assert mgr._saved_text is None
        assert mgr._utterance_id is None
        assert mgr._clipboard_dirty is False


# ===========================================================================
# Text Accumulation
# ===========================================================================

class TestTextAccumulation:

    def test_accumulate_first_word(self):
        mgr = _make_manager()
        mgr.accumulate_text("hello")
        assert mgr.get_accumulated_text() == "hello"

    def test_accumulate_multiple_words(self):
        mgr = _make_manager()
        mgr.accumulate_text("hello")
        mgr.accumulate_text("world")
        assert mgr.get_accumulated_text() == "hello world"

    def test_accumulate_many_words(self):
        mgr = _make_manager()
        for w in ["the", "quick", "brown", "fox"]:
            mgr.accumulate_text(w)
        assert mgr.get_accumulated_text() == "the quick brown fox"

    def test_empty_accumulated_text_initially(self):
        mgr = _make_manager()
        assert mgr.get_accumulated_text() == ""

    def test_accumulator_cleared_on_start_utterance(self, mock_deps):
        mgr = _make_manager()
        mgr._accumulated_text = "leftover text"
        mgr.start_utterance(100)
        assert mgr.get_accumulated_text() == ""

    def test_accumulator_cleared_on_end_utterance(self, mock_deps):
        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.accumulate_text("hello")
        mgr.accumulate_text("world")
        mgr.end_utterance(100)
        assert mgr.get_accumulated_text() == ""


# ===========================================================================
# Safety Timeout
# ===========================================================================

class TestSafetyTimeout:

    def test_timer_created_on_start(self, mock_deps):
        _, mock_threading, _ = mock_deps

        mgr = _make_manager(timeout_seconds=2.0)
        mgr.start_utterance(100)

        mock_threading.Timer.assert_called_once()
        args = mock_threading.Timer.call_args[0]
        assert args[0] == 2.0  # timeout seconds

    def test_timer_cancelled_on_end(self, mock_deps):
        _, mock_threading, _ = mock_deps
        timer_mock = MagicMock()
        mock_threading.Timer.return_value = timer_mock

        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.end_utterance(100)

        timer_mock.cancel.assert_called()

    def test_timer_stored_in_timeout_task(self, mock_deps):
        _, mock_threading, _ = mock_deps
        timer_mock = MagicMock()
        mock_threading.Timer.return_value = timer_mock

        mgr = _make_manager()
        mgr.start_utterance(100)

        assert mgr._timeout_task is timer_mock

    def test_timeout_task_cleared_after_end(self, mock_deps):
        _, mock_threading, _ = mock_deps
        mock_threading.Timer.return_value = MagicMock()

        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.end_utterance(100)

        assert mgr._timeout_task is None

    def test_timeout_callback_ends_utterance(self, mock_deps):
        _, mock_threading, _ = mock_deps

        captured_callback = None

        def capture_timer(seconds, callback):
            nonlocal captured_callback
            captured_callback = callback
            return MagicMock()

        mock_threading.Timer.side_effect = capture_timer

        mgr = _make_manager()
        mgr.start_utterance(100)

        assert captured_callback is not None
        captured_callback()
        assert mgr.is_in_utterance() is False

    def test_timeout_callback_noop_if_different_utterance(self, mock_deps):
        _, mock_threading, _ = mock_deps

        captured_callback = None

        def capture_timer(seconds, callback):
            nonlocal captured_callback
            captured_callback = callback
            return MagicMock()

        mock_threading.Timer.side_effect = capture_timer

        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr._utterance_id = 200  # Simulate new utterance

        captured_callback()
        assert mgr.is_in_utterance() is True
        assert mgr._utterance_id == 200


# ===========================================================================
# Not In Utterance
# ===========================================================================

class TestNotInUtterance:

    def test_end_utterance_when_not_in_utterance_is_noop(self):
        mgr = _make_manager()
        mgr.end_utterance(100)
        assert mgr.is_in_utterance() is False

    def test_end_utterance_none_when_not_in_utterance_is_noop(self):
        mgr = _make_manager()
        mgr.end_utterance(None)
        assert mgr.is_in_utterance() is False


# ===========================================================================
# Adversarial
# ===========================================================================

class TestAdversarial:

    def test_rapid_start_end_cycles(self, mock_deps):
        mgr = _make_manager()

        for i in range(10):
            mgr.start_utterance(i)
            mgr.end_utterance(i)

        assert mgr.is_in_utterance() is False
        assert mgr._utterance_id is None
        assert mgr._saved_text is None

    def test_clipboard_dirty_flag_reset_on_start(self, mock_deps):
        mgr = _make_manager()
        mgr.mark_clipboard_dirty()
        mgr.start_utterance(100)
        assert mgr.is_clipboard_dirty() is False

    def test_accumulate_during_utterance_then_end(self, mock_deps):
        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.accumulate_text("hello")
        mgr.accumulate_text("world")

        assert mgr.get_accumulated_text() == "hello world"
        mgr.end_utterance(100)
        assert mgr.get_accumulated_text() == ""

    def test_skip_flag_with_mismatched_id(self, mock_deps):
        mgr = _make_manager()
        mgr.start_utterance(100)
        mgr.skip_clipboard_restore()
        mgr.end_utterance(999)  # Wrong ID

        assert mgr.is_in_utterance() is True
        assert mgr._skip_restore is True

    def test_cancel_timeout_when_no_task(self):
        mgr = _make_manager()
        assert mgr._timeout_task is None
        mgr._cancel_timeout()
        assert mgr._timeout_task is None


# ===========================================================================
# wh-beka1: PendingRestore Lifecycle (10 cases from the bead)
# ===========================================================================

class TestPendingRestoreLifecycle:
    """Comprehensive coverage of the PendingRestore state machine.

    The mock_deps fixture mocks threading entirely, so threading.Timer never
    fires under its own clock. Tests use fire_pending_restore_now() to drive
    the deferred timer deterministically. clipboard_sequence is also mocked
    so tests control the ownership-check seq.
    """

    def test_normal_end_dirty_seq_unchanged_restores_saved_text(self, mock_deps):
        """Case 1: clipboard_dirty=True, deadline elapses, seq unchanged,
        saved_text restored."""
        mock_pp, _, mock_seq = mock_deps
        mock_pp.paste.return_value = "user clipboard"
        mock_seq.get_sequence_number.return_value = 100

        mgr = _make_manager()
        mgr.start_utterance(1)
        mgr.mark_clipboard_dirty(write_seq=100)
        mgr.end_utterance(1)

        # Pending scheduled; copy not yet fired.
        assert mgr._pending_restore is not None
        assert mock_pp.copy.call_count == 0

        # Deadline elapses (simulated).
        mgr.fire_pending_restore_now()

        mock_pp.copy.assert_called_once_with("user clipboard")
        assert mgr._pending_restore is None

    def test_new_utterance_within_deadline_cancels_pending(self, mock_deps):
        """Case 2: New utterance arrives within deadline -> chain cancels
        the pending restore, no copy fires, new utterance reuses the
        prior baseline as its own saved_text.

        Uses a paste side_effect that returns distinct values per call so
        a regression that fresh-saves on the chained start would observe
        a different saved_text and fail the assertion (wh-t8ws.1).
        """
        mock_pp, _, mock_seq = mock_deps
        mock_pp.paste.side_effect = [
            "first user clipboard",   # start_utterance(1)
            "transient utt1 paste",   # would happen on a regression that
                                       # fresh-saves at start_utterance(2)
        ]
        mock_seq.get_sequence_number.return_value = 100

        mgr = _make_manager()
        mgr.start_utterance(1)
        mgr.mark_clipboard_dirty(write_seq=100)
        mgr.end_utterance(1)

        # Pending scheduled.
        first_pending = mgr._pending_restore
        assert first_pending is not None
        assert first_pending.timer is not None

        # New utterance within chain_gap. Time has not advanced because
        # threading is mocked; _last_utterance_end_time captured monotonic
        # when end_utterance ran, and time.monotonic() returns a real value
        # that is at most a few ms later -- well under the 500 ms chain_gap.
        mgr.start_utterance(2)

        # Chain branch fired: timer cancel called, pending cleared, saved
        # baseline reused. (The MagicMock for threading.Timer is shared
        # across the safety-timeout timer and the deferred-restore timer
        # in this fixture, so call_count includes the safety-timeout's
        # own cancel from end_utterance. Just verify the deferred timer
        # cancel happened at least once.)
        assert first_pending.timer.cancel.call_count >= 1
        assert first_pending.cancelled is True
        assert mgr._pending_restore is None
        # The saved_text must be the FIRST baseline, not the transient
        # value from utterance 1's writes. A regression that fresh-saves
        # would observe "transient utt1 paste" instead.
        assert mgr._saved_text == "first user clipboard"
        # No restore copy occurred.
        mock_pp.copy.assert_not_called()
        # paste called exactly once (the original start_utterance(1) save).
        # A fresh-save on the chain would advance to 2 calls.
        assert mock_pp.paste.call_count == 1
        # New utterance is active.
        assert mgr._utterance_id == 2

    def test_safety_timeout_goes_through_pending_restore(self, mock_deps):
        """Case 3: Safety timeout fires when utterance_end never arrives.
        end_utterance is invoked from the timer callback; the deferred
        PendingRestore is then scheduled and runs the same ownership
        check before restoring (wh-t8ws.2).
        """
        mock_pp, mock_threading, mock_seq = mock_deps
        mock_pp.paste.return_value = "user clipboard"

        captured_callback = None

        def capture_safety_timer(seconds, callback):
            nonlocal captured_callback
            # First call is the safety timeout; subsequent calls (from
            # end_utterance scheduling the deferred restore) are ignored
            # for this test.
            if captured_callback is None:
                captured_callback = callback
            return MagicMock()

        mock_threading.Timer.side_effect = capture_safety_timer
        mock_seq.get_sequence_number.return_value = 100

        mgr = _make_manager()
        mgr.start_utterance(1)
        mgr.mark_clipboard_dirty(write_seq=100)

        # Safety timer fires -- simulates timeout_seconds elapsing without
        # a regular utterance_end.
        assert captured_callback is not None
        captured_callback()

        # Utterance state is reset, but a deferred PendingRestore was
        # scheduled (because clipboard_dirty was True at the time of timeout).
        assert mgr._in_utterance is False
        pending = mgr._pending_restore
        assert pending is not None
        # The pending baseline matches the WheelHouse-write seq tracked
        # by mark_clipboard_dirty(write_seq=100), confirming the same
        # ownership check is used as the regular end path.
        assert pending.clipboard_seq_at_paste == 100
        # saved_text is the user's pre-utterance clipboard.
        assert pending.saved_text == "user clipboard"

        # Now drive the deferred restore. Seq is unchanged (no external
        # write), so pyperclip.copy fires with the saved baseline.
        mgr.fire_pending_restore_now()
        mock_pp.copy.assert_called_once_with("user clipboard")
        assert mgr._pending_restore is None

    def test_failed_paste_dirty_flag_set_restore_still_runs(self, mock_deps):
        """Case 4: Strategy reported clipboard_dirty=True even on failed
        paste (because _safe_copy succeeded before the keystroke failed).
        The restore path must run normally so the user's clipboard is
        recovered.
        """
        mock_pp, _, mock_seq = mock_deps
        mock_pp.paste.return_value = "user clipboard"
        mock_seq.get_sequence_number.return_value = 100

        mgr = _make_manager()
        mgr.start_utterance(1)
        # Strategy clipboard write succeeded; paste keystroke failed.
        # The handler still calls mark_clipboard_dirty because the
        # clipboard_dirty bool is True.
        mgr.mark_clipboard_dirty(write_seq=100)
        mgr.end_utterance(1)
        mgr.fire_pending_restore_now()

        mock_pp.copy.assert_called_once_with("user clipboard")

    def test_copy_skip_drops_saved_text_without_pending(self, mock_deps):
        """Case 5: copy/cut command sets skip_restore. PendingRestore is
        NOT scheduled; saved_text is dropped.
        """
        mock_pp, _, mock_seq = mock_deps
        mock_pp.paste.return_value = "user clipboard"
        mock_seq.get_sequence_number.return_value = 100

        mgr = _make_manager()
        mgr.start_utterance(1)
        mgr.mark_clipboard_dirty(write_seq=100)
        mgr.skip_clipboard_restore()
        mgr.end_utterance(1)

        # No pending scheduled; no copy fired.
        assert mgr._pending_restore is None
        mock_pp.copy.assert_not_called()
        assert mgr._saved_text is None

    def test_unicode_only_utterance_no_pending_restore(self, mock_deps):
        """Case 6: Unicode-only utterance does not write the clipboard,
        so clipboard_dirty stays False and no PendingRestore is scheduled.
        """
        mock_pp, _, _ = mock_deps
        mock_pp.paste.return_value = "user clipboard"

        mgr = _make_manager()
        mgr.start_utterance(1)
        mgr.accumulate_text("hello")
        # Unicode strategy never calls mark_clipboard_dirty.
        mgr.end_utterance(1)

        assert mgr._pending_restore is None
        mock_pp.copy.assert_not_called()

    def test_manual_copy_during_unicode_only_preserves_user(self, mock_deps):
        """Case 7: User manually copies during a Unicode-only utterance.
        Simulate the manual copy by changing the clipboard sequence and
        the current paste value mid-utterance. Because mark_clipboard_dirty
        is never called by the Unicode strategy, end_utterance schedules
        nothing and the user's manual copy survives. This is the bug
        Codex flagged in wh-ysdv.3 (dirty flag fixes it as a side effect)
        (wh-t8ws.3).
        """
        mock_pp, _, mock_seq = mock_deps
        # First paste at start_utterance returns the original baseline.
        mock_pp.paste.return_value = "original baseline"
        mock_seq.get_sequence_number.return_value = 50  # before any writes

        mgr = _make_manager()
        mgr.start_utterance(1)
        mgr.accumulate_text("hello")

        # Simulate the user pressing Ctrl+C: the system clipboard is now
        # "manual copy" and the seq advanced. The Unicode strategy still
        # never calls mark_clipboard_dirty, so the manager has no
        # _last_wheelhouse_seq baseline to compare.
        mock_pp.paste.return_value = "manual copy"
        mock_seq.get_sequence_number.return_value = 60

        mgr.end_utterance(1)

        # No PendingRestore scheduled because dirty was never set.
        assert mgr._pending_restore is None
        # No restore ran -- pyperclip.copy was never invoked.
        mock_pp.copy.assert_not_called()
        # The user's manual copy survives untouched -- nothing the manager
        # did would have overwritten the clipboard.

    def test_manual_copy_after_paste_before_deadline_skips_restore(self, mock_deps):
        """Case 8: Manual copy AFTER a clipboard-backed paste but BEFORE
        the deferred fire. clipboard sequence advanced past the WheelHouse
        baseline; the timer's ownership check skips restore so the
        user's manual copy survives. This is the bug Codex flagged in
        wh-a50g.4 (sequence check fixes it).
        """
        mock_pp, _, mock_seq = mock_deps
        mock_pp.paste.return_value = "user clipboard"
        # WheelHouse paste seq.
        mock_seq.get_sequence_number.return_value = 100

        mgr = _make_manager()
        mgr.start_utterance(1)
        mgr.mark_clipboard_dirty(write_seq=100)
        mgr.end_utterance(1)

        assert mgr._pending_restore is not None

        # User copies manually -- seq advances past 100.
        mock_seq.get_sequence_number.return_value = 105

        # Deferred fire: ownership check sees mismatch, skips restore.
        mgr.fire_pending_restore_now()

        mock_pp.copy.assert_not_called()
        assert mgr._pending_restore is None

    def test_chained_back_to_back_utterances_preserve_original_baseline(
        self, mock_deps
    ):
        """Case 9: Three rapid back-to-back clipboard-backed utterances.
        Chain reuses the original pre-first-utterance baseline through
        all three. After the final utterance ends, the original baseline
        is restored exactly once.

        Uses a paste side_effect that returns distinct values per call,
        so a regression that fresh-saves on chained start_utterance(2)
        or start_utterance(3) would observe a different value and the
        final restore assertion would fail (wh-t8ws.1).
        """
        mock_pp, _, mock_seq = mock_deps
        mock_pp.paste.side_effect = [
            "original user clipboard",  # start_utterance(1)
            "transient utt1",            # would happen if chain broke at 2
            "transient utt2",            # would happen if chain broke at 3
        ]
        mock_seq.get_sequence_number.return_value = 100

        mgr = _make_manager()

        # Utterance 1.
        mgr.start_utterance(1)
        assert mgr._saved_text == "original user clipboard"
        mgr.mark_clipboard_dirty(write_seq=100)
        mgr.end_utterance(1)

        # Utterance 2 chains. Update seq to reflect this utterance's writes.
        mgr.start_utterance(2)
        assert mgr._saved_text == "original user clipboard", (
            "Chain should reuse the prior saved_text, not save fresh"
        )
        mock_seq.get_sequence_number.return_value = 110
        mgr.mark_clipboard_dirty(write_seq=110)
        mgr.end_utterance(2)

        # Utterance 3 chains again.
        mgr.start_utterance(3)
        assert mgr._saved_text == "original user clipboard"
        mock_seq.get_sequence_number.return_value = 120
        mgr.mark_clipboard_dirty(write_seq=120)
        mgr.end_utterance(3)

        # Fire the final PendingRestore.
        mgr.fire_pending_restore_now()

        # The original baseline is restored exactly once. The chain
        # preserved the saved_text from utterance 1 across utterances
        # 2 and 3 -- if any chain broke and re-saved, mock_pp.paste
        # would have been called more than once and the restored value
        # would be a "transient" string instead of the original.
        mock_pp.copy.assert_called_once_with("original user clipboard")
        assert mock_pp.paste.call_count == 1

    def test_mixed_unicode_and_clipboard_within_single_utterance(self, mock_deps):
        """Case 10: First word is clipboard-backed (sets dirty=True), second
        word is Unicode-only (does not change dirty). At end_utterance the
        ownership check runs and the deferred restore fires once.
        """
        mock_pp, _, mock_seq = mock_deps
        mock_pp.paste.return_value = "user clipboard"
        mock_seq.get_sequence_number.return_value = 100

        mgr = _make_manager()
        mgr.start_utterance(1)
        # First word: clipboard-backed.
        mgr.mark_clipboard_dirty(write_seq=100)
        mgr.accumulate_text("hello")
        assert mgr.is_clipboard_dirty() is True

        # Second word: Unicode -- dirty stays True, no new write_seq.
        mgr.accumulate_text("world")
        assert mgr.is_clipboard_dirty() is True, (
            "dirty must remain True even when subsequent words are Unicode-only"
        )

        mgr.end_utterance(1)

        assert mgr._pending_restore is not None
        mgr.fire_pending_restore_now()

        # Restore fires exactly once at end_utterance with ownership check.
        mock_pp.copy.assert_called_once_with("user clipboard")

    # -----------------------------------------------------------------
    # wh-t8ws.4: Fail-safe paths when clipboard_sequence raises.
    # -----------------------------------------------------------------

    def test_seq_read_failure_at_end_utterance_skips_scheduling(self, mock_deps):
        """end_utterance reads the clipboard sequence to perform the
        ownership check before scheduling. If get_sequence_number raises,
        the manager fails safe: no PendingRestore is scheduled and no
        restore copy fires. State is reset cleanly.
        """
        mock_pp, _, mock_seq = mock_deps
        mock_pp.paste.return_value = "user clipboard"
        # mark_clipboard_dirty(write_seq=100) does not call get_sequence_number;
        # the first call comes from _end_utterance_locked. Make that one raise.
        mock_seq.get_sequence_number.side_effect = OSError("clipboard locked")

        mgr = _make_manager()
        mgr.start_utterance(1)
        mgr.mark_clipboard_dirty(write_seq=100)
        mgr.end_utterance(1)

        # Fail-safe: no schedule, no restore.
        assert mgr._pending_restore is None
        mock_pp.copy.assert_not_called()
        # Utterance state is fully reset.
        assert mgr._in_utterance is False
        assert mgr._saved_text is None
        assert mgr._utterance_id is None

    def test_seq_read_failure_at_restore_fire_skips_restore(self, mock_deps):
        """The deferred restore decision reads the sequence to confirm
        ownership before pyperclip.copy. If get_sequence_number raises
        at fire time, the restore is skipped and the pending state is
        cleaned up.
        """
        mock_pp, _, mock_seq = mock_deps
        mock_pp.paste.return_value = "user clipboard"
        # First call: end_utterance reads seq, gets 100 -> match -> schedule.
        # Second call: _execute_restore_decision_locked at fire time -> raises.
        mock_seq.get_sequence_number.side_effect = [100, OSError("clipboard locked")]

        mgr = _make_manager()
        mgr.start_utterance(1)
        mgr.mark_clipboard_dirty(write_seq=100)
        mgr.end_utterance(1)

        # Pending was scheduled.
        assert mgr._pending_restore is not None

        # Timer fires; seq read raises; restore skipped.
        mgr.fire_pending_restore_now()

        mock_pp.copy.assert_not_called()
        assert mgr._pending_restore is None
