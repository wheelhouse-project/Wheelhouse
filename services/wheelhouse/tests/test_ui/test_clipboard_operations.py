"""Tests for ClipboardOperations - clipboard-based UI operations.

Covers:
- Construction and timing config parsing
- Safe clipboard wrappers (_safe_copy, _safe_paste)
- Verified paste (copy, verify loop, focus restore, paste dispatch)
- Selection clearing (sentinel detection, Flutter/standard paths)
- Context gathering (before/after cursor, sentinel-based detection)
- Adversarial: clipboard locked, empty/huge content, rapid cycles
"""
import pytest
import time
from unittest.mock import MagicMock, patch, call

_MOD = "ui.clipboard_operations"


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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_happy_path_standard_app(self, mock_pyperclip, mock_press_keys, mock_time):
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
        mock_press_keys.assert_called_once_with('ctrl', 'v')
        wm.ensure_focused.assert_called_once_with(12345)

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_copy_failure_aborts(self, mock_pyperclip, mock_press_keys, mock_time):
        """If _safe_copy fails, paste should abort and return False."""
        mock_time.perf_counter.return_value = 0.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")

        ops = _make_ops()
        result = ops.verified_paste("text", MagicMock())

        assert result is False
        mock_press_keys.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_log_lines_do_not_contain_text_content(
        self, mock_pyperclip, mock_press_keys, mock_time, caplog,
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_verification_timeout(self, mock_pyperclip, mock_press_keys, mock_time):
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
        mock_press_keys.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_verification_retry_then_success(self, mock_pyperclip, mock_press_keys, mock_time):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_clipboard_read_failure_during_verify_retries(self, mock_pyperclip, mock_press_keys, mock_time):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_paste_uses_sendkeys(self, mock_pyperclip, mock_press_keys, mock_time):
        """Flutter control should use SendKeys instead of press_keys."""
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
        mock_press_keys.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_skips_focus_restoration(self, mock_pyperclip, mock_press_keys, mock_time):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_focus_restore_no_hwnd(self, mock_pyperclip, mock_press_keys, mock_time):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_focus_restore_with_target_control(self, mock_pyperclip, mock_press_keys, mock_time):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_setfocus_exception_doesnt_abort(self, mock_pyperclip, mock_press_keys, mock_time):
        """SetFocus failure should be caught, paste continues."""
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
        mock_press_keys.assert_called_once_with('ctrl', 'v')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_get_target_window_exception_doesnt_abort(self, mock_pyperclip, mock_press_keys, mock_time):
        """get_target_window failure should be caught, paste continues."""
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
        mock_press_keys.assert_called_once_with('ctrl', 'v')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_exists_false_falls_back_to_press_keys(self, mock_pyperclip, mock_press_keys, mock_time):
        """If flutter_control.Exists() is False, fall back to press_keys."""
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()

        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"

        flutter = MagicMock()
        flutter.Exists.return_value = False

        wm = MagicMock()
        # flutter_control present means focus restore is skipped,
        # but Exists=False means press_keys is used for paste
        ops = _make_ops()
        result = ops.verified_paste("text", wm, flutter_control=flutter)

        assert result is True
        mock_press_keys.assert_called_once_with('ctrl', 'v')
        flutter.SendKeys.assert_not_called()

    # -- wh-d43oi: paste provenance flags --
    # The retraction/selection-restore policy needs to know two things about
    # the most recent paste: was it optimistic (clipboard unverified due to
    # lock contention, copy already succeeded), and did the ctrl+v keystroke
    # actually fire. The flags are reset at the start of every verified_paste
    # call so a previous paste's state does not leak forward.

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_sent_flag_false_on_copy_failure(self, mock_pyperclip, mock_press_keys, mock_time):
        """Copy failure: returns False, last_paste_was_sent stays False."""
        mock_time.perf_counter.return_value = 0.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")
        ops = _make_ops()
        ops.last_paste_was_sent = True  # stale value from a prior paste
        result = ops.verified_paste("text", MagicMock())
        assert result is False
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_sent_flag_false_on_wrong_content_verification_failure(
        self, mock_pyperclip, mock_press_keys, mock_time
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
        mock_press_keys.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_sent_flag_true_after_press_keys_success(
        self, mock_pyperclip, mock_press_keys, mock_time
    ):
        """press_keys fires and verification succeeded: last_paste_was_sent True."""
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_sent_flag_true_after_flutter_sendkeys_success(
        self, mock_pyperclip, mock_press_keys, mock_time
    ):
        """Flutter SendKeys fires and verification succeeded: last_paste_was_sent True.

        Directly verifies the contract that the flag is set BEFORE the
        Flutter-vs-non-Flutter dispatch, not only before press_keys.
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
        mock_press_keys.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_sent_flag_reset_at_start_of_every_call(
        self, mock_pyperclip, mock_press_keys, mock_time
    ):
        """A stale True from an earlier paste must not leak into a copy-fail call."""
        mock_time.perf_counter.return_value = 0.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")
        ops = _make_ops()
        ops.last_paste_was_sent = True
        ops.verified_paste("text", MagicMock())
        assert ops.last_paste_was_sent is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_optimistic_flag_true_on_pure_lock_contention(
        self, mock_pyperclip, mock_press_keys, mock_time
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_optimistic_flag_false_on_normal_verified_success(
        self, mock_pyperclip, mock_press_keys, mock_time
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_optimistic_flag_reset_at_start_of_every_call(
        self, mock_pyperclip, mock_press_keys, mock_time
    ):
        """A stale True from a previous optimistic call does not leak into a copy-fail call."""
        mock_time.perf_counter.return_value = 0.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")
        ops = _make_ops()
        ops.last_paste_was_optimistic = True
        ops.verified_paste("text", MagicMock())
        assert ops.last_paste_was_optimistic is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_post_paste_delay_applied(self, mock_pyperclip, mock_press_keys, mock_time):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_explicit_target_hwnd_used_for_focus_skips_get_target_window(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui, _mock_norm
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_post_paste_check_passes_when_foreground_matches(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui, _mock_norm
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_post_paste_check_fails_on_focus_drift(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui, _mock_norm
    ):
        """Foreground HWND drifted away from target_hwnd between SetFocus and
        Ctrl+V (e.g. an alert grabbed focus): refuse to credit the counter
        and return False so retract gating sees the failure.
        """
        times = iter([0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007])
        mock_time.perf_counter.side_effect = lambda: next(times)
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy = MagicMock()
        mock_pyperclip.paste.return_value = "text"
        # The foreground at post-paste check time is a DIFFERENT window.
        mock_win32gui.GetForegroundWindow.return_value = 7777

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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_legacy_caller_skips_post_paste_check(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_explicit_target_overrides_focus_drift_at_paste_time(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui, _mock_norm
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_chromium_child_target_with_root_foreground_succeeds(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_get_foreground_window_exception_returns_false_no_fail_open(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_target_hwnd_normalize_failure_returns_false(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_observed_foreground_normalize_failure_returns_false(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui
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

    @patch(f"{_MOD}.hwnds_match_for_foreground_compare")
    @patch(f"{_MOD}.process_name_for_hwnd")
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_brave_same_process_drift_passes_check(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui,
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
        mock_process_name.return_value = "brave.exe"
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
        # Helper called with allow_same_process=True and the brave.exe
        # process name so non-browser apps cannot accidentally use this path.
        mock_hwnds_match.assert_called_once()
        kwargs = mock_hwnds_match.call_args.kwargs
        assert kwargs["allow_same_process"] is True
        assert kwargs["expected_process_name"] == "brave.exe"

    @patch(f"{_MOD}.hwnds_match_for_foreground_compare")
    @patch(f"{_MOD}.process_name_for_hwnd")
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_non_browser_root_mismatch_still_fails(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui,
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
        mock_process_name.return_value = "notepad.exe"

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

    @patch(f"{_MOD}.hwnds_match_for_foreground_compare")
    @patch(f"{_MOD}.process_name_for_hwnd")
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare", side_effect=lambda h: h if h else None)
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_chromium_helper_returns_false_still_fails(
        self, mock_pyperclip, mock_press_keys, mock_time, mock_win32gui,
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
        mock_process_name.return_value = "chrome.exe"
        mock_hwnds_match.return_value = False

        wm = MagicMock()
        ops = _make_ops()
        before = ops.accumulated_paste_chars
        result = ops.verified_paste("text", wm, target_hwnd=9999)

        assert result is False
        assert ops.accumulated_paste_chars == before
        mock_hwnds_match.assert_called_once()


# ===========================================================================
# Clear Selection
# ===========================================================================

class TestClearSelection:
    """ClipboardOperations.clear_selection - sentinel-based selection clearing."""

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_no_selection_detected(self, mock_pyperclip, mock_press_keys, mock_time):
        """When clipboard returns sentinel, no selection exists."""
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()
        sentinel = "__SENTINEL_SEL_12345.0__"

        mock_pyperclip.paste.return_value = sentinel

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is True
        # Ctrl+C should be sent, but Delete should NOT
        mock_press_keys.assert_called_once_with('ctrl', 'c')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_selection_detected_and_deleted(self, mock_pyperclip, mock_press_keys, mock_time):
        """When clipboard differs from sentinel, selection is deleted."""
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()

        mock_pyperclip.paste.return_value = "selected text"

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is True
        # Should send Ctrl+C then Delete
        calls = mock_press_keys.call_args_list
        assert calls[0] == call('ctrl', 'c')
        assert calls[1] == call('delete')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_empty_selection_not_deleted(self, mock_pyperclip, mock_press_keys, mock_time):
        """Empty string selection (falsy) should NOT trigger delete."""
        mock_time.time.return_value = 12345.0
        mock_time.sleep = MagicMock()

        # Clipboard changed from sentinel to empty string
        mock_pyperclip.paste.return_value = ""

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is True
        # Only Ctrl+C, no Delete (empty string is falsy)
        assert mock_press_keys.call_count == 1
        mock_press_keys.assert_called_with('ctrl', 'c')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_uses_sendkeys(self, mock_pyperclip, mock_press_keys, mock_time):
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
        mock_press_keys.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_exception_returns_false(self, mock_pyperclip, mock_press_keys, mock_time):
        """Exception during selection clearing should return False."""
        mock_time.time.return_value = 12345.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_no_selection_no_delete(self, mock_pyperclip, mock_press_keys, mock_time):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_selection_text_captured_on_detection(
        self, mock_pyperclip, mock_press_keys, mock_time
    ):
        mock_time.time.return_value = 1.0
        mock_time.sleep = MagicMock()
        mock_pyperclip.paste.return_value = "important user text"

        ops = _make_ops()
        ops.clear_selection()

        assert ops.last_cleared_selection == "important user text"

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_no_selection_resets_slot_to_none(
        self, mock_pyperclip, mock_press_keys, mock_time
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_empty_selection_does_not_capture(
        self, mock_pyperclip, mock_press_keys, mock_time
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
    the retract subsystem (wh-t81d9.5)."""

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_non_flutter_uses_press_keys(
        self, mock_pyperclip, mock_press_keys, mock_time
    ):
        mock_time.sleep = MagicMock()
        ops = _make_ops()
        wm = MagicMock()

        ops._raw_paste("restored text", wm, target_hwnd=0xABC)

        mock_pyperclip.copy.assert_called_with("restored text")
        mock_press_keys.assert_called_with('ctrl', 'v')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_uses_sendkeys_and_skips_focus(
        self, mock_pyperclip, mock_press_keys, mock_time
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
        mock_press_keys.assert_not_called()
        # Flutter path skips focus restoration
        wm.ensure_focused.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_focus_restored_via_window_manager(
        self, mock_pyperclip, mock_press_keys, mock_time
    ):
        mock_time.sleep = MagicMock()
        ops = _make_ops()
        wm = MagicMock()

        ops._raw_paste("text", wm, target_hwnd=0xCAFE)

        wm.ensure_focused.assert_called_with(0xCAFE)

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_copy_failure_returns_false_and_no_paste(
        self, mock_pyperclip, mock_press_keys, mock_time
    ):
        mock_time.sleep = MagicMock()
        mock_pyperclip.copy.side_effect = Exception("locked")

        ops = _make_ops()
        wm = MagicMock()

        ok = ops._raw_paste("x", wm)

        assert ok is False
        mock_press_keys.assert_not_called()


class TestRestoreClearedSelection:
    """ClipboardOperations.restore_cleared_selection -- the public entry
    point used by ClipboardFallbackStrategy on pre-send paste failure
    (wh-t81d9.5)."""

    def test_returns_false_when_nothing_to_restore(self):
        ops = _make_ops()
        ops.last_cleared_selection = None

        result = ops.restore_cleared_selection(MagicMock())

        assert result is False

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_returns_true_and_clears_slot_after_restore(
        self, mock_pyperclip, mock_press_keys, mock_time
    ):
        mock_time.sleep = MagicMock()
        ops = _make_ops()
        ops.last_cleared_selection = "saved text"
        wm = MagicMock()

        result = ops.restore_cleared_selection(wm, target_hwnd=0xABC)

        assert result is True
        assert ops.last_cleared_selection is None
        mock_press_keys.assert_called_with('ctrl', 'v')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_does_not_mutate_retract_accounting(
        self, mock_pyperclip, mock_press_keys, mock_time
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_branch_uses_sendkeys(
        self, mock_pyperclip, mock_press_keys, mock_time
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
        mock_press_keys.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_clears_slot_even_when_raw_paste_fails(
        self, mock_pyperclip, mock_press_keys, mock_time
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_full_context_both_directions(self, mock_pyperclip, mock_press_keys, mock_time, mock_seq, mock_wait):
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

        assert result == {'preceding_chars': 'ab', 'has_selection': False}

        # Verify arrow key movements: shift+left+left, ctrl+c, right,
        # then shift+right, ctrl+c, left
        calls = [c[0] for c in mock_press_keys.call_args_list]
        assert ('shift', 'left', 'left') in calls
        assert ('ctrl', 'c') in calls
        assert ('right',) in calls
        assert ('shift', 'right') in calls
        assert ('left',) in calls

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_beginning_of_document(self, mock_pyperclip, mock_press_keys, mock_time, mock_seq, mock_wait):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_end_of_document(self, mock_pyperclip, mock_press_keys, mock_time, mock_seq, mock_wait):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_empty_document(self, mock_pyperclip, mock_press_keys, mock_time, mock_seq, mock_wait):
        """Empty document: both sentinels unchanged."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_before = "__SENTINEL_B_1.0__"
        sentinel_after = "__SENTINEL_A_2.0__"

        mock_pyperclip.paste.side_effect = [sentinel_before, sentinel_after]

        ops = _make_ops()
        result = ops.gather_context()

        assert result == {'preceding_chars': '', 'has_selection': False}

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_flutter_uses_sendkeys(self, mock_pyperclip, mock_press_keys, mock_time):
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

        # Flutter should use SendKeys, not press_keys
        mock_press_keys.assert_not_called()
        sendkeys_calls = [c[0][0] for c in flutter.SendKeys.call_args_list]
        assert '{Shift}{Left}' in sendkeys_calls
        assert '{Ctrl}c' in sendkeys_calls
        assert '{Right}' in sendkeys_calls
        assert '{Shift}{Right}' in sendkeys_calls
        assert '{Left}' in sendkeys_calls

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_exception_returns_empty_context(self, mock_pyperclip, mock_press_keys, mock_time, mock_seq, mock_wait):
        """Any exception should return empty context dict."""
        mock_time.time.return_value = 1.0
        mock_pyperclip.copy.side_effect = Exception("clipboard locked")

        ops = _make_ops()
        result = ops.gather_context()

        assert result == {'preceding_chars': '', 'has_selection': False}

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_has_selection_always_false(self, mock_pyperclip, mock_press_keys, mock_time, mock_seq, mock_wait):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_before_text_found_resets_cursor(self, mock_pyperclip, mock_press_keys, mock_time, mock_seq, mock_wait):
        """When before-text found, right arrow resets cursor position."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_after = "__SENTINEL_A_2.0__"
        mock_pyperclip.paste.side_effect = ["xy", sentinel_after]

        ops = _make_ops()
        ops.gather_context()

        # Right arrow should be called to deselect and reposition
        calls = [c[0] for c in mock_press_keys.call_args_list]
        assert ('right',) in calls

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_no_before_text_skips_right_arrow(self, mock_pyperclip, mock_press_keys, mock_time, mock_seq, mock_wait):
        """When before-text not found (sentinel), right arrow is skipped."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_before = "__SENTINEL_B_1.0__"
        sentinel_after = "__SENTINEL_A_2.0__"
        mock_pyperclip.paste.side_effect = [sentinel_before, sentinel_after]

        ops = _make_ops()
        ops.gather_context()

        # No right arrow (no cursor repositioning needed)
        calls = [c[0] for c in mock_press_keys.call_args_list]
        # Should have shift+left+left, ctrl+c for before attempt,
        # then shift+right, ctrl+c for after attempt
        # But NO right or left for cursor reset
        assert ('right',) not in calls
        assert ('left',) not in calls

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_after_text_found_resets_cursor(self, mock_pyperclip, mock_press_keys, mock_time, mock_seq, mock_wait):
        """When after-text found, left arrow resets cursor position."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_before = "__SENTINEL_B_1.0__"
        mock_pyperclip.paste.side_effect = [sentinel_before, "z"]

        ops = _make_ops()
        ops.gather_context()

        calls = [c[0] for c in mock_press_keys.call_args_list]
        assert ('left',) in calls


# ===========================================================================
# Adversarial Tests
# ===========================================================================

class TestAdversarial:
    """Adversarial scenarios: clipboard contention, edge-case content."""

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_clipboard_locked_during_verified_paste(self, mock_pyperclip, mock_press_keys, mock_time):
        """Clipboard locked by another process during copy -> fails gracefully."""
        mock_time.perf_counter.return_value = 0.0
        mock_pyperclip.copy.side_effect = PermissionError("clipboard locked by another process")

        ops = _make_ops()
        result = ops.verified_paste("text", MagicMock())

        assert result is False
        mock_press_keys.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_huge_clipboard_content(self, mock_pyperclip, mock_press_keys, mock_time):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_empty_text_paste(self, mock_pyperclip, mock_press_keys, mock_time):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_clipboard_intermittent_failures_during_context(self, mock_pyperclip, mock_press_keys, mock_time, mock_seq, mock_wait):
        """Clipboard exceptions during gather_context return empty context."""
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

        assert result == {'preceding_chars': '', 'has_selection': False}

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_clear_selection_with_unicode_text(self, mock_pyperclip, mock_press_keys, mock_time):
        """Unicode selection content should be handled correctly."""
        mock_time.time.return_value = 1.0
        mock_time.sleep = MagicMock()

        mock_pyperclip.paste.return_value = "Hello World"  # unicode chars

        ops = _make_ops()
        result = ops.clear_selection()

        assert result is True
        # Delete should be called since selection was found
        calls = [c[0] for c in mock_press_keys.call_args_list]
        assert ('delete',) in calls

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_clipboard_content_changes_mid_verification(self, mock_pyperclip, mock_press_keys, mock_time):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_optimistic_paste_on_pure_lock_contention(self, mock_pyperclip, mock_press_keys, mock_time):
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
        mock_press_keys.assert_called_with('ctrl', 'v')

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_no_optimistic_paste_when_wrong_content_seen(self, mock_pyperclip, mock_press_keys, mock_time):
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
        mock_press_keys.assert_not_called()

    @patch(f"{_MOD}.time")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_re_copy_on_content_mismatch(self, mock_pyperclip, mock_press_keys, mock_time):
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_gather_context_flutter_exists_false(self, mock_pyperclip, mock_press_keys, mock_time):
        """Flutter control that doesn't exist should use press_keys path."""
        mock_time.time.side_effect = [1.0, 2.0]
        mock_time.sleep = MagicMock()

        sentinel_before = "__SENTINEL_B_1.0__"
        sentinel_after = "__SENTINEL_A_2.0__"
        mock_pyperclip.paste.side_effect = [sentinel_before, sentinel_after]

        flutter = MagicMock()
        flutter.Exists.return_value = False

        ops = _make_ops()
        result = ops.gather_context(flutter_control=flutter)

        # Should fall back to press_keys
        assert mock_press_keys.call_count > 0
        flutter.SendKeys.assert_not_called()


# ---------------------------------------------------------------------------
# Sequence Polling Integration
# ---------------------------------------------------------------------------

class TestGatherContextSequencePolling:
    """gather_context should use adaptive sequence polling for non-Flutter."""

    @patch(f"{_MOD}.wait_for_clipboard_write", return_value=True)
    @patch(f"{_MOD}.get_sequence_number", return_value=100)
    @patch(f"{_MOD}.pyperclip")
    @patch(f"{_MOD}.press_keys")
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
    @patch(f"{_MOD}.press_keys")
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
    @patch(f"{_MOD}.press_keys")
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
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.pyperclip")
    def test_newlines_in_clipboard(self, mock_pyperclip, mock_press_keys, mock_time):
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
