"""The settings window (wh-voice-settings-window, stage B).

Acceptance items covered here: 2 (which settings the window holds), 3 (what
saves), 4 (the restart text and the Restart WheelHouse button) and 5 (every
control is a check box, radio button or push button, so each change is one
"click N").

Every test uses temporary state only: no real config.toml is read or written.
The save path is the Logic process's set_config_value request, so these tests
read the GUI-to-Logic queue; a test that feeds a reply puts it on the
Logic-to-GUI queue and lets the GUI poll read it, as the running program does.
"""
from __future__ import annotations

import threading
from queue import Empty, Queue
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QTextEdit,
    QWidget,
)

pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")

RESTART_TEXT = "takes effect after restart"
WARNING_TEXT = (
    "If the wake word is off and you turn speech off, only the keyboard or "
    "mouse can turn speech on again."
)

CONFIG_KEYS = (
    "CHECK_FOR_UPDATES",
    "LOG_TRANSCRIPTS",
    "REPLACEMENT_TIMEOUT_MS",
    "COMMAND_TIMEOUT_MS",
    "ENABLE_AUDIO_SUPPRESSION",
    "wake_word.enabled",
)
RESTART_KEYS = {
    "LOG_TRANSCRIPTS",
    "REPLACEMENT_TIMEOUT_MS",
    "COMMAND_TIMEOUT_MS",
    "ENABLE_AUDIO_SUPPRESSION",
    "wake_word.enabled",
}

STORED = {
    "CHECK_FOR_UPDATES": True,
    "LOG_TRANSCRIPTS": False,
    "REPLACEMENT_TIMEOUT_MS": 700,
    "COMMAND_TIMEOUT_MS": 700,
    "ENABLE_AUDIO_SUPPRESSION": "auto",
    "wake_word.enabled": False,
}


def _live(**changes):
    state = {
        "ready": True,
        "speech_enabled": True,
        "button_visible": True,
        "interim_results": True,
        "push_to_talk": False,
        "debug": False,
        "stt_providers": [("google_stt", "Google STT"), ("parakeet_tdt", "Parakeet")],
        "stt_current": "google_stt",
        "stt_not_installed": [],
        "ai_providers": [("gpt-oss", "GPT OSS", True), ("qwen", "Qwen", True)],
        "ai_current": "gpt-oss",
    }
    state.update(changes)
    return state


# --------------------------------------------------------------------------- #
#  Construction helpers
# --------------------------------------------------------------------------- #

_DIALOGS = []
_MANAGERS = []


@pytest.fixture(autouse=True)
def _delete_windows_afterwards():
    """A parentless dialog outlives its manager; delete both before the next
    test builds a GuiManager (heap corruption otherwise, measured in
    test_pattern_manager_font_size_persistence.py)."""
    try:
        yield
    finally:
        import shiboken6

        while _MANAGERS:
            manager = _MANAGERS.pop()
            dialog = getattr(manager, "_sw_dialog", None)
            if dialog is not None and shiboken6.isValid(dialog):
                shiboken6.delete(dialog)
            manager._sw_dialog = None
        while _DIALOGS:
            dialog = _DIALOGS.pop()
            if shiboken6.isValid(dialog):
                shiboken6.delete(dialog)


def _make_dialog(loaded=True, live=None, stored=None):
    from settings_window_dialog import SettingsWindowDialog

    dialog = SettingsWindowDialog(parent=None)
    _DIALOGS.append(dialog)
    dialog.apply_live_state(live or _live())
    if loaded:
        dialog.apply_config_values(dict(STORED if stored is None else stored))
    return dialog


def _record(dialog):
    """Connect every outgoing signal of the dialog to a list of (name, args)."""
    seen = []
    for name in (
        "speech_toggle_requested",
        "button_visibility_toggle_requested",
        "interim_toggle_requested",
        "ptt_toggle_requested",
        "stt_provider_requested",
        "ai_provider_requested",
        "debug_toggle_requested",
        "setting_changed",
        "restart_requested",
    ):
        getattr(dialog, name).connect(
            lambda *args, _name=name: seen.append((_name, args))
        )
    return seen


def _named(dialog, name, kind=QAbstractButton):
    found = [
        w for w in dialog.findChildren(kind) if w.accessibleName() == name
    ]
    assert len(found) == 1, f"{name!r}: expected one control, got {len(found)}"
    return found[0]


def _label_text(dialog, accessible_name):
    return _named(dialog, accessible_name, QLabel).text()


@pytest.fixture
def quiet_gui():
    """No native settings notice, no desktop notice, no shown windows.

    A subclass placed at the import site replaces only the three calls that
    would put a real window on the desktop; patching those methods on the
    PySide6 class itself crashes the interpreter (measured on the Pattern
    Manager dialog).
    """
    from settings_window_dialog import SettingsWindowDialog

    class _QuietDialog(SettingsWindowDialog):
        def show(self):
            pass

        def raise_(self):
            pass

        def activateWindow(self):
            pass

    with patch("gui.send_notice"), \
         patch("soft_allow_write_failed_toast.SoftAllowWriteFailedToast"), \
         patch("terminal_editor_window._steal_foreground"), \
         patch("settings_window_dialog.SettingsWindowDialog", _QuietDialog):
        yield


