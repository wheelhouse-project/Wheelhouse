"""Tests for the clipboard restore retry at utterance end.

wh-clipboard-restore-fail-lost. After an utterance, the deferred restore
puts the text the user copied back with one ``pyperclip.copy`` call. When
another program holds the clipboard open for more than pyperclip's own
0.5 s, the call raises "Error calling OpenClipboard", the clipboard keeps
the dictated text, and the text the user copied is lost.

Design under test:

A. The restore timer thread tries the copy again while the exception is
   the OpenClipboard failure, pausing ``_COPY_RETRY_PAUSE`` between
   attempts, until ``COPY_RETRY_SECONDS`` have passed since the first
   attempt: the save retry's window and pause. Any other exception gives
   up at once.
B. The thread holds the manager lock only for one attempt. Before each
   attempt it checks that the pending restore is still current and that
   the clipboard sequence number did not change.
C. A new utterance that starts while the restore still tries does not
   wait for the retry. It stops the retry and keeps the saved text as its
   own baseline, so the text comes back after the new utterance.
D. When every attempt fails, one WARNING names the utterance, the number
   of attempts, and the seconds spent. No ERROR record (an ERROR record
   is a Windows notification).

Only the threaded tests in TestNewUtteranceDuringRetry use real threads.
Nothing here sleeps for real or touches the real clipboard.
"""
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from pyperclip import PyperclipWindowsException

_MOD = "ui.utterance_clipboard_manager"
_LOGGER = "ui.utterance_clipboard_manager"

_OPEN_FAILURE = "Error calling OpenClipboard"


def _make_manager(timeout_seconds=1.0):
    from ui.utterance_clipboard_manager import UtteranceClipboardManager
    return UtteranceClipboardManager(timeout_seconds=timeout_seconds)


class _FakeClock:
    """A clock that only moves when the code under test waits.

    A real failed ``pyperclip.copy`` spends 0.5 s inside its own
    OpenClipboard loop before it raises, so a failed copy advances the
    clock by 0.5 s.
    """

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []
        self.copy_starts = []
        self.copied = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    @property
    def elapsed(self):
        return self.now - 1000.0

    def held_copy(self, *, until=None, message=_OPEN_FAILURE):
        """A pyperclip.copy stand-in for a clipboard held by another program.

        Only the restore calls ``pyperclip.copy`` in the manager. With
        ``until`` the clipboard frees at that elapsed time and the next
        call succeeds. Without it, every call fails.
        """
        def copy(text):
            self.copy_starts.append(self.elapsed)
            if until is not None and self.elapsed >= until:
                self.copied.append(text)
                return None
            self.now += 0.5
            raise PyperclipWindowsException(message)

        return copy


@pytest.fixture
def restore_env():
    """Manager test environment with a fake clock on the retry hooks."""
    clock = _FakeClock()
    real_lock = threading.Lock
    with patch(f"{_MOD}.threading") as mock_threading, \
         patch(f"{_MOD}.pyperclip") as mock_pp, \
         patch(f"{_MOD}.clipboard_sequence") as mock_seq, \
         patch(f"{_MOD}._save_clock", clock.monotonic), \
         patch(f"{_MOD}._save_sleep", clock.sleep):
        mock_threading.Timer.return_value = MagicMock()
        mock_threading.current_thread.return_value = MagicMock()
        mock_threading.main_thread.return_value = MagicMock()
        mock_threading.Lock = real_lock
        mock_pp.paste.return_value = "user text"
        mock_pp.copy = MagicMock()
        mock_seq.get_sequence_number.return_value = 1
        yield mock_pp, mock_seq, clock


def _dictate_one_utterance(mgr, utterance_id=7):
    mgr.start_utterance(utterance_id)
    mgr.mark_clipboard_dirty()
    mgr.end_utterance(utterance_id)
    assert mgr._pending_restore is not None, "no restore was scheduled"


def _records(caplog, level):
    return [
        r for r in caplog.records
        if r.name == _LOGGER and r.levelno == level
    ]


# ===========================================================================
# Acceptance 1 and 3: the restore survives a clipboard hold of up to 3 s
# ===========================================================================

