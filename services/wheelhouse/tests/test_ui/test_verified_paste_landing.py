"""verified_paste checks that a Notepad paste reached the text (wh-dictation-paste-loss-load).

Measured 2026-10-19: Notepad (RichEditD2DPT) did not paste 10 of 1,161
dictated words, and verified_paste reported success for every one. These
tests drive the check that follows the Ctrl+V: the control's text length
must change; if it does not, verified_paste sends Ctrl+V once more; a
word that still does not arrive gets one WARNING line with the utterance
id and no retract credit. The boss ruling of 2026-10-19 21:35 sets the
conditions (one class only, no second paste on any unknown answer).

prepare_paste_check and wait_for_length_change are replaced here; their
own tests are in test_paste_landing.py.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from ui.paste_landing import LengthWait, PasteCheck

_MOD = "ui.clipboard_operations"
_FULL = (True, 6, 6)
_SHORT = (False, 3, 6)
_CHECK = PasteCheck(hwnd=777, length_before=100, root_hwnd=12345)


def _make_ops(**timing):
    from ui.clipboard_operations import ClipboardOperations
    base = {
        "clipboard_verification_timeout_ms": 250,
        "clipboard_operation_delay_ms": 50,
        "selection_clear_delay_ms": 20,
        "context_gather_delay_ms": 10,
        "post_paste_delay_ms": 30,
    }
    base.update(timing)
    ops = ClipboardOperations({"ui_actions": {"timing": base}})
    ops.current_utterance_id = 42
    return ops


@pytest.fixture
def env():
    """Patch every Win32 and clock call verified_paste makes."""
    with patch(f"{_MOD}.time") as mock_time, \
            patch(f"{_MOD}.pyperclip") as mock_clip, \
            patch(f"{_MOD}.verified_press_keys", create=True) as mock_vpk, \
            patch(f"{_MOD}._send_modifier_keyups", create=True) as mock_keyups, \
            patch(f"{_MOD}.prepare_paste_check") as mock_prepare, \
            patch(f"{_MOD}.wait_for_length_change") as mock_wait, \
            patch(f"{_MOD}.get_sequence_number") as mock_seq, \
            patch(f"{_MOD}.focused_control_hwnd") as mock_focus, \
            patch(f"{_MOD}.win32gui.GetForegroundWindow") as mock_fg, \
            patch(f"{_MOD}.normalize_hwnd_for_foreground_compare") as mock_root:
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        mock_clip.paste.return_value = "hello"
        mock_vpk.return_value = _FULL
        mock_prepare.return_value = (_CHECK, "ready")
        mock_seq.return_value = 500
        # The control that holds focus before the second Ctrl+V is the
        # watched one, and the foreground window is the checked window.
        mock_focus.return_value = _CHECK.hwnd
        # The foreground window 4321 is a child of the checked window 12345.
        # The real root lookup is replaced, and every other window is its
        # own root. The real comparison helper runs, so a test sees every
        # WARNING line it logs.
        roots = {4321: 12345}
        mock_fg.return_value = 4321
        mock_root.side_effect = lambda hwnd: roots.get(hwnd, hwnd)
        yield {
            "vpk": mock_vpk, "keyups": mock_keyups, "prepare": mock_prepare,
            "wait": mock_wait, "seq": mock_seq, "time": mock_time,
            "clip": mock_clip, "focus": mock_focus, "fg": mock_fg,
            "root": mock_root, "roots": roots,
        }


def _paste(ops, text="hello"):
    wm = MagicMock()
    wm.get_target_window.return_value = (12345, MagicMock())
    return ops.verified_paste(text, wm, target_class_name="RichEditD2DPT")


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_a_landed_paste_sends_one_ctrl_v_and_credits_once(env, caplog):
    env["wait"].return_value = LengthWait("changed", 105, 0.003)
    ops = _make_ops()
    with caplog.at_level(logging.INFO, logger=_MOD):
        assert _paste(ops) is True
    assert env["vpk"].call_count == 1
    assert env["wait"].call_count == 1
    assert ops.accumulated_paste_chars == 5
    env["prepare"].assert_called_once_with(12345, "RichEditD2DPT")
    # The load session reads this line for the time from Ctrl+V to the change.
    assert any("paste check: length changed" in m for m in caplog.messages)


def test_the_first_wait_uses_the_configured_landing_wait(env):
    env["wait"].return_value = LengthWait("changed", 105, 0.003)
    _paste(_make_ops(paste_landing_wait_ms=400))
    hwnd, length_before = env["wait"].call_args.args[:2]
    assert (hwnd, length_before) == (777, 100)
    assert env["wait"].call_args.kwargs["wait_s"] == pytest.approx(0.4)


def test_the_first_wait_counts_the_time_since_the_ctrl_v(env):
    # The 30 ms post-paste sleep already ran, so 470 ms of the 500 ms
    # landing wait remain for the first length read.
    clock = [0.0]
    env["time"].perf_counter.side_effect = lambda: clock[0]
    env["time"].sleep.side_effect = lambda s: clock.__setitem__(0, clock[0] + s)
    env["wait"].return_value = LengthWait("changed", 105, 0.003)
    _paste(_make_ops(paste_landing_wait_ms=500))
    assert env["wait"].call_args.kwargs["wait_s"] == pytest.approx(0.47)


def test_a_flutter_paste_gets_no_paste_check(env):
    # A real answer, so a check that runs anyway reaches the assertion
    # below instead of a crash on a mock length.
    env["wait"].return_value = LengthWait("changed", 105, 0.003)
    flutter = MagicMock()
    flutter.Exists.return_value = True
    wm = MagicMock()
    wm.get_target_window.return_value = (12345, MagicMock())
    ops = _make_ops()
    assert ops.verified_paste(
        "hello", wm, target_class_name="RichEditD2DPT", flutter_control=flutter,
    ) is True
    flutter.SendKeys.assert_any_call("{Ctrl}v")
    env["prepare"].assert_not_called()
    env["wait"].assert_not_called()
    assert ops.accumulated_paste_chars == 5


def test_an_unchanged_length_sends_ctrl_v_again(env, caplog):
    env["wait"].side_effect = [
        LengthWait("unchanged", 100, 0.5),
        LengthWait("changed", 105, 0.004),
    ]
    ops = _make_ops()
    assert _paste(ops) is True
    assert env["vpk"].call_count == 2
    assert ops.accumulated_paste_chars == 5
    assert any("pasting again" in m and "utterance=42" in m
               for m in _warnings(caplog))


def test_a_word_that_never_arrives_logs_one_warning_and_gets_no_credit(env, caplog):
    env["wait"].side_effect = [
        LengthWait("unchanged", 100, 0.5),
        LengthWait("unchanged", 100, 0.5),
    ]
    ops = _make_ops()
    assert _paste(ops) is True
    assert env["vpk"].call_count == 2
    assert ops.accumulated_paste_chars == 0
    lost = [m for m in _warnings(caplog) if "not typed" in m]
    assert len(lost) == 1
    assert "utterance=42" in lost[0]


def test_a_late_first_paste_is_reported_and_credited_twice(env, caplog):
    env["wait"].side_effect = [
        LengthWait("unchanged", 100, 0.5),
        LengthWait("changed", 110, 0.002),
    ]
    ops = _make_ops()
    assert _paste(ops) is True
    assert ops.accumulated_paste_chars == 10
    assert any("typed twice" in m and "utterance=42" in m
               for m in _warnings(caplog))


def _paste_with_identity(ops, identity):
    wm = MagicMock()
    wm.get_target_window.return_value = (12345, MagicMock())
    return ops.verified_paste(
        "hello", wm, target_class_name="RichEditD2DPT",
        target_identity=identity,
    )


def _unchanged_then_changed(env):
    env["wait"].side_effect = [
        LengthWait("unchanged", 100, 0.5),
        LengthWait("changed", 105, 0.004),
    ]


def _one_warning(caplog, *needles):
    """The only WARNING logged; it must name the utterance and each needle."""
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "utterance=42" in warnings[0]
    for needle in needles:
        assert needle in warnings[0], warnings[0]


def test_the_second_ctrl_v_reads_the_focused_control_of_the_checked_window(env):
    _unchanged_then_changed(env)
    assert _paste(_make_ops()) is True
    env["focus"].assert_called_once_with(12345)
    assert env["vpk"].call_count == 2


def test_a_moved_focus_means_no_second_paste_and_no_credit(env, caplog):
    _unchanged_then_changed(env)
    env["focus"].return_value = 888
    ops = _make_ops()
    assert _paste(ops) is True
    assert env["vpk"].call_count == 1
    assert ops.accumulated_paste_chars == 0
    env["wait"].assert_called_once()
    _one_warning(caplog, "focus", "no second paste")


def test_an_unreadable_focus_means_no_second_paste_and_no_credit(env, caplog):
    _unchanged_then_changed(env)
    env["focus"].return_value = 0
    ops = _make_ops()
    assert _paste(ops) is True
    assert env["vpk"].call_count == 1
    assert ops.accumulated_paste_chars == 0
    env["wait"].assert_called_once()
    _one_warning(caplog, "could not be read", "no second paste")


def test_without_a_target_identity_a_foreign_foreground_means_no_second_paste(
    env, caplog,
):
    _unchanged_then_changed(env)
    env["roots"][4321] = 9999
    ops = _make_ops()
    assert _paste(ops) is True
    assert env["vpk"].call_count == 1
    assert ops.accumulated_paste_chars == 0
    env["wait"].assert_called_once()
    _one_warning(caplog, "foreground", "no second paste")


def test_without_a_target_identity_a_foreground_with_no_root_means_no_second_paste(
    env, caplog,
):
    _unchanged_then_changed(env)
    env["roots"][4321] = None
    ops = _make_ops()
    assert _paste(ops) is True
    assert env["vpk"].call_count == 1
    assert ops.accumulated_paste_chars == 0
    _one_warning(caplog, "foreground", "no second paste")


def test_without_a_target_identity_the_check_window_is_compared_to_the_foreground(env):
    _unchanged_then_changed(env)
    _paste(_make_ops())
    # The checked window (12345), not the watched control (777), against the
    # foreground window that GetForegroundWindow returned.
    looked_up = {c.args[0] for c in env["root"].call_args_list}
    assert looked_up == {12345, 4321}


def test_without_a_target_identity_an_unreadable_foreground_means_no_second_paste(
    env, caplog,
):
    _unchanged_then_changed(env)
    env["fg"].side_effect = OSError("no foreground window")
    ops = _make_ops()
    assert _paste(ops) is True
    assert env["vpk"].call_count == 1
    assert ops.accumulated_paste_chars == 0
    _one_warning(caplog, "foreground", "no second paste")


def test_with_a_target_identity_the_identity_check_stands_in_for_the_foreground_read(env):
    _unchanged_then_changed(env)
    ops = _make_ops()
    with patch.object(ops, "_captured_identity_is_current", return_value=True):
        assert _paste_with_identity(ops, MagicMock()) is True
    assert env["vpk"].call_count == 2
    env["root"].assert_not_called()
    env["fg"].assert_not_called()


def _late_first_paste(env, caplog, text, growth):
    """Run a paste whose second wait sees ``growth`` units of new length.

    Returns (copies credited, whether "typed twice" was logged).
    """
    env["clip"].paste.return_value = text
    env["wait"].side_effect = [
        LengthWait("unchanged", 100, 0.5),
        LengthWait("changed", 100 + growth, 0.002),
    ]
    ops = _make_ops()
    assert _paste(ops, text) is True
    typed_twice = any("typed twice" in m for m in _warnings(caplog))
    return ops.accumulated_paste_chars // len(text), typed_twice


def test_an_emoji_word_with_the_growth_of_one_copy_is_credited_once(env, caplog):
    # One emoji is 1 Python character and 2 UTF-16 units: a growth of 2 is
    # ONE copy. The old rule (growth >= 2 x len(text) = 2) read it as two.
    credited, typed_twice = _late_first_paste(env, caplog, "\U0001F600", 2)
    assert credited == 1
    assert not typed_twice


def test_a_line_break_with_the_growth_of_one_copy_is_credited_once(env, caplog):
    # A rich edit control counts one "\n" as 2 units ("\r\n").
    credited, typed_twice = _late_first_paste(env, caplog, "\n", 2)
    assert credited == 1
    assert not typed_twice


def test_a_line_break_with_the_growth_of_two_copies_is_credited_twice(env, caplog):
    credited, typed_twice = _late_first_paste(env, caplog, "\n", 4)
    assert credited == 2
    assert typed_twice


def test_an_ascii_word_with_the_growth_of_two_copies_is_credited_twice(env, caplog):
    credited, typed_twice = _late_first_paste(env, caplog, "word", 8)
    assert credited == 2
    assert typed_twice


def test_a_failed_length_query_after_the_paste_means_no_second_paste(env, caplog):
    env["wait"].return_value = LengthWait("query_failed", None, 0.1)
    ops = _make_ops()
    assert _paste(ops) is True
    assert env["vpk"].call_count == 1
    assert ops.accumulated_paste_chars == 5
    assert sum("no second paste" in m for m in _warnings(caplog)) == 1


def test_a_failed_length_query_after_the_second_paste_is_not_a_lost_word(env, caplog):
    env["wait"].side_effect = [
        LengthWait("unchanged", 100, 0.5),
        LengthWait("query_failed", None, 0.1),
    ]
    ops = _make_ops()
    assert _paste(ops) is True
    assert env["vpk"].call_count == 2
    assert not any("not typed" in m for m in _warnings(caplog))


def test_no_check_means_no_wait_and_no_second_paste(env):
    env["prepare"].return_value = (None, "class_out_of_scope")
    ops = _make_ops()
    assert _paste(ops) is True
    assert env["vpk"].call_count == 1
    env["wait"].assert_not_called()
    assert ops.accumulated_paste_chars == 5


def test_a_changed_clipboard_means_no_second_paste(env, caplog):
    def another_program_writes_the_clipboard(*args, **kwargs):
        env["seq"].return_value = 501
        return LengthWait("unchanged", 100, 0.5)

    env["wait"].side_effect = another_program_writes_the_clipboard
    ops = _make_ops()
    assert _paste(ops) is True
    assert env["vpk"].call_count == 1
    assert ops.accumulated_paste_chars == 0
    assert any("clipboard changed" in m and "utterance=42" in m
               for m in _warnings(caplog))


def test_a_changed_target_means_no_second_paste(env, caplog):
    env["wait"].return_value = LengthWait("unchanged", 100, 0.5)
    ops = _make_ops()
    with patch.object(ops, "_captured_identity_is_current",
                      side_effect=[True, True, False]):
        assert _paste(ops) is True
    assert env["vpk"].call_count == 1
    assert ops.accumulated_paste_chars == 0


def test_a_short_second_ctrl_v_releases_ctrl_and_gets_no_credit(env, caplog):
    env["wait"].return_value = LengthWait("unchanged", 100, 0.5)
    env["vpk"].side_effect = [_FULL, _SHORT]
    ops = _make_ops()
    assert _paste(ops) is True
    env["keyups"].assert_called_once_with(("ctrl",))
    assert ops.accumulated_paste_chars == 0
    env["wait"].assert_called_once()


def test_start_utterance_gives_the_clipboard_the_utterance_id():
    from ui.ui_action_handler import UIActionHandler
    handler = MagicMock()
    UIActionHandler.start_utterance(handler, 116)
    assert handler.clipboard.current_utterance_id == 116