def _make_manager(config=None):
    with patch("gui.FloatingButton") as mock_button, \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        mock_button.return_value = MagicMock()
        from gui import GuiManager

        manager = GuiManager(threading.Event(), Queue(), Queue(), config=config)
    manager.initial_state_received = True
    manager.button._gesture_running = False
    manager.button._is_dragging = False
    manager.button._is_resizing = False
    manager.stt_providers_available = ["google_stt", "parakeet_tdt"]
    manager.stt_provider = "google_stt"
    manager.stt_provider_display_names = {"google_stt": "Google STT",
                                          "parakeet_tdt": "Parakeet"}
    manager.ai_providers_available = ["gpt-oss", "qwen"]
    manager.ai_provider = "gpt-oss"
    manager.ai_provider_display_names = {"gpt-oss": "GPT OSS", "qwen": "Qwen"}
    _MANAGERS.append(manager)
    return manager


def _drain(queue):
    items = []
    while True:
        try:
            items.append(queue.get_nowait())
        except Empty:
            return items


def _open(manager, stored=None):
    """Open the window the way the GUI does, and feed the Logic reply."""
    manager._open_settings_window()
    sent = _drain(manager.commands_to_logic_queue)
    reads = [c for c in sent if c.get("action") == "get_config_values"]
    assert len(reads) == 1, sent
    manager.state_from_logic_queue.put_nowait({
        "action": "config_values_result", "request_id": reads[0]["request_id"],
        "values": dict(STORED if stored is None else stored),
    })
    manager._check_queues_and_events()
    return manager._sw_dialog, reads[0]


# --------------------------------------------------------------------------- #
#  (g) The timeout step arithmetic (boss ruling R3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("current, direction, expected", [
    (700, +1, 800),       # in range, a multiple of 100
    (700, -1, 600),
    (200, +1, 300),
    (3000, -1, 2900),
    (200, -1, None),      # at the lower limit: nothing happens
    (3000, +1, None),     # at the upper limit: nothing happens
    (150, +1, 200),       # below the range: either press goes to 200
    (150, -1, 200),
    (0, +1, 200),
    (0, -1, 200),
    (-40, -1, 200),
    (3500, -1, 3000),     # above the range: either press goes to 3000
    (3500, +1, 3000),
    (3050, -1, 3000),
    (750, +1, 800),       # not a multiple of 100: the next multiple in the
    (750, -1, 700),       # button's direction
    (2950, +1, 3000),
    (250, -1, 200),
    (210, +1, 300),
    (699.5, +1, 700),
    (699.5, -1, 600),
])
def test_next_timeout_value_arithmetic(current, direction, expected):
    from settings_window_dialog import next_timeout_value

    assert next_timeout_value(current, direction) == expected


@pytest.mark.parametrize("bad", [None, True, False, "700", [], {}])
def test_next_timeout_value_ignores_a_value_that_is_not_a_number(bad):
    from settings_window_dialog import next_timeout_value

    assert next_timeout_value(bad, +1) is None
    assert next_timeout_value(bad, -1) is None


def test_a_timeout_shows_the_value_in_use_and_writes_nothing_before_a_press():
    dialog = _make_dialog(stored={**STORED, "REPLACEMENT_TIMEOUT_MS": 9000,
                                  "COMMAND_TIMEOUT_MS": 750})
    seen = _record(dialog)
    assert _label_text(dialog, "Replacement timeout value") == "9000 ms"
    assert _label_text(dialog, "Command timeout value") == "750 ms"
    # Refreshing from the Logic process must not write either.
    dialog.apply_config_values({"REPLACEMENT_TIMEOUT_MS": 9000})
    dialog.apply_live_state(_live())
    assert seen == []


def test_the_first_press_moves_an_out_of_range_value_into_the_range():
    dialog = _make_dialog(stored={**STORED, "REPLACEMENT_TIMEOUT_MS": 9000,
                                  "COMMAND_TIMEOUT_MS": 750})
    seen = _record(dialog)
    _named(dialog, "Decrease replacement timeout").click()
    _named(dialog, "Increase command timeout").click()
    assert seen == [
        ("setting_changed", ("REPLACEMENT_TIMEOUT_MS", 3000)),
        ("setting_changed", ("COMMAND_TIMEOUT_MS", 800)),
    ]
    assert _label_text(dialog, "Replacement timeout value") == "3000 ms"
    assert _label_text(dialog, "Command timeout value") == "800 ms"


def test_a_press_at_a_limit_writes_nothing():
    dialog = _make_dialog(stored={**STORED, "REPLACEMENT_TIMEOUT_MS": 200,
                                  "COMMAND_TIMEOUT_MS": 3000})
    seen = _record(dialog)
    _named(dialog, "Decrease replacement timeout").click()
    _named(dialog, "Increase command timeout").click()
    assert seen == []
    assert _label_text(dialog, "Replacement timeout value") == "200 ms"
    assert _label_text(dialog, "Command timeout value") == "3000 ms"


