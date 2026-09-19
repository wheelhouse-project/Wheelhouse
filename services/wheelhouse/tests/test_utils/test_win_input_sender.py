"""Tests for win_input_sender.py - Windows SendInput keyboard synthesis.

Tests cover:
- VK_CODE_MAP data integrity
- press_keys key sequence building and modifier ordering
- type_string character-to-event conversion and chunking
- Error handling for invalid keys
"""

from unittest.mock import Mock, patch, MagicMock, call
import ctypes
import logging
from ctypes import wintypes
import sys

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


# ---------------------------------------------------------------------------
# Extended-key flag tests (wh-arrow-keys-missing-extended-flag)
# ---------------------------------------------------------------------------


class TestExtendedKeyFlag:
    """_build_press_keys_events must set KEYEVENTF_EXTENDEDKEY.

    Windows gives the navigation cluster the E0 scan-code prefix. Without
    KEYEVENTF_EXTENDEDKEY, SendInput derives the NUMPAD scan code from the
    virtual key code instead. An application that reads the virtual key
    code sees the right key, but anything that reads the scan code sees a
    numpad key. Measured 2026-08-15 with NVDA 2026.1.1: NVDA named the
    arrow keys numpad2, numpad8, numpad4 and numpad6, bound them to its
    review cursor, and consumed all of them, so the keys never reached the
    application.

    These tests read the constructed event list. They never call SendInput,
    because a real injection would type into whatever window holds the
    focus on this shared desktop.
    """

    # Local copies of the Win32 values, so a wrong constant in the module
    # under test cannot make these tests agree with it.
    EXTENDED = 0x0001
    KEYUP = 0x0002

    @staticmethod
    def _events(*keys):
        """Return [(wVk, dwFlags), ...] for a chord, in send order."""
        from utils.win_input_sender import _build_press_keys_events

        events, count = _build_press_keys_events(tuple(keys))
        assert count == len(events)
        return [(e.ii.ki.wVk, e.ii.ki.dwFlags) for e in events]

    @pytest.mark.parametrize("key", ["up", "down", "left", "right"])
    def test_an_arrow_key_carries_the_flag_on_the_down_and_the_up(self, key):
        from utils.win_input_sender import VK_CODE_MAP

        vk = VK_CODE_MAP[key]
        assert self._events(key) == [
            (vk, self.EXTENDED),
            (vk, self.EXTENDED | self.KEYUP),
        ]

    @pytest.mark.parametrize(
        "key",
        ["home", "end", "pageup", "pagedown", "insert", "delete", "del",
         "printscreen"],
    )
    def test_every_other_extended_key_carries_the_flag(self, key):
        from utils.win_input_sender import VK_CODE_MAP

        vk = VK_CODE_MAP[key]
        assert self._events(key) == [
            (vk, self.EXTENDED),
            (vk, self.EXTENDED | self.KEYUP),
        ]

    @pytest.mark.parametrize(
        "key",
        ["a", "z", "0", "9", "enter", "tab", "space", "backspace", "esc",
         "f1", "f12", "ctrl", "shift", "alt", "win", "capslock", "pause",
         ".", "/"],
    )
    def test_an_ordinary_key_does_not_carry_the_flag(self, key):
        for _vk, flags in self._events(key):
            assert flags & self.EXTENDED == 0

    def test_a_chord_extends_only_the_arrow_and_not_the_modifier(self):
        from utils.win_input_sender import VK_CODE_MAP

        ctrl = VK_CODE_MAP["ctrl"]
        right = VK_CODE_MAP["right"]
        assert self._events("ctrl", "right") == [
            (ctrl, 0),
            (right, self.EXTENDED),
            (right, self.EXTENDED | self.KEYUP),
            (ctrl, self.KEYUP),
        ]

    def test_a_two_modifier_chord_keeps_its_release_order(self):
        from utils.win_input_sender import VK_CODE_MAP

        ctrl = VK_CODE_MAP["ctrl"]
        shift = VK_CODE_MAP["shift"]
        home = VK_CODE_MAP["home"]
        assert self._events("ctrl", "shift", "home") == [
            (ctrl, 0),
            (shift, 0),
            (home, self.EXTENDED),
            (home, self.EXTENDED | self.KEYUP),
            (shift, self.KEYUP),
            (ctrl, self.KEYUP),
        ]

    def test_the_set_holds_the_extended_codes_that_have_no_key_name_yet(self):
        """numlock, right ctrl, right alt and numpad divide are extended.

        None of the four is in VK_CODE_MAP today, so no chord can reach
        them. wh-voice-access-parity.2.17 adds the numpad names. Keying the
        set on the virtual key code rather than the key name means those
        four are already right when the names arrive.
        """
        from utils.win_input_sender import EXTENDED_VK_CODES

        assert 0x90 in EXTENDED_VK_CODES   # VK_NUMLOCK
        assert 0xA3 in EXTENDED_VK_CODES   # VK_RCONTROL
        assert 0xA5 in EXTENDED_VK_CODES   # VK_RMENU, right alt
        assert 0x6F in EXTENDED_VK_CODES   # VK_DIVIDE, numpad slash

    def test_the_set_excludes_every_numpad_key_that_is_not_extended(self):
        """The numpad digits and its other operators are NOT extended.

        Only the numpad divide and the numpad enter carry the E0 prefix.
        This test exists so wh-voice-access-parity.2.17 cannot add the
        numpad names and mark the whole block extended.
        """
        from utils.win_input_sender import EXTENDED_VK_CODES

        for vk in range(0x60, 0x6A):       # VK_NUMPAD0 to VK_NUMPAD9
            assert vk not in EXTENDED_VK_CODES
        for vk in (0x6A, 0x6B, 0x6D, 0x6E):  # multiply, add, subtract, decimal
            assert vk not in EXTENDED_VK_CODES

    def test_a_released_extended_key_keeps_the_flag_in_the_recovery_path(self):
        """_send_modifier_keyups must match the flag it released.

        The recovery path releases keys after a short SendInput. A key-up
        without the flag does not match the key-down that carried it, so a
        scan-code reader can hold the key down for ever.
        """
        from utils.win_input_sender import VK_CODE_MAP

        with patch("utils.win_input_sender.user32") as mock_user32, \
                patch("utils.win_input_sender.kernel32"):
            from utils.win_input_sender import _send_modifier_keyups

            mock_user32.SendInput.return_value = 2
            _send_modifier_keyups(("insert", "t"))

            array = mock_user32.SendInput.call_args[0][1]._obj
            sent = [(e.ii.ki.wVk, e.ii.ki.dwFlags) for e in array]

        assert sent == [
            (VK_CODE_MAP["t"], self.KEYUP),
            (VK_CODE_MAP["insert"], self.EXTENDED | self.KEYUP),
        ]


