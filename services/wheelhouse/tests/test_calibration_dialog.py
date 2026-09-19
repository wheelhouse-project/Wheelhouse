"""GUI tests for the voice-teaching (calibration) window (wh-7ou.7.3.1).

Spec: docs/superpowers/specs/2026-08-07-voice-calibration-design.md
Sections 3.1-3.7. The window renders whatever screen the latest
``cal_state`` payload names and holds no session logic of its own; every
user-facing string below is asserted VERBATIM against the spec (changing
one is a design change). Em dashes appear literally (they are CP1252-safe);
the CP1252-unsafe progress-dot and check-mark characters are built with
chr() so printing this file's source never crashes a CP1252 terminal.

Also covers the GuiManager wiring: the tray-menu items, the
``open_calibration`` / ``cal_state`` state-queue dispatch, and the
``cal_session_open`` command sent when the window opens -- mirroring the
Pattern Manager opening path.
"""
from __future__ import annotations

from queue import Empty
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QPushButton

# wh-pytest-flaky-segfault: constructing the dialog builds real Qt widgets;
# without a QApplication Qt aborts the whole interpreter. The session-scoped
# qapp fixture guarantees one exists even when this file runs alone.
pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------------------
# Exact shipped strings (spec Sections 3.1-3.6, copied verbatim)
# --------------------------------------------------------------------------

WRONG_PROVIDER_TEXT = (
    "Voice teaching works with the Distil-Whisper speech recognizer. "
    "You're currently using a different one, which doesn't need it. If "
    "you switch to Distil-Whisper later, come back here."
)
INTRO_TITLE = "Teach WheelHouse your voice"
INTRO_BODY = (
    "WheelHouse sometimes misses short words like comma. This short "
    "session teaches WheelHouse how you sound so it stops missing them. "
    "There's no time limit — go at your own pace. Ready?"
)
PTT_LINE = (
    "You're in push-to-talk mode — hold the talk button while you "
    "say each word."
)
SAY_THIS_WORD = "Say this word:"
GOT_IT = "Got it!"
TRY_AGAIN = "Let's try that one again."
NOISE_TEXT = (
    "Now cough or clear your throat. Do this three separate times, "
    "pausing between each one. This teaches WheelHouse the difference "
    "between your words and other sounds."
)
REVIEW_FULL = "Done! WheelHouse learned your voice. Apply the improvement?"
REVIEW_PARTIAL = (
    "Done! WheelHouse learned part of your voice. Your words and your "
    "other sounds were too similar to tell apart this time, so part of "
    "the tune-up was kept as it was. Trying again in a quieter room may "
    "help. Apply the part that worked?"
)
REVIEW_NOTHING = (
    "WheelHouse couldn't learn enough from this session. Nothing was "
    "changed. Trying again in a quieter room, or closer to the "
    "microphone, may help. Speaking in a normal tone of voice helps too "
    "— not too loud and not too soft."
)
APPLYING_TEXT = (
    "WheelHouse is restarting its listening — about ten seconds."
)
APPLIED_TEXT = "All set. Try saying comma into a text box."
SAVE_FAILED_TEXT = (
    "WheelHouse couldn't save your settings file, so nothing was "
    "changed. Your results are still here — choose Apply to try "
    "again. If it keeps failing, the usual causes are a full disk or "
    "another program blocking WheelHouse's files, such as antivirus or "
    "backup software. The exact error is under \"Show details.\""
)
RESTART_SLOW_TEXT = (
    "WheelHouse's speech recognizer is taking longer than expected to "
    "restart. It should recover on its own in a moment."
)
CANCELLED_TEXT = (
    "This teaching session has ended. Anything you say now types "
    "normally again. Say \"learn my voice\" any time to start a new "
    "session."
)
TRAY_ITEM_TEXT = "Teach WheelHouse your voice..."

FILLED_DOT = chr(0x25CF)  # black circle (filled progress dot)
EMPTY_DOT = chr(0x25CB)  # white circle (unfilled progress dot)
CHECK_MARK = chr(0x2713)  # check mark shown with "Got it!"