def test_a_timeout_step_inside_the_range_moves_by_100():
    # 1000 and 1200 are multiples of 200 as well as of 100, so a step of 200
    # lands on 1200 and 1000 instead of 1100 and 1100. (The stored 700 cannot
    # tell the two steps apart: 700 is not a multiple of 200, and the next
    # multiple of 200 in either direction is 800 and 600.)
    dialog = _make_dialog(stored={**STORED, "REPLACEMENT_TIMEOUT_MS": 1000,
                                  "COMMAND_TIMEOUT_MS": 1200})
    seen = _record(dialog)
    _named(dialog, "Increase replacement timeout").click()
    _named(dialog, "Decrease command timeout").click()
    assert seen == [
        ("setting_changed", ("REPLACEMENT_TIMEOUT_MS", 1100)),
        ("setting_changed", ("COMMAND_TIMEOUT_MS", 1100)),
    ]


# --------------------------------------------------------------------------- #
#  (e) Settings 8 to 13 send their key and value
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name, key, start, expected", [
    ("Check for updates", "CHECK_FOR_UPDATES", True, False),
    ("Log transcripts", "LOG_TRANSCRIPTS", False, True),
    ("Wake word", "wake_word.enabled", False, True),
])
def test_a_check_box_setting_emits_its_key_and_the_new_value(
    name, key, start, expected
):
    dialog = _make_dialog()
    seen = _record(dialog)
    box = _named(dialog, name, QCheckBox)
    assert box.isChecked() is start
    box.click()
    assert seen == [("setting_changed", (key, expected))]
    assert box.isChecked() is expected


def test_audio_suppression_radios_write_auto_true_and_false():
    dialog = _make_dialog()
    seen = _record(dialog)
    _named(dialog, "Audio suppression: on", QRadioButton).click()
    _named(dialog, "Audio suppression: off", QRadioButton).click()
    _named(dialog, "Audio suppression: auto", QRadioButton).click()
    assert seen == [
        ("setting_changed", ("ENABLE_AUDIO_SUPPRESSION", True)),
        ("setting_changed", ("ENABLE_AUDIO_SUPPRESSION", False)),
        ("setting_changed", ("ENABLE_AUDIO_SUPPRESSION", "auto")),
    ]
    # Booleans must stay booleans: True is not the string "true".
    assert seen[0][1][1] is True
    assert seen[1][1][1] is False


def test_clicking_the_selected_audio_choice_writes_nothing():
    dialog = _make_dialog()
    seen = _record(dialog)
    _named(dialog, "Audio suppression: auto", QRadioButton).click()
    assert seen == []


@pytest.mark.parametrize("stored, selected", [
    ("auto", "auto"),
    (True, "on"),
    (False, "off"),
    ("TRUE", "on"),
    (" False ", "off"),
    ("Auto", "auto"),
    (None, "auto"),          # the key is missing: the code default
    ("maybe", "auto"),       # unreadable: the program uses auto
    (7, "auto"),
])
def test_audio_suppression_shows_what_the_program_would_use(stored, selected):
    values = dict(STORED)
    values["ENABLE_AUDIO_SUPPRESSION"] = stored
    dialog = _make_dialog(stored=values)
    checked = [
        choice for choice in ("auto", "on", "off")
        if _named(dialog, f"Audio suppression: {choice}",
                  QRadioButton).isChecked()
    ]
    assert checked == [selected]


def test_the_window_uses_the_parser_of_the_audio_suppression_module():
    from settings_window_dialog import parse_audio_suppression_setting
    from services.wheelhouse.utils import audio_suppression_decision as module

    assert parse_audio_suppression_setting is module.parse_audio_suppression_setting


# --------------------------------------------------------------------------- #
#  (i) Effective values from a config_values_result
# --------------------------------------------------------------------------- #


def test_effective_values_come_from_the_config_values_result():
    dialog = _make_dialog(loaded=False)
    dialog.apply_config_values({
        "CHECK_FOR_UPDATES": False,
        "LOG_TRANSCRIPTS": True,
        "REPLACEMENT_TIMEOUT_MS": 1200,
        "COMMAND_TIMEOUT_MS": 400,
        "ENABLE_AUDIO_SUPPRESSION": False,
        "wake_word.enabled": True,
    })
    assert _named(dialog, "Check for updates", QCheckBox).isChecked() is False
    assert _named(dialog, "Log transcripts", QCheckBox).isChecked() is True
    assert _named(dialog, "Wake word", QCheckBox).isChecked() is True
    assert _label_text(dialog, "Replacement timeout value") == "1200 ms"
    assert _label_text(dialog, "Command timeout value") == "400 ms"
    assert _named(dialog, "Audio suppression: off", QRadioButton).isChecked()


def test_a_missing_wake_word_key_shows_the_code_default():
    # service_manager.py reads config_service.get("wake_word.enabled", False).
    values = dict(STORED)
    values["wake_word.enabled"] = None   # get_persisted answers None for absent
    dialog = _make_dialog(stored=values)
    assert _named(dialog, "Wake word", QCheckBox).isChecked() is False