class TestRestoreRetry:

    def test_a_2_second_hold_at_restore_still_restores_the_user_text(
        self, restore_env,
    ):
        mock_pp, _seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr)
        mock_pp.copy.side_effect = clock.held_copy(until=2.0)

        mgr.fire_pending_restore_now()

        assert clock.copied == ["user text"]
        assert mgr._pending_restore is None

    def test_a_hold_that_never_ends_stops_retrying_at_three_seconds(
        self, restore_env,
    ):
        mock_pp, _seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr)
        mock_pp.copy.side_effect = clock.held_copy()

        mgr.fire_pending_restore_now()

        # It retried (a single attempt is the behaviour before the fix) ...
        assert mock_pp.copy.call_count >= 3
        # ... but no attempt started at or after 3.0 s ...
        assert max(clock.copy_starts) < 3.0
        # ... and it did not wait far past the window.
        assert clock.elapsed < 3.6
        assert clock.copied == []
        assert mgr._pending_restore is None

    def test_the_pause_between_attempts_is_the_copy_retry_pause(
        self, restore_env,
    ):
        from ui.clipboard_operations import _COPY_RETRY_PAUSE

        mock_pp, _seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr)
        mock_pp.copy.side_effect = clock.held_copy(until=1.0)

        mgr.fire_pending_restore_now()

        assert clock.sleeps, "no pause between attempts"
        assert all(s <= _COPY_RETRY_PAUSE + 1e-9 for s in clock.sleeps)

    def test_another_error_is_not_retried(self, restore_env, caplog):
        mock_pp, _seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr)
        mock_pp.copy.side_effect = clock.held_copy(
            message="Error calling SetClipboardData",
        )

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            mgr.fire_pending_restore_now()

        assert mock_pp.copy.call_count == 1
        assert clock.sleeps == []
        assert len(_records(caplog, logging.WARNING)) == 1
        assert mgr._pending_restore is None


# ===========================================================================
# Acceptance 2: a clipboard write between two attempts stops the restore
# ===========================================================================

class TestRetrySequenceCheck:

    def test_an_external_write_between_two_attempts_stops_the_restore(
        self, restore_env, caplog,
    ):
        mock_pp, mock_seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr)
        mock_pp.copy.side_effect = clock.held_copy(until=1.0)
        # The user copies new text while the first attempt waits: every
        # sequence read after the first failed attempt sees the new number.
        mock_seq.get_sequence_number.side_effect = (
            lambda: 1 if not clock.copy_starts else 2
        )

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            mgr.fire_pending_restore_now()

        assert mock_pp.copy.call_count == 1, (
            "the restore tried again over the user's new clipboard text"
        )
        assert clock.copied == []
        assert mgr._pending_restore is None
        assert any(
            "NOT restoring" in r.getMessage()
            for r in _records(caplog, logging.INFO)
        )

    def test_a_sequence_read_failure_between_attempts_stops_the_restore(
        self, restore_env,
    ):
        mock_pp, mock_seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr)
        mock_pp.copy.side_effect = clock.held_copy(until=1.0)

        def seq():
            if clock.copy_starts:
                raise OSError("GetClipboardSequenceNumber failed")
            return 1

        mock_seq.get_sequence_number.side_effect = seq

        mgr.fire_pending_restore_now()

        assert mock_pp.copy.call_count == 1
        assert clock.copied == []


# ===========================================================================
# Acceptance 4: the failure log line
# ===========================================================================

class TestRetryFailureLog:

    def test_the_warning_names_the_utterance_the_attempts_and_the_time(
        self, restore_env, caplog,
    ):
        mock_pp, _seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr, utterance_id=7)
        mock_pp.copy.side_effect = clock.held_copy()

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            mgr.fire_pending_restore_now()

        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        attempts = mock_pp.copy.call_count
        assert "[UTT-7]" in message
        assert "Could not restore clipboard" in message
        assert f"after {attempts} attempts" in message
        assert f"in {clock.elapsed:.1f} s" in message
        # An ERROR record is a Windows notification; the fix adds none.
        assert _records(caplog, logging.ERROR) == []


# ===========================================================================
# Acceptance 3: a new utterance while the restore still tries
# ===========================================================================

class _PausedSleep:
    """A sleep stand-in that holds the restore thread in its first pause."""

    def __init__(self):
        self.in_pause = threading.Event()
        self.go = threading.Event()
        self.calls = 0

    def __call__(self, _seconds):
        self.calls += 1
        if self.calls == 1:
            self.in_pause.set()
            self.go.wait(timeout=5.0)