def _dots(filled: int, total: int) -> str:
    return " ".join([FILLED_DOT] * filled + [EMPTY_DOT] * (total - filled))


def _make_dialog():
    from calibration_dialog import CalibrationDialog
    return CalibrationDialog(parent=None)


def _collect(dialog):
    emitted = []
    dialog.calibration_action.connect(emitted.append)
    return emitted


# --------------------------------------------------------------------------
# Wrong-provider screen (Section 3.1)
# --------------------------------------------------------------------------

def test_wrong_provider_screen_shows_exact_notice():
    dialog = _make_dialog()
    dialog.handle_state({"screen": "wrong_provider"})
    assert dialog._stack.currentWidget() is dialog._page_wrong_provider
    assert dialog._wrong_provider_label.text() == WRONG_PROVIDER_TEXT
    assert dialog._wrong_provider_ok_btn.text() == "OK"


def test_wrong_provider_ok_emits_cal_cancel():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state({"screen": "wrong_provider"})
    dialog._wrong_provider_ok_btn.click()
    assert emitted == [{"action": "cal_cancel"}]


# --------------------------------------------------------------------------
# Intro screen (Section 3.2)
# --------------------------------------------------------------------------

def test_intro_screen_exact_strings_and_buttons():
    dialog = _make_dialog()
    dialog.handle_state({"screen": "intro", "push_to_talk": False})
    assert dialog._stack.currentWidget() is dialog._page_intro
    assert dialog._intro_title.text() == INTRO_TITLE
    assert dialog._intro_body.text() == INTRO_BODY
    assert dialog._start_btn.text() == "Start"
    assert dialog._intro_not_now_btn.text() == "Not now"
    assert dialog.windowTitle() == INTRO_TITLE


def test_intro_ptt_line_hidden_without_push_to_talk():
    dialog = _make_dialog()
    dialog.handle_state({"screen": "intro", "push_to_talk": False})
    assert not dialog._intro_ptt_line.isVisibleTo(dialog)
    # Missing key tolerated, treated as not push-to-talk.
    dialog.handle_state({"screen": "intro"})
    assert not dialog._intro_ptt_line.isVisibleTo(dialog)


def test_intro_ptt_line_shown_with_exact_text():
    dialog = _make_dialog()
    dialog.handle_state({"screen": "intro", "push_to_talk": True})
    assert dialog._intro_ptt_line.isVisibleTo(dialog)
    assert dialog._intro_ptt_line.text() == PTT_LINE


def test_intro_start_emits_cal_start():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state({"screen": "intro"})
    dialog._start_btn.click()
    assert emitted == [{"action": "cal_start"}]


def test_intro_not_now_emits_cal_cancel():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state({"screen": "intro"})
    dialog._intro_not_now_btn.click()
    assert emitted == [{"action": "cal_cancel"}]


# --------------------------------------------------------------------------
# Word screen (Section 3.3)
# --------------------------------------------------------------------------

def _word_state(**overrides):
    state = {
        "screen": "word",
        "word": "comma",
        "word_index": 1,
        "word_total": 4,
        "sample_count": 0,
        "sample_total": 5,
        "feedback": None,
    }
    state.update(overrides)
    return state


def test_word_screen_renders_instruction_word_progress_and_dots():
    dialog = _make_dialog()
    dialog.handle_state(_word_state(sample_count=2))
    assert dialog._stack.currentWidget() is dialog._page_word
    assert dialog._word_instruction.text() == SAY_THIS_WORD
    assert dialog._word_label.text() == "comma"
    assert dialog._word_progress.text() == "Word 1 of 4"
    assert dialog._word_dots.text() == _dots(2, 5)


def test_word_label_is_very_large():
    dialog = _make_dialog()
    dialog.handle_state(_word_state())
    assert dialog._word_label.font().pointSize() >= 24


def test_word_dots_only_advance_from_state_updates():
    dialog = _make_dialog()
    dialog.handle_state(_word_state(sample_count=1))
    assert dialog._word_dots.text() == _dots(1, 5)
    # Nothing may advance the dots on its own: the dialog owns no timers
    # at all, and processing pending events leaves the dots untouched.
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    assert dialog._word_dots.text() == _dots(1, 5)
    assert dialog.findChildren(QTimer) == []
    # A new state is the only thing that moves them.
    dialog.handle_state(_word_state(sample_count=2))
    assert dialog._word_dots.text() == _dots(2, 5)