def test_missing_update_and_transcript_keys_show_their_code_defaults():
    # update_check.py: a missing CHECK_FOR_UPDATES behaves as on.
    # launcher.py: a missing LOG_TRANSCRIPTS means off.
    values = dict(STORED)
    values["CHECK_FOR_UPDATES"] = None
    values["LOG_TRANSCRIPTS"] = None
    dialog = _make_dialog(stored=values)
    assert _named(dialog, "Check for updates", QCheckBox).isChecked() is True
    assert _named(dialog, "Log transcripts", QCheckBox).isChecked() is False


def test_a_missing_timeout_shows_not_set_and_cannot_be_stepped():
    values = dict(STORED)
    values["REPLACEMENT_TIMEOUT_MS"] = None
    dialog = _make_dialog(stored=values)
    seen = _record(dialog)
    assert _label_text(dialog, "Replacement timeout value") == "not set"
    _named(dialog, "Increase replacement timeout").click()
    _named(dialog, "Decrease replacement timeout").click()
    assert seen == []


def test_the_saved_settings_stay_disabled_until_the_values_arrive():
    dialog = _make_dialog(loaded=False)
    for name in ("Check for updates", "Log transcripts", "Wake word"):
        assert not _named(dialog, name, QCheckBox).isEnabled()
    assert not _named(dialog, "Increase command timeout").isEnabled()
    assert not _named(dialog, "Audio suppression: on").isEnabled()
    dialog.apply_config_values(dict(STORED))
    for name in ("Check for updates", "Log transcripts", "Wake word"):
        assert _named(dialog, name, QCheckBox).isEnabled()
    assert _named(dialog, "Increase command timeout").isEnabled()
    assert _named(dialog, "Audio suppression: on").isEnabled()


def test_save_result_text_is_saved_or_not_saved():
    dialog = _make_dialog()
    dialog.show_save_result(True)
    assert _label_text(dialog, "Save status") == "Saved"
    dialog.show_save_result(False)
    assert _label_text(dialog, "Save status") == "Not saved"


# --------------------------------------------------------------------------- #
#  Live state of settings 1 to 7 reaches the controls without a command
# --------------------------------------------------------------------------- #


def test_live_state_sets_the_controls_and_emits_nothing():
    dialog = _make_dialog(live=_live(
        speech_enabled=False, button_visible=False, interim_results=False,
        push_to_talk=True, debug=True, stt_current="parakeet_tdt",
        ai_current="qwen",
    ))
    seen = _record(dialog)
    dialog.apply_live_state(_live(
        speech_enabled=False, button_visible=False, interim_results=False,
        push_to_talk=True, debug=True, stt_current="parakeet_tdt",
        ai_current="qwen",
    ))
    assert seen == []
    assert not _named(dialog, "Speech Enabled", QCheckBox).isChecked()
    assert not _named(dialog, "Show Floating Button", QCheckBox).isChecked()
    assert not _named(dialog, "Interim Results", QCheckBox).isChecked()
    assert _named(dialog, "Push-to-Talk Mode", QCheckBox).isChecked()
    assert _named(dialog, "Debug", QCheckBox).isChecked()
    assert _named(dialog, "STT Provider: Parakeet", QRadioButton).isChecked()
    assert not _named(dialog, "STT Provider: Google STT",
                      QRadioButton).isChecked()
    assert _named(dialog, "AI Model: Qwen", QRadioButton).isChecked()


def test_a_later_live_state_moves_the_check_without_a_command():
    dialog = _make_dialog()
    seen = _record(dialog)
    dialog.apply_live_state(_live(speech_enabled=False))
    dialog.apply_live_state(_live(speech_enabled=True))
    dialog.apply_live_state(_live(debug=True))
    assert seen == []
    assert _named(dialog, "Speech Enabled", QCheckBox).isChecked()
    assert _named(dialog, "Debug", QCheckBox).isChecked()


def test_provider_lists_follow_the_live_state_and_not_installed_are_buttons():
    dialog = _make_dialog(live=_live(
        stt_not_installed=[("parakeet_v3", "Parakeet v3 (needs its model)")],
        ai_providers=[("__ai_disabled__", "AI disabled", False)],
        ai_current=None,
    ))
    seen = _record(dialog)
    radios = [r.accessibleName() for r in dialog.findChildren(QRadioButton)
              if r.accessibleName().startswith(("STT", "AI"))]
    assert radios == ["STT Provider: Google STT", "STT Provider: Parakeet",
                      "AI Model: AI disabled"]
    assert not _named(dialog, "AI Model: AI disabled", QRadioButton).isEnabled()
    _named(dialog, "STT Provider: Parakeet v3 (needs its model)",
           QPushButton).click()
    assert seen == [("stt_provider_requested", ("parakeet_v3",))]


def test_controls_for_live_settings_are_disabled_until_the_gui_is_ready():
    dialog = _make_dialog(live=_live(ready=False))
    assert not _named(dialog, "Speech Enabled", QCheckBox).isEnabled()
    assert not _named(dialog, "STT Provider: Google STT",
                      QRadioButton).isEnabled()
    dialog.apply_live_state(_live(ready=True))
    assert _named(dialog, "Speech Enabled", QCheckBox).isEnabled()