# ---------------------------------------------------------------------------
# Held-modifier tests (wh-arrow-keys-missing-extended-flag, second gap)
# ---------------------------------------------------------------------------


class TestHeldModifierKeys:
    """A chord must hold its modifier down while the other key is pressed.

    _build_press_keys_events held only ctrl, shift, alt and win, so
    press_keys('insert', 't') sent insert down, insert up, t down, t up --
    a sequence, not a chord. Measured 2026-08-15 with NVDA 2026.1.1: that
    call produced kb(desktop):t, and a hand-built chord that held insert
    down produced kb(desktop):NVDA+t. Insert and Caps Lock are the two keys
    NVDA accepts as its own modifier, so neither NVDA chord could be
    expressed at all.

    These tests read the constructed event list. They never call SendInput.
    """

    EXTENDED = 0x0001
    KEYUP = 0x0002

    @staticmethod
    def _events(*keys):
        from utils.win_input_sender import _build_press_keys_events

        events, count = _build_press_keys_events(tuple(keys))
        assert count == len(events)
        return [(e.ii.ki.wVk, e.ii.ki.dwFlags) for e in events]

    def test_insert_stays_down_while_the_other_key_is_pressed(self):
        from utils.win_input_sender import VK_CODE_MAP

        insert = VK_CODE_MAP["insert"]
        t = VK_CODE_MAP["t"]
        assert self._events("insert", "t") == [
            (insert, self.EXTENDED),
            (t, 0),
            (t, self.KEYUP),
            (insert, self.EXTENDED | self.KEYUP),
        ]

    def test_capslock_stays_down_while_the_other_key_is_pressed(self):
        from utils.win_input_sender import VK_CODE_MAP

        capslock = VK_CODE_MAP["capslock"]
        h = VK_CODE_MAP["h"]
        assert self._events("capslock", "h") == [
            (capslock, 0),
            (h, 0),
            (h, self.KEYUP),
            (capslock, self.KEYUP),
        ]

    def test_insert_with_an_older_modifier_keeps_the_release_order(self):
        from utils.win_input_sender import VK_CODE_MAP

        shift = VK_CODE_MAP["shift"]
        insert = VK_CODE_MAP["insert"]
        t = VK_CODE_MAP["t"]
        assert self._events("shift", "insert", "t") == [
            (shift, 0),
            (insert, self.EXTENDED),
            (t, 0),
            (t, self.KEYUP),
            (insert, self.EXTENDED | self.KEYUP),
            (shift, self.KEYUP),
        ]

    @pytest.mark.parametrize("key", ["insert", "capslock"])
    def test_the_new_modifier_alone_is_still_one_press_and_one_release(
        self, key,
    ):
        """A single key must not change shape because it can now be held."""
        from utils.win_input_sender import VK_CODE_MAP

        vk = VK_CODE_MAP[key]
        extended = self.EXTENDED if key == "insert" else 0
        assert self._events(key) == [
            (vk, extended),
            (vk, extended | self.KEYUP),
        ]

    def test_shift_insert_keeps_the_exact_order_it_had_before(self):
        """A chord in which both keys are now held modifiers.

        Shift and Insert both go down in the first pass and come up in
        reverse in the last, which is the same order the older code
        produced when only shift was held. No shipped command sends this
        shape: the 63 unique key lists in speech/config/patterns.toml
        were extracted and counted, and none of them holds insert or
        capslock. The test is a regression guard for the ordering, not a
        record of a shipped command.
        """
        from utils.win_input_sender import VK_CODE_MAP

        shift = VK_CODE_MAP["shift"]
        insert = VK_CODE_MAP["insert"]
        assert self._events("shift", "insert") == [
            (shift, 0),
            (insert, self.EXTENDED),
            (insert, self.EXTENDED | self.KEYUP),
            (shift, self.KEYUP),
        ]

    def test_an_ordinary_chord_is_untouched(self):
        from utils.win_input_sender import VK_CODE_MAP

        ctrl = VK_CODE_MAP["ctrl"]
        c = VK_CODE_MAP["c"]
        assert self._events("ctrl", "c") == [
            (ctrl, 0),
            (c, 0),
            (c, self.KEYUP),
            (ctrl, self.KEYUP),
        ]

    def test_two_ordinary_keys_stay_a_sequence(self):
        """Only a named modifier is held. Two ordinary keys still type.

        This is the guard against the wider rule of holding every key
        except the last. That rule would turn press_keys('a', 'b') into a
        chord and stop it typing 'ab'.
        """
        from utils.win_input_sender import VK_CODE_MAP

        a = VK_CODE_MAP["a"]
        b = VK_CODE_MAP["b"]
        assert self._events("a", "b") == [
            (a, 0),
            (a, self.KEYUP),
            (b, 0),
            (b, self.KEYUP),
        ]

    def test_the_held_set_names_every_modifier_and_nothing_else(self):
        from utils.win_input_sender import HELD_MODIFIER_VK_CODES, VK_CODE_MAP

        for key in ("ctrl", "shift", "alt", "win", "lwin", "insert",
                    "capslock"):
            assert VK_CODE_MAP[key] in HELD_MODIFIER_VK_CODES
        for key in ("a", "enter", "tab", "delete", "del", "up", "home",
                    "f1", "esc", "space"):
            assert VK_CODE_MAP[key] not in HELD_MODIFIER_VK_CODES


