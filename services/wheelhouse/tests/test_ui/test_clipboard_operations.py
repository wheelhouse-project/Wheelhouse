"""Tests for ClipboardOperations - clipboard-based UI operations.

Covers:
- Construction and timing config parsing
- Safe clipboard wrappers (_safe_copy, _safe_paste)
- Verified paste (copy, verify loop, focus restore, paste dispatch)
- Selection clearing (sentinel detection, Flutter/standard paths)
- Context gathering (before/after cursor, sentinel-based detection)
- Adversarial: clipboard locked, empty/huge content, rapid cycles
"""
import logging
import pytest
import time
from unittest.mock import MagicMock, patch, call

_MOD = "ui.clipboard_operations"

# wh-review-pattern-fixes.36: verified_press_keys returns
# (success, accepted_events, expected_events). These three tuples name
# the delivery outcomes the tests drive.
_SHORT = (False, 3, 6)      # SendInput accepted only part of the chord
_NOTHING = (False, 0, 6)    # SendInput accepted nothing at all
_FULL = (True, 6, 6)        # whole chord accepted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**timing_overrides):
    """Build a minimal config dict for ClipboardOperations."""
    timing = {
        "clipboard_verification_timeout_ms": 250,
        "clipboard_operation_delay_ms": 50,
        "selection_clear_delay_ms": 20,
        "context_gather_delay_ms": 10,
        "post_paste_delay_ms": 30,
    }
    timing.update(timing_overrides)
    return {"ui_actions": {"timing": timing}}


def _make_ops(**timing_overrides):
    """Create a ClipboardOperations instance with optional timing overrides."""
    from ui.clipboard_operations import ClipboardOperations
    return ClipboardOperations(_make_config(**timing_overrides))


# ===========================================================================
# Construction
# ===========================================================================

class TestConstruction:
    """ClipboardOperations.__init__ timing config parsing."""

    def test_default_timing_values(self):
        """All timing values should be converted from ms to seconds."""
        ops = _make_ops()
        assert ops.clipboard_verification_timeout == 0.25
        assert ops.clipboard_operation_delay == 0.05
        assert ops.selection_clear_delay == 0.02
        assert ops.context_gather_delay == 0.01
        assert ops.post_paste_delay == 0.03

    def test_custom_timing_values(self):
        """Custom timing overrides should be applied."""
        ops = _make_ops(
            clipboard_verification_timeout_ms=500,
            post_paste_delay_ms=100,
        )
        assert ops.clipboard_verification_timeout == 0.5
        assert ops.post_paste_delay == 0.1

    def test_missing_timing_section(self):
        """Missing timing section should use defaults from .get()."""
        from ui.clipboard_operations import ClipboardOperations
        ops = ClipboardOperations({})
        # All defaults come from the .get() calls with default values
        assert ops.clipboard_verification_timeout == 0.25
        assert ops.clipboard_operation_delay == 0.05
        assert ops.selection_clear_delay == 0.02
        assert ops.context_gather_delay == 0.01
        assert ops.post_paste_delay == 0.03

    def test_missing_ui_actions_section(self):
        """Missing ui_actions section should use defaults."""
        from ui.clipboard_operations import ClipboardOperations
        ops = ClipboardOperations({"ui_actions": {}})
        assert ops.clipboard_verification_timeout == 0.25


# ===========================================================================
# Safe Clipboard Wrappers
# ===========================================================================

class TestSafeCopy:
    """ClipboardOperations._safe_copy - error-safe clipboard write."""

    @patch(f"{_MOD}.pyperclip")
    def test_successful_copy(self, mock_pyperclip):
        """Successful copy returns True."""
        ops = _make_ops()
        assert ops._safe_copy("hello") is True
        mock_pyperclip.copy.assert_called_once_with("hello")

    @patch(f"{_MOD}.pyperclip")
    def test_copy_failure_returns_false(self, mock_pyperclip):
        """Exception during copy returns False."""
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")
        ops = _make_ops()
        assert ops._safe_copy("hello") is False

    @patch(f"{_MOD}.pyperclip")
    def test_copy_empty_string(self, mock_pyperclip):
        """Copying empty string should succeed."""
        ops = _make_ops()
        assert ops._safe_copy("") is True
        mock_pyperclip.copy.assert_called_once_with("")


class TestSafePaste:
    """ClipboardOperations._safe_paste - error-safe clipboard read."""

    @patch(f"{_MOD}.pyperclip")
    def test_successful_paste(self, mock_pyperclip):
        """Successful paste returns clipboard content."""
        mock_pyperclip.paste.return_value = "hello"
        ops = _make_ops()
        assert ops._safe_paste() == "hello"

    @patch(f"{_MOD}.pyperclip")
    def test_paste_failure_returns_none(self, mock_pyperclip):
        """Exception during paste returns None."""
        mock_pyperclip.paste.side_effect = Exception("clipboard locked")
        ops = _make_ops()
        assert ops._safe_paste() is None

    @patch(f"{_MOD}.pyperclip")
    def test_paste_empty_clipboard(self, mock_pyperclip):
        """Empty clipboard returns empty string."""
        mock_pyperclip.paste.return_value = ""
        ops = _make_ops()
        assert ops._safe_paste() == ""


# ===========================================================================
# Verified Paste
# ===========================================================================