# --------------------------------------------------------------------------- #
#  (f) The restart text and the Restart WheelHouse button
# --------------------------------------------------------------------------- #


def test_restart_text_sits_beside_exactly_settings_9_to_13():
    dialog = _make_dialog()
    notes = [w for w in dialog.findChildren(QLabel) if w.text() == RESTART_TEXT]
    assert len(notes) == 5, [n.text() for n in dialog.findChildren(QLabel)]

    beside = {
        "LOG_TRANSCRIPTS": _named(dialog, "Log transcripts", QCheckBox),
        "REPLACEMENT_TIMEOUT_MS": _named(dialog, "Increase replacement timeout"),
        "COMMAND_TIMEOUT_MS": _named(dialog, "Increase command timeout"),
        "ENABLE_AUDIO_SUPPRESSION": _named(dialog, "Audio suppression: on"),
        "wake_word.enabled": _named(dialog, "Wake word", QCheckBox),
    }
    assert set(beside) == RESTART_KEYS
    for key, control in beside.items():
        row = control.parentWidget()
        siblings = [n for n in notes if n.parentWidget() is row]
        assert len(siblings) == 1, f"{key}: no restart text in its row"

    # No other control shares a row with the restart text.
    note_rows = {n.parentWidget() for n in notes}
    elsewhere = [
        w.accessibleName() for w in dialog.findChildren(QAbstractButton)
        if w.parentWidget() in note_rows
        and w not in beside.values()
        and not w.accessibleName().startswith(
            ("Decrease", "Audio suppression"))
    ]
    assert elsewhere == []

    for name in ("Check for updates", "Speech Enabled", "Show Floating Button",
                 "Interim Results", "Push-to-Talk Mode", "Debug"):
        row = _named(dialog, name, QCheckBox).parentWidget()
        assert all(n.parentWidget() is not row for n in notes), name


def test_the_restart_button_emits_restart_requested():
    dialog = _make_dialog()
    seen = _record(dialog)
    _named(dialog, "Restart WheelHouse", QPushButton).click()
    assert seen == [("restart_requested", ())]


def test_the_wake_word_and_speech_warning_appears_beside_those_two_only():
    dialog = _make_dialog()
    warnings = [w for w in dialog.findChildren(QLabel)
                if w.text() == WARNING_TEXT]
    assert len(warnings) == 2
    rows = {w.parentWidget() for w in warnings}
    assert _named(dialog, "Wake word", QCheckBox).parentWidget() in rows
    assert _named(dialog, "Speech Enabled", QCheckBox).parentWidget() in rows
    # A static note: it has no button of its own and asks nothing.
    assert not any(
        isinstance(c, QAbstractButton) and c.text() == WARNING_TEXT
        for c in dialog.findChildren(QAbstractButton)
    )


# --------------------------------------------------------------------------- #
#  (h) Every control is a check box, radio button or push button
# --------------------------------------------------------------------------- #


def test_only_buttons_check_boxes_and_radio_buttons_take_input():
    dialog = _make_dialog(live=_live(
        stt_not_installed=[("parakeet_v3", "Parakeet v3")]))
    for kind in (QComboBox, QAbstractSpinBox, QLineEdit, QTextEdit, QSlider):
        assert dialog.findChildren(kind) == [], kind.__name__
    buttons = dialog.findChildren(QAbstractButton)
    assert buttons
    for button in buttons:
        assert isinstance(button, (QCheckBox, QRadioButton, QPushButton))
        assert button.accessibleName().strip(), button.text()


def test_the_tab_order_visits_every_control_in_the_listed_order():
    dialog = _make_dialog(live=_live(
        stt_not_installed=[("parakeet_v3", "Parakeet v3")]))
    listed = dialog.tab_order_widgets()
    buttons = dialog.findChildren(QAbstractButton)
    assert set(buttons) <= set(listed)
    assert len(listed) == len(set(listed))
    # Walk the focus chain from the first listed widget.
    chain = [listed[0]]
    widget = listed[0].nextInFocusChain()
    while widget is not listed[0] and widget is not None:
        if isinstance(widget, QAbstractButton):
            chain.append(widget)
        widget = widget.nextInFocusChain()
    assert [w for w in chain if w in buttons] == [
        w for w in listed if w in buttons]


def test_the_tab_order_follows_a_rebuilt_provider_list():
    dialog = _make_dialog()
    dialog.apply_live_state(_live(
        stt_providers=[("only", "Only one")], stt_current="only"))
    listed = dialog.tab_order_widgets()
    names = [w.accessibleName() for w in listed]
    assert "STT Provider: Only one" in names
    assert "STT Provider: Google STT" not in names
    assert set(dialog.findChildren(QAbstractButton)) <= set(listed)


def test_the_window_is_hidden_not_destroyed_on_close():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QCloseEvent

    dialog = _make_dialog()
    assert not dialog.testAttribute(Qt.WidgetAttribute.WA_QuitOnClose)
    event = QCloseEvent()
    dialog.closeEvent(event)
    assert event.isAccepted() is False
    assert dialog.isHidden()