# ---------------------------------------------------------------------------
# root_window_at_point must leave the process-shared user32 alone
# (wh-number-badge-problems)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="drives the real user32 hit test")
class TestRootWindowAtPointLeavesTheSharedUser32Alone:
    """The click hit test must not leave a signature on ``ctypes.windll.user32``.

    ``ctypes.windll`` caches one ``WinDLL`` per library and one function
    object per name for the whole process, so a signature assigned to
    ``ctypes.windll.user32.GetAncestor`` applies to every caller. uiautomation
    2.0.29 ``GetAncestor`` passes ``ctypes.c_int(flag)`` as argument 2, and on
    Windows ``c_int`` is ``c_long``; against ``argtypes=[c_void_p, c_uint]``
    that raises ``ArgumentError: argument 2: TypeError: 'c_long' object cannot
    be interpreted as an integer`` -- the text in trace T-17881312777. Every
    later ``Control.GetTopLevelControl()`` in the Input process then fails
    until restart, and every text-insertion strategy refuses because none can
    resolve the target window. The same helper's ``WindowFromPoint`` signature
    breaks ``input_proc.py``'s click-target check, which passes a
    ``wintypes.POINT``.

    The ``saved_signatures`` fixture snapshots whatever signatures the
    process had before each test and restores them after, so the tests do
    not change the rest of the run. The snapshot is the reference: the
    claim is "unchanged", not "pristine", because uiautomation sets
    ``restype = c_void_p`` on the shared ``GetAncestor`` and
    ``WindowFromPoint`` at import (uiautomation.py lines 129 and 137), so
    any pytest process that imported it first is not pristine. The fixture
    deliberately does NOT clear the signatures first: a helper that
    pollutes the shared object at import time must be caught too, and a
    pre-cleared fixture would hide it.
    """

    _NAMES = ("GetAncestor", "WindowFromPoint")

    @pytest.fixture
    def saved_signatures(self):
        user32 = ctypes.windll.user32
        saved = {
            name: (getattr(user32, name).argtypes, getattr(user32, name).restype)
            for name in self._NAMES
        }
        try:
            yield saved
        finally:
            for name, (argtypes, restype) in saved.items():
                fn = getattr(user32, name)
                fn.argtypes = argtypes
                fn.restype = restype

    @pytest.fixture
    def shared_user32(self, saved_signatures):
        return ctypes.windll.user32

    def test_the_uiautomation_get_ancestor_call_form_still_works_after_a_hit_test(
        self, shared_user32,
    ):
        from utils.win_input_sender import root_window_at_point

        root = root_window_at_point(0, 0)
        try:
            # Exactly uiautomation.GetAncestor's call: a c_void_p handle and a
            # c_int flag (c_long on Windows) against the shared function.
            shared_user32.GetAncestor(ctypes.c_void_p(root), ctypes.c_int(2))
        except ctypes.ArgumentError as exc:
            pytest.fail(
                f"the hit test left a signature on the shared GetAncestor: {exc}"
            )

    def test_the_input_proc_window_from_point_call_form_still_works_after_a_hit_test(
        self, shared_user32,
    ):
        from utils.win_input_sender import root_window_at_point

        root_window_at_point(0, 0)
        try:
            # Exactly input_proc.py's on_user_click call: a wintypes.POINT
            # against the shared function.
            shared_user32.WindowFromPoint(wintypes.POINT(0, 0))
        except ctypes.ArgumentError as exc:
            pytest.fail(
                f"the hit test left a signature on the shared WindowFromPoint: {exc}"
            )

    def test_the_shared_function_objects_carry_no_signature_after_a_hit_test(
        self, shared_user32, saved_signatures,
    ):
        from utils.win_input_sender import root_window_at_point

        root_window_at_point(0, 0)
        for name in self._NAMES:
            fn = getattr(shared_user32, name)
            # argtypes is checked outright, not against the snapshot: the
            # module-level binding runs at import, before the snapshot, so
            # an import-time pollution would already be in the snapshot.
            # Nothing else sets argtypes on these two shared objects (grep
            # for ``user32.GetAncestor.argtypes`` and
            # ``user32.WindowFromPoint.argtypes`` over the service and
            # .venv/Lib/site-packages, 2026-09-01: only
            # scripts/probe_settle_events.py, which no test imports).
            assert fn.argtypes is None, name
            # restype must be unchanged, not pristine: see the class docstring.
            assert fn.restype is saved_signatures[name][1], name

    def test_the_hit_test_still_reports_the_root_window_at_the_point(self):
        # Stays green before and after the fix: the private signatures must
        # keep answering the same root pywin32 reports for the same point.
        win32gui = pytest.importorskip("win32gui")
        if isinstance(win32gui, MagicMock):
            pytest.skip("pywin32 is stubbed on this host")
        win32api = pytest.importorskip("win32api")
        from utils.win_input_sender import root_window_at_point

        def oracle(x, y):
            hwnd = win32gui.WindowFromPoint((x, y))
            return win32gui.GetAncestor(hwnd, 2) if hwnd else 0

        # A point some top-level window provably covers: the centre of the
        # foreground window, else the centre of the primary monitor. A fixed
        # (0, 0) is uncovered on a bare desktop (the desktop window's own
        # GA_ROOT is 0, and an uncovered point has no window at all), which
        # failed this test against correct code.
        candidates = []
        foreground = win32gui.GetForegroundWindow()
        if foreground:
            left, top, right, bottom = win32gui.GetWindowRect(foreground)
            candidates.append(((left + right) // 2, (top + bottom) // 2))
        candidates.append(
            (win32api.GetSystemMetrics(0) // 2, win32api.GetSystemMetrics(1) // 2)
        )
        for x, y in candidates:
            expected = oracle(x, y)
            if expected:
                break
        else:
            pytest.skip(f"no top-level window covers any of {candidates} here")
        actual = root_window_at_point(x, y)
        if actual != expected:
            # The screen can change between the two reads; one re-read
            # settles that race without loosening the comparison.
            expected = oracle(x, y)
        assert actual == expected


class TestCallerNotifiesLowersTheRefusalRecord:
    """wh-keyboard-refusal-notice: the keyword that removes the second box.

    ``ErrorNotificationHandler`` shows a generic ``[ERROR]`` box for every
    ERROR record. A caller that writes its own notice would give the user
    two boxes for one refusal, so it passes ``caller_notifies=True`` and the
    refusal record drops to WARNING. The refusal keeps its full detail in
    the log either way.

    The DEFAULT must stay ERROR. Fifteen call sites of the verified
    variants, and every ``press_keys`` caller, rely on that box today
    (boss e7 ruling A).
    """

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_a_default_short_send_still_logs_error(
        self, mock_kernel, mock_user32, caplog
    ):
        from utils.win_input_sender import verified_press_keys

        mock_user32.SendInput.return_value = 1  # one of two events
        mock_kernel.GetLastError.return_value = 0

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            ok, accepted, expected = verified_press_keys("a")

        assert ok is False
        assert [
            r.levelname for r in caplog.records if "short SendInput" in r.getMessage()
        ] == ["ERROR"]

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_caller_notifies_lowers_a_short_send_to_warning(
        self, mock_kernel, mock_user32, caplog
    ):
        from utils.win_input_sender import verified_press_keys

        mock_user32.SendInput.return_value = 1
        mock_kernel.GetLastError.return_value = 0

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            ok, accepted, expected = verified_press_keys(
                "a", caller_notifies=True
            )

        assert ok is False
        assert [
            r.levelname for r in caplog.records if "short SendInput" in r.getMessage()
        ] == ["WARNING"]

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_a_default_unknown_key_still_logs_error(
        self, mock_kernel, mock_user32, caplog
    ):
        from utils.win_input_sender import verified_press_keys

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            ok, accepted, expected = verified_press_keys("shhift")

        assert (ok, accepted, expected) == (False, 0, 0)
        assert [
            r.levelname for r in caplog.records if "are not valid" in r.getMessage()
        ] == ["ERROR"]

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_caller_notifies_lowers_an_unknown_key_to_warning(
        self, mock_kernel, mock_user32, caplog
    ):
        from utils.win_input_sender import verified_press_keys

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            ok, accepted, expected = verified_press_keys(
                "shhift", caller_notifies=True
            )

        assert (ok, accepted, expected) == (False, 0, 0)
        assert [
            r.levelname for r in caplog.records if "are not valid" in r.getMessage()
        ] == ["WARNING"]

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_press_keys_keeps_its_error_whatever_the_callers_do(
        self, mock_kernel, mock_user32, caplog
    ):
        """press_keys has no keyword and writes no notice of its own."""
        from utils.win_input_sender import press_keys

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            press_keys("shhift")

        assert [
            r.levelname for r in caplog.records if "are not valid" in r.getMessage()
        ] == ["ERROR"]

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_a_default_partial_typing_still_logs_error(
        self, mock_kernel, mock_user32, caplog
    ):
        from utils.win_input_sender import type_string_verified

        mock_user32.SendInput.return_value = 0
        mock_kernel.GetLastError.return_value = 5

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            ok, sent, error = type_string_verified("ab")

        assert ok is False
        assert [
            r.levelname for r in caplog.records
            if "type_string_verified" in r.getMessage()
        ] == ["ERROR"]

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_caller_notifies_lowers_partial_typing_to_warning(
        self, mock_kernel, mock_user32, caplog
    ):
        from utils.win_input_sender import type_string_verified

        mock_user32.SendInput.return_value = 0
        mock_kernel.GetLastError.return_value = 5

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            ok, sent, error = type_string_verified("ab", caller_notifies=True)

        assert ok is False
        assert [
            r.levelname for r in caplog.records
            if "type_string_verified" in r.getMessage()
        ] == ["WARNING"]

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_a_default_raising_send_still_logs_error(
        self, mock_kernel, mock_user32, caplog
    ):
        from utils.win_input_sender import verified_press_keys

        mock_user32.SendInput.side_effect = OSError("the desktop is gone")

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            ok, accepted, expected = verified_press_keys("a")

        assert ok is False
        assert [
            r.levelname for r in caplog.records
            if "SendInput raised" in r.getMessage()
        ] == ["ERROR"]

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_caller_notifies_lowers_a_raising_send_to_warning(
        self, mock_kernel, mock_user32, caplog
    ):
        from utils.win_input_sender import verified_press_keys

        mock_user32.SendInput.side_effect = OSError("the desktop is gone")

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            ok, accepted, expected = verified_press_keys(
                "a", caller_notifies=True
            )

        assert ok is False
        assert [
            r.levelname for r in caplog.records
            if "SendInput raised" in r.getMessage()
        ] == ["WARNING"]

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_a_default_raising_typing_still_logs_error(
        self, mock_kernel, mock_user32, caplog
    ):
        from utils.win_input_sender import type_string_verified

        mock_user32.SendInput.side_effect = OSError("the desktop is gone")

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            ok, sent, error = type_string_verified("ab")

        assert ok is False
        assert [
            r.levelname for r in caplog.records
            if "sendinput exception" in r.getMessage()
        ] == ["ERROR"]

    @patch("utils.win_input_sender.user32")
    @patch("utils.win_input_sender.kernel32")
    def test_caller_notifies_lowers_raising_typing_to_warning(
        self, mock_kernel, mock_user32, caplog
    ):
        from utils.win_input_sender import type_string_verified

        mock_user32.SendInput.side_effect = OSError("the desktop is gone")

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            ok, sent, error = type_string_verified("ab", caller_notifies=True)

        assert ok is False
        assert [
            r.levelname for r in caplog.records
            if "sendinput exception" in r.getMessage()
        ] == ["WARNING"]
