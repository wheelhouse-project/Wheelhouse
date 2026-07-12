"""Tests for win_input_sender.py - Windows SendInput keyboard synthesis.

Tests cover:
- VK_CODE_MAP data integrity
- press_keys key sequence building and modifier ordering
- type_string character-to-event conversion and chunking
- Error handling for invalid keys
"""

from unittest.mock import Mock, patch, MagicMock, call
import ctypes

import pytest


# ---------------------------------------------------------------------------
# VK_CODE_MAP tests
# ---------------------------------------------------------------------------


class TestVkCodeMap:
    """Tests for the virtual key code mapping table."""

    def test_common_keys_present(self):
        from utils.win_input_sender import VK_CODE_MAP

        expected_keys = [
            "enter", "tab", "backspace", "space", "esc",
            "ctrl", "shift", "alt", "win",
            "left", "right", "up", "down",
            "delete", "del", "home", "end",
            "pageup", "pagedown",
        ]
        for key in expected_keys:
            assert key in VK_CODE_MAP, f"Missing key: {key}"

    def test_alphanumeric_keys(self):
        from utils.win_input_sender import VK_CODE_MAP

        for char in "abcdefghijklmnopqrstuvwxyz":
            assert char in VK_CODE_MAP, f"Missing letter: {char}"
        for digit in "0123456789":
            assert digit in VK_CODE_MAP, f"Missing digit: {digit}"

    def test_function_keys(self):
        from utils.win_input_sender import VK_CODE_MAP

        for i in range(1, 13):
            key = f"f{i}"
            assert key in VK_CODE_MAP, f"Missing function key: {key}"

    def test_del_and_delete_same_code(self):
        from utils.win_input_sender import VK_CODE_MAP

        assert VK_CODE_MAP["del"] == VK_CODE_MAP["delete"]

    def test_all_values_are_ints(self):
        from utils.win_input_sender import VK_CODE_MAP

        for key, code in VK_CODE_MAP.items():
            assert isinstance(code, int), f"VK code for '{key}' is not int: {type(code)}"

    def test_punctuation_keys(self):
        from utils.win_input_sender import VK_CODE_MAP

        punctuation = [";", ":", "/", "?", "`", "~", "[", "]", "\\", "|", "'", '"', ",", ".", "<", ">", "=", "+", "-", "_"]
        for p in punctuation:
            assert p in VK_CODE_MAP, f"Missing punctuation: {p}"


# ---------------------------------------------------------------------------
# press_keys tests
# ---------------------------------------------------------------------------


class TestPressKeys:
    """Tests for keyboard hotkey synthesis."""

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_empty_keys_returns_immediately(self, mock_kernel, mock_user32):
        from utils.win_input_sender import press_keys

        press_keys()
        mock_user32.SendInput.assert_not_called()

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_single_key_sends_press_and_release(self, mock_kernel, mock_user32):
        from utils.win_input_sender import press_keys

        mock_user32.SendInput.return_value = 2  # 2 events sent
        press_keys("a")
        mock_user32.SendInput.assert_called_once()
        # Should send 2 events: key down + key up
        args = mock_user32.SendInput.call_args
        assert args[0][0] == 2  # num_events

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_modifier_plus_key_sends_correct_count(self, mock_kernel, mock_user32):
        from utils.win_input_sender import press_keys

        # ctrl+c: ctrl_down, c_down, c_up, ctrl_up = 4 events
        mock_user32.SendInput.return_value = 4
        press_keys("ctrl", "c")
        args = mock_user32.SendInput.call_args
        assert args[0][0] == 4

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_multiple_modifiers(self, mock_kernel, mock_user32):
        from utils.win_input_sender import press_keys

        # ctrl+shift+a: ctrl_down, shift_down, a_down, a_up, shift_up, ctrl_up = 6
        mock_user32.SendInput.return_value = 6
        press_keys("ctrl", "shift", "a")
        args = mock_user32.SendInput.call_args
        assert args[0][0] == 6

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_invalid_key_aborts(self, mock_kernel, mock_user32):
        from utils.win_input_sender import press_keys

        press_keys("nonexistent_key")
        mock_user32.SendInput.assert_not_called()

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_mixed_valid_invalid_aborts(self, mock_kernel, mock_user32):
        from utils.win_input_sender import press_keys

        press_keys("ctrl", "badkey")
        mock_user32.SendInput.assert_not_called()

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_case_insensitive_keys(self, mock_kernel, mock_user32):
        from utils.win_input_sender import press_keys

        mock_user32.SendInput.return_value = 2
        press_keys("A")  # Should work, lowercased internally
        mock_user32.SendInput.assert_called_once()

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_sendinput_partial_failure_logs(self, mock_kernel, mock_user32):
        from utils.win_input_sender import press_keys

        mock_user32.SendInput.return_value = 0  # No events sent
        mock_kernel.GetLastError.return_value = 5  # Access denied
        # Should not raise, just log
        press_keys("a")

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_exception_during_send_logs(self, mock_kernel, mock_user32):
        from utils.win_input_sender import press_keys

        mock_user32.SendInput.side_effect = OSError("SendInput failed")
        # Should not raise
        press_keys("a")