# --------------------------------------------------------------------------- #
#  (b) The GUI opens the window once and reuses it
# --------------------------------------------------------------------------- #


def test_the_gui_dispatch_opens_the_window_and_reuses_it(quiet_gui):
    manager = _make_manager()
    manager.state_from_logic_queue.put_nowait({"action": "open_settings_window"})
    manager._check_queues_and_events()
    first = manager._sw_dialog
    assert first is not None
    manager.state_from_logic_queue.put_nowait({"action": "open_settings_window"})
    manager._check_queues_and_events()
    assert manager._sw_dialog is first


def test_opening_asks_logic_for_the_six_saved_settings(quiet_gui):
    manager = _make_manager()
    manager._open_settings_window()
    sent = _drain(manager.commands_to_logic_queue)
    reads = [c for c in sent if c.get("action") == "get_config_values"]
    assert len(reads) == 1
    assert reads[0]["keys"] == list(CONFIG_KEYS)
    assert reads[0]["request_id"]
    assert not [c for c in sent if c.get("action", "").startswith("set_config")]


def test_opening_again_asks_again_but_builds_nothing_new(quiet_gui):
    manager = _make_manager()
    manager._open_settings_window()
    first = manager._sw_dialog
    _drain(manager.commands_to_logic_queue)
    manager._open_settings_window()
    assert manager._sw_dialog is first
    sent = _drain(manager.commands_to_logic_queue)
    assert [c["action"] for c in sent] == ["get_config_values"]


def _reopen_with_state(manager, state, steal=None):
    """Open again on a stubbed window whose windowState() is ``state``.

    No window is shown: the state and activation calls are stubbed and
    recorded (a real minimized window raised a COM error in the next
    pytest-qt event pass), and _steal_foreground is replaced.
    """
    from PySide6.QtCore import Qt  # noqa: F401

    manager._open_settings_window()
    dialog = manager._sw_dialog
    calls = []
    current = {"state": state}
    dialog.windowState = lambda: current["state"]

    def _set_state(new_state):
        calls.append(("setWindowState", new_state))
        current["state"] = new_state

    dialog.setWindowState = _set_state
    dialog.show = lambda: calls.append(("show",))
    dialog.raise_ = lambda: calls.append(("raise_",))
    dialog.activateWindow = lambda: calls.append(("activateWindow",))
    dialog.winId = lambda: 4242
    steal_mock = steal if steal is not None else MagicMock(return_value=True)

    def _record_steal(hwnd, *args, **kwargs):
        calls.append(("_steal_foreground", hwnd))
        return steal_mock(hwnd, *args, **kwargs)

    with patch("terminal_editor_window._steal_foreground", _record_steal):
        manager._open_settings_window()
    return calls, steal_mock


def test_opening_restores_a_minimized_window(quiet_gui):
    from PySide6.QtCore import Qt

    manager = _make_manager()
    calls, _ = _reopen_with_state(manager, Qt.WindowState.WindowMinimized)
    state_calls = [c for c in calls if c[0] == "setWindowState"]
    assert len(state_calls) == 1, calls
    assert not state_calls[0][1] & Qt.WindowState.WindowMinimized
    assert state_calls[0][1] & Qt.WindowState.WindowActive
    assert calls.index(state_calls[0]) < calls.index(("show",))


def test_opening_leaves_a_normal_window_state_alone(quiet_gui):
    from PySide6.QtCore import Qt

    manager = _make_manager()
    calls, _ = _reopen_with_state(manager, Qt.WindowState.WindowNoState)
    assert [c for c in calls if c[0] == "setWindowState"] == []


def test_opening_takes_the_foreground_with_the_window_handle(quiet_gui):
    from PySide6.QtCore import Qt

    manager = _make_manager()
    calls, steal = _reopen_with_state(manager, Qt.WindowState.WindowNoState)
    steal.assert_called_once()
    assert steal.call_args[0][0] == 4242
    assert [c[0] for c in calls][-4:] == [
        "show", "raise_", "activateWindow", "_steal_foreground"]


def test_opening_survives_a_foreground_failure(quiet_gui):
    from PySide6.QtCore import Qt

    manager = _make_manager()
    steal = MagicMock(side_effect=OSError("SetForegroundWindow refused"))
    calls, _ = _reopen_with_state(
        manager, Qt.WindowState.WindowNoState, steal=steal)
    steal.assert_called_once()
    assert ("activateWindow",) in calls


# --------------------------------------------------------------------------- #
#  (d) Speech Enabled and Debug never save; the others use the menu's path
# --------------------------------------------------------------------------- #


def _saves(commands):
    return [c for c in commands
            if c.get("action") in ("set_config_value", "set_config_values")]


def test_speech_enabled_sends_the_menu_command_and_never_saves(quiet_gui):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, "Speech Enabled", QCheckBox).click()
    sent = _drain(manager.commands_to_logic_queue)
    assert sent == [{"action": "toggle_speech_enabled_state"}]
    assert _saves(sent) == []


def test_debug_sends_the_menu_command_and_never_saves(quiet_gui):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, "Debug", QCheckBox).click()
    sent = _drain(manager.commands_to_logic_queue)
    assert sent == [{"action": "toggle_log_level"}]
    assert _saves(sent) == []