class TestVerifiedPaste:
    """ClipboardOperations.verified_paste - copy, verify, focus, paste."""

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_happy_path_standard_app(self, mock_pyperclip, mock_vpk, mock_time):
        """Verified paste: copy -> verify -> focus -> Ctrl+V."""
        mock_time.perf_counter.side_effect = [
            0.0,   # t_start
            0.001, # t_after_copy
            0.002, # start_time
            0.003, # while check
            0.004, # t_after_verify
            0.005, # t_after_focus
            0.006, # t_after_sendkeys
            0.007, # t_after_sleep
        ]
        mock_time.sleep = MagicMock()  # no-op sleep
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "hello world"

        wm = MagicMock()
        wm.get_target_window.return_value = (12345, MagicMock())

        ops = _make_ops()
        result = ops.verified_paste("hello world", wm)

        assert result is True
        mock_pyperclip.copy.assert_called_once_with("hello world")
        mock_vpk.assert_called_once_with('ctrl', 'v')
        wm.ensure_focused.assert_called_once_with(12345)

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_copy_failure_aborts(self, mock_pyperclip, mock_vpk, mock_time):
        """If _safe_copy fails, paste should abort and return False."""
        mock_time.perf_counter.return_value = 0.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")

        ops = _make_ops()
        result = ops.verified_paste("text", MagicMock())

        assert result is False
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_log_lines_do_not_contain_text_content(
        self, mock_pyperclip, mock_vpk, mock_time, caplog,
    ):
        """verified_paste must not log dictation text content (wh-vbvgf.2.1).

        The retry path passes cached dictation text through verified_paste,
        and the privacy contract says that text must not appear in any log
        line. Both the success and failure log lines previously included
        text[:50]; they now log len(text) instead.
        """
        secret = "this is the secret dictation text the user spoke aloud"

        # Success case: verify the success INFO log does not echo the text.
        mock_time.perf_counter.side_effect = [
            0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007,
        ]
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = secret

        wm = MagicMock()
        wm.get_target_window.return_value = (12345, MagicMock())

        ops = _make_ops()
        with caplog.at_level("DEBUG", logger=_MOD):
            ops.verified_paste(secret, wm)

        for record in caplog.records:
            assert secret not in record.getMessage()
            assert secret not in str(record.args)

        caplog.clear()

        # Failure case: copy raises, error path must not echo the text.
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")
        mock_time.perf_counter.side_effect = [0.0, 0.001]

        with caplog.at_level("DEBUG", logger=_MOD):
            ops.verified_paste(secret, MagicMock())

        for record in caplog.records:
            assert secret not in record.getMessage()
            assert secret not in str(record.args)

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_verification_timeout(self, mock_pyperclip, mock_vpk, mock_time):
        """If clipboard never matches, paste should fail after timeout."""
        # perf_counter: t_start, t_after_copy, start_time, then loop checks
        counter = [0.0]
        def advancing_counter():
            val = counter[0]
            counter[0] += 0.1  # jumps 100ms each call
            return val
        mock_time.perf_counter.side_effect = advancing_counter
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "wrong text"  # never matches

        ops = _make_ops(clipboard_verification_timeout_ms=250)
        result = ops.verified_paste("expected text", MagicMock())

        assert result is False
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_verification_retry_then_success(self, mock_pyperclip, mock_vpk, mock_time):
        """Clipboard verification should poll until content matches."""
        times = iter([
            0.0,   # t_start
            0.001, # t_after_copy
            0.002, # start_time
            0.003, # 1st while check - still in timeout
            0.004, # 2nd while check - still in timeout
            0.005, # 3rd while check - still in timeout
            0.006, # t_after_verify
            0.007, # t_after_focus
            0.008, # t_after_sendkeys
            0.009, # t_after_sleep
        ])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        # First two reads: wrong content; third: correct
        mock_pyperclip.paste.side_effect = ["old text", "still wrong", "target"]

        wm = MagicMock()
        wm.get_target_window.return_value = (1, None)

        ops = _make_ops()
        result = ops.verified_paste("target", wm)

        assert result is True
        assert mock_pyperclip.paste.call_count == 3

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_clipboard_read_failure_during_verify_retries(self, mock_pyperclip, mock_vpk, mock_time):
        """If _safe_paste returns None during verify, it should keep retrying."""
        times = iter([
            0.0,   # t_start
            0.001, # t_after_copy
            0.002, # start_time
            0.003, # 1st while check
            0.004, # 2nd while check
            0.005, # t_after_verify
            0.006, # t_after_focus
            0.007, # t_after_sendkeys
            0.008, # t_after_sleep
        ])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        # First read: exception (None from _safe_paste), second: success
        mock_pyperclip.paste.side_effect = [Exception("locked"), "hello"]

        wm = MagicMock()
        wm.get_target_window.return_value = (1, None)

        ops = _make_ops()
        result = ops.verified_paste("hello", wm)

        assert result is True

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_recovered_clipboard_read_failure_logs_no_error(
        self, mock_pyperclip, mock_vpk, mock_time, caplog,
    ):
        """A recovered clipboard read failure must raise no notification.

        An ERROR record IS a Windows notification in this application:
        ErrorNotificationHandler (utils/error_notifier.py) is a logging
        handler at ERROR level that utils/logging_setup.py attaches to the
        root logger. ``_safe_paste`` has one caller -- the verification
        loop in ``verified_paste`` -- and that caller treats every None it
        returns as retryable, so a read failure that the next poll
        recovers from is not a fault and must not notify. The WARNING
        assertion pins the other half: the locked clipboard still reaches
        the log, at a level the notifier ignores.

        Fixture shape follows
        test_clipboard_read_failure_during_verify_retries above: the same
        perf_counter sequence and the same first-read-raises, second-read-
        succeeds clipboard.
        """
        times = iter([
            0.0,   # t_start
            0.001, # t_after_copy
            0.002, # start_time
            0.003, # 1st while check
            0.004, # 2nd while check
            0.005, # t_after_verify
            0.006, # t_after_focus
            0.007, # t_after_sendkeys
            0.008, # t_after_sleep
        ])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        # First read: exception (None from _safe_paste), second: success
        mock_pyperclip.paste.side_effect = [Exception("locked"), "hello"]

        wm = MagicMock()
        wm.get_target_window.return_value = (1, None)

        ops = _make_ops()
        with caplog.at_level(logging.DEBUG, logger=_MOD):
            result = ops.verified_paste("hello", wm)

        assert result is True
        errors = [
            f"{r.name}: {r.getMessage()}"
            for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert errors == []
        warnings = [
            r.getMessage()
            for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("Clipboard paste/read failed" in m for m in warnings), warnings

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_optimistic_paste_after_lock_failures_logs_no_error(
        self, mock_pyperclip, mock_vpk, mock_time, caplog,
    ):
        """The optimistic Ctrl+V path must raise no notification either.

        Pure lock contention -- copy succeeded, every verification read
        failed, no wrong content seen -- ends the verification loop on its
        timeout and proceeds optimistically. The method sends Ctrl+V and
        returns True, so the user's text lands; the run already logs its
        own WARNING for the optimistic decision. No record on that path
        may be at ERROR level.
        """
        counter = [0.0]

        def advancing_counter():
            val = counter[0]
            counter[0] += 0.1  # jumps 100ms each call
            return val

        mock_time.perf_counter.side_effect = advancing_counter
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        # Every verification read raises: _safe_paste returns None each time.
        mock_pyperclip.paste.side_effect = Exception("locked")

        wm = MagicMock()
        wm.get_target_window.return_value = (1, None)

        ops = _make_ops(clipboard_verification_timeout_ms=250)
        with caplog.at_level(logging.DEBUG, logger=_MOD):
            result = ops.verified_paste("hello", wm)

        assert result is True
        mock_vpk.assert_called_once_with('ctrl', 'v')
        errors = [
            f"{r.name}: {r.getMessage()}"
            for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert errors == []

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_paste_uses_sendkeys(self, mock_pyperclip, mock_vpk, mock_time):
        """Flutter control should use SendKeys instead of the SendInput chord."""
        times = iter([
            0.0, 0.001, 0.002, 0.003,  # setup
            0.004, 0.005, 0.006, 0.007,  # verify/focus/paste/sleep
        ])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "flutter text"

        flutter = MagicMock()
        flutter.Exists.return_value = True

        wm = MagicMock()
        ops = _make_ops()
        result = ops.verified_paste("flutter text", wm, flutter_control=flutter)

        assert result is True
        flutter.SendKeys.assert_called_once_with('{Ctrl}v')
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_skips_focus_restoration(self, mock_pyperclip, mock_vpk, mock_time):
        """Flutter paste should skip window focus restoration."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"

        flutter = MagicMock()
        flutter.Exists.return_value = True
        wm = MagicMock()

        ops = _make_ops()
        ops.verified_paste("text", wm, flutter_control=flutter)

        # Window manager should NOT be called for focus restoration
        wm.get_target_window.assert_not_called()
        wm.ensure_focused.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_focus_restore_no_hwnd(self, mock_pyperclip, mock_vpk, mock_time):
        """If get_target_window returns no hwnd, skip ensure_focused."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"

        wm = MagicMock()
        wm.get_target_window.return_value = (None, None)

        ops = _make_ops()
        result = ops.verified_paste("text", wm)

        assert result is True
        wm.ensure_focused.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_focus_restore_with_target_control(self, mock_pyperclip, mock_vpk, mock_time):
        """Target control should have SetFocus called."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"

        control = MagicMock()
        wm = MagicMock()
        wm.get_target_window.return_value = (123, control)

        ops = _make_ops()
        ops.verified_paste("text", wm)

        control.SetFocus.assert_called_once()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_setfocus_exception_does_not_abort_legacy_target(
        self, mock_pyperclip, mock_vpk, mock_time,
    ):
        """SetFocus failure on the LEGACY path is caught and the paste continues.

        wh-review-pattern-fixes.41 renamed this test and pinned its
        scope. The caller here passes neither target_control nor
        target_hwnd, so verified_paste resolves the target from CURRENT
        focus through get_target_window(None). There is no captured
        target to prove a foreground against, and that path keeps its
        old behavior on purpose. The fail-closed contract for a captured
        target lives in TestVerifiedPastePreSendFocusProof; a SetFocus
        failure there sends no Ctrl+V at all.
        """
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"

        control = MagicMock()
        control.SetFocus.side_effect = Exception("COM error")
        wm = MagicMock()
        wm.get_target_window.return_value = (123, control)

        ops = _make_ops()
        result = ops.verified_paste("text", wm)

        assert result is True  # paste should still succeed
        mock_vpk.assert_called_once_with('ctrl', 'v')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_get_target_window_exception_doesnt_abort(self, mock_pyperclip, mock_vpk, mock_time):
        """get_target_window failure should be caught, paste continues.

        wh-review-pattern-fixes.41: get_target_window only runs on the
        legacy path (no captured target), so this outer-exception
        behavior is unchanged. With a captured target the same outer
        handler now returns False and sends nothing.
        """
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"

        wm = MagicMock()
        wm.get_target_window.side_effect = Exception("window gone")

        ops = _make_ops()
        result = ops.verified_paste("text", wm)

        assert result is True
        mock_vpk.assert_called_once_with('ctrl', 'v')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_exists_false_falls_back_to_press_keys(self, mock_pyperclip, mock_vpk, mock_time):
        """If flutter_control.Exists() is False, fall back to SendInput."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"

        flutter = MagicMock()
        flutter.Exists.return_value = False

        wm = MagicMock()
        # flutter_control present means focus restore is skipped,
        # but Exists=False means the SendInput chord is used for paste
        ops = _make_ops()
        result = ops.verified_paste("text", wm, flutter_control=flutter)

        assert result is True
        mock_vpk.assert_called_once_with('ctrl', 'v')
        flutter.SendKeys.assert_not_called()

    # -- wh-d43oi: paste provenance flags --
    # The retraction/selection-restore policy needs to know two things about
    # the most recent paste: was it optimistic (clipboard unverified due to
    # lock contention, copy already succeeded), and did the ctrl+v keystroke
    # actually fire. The flags are reset at the start of every verified_paste
    # call so a previous paste's state does not leak forward.

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_sent_flag_false_on_copy_failure(self, mock_pyperclip, mock_vpk, mock_time):
        """Copy failure: returns False, last_paste_was_sent stays False."""
        mock_time.perf_counter.return_value = 0.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")
        ops = _make_ops()
        ops.last_paste_was_sent = True  # stale value from a prior paste
        result = ops.verified_paste("text", MagicMock())
        assert result is False
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_sent_flag_false_on_wrong_content_verification_failure(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        """Verification sees wrong content until timeout: returns False,
        last_paste_was_sent stays False."""
        counter = [0.0]
        def advancing_counter():
            val = counter[0]
            counter[0] += 0.1
            return val
        mock_time.perf_counter.side_effect = advancing_counter
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "wrong"
        ops = _make_ops(clipboard_verification_timeout_ms=250)
        ops.last_paste_was_sent = True  # stale value
        result = ops.verified_paste("expected", MagicMock())
        assert result is False
        assert ops.last_paste_was_sent is False
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_sent_flag_true_after_sendinput_success(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        """The chord fires and verification succeeded: last_paste_was_sent True."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        wm = MagicMock()
        wm.get_target_window.return_value = (None, None)
        ops = _make_ops()
        result = ops.verified_paste("text", wm)
        assert result is True
        assert ops.last_paste_was_sent is True

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_sent_flag_true_after_flutter_sendkeys_success(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        """Flutter SendKeys fires and verification succeeded: last_paste_was_sent True.

        Directly verifies the contract that the flag is set BEFORE the
        Flutter-vs-non-Flutter dispatch, not only before the SendInput chord.
        """
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        flutter = MagicMock()
        flutter.Exists.return_value = True
        wm = MagicMock()
        ops = _make_ops()
        result = ops.verified_paste("text", wm, flutter_control=flutter)
        assert result is True
        assert ops.last_paste_was_sent is True
        flutter.SendKeys.assert_called_once_with('{Ctrl}v')
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_sent_flag_reset_at_start_of_every_call(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        """A stale True from an earlier paste must not leak into a copy-fail call."""
        mock_time.perf_counter.return_value = 0.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")
        ops = _make_ops()
        ops.last_paste_was_sent = True
        ops.verified_paste("text", MagicMock())
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_optimistic_flag_true_on_pure_lock_contention(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        """Lock-only contention path sets last_paste_was_optimistic True."""
        counter = [0.0]
        def advancing_counter():
            val = counter[0]
            counter[0] += 0.05
            return val
        mock_time.perf_counter.side_effect = advancing_counter
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.side_effect = Exception("OpenClipboard failed")
        wm = MagicMock()
        wm.get_target_window.return_value = (123, None)
        ops = _make_ops(clipboard_verification_timeout_ms=250)
        result = ops.verified_paste("text", wm)
        assert result is True
        assert ops.last_paste_was_optimistic is True

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_optimistic_flag_false_on_normal_verified_success(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        """Normal verified success clears last_paste_was_optimistic to False."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        wm = MagicMock()
        wm.get_target_window.return_value = (None, None)
        ops = _make_ops()
        ops.last_paste_was_optimistic = True  # stale value
        result = ops.verified_paste("text", wm)
        assert result is True
        assert ops.last_paste_was_optimistic is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_optimistic_flag_reset_at_start_of_every_call(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        """A stale True from a previous optimistic call does not leak into a copy-fail call."""
        mock_time.perf_counter.return_value = 0.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")
        ops = _make_ops()
        ops.last_paste_was_optimistic = True
        ops.verified_paste("text", MagicMock())
        assert ops.last_paste_was_optimistic is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_post_paste_delay_applied(self, mock_pyperclip, mock_vpk, mock_time):
        """Post-paste delay should be called with configured value."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        sleep_calls = []
        mock_time.sleep = lambda s: sleep_calls.append(s)

        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"

        wm = MagicMock()
        wm.get_target_window.return_value = (None, None)

        ops = _make_ops(post_paste_delay_ms=100)
        ops.verified_paste("text", wm)

        # Last sleep should be the post-paste delay (0.1s)
        assert 0.1 in sleep_calls


class TestVerifiedPasteExplicitTarget:
    """wh-59i32: explicit target_control / target_hwnd plumbing.

    The strategies capture the focused control and HWND at strategy entry.
    Forwarding them to verified_paste pins the paste destination so focus
    drift between capture and Ctrl+V can't redirect the dictation. The
    post-paste foreground check catches drift that happens DURING the paste
    (e.g. an alert popped up between SetFocus and the keystroke landing).
    """

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_explicit_target_hwnd_used_for_focus_skips_get_target_window(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm
    ):
        """When target_hwnd is provided, verified_paste must use it directly
        and not call window_manager.get_target_window."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 12345

        control = MagicMock()
        wm = MagicMock()

        ops = _make_ops()
        result = ops.verified_paste(
            "text",
            wm,
            target_control=control,
            target_hwnd=12345,
        )

        assert result is True
        wm.get_target_window.assert_not_called()
        wm.ensure_focused.assert_called_once_with(12345)
        control.SetFocus.assert_called_once()

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_post_paste_check_passes_when_foreground_matches(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm
    ):
        """Foreground HWND matches target_hwnd: counter increments, returns True."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 9999

        wm = MagicMock()
        ops = _make_ops()
        before = ops.accumulated_paste_chars
        result = ops.verified_paste("text", wm, target_hwnd=9999)

        assert result is True
        assert ops.accumulated_paste_chars == before + len("text")

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_post_paste_check_fails_on_focus_drift(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm
    ):
        """Foreground HWND drifted away from target_hwnd between SetFocus and
        Ctrl+V (e.g. an alert grabbed focus): refuse to credit the counter
        and return False so retract gating sees the failure.

        wh-review-pattern-fixes.41: the foreground reads are now
        sequenced. The entry log and the pre-send proof see the captured
        window, and only the post-paste read sees the other window, so
        this test still describes drift DURING the paste rather than
        being answered by the pre-send proof.
        """
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        # Entry log, pre-send proof, then the post-paste read: only the
        # last one sees a DIFFERENT window.
        mock_win32gui.GetForegroundWindow.side_effect = [9999, 9999, 7777]

        wm = MagicMock()
        ops = _make_ops()
        before = ops.accumulated_paste_chars

        result = ops.verified_paste("text", wm, target_hwnd=9999)

        assert result is False
        assert ops.accumulated_paste_chars == before, (
            "Counter must NOT increment when post-paste foreground check "
            "fails -- otherwise a later retract would back-space into the "
            "wrong window."
        )

    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_legacy_caller_skips_post_paste_check(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui
    ):
        """Without target_hwnd there's nothing to compare against, so the
        post-paste check must not run -- and crucially must not flip the
        result to False -- for legacy callers (e.g. AI clipboard ops).
        """
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"

        wm = MagicMock()
        wm.get_target_window.return_value = (None, None)
        ops = _make_ops()
        before = ops.accumulated_paste_chars

        result = ops.verified_paste("text", wm)  # no target_hwnd

        assert result is True
        assert ops.accumulated_paste_chars == before + len("text")
        mock_win32gui.GetForegroundWindow.assert_not_called()

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_explicit_target_overrides_focus_drift_at_paste_time(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm
    ):
        """Strategy captured control A; focus has drifted to control B by
        paste time. Pre-paste resolution must use the captured control,
        not whatever GetFocusedControl would return now (the bug the
        bead exists to fix)."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 100  # matches target

        captured_control = MagicMock(name="control_A")
        captured_top = MagicMock()
        captured_top.NativeWindowHandle = 100
        captured_control.GetTopLevelControl.return_value = captured_top

        wm = MagicMock()
        # If the implementation looked at "current" focus via
        # window_manager.get_target_window(None), it would be redirected
        # to control_B. Make that path observably wrong.
        wm.get_target_window.return_value = (200, MagicMock(name="control_B"))

        ops = _make_ops()
        ops.verified_paste(
            "text",
            wm,
            target_control=captured_control,
            target_hwnd=100,
        )

        wm.get_target_window.assert_not_called()
        wm.ensure_focused.assert_called_once_with(100)
        captured_control.SetFocus.assert_called_once()


class TestPreSendProvenanceRecheck:
    """wh-ensure-focused-same-process-fallback.1.18 (codex round 11).

    Every handler-level provenance probe runs BEFORE the strategy
    re-resolves its paste target and before the clipboard verify loop
    inside this method -- the longest uncovered interval on the retry
    path. When the caller supplies ``expected_provenance``
    (tagged_hwnd, tag), verified_paste re-reads the marker after
    focus restoration, immediately before the Ctrl+V, and refuses on
    mismatch: the marker dies with the window object, so it is the
    one probe a SAME-RUN numeric handle recycle cannot alias
    (cross-run, equal 43-bit salts collide at about 2**-43 per pair
    of runs -- the accepted residual at _RUN_SALT).
    """

    @patch(f"{_MOD}.read_hwnd_provenance", return_value=0)
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_dead_marker_refuses_before_keystroke(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui,
        _mock_norm, _mock_read,
    ):
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 9999

        wm = MagicMock()
        ops = _make_ops()
        result = ops.verified_paste(
            "text", wm, target_hwnd=9999,
            expected_provenance=(9999, 7),
        )

        assert result is False
        mock_vpk.assert_not_called()
        assert ops.last_paste_was_sent is False, (
            "No key reached the target, so the caller's restore path "
            "must stay open."
        )

    @patch(f"{_MOD}.read_hwnd_provenance", return_value=7)
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_live_marker_pastes(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui,
        _mock_norm, mock_read,
    ):
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 9999

        wm = MagicMock()
        ops = _make_ops()
        result = ops.verified_paste(
            "text", wm, target_hwnd=9999,
            expected_provenance=(9999, 7),
        )

        assert result is True
        mock_vpk.assert_called_once()
        mock_read.assert_called_once_with(9999)

    @patch(f"{_MOD}.top_level_hwnd_from_control", return_value=0x1E61)
    @patch(f"{_MOD}.read_hwnd_provenance", return_value=7)
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_reparented_control_after_entry_refuses(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui,
        _mock_norm, _mock_read, _mock_top,
    ):
        # wh-ensure-focused-same-process-fallback.1.20 (codex round
        # 12): the control was reparented into live window B AFTER the
        # strategy resolved target_hwnd (still A) -- the marker on A
        # survives (A stays alive), so a marker-only recheck passes
        # while the keystroke would land wherever the live control now
        # sits. The pre-send binding must re-resolve the control's
        # top-level and require it IS the tagged window.
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 9999

        wm = MagicMock()
        ops = _make_ops()
        result = ops.verified_paste(
            "text", wm,
            target_control=MagicMock(),
            target_hwnd=9999,
            expected_provenance=(9999, 7),
        )

        assert result is False
        mock_vpk.assert_not_called()
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.top_level_hwnd_from_control", return_value=0x1E61)
    @patch(f"{_MOD}.read_hwnd_provenance", return_value=7)
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_reparented_control_before_entry_refuses(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui,
        _mock_norm, _mock_read, _mock_top,
    ):
        # Same reparent, earlier timing: the strategy's own entry-time
        # resolve already saw window B (target_hwnd=0x1E61), while the
        # marker still lives on A (9999). Comparing the ENTRY handle
        # against the tagged handle is not enough either way -- the
        # binding must come from a fresh pre-send resolve, and the
        # tagged window must be the one the paste would enter.
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 0x1E61

        wm = MagicMock()
        ops = _make_ops()
        result = ops.verified_paste(
            "text", wm,
            target_control=MagicMock(),
            target_hwnd=0x1E61,
            expected_provenance=(9999, 7),
        )

        assert result is False
        mock_vpk.assert_not_called()
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.top_level_hwnd_from_control", return_value=None)
    @patch(f"{_MOD}.read_hwnd_provenance", return_value=7)
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_presend_control_unresolvable_refuses(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui,
        _mock_norm, _mock_read, _mock_top,
    ):
        # The pre-send resolve fails (stale COM): with a provenance
        # contract in force there is no proof of where the keystroke
        # would land, so the paste refuses.
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 9999

        wm = MagicMock()
        ops = _make_ops()
        result = ops.verified_paste(
            "text", wm,
            target_control=MagicMock(),
            target_hwnd=9999,
            expected_provenance=(9999, 7),
        )

        assert result is False
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.read_hwnd_provenance", return_value=0)
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_no_expected_provenance_skips_recheck(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui,
        _mock_norm, mock_read,
    ):
        # Ordinary fresh and soft-allow pastes carry no rejection-time
        # marker; the recheck must not run and must not refuse them.
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 9999

        wm = MagicMock()
        ops = _make_ops()
        result = ops.verified_paste("text", wm, target_hwnd=9999)

        assert result is True
        mock_read.assert_not_called()


class TestVerifiedPasteHwndNormalization:
    """wh-oe7u.3: foreground/expected HWND comparison goes through
    ``normalize_hwnd_for_foreground_compare`` so Chromium and Electron
    apps -- where UIA captures a renderer child HWND while
    GetForegroundWindow returns the top-level frame -- compare equal.

    The check is also fail-closed: any normalization failure on either
    side returns False rather than silently passing the gate.
    """

    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_chromium_child_target_with_root_foreground_succeeds(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui
    ):
        """Captured target_hwnd is the renderer child; GetForegroundWindow
        returns the top-level Chrome_WidgetWin_1. Both normalize to the
        same root, so verified_paste returns True and the counter
        advances."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        # Renderer child captured at strategy entry.
        target = 0xC11D
        # Top-level root of the same Chrome window.
        root = 0xC007
        mock_win32gui.GetForegroundWindow.return_value = root

        # Both sides normalize to the same root.
        with patch(
            f"{_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=lambda h: root if h in (target, root) else None,
        ):
            wm = MagicMock()
            ops = _make_ops()
            before = ops.accumulated_paste_chars
            result = ops.verified_paste("text", wm, target_hwnd=target)

        assert result is True, (
            "Chromium child target normalized to the same root as the "
            "top-level foreground -- verified_paste must succeed."
        )
        assert ops.accumulated_paste_chars == before + len("text")

    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_get_foreground_window_exception_returns_false_no_fail_open(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui
    ):
        """The previous code did ``actual_hwnd = target_hwnd`` on
        GetForegroundWindow exception, silently passing the gate. The
        wh-oe7u.3 fix makes this fail-closed: any GetForegroundWindow
        failure returns False so the retract counter is not credited."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.side_effect = OSError("rpc fail")

        with patch(
            f"{_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=lambda h: h if h else None,
        ):
            wm = MagicMock()
            ops = _make_ops()
            before = ops.accumulated_paste_chars
            result = ops.verified_paste("text", wm, target_hwnd=0xCAFE)

        assert result is False, (
            "GetForegroundWindow exception must fail closed; previous "
            "code silently fell back to target_hwnd."
        )
        assert ops.accumulated_paste_chars == before

    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_target_hwnd_normalize_failure_returns_false(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui
    ):
        """If the captured target_hwnd cannot be root-normalized (e.g.
        the captured HWND has been destroyed by paste time), refuse to
        credit the counter."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 0xCAFE

        # Normalize returns None for the target_hwnd specifically.
        def _norm(h):
            if h == 0xDEAD:
                return None
            return h if h else None

        with patch(
            f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=_norm,
        ):
            wm = MagicMock()
            ops = _make_ops()
            before = ops.accumulated_paste_chars
            result = ops.verified_paste("text", wm, target_hwnd=0xDEAD)

        assert result is False
        assert ops.accumulated_paste_chars == before

    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_observed_foreground_normalize_failure_returns_false(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui
    ):
        """The bead's round-3 correction: fail-closed must apply to the
        observed foreground HWND too. If normalize returns None for the
        foreground, return False (do not silently pass)."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 0xDEAD

        # Normalize fails specifically for the observed foreground.
        def _norm(h):
            if h == 0xDEAD:
                return None
            return h if h else None

        with patch(
            f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=_norm,
        ):
            wm = MagicMock()
            ops = _make_ops()
            before = ops.accumulated_paste_chars
            result = ops.verified_paste("text", wm, target_hwnd=0xCAFE)

        assert result is False
        assert ops.accumulated_paste_chars == before


class TestVerifiedPasteChromiumSameProcessFallback:
    """wh-fc1x.2: post-paste foreground check tolerates same-process drift
    inside known Chromium-derived browsers.

    UIA's GetTopLevelControl HWND and Win32 GetForegroundWindow can return
    different roots for the same Brave / Chrome / Edge tab when accessibility
    tree position differs from the OS foreground window. The keystrokes still
    land in the focused renderer of the main HWND. Strict GA_ROOT equality
    flags this as a paste failure and produces a RuntimeError cascade up the
    IPC chain (the wh-3nwy pattern in a different code path).

    The fallback is opt-in to known browser exe names only -- non-browser
    apps with multi-top-level shapes (Word dialogs, Visual Studio popups)
    keep the strict comparison because their drift usually means a
    misdirected paste.
    """

    @patch(f"{_MOD}.same_process_fallback_matches")
    @patch(f"{_MOD}.process_identity_for_hwnd")
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_brave_same_process_drift_passes_check(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui,
        _mock_norm, mock_process_name, mock_hwnds_match,
    ):
        """target_hwnd and GetForegroundWindow return DIFFERENT roots, both
        owned by brave.exe. The fallback recognizes the same-process case
        and the post-paste check passes."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 986964
        # Roots differ -- captured target is one Brave top-level, foreground
        # is a different Brave top-level.
        mock_process_name.return_value = (2584, "brave.exe")
        mock_hwnds_match.return_value = True

        wm = MagicMock()
        ops = _make_ops()
        before = ops.accumulated_paste_chars
        result = ops.verified_paste("text", wm, target_hwnd=6165172)

        assert result is True, (
            "Same-process Chromium drift must NOT fail the post-paste "
            "check: keystrokes land in the focused renderer of the main "
            "HWND despite the GA_ROOT mismatch."
        )
        assert ops.accumulated_paste_chars == before + len("text")
        # Helper called with the brave.exe process name so non-browser
        # apps cannot accidentally use this path.
        # wh-review-pattern-fixes.41: two calls now, one for the pre-send
        # focus proof and one for the post-paste check. Both must carry
        # the same browser scoping, and (.1.34) the pid the name was
        # resolved from as the fallback's snapshot.
        assert mock_hwnds_match.call_count == 2
        for call_args in mock_hwnds_match.call_args_list:
            assert call_args.kwargs["expected_process_name"] == "brave.exe"
            assert call_args.kwargs["expected_pid_snapshot"] == 2584

    @patch(f"{_MOD}.same_process_fallback_matches")
    @patch(f"{_MOD}.process_identity_for_hwnd")
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_non_browser_root_mismatch_still_fails(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui,
        _mock_norm, mock_process_name, mock_hwnds_match,
    ):
        """target_hwnd and GetForegroundWindow return different roots, but
        the process is not in the Chromium browser list. The strict
        GA_ROOT contract still applies -- the check fails."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 7777
        mock_process_name.return_value = (999, "notepad.exe")

        wm = MagicMock()
        ops = _make_ops()
        before = ops.accumulated_paste_chars
        result = ops.verified_paste("text", wm, target_hwnd=9999)

        assert result is False, (
            "Non-browser process with mismatched roots must keep the "
            "strict GA_ROOT contract; same-process fallback is opt-in to "
            "Chromium-derived browsers only."
        )
        assert ops.accumulated_paste_chars == before
        # Helper must NOT be called when process is not on the browser list,
        # because allow_same_process gating short-circuits before invocation.
        mock_hwnds_match.assert_not_called()

    @patch(f"{_MOD}.same_process_fallback_matches")
    @patch(f"{_MOD}.process_identity_for_hwnd")
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_chromium_helper_returns_false_still_fails(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui,
        _mock_norm, mock_process_name, mock_hwnds_match,
    ):
        """When the helper itself returns False (cross-process drift even
        inside a browser process), the post-paste check still fails."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 7777
        mock_process_name.return_value = (8888, "chrome.exe")
        mock_hwnds_match.return_value = False

        wm = MagicMock()
        ops = _make_ops()
        before = ops.accumulated_paste_chars
        result = ops.verified_paste("text", wm, target_hwnd=9999)

        assert result is False
        assert ops.accumulated_paste_chars == before
        mock_hwnds_match.assert_called_once()

    def test_handle_reuse_after_confirmed_mismatch_refuses(self):
        """wh-ensure-focused-same-process-fallback.1.4 (codex round 3):
        _foreground_matches_target confirms a root mismatch, the
        process-name probe sees the live Brave target, then the handle
        is destroyed and reused by a child of the non-Brave foreground
        window. The earlier shape handed the raw handles back to
        hwnds_match_for_foreground_compare, whose re-normalization
        reported strict root equality and credited before any PID or
        exe probe ran. The fallback must instead refuse via the raw
        PID/exe probes, and GetAncestor must run exactly twice."""
        target, foreground = 133604, 68996
        brave_pid, notepad_pid = 2584, 999

        ancestor_calls = {"n": 0}

        def _rebinding_ancestor(hwnd, flag):
            ancestor_calls["n"] += 1
            if ancestor_calls["n"] <= 2:
                return hwnd
            return foreground

        pid_calls = {"target": 0}

        def _pids(hwnd):
            if hwnd == target:
                pid_calls["target"] += 1
                if pid_calls["target"] == 1:
                    return (1000, brave_pid)
                return (1000, notepad_pid)
            if hwnd == foreground:
                return (1000, notepad_pid)
            return (1000, 0)

        def _proc(pid):
            m = MagicMock()
            m.name.return_value = (
                "brave.exe" if pid == brave_pid else "notepad.exe"
            )
            return m

        ops = _make_ops()
        with patch(
            "ui.hwnd_utils.win32gui.GetAncestor",
            side_effect=_rebinding_ancestor,
        ) as mock_ancestor, patch(
            "ui.hwnd_utils.win32process.GetWindowThreadProcessId",
            side_effect=_pids,
        ), patch("ui.hwnd_utils.psutil.Process", side_effect=_proc):
            result = ops._foreground_matches_target(
                target, foreground, phase="post-paste",
            )

        assert result is False, (
            "A handle reused between the strict compare and the "
            "fallback must not credit through re-normalized strict "
            "equality; the PID/exe probes must run and refuse."
        )
        assert mock_ancestor.call_count == 2

    def test_same_exe_rebind_before_fallback_sample_refuses(self):
        """wh-ensure-focused-same-process-fallback.1.34 (deepseek round
        24): _foreground_matches_target resolves the target's exe name
        from pid P (Brave profile A), the transient helper dies, and
        Windows reuses the handle for a helper of a SECOND brave.exe
        process (profile B, pid Q) before the fallback samples. Both
        fallback samples then agree on Q, psutil reports brave.exe for
        Q, the observed helper shape passes, and the .1.31 re-probe
        anchors only the post-rebind samples -- every probe credited a
        paste into the wrong process. The fallback must refuse when its
        first sample of the target differs from the pid the caller's
        identity sample produced."""
        target, foreground = 133604, 68996
        profile_a_pid, profile_b_pid = 2584, 4444

        pid_calls = {"target": 0}

        def _rebinding_pids(hwnd):
            if hwnd == target:
                pid_calls["target"] += 1
                if pid_calls["target"] == 1:
                    return (1000, profile_a_pid)
                return (1000, profile_b_pid)
            if hwnd == foreground:
                return (1000, profile_b_pid)
            return (1000, 0)

        def _proc(pid):
            m = MagicMock()
            m.name.return_value = "brave.exe"  # same exe for BOTH pids
            return m

        ops = _make_ops()
        with patch(
            "ui.hwnd_utils.win32gui.GetAncestor",
            side_effect=lambda hwnd, flag: hwnd,
        ), patch(
            "ui.hwnd_utils.win32process.GetWindowThreadProcessId",
            side_effect=_rebinding_pids,
        ), patch(
            "ui.hwnd_utils.psutil.Process", side_effect=_proc,
        ), patch(
            # The captured target keeps the invisible helper shape; the
            # observed frame is a visible plain window. Pinned live so
            # only the pid-snapshot comparison can refuse (.1.33
            # masking-class discipline).
            "ui.hwnd_utils.win32gui.IsWindowVisible",
            side_effect=lambda hwnd: hwnd != target,
        ), patch(
            "ui.hwnd_utils.win32gui.GetWindowLong", return_value=0,
        ), patch(
            "ui.hwnd_utils.win32gui.IsWindow", return_value=1,
        ):
            result = ops._foreground_matches_target(
                target, foreground, phase="post-paste",
            )

        assert result is False, (
            "A same-exe cross-process handle rebind between the "
            "caller's identity sample and the fallback's first PID "
            "sample must refuse: the keystrokes would land in the "
            "other browser profile's process."
        )

    def test_visible_sibling_foreground_drift_refuses(self):
        """wh-ensure-focused-same-process-fallback.1.28 (codex round
        18): target A and observed foreground B are both VISIBLE plain
        top-level frames of the same Brave process -- ensure_focused
        proved A strictly, then sibling B took foreground before the
        final proof. The raw PID and exe probes both pass, so without
        a pair-shape guard the fallback affirmatively credits B and
        the paste lands in the wrong window. The guard must refuse a
        pair where neither side has the helper shape (invisible or
        WS_EX_TOOLWINDOW)."""
        target, foreground = 133604, 68996
        brave_pid = 2584

        def _proc(pid):
            m = MagicMock()
            m.name.return_value = "brave.exe"
            return m

        ops = _make_ops()
        with patch(
            "ui.hwnd_utils.win32gui.GetAncestor",
            side_effect=lambda hwnd, flag: hwnd,
        ), patch(
            "ui.hwnd_utils.win32gui.IsWindowVisible",
            return_value=True,
        ), patch(
            "ui.hwnd_utils.win32gui.GetWindowLong",
            return_value=0,
        ), patch(
            "ui.hwnd_utils.win32process.GetWindowThreadProcessId",
            return_value=(1000, brave_pid),
        ), patch("ui.hwnd_utils.psutil.Process", side_effect=_proc):
            result = ops._foreground_matches_target(
                target, foreground, phase="post-paste",
            )

        assert result is False, (
            "Two visible plain same-process browser frames are the "
            "wrong-window sibling shape; the fallback must stay strict."
        )


# ===========================================================================
# Clear Selection
# ===========================================================================

class TestClearSelection:
    """ClipboardOperations.clear_selection - sentinel-based selection clearing."""

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_no_selection_detected(self, mock_pyperclip, mock_vpk, mock_time):
        """When clipboard returns sentinel, no selection exists."""
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()
        sentinel = "__SENTINEL_SEL_12345.0__"

        mock_pyperclip.paste.return_value = sentinel

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is True
        # Ctrl+C should be sent, but Delete should NOT
        mock_vpk.assert_called_once_with('ctrl', 'c')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_selection_detected_and_deleted(self, mock_pyperclip, mock_vpk, mock_time):
        """When clipboard differs from sentinel, selection is deleted."""
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()

        mock_pyperclip.paste.return_value = "selected text"

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is True
        # Should send Ctrl+C then Delete
        calls = mock_vpk.call_args_list
        assert calls[0] == call('ctrl', 'c')
        assert calls[1] == call('delete')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_empty_selection_not_deleted(self, mock_pyperclip, mock_vpk, mock_time):
        """Empty string selection (falsy) should NOT trigger delete."""
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()

        # Clipboard changed from sentinel to empty string
        mock_pyperclip.paste.return_value = ""

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is True
        # Only Ctrl+C, no Delete (empty string is falsy)
        assert mock_vpk.call_count == 1
        mock_vpk.assert_called_with('ctrl', 'c')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_uses_sendkeys(self, mock_pyperclip, mock_vpk, mock_time):
        """Flutter control should use SendKeys for copy and delete."""
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()

        mock_pyperclip.paste.return_value = "selected"

        flutter = MagicMock()
        flutter.Exists.return_value = True

        ops = _make_ops()
        result = ops.clear_selection(flutter_control=flutter)

        assert result is True
        flutter.SendKeys.assert_any_call('{Ctrl}c')
        flutter.SendKeys.assert_any_call('{Delete}')
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_exception_returns_false(self, mock_pyperclip, mock_vpk, mock_time):
        """Exception during selection clearing should return False."""
        mock_time.time.return_value = 12345.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_no_selection_no_delete(self, mock_pyperclip, mock_vpk, mock_time):
        """Flutter path: if sentinel unchanged, no delete sent."""
        mock_time.time.return_value = 99.0
        mock_time.sleep = MagicMock()
        sentinel = "__SENTINEL_SEL_99.0__"

        mock_pyperclip.paste.return_value = sentinel

        flutter = MagicMock()
        flutter.Exists.return_value = True

        ops = _make_ops()
        ops.clear_selection(flutter_control=flutter)

        # SendKeys called for Ctrl+C, but NOT for Delete
        flutter.SendKeys.assert_called_once_with('{Ctrl}c')


class TestClearSelectionCapturesSelectionForRestore:
    """clear_selection captures the selection text on
    last_cleared_selection so a later pre-send verified_paste failure
    can restore it (wh-t81d9.5)."""

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_selection_text_captured_on_detection(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        mock_time.time.return_value = 1.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "important user text"

        ops = _make_ops()
        ops.clear_selection()

        assert ops.last_cleared_selection == "important user text"

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_no_selection_resets_slot_to_none(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        """A prior captured selection must NOT survive a no-selection
        clear_selection call -- otherwise a later restore would fire
        with stale text."""
        mock_time.time.return_value = 2.0
        mock_time.sleep = MagicMock()
        sentinel = "__SENTINEL_SEL_2.0__"
        mock_pyperclip.paste.return_value = sentinel

        ops = _make_ops()
        ops.last_cleared_selection = "stale value from a prior call"
        ops.clear_selection()

        assert ops.last_cleared_selection is None

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_empty_selection_does_not_capture(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        """An empty-string read counts as no selection; do not capture."""
        mock_time.time.return_value = 3.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = ""

        ops = _make_ops()
        ops.last_cleared_selection = "stale"
        ops.clear_selection()

        assert ops.last_cleared_selection is None


class TestRawPaste:
    """ClipboardOperations._raw_paste -- bypasses verification and all
    retract-accounting flags so a selection restore is invisible to
    the retract subsystem (wh-t81d9.5).

    wh-review-pattern-fixes.41: the non-Flutter path now proves the
    captured target holds the foreground before the Ctrl+V, so the tests
    that expect a send patch win32gui and the HWND normalizer to report
    the captured window as foreground.
    """

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_non_flutter_uses_sendinput(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        mock_time.sleep = MagicMock()
        mock_win32gui.GetForegroundWindow.return_value = 0xABC
        ops = _make_ops()
        wm = MagicMock()
        wm.ensure_focused.return_value = True

        ops._raw_paste("restored text", wm, target_hwnd=0xABC)

        mock_pyperclip.copy.assert_called_with("restored text")
        mock_vpk.assert_called_with('ctrl', 'v')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_uses_sendkeys_and_skips_focus(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        mock_time.sleep = MagicMock()
        ops = _make_ops()
        wm = MagicMock()
        flutter = MagicMock()
        flutter.Exists.return_value = True

        ops._raw_paste(
            "restored", wm, target_hwnd=0xABC, flutter_control=flutter,
        )

        flutter.SendKeys.assert_called_with('{Ctrl}v')
        mock_vpk.assert_not_called()
        # Flutter path skips focus restoration
        wm.ensure_focused.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_focus_restored_via_window_manager(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        mock_time.sleep = MagicMock()
        ops = _make_ops()
        wm = MagicMock()

        ops._raw_paste("text", wm, target_hwnd=0xCAFE)

        wm.ensure_focused.assert_called_with(0xCAFE)

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_copy_failure_returns_false_and_no_paste(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy.side_effect = Exception("locked")

        ops = _make_ops()
        wm = MagicMock()

        ok = ops._raw_paste("x", wm)

        assert ok is False
        mock_vpk.assert_not_called()


class TestRestoreClearedSelection:
    """ClipboardOperations.restore_cleared_selection -- the public entry
    point used by ClipboardFallbackStrategy on pre-send paste failure
    (wh-t81d9.5)."""

    def test_returns_false_when_nothing_to_restore(self):
        ops = _make_ops()
        ops.last_cleared_selection = None

        result = ops.restore_cleared_selection(MagicMock())

        assert result is False

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_returns_true_and_clears_slot_after_restore(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        mock_time.sleep = MagicMock()
        mock_win32gui.GetForegroundWindow.return_value = 0xABC
        ops = _make_ops()
        ops.last_cleared_selection = "saved text"
        wm = MagicMock()
        wm.ensure_focused.return_value = True

        result = ops.restore_cleared_selection(wm, target_hwnd=0xABC)

        assert result is True
        assert ops.last_cleared_selection is None
        mock_vpk.assert_called_with('ctrl', 'v')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_does_not_mutate_retract_accounting(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        """The restored text is the user's PRIOR content, not new
        dictation. accumulated_paste_chars, last_paste_was_optimistic,
        and last_paste_was_sent must all stay at their pre-call values
        so the retract subsystem cannot see the restore."""
        mock_time.sleep = MagicMock()
        ops = _make_ops()
        ops.last_cleared_selection = "saved"
        ops.accumulated_paste_chars = 7
        ops.last_paste_was_optimistic = True
        ops.last_paste_was_sent = False

        ops.restore_cleared_selection(MagicMock(), target_hwnd=0x1)

        assert ops.accumulated_paste_chars == 7
        assert ops.last_paste_was_optimistic is True
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_branch_uses_sendkeys(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        mock_time.sleep = MagicMock()
        ops = _make_ops()
        ops.last_cleared_selection = "saved"
        flutter = MagicMock()
        flutter.Exists.return_value = True

        ops.restore_cleared_selection(
            MagicMock(), target_hwnd=0x1, flutter_control=flutter,
        )

        flutter.SendKeys.assert_called_with('{Ctrl}v')
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_clears_slot_even_when_raw_paste_fails(
        self, mock_pyperclip, mock_vpk, mock_time
    ):
        """If the underlying _raw_paste raises, we still clear the slot
        in the finally branch -- otherwise a future restore could fire
        on stale text that nobody can verify."""
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy.side_effect = Exception("locked")

        ops = _make_ops()
        ops.last_cleared_selection = "saved"

        ops.restore_cleared_selection(MagicMock())

        assert ops.last_cleared_selection is None


# ===========================================================================
# Gather Context
# ===========================================================================

class TestGatherContext:
    """ClipboardOperations.gather_context - cursor context via clipboard."""

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_full_context_both_directions(self, mock_pyperclip, mock_vpk, mock_time, mock_seq, mock_wait):
        """Gather context with text both before and after cursor."""
        mock_time.time.side_effect = [1.0, 2.0]  # for sentinels
        mock_time.sleep = MagicMock()

        sentinel_before = "__SENTINEL_B_1.0__"
        sentinel_after = "__SENTINEL_A_2.0__"

        # Sequence: copy sentinel_before, paste returns "ab" (before text),
        # copy sentinel_after, paste returns "c" (after text)
        mock_pyperclip.paste.side_effect = ["ab", "c"]

        ops = _make_ops()
        result = ops.gather_context()

        assert result == {'preceding_chars': 'ab', 'has_selection': False, 'delivery_failed': False}

        # Verify arrow key movements: shift+left+left, ctrl+c, right,
        # then shift+right, ctrl+c, left
        calls = [c[0] for c in mock_vpk.call_args_list]
        assert ('shift', 'left', 'left') in calls
        assert ('ctrl', 'c') in calls
        assert ('right',) in calls
        assert ('shift', 'right') in calls
        assert ('left',) in calls

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_beginning_of_document(self, mock_pyperclip, mock_vpk, mock_time, mock_seq, mock_wait):
        """At start of document, sentinel stays unchanged for before-text."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_before = "__SENTINEL_B_1.0__"
        sentinel_after = "__SENTINEL_A_2.0__"

        # Before: sentinel unchanged (no text before cursor)
        # After: has text
        mock_pyperclip.paste.side_effect = [sentinel_before, "x"]

        ops = _make_ops()
        result = ops.gather_context()

        assert result['preceding_chars'] == ''

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_end_of_document(self, mock_pyperclip, mock_vpk, mock_time, mock_seq, mock_wait):
        """At end of document, sentinel stays unchanged for after-text."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_after = "__SENTINEL_A_2.0__"

        # Before: has text, After: sentinel unchanged
        mock_pyperclip.paste.side_effect = ["ab", sentinel_after]

        ops = _make_ops()
        result = ops.gather_context()

        assert result['preceding_chars'] == 'ab'

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_empty_document(self, mock_pyperclip, mock_vpk, mock_time, mock_seq, mock_wait):
        """Empty document: both sentinels unchanged."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_before = "__SENTINEL_B_1.0__"
        sentinel_after = "__SENTINEL_A_2.0__"

        mock_pyperclip.paste.side_effect = [sentinel_before, sentinel_after]

        ops = _make_ops()
        result = ops.gather_context()

        assert result == {'preceding_chars': '', 'has_selection': False, 'delivery_failed': False}

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_uses_sendkeys(self, mock_pyperclip, mock_vpk, mock_time):
        """Flutter control should use SendKeys for all key operations."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        # Both sentinels overwritten
        mock_pyperclip.paste.side_effect = ["ab", "c"]

        flutter = MagicMock()
        flutter.Exists.return_value = True

        ops = _make_ops()
        result = ops.gather_context(flutter_control=flutter)

        assert result['preceding_chars'] == 'ab'

        # Flutter should use SendKeys, not the SendInput chord
        mock_vpk.assert_not_called()
        sendkeys_calls = [c[0][0] for c in flutter.SendKeys.call_args_list]
        assert '{Shift}{Left}' in sendkeys_calls
        assert '{Ctrl}c' in sendkeys_calls
        assert '{Right}' in sendkeys_calls
        assert '{Shift}{Right}' in sendkeys_calls
        assert '{Left}' in sendkeys_calls

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_exception_returns_delivery_failed_context(self, mock_pyperclip, mock_vpk, mock_time, mock_seq, mock_wait):
        """Any exception should report a failed probe.

        wh-review-pattern-fixes.39: this test asserted
        ``delivery_failed: False`` and was named
        ``test_exception_returns_empty_context``. The values are still
        empty; the flag is what stops the caller from reading them as a
        genuine start-of-field.
        """
        mock_time.time.return_value = 1.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")

        ops = _make_ops()
        result = ops.gather_context()

        assert result == {'preceding_chars': '', 'has_selection': False, 'delivery_failed': True}

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_has_selection_always_false(self, mock_pyperclip, mock_vpk, mock_time, mock_seq, mock_wait):
        """has_selection should always be False (documented behavior)."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.side_effect = ["ab", "c"]

        ops = _make_ops()
        result = ops.gather_context()

        assert result['has_selection'] is False

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_before_text_found_resets_cursor(self, mock_pyperclip, mock_vpk, mock_time, mock_seq, mock_wait):
        """When before-text found, right arrow resets cursor position."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_after = "__SENTINEL_A_2.0__"
        mock_pyperclip.paste.side_effect = ["xy", sentinel_after]

        ops = _make_ops()
        ops.gather_context()

        # Right arrow should be called to deselect and reposition
        calls = [c[0] for c in mock_vpk.call_args_list]
        assert ('right',) in calls

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_no_before_text_skips_right_arrow(self, mock_pyperclip, mock_vpk, mock_time, mock_seq, mock_wait):
        """When before-text not found (sentinel), right arrow is skipped."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_before = "__SENTINEL_B_1.0__"
        sentinel_after = "__SENTINEL_A_2.0__"
        mock_pyperclip.paste.side_effect = [sentinel_before, sentinel_after]

        ops = _make_ops()
        ops.gather_context()

        # No right arrow (no cursor repositioning needed)
        calls = [c[0] for c in mock_vpk.call_args_list]
        # Should have shift+left+left, ctrl+c for before attempt,
        # then shift+right, ctrl+c for after attempt
        # But NO right or left for cursor reset
        assert ('right',) not in calls
        assert ('left',) not in calls

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_after_text_found_resets_cursor(self, mock_pyperclip, mock_vpk, mock_time, mock_seq, mock_wait):
        """When after-text found, left arrow resets cursor position."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_before = "__SENTINEL_B_1.0__"
        mock_pyperclip.paste.side_effect = [sentinel_before, "z"]

        ops = _make_ops()
        ops.gather_context()

        calls = [c[0] for c in mock_vpk.call_args_list]
        assert ('left',) in calls


# ===========================================================================
# Adversarial Tests
# ===========================================================================

class TestAdversarial:
    """Adversarial scenarios: clipboard contention, edge-case content."""

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_clipboard_locked_during_verified_paste(self, mock_pyperclip, mock_vpk, mock_time):
        """Clipboard locked by another process during copy -> fails gracefully."""
        mock_time.perf_counter.return_value = 0.0
        mock_pyperclip.copy.side_effect = PermissionError("clipboard locked by another process")

        ops = _make_ops()
        result = ops.verified_paste("text", MagicMock())

        assert result is False
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_huge_clipboard_content(self, mock_pyperclip, mock_vpk, mock_time):
        """Large text should paste without issues."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        huge_text = "x" * 100_000
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = huge_text

        wm = MagicMock()
        wm.get_target_window.return_value = (None, None)

        ops = _make_ops()
        result = ops.verified_paste(huge_text, wm)

        assert result is True

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_empty_text_paste(self, mock_pyperclip, mock_vpk, mock_time):
        """Pasting empty string should work (clipboard verifies empty)."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = ""

        wm = MagicMock()
        wm.get_target_window.return_value = (None, None)

        ops = _make_ops()
        result = ops.verified_paste("", wm)

        assert result is True

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_clipboard_intermittent_failures_during_context(self, mock_pyperclip, mock_vpk, mock_time, mock_seq, mock_wait):
        """Clipboard exceptions during gather_context report a failure.

        wh-review-pattern-fixes.39: this case used to assert
        ``delivery_failed: False``. The clipboard read raises after
        Shift+Left+Left and Ctrl+C have both landed, so the selection is
        live and the caller must not paste.
        """
        mock_time.time.return_value = 1.0
        mock_time.sleep = MagicMock()

        # copy succeeds first, then paste raises
        copy_count = [0]
        def copy_side_effect(text):
            copy_count[0] += 1
            if copy_count[0] == 1:
                return  # first copy succeeds
            raise Exception("clipboard unavailable")

        mock_pyperclip.copy.side_effect = copy_side_effect
        mock_pyperclip.paste.side_effect = Exception("clipboard locked")

        ops = _make_ops()
        result = ops.gather_context()

        assert result == {'preceding_chars': '', 'has_selection': False, 'delivery_failed': True}

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_clear_selection_with_unicode_text(self, mock_pyperclip, mock_vpk, mock_time):
        """Unicode selection content should be handled correctly."""
        mock_time.time.return_value = 1.0
        mock_time.sleep = MagicMock()

        mock_pyperclip.paste.return_value = "Hello World"  # unicode chars

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is True
        # Delete should be called since selection was found
        calls = [c[0] for c in mock_vpk.call_args_list]
        assert ('delete',) in calls

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_clipboard_content_changes_mid_verification(self, mock_pyperclip, mock_vpk, mock_time):
        """Another process modifying clipboard mid-verify should cause timeout."""
        counter = [0.0]
        def advancing_counter():
            val = counter[0]
            counter[0] += 0.05
            return val
        mock_time.perf_counter.side_effect = advancing_counter
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        # Clipboard keeps changing to different wrong values
        mock_pyperclip.paste.side_effect = [
            "foreign1", "foreign2", "foreign3", "foreign4",
            "foreign5", "foreign6", "foreign7", "foreign8",
        ]

        ops = _make_ops(clipboard_verification_timeout_ms=250)
        result = ops.verified_paste("expected", MagicMock())

        assert result is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_optimistic_paste_on_pure_lock_contention(self, mock_pyperclip, mock_vpk, mock_time):
        """When clipboard is locked throughout verification (no wrong content seen),
        proceed with optimistic paste since _safe_copy already succeeded."""
        counter = [0.0]
        def advancing_counter():
            val = counter[0]
            counter[0] += 0.05
            return val
        mock_time.perf_counter.side_effect = advancing_counter
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        # All paste attempts fail with exception (clipboard locked by another process)
        mock_pyperclip.paste.side_effect = Exception("OpenClipboard failed")

        wm = MagicMock()
        wm.get_target_window.return_value = (123, None)

        ops = _make_ops(clipboard_verification_timeout_ms=250)
        result = ops.verified_paste("test text", wm)

        # Should succeed with optimistic paste: copy succeeded, all failures
        # were lock-related (no wrong content observed)
        assert result is True
        mock_vpk.assert_called_with('ctrl', 'v')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_no_optimistic_paste_when_wrong_content_seen(self, mock_pyperclip, mock_vpk, mock_time):
        """When clipboard shows wrong content (not just locks), do NOT optimistic paste."""
        counter = [0.0]
        def advancing_counter():
            val = counter[0]
            counter[0] += 0.1
            return val
        mock_time.perf_counter.side_effect = advancing_counter
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        # Mix of wrong content and lock failures
        mock_pyperclip.paste.side_effect = [
            "wrong content",       # content mismatch
            Exception("locked"),   # then lock failure
        ]

        ops = _make_ops(clipboard_verification_timeout_ms=250)
        result = ops.verified_paste("expected", MagicMock())

        # Should NOT optimistic paste since wrong content was observed
        assert result is False
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_re_copy_on_content_mismatch(self, mock_pyperclip, mock_vpk, mock_time):
        """When another process overwrites clipboard, re-copy our text."""
        times = iter([
            0.0,   # t_start
            0.001, # t_after_copy
            0.002, # start_time
            0.003, # 1st while check
            0.004, # 2nd while check
            0.005, # t_after_verify
            0.006, # t_after_focus
            0.007, # t_after_sendkeys
            0.008, # t_after_sleep
        ])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        # First read: wrong content (another process overwrote clipboard)
        # Second read: correct (after our re-copy restores it)
        mock_pyperclip.paste.side_effect = ["overwritten by browser", "target text"]

        wm = MagicMock()
        wm.get_target_window.return_value = (1, None)

        ops = _make_ops()
        result = ops.verified_paste("target text", wm)

        assert result is True
        # copy should be called twice: initial + re-copy after mismatch
        assert mock_pyperclip.copy.call_count == 2

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_gather_context_flutter_exists_false(self, mock_pyperclip, mock_vpk, mock_time):
        """Flutter control that does not exist should use the SendInput path."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_before = "__SENTINEL_B_1.0__"
        sentinel_after = "__SENTINEL_A_2.0__"
        mock_pyperclip.paste.side_effect = [sentinel_before, sentinel_after]

        flutter = MagicMock()
        flutter.Exists.return_value = False

        ops = _make_ops()
        result = ops.gather_context(flutter_control=flutter)

        # Should fall back to the SendInput chord
        assert mock_vpk.call_count > 0
        flutter.SendKeys.assert_not_called()


# ---------------------------------------------------------------------------
# Sequence Polling Integration
# ---------------------------------------------------------------------------

class TestGatherContextSequencePolling:
    """gather_context should use adaptive sequence polling for non-Flutter."""

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.pyperclip")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    def test_non_flutter_uses_sequence_polling(self, mock_keys, mock_clip, mock_seq, mock_wait):
        """Non-Flutter path should use wait_for_clipboard_write after Ctrl+C."""
        mock_clip.paste.return_value = "ab"
        mock_clip.copy = MagicMock()
        ops = _make_ops()
        ops.gather_context()
        mock_wait.assert_called()

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=False)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.pyperclip")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    def test_reads_clipboard_even_on_polling_timeout(self, mock_keys, mock_clip, mock_seq, mock_wait):
        """Should read clipboard even if polling times out (graceful degradation)."""
        mock_clip.copy = MagicMock()
        mock_clip.paste.return_value = "x"
        ops = _make_ops()
        ops.gather_context()
        assert mock_clip.paste.called

    @patch(f"{_MOD}.wait_for_clipboard_write")
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.pyperclip")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    def test_flutter_skips_sequence_polling(self, mock_keys, mock_clip, mock_seq, mock_wait):
        """Flutter path should use fixed sleep, not sequence polling."""
        mock_clip.paste.return_value = "ab"
        mock_clip.copy = MagicMock()
        flutter_control = MagicMock()
        flutter_control.Exists.return_value = True
        ops = _make_ops()
        ops.gather_context(flutter_control)
        mock_wait.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_newlines_in_clipboard(self, mock_pyperclip, mock_vpk, mock_time):
        """Multi-line clipboard content should paste correctly."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        text = "line1\nline2\nline3"
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = text

        wm = MagicMock()
        wm.get_target_window.return_value = (None, None)

        ops = _make_ops()
        result = ops.verified_paste(text, wm)

        assert result is True


# ===========================================================================
# wh-review-pattern-fixes.36: every non-Flutter state-changing send is
# verified.
#
# press_keys returns None and only logs a short SendInput count, so a
# dropped Ctrl+C made clear_selection read a live user selection as "no
# selection", a dropped Delete was credited as cleared, a dropped arrow
# or copy in gather_context became an empty context, and a dropped
# Ctrl+V was reported as a successful paste. Every non-Flutter send in
# this module now goes through verified_press_keys with modifier key-up
# recovery, and the two preparation methods report the failed delivery
# to their caller. The Flutter SendKeys branches are unchanged -- those
# calls are synchronous and raise on failure.
#
# Every test below also patches ``press_keys`` with create=True. The
# module no longer imports that name, so the patch binds nothing; it is
# there so a mutation run that reverts one call site to the
# fire-and-forget helper cannot reach the real SendInput and type into
# the machine that runs the tests.
# ===========================================================================

class TestVerifiedPasteSendVerification:
    """verified_paste must not report success for an unacknowledged Ctrl+V."""

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_short_ctrl_v_returns_false_and_releases_ctrl(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "hello"
        mock_vpk.return_value = _SHORT

        wm = MagicMock()
        wm.get_target_window.return_value = (12345, MagicMock())

        ops = _make_ops()
        result = ops.verified_paste("hello", wm)

        assert result is False
        mock_vpk.assert_called_once_with('ctrl', 'v')
        mock_keyups.assert_called_once_with(('ctrl',))
        # A short chord must not credit the retract counter.
        assert ops.accumulated_paste_chars == 0

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_partial_delivery_keeps_sent_flag_true(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        """accepted > 0: part of the chord reached the target, so the
        selection-restore path must stay closed."""
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "hello"
        mock_vpk.return_value = _SHORT

        ops = _make_ops()
        assert ops.verified_paste("hello", MagicMock()) is False
        assert ops.last_paste_was_sent is True

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_zero_accepted_clears_sent_flag(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        """accepted == 0: no event reached the target, so the caller may
        safely restore the selection clear_selection deleted."""
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "hello"
        mock_vpk.return_value = _NOTHING

        ops = _make_ops()
        assert ops.verified_paste("hello", MagicMock()) is False
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_full_delivery_still_succeeds(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "hello"
        mock_vpk.return_value = _FULL

        ops = _make_ops()
        assert ops.verified_paste("hello", MagicMock()) is True
        assert ops.accumulated_paste_chars == 5
        mock_keyups.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_branch_unchanged(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        """Flutter SendKeys is synchronous and raises on failure."""
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "hello"
        flutter = MagicMock()
        flutter.Exists.return_value = True

        ops = _make_ops()
        assert ops.verified_paste("hello", MagicMock(), flutter) is True
        flutter.SendKeys.assert_called_once_with('{Ctrl}v')
        mock_vpk.assert_not_called()


class TestRawPasteSendVerification:
    """_raw_paste must not report a restore that was never delivered.

    wh-review-pattern-fixes.41: both scenarios below are about the send
    itself, so the pre-send focus proof is given the healthy shape --
    ensure_focused reports success and the captured HWND is foreground.
    """

    @staticmethod
    def _focused_window_manager():
        wm = MagicMock()
        wm.ensure_focused.return_value = True
        return wm

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_short_ctrl_v_returns_false(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time, mock_win32gui, _mock_norm,
    ):
        mock_time.sleep = MagicMock()
        mock_vpk.return_value = _SHORT
        mock_win32gui.GetForegroundWindow.return_value = 1

        ops = _make_ops()
        assert ops._raw_paste(
            "restored", self._focused_window_manager(), target_hwnd=1,
        ) is False
        mock_keyups.assert_called_once_with(('ctrl',))

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_full_delivery_returns_true(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time, mock_win32gui, _mock_norm,
    ):
        mock_time.sleep = MagicMock()
        mock_vpk.return_value = _FULL
        mock_win32gui.GetForegroundWindow.return_value = 1

        ops = _make_ops()
        assert ops._raw_paste(
            "restored", self._focused_window_manager(), target_hwnd=1,
        ) is True
        mock_vpk.assert_called_once_with('ctrl', 'v')


class TestClearSelectionSendVerification:
    """clear_selection must report a failed delivery instead of
    returning a value that looks like a genuine empty selection."""

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_short_ctrl_c_returns_false_without_delete(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        """The reproduction case: the sentinel is unchanged only because
        the copy never landed, so a live selection must not be read as
        'no selection'."""
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "__SENTINEL_SEL_12345.0__"
        mock_vpk.return_value = _SHORT

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is False
        mock_vpk.assert_called_once_with('ctrl', 'c')
        mock_keyups.assert_called_once_with(('ctrl',))
        assert ops.last_cleared_selection is None

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_short_delete_returns_false(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        """A dropped Delete must not be credited as a cleared selection."""
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "user selection"
        mock_vpk.side_effect = [_FULL, _SHORT]

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is False
        assert mock_vpk.call_args_list == [
            call('ctrl', 'c'), call('delete'),
        ]
        # A short Delete chord can leave the key physically held.
        mock_keyups.assert_called_once_with(('delete',))
        # Nothing safe to restore: the delete state is unknown.
        assert ops.last_cleared_selection is None

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_verified_copy_with_unchanged_sentinel_still_means_no_selection(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        """Preserved behavior: a VERIFIED Ctrl+C whose sentinel is
        unchanged is a genuine empty selection."""
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "__SENTINEL_SEL_12345.0__"
        mock_vpk.return_value = _FULL

        ops = _make_ops()
        ops.last_cleared_selection = "stale"
        result = ops.clear_selection()

        assert result is True
        assert ops.last_cleared_selection is None
        assert mock_vpk.call_args_list == [call('ctrl', 'c')]
        mock_keyups.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_verified_copy_and_delete_still_succeeds(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "user selection"
        mock_vpk.return_value = _FULL

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is True
        assert ops.last_cleared_selection == "user selection"

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_branch_unchanged(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "user selection"
        flutter = MagicMock()
        flutter.Exists.return_value = True

        ops = _make_ops()
        assert ops.clear_selection(flutter_control=flutter) is True
        flutter.SendKeys.assert_any_call('{Ctrl}c')
        flutter.SendKeys.assert_any_call('{Delete}')
        mock_vpk.assert_not_called()


class TestGatherContextSendVerification:
    """gather_context must report a failed delivery instead of an
    empty context that looks like a genuine start-of-field."""

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_short_shift_left_reports_failure_and_stops(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time, mock_seq, mock_wait,
    ):
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.side_effect = ["ab", "c"]
        mock_vpk.return_value = _SHORT

        ops = _make_ops()
        result = ops.gather_context()

        assert result['delivery_failed'] is True
        assert result['preceding_chars'] == ''
        assert result['has_selection'] is False
        # Fail closed: no further keys after the first short chord.
        assert mock_vpk.call_args_list == [call('shift', 'left', 'left')]
        mock_keyups.assert_called_once_with(('shift', 'left', 'left'))

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_short_ctrl_c_reports_failure(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time, mock_seq, mock_wait,
    ):
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.side_effect = ["ab", "c"]
        mock_vpk.side_effect = [_FULL, _SHORT]

        ops = _make_ops()
        result = ops.gather_context()

        assert result['delivery_failed'] is True
        assert result['preceding_chars'] == ''
        mock_keyups.assert_called_once_with(('ctrl', 'c'))

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_short_caret_restore_arrow_reports_failure(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time, mock_seq, mock_wait,
    ):
        """A dropped Right arrow leaves the two-character selection
        live; the caller must not paste over it."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.side_effect = ["ab", "c"]
        mock_vpk.side_effect = [_FULL, _FULL, _SHORT]

        ops = _make_ops()
        result = ops.gather_context()

        assert result['delivery_failed'] is True
        mock_keyups.assert_called_once_with(('right',))

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_full_delivery_reports_no_failure(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time, mock_seq, mock_wait,
    ):
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.side_effect = ["ab", "c"]
        mock_vpk.return_value = _FULL

        ops = _make_ops()
        result = ops.gather_context()

        assert result['preceding_chars'] == 'ab'
        assert result['delivery_failed'] is False
        assert mock_vpk.call_args_list == [
            call('shift', 'left', 'left'),
            call('ctrl', 'c'),
            call('right'),
            call('shift', 'right'),
            call('ctrl', 'c'),
            call('left'),
        ]
        mock_keyups.assert_not_called()

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_verified_sends_with_unchanged_sentinels_still_empty_context(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time, mock_seq, mock_wait,
    ):
        """Preserved behavior: VERIFIED sends whose sentinels are
        unchanged mean a genuinely empty field, not a failure."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.side_effect = [
            "__SENTINEL_B_1.0__", "__SENTINEL_A_2.0__",
        ]
        mock_vpk.return_value = _FULL

        ops = _make_ops()
        result = ops.gather_context()

        assert result['preceding_chars'] == ''
        assert result['delivery_failed'] is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_branch_unchanged(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.side_effect = ["ab", "c"]
        flutter = MagicMock()
        flutter.Exists.return_value = True

        ops = _make_ops()
        result = ops.gather_context(flutter_control=flutter)

        assert result['preceding_chars'] == 'ab'
        assert result['delivery_failed'] is False
        mock_vpk.assert_not_called()


# ===========================================================================
# wh-review-pattern-fixes.39: gather_context's own except branch.
#
# The .36 fix made every short SendInput chord report delivery_failed,
# but left the method's ``except Exception`` returning
# delivery_failed=False. The clipboard read that collects the selected
# characters runs AFTER Shift+Left+Left and Ctrl+C have been delivered,
# so an exception there leaves the two-character selection live and the
# caller pasted over it. Every exception out of this method now reports
# delivery_failed, including the ones raised before any navigation: the
# empty ``preceding_chars`` is then a fabrication, not an observation.
# ===========================================================================

class _COMErrorStub(Exception):
    """Stands in for ``comtypes.COMError``, which derives from Exception.

    A UIA SendKeys failure is not a RuntimeError and not an OSError, so
    a handler narrowed to either would let it escape gather_context.
    """


class TestGatherContextExceptionFailsClosed:
    """gather_context must report delivery_failed on any exception."""

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_clipboard_read_raises_after_left_selection(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time, mock_seq, mock_wait,
    ):
        """The reported reproduction: pyperclip.paste raises after the
        Shift+Left+Left selection and the Ctrl+C both landed."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_vpk.return_value = _FULL
        mock_pyperclip.paste.side_effect = OSError("clipboard unavailable")

        ops = _make_ops()
        result = ops.gather_context()

        assert result == {
            'preceding_chars': '',
            'has_selection': False,
            'delivery_failed': True,
        }
        # The balancing Right arrow never fired: the selection is live.
        assert mock_vpk.call_args_list == [
            call('shift', 'left', 'left'),
            call('ctrl', 'c'),
        ]

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_sequence_number_raises_after_left_selection(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time, mock_seq, mock_wait,
    ):
        """get_sequence_number raises between the selection and the copy."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_vpk.return_value = _FULL
        mock_seq.side_effect = OSError("clipboard sequence read failed")
        mock_pyperclip.paste.side_effect = ["ab", "c"]

        ops = _make_ops()
        result = ops.gather_context()

        assert result['delivery_failed'] is True
        assert result['preceding_chars'] == ''

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_verified_press_keys_raises_reports_failure(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time, mock_seq, mock_wait,
    ):
        """A raising send leaves the chord half-delivered, which is the
        same unknown state a short chord leaves."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_vpk.side_effect = OSError("SendInput failed")
        mock_pyperclip.paste.side_effect = ["ab", "c"]

        ops = _make_ops()
        result = ops.gather_context()

        assert result['delivery_failed'] is True

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_sentinel_copy_failure_before_any_navigation_reports_failure(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time, mock_seq, mock_wait,
    ):
        """No key has been sent yet, so the caret is where the caller
        left it -- but the probe never observed the field. An empty
        ``preceding_chars`` here is a fabrication, so it fails closed
        too."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_vpk.return_value = _FULL
        mock_pyperclip.copy.side_effect = OSError("clipboard locked")

        ops = _make_ops()
        result = ops.gather_context()

        assert result['delivery_failed'] is True
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}._send_modifier_keyups", create=True)
    @patch(f"{_MOD}.verified_press_keys", create=True)
    @patch(f"{_MOD}.press_keys", create=True)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_sendkeys_exception_after_navigation_reports_failure(
        self, mock_pyperclip, mock_press_keys, mock_vpk, mock_keyups,
        mock_time,
    ):
        """SendKeys is a synchronous UIA call that raises on failure.
        The Flutter branches keep their shape; the raise now reaches the
        caller as a failure instead of an empty context."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.side_effect = ["ab", "c"]
        flutter = MagicMock()
        flutter.Exists.return_value = True
        # First two SendKeys are the Shift+Left selection; the Ctrl+C
        # that follows them raises. comtypes.COMError derives straight
        # from Exception, so the stub does too -- a handler narrowed to
        # RuntimeError would let this one escape.
        flutter.SendKeys.side_effect = [None, None, _COMErrorStub("UIA call failed")]

        ops = _make_ops()
        result = ops.gather_context(flutter_control=flutter)

        assert result['delivery_failed'] is True
        assert result['preceding_chars'] == ''


# ===========================================================================
# Pre-send focus proof (wh-review-pattern-fixes.41)
# ===========================================================================

class TestVerifiedPastePreSendFocusProof:
    """verified_paste must prove the captured target is foreground.

    wh-review-pattern-fixes.41: the focus-restore block discarded the
    ``ensure_focused`` result, logged a SetFocus failure and continued,
    and logged the outer exception and continued. The Ctrl+V then fired
    into whatever window held foreground. When the caller supplies an
    explicit target, the paste must now stop before any key is sent.
    """

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_ensure_focused_false_aborts_before_ctrl_v(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        """ensure_focused reports False: no Ctrl+V, and the sent flag stays False."""
        mock_time.perf_counter.side_effect = lambda: 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 9999

        wm = MagicMock()
        wm.ensure_focused.return_value = False

        ops = _make_ops()
        result = ops.verified_paste("text", wm, target_hwnd=9999)

        assert result is False
        mock_vpk.assert_not_called()
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_setfocus_failure_aborts_before_ctrl_v(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        """SetFocus on the captured control raises: no Ctrl+V."""
        mock_time.perf_counter.side_effect = lambda: 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 9999

        control = MagicMock()
        control.SetFocus.side_effect = Exception("COM error")
        wm = MagicMock()
        wm.ensure_focused.return_value = True

        ops = _make_ops()
        result = ops.verified_paste(
            "text", wm, target_control=control, target_hwnd=9999,
        )

        assert result is False
        mock_vpk.assert_not_called()
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_target_resolution_failure_aborts_before_ctrl_v(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        """The captured control cannot yield an HWND: no Ctrl+V."""
        mock_time.perf_counter.side_effect = lambda: 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 9999

        control = MagicMock()
        control.GetTopLevelControl.side_effect = Exception("window gone")
        wm = MagicMock()

        ops = _make_ops()
        result = ops.verified_paste("text", wm, target_control=control)

        assert result is False
        mock_vpk.assert_not_called()
        assert ops.last_paste_was_sent is False
        wm.get_target_window.assert_not_called()
        # With no resolvable HWND there is nothing to activate, so the
        # method must not ask the window manager to focus a missing one.
        wm.ensure_focused.assert_not_called()

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_foreground_mismatch_aborts_before_ctrl_v(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        """ensure_focused reports True but another window is foreground.

        This is the Windows case ensure_focused cannot see on its own:
        the activation call was accepted and the foreground still
        belongs to somebody else.
        """
        mock_time.perf_counter.side_effect = lambda: 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 7777

        wm = MagicMock()
        wm.ensure_focused.return_value = True

        ops = _make_ops()
        result = ops.verified_paste("text", wm, target_hwnd=9999)

        assert result is False
        mock_vpk.assert_not_called()
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_ensure_focused_exception_aborts_before_ctrl_v(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        """The outer focus-restore handler must fail closed for an explicit target."""
        mock_time.perf_counter.side_effect = lambda: 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 9999

        wm = MagicMock()
        wm.ensure_focused.side_effect = Exception("win32 failure")

        ops = _make_ops()
        result = ops.verified_paste("text", wm, target_hwnd=9999)

        assert result is False
        mock_vpk.assert_not_called()
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_legacy_caller_without_captured_target_keeps_pasting(
        self, mock_pyperclip, mock_vpk, mock_time,
    ):
        """No explicit target means no captured intent to prove against.

        The legacy callers pass neither target_hwnd nor target_control,
        so verified_paste resolves the target from CURRENT focus. There
        is nothing to compare that against, and the pre-send proof must
        not change what those callers do.
        """
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"

        control = MagicMock()
        wm = MagicMock()
        wm.ensure_focused.return_value = False
        wm.get_target_window.return_value = (123, control)

        ops = _make_ops()
        result = ops.verified_paste("text", wm)

        assert result is True
        mock_vpk.assert_called_once_with('ctrl', 'v')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_branch_still_skips_the_focus_proof(
        self, mock_pyperclip, mock_vpk, mock_time,
    ):
        """The Flutter branch already holds focus and skips focus restoration.

        The pre-send proof must not run there, so a Flutter paste still
        fires even when the window manager would report a focus failure.
        """
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"

        flutter = MagicMock()
        flutter.Exists.return_value = True
        wm = MagicMock()
        wm.ensure_focused.return_value = False

        ops = _make_ops()
        result = ops.verified_paste(
            "text", wm, flutter_control=flutter, target_hwnd=9999,
        )

        assert result is True
        flutter.SendKeys.assert_called_once_with('{Ctrl}v')
        wm.ensure_focused.assert_not_called()


class TestRawPastePreSendFocusProof:
    """_raw_paste must prove the captured target is foreground.

    wh-review-pattern-fixes.41: this is the selection-restore path. The
    text it sends is the user's own saved selection, so a paste into the
    wrong window both loses the text in the original field and puts it
    into another application. The outer handler was
    ``except Exception: pass``.
    """

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_ensure_focused_false_aborts_before_ctrl_v(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        mock_time.sleep = MagicMock()
        mock_win32gui.GetForegroundWindow.return_value = 0xCAFE

        wm = MagicMock()
        wm.ensure_focused.return_value = False

        ops = _make_ops()
        result = ops._raw_paste("saved text", wm, target_hwnd=0xCAFE)

        assert result is False
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_setfocus_failure_aborts_before_ctrl_v(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        mock_time.sleep = MagicMock()
        mock_win32gui.GetForegroundWindow.return_value = 0xCAFE

        control = MagicMock()
        control.SetFocus.side_effect = Exception("COM error")
        wm = MagicMock()
        wm.ensure_focused.return_value = True

        ops = _make_ops()
        result = ops._raw_paste(
            "saved text", wm, target_control=control, target_hwnd=0xCAFE,
        )

        assert result is False
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_target_resolution_failure_aborts_before_ctrl_v(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        mock_time.sleep = MagicMock()
        mock_win32gui.GetForegroundWindow.return_value = 0xCAFE

        control = MagicMock()
        control.GetTopLevelControl.side_effect = Exception("window gone")
        wm = MagicMock()

        ops = _make_ops()
        result = ops._raw_paste("saved text", wm, target_control=control)

        assert result is False
        mock_vpk.assert_not_called()
        # With no resolvable HWND there is nothing to activate.
        wm.ensure_focused.assert_not_called()

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_foreground_mismatch_aborts_before_ctrl_v(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        """The bead's leak case: field A is gone and field B is foreground."""
        mock_time.sleep = MagicMock()
        mock_win32gui.GetForegroundWindow.return_value = 0xB0B

        wm = MagicMock()
        wm.ensure_focused.return_value = True

        ops = _make_ops()
        result = ops._raw_paste("private text", wm, target_hwnd=0xCAFE)

        assert result is False
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
           side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_outer_exception_aborts_before_ctrl_v(
        self, mock_pyperclip, mock_vpk, mock_time, mock_win32gui, _mock_norm,
    ):
        """The bare ``except Exception: pass`` must fail closed instead."""
        mock_time.sleep = MagicMock()
        mock_win32gui.GetForegroundWindow.return_value = 0xCAFE

        wm = MagicMock()
        wm.ensure_focused.side_effect = Exception("win32 failure")

        ops = _make_ops()
        result = ops._raw_paste("saved text", wm, target_hwnd=0xCAFE)

        assert result is False
        mock_vpk.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_branch_still_skips_the_focus_proof(
        self, mock_pyperclip, mock_vpk, mock_time,
    ):
        mock_time.sleep = MagicMock()
        flutter = MagicMock()
        flutter.Exists.return_value = True
        wm = MagicMock()
        wm.ensure_focused.return_value = False

        ops = _make_ops()
        result = ops._raw_paste(
            "saved text", wm, target_hwnd=0xCAFE, flutter_control=flutter,
        )

        assert result is True
        flutter.SendKeys.assert_called_with('{Ctrl}v')
        wm.ensure_focused.assert_not_called()


# ===========================================================================
# Captured-target HWND re-resolution must log its failures
# ===========================================================================

class TestCapturedTargetResolutionLogging:
    """wh-ensure-focused-same-process-fallback.1.2 (wh-captured-target-
    window-lost Step 1): when a caller passes target_hwnd=None with a
    captured control, verified_paste and _raw_paste re-resolve the HWND
    from the control. That lookup must go through
    top_level_hwnd_from_control so every None path logs a DEBUG line --
    the inline copies these tests were written against swallowed the
    failure in a bare ``except Exception`` and the "could not be
    resolved to an HWND" refusal fired with no clue which lookup
    produced the None.
    """

    _HWND_LOGGER = "ui.hwnd_utils"

    @staticmethod
    def _failing_control():
        control = MagicMock()
        control.GetTopLevelControl.side_effect = RuntimeError(
            "stale UIA control",
        )
        return control

    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.pyperclip")
    def test_verified_paste_resolution_failure_logs_debug(
        self, mock_pyperclip, mock_time, mock_vpk, mock_win32gui, caplog,
    ):
        mock_time.perf_counter.side_effect = lambda: 0.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        mock_win32gui.GetForegroundWindow.return_value = 0xF0F0

        ops = _make_ops()
        wm = MagicMock()
        with caplog.at_level(logging.DEBUG, logger=self._HWND_LOGGER):
            result = ops.verified_paste(
                "text", wm, target_control=self._failing_control(),
            )

        assert result is False
        mock_vpk.assert_not_called()
        assert any(
            "Could not resolve target HWND from control" in r.message
            for r in caplog.records
        ), "the resolution failure must name itself in the log"

    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.verified_press_keys", return_value=_FULL)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.pyperclip")
    def test_raw_paste_resolution_failure_logs_debug(
        self, mock_pyperclip, mock_time, mock_vpk, mock_win32gui, caplog,
    ):
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_win32gui.GetForegroundWindow.return_value = 0xF0F0

        ops = _make_ops()
        wm = MagicMock()
        with caplog.at_level(logging.DEBUG, logger=self._HWND_LOGGER):
            result = ops._raw_paste(
                "text", wm, target_control=self._failing_control(),
            )

        assert result is False
        mock_vpk.assert_not_called()
        assert any(
            "Could not resolve target HWND from control" in r.message
            for r in caplog.records
        ), "the resolution failure must name itself in the log"