# ---------------------------------------------------------------------------
# verified_press_keys tests (wh-eolas.1.2)
# ---------------------------------------------------------------------------


class TestVerifiedPressKeys:
    """Tests for the verified-delivery press_keys variant.

    verified_press_keys returns ``(success, accepted, expected)``. The
    GUI terminal-paste helper uses it to fail closed when SendInput
    accepts fewer events than the chord required -- a partial Ctrl+V
    followed by Enter would submit unintended shell content.
    """

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_empty_keys_returns_success_zero(self, mock_kernel, mock_user32):
        from utils.win_input_sender import verified_press_keys

        success, accepted, expected = verified_press_keys()
        assert success is True
        assert accepted == 0
        assert expected == 0
        mock_user32.SendInput.assert_not_called()

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_full_delivery_returns_success(self, mock_kernel, mock_user32):
        from utils.win_input_sender import verified_press_keys

        # ctrl+v: ctrl_down, v_down, v_up, ctrl_up = 4
        mock_user32.SendInput.return_value = 4
        success, accepted, expected = verified_press_keys("ctrl", "v")
        assert success is True
        assert accepted == 4
        assert expected == 4

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_partial_delivery_returns_failure(self, mock_kernel, mock_user32):
        from utils.win_input_sender import verified_press_keys

        # SendInput inserted only 2 of the 4 events.
        mock_user32.SendInput.return_value = 2
        mock_kernel.GetLastError.return_value = 5
        success, accepted, expected = verified_press_keys("ctrl", "v")
        assert success is False
        assert accepted == 2
        assert expected == 4

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_zero_delivery_returns_failure(self, mock_kernel, mock_user32):
        from utils.win_input_sender import verified_press_keys

        mock_user32.SendInput.return_value = 0
        mock_kernel.GetLastError.return_value = 5
        success, accepted, expected = verified_press_keys("enter")
        assert success is False
        assert accepted == 0
        assert expected == 2  # enter down + up

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_exception_returns_failure(self, mock_kernel, mock_user32):
        from utils.win_input_sender import verified_press_keys

        mock_user32.SendInput.side_effect = OSError("synthetic")
        success, accepted, expected = verified_press_keys("ctrl", "v")
        assert success is False
        assert accepted == 0
        assert expected == 4  # ctrl_down, v_down, v_up, ctrl_up

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_invalid_key_returns_failure_with_zero_expected(
        self, mock_kernel, mock_user32,
    ):
        from utils.win_input_sender import verified_press_keys

        success, accepted, expected = verified_press_keys("not_a_key")
        assert success is False
        assert accepted == 0
        assert expected == 0
        mock_user32.SendInput.assert_not_called()