def test_no_gui_state_update_ever_makes_the_window_save(quiet_gui):
    manager = _make_manager()
    _open(manager)
    manager.state_from_logic_queue.put_nowait({
        "action": "state_update", "speech_enabled": False,
        "debug_mode": True, "interim_results_enabled": False,
        "stt_providers_available": ["google_stt"], "stt_provider": "google_stt",
    })
    manager._check_queues_and_events()
    assert _saves(_drain(manager.commands_to_logic_queue)) == []


def test_interim_results_calls_the_toggle_path(quiet_gui):
    from gui import GuiManager

    manager = _make_manager()
    with patch.object(GuiManager, "toggle_interim_results") as toggle:
        dialog, _ = _open(manager)
        _drain(manager.commands_to_logic_queue)
        _named(dialog, "Interim Results", QCheckBox).click()
    toggle.assert_called_once_with()
    # The save happens in Logic (stage A); the real toggle sends only this.
    manager.toggle_interim_results()
    assert _drain(manager.commands_to_logic_queue) == [
        {"action": "toggle_interim_results"}]


def test_push_to_talk_uses_the_menu_callback(quiet_gui):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, "Push-to-Talk Mode", QCheckBox).click()
    sent = _drain(manager.commands_to_logic_queue)
    assert sent == [{"action": "set_speech_interaction_mode",
                     "mode": "push_to_talk"}]


def test_show_floating_button_uses_the_menu_callback(quiet_gui):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, "Show Floating Button", QCheckBox).click()
    sent = _drain(manager.commands_to_logic_queue)
    assert len(sent) == 1
    assert sent[0]["action"] == "set_config_value"
    assert sent[0]["key"] == "FLOATING_BUTTON_VISIBLE"
    assert sent[0]["value"] is False
    assert sent[0]["request_id"]


def test_provider_radios_use_the_menus_switch_commands(quiet_gui):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, "STT Provider: Parakeet", QRadioButton).click()
    _named(dialog, "AI Model: Qwen", QRadioButton).click()
    sent = _drain(manager.commands_to_logic_queue)
    assert sent == [
        {"action": "switch_stt_provider", "provider": "parakeet_tdt"},
        {"action": "switch_ai_provider", "provider": "qwen"},
    ]


def test_clicking_the_running_provider_sends_nothing(quiet_gui):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, "STT Provider: Google STT", QRadioButton).click()
    _named(dialog, "AI Model: GPT OSS", QRadioButton).click()
    assert _drain(manager.commands_to_logic_queue) == []


# --------------------------------------------------------------------------- #
#  (e) The saved settings go through Logic with a request ID
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name, key, expected", [
    ("Check for updates", "CHECK_FOR_UPDATES", False),
    ("Log transcripts", "LOG_TRANSCRIPTS", True),
    ("Wake word", "wake_word.enabled", True),
])
def test_a_check_box_setting_sends_set_config_value_with_a_request_id(
    quiet_gui, name, key, expected
):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, name, QCheckBox).click()
    sent = _drain(manager.commands_to_logic_queue)
    assert len(sent) == 1
    assert sent[0]["action"] == "set_config_value"
    assert sent[0]["key"] == key
    assert sent[0]["value"] is expected
    assert isinstance(sent[0]["request_id"], str) and sent[0]["request_id"]


def test_timeout_and_audio_settings_send_set_config_value(quiet_gui):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, "Increase replacement timeout").click()
    _named(dialog, "Audio suppression: off", QRadioButton).click()
    sent = _drain(manager.commands_to_logic_queue)
    assert [(c["key"], c["value"]) for c in sent] == [
        ("REPLACEMENT_TIMEOUT_MS", 800),
        ("ENABLE_AUDIO_SUPPRESSION", False),
    ]
    assert all(c["action"] == "set_config_value" and c["request_id"]
               for c in sent)


def test_the_gui_never_writes_a_config_file(quiet_gui, tmp_path):
    """No GUI-side ConfigService write: a stub that records any save call."""
    manager = _make_manager()
    dialog, _ = _open(manager)
    with patch("config_service.ConfigService.save") as save, \
         patch("config_service.ConfigService.set") as set_:
        _named(dialog, "Log transcripts", QCheckBox).click()
        _named(dialog, "Increase command timeout").click()
    save.assert_not_called()
    set_.assert_not_called()


def test_a_write_result_shows_saved_and_keeps_the_new_value(quiet_gui):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, "Log transcripts", QCheckBox).click()
    request = _write_requests_from(manager)
    manager.state_from_logic_queue.put_nowait({
        "action": "config_write_result", "request_id": request["request_id"],
        "saved": True, "values": {"LOG_TRANSCRIPTS": True},
    })
    manager._check_queues_and_events()
    assert _label_text(dialog, "Save status") == "Saved"
    assert _named(dialog, "Log transcripts", QCheckBox).isChecked() is True


def _write_requests_from(manager):
    sent = [c for c in _drain(manager.commands_to_logic_queue)
            if c.get("action") == "set_config_value"]
    assert len(sent) == 1, sent
    return sent[0]