def test_word_feedback_got_it_is_green_check():
    dialog = _make_dialog()
    dialog.handle_state(_word_state(feedback="got_it"))
    assert dialog._word_feedback.text() == f"{CHECK_MARK} {GOT_IT}"
    assert "#15803d" in dialog._word_feedback.styleSheet()


def test_word_feedback_try_again_exact_text():
    dialog = _make_dialog()
    dialog.handle_state(_word_state(feedback="try_again"))
    assert dialog._word_feedback.text() == TRY_AGAIN


def test_word_feedback_absent_renders_empty():
    dialog = _make_dialog()
    dialog.handle_state(_word_state(feedback="got_it"))
    dialog.handle_state(_word_state(feedback=None))
    assert dialog._word_feedback.text() == ""


def test_word_cancel_present_and_emits_cal_cancel():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state(_word_state())
    assert dialog._word_cancel_btn.text() == "Cancel"
    assert dialog._word_cancel_btn.isVisibleTo(dialog)
    dialog._word_cancel_btn.click()
    assert emitted == [{"action": "cal_cancel"}]


def test_word_progress_follows_state_indices():
    dialog = _make_dialog()
    dialog.handle_state(_word_state(word="select", word_index=4, word_total=4))
    assert dialog._word_label.text() == "select"
    assert dialog._word_progress.text() == "Word 4 of 4"


# --------------------------------------------------------------------------
# Noise screen (Section 3.4)
# --------------------------------------------------------------------------

def test_noise_screen_exact_text_dots_and_buttons():
    dialog = _make_dialog()
    dialog.handle_state({
        "screen": "noise", "noise_count": 1, "noise_total": 3,
        "feedback": None,
    })
    assert dialog._stack.currentWidget() is dialog._page_noise
    assert dialog._noise_message.text() == NOISE_TEXT
    assert dialog._noise_dots.text() == _dots(1, 3)
    assert dialog._skip_btn.text() == "Skip this part"
    assert dialog._noise_cancel_btn.text() == "Cancel"


def test_noise_got_it_feedback_per_capture():
    dialog = _make_dialog()
    dialog.handle_state({
        "screen": "noise", "noise_count": 2, "noise_total": 3,
        "feedback": "got_it",
    })
    assert dialog._noise_feedback.text() == f"{CHECK_MARK} {GOT_IT}"


def test_noise_skip_emits_cal_skip_noise():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state({"screen": "noise", "noise_count": 0, "noise_total": 3})
    dialog._skip_btn.click()
    assert emitted == [{"action": "cal_skip_noise"}]


def test_noise_cancel_emits_cal_cancel():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state({"screen": "noise", "noise_count": 0, "noise_total": 3})
    dialog._noise_cancel_btn.click()
    assert emitted == [{"action": "cal_cancel"}]


# --------------------------------------------------------------------------
# Review screen (Section 3.5)
# --------------------------------------------------------------------------

_DETAILS = {
    "current": {"single_word_min_probability": 0.6,
                "single_word_max_no_speech_prob": 0.05},
    "proposed": {"single_word_min_probability": 0.15,
                 "single_word_max_no_speech_prob": 0.03},
    "measured": {"word_probability_range": "0.198 to 0.587",
                 "no_speech_range": "0.007 to 0.017"},
}


def test_review_full_exact_text_and_buttons():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state({
        "screen": "review", "review_kind": "full", "details": _DETAILS,
    })
    assert dialog._stack.currentWidget() is dialog._page_review
    assert dialog._review_message.text() == REVIEW_FULL
    assert dialog._apply_btn.text() == "Apply"
    assert dialog._review_not_now_btn.text() == "Not now"
    assert dialog._apply_btn.isVisibleTo(dialog)
    assert dialog._review_not_now_btn.isVisibleTo(dialog)
    assert not dialog._review_ok_btn.isVisibleTo(dialog)
    dialog._apply_btn.click()
    assert emitted == [{"action": "cal_apply"}]


