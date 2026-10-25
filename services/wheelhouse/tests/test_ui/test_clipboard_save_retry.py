"""Tests for the clipboard save retry at utterance start.

wh-clipboard-save-lost-restore. At utterance start
``UtteranceClipboardManager.start_utterance`` saves the clipboard text with
``pyperclip.paste()``. When another program (ArsClip.exe in load run 2)
holds the clipboard open, pyperclip raises "Error calling OpenClipboard",
the save fails, no restore is scheduled, and the text the user copied is
lost.

Design under test (boss ruling 2026-10-20 20:36, option 1):

A. The fresh-save path retries ``pyperclip.paste()`` while the exception is
   the OpenClipboard failure, pausing ``_COPY_RETRY_PAUSE`` between
   attempts, until ``COPY_RETRY_SECONDS`` have passed since the first
   attempt. Any other exception gives up at once. The module hooks
   ``_save_clock`` and ``_save_sleep`` are patched here with a fake clock.
B. ``last_save_seconds`` holds the seconds the latest start_utterance spent
   on the save (0.0 on the chained path).
C. A save that still fails logs exactly one WARNING with "[UTT-<id>]" and
   "clipboard text was not saved", and schedules no restore.
D. ``ClipboardOperations.charge_next_copy_budget(seconds)`` makes the next
   copy retry budget start with ``seconds`` spent, once.
   ``UIActionHandler.start_utterance`` passes ``last_save_seconds`` to it.

Nothing here sleeps for real or touches the real clipboard.
"""
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest
from pyperclip import PyperclipWindowsException

_MOD = "ui.utterance_clipboard_manager"
_OPS = "ui.clipboard_operations"
_LOGGER = "ui.utterance_clipboard_manager"

_OPEN_FAILURE = "Error calling OpenClipboard"


def _make_manager(timeout_seconds=1.0):
    from ui.utterance_clipboard_manager import UtteranceClipboardManager
    return UtteranceClipboardManager(timeout_seconds=timeout_seconds)


class _FakeClock:
    """A clock that only moves when the code under test waits.

    A real failed ``pyperclip`` call spends 0.5 s inside its own
    OpenClipboard loop before it raises, so ``failing_call`` advances the
    clock by 0.5 s per failed call. Nothing here sleeps for real.
    """

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []
        self.call_starts = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    @property
    def elapsed(self):
        return self.now - 1000.0

    def held_paste(self, *, until=None, result="user text",
                   message=_OPEN_FAILURE):
        """A pyperclip.paste stand-in for a clipboard held by another program.

        With ``until`` the clipboard frees at that elapsed time and the next
        call returns ``result``. Without it, every call fails.
        """
        def paste():
            self.call_starts.append(self.elapsed)
            if until is not None and self.elapsed >= until:
                return result
            self.now += 0.5
            raise PyperclipWindowsException(message)

        return paste

    def held_copy(self, *, message=_OPEN_FAILURE):
        """A pyperclip.copy stand-in that always fails like a held clipboard."""
        self.copy_calls = 0
        self.copy_starts = []

        def copy(_text):
            self.copy_calls += 1
            self.copy_starts.append(self.elapsed)
            self.now += 0.5
            raise PyperclipWindowsException(message)

        return copy


@pytest.fixture
def save_env():
    """Manager test environment with a fake clock on the save hooks.

    ``create=True`` lets the patch succeed before the product code has the
    hooks, so a missing feature shows as a wrong call count or a missing
    restore instead of a fixture error.
    """
    clock = _FakeClock()
    real_lock = threading.Lock
    with patch(f"{_MOD}.threading") as mock_threading, \
         patch(f"{_MOD}.pyperclip") as mock_pp, \
         patch(f"{_MOD}.clipboard_sequence") as mock_seq, \
         patch(f"{_MOD}._save_clock", clock.monotonic, create=True), \
         patch(f"{_MOD}._save_sleep", clock.sleep, create=True):
        mock_threading.Timer.return_value = MagicMock()
        mock_threading.current_thread.return_value = MagicMock()
        mock_threading.main_thread.return_value = MagicMock()
        mock_threading.Lock = real_lock
        mock_pp.copy = MagicMock()
        mock_seq.get_sequence_number.return_value = 1
        yield mock_pp, clock


def _warnings(caplog):
    return [
        r for r in caplog.records
        if r.name == _LOGGER and r.levelno == logging.WARNING
    ]


# ===========================================================================
# Item 3: the save survives a clipboard hold of up to 3 s
# ===========================================================================

