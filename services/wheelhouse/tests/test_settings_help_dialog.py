"""The Settings window help window (wh-settings-window-help, app side).

Covers acceptance items 1, 2, 6, 7 and the app-side tests of item 8. Tests
that name "the real file" read the shipped knowledge/settings_window_help.json.
No test opens a real modal message box.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QGroupBox, QLabel, QPushButton

pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")

_DIALOGS = []


@pytest.fixture(autouse=True)
def _delete_windows_afterwards():
    """Delete every window a test made (same reason as
    test_settings_window_dialog.py)."""
    try:
        yield
    finally:
        import shiboken6

        while _DIALOGS:
            dialog = _DIALOGS.pop()
            if shiboken6.isValid(dialog):
                shiboken6.delete(dialog)


def _make_settings():
    from settings_window_dialog import SettingsWindowDialog

    dialog = SettingsWindowDialog(parent=None)
    _DIALOGS.append(dialog)
    return dialog


def _help_button(dialog):
    found = [
        b for b in dialog.findChildren(QPushButton)
        if b.accessibleName() == "Help"
    ]
    assert len(found) == 1
    return found[0]


def _help_windows(dialog):
    from settings_help_dialog import SettingsHelpDialog

    return dialog.findChildren(SettingsHelpDialog)


def _write(tmp_path, content):
    path = tmp_path / "help.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
#  Item 1: the Help button
# --------------------------------------------------------------------------- #

def test_help_button_in_bottom_row_and_tab_order():
    dialog = _make_settings()
    button = _help_button(dialog)
    assert button.text() == "Help"
    order = dialog.tab_order_widgets()
    assert button in order
    assert order.index(button) == order.index(dialog._wake_box) + 1
    assert order.index(dialog._restart_btn) == order.index(button) + 1
    assert button.autoDefault() is False and button.isDefault() is False
    # The bottom row holds the Help, Restart WheelHouse and Close buttons in
    # one row widget; a Help button placed in another row fails here.
    assert button.parentWidget() is dialog._restart_btn.parentWidget()
    assert button.parentWidget() is dialog._close_btn.parentWidget()


def test_help_button_opens_visible_help_window():
    dialog = _make_settings()
    dialog.show()
    _help_button(dialog).click()
    windows = _help_windows(dialog)
    assert len(windows) == 1
    assert windows[0].isVisible()


def test_second_click_does_not_open_a_second_window():
    dialog = _make_settings()
    dialog.show()
    _help_button(dialog).click()
    _help_button(dialog).click()
    assert len(_help_windows(dialog)) == 1


# --------------------------------------------------------------------------- #
#  Items 2 and 8: labels and order, with the real file
# --------------------------------------------------------------------------- #

def test_setting_labels_has_13_entries_each_visible_in_window():
    dialog = _make_settings()
    labels = dialog.setting_labels()
    assert len(labels) == 13
    visible = set()
    for box in dialog.findChildren(QCheckBox):
        visible.add(box.text())
    for group in dialog.findChildren(QGroupBox):
        visible.add(group.title())
    for label_widget in dialog.findChildren(QLabel):
        visible.add(label_widget.text())
    for label in labels:
        assert label in visible
    assert "Menu settings" not in labels
    assert "Other settings" not in labels


def test_every_setting_control_in_window_has_a_recorded_label():
    """A setting added to the window without a label in setting_labels fails.

    One setting is one check box, one Decrease and Increase pair, or one
    group of radio buttons. The labels are recorded by hand in _build_ui, so
    this count is what notices a new control that skips the recording.
    """
    from PySide6.QtWidgets import QButtonGroup

    dialog = _make_settings()
    boxes = dialog.findChildren(QCheckBox)
    decrease_buttons = [
        b for b in dialog.findChildren(QPushButton) if b.text() == "Decrease"
    ]
    radio_groups = dialog.findChildren(QButtonGroup)
    labels = dialog.setting_labels()
    assert len(labels) == (
        len(boxes) + len(decrease_buttons) + len(radio_groups)
    )
    for box in boxes:
        assert box.text() in labels


def test_help_window_labels_equal_settings_window_order():
    from settings_help_dialog import SettingsHelpDialog, load_settings_help

    dialog = _make_settings()
    help_window = SettingsHelpDialog(load_settings_help())
    _DIALOGS.append(help_window)
    assert help_window.entry_labels() == dialog.setting_labels()


def test_order_check_fails_when_order_differs():
    """The order check above can fail: a reversed file gives another order."""
    from settings_help_dialog import SettingsHelpDialog, load_settings_help

    dialog = _make_settings()
    help_window = SettingsHelpDialog(list(reversed(load_settings_help())))
    _DIALOGS.append(help_window)
    assert help_window.entry_labels() != dialog.setting_labels()


def test_every_setting_has_window_text_in_real_file():
    from settings_help_dialog import load_settings_help

    dialog = _make_settings()
    texts = dict(load_settings_help())
    for label in dialog.setting_labels():
        assert texts.get(label, "").strip(), f"no window text for {label}"


def test_missing_text_check_can_fail():
    """The check above can fail: drop one entry and it names that setting."""
    from settings_help_dialog import load_settings_help

    dialog = _make_settings()
    texts = dict(load_settings_help())
    del texts["Wake word"]
    missing = [
        label for label in dialog.setting_labels()
        if not texts.get(label, "").strip()
    ]
    assert missing == ["Wake word"]


def test_html_escapes_and_entry_starts_with_label():
    from settings_help_dialog import build_settings_help_html

    html_text = build_settings_help_html(
        [("A <i>", "text one <u>"), ("B & C", "text two & more")]
    )
    assert "<i>" not in html_text and "<u>" not in html_text
    assert "A &lt;i&gt;" in html_text and "B &amp; C" in html_text
    assert "text one &lt;u&gt;" in html_text
    assert "text two &amp; more" in html_text
    assert (
        html_text.index("A &lt;i&gt;") < html_text.index("text one")
        < html_text.index("B &amp; C") < html_text.index("text two")
    )


def test_rendered_entries_start_with_label():
    from settings_help_dialog import SettingsHelpDialog, load_settings_help

    pairs = load_settings_help()
    help_window = SettingsHelpDialog(pairs)
    _DIALOGS.append(help_window)
    plain = help_window._browser.toPlainText()
    position = 0
    for label, text in pairs:
        at = plain.index(label, position)
        # The label comes first; its text follows it before the next label.
        assert plain.index(text[:30], at) > at
        position = plain.index(text[:30], at)


# --------------------------------------------------------------------------- #
#  Item 6: load failures
# --------------------------------------------------------------------------- #

def test_load_missing_file_raises_naming_problem_and_path(tmp_path):
    from settings_help_dialog import SettingsHelpError, load_settings_help

    path = tmp_path / "nope.json"
    with pytest.raises(SettingsHelpError) as info:
        load_settings_help(path)
    assert str(path) in str(info.value)
    # The test directory name holds the word "missing", so the word alone
    # cannot tell this branch from the unreadable-file branch.
    assert "help file is missing: " in str(info.value)


def test_load_unreadable_path_raises(tmp_path):
    from settings_help_dialog import SettingsHelpError, load_settings_help

    with pytest.raises(SettingsHelpError) as info:
        load_settings_help(tmp_path)  # a directory: the read fails
    assert str(tmp_path) in str(info.value)


def test_load_invalid_json_raises(tmp_path):
    from settings_help_dialog import SettingsHelpError, load_settings_help

    path = _write(tmp_path, "{not json")
    with pytest.raises(SettingsHelpError) as info:
        load_settings_help(path)
    assert str(path) in str(info.value)
    assert "json" in str(info.value).lower()


def test_load_non_utf8_raises(tmp_path):
    from settings_help_dialog import SettingsHelpError, load_settings_help

    path = _write(tmp_path, b"\xff\xfe\x00bad")
    with pytest.raises(SettingsHelpError) as info:
        load_settings_help(path)
    assert str(path) in str(info.value)


@pytest.mark.parametrize("content", [
    json.dumps([]),
    json.dumps({"settings": "x"}),
    json.dumps({"settings": []}),
    json.dumps({"settings": [{"label": "A"}]}),
    json.dumps({"settings": [{"label": "", "text": "t"}]}),
    json.dumps({"settings": [{"label": "A", "text": 5}]}),
    json.dumps({"settings": [{"label": "A", "text": "   "}]}),
    json.dumps({"settings": ["A"]}),
])
def test_load_wrong_shape_raises(tmp_path, content):
    from settings_help_dialog import SettingsHelpError, load_settings_help

    path = _write(tmp_path, content)
    with pytest.raises(SettingsHelpError) as info:
        load_settings_help(path)
    assert str(path) in str(info.value)
    assert "settings" in str(info.value).lower()


def test_load_good_file_returns_pairs_in_order(tmp_path):
    from settings_help_dialog import load_settings_help

    path = _write(tmp_path, json.dumps({"settings": [
        {"label": "B", "text": "tb"}, {"label": "A", "text": "ta"}]}))
    assert load_settings_help(path) == [("B", "tb"), ("A", "ta")]


def test_help_click_with_missing_file_warns_and_window_keeps_working(tmp_path):
    import settings_help_dialog

    dialog = _make_settings()
    dialog.show()
    seen = []
    missing = tmp_path / "gone.json"
    with patch.object(settings_help_dialog, "SETTINGS_HELP_PATH", missing), \
            patch.object(dialog, "_show_help_error", side_effect=seen.append):
        _help_button(dialog).click()
    assert len(seen) == 1
    assert str(missing) in seen[0] and "help file is missing: " in seen[0]
    assert _help_windows(dialog) == []

    # The Settings window still sends its signals.
    dialog.apply_live_state({"ready": True, "speech_enabled": False})
    got = []
    dialog.speech_toggle_requested.connect(lambda: got.append(1))
    dialog._speech_box.click()
    assert got == [1]


# --------------------------------------------------------------------------- #
#  Item 7: closing
# --------------------------------------------------------------------------- #

def test_close_button_closes_help_window():
    dialog = _make_settings()
    dialog.show()
    _help_button(dialog).click()
    window = _help_windows(dialog)[0]
    close = [b for b in window.findChildren(QPushButton)
             if b.accessibleName() == "Close"]
    assert len(close) == 1 and close[0].text() == "Close"
    close[0].click()
    assert not window.isVisible()


def test_escape_closes_help_window():
    dialog = _make_settings()
    dialog.show()
    _help_button(dialog).click()
    window = _help_windows(dialog)[0]
    assert window.isVisible()
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert not window.isVisible()
