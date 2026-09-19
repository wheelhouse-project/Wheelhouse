"""Tests for the two-mode pattern editor dialog (wh-pattern-editor-dialog).

Spec: docs/plans/2026-07-09-pattern-manager-editor-design-v1.md sections 6,
8, 13, 14. This stage covers the editor core: complete simple mode (phrase
list, four basic action types, wake-word checkbox, try-it line), the
advanced-mode stub (read-only generated expression), both save paths
(pm_create_pattern with phrases / pm_update_pattern with the nested data
shape), the mode-on-open rule decided by stored data, and the
advanced-to-simple gating state. Later stages folded in here: the editable
expression field and step list (wh-pattern-editor-advanced) and the Record
button on the key-combination field (wh-pattern-editor-record-keys).
"""
from __future__ import annotations

import tomllib
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QEvent, QKeyCombination, Qt
from PySide6.QtGui import QFocusEvent, QKeyEvent, QKeySequence, QShowEvent
from PySide6.QtWidgets import QDialog

from speech.phrase_expression import generate_expression

# Constructing the dialog builds real Qt widgets; without a QApplication Qt
# aborts the whole interpreter (wh-pytest-flaky-segfault). The session-scoped
# qapp fixture guarantees one exists even when this file runs alone.
pytestmark = pytest.mark.usefixtures("qapp")


def _make_dialog(**kwargs):
    from create_pattern_dialog import CreatePatternDialog
    return CreatePatternDialog("x-ray", parent=None, **kwargs)


def _simple_entry(**overrides):
    """A pm_get_patterns entry that fits the simple shape."""
    entry = {
        "id": "a" * 64,
        "trigger_display": "deploy",
        "requires_hotword": True,
        "is_user_created": True,
        "overrides_builtin": False,
        "raw_pattern": r"^(?:deploy|ship\ it)$",
        "raw_actions": [{"function": "hk", "params": ["ctrl", "d"]}],
        "phrases": ["deploy", "ship it"],
        "description": "Press Ctrl+D",
    }
    entry.update(overrides)
    return entry


def _fill_valid_hotkey(dialog, phrase="deploy", keys="ctrl+d"):
    dialog._phrase_editor.set_phrases([phrase])
    dialog._hotkey_radio.setChecked(True)
    dialog._on_type_changed()
    dialog._key_input.setText(keys)


# ---------------------------------------------------------------------------
# Simple mode: phrase list editor
# ---------------------------------------------------------------------------