# ---------------------------------------------------------------------------
# type_string tests
# ---------------------------------------------------------------------------


class TestTypeString:
    """Tests for Unicode text typing synthesis."""

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_empty_string_returns_immediately(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string

        type_string("")
        mock_user32.SendInput.assert_not_called()

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_single_char_sends_events(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string

        mock_user32.SendInput.return_value = 2
        type_string("x")
        # One char = 2 events (unicode down + unicode up), one chunk
        mock_user32.SendInput.assert_called_once()
        args = mock_user32.SendInput.call_args
        assert args[0][0] == 2

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_newline_uses_vk_enter(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string, VK_CODE_MAP

        mock_user32.SendInput.return_value = 2
        type_string("\n")
        # Enter key down + up = 2 events
        mock_user32.SendInput.assert_called_once()

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_tab_uses_vk_tab(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string

        mock_user32.SendInput.return_value = 2
        type_string("\t")
        mock_user32.SendInput.assert_called_once()

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_long_string_chunks(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string

        # 10 chars = 20 events, chunk size 8, so 3 chunks (8+8+4)
        mock_user32.SendInput.return_value = 8
        type_string("abcdefghij")
        assert mock_user32.SendInput.call_count == 3

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_chunk_delay_applied(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string

        # Return value must match num_events in each chunk to avoid break
        mock_user32.SendInput.side_effect = lambda n, *a: n
        type_string("abcde", chunk_delay=0.05)
        # 5 chars = 10 events, chunk 8, so 2 chunks -> 2 sleeps
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(0.05)

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_sendinput_failure_breaks_loop(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string

        # First chunk fails
        mock_user32.SendInput.return_value = 0
        mock_kernel.GetLastError.return_value = 5
        type_string("abcdefghijklmnop")  # 16 chars, would be 4 chunks
        # Should stop after first failed chunk
        assert mock_user32.SendInput.call_count == 1

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_unicode_characters(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string

        mock_user32.SendInput.return_value = 2
        # Should handle non-ASCII via KEYEVENTF_UNICODE
        type_string("a")  # Basic test that unicode path works
        mock_user32.SendInput.assert_called_once()


# ---------------------------------------------------------------------------
# type_string_verified tests (wh-jmt5x)
# ---------------------------------------------------------------------------


class TestTypeStringVerified:
    """type_string_verified returns (success, chars_sent, error) so callers
    (VerifiedUnicodeStrategy, raw_insert_text routing) can detect partial
    SendInput delivery and Win32 failures instead of silently breaking."""

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_empty_string_returns_success_zero_chars(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string_verified

        success, chars_sent, error = type_string_verified("")
        assert success is True
        assert chars_sent == 0
        assert error is None
        mock_user32.SendInput.assert_not_called()

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_single_char_full_success(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string_verified

        # 1 char -> 2 events, single chunk
        mock_user32.SendInput.return_value = 2
        success, chars_sent, error = type_string_verified("x")
        assert success is True
        assert chars_sent == 1
        assert error is None

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_multi_char_full_success_across_chunks(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string_verified

        # SendInput accepts every chunk fully (return value matches num_events)
        mock_user32.SendInput.side_effect = lambda n, *a: n
        success, chars_sent, error = type_string_verified("abcdefghij")  # 10 chars
        assert success is True
        assert chars_sent == 10
        assert error is None
        # 10 chars * 2 events = 20 events / chunk size 8 = 3 chunks
        assert mock_user32.SendInput.call_count == 3

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_partial_send_in_first_chunk_returns_failure(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string_verified

        # First chunk: 8 events expected, 5 accepted -> 2 complete chars (4 events)
        mock_user32.SendInput.return_value = 5
        mock_kernel.GetLastError.return_value = 0
        success, chars_sent, error = type_string_verified("abcdefgh")  # 8 chars, 16 events, 2 chunks
        assert success is False
        assert chars_sent == 2  # 5 events // 2 = 2 complete down/up pairs
        assert error is not None
        assert "partial" in error
        # Should stop after the first failed chunk -- no second chunk attempted
        assert mock_user32.SendInput.call_count == 1

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_zero_send_returns_failure_with_win32_error(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string_verified

        mock_user32.SendInput.return_value = 0
        mock_kernel.GetLastError.return_value = 5  # ERROR_ACCESS_DENIED
        success, chars_sent, error = type_string_verified("abc")
        assert success is False
        assert chars_sent == 0
        assert error is not None
        assert "5" in error  # Win32 error code surfaced

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_partial_in_second_chunk_counts_first_chunk_chars(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string_verified

        # First chunk (8 events) succeeds fully; second chunk partial
        mock_user32.SendInput.side_effect = [8, 4]
        mock_kernel.GetLastError.return_value = 0
        success, chars_sent, error = type_string_verified("abcdefgh")  # 8 chars, 2 chunks of 4 chars each
        assert success is False
        # 4 chars from chunk 1 (full) + 2 chars from chunk 2 (4 events accepted) = 6
        assert chars_sent == 6
        assert error is not None
        assert "partial" in error

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_special_chars_counted_per_character(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string_verified

        # Newline + tab + ASCII -- still 2 events per char, 3 chars total = 6 events, single chunk
        mock_user32.SendInput.return_value = 6
        success, chars_sent, error = type_string_verified("\n\ta")
        assert success is True
        assert chars_sent == 3
        assert error is None

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_chunk_delay_applied_between_chunks(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import type_string_verified

        mock_user32.SendInput.side_effect = lambda n, *a: n
        type_string_verified("abcde", chunk_delay=0.05)
        # 5 chars * 2 events = 10 events, chunk size 8 -> 2 chunks -> 2 sleeps
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(0.05)

    # ---- wh-3pw8.1 (Codex): SendInput exception returns failure tuple ----

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_sendinput_exception_in_first_chunk_returns_failure(
        self, mock_sleep, mock_kernel, mock_user32
    ):
        from utils.win_input_sender import type_string_verified

        mock_user32.SendInput.side_effect = OSError("SendInput access violation")
        success, chars_sent, error = type_string_verified("abcd")
        assert success is False
        assert chars_sent == 0
        assert error is not None
        assert "exception" in error.lower()
        assert "OSError" in error or "access violation" in error

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_sendinput_exception_after_first_chunk_preserves_prior_chars(
        self, mock_sleep, mock_kernel, mock_user32
    ):
        from utils.win_input_sender import type_string_verified

        # First chunk delivers all 8 events; second chunk raises.
        mock_user32.SendInput.side_effect = [8, OSError("SendInput failed")]
        success, chars_sent, error = type_string_verified("abcdefgh")
        assert success is False
        assert chars_sent == 4  # First chunk = 4 chars completed
        assert error is not None
        assert "exception" in error.lower()
        # Should not call SendInput a third time
        assert mock_user32.SendInput.call_count == 2

    # ---- wh-3pw8.2 (Codex): non-BMP Unicode via UTF-16 surrogate pairs ----

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_bmp_char_emits_one_down_up_pair(self, mock_sleep, mock_kernel, mock_user32):
        from utils.win_input_sender import _build_unicode_event_groups

        groups = _build_unicode_event_groups("a")
        assert len(groups) == 1
        assert len(groups[0]) == 2  # one down/up pair
        # wScan should hold ord('a') == 0x61
        assert groups[0][0].ii.ki.wScan == ord("a")

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_non_bmp_char_emits_surrogate_pair_events(
        self, mock_sleep, mock_kernel, mock_user32
    ):
        from utils.win_input_sender import _build_unicode_event_groups

        # U+1F600 grinning face emoji -> high surrogate D83D, low surrogate DE00
        groups = _build_unicode_event_groups("\U0001F600")
        assert len(groups) == 1
        assert len(groups[0]) == 4  # high down/up + low down/up
        scan_codes = [ev.ii.ki.wScan for ev in groups[0]]
        assert scan_codes[0] == 0xD83D  # high surrogate down
        assert scan_codes[1] == 0xD83D  # high surrogate up
        assert scan_codes[2] == 0xDE00  # low surrogate down
        assert scan_codes[3] == 0xDE00  # low surrogate up

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_non_bmp_char_full_success_counts_one_char(
        self, mock_sleep, mock_kernel, mock_user32
    ):
        from utils.win_input_sender import type_string_verified

        # Emoji = 4 events, fits in one 8-event chunk
        mock_user32.SendInput.return_value = 4
        success, chars_sent, error = type_string_verified("\U0001F600")
        assert success is True
        assert chars_sent == 1
        assert error is None

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_non_bmp_partial_send_within_surrogate_pair_does_not_claim_char(
        self, mock_sleep, mock_kernel, mock_user32
    ):
        from utils.win_input_sender import type_string_verified

        # Emoji = 4 events. SendInput accepts only 2 (high surrogate down/up).
        # Low surrogate did not land -> the character is incomplete.
        mock_user32.SendInput.return_value = 2
        mock_kernel.GetLastError.return_value = 0
        success, chars_sent, error = type_string_verified("\U0001F600")
        assert success is False
        assert chars_sent == 0  # No complete Python characters delivered
        assert error is not None

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_mixed_bmp_and_non_bmp_char_counting(
        self, mock_sleep, mock_kernel, mock_user32
    ):
        from utils.win_input_sender import type_string_verified

        # "a" (2 events) + emoji (4 events) = 6 events = one chunk
        mock_user32.SendInput.return_value = 6
        success, chars_sent, error = type_string_verified("a\U0001F600")
        assert success is True
        assert chars_sent == 2  # Two Python characters
        assert error is None

    # ---- wh-3pw8.3 (Codex): Win32 error code surfaced on partial sends ----

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_partial_send_includes_nonzero_win32_error_code(
        self, mock_sleep, mock_kernel, mock_user32
    ):
        from utils.win_input_sender import type_string_verified

        mock_user32.SendInput.return_value = 5
        mock_kernel.GetLastError.return_value = 5  # ERROR_ACCESS_DENIED
        success, chars_sent, error = type_string_verified("abcdefgh")
        assert success is False
        assert error is not None
        assert "partial" in error
        assert "win32 error 5" in error

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_partial_send_omits_win32_suffix_when_error_code_zero(
        self, mock_sleep, mock_kernel, mock_user32
    ):
        from utils.win_input_sender import type_string_verified

        mock_user32.SendInput.return_value = 5
        mock_kernel.GetLastError.return_value = 0
        success, chars_sent, error = type_string_verified("abcdefgh")
        assert success is False
        assert error is not None
        assert "partial" in error
        assert "win32 error" not in error  # No suffix when code is 0


# ---------------------------------------------------------------------------
# send_backspaces tests (wh-t81d9.1)
# ---------------------------------------------------------------------------


class TestSendBackspaces:
    """send_backspaces returns bool so retract() can refuse to claim
    success on partial SendInput delivery (wh-t81d9.1)."""

    def test_zero_count_is_noop_returns_true(self):
        from utils.win_input_sender import send_backspaces
        # No request, no failure -- caller sees True so retract logic
        # treats this as "nothing to do, succeeded".
        assert send_backspaces(0) is True
        assert send_backspaces(-3) is True

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_full_delivery_returns_true(self, mock_kernel, mock_user32):
        from utils.win_input_sender import send_backspaces

        # 5 backspaces -> 5 down + 5 up = 10 events
        mock_user32.SendInput.return_value = 10
        assert send_backspaces(5) is True

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_partial_delivery_returns_false(self, mock_kernel, mock_user32):
        from utils.win_input_sender import send_backspaces

        # 5 backspaces requested, only 6 events accepted (out of 10)
        mock_user32.SendInput.return_value = 6
        mock_kernel.GetLastError.return_value = 5
        assert send_backspaces(5) is False

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_zero_delivery_returns_false(self, mock_kernel, mock_user32):
        from utils.win_input_sender import send_backspaces

        mock_user32.SendInput.return_value = 0
        mock_kernel.GetLastError.return_value = 5
        assert send_backspaces(3) is False


# ---------------------------------------------------------------------------
# wh-trailing-corruption-instrument: modifier-state snapshot and chunk
# debug log on the happy path of type_string_verified.
# ---------------------------------------------------------------------------


class TestSnapshotModifierState:
    """snapshot_modifier_state() returns a loggable string describing the
    current GetAsyncKeyState bits for SHIFT, CTRL, ALT, LWIN, and CAPSLOCK.
    Used by VerifiedUnicodeStrategy to capture cold-keyboard-state evidence
    on each dispatch (wh-startup-trailing-corruption hypothesis 3)."""

    @patch("utils.win_input_sender.user32")
    def test_all_keys_up_reports_dashes(self, mock_user32):
        from utils.win_input_sender import snapshot_modifier_state

        mock_user32.GetAsyncKeyState.return_value = 0
        state = snapshot_modifier_state()
        assert "shift=-" in state
        assert "ctrl=-" in state
        assert "alt=-" in state
        assert "lwin=-" in state
        assert "caps=-" in state

    @patch("utils.win_input_sender.user32")
    def test_pressed_keys_report_down(self, mock_user32):
        from utils.win_input_sender import snapshot_modifier_state

        # 0x8000 = high bit set = currently pressed
        mock_user32.GetAsyncKeyState.return_value = 0x8000
        state = snapshot_modifier_state()
        assert "shift=down" in state
        assert "ctrl=down" in state

    @patch("utils.win_input_sender.user32")
    def test_recently_pressed_keys_report_recent(self, mock_user32):
        from utils.win_input_sender import snapshot_modifier_state

        # 0x0001 = low bit set, high bit clear = pressed since last call
        mock_user32.GetAsyncKeyState.return_value = 0x0001
        state = snapshot_modifier_state()
        assert "shift=recent" in state

    @patch("utils.win_input_sender.user32")
    def test_getasynckeystate_exception_yields_question_mark(self, mock_user32):
        from utils.win_input_sender import snapshot_modifier_state

        mock_user32.GetAsyncKeyState.side_effect = OSError("boom")
        # The diagnostic helper must never crash the dispatch path.
        state = snapshot_modifier_state()
        assert "shift=?" in state
        assert "ctrl=?" in state


class TestTypeStringVerifiedChunkDebugLog:
    """Happy-path chunk delivery logs at DEBUG so the user can verify
    every chunk was accepted when reproducing wh-startup-trailing-corruption.
    The bug surfaces as SendInput-clean delivery with corrupt on-screen
    text; the chunk log lets the user see whether one chunk's
    sent/expected pair drifted even when the overall return was True."""

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    @patch("utils.win_input_sender.time.sleep")
    def test_each_successful_chunk_emits_debug_log(
        self, mock_sleep, mock_kernel, mock_user32, caplog
    ):
        import logging

        from utils.win_input_sender import type_string_verified

        # 10 chars * 2 events = 20 events, 8-event chunks => 3 chunks.
        mock_user32.SendInput.side_effect = lambda n, *a: n
        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            success, chars_sent, error = type_string_verified("abcdefghij")
        assert success is True
        chunk_logs = [
            r for r in caplog.records
            if "chunk" in r.getMessage() and r.levelno == logging.DEBUG
        ]
        assert len(chunk_logs) == 3, (
            f"expected 3 chunk DEBUG logs, got {len(chunk_logs)}: "
            f"{[r.getMessage() for r in chunk_logs]}"
        )
        # Each log line includes sent and expected counts so a partial
        # delivery would be visible inside a chunk.
        for record in chunk_logs:
            msg = record.getMessage()
            assert "sent=" in msg
            assert "expected=" in msg