class TestNewUtteranceDuringRetry:

    def _start_restore_in_pause(self, mgr, mock_pp, clock):
        """Run the restore on a thread and hold it in its first pause."""
        paused = _PausedSleep()
        mock_pp.copy.side_effect = clock.held_copy()
        patcher = patch(f"{_MOD}._save_sleep", paused)
        patcher.start()
        thread = threading.Thread(target=mgr.fire_pending_restore_now,
                                  daemon=True)
        thread.start()
        if not paused.in_pause.wait(timeout=5.0):
            paused.go.set()
            thread.join(timeout=5.0)
            patcher.stop()
            pytest.fail("the restore did not try again after a held clipboard")
        return paused, thread, patcher

    def _start_next_utterance(self, mgr, utterance_id):
        """Start an utterance on a thread; return the seconds it took."""
        done = threading.Event()
        started = time.monotonic()
        starter = threading.Thread(
            target=lambda: (mgr.start_utterance(utterance_id), done.set()),
            daemon=True,
        )
        starter.start()
        finished = done.wait(timeout=2.0)
        return finished, time.monotonic() - started

    # Two named tests, not one parametrized test: the mutation gate reads
    # test names without their parameter lists.
    def test_the_next_utterance_inside_the_chain_gap_does_not_wait_and_keeps_the_user_text(
        self, restore_env,
    ):
        self._check_next_utterance(restore_env, gap_passed=False)

    def test_the_next_utterance_after_the_chain_gap_does_not_wait_and_keeps_the_user_text(
        self, restore_env,
    ):
        self._check_next_utterance(restore_env, gap_passed=True)

    def _check_next_utterance(self, restore_env, gap_passed):
        mock_pp, _seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr, utterance_id=7)
        if gap_passed:
            # The retry runs up to 3 s, far past the 0.5 s chain gap.
            mgr._last_utterance_end_time -= 10.0
        # A fresh save now would read the dictated text still on the
        # clipboard, not the text the user copied.
        mock_pp.paste.return_value = "dictated text"

        paused, thread, patcher = self._start_restore_in_pause(
            mgr, mock_pp, clock,
        )
        try:
            finished, seconds = self._start_next_utterance(mgr, 8)
            copies_before_go = mock_pp.copy.call_count
        finally:
            paused.go.set()
            thread.join(timeout=5.0)
            patcher.stop()

        # The next utterance did not wait for the retry.
        assert finished, "start_utterance waited for the restore retry"
        assert seconds < 1.0
        # The user's text is the new utterance's baseline, not lost.
        assert mgr._saved_text == "user text"
        # The retry stopped: no restore attempt after the new utterance
        # started, so no saved text can land between the new utterance's
        # copy and its paste.
        assert not thread.is_alive()
        assert mock_pp.copy.call_count == copies_before_go == 1
        assert clock.copied == []

        # The text comes back after the new utterance.
        mock_pp.copy.side_effect = None
        mock_pp.copy.reset_mock()
        mgr.mark_clipboard_dirty()
        mgr.end_utterance(8)
        mgr.fire_pending_restore_now()
        mock_pp.copy.assert_called_once_with("user text")

    def test_a_next_utterance_that_never_writes_the_clipboard_still_gets_the_user_text_back(
        self, restore_env,
    ):
        """wh-clipboard-restore-fail-lost.1.1: a voice command, a Unicode
        insertion, or an empty utterance never marks the clipboard dirty.
        The clipboard still holds the dictated text of the utterance
        before, so the restore that the chain took over is still owed.
        """
        mock_pp, _seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr, utterance_id=7)
        mgr._last_utterance_end_time -= 10.0
        mock_pp.paste.return_value = "dictated text"

        paused, thread, patcher = self._start_restore_in_pause(
            mgr, mock_pp, clock,
        )
        try:
            finished, _seconds = self._start_next_utterance(mgr, 8)
        finally:
            paused.go.set()
            thread.join(timeout=5.0)
            patcher.stop()
        assert finished

        mock_pp.copy.side_effect = None
        mock_pp.copy.reset_mock()
        mgr.end_utterance(8)
        assert mgr._pending_restore is not None, "the owed restore was dropped"
        mgr.fire_pending_restore_now()
        mock_pp.copy.assert_called_once_with("user text")

    def test_an_external_write_during_the_retry_gives_the_next_utterance_a_fresh_save(
        self, restore_env,
    ):
        mock_pp, mock_seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr, utterance_id=7)
        mock_pp.paste.return_value = "new user text"

        paused, thread, patcher = self._start_restore_in_pause(
            mgr, mock_pp, clock,
        )
        try:
            mock_seq.get_sequence_number.return_value = 2
            finished, _seconds = self._start_next_utterance(mgr, 8)
        finally:
            paused.go.set()
            thread.join(timeout=5.0)
            patcher.stop()

        assert finished
        assert mgr._saved_text == "new user text"
        assert mock_pp.copy.call_count == 1
        assert clock.copied == []