class TestSaveRetry:

    def test_a_2_6_second_hold_still_saves_and_restores_the_user_text(
        self, save_env,
    ):
        mock_pp, clock = save_env
        mock_pp.paste.side_effect = clock.held_paste(until=2.6)

        mgr = _make_manager()
        mgr.start_utterance(7)
        mgr.mark_clipboard_dirty()
        mgr.end_utterance(7)
        mgr.fire_pending_restore_now()

        mock_pp.copy.assert_called_with("user text")
        assert 2.6 <= mgr.last_save_seconds < 3.5

    def test_a_hold_that_never_ends_stops_retrying_at_three_seconds(
        self, save_env,
    ):
        mock_pp, clock = save_env
        mock_pp.paste.side_effect = clock.held_paste()

        mgr = _make_manager()
        mgr.start_utterance(7)
        mgr.mark_clipboard_dirty()
        mgr.end_utterance(7)
        mgr.fire_pending_restore_now()

        # It retried (a single attempt is the behaviour before the fix) ...
        assert mock_pp.paste.call_count >= 3
        # ... but no attempt started at or after 3.0 s ...
        assert max(clock.call_starts) < 3.0
        # ... and it did not wait far past the window.
        assert clock.elapsed < 3.6
        # Nothing was saved, so nothing is restored.
        mock_pp.copy.assert_not_called()
        assert mgr._pending_restore is None

    def test_the_pause_between_attempts_is_the_copy_retry_pause(
        self, save_env,
    ):
        from ui.clipboard_operations import _COPY_RETRY_PAUSE

        mock_pp, clock = save_env
        mock_pp.paste.side_effect = clock.held_paste(until=1.0)

        mgr = _make_manager()
        mgr.start_utterance(7)

        assert clock.sleeps, "no pause between attempts"
        assert all(s <= _COPY_RETRY_PAUSE + 1e-9 for s in clock.sleeps)

    def test_another_error_is_not_retried(self, save_env):
        mock_pp, clock = save_env
        mock_pp.paste.side_effect = clock.held_paste(
            message="Error calling GetClipboardData",
        )

        mgr = _make_manager()
        mgr.start_utterance(7)

        assert mock_pp.paste.call_count == 1
        assert clock.sleeps == []
        # A failed save that did not retry spent no more than the one call.
        assert isinstance(mgr.last_save_seconds, float)
        assert mgr.last_save_seconds <= 0.5 + 1e-9

    def test_a_non_windows_exception_is_not_retried(self, save_env):
        mock_pp, clock = save_env
        mock_pp.paste.side_effect = RuntimeError("Error calling OpenClipboard")

        mgr = _make_manager()
        mgr.start_utterance(7)

        assert mock_pp.paste.call_count == 1
        assert clock.sleeps == []
        assert isinstance(mgr.last_save_seconds, float)


# ===========================================================================
# Item 5: a save that still fails is logged once, with the utterance number
# ===========================================================================

class TestSaveFailureWarning:

    def test_a_save_that_fails_after_its_retries_logs_one_warning(
        self, save_env, caplog,
    ):
        mock_pp, clock = save_env
        mock_pp.paste.side_effect = clock.held_paste()

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            mgr = _make_manager()
            mgr.start_utterance(7)
            mgr.mark_clipboard_dirty()
            mgr.end_utterance(7)

        warnings = _warnings(caplog)
        assert len(warnings) == 1, [w.getMessage() for w in warnings]
        message = warnings[0].getMessage()
        assert "[UTT-7]" in message
        assert "clipboard text was not saved" in message

    def test_a_save_that_succeeds_after_a_hold_logs_no_warning(
        self, save_env, caplog,
    ):
        mock_pp, clock = save_env
        mock_pp.paste.side_effect = clock.held_paste(until=1.0)

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            mgr = _make_manager()
            mgr.start_utterance(7)

        assert mgr._saved_text == "user text"
        assert _warnings(caplog) == []


# ===========================================================================
# Item 6: with no hold, the save is unchanged
# ===========================================================================

class TestNoHold:

    def test_no_hold_makes_one_paste_call_and_never_sleeps(self, save_env):
        mock_pp, clock = save_env
        mock_pp.paste.return_value = "user text"

        mgr = _make_manager()
        mgr.start_utterance(7)

        assert mock_pp.paste.call_count == 1
        assert clock.sleeps == []
        assert mgr._saved_text == "user text"
        assert mgr.last_save_seconds == pytest.approx(0.0, abs=0.01)

    def test_a_chained_start_does_not_save_and_reports_zero_seconds(
        self, save_env,
    ):
        mock_pp, clock = save_env
        # The first save waits on a 1.0 s hold, so last_save_seconds is not
        # 0.0 before the chained start. A chained start that kept the old
        # value would then fail the last assertion.
        mock_pp.paste.side_effect = clock.held_paste(until=1.0)

        mgr = _make_manager()
        mgr.start_utterance(1)
        mgr.mark_clipboard_dirty()
        mgr.end_utterance(1)
        saves = mock_pp.paste.call_count
        assert mgr.last_save_seconds >= 1.0

        mgr.start_utterance(2)  # inside the chain gap: chained

        assert mock_pp.paste.call_count == saves
        assert mgr.last_save_seconds == 0.0


# ===========================================================================
# Item D: the copy budget shares the window with the save
# ===========================================================================