def test_review_not_now_emits_cal_cancel():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state({
        "screen": "review", "review_kind": "full", "details": _DETAILS,
    })
    dialog._review_not_now_btn.click()
    assert emitted == [{"action": "cal_cancel"}]


def test_review_partial_exact_text():
    dialog = _make_dialog()
    dialog.handle_state({
        "screen": "review", "review_kind": "partial", "details": _DETAILS,
    })
    assert dialog._review_message.text() == REVIEW_PARTIAL
    assert dialog._apply_btn.isVisibleTo(dialog)


def test_review_nothing_exact_text_shows_ok_not_apply():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state({
        "screen": "review", "review_kind": "nothing", "details": None,
    })
    assert dialog._review_message.text() == REVIEW_NOTHING
    assert not dialog._apply_btn.isVisibleTo(dialog)
    assert not dialog._review_not_now_btn.isVisibleTo(dialog)
    assert dialog._review_ok_btn.isVisibleTo(dialog)
    assert dialog._review_ok_btn.text() == "OK"
    dialog._review_ok_btn.click()
    assert emitted == [{"action": "cal_cancel"}]


def test_show_details_hidden_until_clicked_and_renders_numbers():
    dialog = _make_dialog()
    dialog.handle_state({
        "screen": "review", "review_kind": "full", "details": _DETAILS,
    })
    assert dialog._details_btn.text() == "Show details"
    assert dialog._details_btn.isVisibleTo(dialog)
    assert not dialog._details_text.isVisibleTo(dialog)
    dialog._details_btn.click()
    assert dialog._details_text.isVisibleTo(dialog)
    body = dialog._details_text.text()
    # Before-and-after values and the measured ranges all render.
    assert "0.6" in body
    assert "0.15" in body
    assert "0.198 to 0.587" in body
    assert "0.007 to 0.017" in body


def test_show_details_absent_when_no_details():
    dialog = _make_dialog()
    dialog.handle_state({
        "screen": "review", "review_kind": "nothing", "details": None,
    })
    assert not dialog._details_btn.isVisibleTo(dialog)
    assert not dialog._details_text.isVisibleTo(dialog)


# --------------------------------------------------------------------------
# Applying / applied / save_failed / restart_slow (Section 3.6)
# --------------------------------------------------------------------------

def test_applying_screen_exact_text():
    dialog = _make_dialog()
    dialog.handle_state({"screen": "applying"})
    assert dialog._stack.currentWidget() is dialog._page_applying
    assert dialog._applying_label.text() == APPLYING_TEXT


def test_applied_screen_exact_text():
    dialog = _make_dialog()
    dialog.handle_state({"screen": "applied"})
    assert dialog._stack.currentWidget() is dialog._page_applied
    assert dialog._applied_label.text() == APPLIED_TEXT


def test_save_failed_returns_to_finish_screen_with_apply():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state({
        "screen": "save_failed",
        "details": _DETAILS,
        "error_detail": "PermissionError: [Errno 13] config.toml",
    })
    # The save-failed state reuses the finish screen with Apply available.
    assert dialog._stack.currentWidget() is dialog._page_review
    assert dialog._review_message.text() == SAVE_FAILED_TEXT
    assert dialog._apply_btn.isVisibleTo(dialog)
    dialog._apply_btn.click()
    assert emitted == [{"action": "cal_apply"}]


def test_save_failed_error_detail_inside_show_details():
    dialog = _make_dialog()
    dialog.handle_state({
        "screen": "save_failed",
        "details": _DETAILS,
        "error_detail": "PermissionError: [Errno 13] config.toml",
    })
    assert dialog._details_btn.isVisibleTo(dialog)
    dialog._details_btn.click()
    assert "PermissionError: [Errno 13] config.toml" in dialog._details_text.text()


def test_save_failed_error_detail_shown_even_without_details():
    dialog = _make_dialog()
    dialog.handle_state({
        "screen": "save_failed",
        "details": None,
        "error_detail": "disk full",
    })
    assert dialog._details_btn.isVisibleTo(dialog)
    dialog._details_btn.click()
    assert "disk full" in dialog._details_text.text()