def test_a_failed_write_shows_not_saved_and_restores_the_stored_value(
    quiet_gui,
):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, "Log transcripts", QCheckBox).click()
    assert _named(dialog, "Log transcripts", QCheckBox).isChecked() is True
    request = _write_requests_from(manager)
    manager.state_from_logic_queue.put_nowait({
        "action": "config_write_result", "request_id": request["request_id"],
        "saved": False, "values": {"LOG_TRANSCRIPTS": False},
    })
    manager._check_queues_and_events()
    assert _label_text(dialog, "Save status") == "Not saved"
    assert _named(dialog, "Log transcripts", QCheckBox).isChecked() is False
    # The restore is not a new user choice: it writes nothing.
    assert _drain(manager.commands_to_logic_queue) == []


def test_the_floating_button_write_tracking_still_works(quiet_gui):
    """The window's request IDs must not break the four tracked keys."""
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, "Show Floating Button", QCheckBox).click()
    request = _write_requests_from(manager)
    assert manager._settings_requests[request["request_id"]]["values"] == {
        "FLOATING_BUTTON_VISIBLE": False}
    manager.state_from_logic_queue.put_nowait({
        "action": "config_write_result", "request_id": request["request_id"],
        "saved": True, "values": {"FLOATING_BUTTON_VISIBLE": False},
    })
    manager._check_queues_and_events()
    assert manager.button_visible is False
    assert manager._settings_requests == {}


def test_a_failed_save_of_a_window_key_still_shows_the_gui_failure_notice(
    quiet_gui,
):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    _named(dialog, "Wake word", QCheckBox).click()
    request = _write_requests_from(manager)
    manager.state_from_logic_queue.put_nowait({
        "action": "config_write_result", "request_id": request["request_id"],
        "saved": False, "values": {"wake_word.enabled": False},
    })
    manager._check_queues_and_events()
    assert "Couldn't save settings" in manager.settings_status_text


def test_a_read_reply_for_another_request_does_not_touch_the_window(quiet_gui):
    manager = _make_manager()
    dialog, _ = _open(manager)
    manager.state_from_logic_queue.put_nowait({
        "action": "config_values_result", "request_id": "someone-else",
        "values": {"LOG_TRANSCRIPTS": True},
    })
    manager._check_queues_and_events()
    assert _named(dialog, "Log transcripts", QCheckBox).isChecked() is False


def test_the_restart_button_calls_request_restart(quiet_gui):
    manager = _make_manager()
    dialog, _ = _open(manager)
    with patch.object(type(manager), "request_restart") as restart:
        _named(dialog, "Restart WheelHouse", QPushButton).click()
    restart.assert_called_once()


def test_a_state_update_while_the_window_is_open_refreshes_without_commands(
    quiet_gui,
):
    manager = _make_manager()
    dialog, _ = _open(manager)
    _drain(manager.commands_to_logic_queue)
    manager.state_from_logic_queue.put_nowait({
        "action": "state_update", "speech_enabled": False, "debug_mode": True,
        "interim_results_enabled": False, "button_visible": True,
        "stt_provider": "parakeet_tdt",
        "stt_providers_available": ["google_stt", "parakeet_tdt"],
        "stt_provider_display_names": {"google_stt": "Google STT",
                                       "parakeet_tdt": "Parakeet"},
        "ai_provider": "qwen", "ai_providers_available": ["gpt-oss", "qwen"],
        "ai_provider_display_names": {"gpt-oss": "GPT OSS", "qwen": "Qwen"},
    })
    manager._check_queues_and_events()
    assert not _named(dialog, "Speech Enabled", QCheckBox).isChecked()
    assert _named(dialog, "Debug", QCheckBox).isChecked()
    assert not _named(dialog, "Interim Results", QCheckBox).isChecked()
    assert _named(dialog, "STT Provider: Parakeet", QRadioButton).isChecked()
    assert _named(dialog, "AI Model: Qwen", QRadioButton).isChecked()
    assert _drain(manager.commands_to_logic_queue) == []


def test_the_push_to_talk_toggle_never_touches_the_window(quiet_gui):
    # The tray menu calls _toggle_ptt_mode on pystray's own thread
    # (gui.py tray branch of _create_menu). Qt widgets may change only on
    # the Qt thread, so the toggle must not refresh the window itself; the
    # state update that Logic sends after the mode change does it.
    manager = _make_manager()
    dialog, _ = _open(manager)
    with patch.object(manager, "_refresh_settings_window") as refresh, \
         patch.object(dialog, "apply_live_state") as apply_live_state:
        manager._toggle_ptt_mode()
    refresh.assert_not_called()
    apply_live_state.assert_not_called()


def test_the_state_update_after_a_push_to_talk_change_refreshes_the_window(
    quiet_gui,
):
    manager = _make_manager()
    dialog, _ = _open(manager)
    manager.state_from_logic_queue.put_nowait({
        "action": "state_update", "speech_enabled": False,
        "speech_interaction_mode": "push_to_talk",
    })
    manager._check_queues_and_events()
    assert _named(dialog, "Push-to-Talk Mode", QCheckBox).isChecked()