@pytest.fixture
def copy_clock():
    clock = _FakeClock()
    with patch(f"{_OPS}._copy_monotonic", clock.monotonic), \
         patch(f"{_OPS}._copy_sleep", clock.sleep):
        yield clock


def _make_ops():
    from ui.clipboard_operations import ClipboardOperations
    return ClipboardOperations({"ui_actions": {"timing": {
        "clipboard_verification_timeout_ms": 250,
        "clipboard_operation_delay_ms": 50,
        "selection_clear_delay_ms": 20,
        "context_gather_delay_ms": 10,
        "post_paste_delay_ms": 30,
    }}})


class TestChargedCopyBudget:

    @patch(f"{_OPS}.pyperclip")
    def test_a_fully_charged_budget_still_makes_one_attempt(
        self, mock_pyperclip, copy_clock,
    ):
        mock_pyperclip.copy.side_effect = copy_clock.held_copy()
        ops = _make_ops()

        ops.charge_next_copy_budget(3.0)
        with ops.copy_retry_budget():
            assert ops._safe_copy("hello") is False

        assert mock_pyperclip.copy.call_count == 1

    @patch(f"{_OPS}.pyperclip")
    def test_a_budget_charged_one_second_retries_for_about_two_more(
        self, mock_pyperclip, copy_clock,
    ):
        mock_pyperclip.copy.side_effect = copy_clock.held_copy()
        ops = _make_ops()

        ops.charge_next_copy_budget(1.0)
        with ops.copy_retry_budget():
            assert ops._safe_copy("hello") is False

        # Retried (a charged budget of 1.0 s is not a spent one) ...
        assert mock_pyperclip.copy.call_count >= 3
        # ... and stopped near 2.0 s, not near 3.0 s.
        assert max(copy_clock.copy_starts) < 2.0
        assert 2.0 <= copy_clock.elapsed < 2.6

    @patch(f"{_OPS}.pyperclip")
    def test_the_charge_applies_to_one_budget_only(
        self, mock_pyperclip, copy_clock,
    ):
        mock_pyperclip.copy.side_effect = copy_clock.held_copy()
        ops = _make_ops()

        ops.charge_next_copy_budget(2.0)
        with ops.copy_retry_budget():
            ops._safe_copy("first")
        first_elapsed = copy_clock.elapsed
        assert first_elapsed < 1.6  # about 1.0 s left, not 3.0 s

        started = copy_clock.elapsed
        with ops.copy_retry_budget():
            assert ops._safe_copy("second") is False
        second_elapsed = copy_clock.elapsed - started

        # The second budget got the full window.
        assert 3.0 <= second_elapsed < 3.6

    @patch(f"{_OPS}.pyperclip")
    def test_an_uncharged_budget_still_gets_the_full_window(
        self, mock_pyperclip, copy_clock,
    ):
        mock_pyperclip.copy.side_effect = copy_clock.held_copy()
        ops = _make_ops()

        with ops.copy_retry_budget():
            assert ops._safe_copy("hello") is False

        assert 3.0 <= copy_clock.elapsed < 3.6


# ===========================================================================
# Item D wiring: the handler passes the save time to the clipboard
# ===========================================================================

class TestHandlerWiring:

    def test_start_utterance_charges_the_copy_budget_with_the_save_time(self):
        from ui.ui_action_handler import UIActionHandler

        handler = MagicMock()
        handler.utterance_manager.last_save_seconds = 0.0

        def manager_start(_utterance_id):
            # The manager measures the save inside its start_utterance.
            handler.utterance_manager.last_save_seconds = 2.6

        handler.utterance_manager.start_utterance.side_effect = manager_start

        UIActionHandler.start_utterance(handler, 7)

        handler.clipboard.charge_next_copy_budget.assert_called_once_with(2.6)

    @patch(f"{_OPS}.pyperclip")
    def test_end_utterance_clears_a_charge_that_no_budget_consumed(
        self, mock_pyperclip, copy_clock,
    ):
        """An utterance that opens no copy budget must not leave its charge.

        start_utterance charges the save time; when the utterance ends
        without a depth-0 copy_retry_budget (terminal editor utterance, or
        zero inserts) the charge would stay and shorten the next, unrelated
        budget.
        """
        from ui.ui_action_handler import UIActionHandler

        mock_pyperclip.copy.side_effect = copy_clock.held_copy()
        handler = MagicMock()
        handler.clipboard = _make_ops()
        handler.utterance_manager.last_save_seconds = 0.0

        def manager_start(_utterance_id):
            handler.utterance_manager.last_save_seconds = 2.6

        handler.utterance_manager.start_utterance.side_effect = manager_start

        UIActionHandler.start_utterance(handler, 7)
        UIActionHandler.end_utterance(handler, 7)

        # No budget was opened during the utterance. A later top-level
        # budget gets the full window, not the 0.4 s the charge would leave.
        started = copy_clock.elapsed
        with handler.clipboard.copy_retry_budget():
            assert handler.clipboard._safe_copy("later") is False
        assert 3.0 <= copy_clock.elapsed - started < 3.6