def test_restart_slow_screen_exact_text():
    dialog = _make_dialog()
    dialog.handle_state({"screen": "restart_slow"})
    assert dialog._stack.currentWidget() is dialog._page_restart_slow
    assert dialog._restart_slow_label.text() == RESTART_SLOW_TEXT


def test_session_ended_screen_exact_text():
    dialog = _make_dialog()
    dialog.handle_state({"screen": "cancelled"})
    assert dialog._stack.currentWidget() is dialog._page_cancelled
    assert dialog._cancelled_label.text() == CANCELLED_TEXT


def test_session_ended_ok_sends_cancel_and_hides():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state({"screen": "cancelled"})
    dialog._cancelled_ok_btn.click()
    # Logic ignores a cal_cancel for a session that already ended, so
    # the one-shot cancel is harmless and the window just goes away.
    assert emitted == [{"action": "cal_cancel"}]
    assert dialog.isHidden()


# --------------------------------------------------------------------------
# Cancel / close semantics
# --------------------------------------------------------------------------

def test_close_event_sends_cal_cancel_and_hides_instead_of_closing():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state(_word_state())
    event = QCloseEvent()
    dialog.closeEvent(event)
    assert emitted == [{"action": "cal_cancel"}]
    # Hide-not-close, like the Pattern Manager: the event is ignored so
    # Qt never destroys the window.
    assert not event.isAccepted()


def test_cancel_sent_only_once_per_session():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state(_word_state())
    dialog._word_cancel_btn.click()
    dialog.closeEvent(QCloseEvent())
    assert emitted == [{"action": "cal_cancel"}]


def test_reset_session_rearms_cancel_and_blanks_screen():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state(_word_state())
    dialog._word_cancel_btn.click()
    dialog.reset_session()
    assert dialog._stack.currentWidget() is dialog._page_blank
    dialog.handle_state(_word_state())
    dialog._word_cancel_btn.click()
    assert emitted == [{"action": "cal_cancel"}, {"action": "cal_cancel"}]


def test_escape_key_reject_sends_cal_cancel():
    dialog = _make_dialog()
    emitted = _collect(dialog)
    dialog.handle_state(_word_state())
    dialog.reject()
    assert emitted == [{"action": "cal_cancel"}]


# --------------------------------------------------------------------------
# Robustness: the GUI must tolerate missing keys and unknown screens
# --------------------------------------------------------------------------

def test_handle_state_tolerates_missing_keys():
    dialog = _make_dialog()
    for screen in ("wrong_provider", "intro", "word", "noise", "review",
                   "applying", "applied", "save_failed", "restart_slow"):
        dialog.handle_state({"screen": screen})


def test_handle_state_ignores_unknown_screen_and_bad_payloads():
    dialog = _make_dialog()
    dialog.handle_state(_word_state())
    before = dialog._stack.currentWidget()
    dialog.handle_state({"screen": "no_such_screen"})
    assert dialog._stack.currentWidget() is before
    dialog.handle_state({})
    dialog.handle_state(None)
    assert dialog._stack.currentWidget() is before


# --------------------------------------------------------------------------
# Accessibility: real buttons, visible text as accessible name, keyboard
# --------------------------------------------------------------------------

def test_all_buttons_are_real_qpushbuttons_named_by_visible_text():
    dialog = _make_dialog()
    buttons = dialog.findChildren(QPushButton)
    assert buttons, "expected the dialog to be built from real QPushButtons"
    for btn in buttons:
        # Voice clicking finds buttons through UI Automation by their
        # accessible name, which must be the visible text.
        assert btn.accessibleName() == btn.text()
        # Keyboard reachability: every button accepts tab focus.
        assert btn.focusPolicy() != Qt.FocusPolicy.NoFocus


def test_default_buttons_are_the_affirmative_actions():
    dialog = _make_dialog()
    dialog.handle_state({"screen": "intro"})
    assert dialog._start_btn.isDefault()
    dialog.handle_state({"screen": "review", "review_kind": "full",
                         "details": None})
    assert dialog._apply_btn.isDefault()
    assert not dialog._start_btn.isDefault()