class TestPhraseListEditor:
    def test_create_mode_starts_with_one_empty_row(self):
        dialog = _make_dialog()
        assert dialog._phrase_editor.phrases() == [""]

    def test_add_row_appends_and_remove_row_removes(self):
        dialog = _make_dialog()
        dialog._phrase_editor.set_phrases(["editor"])
        dialog._phrase_editor.add_row("code editor")
        assert dialog._phrase_editor.phrases() == ["editor", "code editor"]
        dialog._phrase_editor.remove_row(0)
        assert dialog._phrase_editor.phrases() == ["code editor"]

    def test_last_row_cannot_be_removed(self):
        dialog = _make_dialog()
        dialog._phrase_editor.set_phrases(["editor"])
        dialog._phrase_editor.remove_row(0)
        assert dialog._phrase_editor.phrases() == ["editor"]

    def test_empty_phrase_disables_save_with_field_error(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        assert dialog._save_btn.isEnabled()
        dialog._phrase_editor.set_phrases([""])
        assert not dialog._save_btn.isEnabled()
        assert not dialog._phrase_error_label.isHidden()

    def test_duplicate_phrases_flagged_case_insensitively(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._phrase_editor.set_phrases(["Editor", "editor"])
        assert not dialog._save_btn.isEnabled()
        assert "Duplicate" in dialog._phrase_error_label.text()

    def test_valid_phrases_hide_the_error(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._phrase_editor.set_phrases(["editor", "code editor"])
        assert dialog._phrase_error_label.isHidden()
        assert dialog._save_btn.isEnabled()


# ---------------------------------------------------------------------------
# Simple mode: params validation and save gating
# ---------------------------------------------------------------------------


class TestSimpleValidation:
    def test_missing_hotkey_param_disables_save(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._key_input.setText("  ")
        assert not dialog._save_btn.isEnabled()
        assert not dialog._param_error_label.isHidden()

    def test_missing_text_param_disables_save(self):
        dialog = _make_dialog()
        dialog._phrase_editor.set_phrases(["gpt"])
        dialog._text_radio.setChecked(True)
        dialog._on_type_changed()
        assert not dialog._save_btn.isEnabled()
        dialog._text_output.setText("GPT")
        assert dialog._save_btn.isEnabled()

    def test_text_type_disables_and_unchecks_hotword(self):
        dialog = _make_dialog()
        dialog._text_radio.setChecked(True)
        dialog._on_type_changed()
        assert not dialog._hotword_check.isEnabled()
        assert not dialog._hotword_check.isChecked()


# ---------------------------------------------------------------------------
# Record button: Qt key capture -> WheelHouse key names
# (wh-pattern-editor-record-keys)
# ---------------------------------------------------------------------------


NO_MOD = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier
META = Qt.KeyboardModifier.MetaModifier


def _convert(key, modifiers=NO_MOD):
    from create_pattern_dialog import qt_key_to_wheelhouse
    return qt_key_to_wheelhouse(key, modifiers)


def _capture_seq(key, modifiers=NO_MOD):
    return QKeySequence(QKeyCombination(modifiers, key))


class TestQtKeyToWheelhouse:
    """The converter is a pure function: Qt key data in, WheelHouse name
    string out. Every emitted name must be accepted by the Input process
    (utils/win_input_sender.py VK_CODE_MAP), which aborts the whole chord
    on any unmapped name."""

    def test_letter_with_modifiers(self):
        assert _convert(Qt.Key.Key_N, CTRL | SHIFT) == ("ctrl+shift+n", None)

    def test_modifiers_emitted_in_canonical_order(self):
        # Flag order in the input must not matter: ctrl, shift, alt, win.
        combo, error = _convert(Qt.Key.Key_A, META | ALT | SHIFT | CTRL)
        assert error is None
        assert combo == "ctrl+shift+alt+win+a"

    def test_plain_letter_and_digit(self):
        assert _convert(Qt.Key.Key_A) == ("a", None)
        assert _convert(Qt.Key.Key_7) == ("7", None)

    def test_accepts_plain_ints(self):
        # combo.key()/keyboardModifiers() may surface as ints.
        assert _convert(0x41, 0) == ("a", None)

    def test_keypad_modifier_ignored(self):
        combo, error = _convert(
            Qt.Key.Key_7, Qt.KeyboardModifier.KeypadModifier
        )
        assert (combo, error) == ("7", None)

    def test_function_keys(self):
        assert _convert(Qt.Key.Key_F1) == ("f1", None)
        assert _convert(Qt.Key.Key_F12, CTRL) == ("ctrl+f12", None)

    def test_f13_fails_runtime_stops_at_f12(self):
        combo, error = _convert(Qt.Key.Key_F13)
        assert combo is None
        assert "F12" in error

    @pytest.mark.parametrize("key,expected", [
        (Qt.Key.Key_Return, "enter"),
        (Qt.Key.Key_Enter, "enter"),      # numpad enter
        (Qt.Key.Key_Tab, "tab"),
        (Qt.Key.Key_Escape, "esc"),       # the map has esc, not escape
        (Qt.Key.Key_Space, "space"),
        (Qt.Key.Key_Backspace, "backspace"),
        (Qt.Key.Key_Delete, "delete"),
        (Qt.Key.Key_Insert, "insert"),
        (Qt.Key.Key_Home, "home"),
        (Qt.Key.Key_End, "end"),
        (Qt.Key.Key_PageUp, "pageup"),
        (Qt.Key.Key_PageDown, "pagedown"),
        (Qt.Key.Key_Left, "left"),
        (Qt.Key.Key_Up, "up"),
        (Qt.Key.Key_Right, "right"),
        (Qt.Key.Key_Down, "down"),
        (Qt.Key.Key_Print, "printscreen"),
        (Qt.Key.Key_Pause, "pause"),
        (Qt.Key.Key_CapsLock, "capslock"),
    ])
    def test_named_keys(self, key, expected):
        assert _convert(key) == (expected, None)

    def test_backtab_is_shift_tab(self):
        # Qt reports Shift+Tab as Key_Backtab plus the shift modifier.
        assert _convert(Qt.Key.Key_Backtab, SHIFT) == ("shift+tab", None)

    def test_punctuation_names(self):
        assert _convert(Qt.Key.Key_Comma, CTRL) == ("ctrl+,", None)
        assert _convert(Qt.Key.Key_Equal, CTRL) == ("ctrl+=", None)
        assert _convert(Qt.Key.Key_Colon, CTRL | SHIFT) == (
            "ctrl+shift+:", None
        )

    def test_shifted_plus_becomes_shift_equal(self):
        # '+' cannot live in the '+'-joined field; Shift+= IS the plus
        # chord on the layout VK_CODE_MAP assumes, so it is preserved.
        assert _convert(Qt.Key.Key_Plus, CTRL | SHIFT) == (
            "ctrl+shift+=", None
        )

    def test_plus_without_shift_fails(self):
        combo, error = _convert(Qt.Key.Key_Plus)
        assert combo is None
        assert error

    def test_modifier_only_fails(self):
        for key in (Qt.Key.Key_Control, Qt.Key.Key_Shift,
                    Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            combo, error = _convert(key)
            assert combo is None
            assert error

    def test_unknown_key_fails_with_message(self):
        combo, error = _convert(Qt.Key.Key_VolumeUp)
        assert combo is None
        assert "Volume" in error

    def test_every_emittable_name_is_in_the_runtime_vk_map(self):
        # The strongest guarantee the bead asks for: the converter can
        # never write a name press_keys would reject.
        from create_pattern_dialog import _QT_KEY_NAMES, _QT_MODIFIER_NAMES
        from utils.win_input_sender import VK_CODE_MAP

        names = set(_QT_KEY_NAMES.values())
        names.update(name for _mod, name in _QT_MODIFIER_NAMES)
        names.update(chr(c) for c in range(ord("a"), ord("z") + 1))
        names.update(str(d) for d in range(10))
        names.update(f"f{n}" for n in range(1, 13))
        missing = sorted(n for n in names if n not in VK_CODE_MAP)
        assert not missing, f"names the runtime would reject: {missing}"


class TestRecordButton:
    def test_button_present_tab_focusable_not_autodefault(self):
        dialog = _make_dialog()
        btn = dialog._record_btn
        assert btn.text() == "Record"
        assert btn.focusPolicy() & Qt.FocusPolicy.TabFocus
        assert not btn.autoDefault()

    def test_click_starts_recording_second_click_cancels(self):
        dialog = _make_dialog()
        dialog._record_btn.click()
        assert dialog._recording_keys
        assert dialog._key_capture.isVisibleTo(dialog)
        assert not dialog._key_input.isVisibleTo(dialog)
        dialog._record_btn.click()
        assert not dialog._recording_keys
        assert dialog._key_input.isVisibleTo(dialog)
        assert not dialog._key_capture.isVisibleTo(dialog)

    def test_captured_combination_written_as_wheelhouse_names(self):
        dialog = _make_dialog()
        dialog._phrase_editor.set_phrases(["deploy"])
        dialog._record_btn.click()
        dialog._on_key_captured(_capture_seq(Qt.Key.Key_N, CTRL | SHIFT))
        assert dialog._key_input.text() == "ctrl+shift+n"
        assert not dialog._recording_keys
        assert dialog._key_input.isVisibleTo(dialog)
        assert dialog.get_pattern_data()["action_params"] == {
            "keys": ["ctrl", "shift", "n"],
        }
        assert dialog._save_btn.isEnabled()

    def test_field_stays_hand_editable_after_recording(self):
        dialog = _make_dialog()
        dialog._record_btn.click()
        dialog._on_key_captured(_capture_seq(Qt.Key.Key_S, CTRL))
        assert not dialog._key_input.isReadOnly()
        dialog._key_input.setText("ctrl+shift+s")
        assert dialog._key_input.text() == "ctrl+shift+s"

    def test_unconvertible_key_shows_field_error_and_keeps_text(self):
        dialog = _make_dialog()
        dialog._key_input.setText("ctrl+d")
        dialog._record_btn.click()
        dialog._on_key_captured(_capture_seq(Qt.Key.Key_F13))
        assert dialog._key_input.text() == "ctrl+d"
        assert not dialog._record_error_label.isHidden()
        assert dialog._record_error_label.text()
        assert not dialog._recording_keys

    def test_record_error_cleared_on_new_recording_and_hand_edit(self):
        dialog = _make_dialog()
        dialog._record_btn.click()
        dialog._on_key_captured(_capture_seq(Qt.Key.Key_F13))
        assert not dialog._record_error_label.isHidden()
        dialog._record_btn.click()
        assert dialog._record_error_label.isHidden()
        dialog._on_key_captured(_capture_seq(Qt.Key.Key_F13))
        assert not dialog._record_error_label.isHidden()
        dialog._key_input.setText("ctrl+e")
        assert dialog._record_error_label.isHidden()

    def test_capture_ignored_when_not_recording_or_empty(self):
        dialog = _make_dialog()
        dialog._key_input.setText("ctrl+d")
        dialog._on_key_captured(QKeySequence())
        dialog._on_key_captured(_capture_seq(Qt.Key.Key_S, CTRL))
        assert dialog._key_input.text() == "ctrl+d"
        dialog._record_btn.click()
        # QKeySequenceEdit.clear() emits an empty sequence; still recording.
        dialog._on_key_captured(QKeySequence())
        assert dialog._recording_keys

    def test_escape_cancels_capture_without_writing(self):
        dialog = _make_dialog()
        dialog._key_input.setText("ctrl+d")
        dialog._record_btn.click()
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, NO_MOD)
        dialog._key_capture.keyPressEvent(event)
        assert not dialog._recording_keys
        assert dialog._key_input.text() == "ctrl+d"
        assert dialog._key_input.isVisibleTo(dialog)

    def test_focus_out_cancels_capture(self):
        dialog = _make_dialog()
        dialog._record_btn.click()
        dialog._key_capture.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        assert not dialog._recording_keys
        assert dialog._key_input.isVisibleTo(dialog)

    def test_enter_on_focused_button_starts_recording(self):
        # autoDefault is False dialog-wide (Enter must not fire Save), so
        # the button itself must handle Return/Enter (spec section 13).
        dialog = _make_dialog()
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, NO_MOD)
        dialog._record_btn.keyPressEvent(event)
        assert dialog._recording_keys


# ---------------------------------------------------------------------------
# Hand-typed key names validated against the Input process's map
# (wh-pattern-editor-r0.5)
# ---------------------------------------------------------------------------


class TestKeyNameValidation:
    """The Record path guarantees valid names, but both key fields stay
    hand-editable: a typo like "ctrl+shft+n" used to save cleanly and abort
    silently at runtime -- a pattern that lies."""

    def test_typo_key_blocks_save_in_simple_pane(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog, keys="ctrl+shft+n")
        assert not dialog._save_btn.isEnabled()
        assert not dialog._param_error_label.isHidden()
        assert "shft" in dialog._param_error_label.text()

    def test_first_unrecognized_key_is_named(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog, keys="kontrol+shft+n")
        assert "kontrol" in dialog._param_error_label.text()
        assert "shft" not in dialog._param_error_label.text()

    def test_fixing_typo_re_enables_save(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog, keys="ctrl+shft+n")
        dialog._key_input.setText("ctrl+shift+n")
        assert dialog._param_error_label.isHidden()
        assert dialog._save_btn.isEnabled()

    def test_key_names_case_insensitive(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog, keys="Ctrl+Shift+N")
        assert dialog._param_error_label.isHidden()
        assert dialog._save_btn.isEnabled()

    def test_trailing_digit_key_blocks_save_in_simple(self):
        # ActionFunctions.hotkey peels the LAST argument as a repeat
        # count when it converts to a number: hotkey('ctrl','3') presses
        # ctrl three times, not the chord ctrl+3. Accepting it here saves
        # a pattern that lies (wh-hk-trailing-repeat-lie).
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog, keys="ctrl+3")
        assert not dialog._save_btn.isEnabled()
        assert not dialog._param_error_label.isHidden()
        assert "repeat" in dialog._param_error_label.text()

    def test_non_trailing_digit_key_is_fine_in_simple(self):
        # Only the last argument is peeled; a digit elsewhere is a real
        # key press.
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog, keys="3+ctrl")
        assert dialog._param_error_label.isHidden()
        assert dialog._save_btn.isEnabled()

    def test_trailing_digit_key_flagged_in_advanced_unless_repeat_set(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        row = dialog._steps_editor._rows[0]
        row.set_param_value(0, "ctrl+3")
        assert not dialog._save_btn.isEnabled()
        assert "repeat" in dialog._steps_error_label.text()
        # A set repeat serializes after the keys, so the digit becomes a
        # real key press and the error clears.
        row.set_param_value(1, "2")
        assert dialog._steps_error_label.isHidden()
        assert dialog._save_btn.isEnabled()
        assert dialog._steps_editor.steps() == [
            {"function": "hk", "params": ["ctrl", "3", 2]},
        ]

    def test_typo_key_blocks_save_in_advanced_hk_step(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        assert dialog._save_btn.isEnabled()
        dialog._steps_editor._rows[0].set_param_value(0, "ctrl+shft+n")
        assert not dialog._save_btn.isEnabled()
        assert not dialog._steps_error_label.isHidden()
        assert "shft" in dialog._steps_error_label.text()
        dialog._steps_editor._rows[0].set_param_value(0, "ctrl+shift+n")
        assert dialog._steps_error_label.isHidden()
        assert dialog._save_btn.isEnabled()

    def test_preserved_hk_step_never_flags(self):
        # A read-only (verbatim round-trip) hk step cannot be edited, so
        # flagging it would strand the user with an unfixable error.
        entry = _simple_entry(raw_actions=[
            {"function": "hk", "params": [True, "ctrl"]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog.mode == "advanced"
        row = dialog._steps_editor._rows[0]
        assert row._preserved_params is not None
        assert dialog._steps_error_label.isHidden()

    def test_every_record_emitted_name_passes_validation(self):
        from create_pattern_dialog import _QT_KEY_NAMES, _QT_MODIFIER_NAMES
        from speech.key_names import VALID_KEY_NAMES

        names = set(_QT_KEY_NAMES.values())
        names.update(name for _mod, name in _QT_MODIFIER_NAMES)
        names.update(chr(c) for c in range(ord("a"), ord("z") + 1))
        names.update(str(d) for d in range(10))
        names.update(f"f{n}" for n in range(1, 13))
        missing = sorted(n for n in names if n not in VALID_KEY_NAMES)
        assert not missing, f"Record-emittable names not accepted: {missing}"

    def test_valid_key_names_mirror_runtime_vk_map(self):
        # The literal set is pinned to the Input process's VK_CODE_MAP;
        # the runtime module (ctypes.windll and the whole Win32 input
        # stack) is imported inside the test only.
        from speech.key_names import VALID_KEY_NAMES
        from utils.win_input_sender import VK_CODE_MAP

        assert VALID_KEY_NAMES == set(VK_CODE_MAP)


# ---------------------------------------------------------------------------
# Save paths
# ---------------------------------------------------------------------------


class TestCreateSavePath:
    def test_save_emits_pm_create_pattern_with_phrases_and_no_trigger(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog, phrase="deploy", keys="ctrl+shift+d")
        dialog._phrase_editor.add_row("ship it")
        dialog._hotword_check.setChecked(True)
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._save_btn.click()
        assert len(emitted) == 1
        msg = emitted[0]
        assert msg["action"] == "pm_create_pattern"
        data = msg["data"]
        assert "trigger" not in data
        assert data["phrases"] == ["deploy", "ship it"]
        assert data["pattern_type"] == "command"
        assert data["action_type"] == "hotkey"
        assert data["action_params"] == {"keys": ["ctrl", "shift", "d"]}
        assert data["requires_hotword"] is True

    def test_text_action_maps_to_replacement(self):
        dialog = _make_dialog()
        dialog._phrase_editor.set_phrases(["gpt"])
        dialog._text_radio.setChecked(True)
        dialog._on_type_changed()
        dialog._text_output.setText("GPT")
        data = dialog.get_pattern_data()
        assert data["pattern_type"] == "replacement"
        assert data["action_type"] == "text"
        assert data["action_params"] == {"output": "GPT"}
        assert data["requires_hotword"] is False


class TestEditSavePath:
    def test_edit_mode_prefills_from_entry(self):
        entry = _simple_entry()
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog.mode == "simple"
        assert dialog._phrase_editor.phrases() == ["deploy", "ship it"]
        assert dialog._hotkey_radio.isChecked()
        assert dialog._key_input.text() == "ctrl+d"
        assert dialog._hotword_check.isChecked()

    def test_edit_mode_prefill_without_optional_keys_opens_advanced(self):
        # Older Logic builds may omit phrases entirely; the dialog must not
        # crash and must fall back to the advanced (read-only) pane.
        entry = {
            "id": "b" * 64,
            "trigger_display": "save",
            "raw_pattern": "^save$",
            "raw_actions": [{"function": "hk", "params": ["ctrl", "s"]}],
        }
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog.mode == "advanced"

    def test_save_emits_pm_update_pattern_with_nested_data(self):
        entry = _simple_entry()
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        dialog._key_input.setText("ctrl+e")
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._save_btn.click()
        assert len(emitted) == 1
        msg = emitted[0]
        assert msg["action"] == "pm_update_pattern"
        assert msg["data"]["pattern_id"] == entry["id"]
        inner = msg["data"]["data"]
        assert inner["phrases"] == ["deploy", "ship it"]
        assert inner["action_params"] == {"keys": ["ctrl", "e"]}


# ---------------------------------------------------------------------------
# Mode on open: decided by stored data (spec section 6)
# ---------------------------------------------------------------------------


class TestModeOnOpen:
    def test_create_mode_opens_simple(self):
        dialog = _make_dialog()
        assert dialog.mode == "simple"

    def test_phrases_and_one_basic_action_opens_simple(self):
        entry = _simple_entry()
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog.mode == "simple"

    def test_no_phrases_opens_advanced(self):
        entry = _simple_entry()
        del entry["phrases"]
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog.mode == "advanced"

    def test_two_actions_opens_advanced(self):
        entry = _simple_entry(raw_actions=[
            {"function": "hk", "params": ["ctrl", "d"]},
            {"function": "hk", "params": ["enter"]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog.mode == "advanced"

    def test_non_basic_action_opens_advanced(self):
        entry = _simple_entry(raw_actions=[
            {"function": "press_keys", "params": ["ctrl", "d"]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog.mode == "advanced"

    def test_non_string_hk_params_open_advanced(self):
        # A repeat count int cannot round-trip through the simple key field.
        entry = _simple_entry(raw_actions=[
            {"function": "hk", "params": ["ctrl", "z", 3]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog.mode == "advanced"

    def test_advanced_open_enables_save_for_valid_entry(self):
        # wh-pattern-editor-advanced unlocked saving: the pane is editable,
        # so a valid advanced-opened entry can be saved via pm_update_pattern.
        entry = _simple_entry(raw_actions=[
            {"function": "press_keys", "params": ["ctrl", "d"]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog._save_btn.isEnabled()


# ---------------------------------------------------------------------------
# Add flow opening page: "What do you want to happen?"
# (wh-pattern-editor-templates, spec section 11)
# ---------------------------------------------------------------------------


def _shown(dialog):
    """Deliver the first-show event without creating a native window
    (incidental native widgets are access-violation surface in full-suite
    runs, wh-pytest-flaky-segfault)."""
    dialog.showEvent(QShowEvent())
    return dialog


def _choose_goal(dialog, key):
    """Select the goal item carrying the given template key and activate
    it the way Enter / a click would."""
    goal_list = dialog._goal_list
    for i in range(goal_list.count()):
        item = goal_list.item(i)
        if item.data(Qt.ItemDataRole.UserRole) == key:
            goal_list.setCurrentItem(item)
            goal_list.itemActivated.emit(item)
            return
    raise AssertionError(f"goal {key!r} not on the page")


class TestGoalPage:
    def test_fresh_add_shows_goal_page_on_first_show(self):
        dialog = _make_dialog()
        # Construction leaves the editor current (programmatic use is
        # unaffected); the first show flips to the goal page.
        assert dialog._root_stack.currentWidget() is dialog._editor_page
        _shown(dialog)
        assert dialog._root_stack.currentWidget() is dialog._goal_page
        assert dialog.focusWidget() is dialog._goal_list

    def test_entry_prefill_and_edit_mode_never_see_the_page(self):
        entry = _simple_entry()
        edit_dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        _shown(edit_dialog)
        assert (
            edit_dialog._root_stack.currentWidget()
            is edit_dialog._editor_page
        )
        # Duplicate/Customize pass an entry without a pattern_id.
        dup_dialog = _make_dialog(entry=_simple_entry())
        _shown(dup_dialog)
        assert (
            dup_dialog._root_stack.currentWidget() is dup_dialog._editor_page
        )

    def test_page_never_reappears_after_a_goal_is_chosen(self):
        dialog = _shown(_make_dialog())
        _choose_goal(dialog, "hotkey")
        _shown(dialog)  # a second show must not resurrect the page
        assert dialog._root_stack.currentWidget() is dialog._editor_page

    def test_six_goals_each_with_one_sentence_hover_help(self):
        dialog = _make_dialog()
        goal_list = dialog._goal_list
        assert goal_list.count() == 6
        for i in range(goal_list.count()):
            item = goal_list.item(i)
            assert item.text().strip()
            assert item.toolTip().strip()

    @pytest.mark.parametrize("key,radio_attr,stack_index", [
        ("run", "_run_radio", 2),
        ("activate", "_activate_radio", 3),
        ("hotkey", "_hotkey_radio", 0),
        ("text", "_text_radio", 1),
        ("correction", "_text_radio", 1),
    ])
    def test_goal_lands_in_simple_mode_with_action_preselected(
        self, key, radio_attr, stack_index,
    ):
        dialog = _shown(_make_dialog())
        _choose_goal(dialog, key)
        assert dialog._root_stack.currentWidget() is dialog._editor_page
        assert dialog.mode == "simple"
        assert getattr(dialog, radio_attr).isChecked()
        assert dialog._params_stack.currentIndex() == stack_index
        # Focus lands in the first empty field: the first phrase row.
        assert dialog.focusWidget() is dialog._phrase_editor.row_edits()[0]

    def test_correction_and_snippet_templates_differ_helpfully(self):
        snippet = _shown(_make_dialog())
        _choose_goal(snippet, "text")
        fix = _shown(_make_dialog())
        _choose_goal(fix, "correction")
        # Both are the text action, hence replacement patterns (the dialog
        # infers pattern type from the action).
        assert snippet.get_pattern_data()["pattern_type"] == "replacement"
        assert fix.get_pattern_data()["pattern_type"] == "replacement"
        # ...but the guidance differs: the mishear-fix idiom teaches that
        # the phrase is what the microphone hears.
        assert snippet._phrases_label.text() != fix._phrases_label.text()
        assert (
            snippet._phrase_editor.row_edits()[0].placeholderText()
            != fix._phrase_editor.row_edits()[0].placeholderText()
        )
        assert (
            snippet._text_output.placeholderText()
            != fix._text_output.placeholderText()
        )

    def test_scratch_is_the_plain_simple_pane(self):
        dialog = _shown(_make_dialog())
        _choose_goal(dialog, "scratch")
        assert dialog.mode == "simple"
        assert dialog._hotkey_radio.isChecked()  # untouched default
        assert (
            dialog._phrase_editor.row_edits()[0].placeholderText()
            == "e.g., save project"
        )
        assert dialog.focusWidget() is dialog._phrase_editor.row_edits()[0]

    def test_enter_on_list_activates_current_goal(self):
        # Keyboard-first (spec section 13): arrow keys move the current
        # row; Enter selects it regardless of platform activation rules.
        dialog = _shown(_make_dialog())
        goal_list = dialog._goal_list
        target = next(
            i for i in range(goal_list.count())
            if goal_list.item(i).data(Qt.ItemDataRole.UserRole) == "activate"
        )
        goal_list.setCurrentRow(target)
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, NO_MOD)
        goal_list.keyPressEvent(event)
        assert dialog._activate_radio.isChecked()
        assert dialog._root_stack.currentWidget() is dialog._editor_page

    def test_focus_falls_to_action_field_when_phrases_filled(self):
        dialog = _shown(_make_dialog())
        dialog._phrase_editor.set_phrases(["save file"])
        _choose_goal(dialog, "hotkey")
        assert dialog.focusWidget() is dialog._key_input

    def test_template_is_a_starting_point_not_a_lock(self):
        dialog = _shown(_make_dialog())
        _choose_goal(dialog, "run")
        dialog._text_radio.setChecked(True)
        dialog._on_type_changed()
        dialog._text_output.setText("GPT")
        assert dialog.get_pattern_data()["action_type"] == "text"


# ---------------------------------------------------------------------------
# Advanced pane stub + mode toggle gating
# ---------------------------------------------------------------------------


class TestAdvancedStubAndGating:
    def test_toggle_to_advanced_shows_generated_expression_editable(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog, phrase="deploy")
        dialog._phrase_editor.add_row("ship it")
        dialog._advanced_toggle.setChecked(True)
        assert dialog.mode == "advanced"
        expected = generate_expression(["deploy", "ship it"], "command")
        assert dialog._expression_edit.text() == expected
        assert not dialog._expression_edit.isReadOnly()

    def test_advanced_open_shows_raw_pattern(self):
        entry = _simple_entry(raw_actions=[
            {"function": "press_keys", "params": ["ctrl", "d"]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog._expression_edit.text() == entry["raw_pattern"]

    def test_back_to_simple_enabled_for_untouched_single_basic_action(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        assert dialog._advanced_toggle.isEnabled()
        dialog._advanced_toggle.setChecked(False)
        assert dialog.mode == "simple"

    def test_touched_expression_disables_back_toggle_with_tooltip(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        dialog.set_expression_touched(True)
        assert not dialog._advanced_toggle.isEnabled()
        assert dialog._advanced_toggle.toolTip()
        dialog.set_expression_touched(False)
        assert dialog._advanced_toggle.isEnabled()

    def test_multi_action_entry_disables_back_toggle_with_tooltip(self):
        entry = _simple_entry(raw_actions=[
            {"function": "hk", "params": ["ctrl", "d"]},
            {"function": "hk", "params": ["enter"]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog.mode == "advanced"
        assert not dialog._advanced_toggle.isEnabled()
        assert "action" in dialog._advanced_toggle.toolTip().lower()

    def test_expression_touched_tracks_generated_expression(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        assert dialog.expression_touched is False
        entry = _simple_entry()
        del entry["phrases"]
        raw_dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        # A raw-regex pattern was never generated by simple mode.
        assert raw_dialog.expression_touched is True


# ---------------------------------------------------------------------------
# Advanced pane: expression field (wh-pattern-editor-advanced)
# ---------------------------------------------------------------------------


def _advanced_entry(**overrides):
    """An entry that opens in advanced mode (non-basic action)."""
    return _simple_entry(raw_actions=[
        {"function": "press_keys", "params": ["ctrl", "d"]},
    ], **overrides)


class TestAdvancedExpressionField:
    @pytest.mark.parametrize("length", [500, 602])
    @pytest.mark.parametrize("selection", [False, True])
    def test_expression_length_budget_rejection_preserves_editing_state(self, length, selection):
        original = "^ab" + "c" * (length - 4) + "$"
        entry = _advanced_entry(raw_pattern=original)
        entry.pop("phrases", None)
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        edit = dialog._expression_edit
        # Establish real undo history before a rejected edit.
        edit.setCursorPosition(3)
        edit.backspace()
        shortened = original[:2] + original[3:]
        if selection:
            edit.setSelection(1, 1)
        else:
            edit.setCursorPosition(2)
        position = edit.cursorPosition()
        edit.insert("z" * 20)
        assert edit.text() == shortened
        assert edit.cursorPosition() == position
        assert edit.selectedText() == ("a" if selection else "")
        assert edit.isUndoAvailable()
        edit.undo()
        assert edit.text() == original
        # Reject again, then delete at the retained cursor/selection.
        if selection:
            edit.setSelection(1, 1)
        else:
            edit.setCursorPosition(2)
        edit.insert("z" * 20)
        edit.backspace()
        assert edit.text() == original[:1] + original[2:]

    @pytest.mark.parametrize("source", ["legacy", "simple"])
    def test_expression_length_budget_loaded_oversize_can_be_shortened(self, source):
        if source == "legacy":
            original = "^" + "a" * 600 + "$"
            entry = _advanced_entry(raw_pattern=original)
            entry.pop("phrases", None)
            dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        else:
            dialog = _make_dialog()
            _fill_valid_hotkey(dialog)
            dialog._phrase_editor.set_phrases(["a" * 600])
            original = dialog.generated_expression
            assert dialog._save_btn.isEnabled()
            dialog._advanced_toggle.setChecked(True)
        edit = dialog._expression_edit
        assert edit.text() == original
        assert edit.maxLength() == 500
        assert "500 characters" in dialog._expression_error_label.text()
        assert not dialog._save_btn.isEnabled()
        with patch("create_pattern_dialog.re.compile") as compile_expression:
            assert dialog._advanced_group_count() == 0
            compile_expression.assert_not_called()
        edit.setCursorPosition(2)
        edit.insert("b")
        assert edit.text() == original
        # Native editing may shorten a loaded oversized value incrementally.
        edit.backspace()
        assert len(edit.text()) == len(original) - 1
        assert not dialog._save_btn.isEnabled()
        edit.selectAll()
        edit.insert("^short$")
        assert edit.text() == "^short$"
        assert dialog._save_btn.isEnabled()
        assert dialog._expression_error_label.isHidden()

    @pytest.mark.parametrize("characters", [500, 1000])
    def test_expression_length_budget_native_setter_does_not_truncate_into_valid_input(self, characters):
        from PySide6.QtWidgets import QLineEdit

        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        edit = dialog._expression_edit
        original = edit.text()
        # Native/accessibility callers can reach Qt's setter directly. This
        # text must still be rejected if Qt first shortens its UTF-16 storage.
        QLineEdit.setText(edit, "^" + "\U0001f600" * characters + "$")
        assert edit.text() == original
        assert "500 characters" in dialog._expression_error_label.text()
        assert not dialog._save_btn.isEnabled()

    @pytest.mark.parametrize("characters", [250, 498])
    def test_expression_length_budget_unicode_backend_dialog_round_trip(self, tmp_path, characters):
        from speech.pattern_manager import PatternManager

        system = tmp_path / "patterns.toml"
        system.write_text("", encoding="utf-8")
        user = tmp_path / "user_patterns.toml"
        manager = PatternManager(str(system), str(user))
        expression = "^" + "\U0001f600" * characters + "$"
        result = manager.create_pattern(
            expression=expression, pattern_type="command",
            actions=[{"function": "hk", "params": ["ctrl", "d"]}],
        )
        assert result["success"] is True
        stored = tomllib.loads(user.read_text(encoding="utf-8"))["pattern"][0]
        assert stored["pattern"] == expression
        entry = next(
            entry
            for category in manager.get_all_patterns_structured()["categories"].values()
            for entry in category["patterns"]
            if entry["id"] == result["pattern_id"]
        )
        dialog = _make_dialog(entry=entry, pattern_id=result["pattern_id"])
        assert dialog._expression_edit.text() == expression
        assert dialog._expression_edit.maxLength() == 500
        assert dialog._expression_error_label.isHidden()
        assert dialog._save_btn.isEnabled()

    def test_expression_length_budget_maximum(self):
        dialog = _make_dialog()
        assert dialog._expression_edit.maxLength() == 500

    @pytest.mark.parametrize("character", ["a", "\U0001f600"])
    @pytest.mark.parametrize("method", ["setText", "insert", "paste", "typing"])
    def test_expression_length_budget_rejects_without_truncation(self, method, character, qapp):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        edit = dialog._expression_edit
        original = "^" + character * 498 + "$" if method == "typing" else "^deploy$"
        edit.setText(original)
        if method == "typing":
            from PySide6.QtTest import QTest
            QTest.keyClicks(edit, "b")
        else:
            edit.selectAll()
            oversized = "^" + character * 499 + "$"
            if method == "paste":
                qapp.clipboard().setText(oversized)
                edit.paste()
            else:
                getattr(edit, method)(oversized)
        assert edit.text() == original
        assert "500 characters" in dialog._expression_error_label.text()
        assert not dialog._expression_error_label.isHidden()
        assert not dialog._save_btn.isEnabled()
        edit.setText("^" + character * 498 + "$")
        assert len(edit.text()) == 500
        assert dialog._expression_error_label.isHidden()
        assert dialog._save_btn.isEnabled()

    def test_user_edit_sets_touched_and_restore_clears(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog, phrase="deploy")
        dialog._advanced_toggle.setChecked(True)
        assert dialog.expression_touched is False
        dialog._expression_edit.setText("^deploy$")
        assert dialog.expression_touched is True
        dialog._expression_edit.setText(dialog.generated_expression)
        assert dialog.expression_touched is False

    def test_invalid_expression_shows_error_and_disables_save(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        assert dialog._save_btn.isEnabled()
        dialog._expression_edit.setText("^(unclosed$")
        assert not dialog._save_btn.isEnabled()
        assert not dialog._expression_error_label.isHidden()
        assert "compile" in dialog._expression_error_label.text()

    def test_empty_expression_disables_save(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        dialog._expression_edit.setText("   ")
        assert not dialog._save_btn.isEnabled()

    def test_group_count_label_tracks_expression(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        dialog._expression_edit.setText("^open (.+) with (.+)$")
        assert "2" in dialog._group_count_label.text()
        dialog._expression_edit.setText("^open (.+)$")
        assert "1" in dialog._group_count_label.text()

    def test_regex101_link_uses_pinned_url(self):
        from pattern_help_dialog import REGEX_CHECKER_URL

        dialog = _make_dialog()
        assert REGEX_CHECKER_URL in dialog._regex_link_label.text()
        assert dialog._regex_link_label.openExternalLinks()

    def test_type_contradiction_is_field_error_gating_save(self):
        # The runtime loader decides the kind from the ^ anchor; an
        # unanchored expression declared "command" would store a lying
        # type key, so it is a field error, never a silent rewrite.
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        dialog._expression_edit.setText(r"\bdeploy\b")
        assert dialog._adv_command_radio.isChecked()
        assert not dialog._save_btn.isEnabled()
        assert not dialog._type_error_label.isHidden()
        dialog._adv_replacement_radio.setChecked(True)
        dialog._on_adv_type_changed()
        assert dialog._type_error_label.isHidden()
        assert dialog._save_btn.isEnabled()

    def test_advanced_open_prefills_type_from_entry_key(self):
        entry = _advanced_entry(
            raw_pattern=r"\bteh\b",
            type="replacement",
        )
        del entry["phrases"]
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog._adv_replacement_radio.isChecked()


# ---------------------------------------------------------------------------
# Advanced pane: step list
# ---------------------------------------------------------------------------


class TestAdvancedSteps:
    def test_advanced_open_prefills_steps(self):
        entry = _simple_entry(raw_actions=[
            {"function": "press", "params": ["del", "g1"]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        rows = dialog._steps_editor._rows
        assert len(rows) == 1
        assert rows[0].function_name() == "press"
        assert rows[0].param_value(0) == "del"
        assert rows[0].param_value(1) == "g1"
        assert dialog._steps_editor.steps() == [
            {"function": "press", "params": ["del", "g1"]},
        ]

    def test_internal_functions_hidden_from_picker(self):
        dialog = _make_dialog()
        combo = dialog._steps_editor._rows[0]._function_combo
        names = [combo.itemData(i) for i in range(combo.count())]
        assert "hk" in names
        assert "press" in names
        for internal in (
            "skip_clipboard_restore", "capture_clipboard",
            "add_hint_to_stt", "set_speech_interaction_mode",
        ):
            assert internal not in names

    def test_internal_function_prefill_preserved_verbatim(self):
        # A Customize of a built-in that uses an internal step must not
        # crash and must round-trip the step untouched (spec section 14).
        actions = [
            {"function": "skip_clipboard_restore", "params": []},
            {"function": "hk", "params": ["ctrl", "c"]},
        ]
        entry = _simple_entry(raw_actions=actions)
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog._steps_editor.steps() == actions

    def test_hk_round_trip_with_int_repeat(self):
        entry = _simple_entry(raw_actions=[
            {"function": "hk", "params": ["ctrl", "z", 3]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        row = dialog._steps_editor._rows[0]
        assert row.param_value(0) == "ctrl+z"
        assert row.param_value(1) == "3"
        assert dialog._steps_editor.steps() == [
            {"function": "hk", "params": ["ctrl", "z", 3]},
        ]

    def test_run_capture_numeric_string_first_param_is_program_not_timeout(self):
        original_step = {
            "function": "run_capture",
            "params": ["5", "C:\\Tools\\lookup.exe", "g1", "--plain"],
            "result": "answer",
        }
        entry = _simple_entry(raw_actions=[original_step])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        row = dialog._steps_editor._rows[0]
        assert row._preserved_params is None
        assert [row.param_value(index) for index in range(5)] == [
            "", "5", "C:\\Tools\\lookup.exe", "g1", "--plain",
        ]
        assert dialog._steps_editor.steps() == [original_step]

    @pytest.mark.parametrize(
        ("timeout_text", "expected_timeout"),
        [("10", 10), ("0.5", 0.5)],
    )
    def test_run_capture_editor_serializes_timeout_as_a_toml_number(
        self, timeout_text, expected_timeout
    ):
        from create_pattern_dialog import ActionStepRow

        row = ActionStepRow()
        index = row._function_combo.findData("run_capture")
        row._function_combo.setCurrentIndex(index)
        row.set_param_value(0, timeout_text)
        row.set_param_value(1, "C:\\Tools\\lookup.exe")
        row._add_run_capture_argument("g1")
        row._add_run_capture_argument("--plain")
        assert row.step() == {
            "function": "run_capture",
            "params": [
                expected_timeout, "C:\\Tools\\lookup.exe", "g1", "--plain",
            ],
        }

    def test_run_capture_editor_rejects_non_numeric_timeout(self):
        entry = _simple_entry(raw_pattern="^capture$", raw_actions=[
            {"function": "run_capture", "params": [10, "C:\\Tools\\lookup.exe"]},
        ])
        del entry["phrases"]
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        row = dialog._steps_editor._rows[0]

        row.set_param_value(0, "later")
        dialog._validate_advanced()

        assert "timeout must be a number" in dialog._steps_error_label.text().lower()
        assert not dialog._save_btn.isEnabled()

    def test_run_capture_editor_rejects_unicode_digit_timeout_without_crashing(self):
        from create_pattern_dialog import _parse_run_capture_timeout

        entry = _simple_entry(raw_pattern="^capture$", raw_actions=[
            {"function": "run_capture", "params": [10, "C:\\Tools\\lookup.exe"]},
        ])
        del entry["phrases"]
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        row = dialog._steps_editor._rows[0]

        row.set_param_value(0, "²")
        dialog._validate_advanced()

        assert _parse_run_capture_timeout("²") is None
        assert "timeout must be a number" in dialog._steps_error_label.text().lower()
        assert not dialog._save_btn.isEnabled()

    def test_hk_multi_digit_group_ref_repeat_recognized(self):
        # The save-side check accepts g10+ when the expression has that
        # many groups, so the editor must recognize it as the repeat
        # count too. With the old single-digit matcher, "g10" stayed in
        # the keys and the key-name check flagged it -- a valid saved
        # pattern reopened as un-saveable (wh-pattern-editor-r5.1).
        from create_pattern_dialog import _split_hk_params

        assert _split_hk_params(["ctrl", "z", "g2"]) == (["ctrl", "z"], "g2")
        assert _split_hk_params(["ctrl", "z", "g10"]) == (
            ["ctrl", "z"], "g10",
        )
        # "g0" is not a group reference; it stays a key name (and is then
        # flagged as unknown, which is correct).
        keys, repeat = _split_hk_params(["ctrl", "g0"])
        assert keys == ["ctrl", "g0"] and repeat is None

    def test_hk_g10_repeat_round_trips_without_key_error(self):
        entry = _simple_entry(raw_actions=[
            {"function": "hk", "params": ["ctrl", "z", "g10"]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        row = dialog._steps_editor._rows[0]
        assert row.param_value(0) == "ctrl+z"
        assert row.param_value(1) == "g10"
        assert row.invalid_key_name() is None
        assert dialog._steps_editor.steps() == [
            {"function": "hk", "params": ["ctrl", "z", "g10"]},
        ]

    def test_add_remove_and_last_step_not_removable(self):
        dialog = _make_dialog()
        editor = dialog._steps_editor
        assert len(editor._rows) == 1
        editor.add_step()
        assert len(editor._rows) == 2
        editor.remove_step(1)
        assert len(editor._rows) == 1
        editor.remove_step(0)  # last row is kept
        assert len(editor._rows) == 1

    def test_move_step_reorders(self):
        dialog = _make_dialog()
        editor = dialog._steps_editor
        editor.set_steps([
            {"function": "hk", "params": ["ctrl", "c"]},
            {"function": "run", "params": ["notepad.exe"]},
        ])
        editor.move_step(1, -1)
        assert [s["function"] for s in editor.steps()] == ["run", "hk"]
        editor.move_step(0, 1)
        assert [s["function"] for s in editor.steps()] == ["hk", "run"]

    def test_group_ref_choices_follow_expression_groups(self):
        entry = _simple_entry(
            raw_pattern="^click (.+) on (.+)$",
            raw_actions=[{"function": "click_element", "params": ["g1"]}],
        )
        del entry["phrases"]
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        row = dialog._steps_editor._rows[0]
        spec, widget = row._param_widgets[0]
        assert spec["kind"] == "group_ref"
        items = [widget.itemText(i) for i in range(widget.count())]
        assert "g1" in items and "g2" in items
        dialog._expression_edit.setText("^click (.+)$")
        items = [widget.itemText(i) for i in range(widget.count())]
        assert "g1" in items and "g2" not in items
        # The chosen value survives the refresh.
        assert row.param_value(0) == "g1"

    def test_choice_param_renders_catalog_choices(self):
        entry = _simple_entry(raw_actions=[
            {"function": "transform_selection", "params": ["snake_case"]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        row = dialog._steps_editor._rows[0]
        spec, widget = row._param_widgets[0]
        assert spec["kind"] == "choice"
        items = [widget.itemText(i) for i in range(widget.count())]
        assert "snake_case" in items and "title_case" in items
        assert row.param_value(0) == "snake_case"

    def test_function_summary_shown_inline(self):
        dialog = _make_dialog()
        row = dialog._steps_editor._rows[0]
        from speech.action_catalog import CATALOG_BY_NAME
        assert row._summary_label.text() == CATALOG_BY_NAME["hk"]["summary"]

    def test_non_dict_step_from_hand_edit_skipped_not_crash(self):
        # A hand-edited user file can carry `actions = [5, {...}]`; the
        # entry's raw_actions arrive verbatim. Degrade by dropping the
        # garbage step, never crash (spec section 14).
        entry = _simple_entry(raw_actions=[
            5,
            {"function": "press", "params": ["del"]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog._steps_editor.steps() == [
            {"function": "press", "params": ["del"]},
        ]

    def test_all_garbage_steps_leave_one_default_row(self):
        entry = _simple_entry(raw_actions=[5, "junk"])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert len(dialog._steps_editor._rows) == 1

    def test_non_list_params_degrade_to_empty_fields(self):
        # `params = "ctrl"` (a string) must not explode into per-character
        # params via list(); the fields load empty instead (the empty key
        # field serializes verbatim, like any other text-kind field).
        entry = _simple_entry(raw_actions=[
            {"function": "press", "params": "ctrl"},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog._steps_editor.steps() == [
            {"function": "press", "params": [""]},
        ]


# ---------------------------------------------------------------------------
# Advanced pane: save shape, gating feed, mode sync
# ---------------------------------------------------------------------------


class TestGroupRefRangeValidation:
    """A whole-param g<N> reference beyond the expression's capture-group
    count is a live field error in advanced mode (wh-pattern-editor-r2.2),
    matching the save-time seam check -- without it the Save click would
    bounce off the Logic-side rejection with no field-level hint."""

    def _dialog_with_group_ref(self, raw_pattern="^open (.+)$"):
        entry = _simple_entry(
            raw_pattern=raw_pattern,
            raw_actions=[{"function": "click_element", "params": ["g1"]}],
        )
        del entry["phrases"]
        return _make_dialog(entry=entry, pattern_id=entry["id"])

    def test_out_of_range_group_ref_blocks_save(self):
        dialog = self._dialog_with_group_ref()
        assert dialog.mode == "advanced"
        assert dialog._steps_error_label.isHidden()
        assert dialog._save_btn.isEnabled()
        dialog._steps_editor._rows[0].set_param_value(0, "g2")
        assert not dialog._save_btn.isEnabled()
        assert not dialog._steps_error_label.isHidden()
        assert "g2" in dialog._steps_error_label.text()

    def test_adding_a_group_clears_the_error(self):
        dialog = self._dialog_with_group_ref()
        dialog._steps_editor._rows[0].set_param_value(0, "g2")
        assert not dialog._save_btn.isEnabled()
        dialog._expression_edit.setText("^open (.+) on (.+)$")
        assert dialog._steps_error_label.isHidden()
        assert dialog._save_btn.isEnabled()

    def test_two_digit_ref_checked(self):
        dialog = self._dialog_with_group_ref()
        dialog._steps_editor._rows[0].set_param_value(0, "g10")
        assert not dialog._save_btn.isEnabled()
        assert "g10" in dialog._steps_error_label.text()


class TestAdvancedSaveAndSync:
    def test_advanced_get_pattern_data_shape(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog, phrase="deploy", keys="ctrl+d")
        dialog._advanced_toggle.setChecked(True)
        data = dialog.get_pattern_data()
        assert data["expression"] == dialog.generated_expression
        assert data["actions"] == [
            {"function": "hk", "params": ["ctrl", "d"]},
        ]
        assert data["pattern_type"] == "command"
        assert "phrases" not in data
        assert "action_type" not in data
        assert "action_params" not in data

    def test_advanced_open_save_emits_update_with_expression(self):
        entry = _advanced_entry()
        del entry["phrases"]
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._save_btn.click()
        assert len(emitted) == 1
        msg = emitted[0]
        assert msg["action"] == "pm_update_pattern"
        assert msg["data"]["pattern_id"] == entry["id"]
        inner = msg["data"]["data"]
        assert inner["expression"] == entry["raw_pattern"]
        assert inner["actions"] == entry["raw_actions"]
        assert "phrases" not in inner

    def test_try_it_draft_carries_expression_and_actions(self):
        entry = _advanced_entry()
        del entry["phrases"]
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._try_input.setText("deploy")
        dialog._send_test_draft()
        assert len(emitted) == 1
        draft = emitted[0]["data"]["draft"]
        assert draft["expression"] == entry["raw_pattern"]
        assert draft["actions"] == entry["raw_actions"]
        assert draft["exclude_pattern_id"] == entry["id"]
        assert "phrases" not in draft

    def test_step_edits_feed_mode_gate(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        assert dialog._advanced_toggle.isEnabled()
        dialog._steps_editor.add_step()
        assert not dialog._advanced_toggle.isEnabled()
        dialog._steps_editor.remove_step(1)
        assert dialog._advanced_toggle.isEnabled()

    def test_advanced_to_simple_syncs_step_edit_back(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog, keys="ctrl+d")
        dialog._advanced_toggle.setChecked(True)
        dialog._steps_editor._rows[0].set_param_value(0, "ctrl+e")
        assert dialog._advanced_toggle.isEnabled()
        dialog._advanced_toggle.setChecked(False)
        assert dialog.mode == "simple"
        assert dialog._key_input.text() == "ctrl+e"

    def test_simple_to_advanced_builds_steps_from_simple_fields(self):
        dialog = _make_dialog()
        dialog._phrase_editor.set_phrases(["notes"])
        dialog._run_radio.setChecked(True)
        dialog._on_type_changed()
        dialog._run_path.setText("notepad.exe")
        dialog._advanced_toggle.setChecked(True)
        assert dialog._steps_editor.steps() == [
            {"function": "run", "params": ["notepad.exe"]},
        ]
        assert dialog._expression_edit.text() == dialog.generated_expression

    def test_hotword_checkbox_visible_in_advanced(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        # The checkbox lives outside the stacked panes; a widget parked on
        # the hidden simple page would report isVisibleTo(dialog) False.
        assert dialog._hotword_check.isVisibleTo(dialog)

    def test_replacement_type_disables_hotword_in_advanced(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        dialog._adv_replacement_radio.setChecked(True)
        dialog._on_adv_type_changed()
        assert not dialog._hotword_check.isEnabled()
        assert not dialog._hotword_check.isChecked()
        dialog._adv_command_radio.setChecked(True)
        dialog._on_adv_type_changed()
        assert dialog._hotword_check.isEnabled()

    def test_try_result_shows_groups_and_steps_in_advanced(self):
        entry = _simple_entry(
            raw_pattern="^open (.+)$",
            raw_actions=[{"function": "activate", "params": ["g1"]},
                         {"function": "press", "params": ["enter"]}],
        )
        del entry["phrases"]
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        dialog._try_input.setText("open notepad")
        dialog.handle_response({
            "action": "pm_test_draft_result",
            "data": {
                "success": True, "draft_error": None, "draft_matches": True,
                "winner": "draft", "shadowed_by": None,
                "groups": ["notepad"],
                "resolved_steps": [
                    {"function": "activate", "params": ["notepad"]},
                    {"function": "press", "params": ["enter"]},
                ],
            },
        })
        text = dialog._try_result_label.text()
        assert "will run this pattern" in text
        assert "g1" in text and "notepad" in text
        assert "activate" in text and "press" in text


# ---------------------------------------------------------------------------
# UX quality pass (wh-pattern-editor-ux, spec section 13)
# ---------------------------------------------------------------------------


def _focus_chain(start, limit=800):
    """Every widget in start's window focus chain, starting at start."""
    chain = [start]
    widget = start.nextInFocusChain()
    while widget is not start and len(chain) < limit:
        chain.append(widget)
        widget = widget.nextInFocusChain()
    return chain


def _assert_in_order(chain, widgets):
    positions = []
    for widget in widgets:
        assert widget in chain, f"{widget!r} missing from focus chain"
        positions.append(chain.index(widget))
    assert positions == sorted(positions), (
        f"focus order wrong: {positions} for {widgets}"
    )


def _key(dialog, key, modifiers=NO_MOD):
    dialog.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, modifiers))


class TestEditorKeyboard:
    def test_tab_order_walks_major_controls_in_order(self):
        # The shared controls (wake word, try-it, buttons) are CREATED
        # before the panes but sit BELOW them visually; without an explicit
        # setTabOrder chain, Tab would visit them first.
        dialog = _make_dialog()
        chain = _focus_chain(dialog._advanced_toggle)
        _assert_in_order(chain, [
            dialog._advanced_toggle,
            dialog._phrase_editor.row_edits()[0],
            dialog._phrase_editor.add_btn,
            dialog._hotkey_radio,
            dialog._text_radio,
            dialog._run_radio,
            dialog._activate_radio,
            dialog._key_input,
            dialog._record_btn,
            dialog._expression_edit,
            dialog._adv_command_radio,
            dialog._hotword_check,
            dialog._try_input,
            dialog._save_btn,
            dialog._cancel_btn,
        ])

    def test_added_phrase_row_joins_focus_chain_in_order(self):
        dialog = _make_dialog()
        dialog._phrase_editor.add_row("second")
        chain = _focus_chain(dialog._advanced_toggle)
        edits = dialog._phrase_editor.row_edits()
        _assert_in_order(chain, [
            edits[0],
            edits[1],
            dialog._phrase_editor.add_btn,
            dialog._hotkey_radio,
        ])

    def test_step_row_controls_sit_between_type_and_shared_controls(self):
        entry = _advanced_entry()
        del entry["phrases"]
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        row = dialog._steps_editor._rows[0]
        chain = _focus_chain(dialog._advanced_toggle)
        _assert_in_order(chain, [
            dialog._expression_edit,
            dialog._adv_command_radio,
            row._function_combo,
            row.up_btn,
            row.remove_btn,
            dialog._steps_editor.add_btn,
            dialog._hotword_check,
            dialog._save_btn,
        ])

    def test_ctrl_enter_saves_when_valid(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        _key(dialog, Qt.Key.Key_Return, CTRL)
        assert len(emitted) == 1
        assert emitted[0]["action"] == "pm_create_pattern"

    def test_ctrl_enter_noop_when_invalid(self):
        dialog = _make_dialog()  # no phrases yet: Save is disabled
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        _key(dialog, Qt.Key.Key_Return, CTRL)
        _key(dialog, Qt.Key.Key_Enter, CTRL)
        assert emitted == []

    def test_save_tooltip_mentions_ctrl_enter(self):
        dialog = _make_dialog()
        assert "Ctrl+Enter" in dialog._save_btn.toolTip()

    def test_enter_in_try_input_sends_test_immediately(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._try_input.setText("deploy")
        dialog._try_input.setFocus()
        _key(dialog, Qt.Key.Key_Return)
        assert len(emitted) == 1
        assert emitted[0]["action"] == "pm_test_draft"
        # Enter cancels the pending debounce so nothing sends twice.
        assert not dialog._try_timer.isActive()

    def test_enter_in_phrase_row_moves_focus_forward(self):
        # Enter in a single-line field must never save; it moves focus on
        # (the Stage 4 autoDefault-False decision made explicit).
        dialog = _make_dialog()
        edit = dialog._phrase_editor.row_edits()[0]
        edit.setFocus()
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        _key(dialog, Qt.Key.Key_Return)
        assert dialog.focusWidget() is not edit
        assert emitted == []

    def test_enter_on_focused_button_clicks_it(self):
        dialog = _make_dialog()
        rejected = []
        dialog.rejected.connect(lambda: rejected.append(True))
        dialog._cancel_btn.setFocus()
        _key(dialog, Qt.Key.Key_Return)
        assert rejected == [True]

    def test_escape_rejects_dialog(self):
        dialog = _make_dialog()
        rejected = []
        dialog.rejected.connect(lambda: rejected.append(True))
        _key(dialog, Qt.Key.Key_Escape)
        assert rejected == [True]


class TestEditorAccessibilityAndTooltips:
    def test_every_interactive_control_named_and_described(self):
        dialog = _make_dialog()
        row = dialog._steps_editor._rows[0]
        widgets = [
            dialog._advanced_toggle,
            dialog._hotword_check,
            dialog._try_input,
            dialog._save_btn,
            dialog._cancel_btn,
            dialog._hotkey_radio,
            dialog._text_radio,
            dialog._run_radio,
            dialog._activate_radio,
            dialog._key_input,
            dialog._key_capture,
            dialog._record_btn,
            dialog._text_output,
            dialog._run_path,
            dialog._browse_btn,
            dialog._activate_target,
            dialog._expression_edit,
            dialog._regex_link_label,
            dialog._adv_command_radio,
            dialog._adv_replacement_radio,
            dialog._goal_list,
            dialog._goal_cancel_btn,
            dialog._phrase_editor.row_edits()[0],
            dialog._phrase_editor.add_btn,
            dialog._steps_editor.add_btn,
            row._function_combo,
            row.up_btn,
            row.down_btn,
            row.remove_btn,
        ]
        for widget in widgets:
            assert widget.accessibleName() != "", widget
            assert widget.accessibleDescription() != "", widget

    def test_phrase_remove_buttons_named_and_described(self):
        dialog = _make_dialog()
        dialog._phrase_editor.add_row("second")
        for _w, _edit, btn in dialog._phrase_editor._rows:
            assert btn.accessibleName() != ""
            assert btn.accessibleDescription() != ""

    def test_tooltips_on_flagged_controls(self):
        # The hover-help sweep gaps the spec calls out: pattern-type
        # radios, expression field, group-count label, try-it input.
        dialog = _make_dialog()
        for widget in (
            dialog._hotkey_radio,
            dialog._text_radio,
            dialog._run_radio,
            dialog._activate_radio,
            dialog._adv_command_radio,
            dialog._adv_replacement_radio,
            dialog._expression_edit,
            dialog._group_count_label,
            dialog._try_input,
            dialog._hotword_check,
            dialog._save_btn,
            dialog._cancel_btn,
            dialog._browse_btn,
        ):
            assert widget.toolTip() != "", widget

    def test_regex_link_keyboard_accessible(self):
        # No mouse-only path: the regex101 link must be Tab-focusable and
        # keyboard-activatable, not click-only.
        dialog = _make_dialog()
        link = dialog._regex_link_label
        assert link.focusPolicy() & Qt.FocusPolicy.TabFocus
        assert (
            link.textInteractionFlags()
            & Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )

    def test_step_param_labels_have_buddies(self):
        from PySide6.QtWidgets import QFormLayout

        entry = _simple_entry(raw_actions=[
            {"function": "press", "params": ["del", "g1"]},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        row = dialog._steps_editor._rows[0]
        for i, (_spec, widget) in enumerate(row._param_widgets):
            label_item = row._params_form.itemAt(
                i, QFormLayout.ItemRole.LabelRole
            )
            assert label_item.widget().buddy() is widget


class TestStepReorderButtons:
    def test_move_buttons_disabled_at_list_edges(self):
        dialog = _make_dialog()
        editor = dialog._steps_editor
        editor.set_steps([
            {"function": "hk", "params": ["ctrl", "c"]},
            {"function": "run", "params": ["notepad.exe"]},
            {"function": "press", "params": ["enter"]},
        ])
        first, middle, last = editor._rows
        assert not first.up_btn.isEnabled()
        assert first.down_btn.isEnabled()
        assert middle.up_btn.isEnabled()
        assert middle.down_btn.isEnabled()
        assert last.up_btn.isEnabled()
        assert not last.down_btn.isEnabled()

    def test_single_row_disables_all_reorder_buttons(self):
        dialog = _make_dialog()
        row = dialog._steps_editor._rows[0]
        assert not row.up_btn.isEnabled()
        assert not row.down_btn.isEnabled()
        assert not row.remove_btn.isEnabled()

    def test_move_buttons_have_tooltips(self):
        dialog = _make_dialog()
        dialog._steps_editor.add_step()
        for row in dialog._steps_editor._rows:
            assert row.up_btn.toolTip() != ""
            assert row.down_btn.toolTip() != ""
            assert row.remove_btn.toolTip() != ""

    def test_step_list_never_renders_empty(self):
        # The empty state is structurally unreachable: clearing the list
        # always leaves one default row (spec section 13 empty states).
        dialog = _make_dialog()
        dialog._steps_editor.set_steps([])
        assert len(dialog._steps_editor._rows) == 1


class TestGoalHeadingStyle:
    def test_heading_styled_via_font_not_stylesheet(self):
        # Consistency (spec section 13): headings use QFont like the
        # manager's title, not a hardcoded-pixel stylesheet that ignores
        # the user's base font.
        dialog = _make_dialog()
        heading = dialog._goal_heading
        assert heading.styleSheet() == ""
        assert heading.font().bold()
        assert (
            heading.font().pointSize()
            > dialog._phrases_label.font().pointSize()
        )


# ---------------------------------------------------------------------------
# Try-it line (debounced pm_test_draft)
# ---------------------------------------------------------------------------


class TestTryItLine:
    def test_typing_restarts_debounce_timer(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._try_input.setText("deploy")
        assert dialog._try_timer.isActive()

    def test_empty_text_stops_timer_and_clears_result(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._try_input.setText("deploy")
        dialog._try_input.setText("")
        assert not dialog._try_timer.isActive()
        assert dialog._try_result_label.text() == ""

    def test_timeout_emits_pm_test_draft_with_draft_and_text(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._try_input.setText("deploy")
        dialog._send_test_draft()
        assert len(emitted) == 1
        msg = emitted[0]
        assert msg["action"] == "pm_test_draft"
        assert msg["data"]["text"] == "deploy"
        draft = msg["data"]["draft"]
        assert draft["phrases"] == ["deploy"]
        assert "exclude_pattern_id" not in draft

    def test_edit_mode_sets_exclude_pattern_id(self):
        entry = _simple_entry()
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._try_input.setText("deploy")
        dialog._send_test_draft()
        draft = emitted[0]["data"]["draft"]
        assert draft["exclude_pattern_id"] == entry["id"]

    def test_result_winner_draft_renders_match_message(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._try_input.setText("deploy")
        dialog.handle_response({
            "action": "pm_test_draft_result",
            "data": {
                "success": True, "draft_error": None, "draft_matches": True,
                "winner": "draft", "shadowed_by": None,
                "groups": [], "resolved_steps": [],
            },
        })
        assert "will run this pattern" in dialog._try_result_label.text()
        assert "deploy" in dialog._try_result_label.text()

    def test_result_winner_existing_names_shadowing_pattern(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._try_input.setText("save")
        dialog.handle_response({
            "action": "pm_test_draft_result",
            "data": {
                "success": True, "draft_error": None, "draft_matches": True,
                "winner": "existing",
                "shadowed_by": {
                    "pattern_id": "c" * 64,
                    "trigger_display": "save",
                    "is_user_created": False,
                },
                "groups": [], "resolved_steps": [],
            },
        })
        assert "save" in dialog._try_result_label.text()
        assert "first" in dialog._try_result_label.text()

    def test_result_draft_error_rendered_inline(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._try_input.setText("deploy")
        dialog.handle_response({
            "action": "pm_test_draft_result",
            "data": {
                "success": True,
                "draft_error": "Expression does not compile: boom",
                "draft_matches": False, "winner": "none",
                "shadowed_by": None, "groups": [], "resolved_steps": [],
            },
        })
        assert "boom" in dialog._try_result_label.text()

    def test_result_no_match_renders_no_match_message(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._try_input.setText("something else")
        dialog.handle_response({
            "action": "pm_test_draft_result",
            "data": {
                "success": True, "draft_error": None, "draft_matches": False,
                "winner": "none", "shadowed_by": None,
                "groups": [], "resolved_steps": [],
            },
        })
        assert "no pattern" in dialog._try_result_label.text().lower()

    def test_error_envelope_renders_error_not_no_match(self):
        # A pm_test_draft handler failure/timeout arrives as
        # {"success": False, "error": ...}; rendering it as the affirmative
        # "No pattern matches" would be a lie (wh-pattern-editor-r0.3).
        from create_pattern_dialog import _ERROR_STYLE

        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._try_input.setText("deploy")
        dialog.handle_response({
            "action": "pm_test_draft_result",
            "data": {"success": False, "error": "Draft test failed: boom"},
        })
        text = dialog._try_result_label.text()
        assert "Draft test failed: boom" in text
        assert "no pattern" not in text.lower()
        assert dialog._try_result_label.styleSheet() == _ERROR_STYLE

    def test_error_envelope_without_error_text_still_says_failed(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._try_input.setText("deploy")
        dialog.handle_response({
            "action": "pm_test_draft_result",
            "data": {"success": False},
        })
        text = dialog._try_result_label.text()
        assert text != ""
        assert "no pattern" not in text.lower()


class TestTryResultCorrelation:
    """A try-it answer renders only against the request that produced it
    (wh-pattern-editor-r6.1): every pm_test_draft carries an increasing
    request_id, a result carrying an out-of-date id is dropped, and a
    current result names the text that was actually tested."""

    def _draft_result(self, request_id=None, winner="draft"):
        data = {
            "success": True, "draft_error": None,
            "draft_matches": winner == "draft", "winner": winner,
            "shadowed_by": None, "groups": [], "resolved_steps": [],
        }
        if request_id is not None:
            data["request_id"] = request_id
        return {"action": "pm_test_draft_result", "data": data}

    def test_requests_carry_increasing_request_ids(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._try_input.setText("deploy")
        dialog._send_test_draft()
        dialog._try_input.setText("deploy now")
        dialog._send_test_draft()
        ids = [m["data"]["request_id"] for m in emitted]
        assert ids == [1, 2]

    def test_out_of_date_result_is_dropped(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._try_input.setText("deploy")
        dialog._send_test_draft()
        dialog._try_input.setText("deploy now")
        dialog._send_test_draft()
        old_id = emitted[0]["data"]["request_id"]
        dialog.handle_response(self._draft_result(request_id=old_id))
        assert dialog._try_result_label.text() == ""

    def test_current_result_renders_sent_text_not_box_text(self):
        # The box was edited after the send; the answer is about the SENT
        # text, and the pending debounce refreshes for the new text.
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._try_input.setText("deploy")
        dialog._send_test_draft()
        dialog._try_input.setText("deploy now")
        current_id = emitted[0]["data"]["request_id"]
        dialog.handle_response(self._draft_result(request_id=current_id))
        text = dialog._try_result_label.text()
        assert "'deploy'" in text
        assert "deploy now" not in text

    def test_result_without_request_id_still_renders(self):
        # Compatibility: an id-less result keeps the old tolerant path
        # (handler failure envelopes and older Logic processes carry none).
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._try_input.setText("deploy")
        dialog.handle_response(self._draft_result())
        assert "will run this pattern" in dialog._try_result_label.text()

    def test_clearing_input_invalidates_in_flight_result(self):
        # Clearing the box is a deliberate reset; an answer still in
        # flight must not reappear in the cleared box
        # (wh-pattern-editor-r7.1).
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._try_input.setText("deploy")
        dialog._send_test_draft()
        in_flight_id = emitted[0]["data"]["request_id"]
        dialog._try_input.setText("")
        dialog.handle_response(self._draft_result(request_id=in_flight_id))
        assert dialog._try_result_label.text() == ""


# ---------------------------------------------------------------------------
# Save result handling
# ---------------------------------------------------------------------------


class TestSaveResults:
    def test_create_success_accepts_dialog(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog.handle_response({
            "action": "pm_create_result", "data": {"success": True},
        })
        assert dialog.result() == QDialog.DialogCode.Accepted

    def test_create_failure_shows_error_and_keeps_dialog_open(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._save_btn.click()
        dialog.handle_response({
            "action": "pm_create_result",
            "data": {"success": False, "error": "Disk on fire"},
        })
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert "Disk on fire" in dialog._save_error_label.text()
        assert not dialog._save_error_label.isHidden()
        # The user can retry.
        assert dialog._save_btn.isEnabled()

    def test_update_stale_id_error_surfaced_verbatim(self):
        entry = _simple_entry()
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        stale = (
            "Pattern with ID aaaaaaaaaaaa... not found; it may have been "
            "changed or deleted outside this window"
        )
        dialog.handle_response({
            "action": "pm_update_result",
            "data": {"success": False, "error": stale},
        })
        assert dialog._save_error_label.text() == stale

    def test_save_click_disables_button_until_result(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._save_btn.click()
        assert not dialog._save_btn.isEnabled()


# ---------------------------------------------------------------------------
# Save timeout + in-flight gating (wh-pattern-editor-r0.2)
# ---------------------------------------------------------------------------


class TestSaveTimeout:
    """A lost create/update result must not leave the dialog stuck, and a
    merely DELAYED one must not allow a second Save click to double-create:
    _validate() may not re-enable Save from field state while a save is in
    flight."""

    def _click_save(self, dialog):
        _fill_valid_hotkey(dialog)
        dialog._save_btn.click()

    def test_save_click_starts_timeout_timer(self):
        dialog = _make_dialog()
        self._click_save(dialog)
        assert dialog._save_timeout_timer.isActive()

    def test_timeout_re_enables_save_and_shows_message(self):
        dialog = _make_dialog()
        self._click_save(dialog)
        dialog._on_save_timeout()
        assert not dialog._save_timeout_timer.isActive()
        assert dialog._save_btn.isEnabled()
        assert not dialog._save_error_label.isHidden()
        assert "did not respond" in dialog._save_error_label.text()

    def test_success_result_stops_timeout_timer(self):
        dialog = _make_dialog()
        self._click_save(dialog)
        dialog.handle_response({
            "action": "pm_create_result", "data": {"success": True},
        })
        assert not dialog._save_timeout_timer.isActive()

    def test_failure_result_stops_timer_and_re_enables_save(self):
        dialog = _make_dialog()
        self._click_save(dialog)
        dialog.handle_response({
            "action": "pm_create_result",
            "data": {"success": False, "error": "nope"},
        })
        assert not dialog._save_timeout_timer.isActive()
        assert dialog._save_btn.isEnabled()

    def test_field_edit_during_in_flight_save_keeps_save_disabled(self):
        # The double-create path: any field edit re-runs _validate(),
        # which used to re-enable Save purely from field state.
        dialog = _make_dialog()
        self._click_save(dialog)
        dialog._key_input.setText("ctrl+e")
        assert not dialog._save_btn.isEnabled()
        dialog.handle_response({
            "action": "pm_create_result",
            "data": {"success": False, "error": "nope"},
        })
        assert dialog._save_btn.isEnabled()

    def test_field_edit_during_in_flight_save_advanced_pane(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        dialog._advanced_toggle.setChecked(True)
        dialog._save_btn.click()
        dialog._expression_edit.setText("^deploy$")
        assert not dialog._save_btn.isEnabled()

    def test_after_timeout_edits_validate_normally_again(self):
        dialog = _make_dialog()
        self._click_save(dialog)
        dialog._on_save_timeout()
        dialog._key_input.setText("ctrl+e")
        assert dialog._save_btn.isEnabled()


# ---------------------------------------------------------------------------
# Manager dialog wiring (call-site compatibility)
# ---------------------------------------------------------------------------


class TestManagerWiring:
    def _manager(self):
        from pattern_manager_dialog import PatternManagerDialog
        return PatternManagerDialog(parent=None)

    def test_add_clicked_routes_editor_actions_through_manager(self):
        from create_pattern_dialog import CreatePatternDialog

        manager = self._manager()
        emitted = []
        manager.pattern_action.connect(emitted.append)
        seen = {}

        def fake_exec(dialog_self):
            seen["editor_during_exec"] = manager._editor_dialog
            dialog_self.pattern_action.emit({"action": "probe"})
            return 0

        with patch.object(CreatePatternDialog, "exec", fake_exec):
            manager._on_add_clicked()

        assert seen["editor_during_exec"] is not None
        assert manager._editor_dialog is None
        assert {"action": "probe"} in emitted

    def test_create_result_forwarded_to_open_editor_and_tree_refreshed(self):
        manager = self._manager()
        editor = MagicMock()
        manager._editor_dialog = editor
        emitted = []
        manager.pattern_action.connect(emitted.append)
        message = {"action": "pm_create_result", "data": {"success": True}}
        manager.handle_response(message)
        editor.handle_response.assert_called_once_with(message)
        assert {"action": "pm_get_patterns"} in emitted

    def test_create_failure_with_editor_open_shows_no_modal(self):
        manager = self._manager()
        editor = MagicMock()
        manager._editor_dialog = editor
        message = {
            "action": "pm_create_result",
            "data": {"success": False, "error": "nope"},
        }
        with patch("pattern_manager_dialog.QMessageBox.warning") as warn:
            manager.handle_response(message)
        warn.assert_not_called()
        editor.handle_response.assert_called_once_with(message)

    def test_create_failure_without_editor_keeps_modal_fallback(self):
        manager = self._manager()
        message = {
            "action": "pm_create_result",
            "data": {"success": False, "error": "nope"},
        }
        with patch("pattern_manager_dialog.QMessageBox.warning") as warn:
            manager.handle_response(message)
        warn.assert_called_once()

    def test_update_result_forwarded_and_tree_refreshed_on_success(self):
        manager = self._manager()
        editor = MagicMock()
        manager._editor_dialog = editor
        emitted = []
        manager.pattern_action.connect(emitted.append)
        message = {"action": "pm_update_result", "data": {"success": True}}
        manager.handle_response(message)
        editor.handle_response.assert_called_once_with(message)
        assert {"action": "pm_get_patterns"} in emitted

    def test_test_draft_result_forwarded_to_editor(self):
        manager = self._manager()
        editor = MagicMock()
        manager._editor_dialog = editor
        message = {"action": "pm_test_draft_result", "data": {"success": True}}
        manager.handle_response(message)
        editor.handle_response.assert_called_once_with(message)


# ---------------------------------------------------------------------------
# Logic handler: pm_create_pattern passes phrases through (main.py branch)
# ---------------------------------------------------------------------------

SYSTEM_CONTENT = (
    'COMMAND_HOTWORD = "x-ray"\n'
    '\n'
    '[[pattern]]\n'
    "pattern = '''^save$'''\n"
    'requires_hotword = true\n'
    'actions = [{ function = "hk", params = ["ctrl", "s"] }]\n'
)


class _FakeTextParser:
    def __init__(self):
        self.patterns = []


class _FakeSpeechHandler:
    def __init__(self, patterns_file, user_patterns_file):
        from speech.pattern_catalog import PatternCatalog
        self.patterns_file = patterns_file
        self.user_patterns_file = user_patterns_file
        self.pattern_catalog = PatternCatalog(patterns_file, user_patterns_file)
        self.text_parser = _FakeTextParser()

    def apply_hotword(self, hotword):
        pass


class _CapturingQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


def _make_controller(system_file, user_file):
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller._handle_pattern_manager_action = (
        LogicController._handle_pattern_manager_action.__get__(controller)
    )
    handler = _FakeSpeechHandler(system_file, user_file)
    controller.service_manager = MagicMock()
    controller.service_manager.speech_handler = handler
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = _CapturingQueue()
    return controller, handler


class TestCreatePatternPhrasesPassThrough:
    async def test_create_with_phrases_and_no_trigger(self, tmp_path):
        system_file = tmp_path / "patterns.toml"
        system_file.write_text(SYSTEM_CONTENT, encoding="utf-8")
        user_file = tmp_path / "user_patterns.toml"
        controller, handler = _make_controller(str(system_file), str(user_file))

        await controller._handle_pattern_manager_action(
            "pm_create_pattern",
            {"data": {
                # No trigger key at all: simple mode sends phrases instead.
                "pattern_type": "command",
                "action_type": "hotkey",
                "action_params": {"keys": ["ctrl", "d"]},
                "requires_hotword": False,
                "phrases": ["deploy", "ship it"],
            }},
        )

        results = [
            m for m in controller.state_manager.state_to_gui_queue.items
            if m.get("action") == "pm_create_result"
        ]
        assert len(results) == 1
        assert results[0]["data"]["success"] is True, results[0]["data"]

        with open(user_file, "rb") as fh:
            written = tomllib.load(fh)["pattern"][0]
        assert written["phrases"] == ["deploy", "ship it"]
        assert written["pattern"] == generate_expression(
            ["deploy", "ship it"], "command",
        )


class TestCreatePatternRawPassThrough:
    async def test_create_with_expression_and_raw_actions(self, tmp_path):
        # Advanced-mode save: no trigger, no phrases, no action_type or
        # action_params keys at all -- the branch must pass expression and
        # actions through without KeyError (wh-pattern-editor-advanced).
        system_file = tmp_path / "patterns.toml"
        system_file.write_text(SYSTEM_CONTENT, encoding="utf-8")
        user_file = tmp_path / "user_patterns.toml"
        controller, handler = _make_controller(str(system_file), str(user_file))

        await controller._handle_pattern_manager_action(
            "pm_create_pattern",
            {"data": {
                "pattern_type": "command",
                "expression": r"^find\s+(.+)$",
                "actions": [
                    {"function": "hk", "params": ["ctrl", "f"]},
                    {"function": "type_text", "params": ["g1"]},
                ],
                "requires_hotword": False,
            }},
        )

        results = [
            m for m in controller.state_manager.state_to_gui_queue.items
            if m.get("action") == "pm_create_result"
        ]
        assert len(results) == 1
        assert results[0]["data"]["success"] is True, results[0]["data"]

        with open(user_file, "rb") as fh:
            written = tomllib.load(fh)["pattern"][0]
        assert written["pattern"] == r"^find\s+(.+)$"
        assert written["type"] == "command"
        assert "phrases" not in written
        assert written["actions"] == [
            {"function": "hk", "params": ["ctrl", "f"]},
            {"function": "type_text", "params": ["g1"]},
        ]
        # The reloaded catalog carries the new pattern (the type key is an
        # unknown key to the loader and must be tolerated).
        patterns = handler.pattern_catalog.get_all_patterns()
        assert any(p["raw_pattern"] == r"^find\s+(.+)$" for p in patterns)


class TestAwaitsDoneGuiRoundTrip:
    """The editor must not strip awaits_done from steps it round-trips
    (wh-pattern-editor-r8.1)."""

    def test_step_row_round_trips_awaits_done(self):
        from create_pattern_dialog import ActionStepRow

        row = ActionStepRow()
        row.set_step(
            {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
        )
        assert row.step() == {
            "function": "hk", "params": ["ctrl", "c"], "awaits_done": True,
        }

    def test_preserved_row_round_trips_awaits_done(self):
        from create_pattern_dialog import ActionStepRow

        row = ActionStepRow()
        row.set_step({
            "function": "capture_clipboard", "params": [],
            "awaits_done": True,
        })
        assert row.step() == {
            "function": "capture_clipboard", "params": [],
            "awaits_done": True,
        }

    def test_changing_function_drops_carried_keys(self):
        from create_pattern_dialog import ActionStepRow

        row = ActionStepRow()
        row.set_step(
            {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
        )
        combo = row._function_combo
        idx = next(
            i for i in range(combo.count()) if combo.itemData(i) == "press"
        )
        combo.setCurrentIndex(idx)
        assert "awaits_done" not in row.step()

    def test_step_with_awaits_done_opens_in_advanced(self):
        # The simple pane rebuilds actions from scratch and would lose the
        # flag; a step carrying it must open in the advanced pane where
        # rows round-trip extras.
        entry = _simple_entry(raw_actions=[
            {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
        ])
        dialog = _make_dialog(entry=entry, pattern_id=entry["id"])
        assert dialog._mode == "advanced"
        assert dialog._steps_editor.steps() == [
            {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
        ]


class TestRepeatFieldValidation:
    """Garbage in the hk repeat field silently kills the whole key chord
    at runtime; the press key field skipped key-name validation
    (wh-pattern-editor-r8.4)."""

    def _advanced_dialog(self, steps):
        entry = _simple_entry(
            raw_pattern="^deploy$",
            raw_actions=steps,
        )
        del entry["phrases"]
        return _make_dialog(entry=entry, pattern_id=entry["id"])

    def test_garbage_repeat_blocks_save(self):
        dialog = self._advanced_dialog(
            [{"function": "hk", "params": ["ctrl", "z"]}],
        )
        row = dialog._steps_editor._rows[0]
        row.set_param_value(1, "abc")
        dialog._validate_advanced()
        assert "Repeat" in dialog._steps_error_label.text()
        assert not dialog._save_btn.isEnabled()

    def test_digit_and_group_ref_repeat_allowed(self):
        dialog = self._advanced_dialog(
            [{"function": "hk", "params": ["ctrl", "z"]}],
        )
        row = dialog._steps_editor._rows[0]
        for good in ("3", "g1"):
            row.set_param_value(1, good)
            dialog._validate_advanced()
            assert "Repeat" not in dialog._steps_error_label.text()

    def test_press_unknown_key_blocks_save(self):
        dialog = self._advanced_dialog(
            [{"function": "press", "params": ["foo"]}],
        )
        dialog._validate_advanced()
        assert "Unknown key name: 'foo'" in dialog._steps_error_label.text()
        assert not dialog._save_btn.isEnabled()

    def test_press_known_key_allowed(self):
        dialog = self._advanced_dialog(
            [{"function": "press", "params": ["enter"]}],
        )
        dialog._validate_advanced()
        assert "Unknown key name" not in dialog._steps_error_label.text()


class TestPositionCarryOnCreate:
    """Customize/Duplicate of a trailing pattern must not silently drop
    the position key -- the create path needs the same carry-forward the
    update path already has (wh-pattern-editor-r8.5)."""

    def test_entry_position_included_in_create_payload(self):
        entry = _simple_entry()
        entry["position"] = "trailing"
        # No pattern_id: this is the Customize/Duplicate create flow.
        dialog = _make_dialog(entry=entry)
        assert dialog.get_pattern_data().get("position") == "trailing"

    def test_no_position_key_when_absent(self):
        dialog = _make_dialog(entry=_simple_entry())
        assert "position" not in dialog.get_pattern_data()

    def test_non_string_position_not_carried(self):
        entry = _simple_entry()
        entry["position"] = 5
        dialog = _make_dialog(entry=entry)
        assert "position" not in dialog.get_pattern_data()

    def test_entry_position_included_in_advanced_payload(self):
        # A shipped trailing pattern has no phrases metadata, so Customize
        # opens it in ADVANCED mode -- the carry must hold on that exit of
        # get_pattern_data too, not just the simple one
        # (wh-int8-punctuation-mishears.1.3, same gap pattern as the
        # whole_utterance_only flag).
        entry = _simple_entry(
            phrases=None,
            raw_pattern="submit",
            raw_actions=[{"function": "press", "params": ["enter"]}],
        )
        entry["position"] = "trailing"
        dialog = _make_dialog(entry=entry)
        assert dialog._mode == "advanced"
        assert dialog.get_pattern_data().get("position") == "trailing"

    async def test_create_handler_writes_position(self, tmp_path):
        system_file = tmp_path / "patterns.toml"
        system_file.write_text(SYSTEM_CONTENT, encoding="utf-8")
        user_file = tmp_path / "user_patterns.toml"
        controller, handler = _make_controller(str(system_file), str(user_file))

        await controller._handle_pattern_manager_action(
            "pm_create_pattern",
            {"data": {
                # An unanchored trailing expression passes the advanced
                # pane's anchor check as a replacement -- exactly what a
                # Customize of the shipped trailing "submit" would send.
                "pattern_type": "replacement",
                "expression": "submit",
                "actions": [{"function": "hk", "params": ["enter"]}],
                "requires_hotword": False,
                "position": "trailing",
            }},
        )
        results = [
            m for m in controller.state_manager.state_to_gui_queue.items
            if m.get("action") == "pm_create_result"
        ]
        assert results[0]["data"]["success"] is True, results[0]["data"]
        with open(user_file, "rb") as fh:
            written = tomllib.load(fh)["pattern"][0]
        assert written["position"] == "trailing"


class TestWholeUtteranceCarryOnCreate:
    """Customize/Duplicate of a shipped punctuation alias must not silently
    drop ``whole_utterance_only`` -- same carry-forward as position
    (review finding wh-int8-punctuation-mishears.1.1)."""

    def test_entry_flag_included_in_create_payload(self):
        entry = _simple_entry()
        entry["whole_utterance_only"] = True
        # No pattern_id: this is the Customize/Duplicate create flow.
        dialog = _make_dialog(entry=entry)
        assert dialog.get_pattern_data().get("whole_utterance_only") is True

    def test_no_flag_key_when_absent(self):
        dialog = _make_dialog(entry=_simple_entry())
        assert "whole_utterance_only" not in dialog.get_pattern_data()

    def test_non_bool_flag_not_carried(self):
        entry = _simple_entry()
        entry["whole_utterance_only"] = "true"
        dialog = _make_dialog(entry=entry)
        assert "whole_utterance_only" not in dialog.get_pattern_data()

    def test_entry_flag_included_in_advanced_payload(self):
        # The shipped alias entries (^colin$, ^(?:come|kama|commer|come on)$)
        # carry NO phrases metadata, so Customize opens them in ADVANCED
        # mode. The advanced exit of get_pattern_data must attach the flag
        # too -- the simple-mode test alone would stay green if the
        # advanced attach were deleted (wh-int8-punctuation-mishears.1.3).
        entry = _simple_entry(
            phrases=None,
            raw_pattern="^colin$",
            raw_actions=[{"function": "text", "params": [":"]}],
        )
        entry["whole_utterance_only"] = True
        dialog = _make_dialog(entry=entry)
        assert dialog._mode == "advanced"
        data = dialog.get_pattern_data()
        assert data.get("expression") == "^colin$"
        assert data.get("whole_utterance_only") is True

    def test_flag_survives_simple_to_advanced_switch(self):
        # Switching modes mid-edit must not lose the hidden carry.
        entry = _simple_entry()
        entry["whole_utterance_only"] = True
        dialog = _make_dialog(entry=entry)
        assert dialog._mode == "simple"
        dialog._advanced_toggle.setChecked(True)
        assert dialog._mode == "advanced"
        assert dialog.get_pattern_data().get("whole_utterance_only") is True

    async def test_create_handler_writes_flag(self, tmp_path):
        system_file = tmp_path / "patterns.toml"
        system_file.write_text(SYSTEM_CONTENT, encoding="utf-8")
        user_file = tmp_path / "user_patterns.toml"
        controller, handler = _make_controller(str(system_file), str(user_file))

        await controller._handle_pattern_manager_action(
            "pm_create_pattern",
            {"data": {
                # Exactly what a Customize of the shipped "^colin$" alias
                # sends: an anchored command expression plus the flag.
                "pattern_type": "command",
                "expression": "^colin$",
                "actions": [{"function": "text", "params": [":"]}],
                "requires_hotword": False,
                "whole_utterance_only": True,
            }},
        )
        results = [
            m for m in controller.state_manager.state_to_gui_queue.items
            if m.get("action") == "pm_create_result"
        ]
        assert results[0]["data"]["success"] is True, results[0]["data"]
        with open(user_file, "rb") as fh:
            written = tomllib.load(fh)["pattern"][0]
        assert written["whole_utterance_only"] is True


class TestSaveResultCorrelation:
    """A save answer must act only on the save that produced it: a result
    for a superseded save is ignored, and a success that lands after the
    watchdog fired must not close the dialog over newer edits
    (wh-pattern-editor-r8.6)."""

    def _save_result(self, request_id=None, success=True):
        data = {"success": success}
        if not success:
            data["error"] = "nope"
        if request_id is not None:
            data["request_id"] = request_id
        return {"action": "pm_create_result", "data": data}

    def test_save_sends_request_id(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._on_save_clicked()
        assert emitted[0]["data"]["request_id"] == 1

    def test_current_success_still_accepts(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(True))
        dialog._on_save_clicked()
        dialog.handle_response(self._save_result(request_id=1))
        assert accepted

    def test_superseded_save_result_ignored(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(True))
        dialog._on_save_clicked()
        dialog._on_save_timeout()
        dialog._on_save_clicked()   # second save, new request id
        dialog.handle_response(self._save_result(request_id=1))
        assert not accepted

    def test_late_success_after_timeout_shows_notice_not_close(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(True))
        dialog._on_save_clicked()
        dialog._on_save_timeout()
        dialog.handle_response(self._save_result(request_id=1))
        assert not accepted
        assert "Saved" in dialog._save_error_label.text()
        assert dialog._save_error_label.isVisibleTo(dialog)

    def test_idless_result_keeps_legacy_accept(self):
        dialog = _make_dialog()
        _fill_valid_hotkey(dialog)
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(True))
        dialog._on_save_clicked()
        dialog.handle_response(self._save_result())
        assert accepted


class TestSaveResultRequestIdEcho:
    """The create/update handlers echo the sender's request_id so the
    dialog can pair the answer with its save (wh-pattern-editor-r8.6)."""

    async def test_create_result_echoes_request_id(self, tmp_path):
        system_file = tmp_path / "patterns.toml"
        system_file.write_text(SYSTEM_CONTENT, encoding="utf-8")
        user_file = tmp_path / "user_patterns.toml"
        controller, handler = _make_controller(str(system_file), str(user_file))
        await controller._handle_pattern_manager_action(
            "pm_create_pattern",
            {"data": {
                "pattern_type": "command",
                "phrases": ["deploy"],
                "action_type": "hotkey",
                "action_params": {"keys": ["ctrl", "d"]},
                "requires_hotword": False,
                "request_id": 4,
            }},
        )
        results = [
            m for m in controller.state_manager.state_to_gui_queue.items
            if m.get("action") == "pm_create_result"
        ]
        assert results[0]["data"]["request_id"] == 4

    async def test_update_result_echoes_request_id(self, tmp_path):
        system_file = tmp_path / "patterns.toml"
        system_file.write_text(SYSTEM_CONTENT, encoding="utf-8")
        user_file = tmp_path / "user_patterns.toml"
        controller, handler = _make_controller(str(system_file), str(user_file))
        await controller._handle_pattern_manager_action(
            "pm_create_pattern",
            {"data": {
                "pattern_type": "command",
                "phrases": ["deploy"],
                "action_type": "hotkey",
                "action_params": {"keys": ["ctrl", "d"]},
                "requires_hotword": False,
            }},
        )
        created = [
            m for m in controller.state_manager.state_to_gui_queue.items
            if m.get("action") == "pm_create_result"
        ][0]["data"]
        await controller._handle_pattern_manager_action(
            "pm_update_pattern",
            {"data": {
                "pattern_id": created["pattern_id"],
                "request_id": 9,
                "data": {
                    "pattern_type": "command",
                    "phrases": ["deploy"],
                    "action_type": "hotkey",
                    "action_params": {"keys": ["ctrl", "e"]},
                    "requires_hotword": False,
                },
            }},
        )
        results = [
            m for m in controller.state_manager.state_to_gui_queue.items
            if m.get("action") == "pm_update_result"
        ]
        assert results[0]["data"]["request_id"] == 9


# ---------------------------------------------------------------------------
# Action picker order (wh-action-picker-ordering)
# ---------------------------------------------------------------------------


def _picker_rows(combo):
    """(text, function name, enabled) for every row of a step picker."""
    model = combo.model()
    return [
        (combo.itemText(i), combo.itemData(i), model.item(i).isEnabled())
        for i in range(combo.count())
    ]


class TestActionPickerOrder:
    """The picker listed 37 entries in source-file order, which a user
    cannot predict (wh-action-picker-ordering). The visible rule now: A-Z
    by label, with the long advanced list split into A-Z named groups."""

    def _combo(self):
        dialog = _make_dialog()
        return dialog._steps_editor._rows[0]._function_combo

    def _sections(self, combo):
        """Parse the picker into {audience heading: [(group, [labels])]}.

        A disabled row is a heading; an indented disabled row is a group
        sub-heading, which is exactly how a user tells the two apart.
        """
        sections = {}
        audience = None
        group = None
        for text, name, enabled in _picker_rows(combo):
            if not enabled:
                if text.startswith(" "):
                    group = text.strip()
                    sections[audience].append((group, []))
                else:
                    audience = text.strip()
                    group = None
                    sections[audience] = []
                continue
            assert audience is not None, "an entry appeared before a heading"
            if not sections[audience]:
                sections[audience].append((None, []))
            sections[audience][-1][1].append(text)
        return sections

    def test_basic_entries_are_alphabetical_under_one_heading(self):
        sections = self._sections(self._combo())
        basic = sections["Basic actions"]
        assert len(basic) == 1
        group, labels = basic[0]
        assert group is None
        assert labels == sorted(labels, key=str.casefold)

    def test_advanced_entries_sit_under_alphabetical_group_headings(self):
        sections = self._sections(self._combo())
        advanced = sections["Advanced actions"]
        groups = [group for group, _labels in advanced]
        assert len(groups) > 1
        assert all(group is not None for group in groups)
        assert groups == sorted(groups, key=str.casefold)
        for group, labels in advanced:
            assert labels, group
            assert labels == sorted(labels, key=str.casefold), group

    def test_group_headings_are_not_selectable_actions(self):
        for text, name, enabled in _picker_rows(self._combo()):
            if not enabled:
                assert name is None, text

    def test_no_action_moves_between_the_two_headings(self):
        from speech.action_catalog import CATALOG_BY_NAME

        combo = self._combo()
        rows = _picker_rows(combo)
        seen = {"Basic actions": [], "Advanced actions": []}
        audience = None
        for text, name, enabled in rows:
            if not enabled and not text.startswith(" "):
                audience = text.strip()
            elif name is not None:
                seen[audience].append(name)
        assert seen["Basic actions"] and seen["Advanced actions"]
        for heading, expected in (
            ("Basic actions", "basic"),
            ("Advanced actions", "advanced"),
        ):
            for name in seen[heading]:
                assert CATALOG_BY_NAME[name]["audience"] == expected

    def test_new_step_still_defaults_to_the_hotkey_action(self):
        # Sorting must not silently change which action a fresh step
        # starts on; the editor has always opened on the hotkey action.
        dialog = _make_dialog()
        assert dialog._steps_editor._rows[0].function_name() == "hk"


# ---------------------------------------------------------------------------
# Step result names (wh-editor-step-result-name)
# ---------------------------------------------------------------------------


def _row_for(function: str):
    """A standalone step row switched to one catalog function."""
    from create_pattern_dialog import ActionStepRow

    row = ActionStepRow()
    index = row._function_combo.findData(function)
    assert index >= 0, function
    row._function_combo.setCurrentIndex(index)
    return row


class TestStepResultName:
    """The editor preserved a hand-written ``result`` name but offered no
    way to create one, so a user could not connect one step's answer to
    the next step's parameter (wh-editor-step-result-name)."""

    def test_field_offered_only_for_actions_that_produce_a_value(self):
        for function in ("ask_ai", "run_capture", "date"):
            assert _row_for(function)._result_edit is not None, function
        for function in ("hk", "press", "insert_text"):
            assert _row_for(function)._result_edit is None, function

    def test_typed_name_becomes_the_step_result_key(self):
        row = _row_for("ask_ai")
        row.set_param_value(0, "Answer in one short sentence: g1")
        row.set_result_name("answer")
        assert row.step() == {
            "function": "ask_ai",
            "params": ["Answer in one short sentence: g1"],
            "result": "answer",
        }

    def test_empty_name_writes_no_result_key_at_all(self):
        row = _row_for("ask_ai")
        row.set_param_value(0, "hello")
        assert row.step() == {"function": "ask_ai", "params": ["hello"]}
        row.set_result_name("   ")
        assert "result" not in row.step()

    def test_stored_name_loads_into_the_field_and_round_trips(self):
        step = {
            "function": "ask_ai", "params": ["g1"], "result": "answer",
        }
        row = _row_for("hk")
        row.set_step(step)
        assert row.result_name() == "answer"
        assert row.step() == step

    def test_clearing_the_field_drops_a_loaded_name(self):
        row = _row_for("hk")
        row.set_step(
            {"function": "ask_ai", "params": ["g1"], "result": "answer"},
        )
        row.set_result_name("")
        assert row.step() == {"function": "ask_ai", "params": ["g1"]}

    def test_name_survives_alongside_other_carried_keys(self):
        step = {
            "function": "run_capture",
            "params": ["C:\\Tools\\lookup.exe", "g1"],
            "awaits_done": True,
            "result": "answer",
        }
        row = _row_for("hk")
        row.set_step(step)
        assert row.result_name() == "answer"
        assert row.step() == step


def _switch_function(row, function: str):
    """Drive the row's picker to another function, as a user would."""
    index = row._function_combo.findData(function)
    assert index >= 0, function
    row._function_combo.setCurrentIndex(index)


class TestStoredResultNameSurvivesFunctionSwitch:
    """Before this batch, _with_extras re-attached a stored ``result``
    whenever the function matched the loaded one, so switching away and
    back kept the name. _load_result_extra pops the name into the field,
    and a function switch destroys the field, so the name was lost
    permanently (wh-review-pattern-fixes.4)."""

    def _loaded_row(self):
        row = _row_for("hk")
        row.set_step(
            {"function": "ask_ai", "params": ["g1"], "result": "answer"},
        )
        return row

    def test_switch_away_and_back_keeps_the_stored_name(self):
        row = self._loaded_row()
        _switch_function(row, "insert_text")
        _switch_function(row, "ask_ai")
        assert row.result_name() == "answer"
        assert row.step().get("result") == "answer"

    def test_switch_through_another_result_producer_keeps_the_name(self):
        row = self._loaded_row()
        _switch_function(row, "date")
        # The name belongs to the LOADED function only; date's fresh
        # field stays empty.
        assert row.result_name() == ""
        _switch_function(row, "ask_ai")
        assert row.step().get("result") == "answer"

    def test_fresh_typed_name_still_vanishes_on_function_switch(self):
        # Matches how params behave: only the LOADED name round-trips.
        row = _row_for("ask_ai")
        row.set_result_name("mine")
        _switch_function(row, "date")
        _switch_function(row, "ask_ai")
        assert row.result_name() == ""
        assert "result" not in row.step()


class TestResultNameValidation:
    """A result name is a lookup key the engine writes into the run
    context, so a name that shadows a capture group, repeats another
    step's name, or hides another step's stored value must not save."""

    def _dialog(self, steps, expression="^ask (.+)$"):
        entry = _simple_entry(raw_pattern=expression, raw_actions=steps)
        del entry["phrases"]
        return _make_dialog(entry=entry, pattern_id=entry["id"])

    def _ask_and_insert(self, result="answer"):
        return [
            {"function": "ask_ai", "params": ["g1"], "result": result},
            {"function": "insert_text", "params": [result]},
        ]

    def test_a_plain_name_saves_and_reaches_the_save_payload(self):
        dialog = self._dialog(self._ask_and_insert())
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()
        assert dialog.get_pattern_data()["actions"] == self._ask_and_insert()

    def test_capture_group_name_blocks_save(self):
        dialog = self._dialog(self._ask_and_insert())
        dialog._steps_editor._rows[0].set_result_name("g1")
        dialog._validate_advanced()
        assert "capture group" in dialog._steps_error_label.text().lower()
        assert not dialog._save_btn.isEnabled()

    def test_name_with_spaces_or_punctuation_blocks_save(self):
        dialog = self._dialog(self._ask_and_insert())
        dialog._steps_editor._rows[0].set_result_name("the answer")
        dialog._validate_advanced()
        assert "result name" in dialog._steps_error_label.text().lower()
        assert not dialog._save_btn.isEnabled()

    def test_two_steps_with_the_same_name_block_save(self):
        dialog = self._dialog([
            {"function": "ask_ai", "params": ["g1"], "result": "answer"},
            {"function": "ask_ai", "params": ["g1"], "result": "answer"},
        ])
        dialog._validate_advanced()
        assert "answer" in dialog._steps_error_label.text()
        assert not dialog._save_btn.isEnabled()

    def test_name_that_hides_another_steps_value_blocks_save(self):
        # The engine also stores every step's value under its function
        # name, so naming this result "date" would overwrite what the
        # date step stored (command_engine.py lines 342-347).
        dialog = self._dialog([
            {"function": "date", "params": ["%Y-%m-%d"]},
            {"function": "ask_ai", "params": ["g1"], "result": "date"},
        ])
        dialog._validate_advanced()
        assert "date" in dialog._steps_error_label.text()
        assert not dialog._save_btn.isEnabled()

    def test_a_step_may_reuse_its_own_function_name(self):
        # Same value under the same key: nothing is hidden.
        dialog = self._dialog([
            {"function": "date", "params": ["%Y-%m-%d"], "result": "date"},
            {"function": "insert_text", "params": ["date"]},
        ])
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()


class TestResultNameCollisionOnlyForResultProducers:
    """The function-name collision check must flag only functions in
    RESULT_PRODUCING_ACTIONS. The engine stores context[func_name] only
    when the step's return value is a str (command_engine.py lines
    342-343), so the name of a dict-returning action (press, hk, ...)
    never becomes a context key, and a result key may safely reuse it
    (wh-review-pattern-fixes.20)."""

    def _dialog(self, steps, expression="^ask (.+)$"):
        entry = _simple_entry(raw_pattern=expression, raw_actions=steps)
        del entry["phrases"]
        return _make_dialog(entry=entry, pattern_id=entry["id"])

    def test_result_named_after_a_dict_returning_step_saves(self):
        # The finding's sequence: at run time ask_ai stores its string
        # under context['press'] via the result key, the press step
        # returns a UI-action dict and never enters the str store, and
        # type_text receives the stored string by exact lookup. Nothing
        # is hidden, so the editor must not refuse the name.
        dialog = self._dialog([
            {"function": "ask_ai", "params": ["g1"], "result": "press"},
            {"function": "press", "params": ["tab"]},
            {"function": "type_text", "params": ["press"]},
        ])
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_result_named_after_a_later_dict_returning_step_saves(self):
        # Same exemption when the dict-returning step comes later: hk
        # never stores context['hk'], so the earlier result name 'hk'
        # hides nothing.
        dialog = self._dialog([
            {"function": "ask_ai", "params": ["g1"], "result": "hk"},
            {"function": "hk", "params": ["ctrl", "c"]},
        ])
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_result_named_after_a_separate_ask_ai_step_still_blocks(self):
        # Control: ask_ai IS in RESULT_PRODUCING_ACTIONS, so a result
        # name equal to a SEPARATE ask_ai step's function name still
        # hides that step's stored value and must still flag. (The
        # matching 'date' control is
        # TestResultNameValidation.test_name_that_hides_another_steps
        # _value_blocks_save.)
        dialog = self._dialog([
            {"function": "ask_ai", "params": ["g1"], "result": "ask_ai"},
            {"function": "ask_ai", "params": ["g1"]},
        ])
        dialog._validate_advanced()
        assert "ask_ai" in dialog._steps_error_label.text()
        assert not dialog._save_btn.isEnabled()


class TestResultNamePreservedRows:
    """A preserved (read-only) row has no result-name field, so an error
    about its stored name cannot be fixed in the dialog. The sibling
    checks (invalid_key_name, invalid_repeat_value,
    invalid_run_capture_timeout) skip preserved rows for that reason;
    the result-name check must do the same
    (wh-review-pattern-fixes.3)."""

    def _dialog(self, steps, expression="^ask (.+)$"):
        entry = _simple_entry(raw_pattern=expression, raw_actions=steps)
        del entry["phrases"]
        return _make_dialog(entry=entry, pattern_id=entry["id"])

    def test_preserved_capture_shaped_name_does_not_block_save(self):
        # capture_clipboard is internal-audience, so its row degrades to
        # preserved mode; the stored name stays in the row's extras.
        dialog = self._dialog([
            {"function": "capture_clipboard", "params": [], "result": "g1"},
            {"function": "insert_text", "params": ["hello"]},
        ])
        assert dialog._steps_editor._rows[0].is_preserved()
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_two_preserved_rows_with_one_name_do_not_block_save(self):
        dialog = self._dialog([
            {"function": "capture_clipboard", "params": [], "result": "note"},
            {"function": "capture_clipboard", "params": [], "result": "note"},
        ])
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_preserved_non_identifier_name_does_not_block_save(self):
        dialog = self._dialog([
            {
                "function": "capture_clipboard",
                "params": [],
                "result": "the note",
            },
            {"function": "insert_text", "params": ["hello"]},
        ])
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_editable_name_duplicating_a_preserved_name_still_flags(self):
        # This collision is fixable: the editable row's field can take
        # another name, so the runtime hazard is still reported.
        dialog = self._dialog([
            {"function": "capture_clipboard", "params": [], "result": "note"},
            {"function": "ask_ai", "params": ["g1"], "result": "note"},
        ])
        dialog._validate_advanced()
        assert "note" in dialog._steps_error_label.text()
        assert not dialog._save_btn.isEnabled()

    def test_preserved_name_round_trips_unchanged(self):
        steps = [
            {"function": "capture_clipboard", "params": [], "result": "g1"},
            {"function": "insert_text", "params": ["hello"]},
        ]
        dialog = self._dialog(steps)
        assert dialog._steps_editor.steps() == steps


class TestResultNameSubstringCorruption:
    """The engine's replacement loop substitutes a context key anywhere
    it appears inside a later step's text parameter (command_engine.py
    lines 252-260). A short result name such as 'an' therefore corrupts
    literal text like 'answer the question'. The editor must refuse such
    a name (wh-review-pattern-fixes.2)."""

    def _dialog(self, steps, expression="^ask (.+)$"):
        entry = _simple_entry(raw_pattern=expression, raw_actions=steps)
        del entry["phrases"]
        return _make_dialog(entry=entry, pattern_id=entry["id"])

    def test_name_inside_a_later_steps_text_blocks_save(self):
        dialog = self._dialog([
            {"function": "ask_ai", "params": ["g1"], "result": "an"},
            {"function": "insert_text", "params": ["answer the question"]},
        ])
        dialog._validate_advanced()
        error = dialog._steps_error_label.text()
        assert "'an'" in error
        assert "answer the question" in error
        assert "insert_text" in error
        assert not dialog._save_btn.isEnabled()

    def test_name_inside_an_earlier_steps_text_is_allowed(self):
        # The engine stores the value only after the defining step runs,
        # so earlier steps resolve before the name exists.
        dialog = self._dialog([
            {"function": "insert_text", "params": ["answer the question"]},
            {"function": "ask_ai", "params": ["g1"], "result": "an"},
        ])
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_name_inside_its_own_steps_params_is_allowed(self):
        # The defining step's own params resolve before the store.
        dialog = self._dialog([
            {"function": "ask_ai", "params": ["plan g1"], "result": "an"},
            {"function": "insert_text", "params": ["done"]},
        ])
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_exact_whole_param_reference_is_allowed(self):
        # An exact key gets a direct context lookup, never the
        # replacement loop, so the reference is the intended use.
        dialog = self._dialog([
            {"function": "ask_ai", "params": ["g1"], "result": "an"},
            {"function": "insert_text", "params": ["an"]},
        ])
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_editable_name_inside_a_preserved_rows_text_blocks_save(self):
        # The later row is read-only, but the fix is on the editable
        # row's own field: rename the result.
        dialog = self._dialog([
            {"function": "ask_ai", "params": ["g1"], "result": "an"},
            {
                "function": "add_hint_to_stt",
                "params": ["answer the question"],
            },
        ])
        dialog._validate_advanced()
        assert "'an'" in dialog._steps_error_label.text()
        assert not dialog._save_btn.isEnabled()

    def test_param_that_is_an_earlier_result_name_is_allowed(self):
        # 'answer' contains 'an', but the whole param is itself an
        # earlier step's result name: a direct context lookup at run
        # time, so the replacement loop never touches it.
        dialog = self._dialog([
            {"function": "ask_ai", "params": ["g1"], "result": "an"},
            {"function": "ask_ai", "params": ["g1"], "result": "answer"},
            {"function": "insert_text", "params": ["answer"]},
        ])
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_param_equal_to_a_non_string_function_name_blocks_save(self):
        # The engine stores context[function] only when the return value
        # is a str (command_engine.py lines 342-344). press returns a
        # UI-action dict, so no context['press'] key exists at run time;
        # the literal 'press' falls into the replacement loop and the
        # stored 's' corrupts it (wh-review-pattern-fixes.7).
        dialog = self._dialog([
            {"function": "press", "params": ["tab"]},
            {"function": "date", "params": ["%Y-%m-%d"], "result": "s"},
            {"function": "type_text", "params": ["press"]},
        ])
        dialog._validate_advanced()
        error = dialog._steps_error_label.text()
        assert "'s'" in error
        assert "press" in error
        assert "type_text" in error
        assert not dialog._save_btn.isEnabled()

    def test_param_equal_to_a_string_producing_function_name_is_allowed(self):
        # date is in RESULT_PRODUCING_ACTIONS, so the engine stores
        # context['date'] when the step runs; the later exact param
        # 'date' gets a direct lookup and never enters the replacement
        # loop, even though the result name 'at' sits inside it.
        dialog = self._dialog([
            {"function": "date", "params": ["%Y-%m-%d"], "result": "at"},
            {"function": "type_text", "params": ["date"]},
        ])
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()


class TestLoadedResultNameRoundTripsVerbatim:
    """The engine stores a step's value under the LITERAL ``result``
    string and resolves a later parameter by an exact context lookup
    (command_engine.py lines 346-347 and 171), and the save layer keeps
    a string result unchanged (PatternManager._validate_raw_actions).
    The editor stripped the loaded name, so an untouched edit/save
    silently renamed the runtime key, dropped a whitespace-only key,
    and a preserved raw ' a' wrongly pre-claimed the trimmed 'a'
    (wh-review-pattern-fixes.10)."""

    def _dialog(self, steps, expression="^ask (.+)$"):
        entry = _simple_entry(raw_pattern=expression, raw_actions=steps)
        del entry["phrases"]
        return _make_dialog(entry=entry, pattern_id=entry["id"])

    def test_loaded_name_with_whitespace_round_trips_verbatim(self):
        step = {
            "function": "date", "params": ["%Y-%m-%d"], "result": " answer",
        }
        row = _row_for("hk")
        row.set_step(step)
        assert row.step() == step

    def test_whitespace_only_loaded_name_round_trips_verbatim(self):
        # The engine treats ' ' as a real key (``if res_key:`` is true),
        # so dropping it would change what the pattern does. The UNEDITED
        # field must also still count as verbatim under the user-edit
        # tracking of wh-review-pattern-fixes.15.
        step = {"function": "date", "params": ["%Y-%m-%d"], "result": " "}
        row = _row_for("hk")
        row.set_step(step)
        assert row.result_is_loaded_verbatim()
        assert row.step() == step

    def test_preserved_raw_name_does_not_claim_the_trimmed_name(self):
        # The preserved row's literal run-time key is ' a'; the editable
        # row's 'a' is a different context key, so there is no duplicate.
        dialog = self._dialog([
            {"function": "capture_clipboard", "params": [], "result": " a"},
            {"function": "ask_ai", "params": ["g1"], "result": "a"},
        ])
        assert dialog._steps_editor._rows[0].is_preserved()
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_edited_field_takes_the_new_name_rules(self):
        # Guard: only the UNEDITED loaded value round-trips verbatim; a
        # typed replacement is stripped and validated as before.
        row = _row_for("hk")
        row.set_step(
            {"function": "ask_ai", "params": ["g1"], "result": " answer"},
        )
        row.set_result_name("  answer2  ")
        assert row.step()["result"] == "answer2"

    def test_typed_whitespace_only_name_still_drops_the_key(self):
        # Guard: a TYPED whitespace-only value still means "no result
        # key"; only a loaded one round-trips.
        row = _row_for("date")
        row.set_result_name("   ")
        assert "result" not in row.step()

    def test_unedited_loaded_non_identifier_name_does_not_block_save(self):
        # Pre-existing data: flagging the loaded ' answer' would dead-end
        # Save the same way wh-review-pattern-fixes.3 did for preserved
        # rows. It must pass validation AND reach the payload verbatim.
        steps = [
            {"function": "ask_ai", "params": ["g1"], "result": " answer"},
            {"function": "insert_text", "params": ["done"]},
        ]
        dialog = self._dialog(steps)
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()
        assert dialog.get_pattern_data()["actions"] == steps


class TestHiddenResultOnFieldlessRow:
    """A loaded step whose function is NOT in RESULT_PRODUCING_ACTIONS can
    still carry a hand-written string ``result``. Such a row is editable,
    so it is not preserved, but it has no result-name field: the name is
    invisible and unfixable, yet _with_extras still serializes it. An
    error about it would dead-end Save exactly like a preserved row's
    (wh-review-pattern-fixes.14). The name must never be the SUBJECT of
    an error while it still claims its literal key, so an editable
    sibling that duplicates it flags -- fixable in the sibling's field."""

    def _dialog(self, steps, expression="^ask (.+)$"):
        entry = _simple_entry(raw_pattern=expression, raw_actions=steps)
        del entry["phrases"]
        return _make_dialog(entry=entry, pattern_id=entry["id"])

    def test_hidden_format_invalid_result_does_not_block_save(self):
        # press has no result field, so 'bad name' is on no screen and
        # in no field; the format error would be unfixable.
        dialog = self._dialog([
            {"function": "press", "params": ["tab"], "result": "bad name"},
            {"function": "insert_text", "params": ["hello"]},
        ])
        assert not dialog._steps_editor._rows[0].is_preserved()
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_hidden_capture_shaped_result_does_not_block_save(self):
        # The capture-group rule is just as unreachable from any field.
        dialog = self._dialog([
            {"function": "press", "params": ["tab"], "result": "g1"},
            {"function": "insert_text", "params": ["hello"]},
        ])
        dialog._validate_advanced()
        assert dialog._steps_error_label.text() == ""
        assert dialog._save_btn.isEnabled()

    def test_editable_name_duplicating_a_hidden_result_still_flags(self):
        # This collision is fixable: the editable row's field can take
        # another name, so the runtime hazard is still reported.
        dialog = self._dialog([
            {"function": "press", "params": ["tab"], "result": "note"},
            {"function": "ask_ai", "params": ["g1"]},
        ])
        dialog._steps_editor._rows[1].set_result_name("note")
        dialog._validate_advanced()
        assert "note" in dialog._steps_error_label.text()
        assert not dialog._save_btn.isEnabled()

    def test_hidden_result_round_trips_unchanged(self):
        step = {"function": "press", "params": ["tab"], "result": "bad name"}
        row = _row_for("hk")
        row.set_step(step)
        assert row.result_is_hidden_extra()
        assert row.step() == step


def _user_clear(edit):
    """Empty a line edit the way a user would (fires textEdited)."""
    from PySide6.QtTest import QTest

    edit.selectAll()
    QTest.keyClick(edit, Qt.Key.Key_Backspace)


def _user_type(edit, text: str):
    """Type into a line edit the way a user would (fires textEdited)."""
    from PySide6.QtTest import QTest

    QTest.keyClicks(edit, text)


class TestRetypedResultNameGetsTypedHandling:
    """result_name() decided 'unedited loaded value' by pure text
    equality, so a user who cleared the field and retyped the exact raw
    spelling was treated as if nothing happened: a freshly typed ' '
    still serialized as the whitespace-only key, surrounding whitespace
    was kept instead of stripped, and a format-invalid spelling kept its
    verbatim exemption. A user edit must switch the field to typed
    handling permanently, even when the keystrokes reproduce the loaded
    raw string (wh-review-pattern-fixes.15)."""

    def test_cleared_and_retyped_whitespace_drops_the_key(self):
        row = _row_for("hk")
        row.set_step(
            {"function": "date", "params": ["%Y-%m-%d"], "result": " "},
        )
        _user_clear(row._result_edit)
        _user_type(row._result_edit, " ")
        assert row._result_edit.text() == " "
        assert not row.result_is_loaded_verbatim()
        assert "result" not in row.step()

    def test_cleared_and_retyped_name_is_stripped(self):
        row = _row_for("hk")
        row.set_step(
            {"function": "date", "params": ["%Y-%m-%d"], "result": " ok "},
        )
        _user_clear(row._result_edit)
        _user_type(row._result_edit, " ok ")
        assert row._result_edit.text() == " ok "
        assert not row.result_is_loaded_verbatim()
        assert row.step()["result"] == "ok"

    def test_prefill_restore_does_not_count_as_a_user_edit(self):
        # The switch-back prefill writes the field with setText, which
        # never fires textEdited, so the loaded raw name still
        # round-trips verbatim after a function switch away and back
        # (wh-review-pattern-fixes.4).
        row = _row_for("hk")
        row.set_step(
            {"function": "ask_ai", "params": ["g1"], "result": " answer"},
        )
        _switch_function(row, "insert_text")
        _switch_function(row, "ask_ai")
        assert row.result_is_loaded_verbatim()
        assert row.step()["result"] == " answer"
