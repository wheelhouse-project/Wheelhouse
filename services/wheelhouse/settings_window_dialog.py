"""Settings window -- the menu settings and a few more in one window.

Opened by the right-click menu item "Settings" and by the voice command
"settings" (wh-voice-settings-window). It lives in the GUI process and holds
no settings logic of its own: it shows what it is given and emits a signal
for each change. ``GuiManager`` (gui.py) connects those signals to the same
callables the right-click menu uses, and sends the saved settings to the
Logic process, which owns config.toml.

Voice access: every control is a check box, a radio button or a push button,
so "show numbers" and "click N" can change any setting. There is no combo
box, spin box or text field. A timeout is a value label with a Decrease and
an Increase button.

IPC contract:
    - Emits the ``*_requested`` signals for settings 1 to 7 (the menu's own
      settings; the GUI manager calls the menu's callable for each).
    - Emits ``setting_changed(key, value)`` for settings 8 to 13, which the
      GUI manager saves through the Logic process.
    - Receives state via ``apply_live_state`` (settings 1 to 7) and
      ``apply_config_values`` (settings 8 to 13), and the outcome of a save
      via ``show_save_result``.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

# The size clamp is shared with the Pattern Manager window: both must stay
# inside the screen that shows them.
from pattern_manager_dialog import _clamp_dialog_size
from settings_help_dialog import (
    SettingsHelpDialog,
    SettingsHelpError,
    load_settings_help,
)
from services.wheelhouse.utils.audio_suppression_decision import (
    AUDIO_SUPPRESSION_DEFAULT,
    parse_audio_suppression_setting,
)
from update_check import UPDATE_CHECK_DEFAULT

logger = logging.getLogger(__name__)

# The settings the window reads from, and saves through, the Logic process.
CHECK_FOR_UPDATES_KEY = "CHECK_FOR_UPDATES"
LOG_TRANSCRIPTS_KEY = "LOG_TRANSCRIPTS"
REPLACEMENT_TIMEOUT_KEY = "REPLACEMENT_TIMEOUT_MS"
COMMAND_TIMEOUT_KEY = "COMMAND_TIMEOUT_MS"
AUDIO_SUPPRESSION_KEY = "ENABLE_AUDIO_SUPPRESSION"
WAKE_WORD_KEY = "wake_word.enabled"

CONFIG_KEYS = (
    CHECK_FOR_UPDATES_KEY,
    LOG_TRANSCRIPTS_KEY,
    REPLACEMENT_TIMEOUT_KEY,
    COMMAND_TIMEOUT_KEY,
    AUDIO_SUPPRESSION_KEY,
    WAKE_WORD_KEY,
)

# What a missing key means. CHECK_FOR_UPDATES and ENABLE_AUDIO_SUPPRESSION
# are imported from the modules that read them. LOG_TRANSCRIPTS is on only
# for a true boolean (launcher.py _read_transcript_logging_flag). The wake
# word is off when the key is missing: service_manager.py reads
# config_service.get("wake_word.enabled", False) inline, so there is no
# constant to import.
WAKE_WORD_ENABLED_DEFAULT = False

RESTART_TEXT = "takes effect after restart"
WAKE_WORD_SPEECH_WARNING = (
    "If the wake word is off and you turn speech off, only the keyboard or "
    "mouse can turn speech on again."
)

# Boss ruling R3 (2026-10-09): the Decrease and Increase buttons move a
# timeout by 100 ms inside 200 to 3000 ms.
TIMEOUT_STEP_MS = 100
TIMEOUT_MIN_MS = 200
TIMEOUT_MAX_MS = 3000

_PREFERRED_SIZE = QSize(720, 820)
_MIN_SIZE = QSize(520, 420)


def _is_number(value) -> bool:
    """True for a finite int or float that is not a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def next_timeout_value(current, direction: int):
    """The value a Decrease (direction < 0) or Increase (> 0) press gives.

    Returns None when the press must do nothing: ``current`` is not a
    number, or it already sits at the limit the press moves toward.

    A value outside the range moves to the nearest end of the range on
    either press. A value inside the range that is not a multiple of the
    step moves to the next multiple in the button's direction. The window
    never calls this before a press, so it never rewrites a value the user
    did not change.
    """
    if not _is_number(current):
        return None
    if current < TIMEOUT_MIN_MS:
        return TIMEOUT_MIN_MS
    if current > TIMEOUT_MAX_MS:
        return TIMEOUT_MAX_MS
    if direction > 0:
        target = (current // TIMEOUT_STEP_MS + 1) * TIMEOUT_STEP_MS
        target = min(int(target), TIMEOUT_MAX_MS)
    else:
        ceiling = -((-current) // TIMEOUT_STEP_MS)
        target = max(int((ceiling - 1) * TIMEOUT_STEP_MS), TIMEOUT_MIN_MS)
    if target == current:
        return None
    return target


def _format_ms(value) -> str:
    """The value the program uses, as text; "not set" when it has none."""
    if not _is_number(value):
        return "not set"
    if float(value).is_integer():
        return f"{int(value)} ms"
    return f"{value:g} ms"


def _audio_choice(value) -> str:
    """"auto", "on" or "off": what the program makes of the stored value.

    A missing key means the program's default. The parser is the one
    main.py's startup decision uses, so a value it reads as "auto" (a
    mistyped string, for instance) shows as "auto" here as well.
    """
    if value is None:
        value = AUDIO_SUPPRESSION_DEFAULT
    forced = parse_audio_suppression_setting(value)
    if forced is True:
        return "on"
    if forced is False:
        return "off"
    return "auto"


_AUDIO_VALUES = {"auto": "auto", "on": True, "off": False}


def _set_checked_quietly(button, checked: bool) -> None:
    """Set a button's state without letting it signal."""
    was_blocked = button.blockSignals(True)
    try:
        button.setChecked(checked)
    finally:
        button.blockSignals(was_blocked)


def _check_only(group: QButtonGroup, target) -> None:
    """Check ``target`` (a button, or None for none) inside an exclusive group.

    An exclusive group refuses to uncheck its checked button, so it is made
    non-exclusive for the change.
    """
    group.setExclusive(False)
    for button in group.buttons():
        _set_checked_quietly(button, button is target)
    group.setExclusive(True)


class _ProviderGroup:
    """One radio button per provider, rebuilt when the provider list changes.

    The list and the callable are the right-click menu's own: the entries
    come from the same GuiManager lists and a choice calls the same switch.
    Entries that cannot run yet (an engine without its model) are push
    buttons, not radio buttons, because choosing one does not change the
    running engine and a radio button would then show the wrong one.
    """

    def __init__(self, dialog, layout, prefix, on_choose):
        self._dialog = dialog
        self._layout = layout
        self._prefix = prefix
        self._on_choose = on_choose
        self._buttons = QButtonGroup(dialog)
        self._buttons.setExclusive(True)
        self._radios: dict[str, QRadioButton] = {}
        self._selectable: dict[str, bool] = {}
        self._extras: list[QPushButton] = []
        self._absent: QRadioButton | None = None
        self._signature = None
        self.current = None

    def widgets(self) -> list:
        order = list(self._radios.values())
        if self._absent is not None:
            order.append(self._absent)
        return order + list(self._extras)

    def update(self, entries, current, extras, absent_label, ready) -> bool:
        """Show ``entries`` with ``current`` checked; True if rebuilt."""
        signature = (tuple(entries), tuple(extras), absent_label)
        rebuilt = signature != self._signature
        if rebuilt:
            self._rebuild(entries, extras, absent_label)
            self._signature = signature
        self.current = current
        target = self._radios.get(current)
        if target is None and absent_label:
            target = self._absent
        _check_only(self._buttons, target)
        for provider, radio in self._radios.items():
            radio.setEnabled(ready and self._selectable[provider])
        for button in self._extras:
            button.setEnabled(ready)
        return rebuilt

    def _discard(self, widget) -> None:
        if isinstance(widget, QRadioButton):
            self._buttons.removeButton(widget)
        self._layout.removeWidget(widget)
        widget.hide()
        widget.setParent(None)
        widget.deleteLater()

    def _rebuild(self, entries, extras, absent_label) -> None:
        for widget in self.widgets():
            self._discard(widget)
        self._radios = {}
        self._selectable = {}
        self._extras = []
        self._absent = None
        for provider, label, selectable in entries:
            radio = QRadioButton(label)
            radio.setAccessibleName(f"{self._prefix}: {label}")
            radio.clicked.connect(
                lambda checked=False, p=provider: self._chosen(p)
            )
            self._buttons.addButton(radio)
            self._layout.addWidget(radio)
            self._radios[provider] = radio
            self._selectable[provider] = bool(selectable)
        if absent_label:
            # The menu lists this as a disabled entry: the settings file
            # names a model the server does not offer. Nothing to choose.
            self._absent = QRadioButton(absent_label)
            self._absent.setAccessibleName(f"{self._prefix}: {absent_label}")
            self._absent.setEnabled(False)
            self._buttons.addButton(self._absent)
            self._layout.addWidget(self._absent)
        for provider, label in extras:
            button = QPushButton(label)
            button.setAccessibleName(f"{self._prefix}: {label}")
            button.setAutoDefault(False)
            button.setDefault(False)
            button.clicked.connect(
                lambda checked=False, p=provider: self._on_choose(p)
            )
            self._layout.addWidget(button)
            self._extras.append(button)

    def _chosen(self, provider) -> None:
        # The running provider is already the one the user picked.
        if provider != self.current:
            self._on_choose(provider)


class SettingsWindowDialog(QDialog):
    """The settings window.

    Signals (settings 1 to 7 use the right-click menu's own callable, wired
    in ``GuiManager._open_settings_window``; none of them saves anything
    from this window):
        speech_toggle_requested, button_visibility_toggle_requested,
        interim_toggle_requested, ptt_toggle_requested,
        stt_provider_requested(str), ai_provider_requested(str),
        debug_toggle_requested.

    Settings 8 to 13 emit ``setting_changed(key, value)``; the GUI manager
    saves each one through the Logic process.

    ``restart_requested`` asks the GUI manager to restart WheelHouse.
    """

    speech_toggle_requested = Signal()
    button_visibility_toggle_requested = Signal()
    interim_toggle_requested = Signal()
    ptt_toggle_requested = Signal()
    stt_provider_requested = Signal(str)
    ai_provider_requested = Signal(str)
    debug_toggle_requested = Signal()
    setting_changed = Signal(str, object)
    restart_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("WheelHouse Settings")
        # Standard title-bar minimize and maximize buttons, so the window
        # behaves like the Pattern Manager for the voice commands that
        # minimize and maximize a window.
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self._apply_screen_bounded_size()
        # Closing this window must not end the program.
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)

        # Stored values of settings 8 to 13, as the Logic process reported
        # them (None for a key the settings file does not hold).
        self._values: dict = {}
        self._loaded = False
        self._ready = False
        self._tab_widgets: list = []
        # The 13 setting labels in window order, recorded where _build_ui
        # creates each control (setting_labels).
        self._setting_labels: list[str] = []
        self._help_window = None

        self._build_ui()
        self._refresh_config_controls()

    # ------------------------------------------------------------------ #
    #  Sizing and closing
    # ------------------------------------------------------------------ #

    def _apply_screen_bounded_size(self):
        """Set the minimum and initial size, clamped to the current screen."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
        else:
            available = QRect(
                0, 0, _PREFERRED_SIZE.width(), _PREFERRED_SIZE.height()
            )
        min_size, initial_size = _clamp_dialog_size(
            _PREFERRED_SIZE, _MIN_SIZE, available
        )
        self.setMinimumSize(min_size)
        self.resize(initial_size)

    def closeEvent(self, event):
        """Hide instead of closing, so the window is built once."""
        self.hide()
        event.ignore()

    # ------------------------------------------------------------------ #
    #  UI construction
    # ------------------------------------------------------------------ #

    @staticmethod
    def _row(parent_layout):
        """A row widget with a horizontal layout, added to ``parent_layout``."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        parent_layout.addWidget(row)
        return row, layout

    @staticmethod
    def _note(text):
        label = QLabel(text)
        label.setWordWrap(True)
        return label

    def _check_box(self, parent_layout, text, on_click):
        self._setting_labels.append(text)
        row, layout = self._row(parent_layout)
        box = QCheckBox(text)
        box.setAccessibleName(text)
        box.clicked.connect(on_click)
        layout.addWidget(box)
        return box, layout

    def _group_box(self, parent_layout, title):
        group = QGroupBox(title)
        group.setAccessibleName(title)
        layout = QVBoxLayout(group)
        parent_layout.addWidget(group)
        return layout

    def _build_ui(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)

        # --- Settings 1 to 7: the right-click menu's own settings ---
        menu_layout = self._group_box(main, "Menu settings")

        self._speech_box, layout = self._check_box(
            menu_layout, "Speech Enabled",
            lambda checked=False: self.speech_toggle_requested.emit(),
        )
        # Beside Speech Enabled and the wake word, because turning speech
        # off with the wake word off leaves no spoken way back (boss ruling
        # R4). A static text: no confirmation step.
        layout.addWidget(self._note(WAKE_WORD_SPEECH_WARNING), 1)

        self._button_box, _ = self._check_box(
            menu_layout, "Show Floating Button",
            lambda checked=False: self.button_visibility_toggle_requested.emit(),
        )
        self._interim_box, _ = self._check_box(
            menu_layout, "Interim Results",
            lambda checked=False: self.interim_toggle_requested.emit(),
        )
        self._ptt_box, _ = self._check_box(
            menu_layout, "Push-to-Talk Mode",
            lambda checked=False: self.ptt_toggle_requested.emit(),
        )
        self._debug_box, _ = self._check_box(
            menu_layout, "Debug",
            lambda checked=False: self.debug_toggle_requested.emit(),
        )

        self._setting_labels.append("STT Provider")
        stt_layout = self._group_box(main, "STT Provider")
        self._stt = _ProviderGroup(
            self, stt_layout, "STT Provider", self.stt_provider_requested.emit
        )
        self._setting_labels.append("AI Model")
        ai_layout = self._group_box(main, "AI Model")
        self._ai = _ProviderGroup(
            self, ai_layout, "AI Model", self.ai_provider_requested.emit
        )

        # --- Settings 8 to 13: saved in config.toml through Logic ---
        other_layout = self._group_box(main, "Other settings")

        self._updates_box, _ = self._check_box(
            other_layout, "Check for updates",
            lambda checked=False: self._on_check(
                CHECK_FOR_UPDATES_KEY, self._updates_box),
        )
        self._transcripts_box, layout = self._check_box(
            other_layout, "Log transcripts",
            lambda checked=False: self._on_check(
                LOG_TRANSCRIPTS_KEY, self._transcripts_box),
        )
        self._restart_notes = {}
        self._add_restart_note(layout, LOG_TRANSCRIPTS_KEY)

        self._timeouts = {}
        for key, title, short in (
            (REPLACEMENT_TIMEOUT_KEY, "Replacement timeout", "replacement"),
            (COMMAND_TIMEOUT_KEY, "Command timeout", "command"),
        ):
            self._timeouts[key] = self._build_timeout_row(
                other_layout, key, title, short)

        row, layout = self._row(other_layout)
        self._setting_labels.append("Audio suppression")
        layout.addWidget(QLabel("Audio suppression"))
        self._audio_group = QButtonGroup(self)
        self._audio_group.setExclusive(True)
        self._audio_radios = {}
        for choice in ("auto", "on", "off"):
            radio = QRadioButton(choice.capitalize())
            radio.setAccessibleName(f"Audio suppression: {choice}")
            radio.clicked.connect(
                lambda checked=False, c=choice: self._on_audio(c)
            )
            self._audio_group.addButton(radio)
            self._audio_radios[choice] = radio
            layout.addWidget(radio)
        self._add_restart_note(layout, AUDIO_SUPPRESSION_KEY)

        self._wake_box, layout = self._check_box(
            other_layout, "Wake word",
            lambda checked=False: self._on_check(WAKE_WORD_KEY, self._wake_box),
        )
        self._add_restart_note(layout, WAKE_WORD_KEY)
        layout.addWidget(self._note(WAKE_WORD_SPEECH_WARNING), 1)

        main.addStretch(1)

        # --- Bottom row: save result, Restart WheelHouse, Close ---
        bottom, layout = self._row(main)
        self._status = QLabel("Loading settings...")
        self._status.setAccessibleName("Save status")
        layout.addWidget(self._status, 1)
        self._help_btn = QPushButton("Help")
        self._help_btn.setAccessibleName("Help")
        self._help_btn.clicked.connect(
            lambda checked=False: self._on_help_clicked()
        )
        layout.addWidget(self._help_btn)
        self._restart_btn = QPushButton("Restart WheelHouse")
        self._restart_btn.setAccessibleName("Restart WheelHouse")
        self._restart_btn.clicked.connect(
            lambda checked=False: self.restart_requested.emit()
        )
        layout.addWidget(self._restart_btn)
        self._close_btn = QPushButton("Close")
        self._close_btn.setAccessibleName("Close")
        self._close_btn.clicked.connect(lambda checked=False: self.close())
        layout.addWidget(self._close_btn)

        # No button may be the default: Enter must not click one by itself.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

        self._apply_tab_order()

    def _add_restart_note(self, layout, key):
        note = self._note(RESTART_TEXT)
        self._restart_notes[key] = note
        layout.addWidget(note)

    def _build_timeout_row(self, parent_layout, key, title, short):
        row, layout = self._row(parent_layout)
        self._setting_labels.append(title)
        layout.addWidget(QLabel(title))
        decrease = QPushButton("Decrease")
        decrease.setAccessibleName(f"Decrease {short} timeout")
        decrease.clicked.connect(
            lambda checked=False, k=key: self._on_step(k, -1)
        )
        value = QLabel("")
        value.setAccessibleName(f"{title} value")
        increase = QPushButton("Increase")
        increase.setAccessibleName(f"Increase {short} timeout")
        increase.clicked.connect(
            lambda checked=False, k=key: self._on_step(k, +1)
        )
        layout.addWidget(decrease)
        layout.addWidget(value)
        layout.addWidget(increase)
        self._add_restart_note(layout, key)
        return {"decrease": decrease, "value": value, "increase": increase}

    # ------------------------------------------------------------------ #
    #  Help window
    # ------------------------------------------------------------------ #

    def _on_help_clicked(self) -> None:
        """Show the help window; on a bad help file, say what is wrong.

        The help window is not modal, so this window keeps working. One
        instance is kept: a second click shows the same window again.
        """
        try:
            pairs = load_settings_help()
        except SettingsHelpError as exc:
            self._show_help_error(str(exc))
            return
        dlg = self._help_window
        if dlg is not None:
            import shiboken6

            if not shiboken6.isValid(dlg):
                dlg = None
        if dlg is None:
            dlg = SettingsHelpDialog(pairs, parent=self)
            dlg.setFont(self.font())
            self._help_window = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _show_help_error(self, message: str) -> None:
        """Warn that the help text cannot be shown (patched out in tests)."""
        QMessageBox.warning(self, "WheelHouse Settings Help", message)

    # ------------------------------------------------------------------ #
    #  Tab order
    # ------------------------------------------------------------------ #

    def tab_order_widgets(self) -> list:
        """Every control, in the order Tab visits them."""
        order = [
            self._speech_box, self._button_box, self._interim_box,
            self._ptt_box, self._debug_box,
            *self._stt.widgets(), *self._ai.widgets(),
            self._updates_box, self._transcripts_box,
        ]
        for key in (REPLACEMENT_TIMEOUT_KEY, COMMAND_TIMEOUT_KEY):
            order += [self._timeouts[key]["decrease"],
                      self._timeouts[key]["increase"]]
        order += [self._audio_radios[c] for c in ("auto", "on", "off")]
        order += [self._wake_box, self._help_btn, self._restart_btn,
                  self._close_btn]
        return order

    def setting_labels(self) -> list:
        """The 13 setting labels, in the order the window shows them."""
        return list(self._setting_labels)

    def _apply_tab_order(self) -> None:
        """Tab follows the visual order; rebuilt after a provider list change."""
        order = self.tab_order_widgets()
        for first, second in zip(order, order[1:]):
            QWidget.setTabOrder(first, second)

    # ------------------------------------------------------------------ #
    #  Settings 1 to 7: live state
    # ------------------------------------------------------------------ #

    def apply_live_state(self, state: dict) -> None:
        """Show the live state the GUI manager holds; sends no command.

        Keys: ready, speech_enabled, button_visible, interim_results,
        push_to_talk, debug, stt_providers [(id, label)], stt_current,
        stt_not_installed [(id, label)], ai_providers [(id, label,
        selectable)], ai_current, ai_absent_label.
        """
        self._ready = bool(state.get("ready"))
        for box, key in (
            (self._speech_box, "speech_enabled"),
            (self._button_box, "button_visible"),
            (self._interim_box, "interim_results"),
            (self._ptt_box, "push_to_talk"),
            (self._debug_box, "debug"),
        ):
            _set_checked_quietly(box, bool(state.get(key)))
            box.setEnabled(self._ready)

        rebuilt = self._stt.update(
            [(p, label, True) for p, label in state.get("stt_providers", [])],
            state.get("stt_current"),
            list(state.get("stt_not_installed", [])),
            None,
            self._ready,
        )
        rebuilt = self._ai.update(
            list(state.get("ai_providers", [])),
            state.get("ai_current"),
            [],
            state.get("ai_absent_label"),
            self._ready,
        ) or rebuilt
        if rebuilt:
            self._apply_tab_order()

    # ------------------------------------------------------------------ #
    #  Settings 8 to 13: stored values
    # ------------------------------------------------------------------ #

    def apply_config_values(self, values: dict) -> None:
        """Show the values the Logic process reports; sends nothing.

        ``values`` maps a key to its stored value, or None for a key the
        settings file does not hold. The controls stay disabled until every
        key has been reported once.
        """
        for key in CONFIG_KEYS:
            if key in values:
                self._values[key] = values[key]
        if not self._loaded and all(k in self._values for k in CONFIG_KEYS):
            self._loaded = True
            self._status.setText("")
        self._refresh_config_controls()

    def _effective_bool(self, key) -> bool:
        value = self._values.get(key)
        if key == CHECK_FOR_UPDATES_KEY:
            return UPDATE_CHECK_DEFAULT if value is None else bool(value)
        if key == LOG_TRANSCRIPTS_KEY:
            return value is True
        return WAKE_WORD_ENABLED_DEFAULT if value is None else bool(value)

    def _refresh_config_controls(self) -> None:
        for key, box in (
            (CHECK_FOR_UPDATES_KEY, self._updates_box),
            (LOG_TRANSCRIPTS_KEY, self._transcripts_box),
            (WAKE_WORD_KEY, self._wake_box),
        ):
            _set_checked_quietly(box, self._effective_bool(key))
            box.setEnabled(self._loaded)
        for key, parts in self._timeouts.items():
            value = self._values.get(key)
            parts["value"].setText(_format_ms(value))
            usable = self._loaded and _is_number(value)
            parts["decrease"].setEnabled(usable)
            parts["increase"].setEnabled(usable)
        choice = _audio_choice(self._values.get(AUDIO_SUPPRESSION_KEY))
        _check_only(self._audio_group, self._audio_radios[choice])
        for radio in self._audio_radios.values():
            radio.setEnabled(self._loaded)

    def show_save_result(self, saved: bool) -> None:
        """Say whether the last save reached config.toml."""
        self._status.setText("Saved" if saved else "Not saved")

    # ------------------------------------------------------------------ #
    #  User actions on settings 8 to 13
    # ------------------------------------------------------------------ #

    def _on_check(self, key, box) -> None:
        checked = box.isChecked()
        self._values[key] = checked
        self.setting_changed.emit(key, checked)

    def _on_audio(self, choice) -> None:
        # Choosing what is already selected changes nothing, so it writes
        # nothing: the stored value may be spelled differently ("Auto ").
        if choice == _audio_choice(self._values.get(AUDIO_SUPPRESSION_KEY)):
            return
        value = _AUDIO_VALUES[choice]
        self._values[AUDIO_SUPPRESSION_KEY] = value
        self.setting_changed.emit(AUDIO_SUPPRESSION_KEY, value)

    def _on_step(self, key, direction) -> None:
        new_value = next_timeout_value(self._values.get(key), direction)
        if new_value is None:
            if _is_number(self._values.get(key)):
                limit = "lowest" if direction < 0 else "highest"
                self._status.setText(f"Already at the {limit} value.")
            return
        self._values[key] = new_value
        self._timeouts[key]["value"].setText(_format_ms(new_value))
        self.setting_changed.emit(key, new_value)