# --------------------------------------------------------------------------
# GuiManager wiring (tray menu, queue dispatch, opening path)
# --------------------------------------------------------------------------

@pytest.fixture
def manager():
    with patch("gui.FloatingButton"), \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        from gui import GuiManager
        cmds_q = MagicMock()
        state_q = MagicMock()
        shutdown = MagicMock()
        shutdown.is_set.return_value = False
        mgr = GuiManager(shutdown, cmds_q, state_q)
        yield mgr, mock_pystray


def test_qmenu_has_teach_voice_item_wired_to_open(manager):
    mgr, _ = manager
    # The item is enabled only once Logic has reported initial state
    # (same gate as the Pattern Manager item); a disabled QAction
    # ignores trigger().
    mgr.initial_state_received = True
    # The entry sits inside the STT Provider submenu, and both branches
    # build that submenu only when an engine is available
    # (wh-voice-teaching-stt-submenu).
    mgr.stt_providers_available = ["google_stt"]
    # Patch before building the menu: the QAction binds the handler at
    # connect time, so a patch applied afterwards would never be seen.
    with patch.object(mgr, "_open_calibration") as mock_open:
        menu = mgr._create_menu(is_tray_menu=False)
        submenus = [a.menu() for a in menu.actions()
                    if a.text() == "STT Provider"]
        assert len(submenus) == 1
        actions = [a for a in submenus[0].actions()
                   if a.text() == TRAY_ITEM_TEXT]
        assert len(actions) == 1
        actions[0].trigger()
    mock_open.assert_called_once()


def test_pystray_menu_has_teach_voice_item_marshalled_via_state_queue(manager):
    mgr, mock_pystray = manager
    # The entry sits inside the STT Provider submenu, built only when an
    # engine is available (wh-voice-teaching-stt-submenu).
    mgr.stt_providers_available = ["google_stt"]
    mock_pystray.MenuItem.reset_mock()
    mgr._create_menu(is_tray_menu=True)
    calls = [c for c in mock_pystray.MenuItem.call_args_list
             if c.args and c.args[0] == TRAY_ITEM_TEXT]
    assert len(calls) == 1
    # The tray item marshals to the Qt thread via the state queue, like
    # the Pattern Manager item.
    calls[0].args[1]()
    mgr.state_from_logic_queue.put.assert_called_with(
        {"action": "open_calibration"}
    )


def test_open_calibration_sends_session_open_and_shows(manager):
    mgr, _ = manager
    with patch("calibration_dialog.CalibrationDialog") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        mgr._open_calibration()
    mock_cls.assert_called_once()
    instance.reset_session.assert_called_once()
    mgr.commands_to_logic_queue.put_nowait.assert_any_call(
        {"action": "cal_session_open"}
    )
    instance.show.assert_called_once()


def test_open_calibration_reuses_dialog(manager):
    mgr, _ = manager
    with patch("calibration_dialog.CalibrationDialog") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        mgr._open_calibration()
        mgr._open_calibration()
    mock_cls.assert_called_once()
    assert instance.show.call_count == 2


def test_dispatch_open_calibration_action(manager):
    mgr, _ = manager
    mgr.state_from_logic_queue.get_nowait.side_effect = [
        {"action": "open_calibration"}, Empty(),
    ]
    with patch.object(mgr, "_open_calibration") as mock_open:
        mgr._check_queues_and_events()
    mock_open.assert_called_once()


def test_dispatch_cal_state_routes_to_dialog(manager):
    mgr, _ = manager
    state = {"screen": "intro", "push_to_talk": False}
    mgr._cal_dialog = MagicMock()
    mgr.state_from_logic_queue.get_nowait.side_effect = [
        {"action": "cal_state", "state": state}, Empty(),
    ]
    mgr._check_queues_and_events()
    mgr._cal_dialog.handle_state.assert_called_once_with(state)


def test_dispatch_cal_state_without_dialog_does_not_crash(manager):
    mgr, _ = manager
    mgr.state_from_logic_queue.get_nowait.side_effect = [
        {"action": "cal_state", "state": {"screen": "intro"}}, Empty(),
    ]
    mgr._check_queues_and_events()  # must not raise