# ===========================================================================
# Acceptance 3: an expired restore that finds the clipboard held
# ===========================================================================

class TestExpiredRestoreHeld:

    def test_the_next_utterance_keeps_the_user_text_as_its_baseline(
        self, restore_env, caplog,
    ):
        """start_utterance runs an expired restore itself (wh-9pye.4).

        One attempt there is the behaviour before the fix. When the
        clipboard is held, the text must not be lost and start_utterance
        must not retry.
        """
        mock_pp, _seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr, utterance_id=7)
        mgr._last_utterance_end_time -= 10.0
        mock_pp.copy.side_effect = clock.held_copy()
        mock_pp.paste.return_value = "dictated text"

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            mgr.start_utterance(8)

        assert mock_pp.copy.call_count == 1
        assert clock.sleeps == []
        assert mgr._saved_text == "user text"
        assert mgr._pending_restore is None
        assert _records(caplog, logging.ERROR) == []

    def test_a_next_utterance_that_never_writes_the_clipboard_still_gets_the_user_text_back(
        self, restore_env,
    ):
        """wh-clipboard-restore-fail-lost.1.1, the expired-restore branch."""
        mock_pp, _seq, clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr, utterance_id=7)
        mgr._last_utterance_end_time -= 10.0
        mock_pp.copy.side_effect = clock.held_copy()
        mock_pp.paste.return_value = "dictated text"
        mgr.start_utterance(8)

        mock_pp.copy.side_effect = None
        mock_pp.copy.reset_mock()
        mgr.end_utterance(8)
        assert mgr._pending_restore is not None, "the owed restore was dropped"
        mgr.fire_pending_restore_now()
        mock_pp.copy.assert_called_once_with("user text")


# ===========================================================================
# wh-clipboard-restore-fail-lost.1.1: a chained utterance that never writes
# the clipboard
# ===========================================================================

class TestChainedUtteranceWithoutClipboardWrite:

    def test_a_chained_utterance_inside_the_chain_gap_still_gets_the_user_text_back(
        self, restore_env,
    ):
        """The 0.5 s chain gap takes the same chain branch as the retry.

        Before this fix the text was lost here too: the timer of UTT-7
        is cancelled, and UTT-8 scheduled no restore.
        """
        mock_pp, _seq, _clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr, utterance_id=7)
        mock_pp.paste.return_value = "dictated text"
        mgr.start_utterance(8)
        assert mgr._saved_text == "user text"

        mgr.end_utterance(8)
        assert mgr._pending_restore is not None, "the owed restore was dropped"
        mgr.fire_pending_restore_now()
        mock_pp.copy.assert_called_once_with("user text")

    def test_an_external_write_during_the_chained_utterance_stops_the_owed_restore(
        self, restore_env,
    ):
        """Acceptance item 2: a copy that the user makes during UTT-8 is
        kept. The owed restore compares the sequence number with the one
        that the chain checked, not with the sequence number at the end.

        This test passes before the fix too, because no restore was
        scheduled then. The mutation gate shows that it fails when the
        owed restore takes the sequence number at the end as its baseline.
        """
        mock_pp, mock_seq, _clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr, utterance_id=7)
        mock_pp.paste.return_value = "dictated text"
        mgr.start_utterance(8)

        mock_seq.get_sequence_number.return_value = 2
        mgr.end_utterance(8)
        mgr.fire_pending_restore_now()
        mock_pp.copy.assert_not_called()

    def test_a_chained_utterance_that_writes_without_a_sequence_number_keeps_the_fallback(
        self, restore_env,
    ):
        """A dirty utterance with no tracked write sequence number keeps
        the fallback to the current sequence number, as before this fix.
        Its own clipboard write moved the sequence number, so a compare
        with the chained sequence number would drop the restore.

        This test passes before the fix too. The mutation gate shows that
        it fails when the chained sequence number replaces the fallback.
        """
        mock_pp, mock_seq, _clock = restore_env
        mgr = _make_manager()
        _dictate_one_utterance(mgr, utterance_id=7)
        mock_pp.paste.return_value = "dictated text"
        mgr.start_utterance(8)

        mgr.mark_clipboard_dirty()
        mock_seq.get_sequence_number.return_value = 2
        mgr.end_utterance(8)
        mgr.fire_pending_restore_now()
        mock_pp.copy.assert_called_once_with("user text")
