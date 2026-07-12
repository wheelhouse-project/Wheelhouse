"""Two-mode pattern editor dialog (wh-pattern-editor-dialog).

Opened from PatternManagerDialog for Add (create mode) and, by later
manager-window work, for Edit/Duplicate/Customize (edit mode passes the
stored entry plus its pattern id). One dialog, two modes (spec section 8 of
docs/plans/2026-07-09-pattern-manager-editor-design-v1.md):

* Simple mode: a phrase-list editor (one or more spoken phrasings), the four
  basic action types with their parameter fields, the wake-word checkbox,
  and a debounced try-it line driven by pm_test_draft.
* Advanced mode (wh-pattern-editor-advanced): an editable expression field
  with a live local compile check, capture-group count, and a regex101
  link; an honest pattern-type chooser (validated against the expression's
  ``^`` anchoring, since the runtime loader decides the kind from the
  anchor alone); and an ordered step list whose rows are generated from
  speech/action_catalog.py (internal-audience functions hidden). Saves from
  advanced mode carry the raw ``expression`` + raw ``actions`` steps and NO
  ``phrases`` key, which is what reopens the pattern in advanced mode next
  time (spec section 6).

IPC: the dialog emits ``pattern_action(dict)`` (pm_create_pattern /
pm_update_pattern / pm_test_draft) and receives the matching ``*_result``
messages via ``handle_response(dict)``, forwarded by PatternManagerDialog
while the dialog is open. On save success the dialog closes; on failure the
error shows inline and the dialog stays open (spec section 14).

Mode on open is decided by STORED DATA only (spec section 6): phrases
present, exactly one action, and that action one of the four basic types
opens simple mode; anything else opens advanced. Mode transitions keep the
two panes in sync: entering advanced rebuilds the expression and step list
from the simple fields; returning to simple (allowed only while the
expression is untouched and the single step is basic) pushes the step back
into the simple fields, so no edit is silently dropped.

A fresh Add (no entry, no pattern_id) opens with the "What do you want to
happen?" goal page (wh-pattern-editor-templates, spec section 11): six
concrete goals that prefill the simple pane with the right action type,
goal-appropriate wording/placeholders, and focus in the first empty field.
Edit/Duplicate/Customize (entry passed) never see the page, and it never
reappears once a goal is chosen -- it is a starting point, not a wizard.
"""
import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLineEdit,
    QRadioButton,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QKeySequenceEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QWidget,
    QLabel,
    QFileDialog,
)

from pattern_help_dialog import REGEX_CHECKER_URL
from speech.action_catalog import ACTION_CATALOG, CATALOG_BY_NAME
from speech.key_names import VALID_KEY_NAMES
from speech.phrase_expression import generate_expression, validate_phrases

# Debounce for the try-it line: restart on every keystroke, send when the
# user pauses (QTimer single-shot restart pattern).
TRY_IT_DEBOUNCE_MS = 400

# How long a Save waits for its pm_create_result / pm_update_result before
# giving up (wh-pattern-editor-r0.2). A lost response must not leave the
# dialog stuck with Save disabled and no explanation.
SAVE_RESPONSE_TIMEOUT_MS = 5000

# Stored action function -> simple-mode action type. These are the four
# functions generate_actions produces, so only they can round-trip through
# the simple pane. (The catalog's "basic" audience also lists insert_text,
# but the simple editor cannot regenerate it -- such patterns open in
# advanced mode.)
_BASIC_FUNCTIONS = {
    "hk": "hotkey",
    "text": "text",
    "run": "run",
    "activate": "activate",
}

_ERROR_STYLE = "color: #dc2626; font-size: 11px;"
_OK_STYLE = "color: #15803d; font-size: 11px;"
_MUTED_STYLE = "color: gray; font-size: 11px;"

# A trailing hk param that is a stored int, a digit string, or a capture
# reference is the optional repeat count (mirrors the runtime peel in
# ActionFunctions.hotkey, which words_to_int's the last argument). Multi-
# digit references (g10+) are legal whenever the expression has that many
# groups -- same rule as _validate_group_refs (wh-pattern-editor-r5.1).
_HK_REPEAT_RE = re.compile(r"g[1-9][0-9]*")

# Hover text for group_ref parameter fields (spec section 8).
_GROUP_REF_HELP = (
    "g1..gN pass the words captured by that numbered group in the "
    "expression to this action."
)

# ---------------------------------------------------------------------------
# Add-flow goal templates (wh-pattern-editor-templates, spec section 11)
# ---------------------------------------------------------------------------
# Default wording the templates may override; applying any template writes
# every one of these strings (override or default), so the pane's guidance
# is deterministic no matter which goal was picked.
_DEFAULT_PHRASES_LABEL = "What should you say? (one or more phrasings)"
_DEFAULT_PHRASE_PLACEHOLDER = "e.g., save project"
_DEFAULT_TEXT_LABEL = "Output text:"
_DEFAULT_TEXT_PLACEHOLDER = "e.g., GPT"

# The "What do you want to happen?" goal list, in spec order. Each entry:
# ``key`` (stable id, stored as item data), ``title`` (list row), ``help``
# (one sentence of hover help), ``action`` (simple-mode radio to preselect;
# None leaves the pane exactly as today -- start from scratch), and optional
# wording overrides. The two text goals differ only in wording, placeholder
# text, and hover help: the snippet goal teaches "phrase -> text to type",
# the correction goal teaches the replacement idiom "what the microphone
# hears -> what you meant" (the dialog already infers the replacement
# pattern type from the text action).
_GOAL_TEMPLATES = [
    {
        "key": "run",
        "title": "Open a program",
        "help": (
            "Launches a program when you say the phrase, like opening "
            "Notepad by voice."
        ),
        "action": "run",
        "phrase_placeholder": "e.g., open notepad",
    },
    {
        "key": "activate",
        "title": "Switch to an app",
        "help": (
            "Brings a window that is already open to the front, like "
            "jumping to your browser."
        ),
        "action": "activate",
        "phrase_placeholder": "e.g., go to browser",
    },
    {
        "key": "hotkey",
        "title": "Press a keyboard shortcut",
        "help": (
            "Presses keys for you, like Ctrl+S to save, without touching "
            "the keyboard."
        ),
        "action": "hotkey",
        "phrase_placeholder": "e.g., save file",
    },
    {
        "key": "text",
        "title": "Type a phrase you say often",
        "help": (
            "Types a snippet you dictate often, like your email address, "
            "wherever the cursor is."
        ),
        "action": "text",
        "phrase_placeholder": "e.g., my email",
        "text_label": "Text to type:",
        "text_placeholder": "e.g., yourname@example.com",
        "text_tooltip": (
            "The exact text WheelHouse types when you say the phrase."
        ),
    },
    {
        "key": "correction",
        "title": "Correct a word the microphone keeps getting wrong",
        "help": (
            "Replaces a word the microphone keeps mishearing with the "
            "word you actually said."
        ),
        "action": "text",
        "phrases_label": "What does the microphone type by mistake?",
        "phrase_placeholder": "e.g., jason",
        "text_label": "What should it type instead?",
        "text_placeholder": "e.g., JSON",
        "text_tooltip": (
            "Whenever dictation produces the misheard word, WheelHouse "
            "types this instead."
        ),
    },
    {
        "key": "scratch",
        "title": "Start from scratch",
        "help": "Opens the blank editor with every option available.",
        "action": None,
    },
]

# ---------------------------------------------------------------------------
# Qt key capture -> WheelHouse key names (wh-pattern-editor-record-keys)
# ---------------------------------------------------------------------------
# The Input process presses hotkeys via utils/win_input_sender.py
# press_keys, which looks every name up in VK_CODE_MAP and ABORTS the whole
# chord on any unmapped name. The converter must therefore emit ONLY names
# that map holds (lowercase, '+'-joined); the test suite cross-checks the
# full emittable set against VK_CODE_MAP.

# Canonical modifier order (matches the spec's ctrl+shift+n example).
_QT_MODIFIER_NAMES = (
    (Qt.KeyboardModifier.ControlModifier, "ctrl"),
    (Qt.KeyboardModifier.ShiftModifier, "shift"),
    (Qt.KeyboardModifier.AltModifier, "alt"),
    (Qt.KeyboardModifier.MetaModifier, "win"),
)

# A capture that is only a modifier key is an unfinished combination.
_QT_MODIFIER_KEY_CODES = frozenset(int(k) for k in (
    Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
    Qt.Key.Key_Meta, Qt.Key.Key_AltGr,
))

# Named keys and the punctuation VK_CODE_MAP itself lists. '+' is
# deliberately absent: it is the join character of the key field (Shift+=
# is handled as a special case instead). Qt reports Shift+Tab as
# Key_Backtab, so it maps to tab and the shift modifier restores the chord.
_QT_KEY_NAMES = {int(k): v for k, v in {
    Qt.Key.Key_Return: "enter",
    Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Backtab: "tab",
    Qt.Key.Key_Escape: "esc",
    Qt.Key.Key_Space: "space",
    Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Delete: "delete",
    Qt.Key.Key_Insert: "insert",
    Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "pageup",
    Qt.Key.Key_PageDown: "pagedown",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_Down: "down",
    Qt.Key.Key_Print: "printscreen",
    Qt.Key.Key_Pause: "pause",
    Qt.Key.Key_CapsLock: "capslock",
    Qt.Key.Key_Equal: "=",
    Qt.Key.Key_Minus: "-",
    Qt.Key.Key_Underscore: "_",
    Qt.Key.Key_Semicolon: ";",
    Qt.Key.Key_Colon: ":",
    Qt.Key.Key_Slash: "/",
    Qt.Key.Key_Question: "?",
    Qt.Key.Key_QuoteLeft: "`",
    Qt.Key.Key_AsciiTilde: "~",
    Qt.Key.Key_BracketLeft: "[",
    Qt.Key.Key_BraceLeft: "{",
    Qt.Key.Key_Backslash: "\\",
    Qt.Key.Key_Bar: "|",
    Qt.Key.Key_BracketRight: "]",
    Qt.Key.Key_BraceRight: "}",
    Qt.Key.Key_Apostrophe: "'",
    Qt.Key.Key_QuoteDbl: '"',
    Qt.Key.Key_Comma: ",",
    Qt.Key.Key_Less: "<",
    Qt.Key.Key_Period: ".",
    Qt.Key.Key_Greater: ">",
}.items()}


def _qt_enum_value(value) -> int:
    """Plain int of a Qt enum/flag or int. Qt.Key is an IntEnum but
    Qt.KeyboardModifier is a plain enum.Flag, which int() rejects."""
    return int(getattr(value, "value", value))


def _qt_key_display(key: int) -> str:
    """Human-readable name of a Qt key for error messages."""
    try:
        return Qt.Key(key).name.removeprefix("Key_")
    except ValueError:
        return f"key code {key}"


def qt_key_to_wheelhouse(key, modifiers):
    """Convert a captured Qt key + modifiers to WheelHouse key names.

    Pure function: Qt key data in (``Qt.Key``/``Qt.KeyboardModifier`` or
    plain ints), WheelHouse name string out. Returns ``(combo, error)``
    where exactly one is None: ``combo`` is lowercase names joined with
    '+' in canonical modifier order (ctrl, shift, alt, win), every name
    guaranteed to be in the Input process VK_CODE_MAP; ``error`` is a
    field-level message for a key WheelHouse cannot press.
    """
    key = _qt_enum_value(key)
    mods = _qt_enum_value(modifiers)
    if key in _QT_MODIFIER_KEY_CODES:
        return None, (
            "Keep holding the modifiers and press a regular key to finish "
            "the combination (for example Ctrl+Shift+N)"
        )
    if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z):
        base = chr(key).lower()
    elif int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
        base = chr(key)
    elif int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F12):
        base = f"f{key - int(Qt.Key.Key_F1) + 1}"
    elif int(Qt.Key.Key_F13) <= key <= int(Qt.Key.Key_F35):
        return None, "WheelHouse can only press F1 through F12"
    elif key == int(Qt.Key.Key_Plus) and (
        mods & _qt_enum_value(Qt.KeyboardModifier.ShiftModifier)
    ):
        # '+' cannot appear in the '+'-joined field. Shift+= IS the plus
        # key on the layout VK_CODE_MAP assumes, so writing '=' (with the
        # shift modifier already captured) preserves the exact chord.
        base = "="
    else:
        base = _QT_KEY_NAMES.get(key)
    if base is None:
        return None, (
            f"WheelHouse cannot press the {_qt_key_display(key)} key"
        )
    parts = [
        name for mod, name in _QT_MODIFIER_NAMES
        if mods & _qt_enum_value(mod)
    ]
    parts.append(base)
    return "+".join(parts), None


class _KeyCaptureEdit(QKeySequenceEdit):
    """QKeySequenceEdit variant for the Record flow.

    Plain Escape cancels the capture instead of being recorded, so
    recording never traps the keyboard (Escape WITH modifiers still
    records -- ctrl+esc is a real hotkey). Losing focus also cancels, so
    the row can never be left stuck in recording mode.
    """

    cancel_requested = Signal()
    focus_lost = Signal()

    def keyPressEvent(self, event):
        if (
            event.key() == Qt.Key.Key_Escape
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            event.accept()
            self.cancel_requested.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.focus_lost.emit()


class _RecordButton(QPushButton):
    """QPushButton that also activates on Return/Enter.

    The dialog sets autoDefault False on every button so Enter in a text
    field cannot fire Save; that also disables Enter on a focused button.
    The Record button must be operable by both Space (native) and Enter
    (spec section 13), so it handles Return/Enter itself -- the accepted
    event never reaches the dialog's default-button logic.
    """

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            self.click()
            return
        super().keyPressEvent(event)


class _GoalList(QListWidget):
    """Goal list for the Add flow's opening page.

    QListWidget emits itemActivated for Return/Enter only on some
    platforms/styles; the page is keyboard-first (spec section 13), so
    Return and Enter activate the current item everywhere.
    """

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self.currentItem()
            if item is not None:
                event.accept()
                self.itemActivated.emit(item)
                return
        super().keyPressEvent(event)


def _first_invalid_key_name(keys_text: str):
    """First '+'-separated name in a key-combination field that the Input
    process would reject, or None (wh-pattern-editor-r0.5). Empty segments
    are skipped -- the field-not-empty check is a separate error."""
    for part in keys_text.split("+"):
        name = part.strip().lower()
        if name and name not in VALID_KEY_NAMES:
            return name
    return None


def _group_ref_error(steps, group_count: int):
    """First whole-param g<N> reference beyond ``group_count`` across the
    serialized steps, rendered as the field error, or None
    (wh-pattern-editor-r2.2). Mirrors PatternManager._validate_group_refs
    (same rule, same wording) so Save never bounces off the Logic-side
    rejection without a field-level hint. Embedded references inside
    longer text are not checked, for the same reason the seam skips them:
    the engine replaces those only when the group actually matched."""
    for step in steps:
        for param in step.get("params", []):
            if not isinstance(param, str):
                continue
            ref = re.fullmatch(r"g([1-9][0-9]*)", param)
            if ref is None:
                continue
            n = int(ref.group(1))
            if n > group_count:
                return (
                    f"Step '{step.get('function')}': '{param}' points "
                    f"at capture group {n}, but the expression has "
                    f"only {group_count} capture group(s)"
                )
    return None


def _trailing_digit_key(keys_text: str):
    """The final '+'-separated segment of a keys field when it is all
    digits, else None. The runtime peels the last hk argument as a repeat
    count when it converts to a number (ActionFunctions.hotkey), so a
    trailing digit typed as a key would silently repeat the chord instead
    of pressing the key (wh-hk-trailing-repeat-lie)."""
    segs = [k.strip() for k in keys_text.split("+") if k.strip()]
    if segs and segs[-1].isdigit():
        return segs[-1]
    return None


def _split_hk_params(params):
    """Split raw ``hk`` params into ``(keys, repeat)``, or None when the
    values cannot round-trip through the two catalog fields."""
    keys = list(params)
    repeat = None
    if keys:
        last = keys[-1]
        if isinstance(last, bool):
            return None
        if isinstance(last, int):
            repeat = keys.pop()
        elif isinstance(last, str) and (
            last.isdigit() or _HK_REPEAT_RE.fullmatch(last)
        ):
            repeat = keys.pop()
    if any(not isinstance(k, str) for k in keys):
        return None
    return keys, repeat


class ActionStepRow(QWidget):
    """One ordered action step: function picker + generated param fields.

    The picker offers every catalog entry whose audience is basic or
    advanced (basic first, then a separator; internal functions are never
    listed). Parameter fields are generated from the catalog entry's
    ``params`` specs: ``choice`` renders a fixed combo, ``group_ref`` an
    editable combo offering g1..gN from the current expression's group
    count, everything else a line edit; each field carries the param
    summary as hover help.

    A stored step the fields cannot represent -- an internal or unknown
    function, more params than the catalog declares, or non-string hk keys
    -- degrades to a read-only display and round-trips its params verbatim
    (spec section 14: degrade, never crash).
    """

    changed = Signal()

    def __init__(self, parent=None, group_count: int = 0):
        super().__init__(parent)
        self._group_count = group_count
        self._loading = False
        self._preserved_params = None  # non-None => verbatim round-trip
        self._preserved_display = None
        self._param_widgets = []       # list of (param_spec, widget)
        self._loaded_function = None   # function the stored step carried
        self._step_extras = {}         # step keys beyond function/params

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)

        header = QHBoxLayout()
        self._function_combo = QComboBox()
        self._populate_function_combo()
        self.up_btn = QPushButton("Move up")
        self.up_btn.setAccessibleDescription(
            "Moves this step earlier in the run order"
        )
        self.down_btn = QPushButton("Move down")
        self.down_btn.setAccessibleDescription(
            "Moves this step later in the run order"
        )
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setAccessibleDescription(
            "Removes this step from the pattern"
        )
        for btn in (self.up_btn, self.down_btn, self.remove_btn):
            btn.setAutoDefault(False)
        header.addWidget(self._function_combo, stretch=1)
        header.addWidget(self.up_btn)
        header.addWidget(self.down_btn)
        header.addWidget(self.remove_btn)

        self._summary_label = QLabel()
        self._summary_label.setStyleSheet(_MUTED_STYLE)
        self._summary_label.setWordWrap(True)

        params_container = QWidget()
        self._params_form = QFormLayout(params_container)
        self._params_form.setContentsMargins(12, 0, 0, 0)

        layout.addLayout(header)
        layout.addWidget(self._summary_label)
        layout.addWidget(params_container)

        self._rebuild_params()
        self._function_combo.currentIndexChanged.connect(
            self._on_function_changed
        )

    # -------------------------------------------------------------- #
    #  Public API
    # -------------------------------------------------------------- #

    def function_name(self) -> str:
        data = self._function_combo.currentData()
        return data if isinstance(data, str) else ""

    def step(self) -> dict:
        """Serialize this row back to a raw ``{function, params}`` step."""
        name = self.function_name()
        if self._preserved_params is not None:
            return self._with_extras(
                {"function": name, "params": list(self._preserved_params)}
            )
        params = []
        for i, (spec, _widget) in enumerate(self._param_widgets):
            text = self.param_value(i)
            kind = spec.get("kind")
            if name == "hk" and kind == "keys":
                params.extend(
                    k.strip().lower() for k in text.split("+") if k.strip()
                )
            elif kind == "number":
                value = text.strip()
                if value.isdigit():
                    params.append(int(value))
                elif value:
                    params.append(value)
                # An empty number field is an omitted optional param.
            else:
                params.append(text)
        return self._with_extras({"function": name, "params": params})

    def _with_extras(self, step: dict) -> dict:
        """Re-attach the stored step's extra keys (awaits_done, ...) so an
        edit does not silently strip them. Only while the function is
        unchanged -- extras describe the loaded function's behavior, so a
        different function makes them meaningless
        (wh-pattern-editor-r8.1)."""
        if self._step_extras and step["function"] == self._loaded_function:
            for key, value in self._step_extras.items():
                step[key] = value
        return step

    def set_step(self, step: dict):
        """Load a stored step into the row (prefill / pane sync)."""
        self._loading = True
        try:
            name = step.get("function")
            name = name if isinstance(name, str) else ""
            self._loaded_function = name
            self._step_extras = {
                k: v for k, v in step.items()
                if k not in ("function", "params")
            }
            raw_params = step.get("params")
            # A non-list params value is a hand-edit; list("ctrl") would
            # explode a string into characters, so degrade to empty.
            params = list(raw_params) if isinstance(raw_params, list) else []
            entry = CATALOG_BY_NAME.get(name)
            in_picker = entry is not None and entry["audience"] != "internal"
            self._select_function(name, add_if_missing=not in_picker)
            if not in_picker:
                self._enter_preserved_mode(name, params)
                return
            if name == "hk":
                split = _split_hk_params(params)
                if split is None:
                    self._enter_preserved_mode(name, params)
                    return
                keys, repeat = split
                self._rebuild_params()
                self.set_param_value(0, "+".join(keys))
                self.set_param_value(
                    1, "" if repeat is None else str(repeat),
                )
                return
            specs = entry["params"]
            representable = len(params) <= len(specs) and all(
                isinstance(p, (str, int, float)) and not isinstance(p, bool)
                for p in params
            )
            if not representable:
                self._enter_preserved_mode(name, params)
                return
            self._rebuild_params()
            for i, param in enumerate(params):
                self.set_param_value(i, str(param))
        finally:
            self._loading = False
        self.changed.emit()

    def param_value(self, index: int) -> str:
        _spec, widget = self._param_widgets[index]
        if isinstance(widget, QComboBox):
            return widget.currentText()
        return widget.text()

    def set_param_value(self, index: int, text: str):
        _spec, widget = self._param_widgets[index]
        if isinstance(widget, QComboBox):
            if widget.isEditable():
                widget.setEditText(text)
            else:
                i = widget.findText(text)
                if i < 0:
                    # A stored value outside the catalog choices (hand
                    # edit) is kept selectable rather than dropped.
                    widget.addItem(text)
                    i = widget.count() - 1
                widget.setCurrentIndex(i)
        else:
            widget.setText(text)

    def invalid_key_name(self):
        """First unrecognized key name in this row's key-name fields, or
        None. Covers the hk keys field and any single-key param (press's
        key field, wh-pattern-editor-r8.4). Preserved (read-only) rows
        never flag -- they cannot be edited, so an error would be
        unfixable."""
        if self._preserved_params is not None:
            return None
        if self.function_name() == "hk":
            return _first_invalid_key_name(self.param_value(0))
        for i, (spec, _widget) in enumerate(self._param_widgets):
            if spec.get("kind") == "key":
                bad = _first_invalid_key_name(self.param_value(i))
                if bad is not None:
                    return bad
        return None

    def invalid_repeat_value(self):
        """This row's hk repeat-field text when it is neither digits nor a
        g<N> group reference, or None. Anything else serializes as an
        extra key argument and the runtime presses it as a key instead of
        repeating (wh-pattern-editor-r8.4). Only editable hk rows have
        the repeat field; press's repeat accepts number words by design,
        so it is not checked here."""
        if self.function_name() != "hk" or self._preserved_params is not None:
            return None
        if len(self._param_widgets) < 2:
            return None
        value = self.param_value(1).strip()
        if not value or value.isdigit() or _HK_REPEAT_RE.fullmatch(value):
            return None
        return value

    def trailing_repeat_key(self):
        """Digit segment ending this row's hk keys field while the repeat
        field is empty, or None. With no repeat set, the digit serializes
        last and the runtime peels it as a repeat count instead of
        pressing it (wh-hk-trailing-repeat-lie); a set repeat serializes
        after the keys and shields the digit."""
        if self.function_name() != "hk" or self._preserved_params is not None:
            return None
        if len(self._param_widgets) > 1 and self.param_value(1).strip():
            return None
        return _trailing_digit_key(self.param_value(0))

    def set_group_count(self, count: int):
        """Refresh group_ref combos to offer g1..g<count>."""
        self._group_count = count
        for spec, widget in self._param_widgets:
            if spec.get("kind") == "group_ref" and isinstance(widget, QComboBox):
                current = widget.currentText()
                widget.blockSignals(True)
                widget.clear()
                widget.addItems(self._group_ref_items())
                widget.setEditText(current)
                widget.blockSignals(False)

    def set_position(self, index: int, count: int):
        """Renumber accessible names and gate the reorder/remove buttons."""
        n = index + 1
        self._function_combo.setAccessibleName(f"Step {n} function")
        self.up_btn.setAccessibleName(f"Move step {n} up")
        self.down_btn.setAccessibleName(f"Move step {n} down")
        self.remove_btn.setAccessibleName(f"Remove step {n}")
        self.up_btn.setEnabled(index > 0)
        self.down_btn.setEnabled(index < count - 1)
        self.remove_btn.setEnabled(count > 1)
        self.up_btn.setToolTip(
            "Already the first step" if index == 0
            else "Run this step earlier"
        )
        self.down_btn.setToolTip(
            "Already the last step" if index >= count - 1
            else "Run this step later"
        )
        self.remove_btn.setToolTip(
            "The last step cannot be removed" if count <= 1
            else "Remove this step"
        )

    def focus_widgets(self) -> list:
        """Every focusable control in visual order (tab-order chaining)."""
        out = [
            self._function_combo, self.up_btn, self.down_btn,
            self.remove_btn,
        ]
        out.extend(widget for _spec, widget in self._param_widgets)
        if self._preserved_display is not None:
            out.append(self._preserved_display)
        return out

    # -------------------------------------------------------------- #
    #  Internals
    # -------------------------------------------------------------- #

    def _populate_function_combo(self):
        combo = self._function_combo
        combo.setToolTip("What this step does")
        combo.setAccessibleDescription(
            "Chooses the action this step performs"
        )
        model = combo.model()
        for audience, heading in (
            ("basic", "Basic actions"),
            ("advanced", "Advanced actions"),
        ):
            combo.addItem(heading)
            model.item(combo.count() - 1).setEnabled(False)
            for entry in ACTION_CATALOG:
                if entry["audience"] != audience:
                    continue
                combo.addItem(entry["label"], entry["name"])
                combo.setItemData(
                    combo.count() - 1, entry["summary"],
                    Qt.ItemDataRole.ToolTipRole,
                )
        combo.setCurrentIndex(1)  # first real entry after the heading

    def _select_function(self, name: str, add_if_missing: bool):
        combo = self._function_combo
        index = combo.findData(name)
        if index < 0 and add_if_missing:
            combo.addItem(name, name)
            index = combo.count() - 1
        if index >= 0:
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _on_function_changed(self, _index):
        if self._loading:
            return
        self._rebuild_params()
        self.changed.emit()

    def _clear_params(self):
        while self._params_form.rowCount():
            self._params_form.removeRow(0)
        self._param_widgets = []
        self._preserved_params = None
        self._preserved_display = None

    def _rebuild_params(self):
        self._clear_params()
        entry = CATALOG_BY_NAME.get(self.function_name())
        if entry is None:
            self._summary_label.setText("")
            return
        self._summary_label.setText(entry["summary"])
        for spec in entry["params"]:
            widget = self._create_param_widget(spec)
            label = QLabel(f"{spec['name']}:")
            label.setToolTip(spec.get("summary", ""))
            label.setBuddy(widget)
            self._params_form.addRow(label, widget)
            self._param_widgets.append((spec, widget))

    def _group_ref_items(self):
        return [""] + [f"g{i}" for i in range(1, self._group_count + 1)]

    def _create_param_widget(self, spec):
        kind = spec.get("kind")
        help_text = spec.get("summary", "")
        if kind == "choice":
            widget = QComboBox()
            widget.addItems([str(c) for c in spec.get("choices", [])])
            widget.currentTextChanged.connect(lambda _t: self.changed.emit())
        elif kind == "group_ref":
            widget = QComboBox()
            widget.setEditable(True)
            widget.addItems(self._group_ref_items())
            widget.setEditText("")
            help_text = (
                f"{help_text}\n{_GROUP_REF_HELP}" if help_text
                else _GROUP_REF_HELP
            )
            widget.currentTextChanged.connect(lambda _t: self.changed.emit())
            widget.editTextChanged.connect(lambda _t: self.changed.emit())
        else:
            widget = QLineEdit()
            widget.textChanged.connect(lambda _t: self.changed.emit())
        widget.setToolTip(help_text)
        widget.setAccessibleName(spec.get("name", ""))
        widget.setAccessibleDescription(help_text)
        return widget

    def _enter_preserved_mode(self, name: str, params: list):
        """Show the step read-only and round-trip its params verbatim."""
        self._clear_params()
        self._preserved_params = list(params)
        entry = CATALOG_BY_NAME.get(name)
        self._summary_label.setText(
            entry["summary"] if entry
            else "This function is not in the catalog."
        )
        display = QLineEdit(", ".join(str(p) for p in params))
        display.setReadOnly(True)
        display.setAccessibleName("Preserved parameters")
        display.setAccessibleDescription(
            "Parameters kept exactly as stored; not editable here"
        )
        display.setToolTip(
            "These parameters are kept exactly as stored; they cannot be "
            "edited here."
        )
        self._preserved_display = display
        params_label = QLabel("params:")
        params_label.setBuddy(display)
        self._params_form.addRow(params_label, display)


class ActionStepListEditor(QWidget):
    """Ordered, editable list of action steps (add / remove / reorder).

    Buttons only, keyboard-reachable (spec section 13); the last remaining
    row is not removable so the list never collapses to nothing. Emits
    ``changed`` on any edit.
    """

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._group_count = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self._rows_layout)

        add_btn = QPushButton("Add step")
        self.add_btn = add_btn
        add_btn.setAccessibleName("Add step")
        add_btn.setAccessibleDescription(
            "Adds another action step to run after the steps above"
        )
        add_btn.setToolTip("Add another action to run after the steps above")
        add_btn.setAutoDefault(False)
        add_btn.clicked.connect(lambda: self.add_step())
        add_row = QHBoxLayout()
        add_row.addWidget(add_btn)
        add_row.addStretch()
        outer.addLayout(add_row)

        self._append_row()
        self._refresh_rows()

    # -------------------------------------------------------------- #
    #  Public API
    # -------------------------------------------------------------- #

    def steps(self) -> list:
        return [row.step() for row in self._rows]

    def set_steps(self, steps):
        while self._rows:
            self._remove_row_widget(0)
        for step in steps or []:
            # A hand-edited user file can carry non-table steps in
            # raw_actions; drop them instead of crashing (spec section 14).
            if not isinstance(step, dict):
                continue
            self._append_row().set_step(step)
        if not self._rows:
            self._append_row()
        self._refresh_rows()
        self.changed.emit()

    def add_step(self):
        self._append_row()
        self._refresh_rows()
        self.changed.emit()

    def remove_step(self, index: int):
        if len(self._rows) <= 1 or not 0 <= index < len(self._rows):
            return
        self._remove_row_widget(index)
        self._refresh_rows()
        self.changed.emit()

    def move_step(self, index: int, delta: int):
        target = index + delta
        if not 0 <= index < len(self._rows):
            return
        if not 0 <= target < len(self._rows) or target == index:
            return
        row = self._rows.pop(index)
        self._rows.insert(target, row)
        self._rows_layout.removeWidget(row)
        self._rows_layout.insertWidget(target, row)
        self._refresh_rows()
        self.changed.emit()

    def set_group_count(self, count: int):
        if count == self._group_count:
            return
        self._group_count = count
        for row in self._rows:
            row.set_group_count(count)

    def first_invalid_key_name(self):
        """First key name in any editable hk row's keys field that the
        Input process would reject, or None (wh-pattern-editor-r0.5)."""
        for row in self._rows:
            bad = row.invalid_key_name()
            if bad is not None:
                return bad
        return None

    def first_trailing_repeat_key(self):
        """First unshielded trailing digit in any editable hk row's keys
        field, or None (wh-hk-trailing-repeat-lie)."""
        for row in self._rows:
            seg = row.trailing_repeat_key()
            if seg is not None:
                return seg
        return None

    def first_invalid_repeat_value(self):
        """First hk repeat value in any editable row that is neither
        digits nor a g<N> reference, or None (wh-pattern-editor-r8.4)."""
        for row in self._rows:
            bad = row.invalid_repeat_value()
            if bad is not None:
                return bad
        return None

    def focus_widgets(self) -> list:
        """Every focusable control in visual order (tab-order chaining)."""
        out = []
        for row in self._rows:
            out.extend(row.focus_widgets())
        out.append(self.add_btn)
        return out

    # -------------------------------------------------------------- #
    #  Internals
    # -------------------------------------------------------------- #

    def _append_row(self) -> ActionStepRow:
        row = ActionStepRow(group_count=self._group_count)
        self._rows.append(row)
        self._rows_layout.addWidget(row)
        row.changed.connect(self.changed.emit)
        row.remove_btn.clicked.connect(
            lambda _checked=False, r=row: self._on_remove_clicked(r)
        )
        row.up_btn.clicked.connect(
            lambda _checked=False, r=row: self._on_move_clicked(r, -1)
        )
        row.down_btn.clicked.connect(
            lambda _checked=False, r=row: self._on_move_clicked(r, 1)
        )
        return row

    def _on_remove_clicked(self, row):
        try:
            self.remove_step(self._rows.index(row))
        except ValueError:
            pass

    def _on_move_clicked(self, row, delta):
        try:
            self.move_step(self._rows.index(row), delta)
        except ValueError:
            pass

    def _remove_row_widget(self, index: int):
        row = self._rows.pop(index)
        self._rows_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()

    def _refresh_rows(self):
        count = len(self._rows)
        for i, row in enumerate(self._rows):
            row.set_position(i, count)


class PhraseListEditor(QWidget):
    """Editable list of spoken phrasings: one QLineEdit row per phrase.

    Rows can be added and removed; the last row cannot be removed so the
    pane never collapses to nothing (an empty list is still a validation
    error -- the row is just blank). Emits ``changed`` on any edit.
    """

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []  # list of (row_widget, QLineEdit, QPushButton)
        self._row_placeholder = _DEFAULT_PHRASE_PLACEHOLDER
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self._rows_layout)

        add_btn = QPushButton("Add phrase")
        self.add_btn = add_btn
        add_btn.setAccessibleName("Add phrase")
        add_btn.setAccessibleDescription(
            "Adds another phrase row that triggers the same pattern"
        )
        add_btn.setToolTip("Add another way to say this command")
        add_btn.setAutoDefault(False)
        add_btn.clicked.connect(lambda: self.add_row("", focus=True))
        add_row_layout = QHBoxLayout()
        add_row_layout.addWidget(add_btn)
        add_row_layout.addStretch()
        outer.addLayout(add_row_layout)

        self.add_row("")

    # -------------------------------------------------------------- #
    #  Public API
    # -------------------------------------------------------------- #

    def phrases(self) -> list:
        """Return every row's text, in order (empty rows included so the
        caller's validation can flag them)."""
        return [edit.text() for _w, edit, _b in self._rows]

    def row_edits(self) -> list:
        """The row QLineEdits, in order (focus placement for the Add
        flow's goal templates)."""
        return [edit for _w, edit, _b in self._rows]

    def focus_widgets(self) -> list:
        """Every focusable control in visual order (tab-order chaining)."""
        out = []
        for _w, edit, btn in self._rows:
            out.extend((edit, btn))
        out.append(self.add_btn)
        return out

    def set_row_placeholder(self, text: str):
        """Placeholder shown in every phrase row, current and future
        (goal templates suggest goal-appropriate examples)."""
        self._row_placeholder = text
        for _w, edit, _b in self._rows:
            edit.setPlaceholderText(text)

    def set_phrases(self, phrases):
        """Replace all rows with the given phrases (at least one row)."""
        while self._rows:
            self._remove_row_widget(0)
        for phrase in phrases or [""]:
            self.add_row(phrase)
        self._refresh_rows()
        self.changed.emit()

    def add_row(self, text: str = "", focus: bool = False):
        edit = QLineEdit()
        edit.setText(text)
        edit.setPlaceholderText(self._row_placeholder)
        edit.textChanged.connect(lambda _t: self.changed.emit())

        remove_btn = QPushButton("Remove")
        remove_btn.setAutoDefault(False)
        remove_btn.setMaximumWidth(70)
        remove_btn.setToolTip("Remove this phrasing")

        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(edit, stretch=1)
        row_layout.addWidget(remove_btn)

        row = (row_widget, edit, remove_btn)
        self._rows.append(row)
        remove_btn.clicked.connect(lambda: self._on_remove_clicked(row))
        self._rows_layout.addWidget(row_widget)
        self._refresh_rows()
        if focus:
            edit.setFocus()
        self.changed.emit()

    def remove_row(self, index: int):
        """Remove the index-th row; the last remaining row is kept."""
        if len(self._rows) <= 1:
            return
        self._remove_row_widget(index)
        self._refresh_rows()
        self.changed.emit()

    # -------------------------------------------------------------- #
    #  Internals
    # -------------------------------------------------------------- #

    def _on_remove_clicked(self, row):
        try:
            index = self._rows.index(row)
        except ValueError:
            return
        self.remove_row(index)

    def _remove_row_widget(self, index: int):
        row_widget, _edit, _btn = self._rows.pop(index)
        self._rows_layout.removeWidget(row_widget)
        row_widget.setParent(None)
        row_widget.deleteLater()

    def _refresh_rows(self):
        """Renumber accessible names and gate the remove buttons."""
        single = len(self._rows) == 1
        for i, (_w, edit, btn) in enumerate(self._rows):
            edit.setAccessibleName(f"Phrase {i + 1}")
            edit.setAccessibleDescription(
                "One of the spoken phrasings that triggers this pattern"
            )
            btn.setEnabled(not single)
            btn.setAccessibleName(f"Remove phrase {i + 1}")
            btn.setAccessibleDescription(
                "Removes this phrasing from the list"
            )
            btn.setToolTip(
                "The last phrasing cannot be removed" if single
                else "Remove this phrasing"
            )


class CreatePatternDialog(QDialog):
    """Pattern editor: create or edit a user pattern in simple/advanced mode.

    Args:
        hotword: The active wake word (for the checkbox label).
        parent: Qt parent widget.
        entry: A pm_get_patterns entry dict to prefill from (Edit and, in a
            later bead, Duplicate/Customize). Optional keys are read
            defensively; ``None`` means a blank Add dialog.
        pattern_id: When set, the dialog is in EDIT mode: Save sends
            pm_update_pattern for this id and try-it drafts exclude it so
            the stale self does not shadow the draft. ``entry`` without
            ``pattern_id`` prefills but still saves as a new pattern.

    Signals:
        pattern_action(dict): pm_create_pattern / pm_update_pattern /
            pm_test_draft requests for the Logic process, routed through
            PatternManagerDialog's pattern_action.
    """

    pattern_action = Signal(dict)

    def __init__(self, hotword: str = "x-ray", parent=None,
                 entry: dict = None, pattern_id: str = None):
        super().__init__(parent)
        self._hotword = hotword
        self._entry = dict(entry) if entry else None
        self._pattern_id = pattern_id
        self._edit_mode = pattern_id is not None
        # A shipped positional pattern's position key must survive a
        # Customize, or the user copy binds at the wrong place in the
        # utterance (wh-pattern-editor-r8.5).
        raw_position = (entry or {}).get("position")
        self._entry_position = (
            raw_position if isinstance(raw_position, str) else None
        )
        self.setWindowTitle("Edit Pattern" if self._edit_mode else "New Pattern")
        self.setMinimumWidth(480)

        # ---- Mode/gating state (spec section 8, mode transitions) ----
        # Whether the simple pane holds (or can hold) this pattern's data.
        self._simple_capable = (
            self._entry is None or self._entry_fits_simple(self._entry)
        )
        # expression_touched is False whenever the expression equals what
        # simple mode generated. A stored raw-regex pattern was never
        # generated by simple mode, so it opens touched. The advanced
        # editor bead sets this on user edits via set_expression_touched.
        self._expression_touched = not (
            self._entry is None or self._has_valid_phrases(self._entry)
        )
        if self._simple_capable:
            self._action_count = 1
            self._single_action_basic = True
        else:
            actions = self._entry.get("raw_actions") or []
            self._action_count = len(actions)
            self._single_action_basic = (
                len(actions) == 1 and self._action_fits_simple(actions[0])
            )

        self._mode = "simple" if self._simple_capable else "advanced"
        self._last_try_text = ""
        # Pairs each pm_test_draft with its result so an answer for an
        # older draft/text never renders against newer input
        # (wh-pattern-editor-r6.1).
        self._try_seq = 0
        # Pairs each save with its result the same way, so a stale
        # answer never accepts a newer save's dialog; the timed-out flag
        # distinguishes a late success from a current one
        # (wh-pattern-editor-r8.6).
        self._save_seq = 0
        self._save_timed_out = False
        # True from Save click until the result arrives or the timeout
        # fires (wh-pattern-editor-r0.2). While set, _validate() may not
        # re-enable Save: a field edit during a merely DELAYED create
        # would otherwise arm a second click that double-creates.
        self._save_in_flight = False
        # Guards programmatic expression loads (prefill, pane sync) so only
        # USER edits update expression_touched.
        self._loading_expression = False
        # The "What do you want to happen?" goal page shows on the FIRST
        # show of a fresh Add only (wh-pattern-editor-templates): entry or
        # pattern_id means Edit/Duplicate/Customize, which never see it.
        # Deferring to showEvent (rather than making the page current at
        # construction) keeps programmatic/offscreen construction on the
        # editor page; real dialogs are exec()'d right after construction,
        # so users always meet the goal page first.
        self._goal_page_pending = entry is None and pattern_id is None
        # _fix_tab_order guard: field-change slots fire during construction
        # before every widget the chain names exists.
        self._ui_ready = False

        self._build_ui()
        if self._entry is not None:
            self._prefill(self._entry)
        self._apply_mode()
        self._validate()

    # ------------------------------------------------------------------ #
    #  Stored-data shape checks (mode on open, spec section 6)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _has_valid_phrases(entry: dict) -> bool:
        phrases = entry.get("phrases")
        return (
            isinstance(phrases, list) and bool(phrases)
            and all(isinstance(p, str) for p in phrases)
        )

    @staticmethod
    def _action_fits_simple(action) -> bool:
        """Whether one stored action round-trips through the simple pane."""
        if not isinstance(action, dict):
            return False
        # Extra step keys (awaits_done, ...) have no simple-pane field;
        # the advanced pane's rows round-trip them (wh-pattern-editor-r8.1).
        if any(k not in ("function", "params") for k in action):
            return False
        function = action.get("function")
        params = action.get("params", [])
        if not isinstance(params, list):
            return False
        if function == "hk":
            # A repeat component -- a stored int, a digit string, or a
            # g<N> capture reference; the runtime peels the LAST argument
            # (ActionFunctions.hotkey) -- cannot round-trip through the
            # plain key-combination field. Cramming it in there displays
            # "ctrl+z+g10" and the key-name check then flags a valid
            # saved pattern as un-saveable (wh-pattern-editor-r5.1).
            # Those open in the advanced pane's keys + repeat fields.
            split = _split_hk_params(params)
            return (
                split is not None and bool(split[0]) and split[1] is None
            )
        if function in ("text", "run", "activate"):
            return len(params) == 1 and isinstance(params[0], str)
        return False

    @classmethod
    def _entry_fits_simple(cls, entry: dict) -> bool:
        if not cls._has_valid_phrases(entry):
            return False
        actions = entry.get("raw_actions") or []
        return len(actions) == 1 and cls._action_fits_simple(actions[0])

    # ------------------------------------------------------------------ #
    #  UI Construction
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        # Top-level stack: goal page (fresh Add's opening page) vs the
        # editor. The editor is current at construction; showEvent flips
        # to the goal page on the first show of a fresh Add.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._root_stack = QStackedWidget()
        outer.addWidget(self._root_stack)
        self._goal_page = self._build_goal_page()
        self._editor_page = QWidget()
        layout = QVBoxLayout(self._editor_page)
        self._root_stack.addWidget(self._goal_page)    # index 0
        self._root_stack.addWidget(self._editor_page)  # index 1
        self._root_stack.setCurrentWidget(self._editor_page)

        # --- Mode toggle ---
        toggle_row = QHBoxLayout()
        toggle_row.addStretch()
        self._advanced_toggle = QCheckBox("Advanced")
        self._advanced_toggle.setAccessibleName("Advanced mode")
        self._advanced_toggle.setAccessibleDescription(
            "Switch between the simple editor and the advanced "
            "expression editor"
        )
        self._advanced_toggle.toggled.connect(self._on_mode_toggled)
        toggle_row.addWidget(self._advanced_toggle)
        layout.addLayout(toggle_row)

        # --- Shared controls (visible in BOTH modes; created before the
        # panes because pane slots reference them) ---
        self._build_shared_controls()

        # --- Panes ---
        self._panes = QStackedWidget()
        self._panes.addWidget(self._build_simple_pane())    # index 0
        self._panes.addWidget(self._build_advanced_pane())  # index 1
        layout.addWidget(self._panes)

        # --- Shared assembly: wake word + try-it live OUTSIDE the panes so
        # advanced mode keeps them (spec section 8) ---
        layout.addSpacing(10)
        layout.addWidget(self._hotword_check)
        layout.addSpacing(10)
        try_row = QHBoxLayout()
        try_row.addWidget(self._try_label)
        try_row.addWidget(self._try_input, stretch=1)
        layout.addLayout(try_row)
        layout.addWidget(self._try_result_label)

        # --- Save error (whole-operation failures show inline, not modal) ---
        self._save_error_label = QLabel()
        self._save_error_label.setStyleSheet(_ERROR_STYLE)
        self._save_error_label.setWordWrap(True)
        self._save_error_label.setVisible(False)
        layout.addWidget(self._save_error_label)

        # --- Buttons ---
        btn_layout = QHBoxLayout()
        save_word = "Save" if self._edit_mode else "Create"
        self._save_btn = QPushButton(save_word)
        self._save_btn.setEnabled(False)
        self._save_btn.setAccessibleName(f"{save_word} pattern")
        self._save_btn.setAccessibleDescription(
            "Saves the pattern; Ctrl+Enter does the same while the "
            "fields are valid"
        )
        self._save_btn.setToolTip(
            f"{save_word} this pattern (Ctrl+Enter).\n"
            "Disabled until every field is valid."
        )
        self._save_btn.clicked.connect(self._on_save_clicked)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setAccessibleName("Cancel")
        self._cancel_btn.setAccessibleDescription(
            "Closes the editor without saving"
        )
        self._cancel_btn.setToolTip("Close without saving (Escape).")
        self._cancel_btn.clicked.connect(self.reject)
        # Enter in a field must not fire Save mid-edit (an accidental save
        # overwrites the pattern in edit mode); Escape still cancels. The
        # explicit keyboard confirm is Ctrl+Enter (keyPressEvent below).
        for btn in (self._save_btn, self._cancel_btn):
            btn.setAutoDefault(False)
            btn.setDefault(False)
        btn_layout.addStretch()
        btn_layout.addWidget(self._save_btn)
        btn_layout.addWidget(self._cancel_btn)
        layout.addLayout(btn_layout)

        self._ui_ready = True
        self._fix_tab_order()

    def keyPressEvent(self, event):
        """Keyboard-first behavior (spec section 13).

        Ctrl+Enter is the explicit save (Save keeps autoDefault False so a
        plain Enter mid-edit can never overwrite -- a Stage 4 decision).
        Plain Enter runs the focused widget's obvious local action: the
        try-it line tests at once, a focused button clicks, and any other
        single-line field just moves focus forward. Escape falls through
        to QDialog's reject.
        """
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                event.accept()
                if (
                    self._save_btn.isEnabled()
                    and self._root_stack.currentWidget() is self._editor_page
                ):
                    self._on_save_clicked()
                return
            focus = self.focusWidget()
            if focus is self._try_input:
                event.accept()
                self._try_timer.stop()
                self._send_test_draft()
                return
            if isinstance(focus, QPushButton):
                if focus.isEnabled():
                    event.accept()
                    focus.click()
                    return
            elif isinstance(focus, QLineEdit):
                event.accept()
                self.focusNextChild()
                return
        super().keyPressEvent(event)

    def _fix_tab_order(self):
        """Chain the tab order to match the visual order.

        Needed because the shared controls (wake word, try-it, buttons)
        are CREATED before the panes -- construction order would put them
        first -- and because phrase/step rows come and go at runtime.
        Hidden widgets (the inactive pane, the non-current param page) stay
        in the chain but are skipped by Tab, so one chain serves both
        modes. Re-run on any row add/remove/move.
        """
        if not self._ui_ready:
            return
        order = (
            [self._advanced_toggle]
            + self._phrase_editor.focus_widgets()
            + [
                self._hotkey_radio, self._text_radio, self._run_radio,
                self._activate_radio, self._key_input, self._key_capture,
                self._record_btn, self._text_output, self._run_path,
                self._browse_btn, self._activate_target,
                self._expression_edit, self._regex_link_label,
                self._adv_command_radio, self._adv_replacement_radio,
            ]
            + self._steps_editor.focus_widgets()
            + [
                self._hotword_check, self._try_input, self._save_btn,
                self._cancel_btn,
            ]
        )
        for first, second in zip(order, order[1:]):
            QWidget.setTabOrder(first, second)

    def _build_shared_controls(self):
        """Wake-word checkbox and try-it line, shared by both panes."""
        self._hotword_check = QCheckBox(
            f'Require "{self._hotword}" before command'
        )
        self._hotword_check.setChecked(True)
        self._hotword_check.setAccessibleName("Require wake word")
        self._hotword_check.setAccessibleDescription(
            "When checked, the wake word must be said before this command "
            "responds"
        )
        self._hotword_check.setToolTip(
            "When enabled, you must say the wake word before the phrase.\n"
            "Use for destructive commands (close window) or ambiguous "
            "ones (save)."
        )

        self._try_label = QLabel("Try it:")
        self._try_input = QLineEdit()
        self._try_input.setPlaceholderText("Type what you would say...")
        self._try_input.setAccessibleName("Try a phrase")
        self._try_input.setAccessibleDescription(
            "Tests what saying this phrase would do, without saving"
        )
        self._try_input.setToolTip(
            "Type what you would say; the result below shows what this\n"
            "draft would do. Nothing is saved or run. Enter tests at once."
        )
        self._try_label.setBuddy(self._try_input)
        self._try_input.textChanged.connect(self._on_try_text_changed)

        self._try_result_label = QLabel()
        self._try_result_label.setStyleSheet(_MUTED_STYLE)
        self._try_result_label.setWordWrap(True)

        self._try_timer = QTimer(self)
        self._try_timer.setSingleShot(True)
        self._try_timer.setInterval(TRY_IT_DEBOUNCE_MS)
        self._try_timer.timeout.connect(self._send_test_draft)

        # Save watchdog (wh-pattern-editor-r0.2): started on Save dispatch,
        # stopped by any create/update result; expiry unsticks the dialog.
        self._save_timeout_timer = QTimer(self)
        self._save_timeout_timer.setSingleShot(True)
        self._save_timeout_timer.setInterval(SAVE_RESPONSE_TIMEOUT_MS)
        self._save_timeout_timer.timeout.connect(self._on_save_timeout)

    def _build_simple_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Phrase list ---
        # Stored so goal templates can reword it (the correction template
        # teaches that the phrase is what the microphone hears).
        self._phrases_label = QLabel(_DEFAULT_PHRASES_LABEL)
        phrases_label = self._phrases_label
        self._phrase_editor = PhraseListEditor()
        phrases_label.setBuddy(self._phrase_editor)
        self._phrase_editor.setAccessibleName("Spoken phrasings")
        self._phrase_editor.changed.connect(self._on_fields_changed)

        self._phrase_error_label = QLabel()
        self._phrase_error_label.setStyleSheet(_ERROR_STYLE)
        self._phrase_error_label.setWordWrap(True)
        self._phrase_error_label.setVisible(False)

        # --- Action type ---
        type_label = QLabel("What should happen?")
        self._type_group = QButtonGroup(self)
        self._hotkey_radio = QRadioButton("Press a key combination")
        self._text_radio = QRadioButton("Insert text")
        self._run_radio = QRadioButton("Launch a program")
        self._activate_radio = QRadioButton("Activate a window")
        self._hotkey_radio.setChecked(True)
        type_label.setBuddy(self._hotkey_radio)

        for radio, help_text in (
            (self._hotkey_radio,
             "Presses keys for you, like Ctrl+S, without touching the "
             "keyboard."),
            (self._text_radio,
             "Types text wherever the cursor is."),
            (self._run_radio,
             "Starts a program."),
            (self._activate_radio,
             "Brings a window that is already open to the front."),
        ):
            radio.setAccessibleName(radio.text())
            radio.setAccessibleDescription(help_text)
            radio.setToolTip(help_text)
            self._type_group.addButton(radio)

        self._type_group.buttonClicked.connect(self._on_type_changed)

        # --- Dynamic parameter fields (stacked widget) ---
        self._params_stack = QStackedWidget()
        self._params_stack.addWidget(self._build_hotkey_page())    # index 0
        self._params_stack.addWidget(self._build_text_page())      # index 1
        self._params_stack.addWidget(self._build_run_page())       # index 2
        self._params_stack.addWidget(self._build_activate_page())  # index 3

        self._param_error_label = QLabel()
        self._param_error_label.setStyleSheet(_ERROR_STYLE)
        self._param_error_label.setWordWrap(True)
        self._param_error_label.setVisible(False)

        # --- Assemble ---
        layout.addWidget(phrases_label)
        layout.addWidget(self._phrase_editor)
        layout.addWidget(self._phrase_error_label)
        layout.addSpacing(10)
        layout.addWidget(type_label)
        for radio in [
            self._hotkey_radio,
            self._text_radio,
            self._run_radio,
            self._activate_radio,
        ]:
            layout.addWidget(radio)
        layout.addSpacing(5)
        layout.addWidget(self._params_stack)
        layout.addWidget(self._param_error_label)
        return pane

    def _build_hotkey_page(self) -> QWidget:
        page = QWidget()
        page_layout = QFormLayout(page)
        self._build_key_row(page_layout)
        hotkey_help = QLabel(
            "Separate keys with +. Examples: ctrl+s, alt+f4, ctrl+shift+n. "
            "Or click Record and press the shortcut."
        )
        hotkey_help.setWordWrap(True)
        hotkey_help.setStyleSheet(_MUTED_STYLE)
        page_layout.addRow(hotkey_help)
        return page

    def _build_key_row(self, form_layout: QFormLayout):
        """The key-combination field plus its Record button
        (wh-pattern-editor-record-keys). Record swaps the text field for a
        QKeySequenceEdit capture; the first captured combination is
        converted by qt_key_to_wheelhouse and written back into the text
        field, which stays the source of truth and hand-editable."""
        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText("e.g., ctrl+s or alt+f4")
        self._key_input.setAccessibleName("Key combination")
        self._key_input.setAccessibleDescription(
            "The keys this pattern presses, joined with plus signs"
        )
        self._key_input.setToolTip(
            "The keys to press, separated with +, like ctrl+shift+n."
        )
        self._key_input.textChanged.connect(self._on_fields_changed)
        # Any edit (typed or recorded) supersedes a stale capture error.
        self._key_input.textChanged.connect(
            lambda _t: self._show_record_error(None)
        )

        self._recording_keys = False
        self._key_capture = _KeyCaptureEdit()
        self._key_capture.setVisible(False)
        self._key_capture.setAccessibleName("Key capture")
        self._key_capture.setAccessibleDescription(
            "Press the key combination to record it; Escape cancels"
        )
        self._key_capture.keySequenceChanged.connect(self._on_key_captured)
        self._key_capture.cancel_requested.connect(
            lambda: self._end_key_record(focus_button=True)
        )
        # A focus-out cancel must not steal focus back from wherever the
        # user clicked.
        self._key_capture.focus_lost.connect(
            lambda: self._end_key_record(focus_button=False)
        )

        self._record_btn = _RecordButton("Record")
        self._record_btn.setAutoDefault(False)
        self._record_btn.setAccessibleName("Record key combination")
        self._record_btn.setAccessibleDescription(
            "Records the next key combination you press into the Keys "
            "field"
        )
        self._record_btn.setToolTip(
            "Press the shortcut on your keyboard instead of typing it.\n"
            "Escape cancels the recording."
        )
        self._record_btn.clicked.connect(self._on_record_clicked)

        key_label = QLabel("Keys:")
        key_label.setBuddy(self._key_input)
        key_row = QHBoxLayout()
        key_row.addWidget(self._key_input, stretch=1)
        key_row.addWidget(self._key_capture, stretch=1)
        key_row.addWidget(self._record_btn)
        form_layout.addRow(key_label, key_row)

        self._record_error_label = QLabel()
        self._record_error_label.setStyleSheet(_ERROR_STYLE)
        self._record_error_label.setWordWrap(True)
        self._record_error_label.setVisible(False)
        form_layout.addRow(self._record_error_label)

    # ------------------------------------------------------------------ #
    #  Key recording (wh-pattern-editor-record-keys)
    # ------------------------------------------------------------------ #

    def _on_record_clicked(self):
        if self._recording_keys:
            self._end_key_record(focus_button=True)
        else:
            self._start_key_record()

    def _start_key_record(self):
        self._show_record_error(None)
        self._recording_keys = True
        self._record_btn.setText("Cancel")
        # clear() emits an empty keySequenceChanged; not a capture.
        self._key_capture.blockSignals(True)
        self._key_capture.clear()
        self._key_capture.blockSignals(False)
        self._key_input.setVisible(False)
        self._key_capture.setVisible(True)
        self._key_capture.setFocus()

    def _end_key_record(self, focus_button: bool):
        """Leave recording mode. Idempotent on purpose: hiding the capture
        field drops its focus, which re-enters here via focus_lost."""
        if not self._recording_keys:
            return
        self._recording_keys = False
        self._record_btn.setText("Record")
        self._key_capture.setVisible(False)
        self._key_input.setVisible(True)
        if focus_button:
            self._record_btn.setFocus()

    def _on_key_captured(self, sequence):
        """First complete chord from the capture field: convert and write."""
        if not self._recording_keys or sequence.count() == 0:
            return
        combo = sequence[0]
        name, error = qt_key_to_wheelhouse(
            combo.key(), combo.keyboardModifiers()
        )
        self._end_key_record(focus_button=True)
        if name is None:
            self._show_record_error(error)
            return
        # setText fires textChanged, which clears any record error and
        # revalidates through the normal field path.
        self._key_input.setText(name)

    def _show_record_error(self, message):
        self._record_error_label.setText(message or "")
        self._record_error_label.setVisible(bool(message))

    def _build_text_page(self) -> QWidget:
        page = QWidget()
        page_layout = QFormLayout(page)
        self._text_output = QLineEdit()
        self._text_output.setPlaceholderText(_DEFAULT_TEXT_PLACEHOLDER)
        self._text_output.setAccessibleName("Output text")
        self._text_output.setAccessibleDescription(
            "The text WheelHouse types when you say the phrase"
        )
        self._text_output.textChanged.connect(self._on_fields_changed)
        # Stored so goal templates can reword it (snippet vs correction).
        self._text_output_label = QLabel(_DEFAULT_TEXT_LABEL)
        self._text_output_label.setBuddy(self._text_output)
        page_layout.addRow(self._text_output_label, self._text_output)
        return page

    def _build_run_page(self) -> QWidget:
        page = QWidget()
        page_layout = QHBoxLayout(page)
        self._run_path = QLineEdit()
        self._run_path.setPlaceholderText(
            "e.g., notepad.exe or C:\\path\\to\\app.exe"
        )
        self._run_path.setAccessibleName("Program path")
        self._run_path.setAccessibleDescription(
            "The program this pattern launches: a name on PATH or a full "
            "path"
        )
        self._run_path.textChanged.connect(self._on_fields_changed)
        browse_btn = QPushButton("Browse...")
        self._browse_btn = browse_btn
        browse_btn.setAutoDefault(False)
        browse_btn.setAccessibleName("Browse for program")
        browse_btn.setAccessibleDescription(
            "Opens a file picker to choose the program to launch"
        )
        browse_btn.setToolTip(
            "Pick the program file instead of typing its path."
        )
        browse_btn.clicked.connect(self._browse_program)
        run_label = QLabel("Program:")
        run_label.setBuddy(self._run_path)
        page_layout.addWidget(run_label)
        page_layout.addWidget(self._run_path)
        page_layout.addWidget(browse_btn)
        return page

    def _build_activate_page(self) -> QWidget:
        page = QWidget()
        page_layout = QFormLayout(page)
        self._activate_target = QLineEdit()
        self._activate_target.setPlaceholderText(
            "e.g., brave.exe or WindowsTerminal.exe"
        )
        self._activate_target.setAccessibleName("Window or process")
        self._activate_target.setAccessibleDescription(
            "The window title or process name this pattern brings to the "
            "front"
        )
        self._activate_target.textChanged.connect(self._on_fields_changed)
        activate_label = QLabel("Window/process:")
        activate_label.setBuddy(self._activate_target)
        page_layout.addRow(activate_label, self._activate_target)
        return page

    def _build_advanced_pane(self) -> QWidget:
        """Advanced-mode pane: editable expression with a live local compile
        check, capture-group count, regex101 link, honest pattern-type
        chooser, and the ordered step list (wh-pattern-editor-advanced)."""
        self._advanced_pane = QWidget()
        layout = QVBoxLayout(self._advanced_pane)
        layout.setContentsMargins(0, 0, 0, 0)
        self._advanced_layout = layout

        # --- Expression field ---
        expr_label = QLabel("Regular expression:")
        self._expression_edit = QLineEdit()
        self._expression_edit.setAccessibleName("Regular expression")
        self._expression_edit.setAccessibleDescription(
            "The pattern matched against what you say, as a Python "
            "regular expression"
        )
        self._expression_edit.setToolTip(
            "A Python regular expression matched against what you say.\n"
            "Start with ^ for a command; leave unanchored for a "
            "replacement."
        )
        expr_label.setBuddy(self._expression_edit)
        self._expression_edit.textChanged.connect(self._on_expression_changed)

        self._expression_error_label = QLabel()
        self._expression_error_label.setStyleSheet(_ERROR_STYLE)
        self._expression_error_label.setWordWrap(True)
        self._expression_error_label.setVisible(False)

        info_row = QHBoxLayout()
        self._group_count_label = QLabel("Capture groups: 0")
        self._group_count_label.setStyleSheet(_MUTED_STYLE)
        self._group_count_label.setToolTip(
            "How many capture groups the expression has. Steps can use\n"
            "g1, g2, ... to receive the words each group captured."
        )
        self._regex_link_label = QLabel(
            f'<a href="{REGEX_CHECKER_URL}">Test on regex101.com</a>'
        )
        self._regex_link_label.setOpenExternalLinks(True)
        self._regex_link_label.setAccessibleName("Test on regex101.com")
        self._regex_link_label.setAccessibleDescription(
            "Opens regex101.com preset to the Python flavor to test the "
            "expression"
        )
        self._regex_link_label.setToolTip(
            "Opens an online checker preset to the Python flavor "
            "WheelHouse matches with"
        )
        # Keyboard-first: the link must be Tab-focusable and Enter/Space
        # activatable, not click-only (spec section 13).
        self._regex_link_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        self._regex_link_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        info_row.addWidget(self._group_count_label)
        info_row.addStretch()
        info_row.addWidget(self._regex_link_label)

        # --- Pattern type chooser (honest: validated against the anchor,
        # because the runtime loader decides the kind from '^' alone) ---
        type_label = QLabel("Pattern type:")
        self._adv_type_group = QButtonGroup(self)
        self._adv_command_radio = QRadioButton(
            "Command (matches a whole spoken phrase)"
        )
        self._adv_replacement_radio = QRadioButton(
            "Replacement (rewrites words during dictation)"
        )
        self._adv_command_radio.setChecked(True)
        type_label.setBuddy(self._adv_command_radio)
        for radio, help_text in (
            (self._adv_command_radio,
             "Runs the steps when the whole spoken phrase matches; the "
             "expression must start with ^."),
            (self._adv_replacement_radio,
             "Rewrites matching words while you dictate; the expression "
             "must not start with ^."),
        ):
            radio.setAccessibleName(radio.text())
            radio.setAccessibleDescription(help_text)
            radio.setToolTip(help_text)
            self._adv_type_group.addButton(radio)
        self._adv_type_group.buttonClicked.connect(self._on_adv_type_changed)

        self._type_error_label = QLabel()
        self._type_error_label.setStyleSheet(_ERROR_STYLE)
        self._type_error_label.setWordWrap(True)
        self._type_error_label.setVisible(False)

        # --- Ordered step list ---
        steps_label = QLabel("Steps (run in order):")
        self._steps_editor = ActionStepListEditor()
        steps_label.setBuddy(self._steps_editor)
        self._steps_editor.setAccessibleName("Action steps")
        self._steps_editor.changed.connect(self._on_steps_changed)

        self._steps_error_label = QLabel()
        self._steps_error_label.setStyleSheet(_ERROR_STYLE)
        self._steps_error_label.setWordWrap(True)
        self._steps_error_label.setVisible(False)

        layout.addWidget(expr_label)
        layout.addWidget(self._expression_edit)
        layout.addWidget(self._expression_error_label)
        layout.addLayout(info_row)
        layout.addSpacing(10)
        layout.addWidget(type_label)
        layout.addWidget(self._adv_command_radio)
        layout.addWidget(self._adv_replacement_radio)
        layout.addWidget(self._type_error_label)
        layout.addSpacing(10)
        layout.addWidget(steps_label)
        layout.addWidget(self._steps_editor)
        layout.addWidget(self._steps_error_label)
        layout.addStretch()
        return self._advanced_pane

    # ------------------------------------------------------------------ #
    #  Add-flow goal page (wh-pattern-editor-templates, spec section 11)
    # ------------------------------------------------------------------ #

    def _build_goal_page(self) -> QWidget:
        """The "What do you want to happen?" opening page: a keyboard-first
        goal list (arrow keys move, Enter or a click selects, each entry
        carries one sentence of hover help)."""
        page = QWidget()
        layout = QVBoxLayout(page)

        heading = QLabel("What do you want to happen?")
        self._goal_heading = heading
        # Headings use QFont (like the manager's title label) so they
        # scale with the user's base font, not a hardcoded pixel size.
        heading_font = heading.font()
        heading_font.setPointSize(heading_font.pointSize() + 3)
        heading_font.setBold(True)
        heading.setFont(heading_font)
        heading.setWordWrap(True)

        self._goal_list = _GoalList()
        self._goal_list.setAccessibleName("Pattern goals")
        self._goal_list.setAccessibleDescription(
            "Choose what the new voice command should do; the editor "
            "opens prefilled for that goal"
        )
        for template in _GOAL_TEMPLATES:
            item = QListWidgetItem(template["title"])
            item.setToolTip(template["help"])
            item.setData(Qt.ItemDataRole.UserRole, template["key"])
            self._goal_list.addItem(item)
        self._goal_list.setCurrentRow(0)
        self._goal_list.itemActivated.connect(self._on_goal_activated)
        self._goal_list.itemClicked.connect(self._on_goal_activated)
        heading.setBuddy(self._goal_list)

        hint = QLabel("Use the arrow keys and press Enter, or click a goal.")
        hint.setStyleSheet(_MUTED_STYLE)
        hint.setWordWrap(True)

        # The editor's Cancel button lives on the other stack page, so the
        # goal page carries its own (Escape rejects either way).
        cancel_btn = QPushButton("Cancel")
        self._goal_cancel_btn = cancel_btn
        cancel_btn.setAutoDefault(False)
        cancel_btn.setDefault(False)
        cancel_btn.setAccessibleName("Cancel")
        cancel_btn.setAccessibleDescription(
            "Closes the editor without creating a pattern"
        )
        cancel_btn.setToolTip("Close without creating a pattern (Escape).")
        cancel_btn.clicked.connect(self.reject)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)

        layout.addWidget(heading)
        layout.addWidget(self._goal_list)
        layout.addWidget(hint)
        layout.addLayout(btn_row)
        return page

    def showEvent(self, event):
        super().showEvent(event)
        if self._goal_page_pending:
            self._goal_page_pending = False
            self._root_stack.setCurrentWidget(self._goal_page)
            self._goal_list.setFocus()

    def _on_goal_activated(self, item):
        key = item.data(Qt.ItemDataRole.UserRole)
        template = next(
            (t for t in _GOAL_TEMPLATES if t["key"] == key), None
        )
        if template is not None:
            self._apply_goal_template(template)

    def _apply_goal_template(self, template: dict):
        """Land in simple mode with the goal's action type preselected,
        goal-appropriate wording, and focus in the first empty field. A
        template is a starting point, not a wizard lock: every field stays
        editable and the action type can still be changed."""
        radio = {
            "hotkey": self._hotkey_radio,
            "text": self._text_radio,
            "run": self._run_radio,
            "activate": self._activate_radio,
        }.get(template.get("action"))
        if radio is not None:
            radio.setChecked(True)
        self._phrases_label.setText(
            template.get("phrases_label", _DEFAULT_PHRASES_LABEL)
        )
        self._phrase_editor.set_row_placeholder(
            template.get("phrase_placeholder", _DEFAULT_PHRASE_PLACEHOLDER)
        )
        self._text_output_label.setText(
            template.get("text_label", _DEFAULT_TEXT_LABEL)
        )
        self._text_output.setPlaceholderText(
            template.get("text_placeholder", _DEFAULT_TEXT_PLACEHOLDER)
        )
        self._text_output.setToolTip(template.get("text_tooltip", ""))
        self._on_type_changed()
        self._goal_page_pending = False
        self._root_stack.setCurrentWidget(self._editor_page)
        self._focus_first_empty_field()

    def _focus_first_empty_field(self):
        """Focus the first empty field, top to bottom: phrase rows, then
        the selected action's parameter field (spec section 11)."""
        for edit in self._phrase_editor.row_edits():
            if not edit.text().strip():
                edit.setFocus()
                return
        param = self._simple_param_field()
        if param is not None and not param.text().strip():
            param.setFocus()
            return
        # Everything already filled (programmatic prefill): start at the
        # top anyway so focus is somewhere useful.
        edits = self._phrase_editor.row_edits()
        if edits:
            edits[0].setFocus()

    def _simple_param_field(self):
        if self._hotkey_radio.isChecked():
            return self._key_input
        if self._text_radio.isChecked():
            return self._text_output
        if self._run_radio.isChecked():
            return self._run_path
        if self._activate_radio.isChecked():
            return self._activate_target
        return None

    # ------------------------------------------------------------------ #
    #  Prefill (edit mode)
    # ------------------------------------------------------------------ #

    def _prefill(self, entry: dict):
        self._hotword_check.setChecked(
            bool(entry.get("requires_hotword", False))
        )
        # Load the phrases whenever they are valid, even for an
        # advanced-opened entry: if step edits later make the pattern fit
        # the simple shape again, the simple pane must be populated.
        if self._has_valid_phrases(entry):
            self._phrase_editor.set_phrases(entry.get("phrases", []))

        if self._simple_capable:
            self._load_simple_action((entry.get("raw_actions") or [{}])[0])
            return

        # Advanced-only pattern: prefill the advanced pane. Expression
        # first, so the step rows are created with the right group count.
        raw = entry.get("raw_pattern")
        self._set_expression_text(raw if isinstance(raw, str) else "")
        stored_type = entry.get("type")
        if stored_type in ("command", "replacement"):
            is_command = stored_type == "command"
        else:
            is_command = isinstance(raw, str) and raw.startswith("^")
        (
            self._adv_command_radio if is_command
            else self._adv_replacement_radio
        ).setChecked(True)
        actions = entry.get("raw_actions") or []
        if actions:
            self._steps_editor.set_steps(actions)

    def _load_simple_action(self, action: dict):
        """Load one basic-shaped action into the simple pane's fields."""
        function = action.get("function")
        params = action.get("params", [])
        if function == "hk":
            self._hotkey_radio.setChecked(True)
            self._key_input.setText("+".join(params))
        elif function == "text":
            self._text_radio.setChecked(True)
            self._text_output.setText(params[0] if params else "")
        elif function == "run":
            self._run_radio.setChecked(True)
            self._run_path.setText(params[0] if params else "")
        elif function == "activate":
            self._activate_radio.setChecked(True)
            self._activate_target.setText(params[0] if params else "")
        self._on_type_changed()

    # ------------------------------------------------------------------ #
    #  Mode switching and gating (spec section 8)
    # ------------------------------------------------------------------ #

    @property
    def mode(self) -> str:
        """'simple' or 'advanced'."""
        return self._mode

    @property
    def expression_touched(self) -> bool:
        """False whenever the expression equals what simple mode generated."""
        return self._expression_touched

    def set_expression_touched(self, touched: bool):
        """Gating seam for wh-pattern-editor-advanced: the editable
        expression field sets this on user edits (and clears it when the
        text returns to the generated expression)."""
        self._expression_touched = bool(touched)
        self._update_mode_gate()

    @property
    def generated_expression(self) -> str:
        """What simple mode generates from the current phrase list, or ''
        when the phrase list does not validate."""
        phrases = self._phrase_editor.phrases()
        if validate_phrases(phrases) is not None:
            return ""
        return generate_expression(phrases, self._pattern_type())

    def _advanced_to_simple_block_reason(self):
        """Why the advanced->simple toggle is disabled, or None if allowed."""
        if self._expression_touched:
            return (
                "The expression was edited by hand; simple mode cannot "
                "represent it"
            )
        if self._action_count != 1:
            return "Simple mode supports exactly one action"
        if not self._single_action_basic:
            return (
                "Simple mode supports only the four basic action types"
            )
        return None

    def _on_mode_toggled(self, checked: bool):
        previous = self._mode
        self._mode = "advanced" if checked else "simple"
        if self._mode != previous:
            # Keep the panes in sync so no edit is silently dropped:
            # entering advanced rebuilds it from the simple fields; the
            # gate guarantees the way back is lossless (one basic step,
            # untouched expression).
            if self._mode == "advanced":
                self._sync_simple_to_advanced()
            else:
                self._sync_advanced_to_simple()
        self._apply_mode()

    def _apply_mode(self):
        advanced = self._mode == "advanced"
        # Guard against signal recursion: setChecked fires toggled only on
        # an actual change.
        if self._advanced_toggle.isChecked() != advanced:
            self._advanced_toggle.setChecked(advanced)
        self._panes.setCurrentIndex(1 if advanced else 0)
        # Re-apply the active pane's hotword-enablement rule (each pane's
        # type control owns the shared checkbox while its pane is current).
        if advanced:
            self._on_adv_type_changed()
        else:
            self._on_type_changed()
        self._update_mode_gate()

    def _sync_simple_to_advanced(self):
        self._set_expression_text(self.generated_expression)
        is_command = self._pattern_type() == "command"
        (
            self._adv_command_radio if is_command
            else self._adv_replacement_radio
        ).setChecked(True)
        self._steps_editor.set_steps([self._simple_action_step()])

    def _sync_advanced_to_simple(self):
        steps = self._steps_editor.steps()
        if steps and self._action_fits_simple(steps[0]):
            self._load_simple_action(steps[0])

    def _simple_action_step(self) -> dict:
        """The simple pane's action as one raw step (the generate_actions
        mapping, inlined so the GUI process needs no PatternManager)."""
        if self._hotkey_radio.isChecked():
            keys = [
                k.strip().lower()
                for k in self._key_input.text().split("+")
                if k.strip()
            ]
            return {"function": "hk", "params": keys}
        if self._text_radio.isChecked():
            return {"function": "text", "params": [self._text_output.text()]}
        if self._run_radio.isChecked():
            return {"function": "run", "params": [self._run_path.text().strip()]}
        return {
            "function": "activate",
            "params": [self._activate_target.text().strip()],
        }

    def _set_expression_text(self, text: str):
        """Set the expression field programmatically (does not count as a
        user edit for expression_touched)."""
        self._loading_expression = True
        try:
            self._expression_edit.setText(text)
        finally:
            self._loading_expression = False

    def _update_mode_gate(self):
        if self._mode == "advanced":
            reason = self._advanced_to_simple_block_reason()
            self._advanced_toggle.setEnabled(reason is None)
            self._advanced_toggle.setToolTip(
                reason or "Switch back to the simple editor"
            )
        else:
            # Simple -> advanced is always allowed.
            self._advanced_toggle.setEnabled(True)
            self._advanced_toggle.setToolTip(
                "Show the generated regular expression"
            )

    # ------------------------------------------------------------------ #
    #  Slots
    # ------------------------------------------------------------------ #

    def _on_expression_changed(self, text: str):
        if not self._loading_expression:
            # Touched whenever the text differs from what simple mode
            # would generate; typing it back clears the flag.
            self.set_expression_touched(text != self.generated_expression)
        self._on_advanced_changed()

    def _on_adv_type_changed(self, *_args):
        # Replacements fire mid-dictation; gating them behind the wake
        # word is incoherent (mirror of the simple-mode text action rule).
        if self._adv_replacement_radio.isChecked():
            self._hotword_check.setEnabled(False)
            self._hotword_check.setChecked(False)
        else:
            self._hotword_check.setEnabled(True)
        self._on_advanced_changed()

    def _on_steps_changed(self):
        steps = self._steps_editor.steps()
        self._action_count = len(steps)
        self._single_action_basic = (
            len(steps) == 1 and self._action_fits_simple(steps[0])
        )
        self._update_mode_gate()
        self._on_advanced_changed()

    def _on_advanced_changed(self):
        """Any advanced-pane field changed: refresh the group offers, the
        count display, tab order (step rows come and go), validation, and
        the try-it test."""
        count = self._advanced_group_count()
        self._steps_editor.set_group_count(count)
        self._group_count_label.setText(f"Capture groups: {count}")
        self._fix_tab_order()
        self._validate()
        if self._try_input.text().strip():
            self._try_timer.start()

    def _advanced_group_count(self) -> int:
        try:
            return re.compile(self._expression_edit.text()).groups
        except re.error:
            return 0

    def _on_type_changed(self):
        """Switch parameter fields and hotword availability."""
        if self._hotkey_radio.isChecked():
            self._params_stack.setCurrentIndex(0)
            self._hotword_check.setEnabled(True)
        elif self._text_radio.isChecked():
            self._params_stack.setCurrentIndex(1)
            self._hotword_check.setEnabled(False)
            self._hotword_check.setChecked(False)
        elif self._run_radio.isChecked():
            self._params_stack.setCurrentIndex(2)
            self._hotword_check.setEnabled(True)
        elif self._activate_radio.isChecked():
            self._params_stack.setCurrentIndex(3)
            self._hotword_check.setEnabled(True)
        self._on_fields_changed()

    def _on_fields_changed(self, *_args):
        """Any draft field changed: refresh tab order (phrase rows come
        and go), revalidate, and refresh the try-it test."""
        self._fix_tab_order()
        self._validate()
        if self._try_input.text().strip():
            self._try_timer.start()

    def _validate(self):
        """Field-level validation gates the Save button (spec section 14)."""
        if self._mode == "advanced":
            self._validate_advanced()
            return

        phrase_error = validate_phrases(self._phrase_editor.phrases())
        self._phrase_error_label.setText(phrase_error or "")
        self._phrase_error_label.setVisible(phrase_error is not None)

        param_error = self._param_error()
        self._param_error_label.setText(param_error or "")
        self._param_error_label.setVisible(param_error is not None)

        self._save_btn.setEnabled(
            phrase_error is None and param_error is None
            and not self._save_in_flight
        )

    def _param_error(self):
        if self._hotkey_radio.isChecked():
            if not self._key_input.text().strip():
                return "Enter the keys to press (e.g. ctrl+s)"
            # A name the Input process's VK_CODE_MAP does not hold would
            # abort the whole chord silently at runtime -- a pattern that
            # lies (wh-pattern-editor-r0.5).
            bad = _first_invalid_key_name(self._key_input.text())
            if bad is not None:
                return f"Unknown key name: '{bad}'"
            # The runtime peels the LAST hk argument as a repeat count
            # when it converts to a number: 'ctrl+3' presses ctrl three
            # times, not the chord (wh-hk-trailing-repeat-lie).
            seg = _trailing_digit_key(self._key_input.text())
            if seg is not None:
                return (
                    f"A number at the end ('{seg}') is read as a repeat "
                    f"count, not a key press. Use Advanced mode's repeat "
                    f"field to repeat the keys."
                )
        elif self._text_radio.isChecked():
            if not self._text_output.text().strip():
                return "Enter the text to insert"
        elif self._run_radio.isChecked():
            if not self._run_path.text().strip():
                return "Enter the program to launch"
        elif self._activate_radio.isChecked():
            if not self._activate_target.text().strip():
                return "Enter the window or process to activate"
        return None

    def _validate_advanced(self):
        expr_error = self._advanced_expression_error()
        self._expression_error_label.setText(expr_error or "")
        self._expression_error_label.setVisible(expr_error is not None)

        # The anchoring check only makes sense once the expression exists.
        type_error = None if expr_error else self._advanced_type_error()
        self._type_error_label.setText(type_error or "")
        self._type_error_label.setVisible(type_error is not None)

        if not self._steps_editor.steps():
            steps_error = "At least one action step is required"
        else:
            # hk steps' hand-editable keys fields get the same key-name
            # check as the simple pane (wh-pattern-editor-r0.5).
            bad_key = self._steps_editor.first_invalid_key_name()
            steps_error = (
                None if bad_key is None
                else f"Unknown key name: '{bad_key}'"
            )
            if steps_error is None:
                seg = self._steps_editor.first_trailing_repeat_key()
                if seg is not None:
                    steps_error = (
                        f"A number at the end of the keys ('{seg}') is "
                        f"read as a repeat count, not a key press. Put it "
                        f"in the repeat field instead."
                    )
            if steps_error is None:
                # A repeat that is neither digits nor g<N> serializes as
                # an extra key argument and gets pressed as a key
                # (wh-pattern-editor-r8.4).
                bad_repeat = self._steps_editor.first_invalid_repeat_value()
                if bad_repeat is not None:
                    steps_error = (
                        f"Repeat must be a number or a group reference "
                        f"like g1, not '{bad_repeat}'"
                    )
            if steps_error is None and expr_error is None:
                # Group-ref range check only once the expression compiles;
                # with a broken expression the count would read 0 and this
                # error would pile on top of the expression error
                # (wh-pattern-editor-r2.2).
                steps_error = _group_ref_error(
                    self._steps_editor.steps(),
                    self._advanced_group_count(),
                )
        self._steps_error_label.setText(steps_error or "")
        self._steps_error_label.setVisible(steps_error is not None)

        self._save_btn.setEnabled(
            expr_error is None and type_error is None and steps_error is None
            and not self._save_in_flight
        )

    def _advanced_expression_error(self):
        """Local live compile check (same Python engine as the runtime)."""
        text = self._expression_edit.text()
        if not text.strip():
            return "Enter a regular expression"
        try:
            re.compile(text)
        except re.error as exc:
            return f"Expression does not compile: {exc}"
        return None

    def _advanced_type_error(self):
        """Honest type chooser: the runtime loader decides command vs
        replacement from the '^' anchor alone, so a contradictory choice is
        a field error -- the user's regex is never silently rewritten."""
        expr = self._expression_edit.text()
        if self._adv_command_radio.isChecked() and not expr.startswith("^"):
            return (
                "A command must start with '^' -- WheelHouse treats "
                "unanchored expressions as replacements"
            )
        if self._adv_replacement_radio.isChecked() and expr.startswith("^"):
            return (
                "A replacement must not start with '^' -- WheelHouse "
                "treats '^'-anchored expressions as commands"
            )
        return None

    def _browse_program(self):
        """Open a file picker for selecting an executable."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Program", "", "Executables (*.exe);;All Files (*)"
        )
        if path:
            self._run_path.setText(path)

    # ------------------------------------------------------------------ #
    #  Try-it line
    # ------------------------------------------------------------------ #

    def _on_try_text_changed(self, text: str):
        if not text.strip():
            self._try_timer.stop()
            # Clearing is a deliberate reset: advance the request counter
            # so an answer still in flight counts as out of date and
            # cannot reappear in the cleared box (wh-pattern-editor-r7.1).
            self._try_seq += 1
            self._set_try_result("", _MUTED_STYLE)
            return
        self._try_timer.start()

    def _send_test_draft(self):
        """Send the current draft + try text to pm_test_draft (debounced)."""
        text = self._try_input.text().strip()
        if not text:
            return
        self._last_try_text = text
        self._try_seq += 1
        draft = self.get_pattern_data()
        if self._edit_mode:
            # The stale self must not shadow its own replacement.
            draft["exclude_pattern_id"] = self._pattern_id
        self.pattern_action.emit({
            "action": "pm_test_draft",
            "data": {
                "draft": draft, "text": text, "request_id": self._try_seq,
            },
        })

    def _render_try_result(self, data: dict):
        # An answer for an older request must not render against newer
        # input: the echoed request_id pairs the result with its send,
        # and an out-of-date one is dropped -- the request in flight for
        # the current input renders when it lands (wh-pattern-editor-r6.1).
        # An id-less result (handler failure envelope, older Logic
        # process) keeps the tolerant legacy path.
        request_id = data.get("request_id")
        if request_id is not None and request_id != self._try_seq:
            return
        # A handler failure or timeout arrives as {"success": False,
        # "error": ...}; it must never render as the affirmative "No
        # pattern matches" answer (wh-pattern-editor-r0.3).
        if not data.get("success", True):
            self._set_try_result(
                data.get("error", "Draft test failed"), _ERROR_STYLE
            )
            return
        # An id match proves the result answered the last-sent text, so
        # show that; without an id, fall back to the box text.
        if request_id is not None:
            shown = self._last_try_text
        else:
            shown = self._try_input.text().strip() or self._last_try_text
        draft_error = data.get("draft_error")
        if draft_error:
            self._set_try_result(draft_error, _ERROR_STYLE)
            return
        winner = data.get("winner")
        if winner == "draft":
            lines = [f"Saying '{shown}' will run this pattern"]
            if self._mode == "advanced":
                # Advanced preview: what each group captured and the
                # resolved step list (spec section 8).
                for i, group in enumerate(data.get("groups") or [], start=1):
                    lines.append(
                        f"g{i} captured: '{group}'" if group is not None
                        else f"g{i} captured nothing"
                    )
                steps = data.get("resolved_steps") or []
                if steps:
                    rendered = "; ".join(
                        "{}({})".format(
                            step.get("function"),
                            ", ".join(
                                str(p) for p in step.get("params", [])
                            ),
                        )
                        for step in steps
                    )
                    lines.append(f"Steps: {rendered}")
            self._set_try_result("\n".join(lines), _OK_STYLE)
        elif winner == "existing":
            shadowed_by = data.get("shadowed_by") or {}
            name = shadowed_by.get("trigger_display") or "another pattern"
            self._set_try_result(
                f"The pattern '{name}' responds first -- this pattern "
                f"will not run for that phrase",
                _ERROR_STYLE,
            )
        else:
            self._set_try_result(
                f"No pattern matches '{shown}'",
                _MUTED_STYLE,
            )

    def _set_try_result(self, text: str, style: str):
        self._try_result_label.setStyleSheet(style)
        self._try_result_label.setText(text)

    # ------------------------------------------------------------------ #
    #  Save paths and result handling
    # ------------------------------------------------------------------ #

    def _on_save_clicked(self):
        self._save_error_label.setVisible(False)
        self._save_error_label.setText("")
        self._save_seq += 1
        self._save_timed_out = False
        data = self.get_pattern_data()
        if self._edit_mode:
            message = {
                "action": "pm_update_pattern",
                "data": {
                    "pattern_id": self._pattern_id,
                    "data": data,
                    "request_id": self._save_seq,
                },
            }
        else:
            data["request_id"] = self._save_seq
            message = {"action": "pm_create_pattern", "data": data}
        # Disabled until the result arrives or the watchdog fires; a
        # failure re-enables it (wh-pattern-editor-r0.2).
        self._save_in_flight = True
        self._save_btn.setEnabled(False)
        self._save_timeout_timer.start()
        self.pattern_action.emit(message)

    def _on_save_timeout(self):
        """No create/update result within the timeout: unstick the dialog
        (wh-pattern-editor-r0.2). Save re-enables from field state and the
        inline error says what happened; a late result is still handled
        normally by handle_response."""
        self._save_timeout_timer.stop()
        self._save_in_flight = False
        self._save_timed_out = True
        self._save_error_label.setText(
            "WheelHouse did not respond. Try again."
        )
        self._save_error_label.setVisible(True)
        self._validate()

    def handle_response(self, message: dict):
        """Handle a forwarded IPC response from the Logic process.

        PatternManagerDialog forwards pm_create_result, pm_update_result,
        and pm_test_draft_result here while this dialog is open.
        """
        action = message.get("action")
        data = message.get("data", {})

        if action in ("pm_create_result", "pm_update_result"):
            # An answer carrying an id from an EARLIER save is dropped:
            # the save now in flight gets its own answer, and accepting
            # on the old one would close the dialog on unsaved input
            # (wh-pattern-editor-r8.6). Id-less results (older Logic)
            # keep the legacy always-handle path.
            request_id = data.get("request_id")
            if request_id is not None and request_id != self._save_seq:
                return
            timed_out = self._save_timed_out
            self._save_timed_out = False
            # The save is no longer in flight, however it went.
            self._save_timeout_timer.stop()
            self._save_in_flight = False
            if data.get("success"):
                if request_id is not None and timed_out:
                    # The save landed AFTER the timeout told the user to
                    # try again; they may have edited since, so closing
                    # now would discard that input silently.
                    self._save_error_label.setText(
                        "Saved a moment ago -- review the fields and "
                        "click Save again if you changed anything."
                    )
                    self._save_error_label.setVisible(True)
                    self._validate()
                else:
                    self.accept()
            else:
                # Verbatim error, inline (spec section 14) -- including the
                # stale-id "changed underneath you" message from update.
                self._save_error_label.setText(
                    data.get("error", "Unknown error")
                )
                self._save_error_label.setVisible(True)
                self._validate()

        elif action == "pm_test_draft_result":
            self._render_try_result(data)

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def _pattern_type(self) -> str:
        # Simple mode infers the pattern type from the action type, as the
        # old dialog did (spec section 8): text replaces words mid-dictation,
        # everything else is a command.
        return "replacement" if self._text_radio.isChecked() else "command"

    def get_pattern_data(self) -> dict:
        """Return the create-shaped data dict for the current fields.

        Simple mode sends ``phrases`` and NO ``trigger`` key: the phrase
        list replaces the trigger field (wh-pattern-editor-dialog).
        Advanced mode sends the raw ``expression`` + raw ``actions`` steps
        and NO phrases/action_type keys, so the saved block reopens in
        advanced mode (wh-pattern-editor-advanced, spec section 6).
        """
        if self._mode == "advanced":
            data = {
                "pattern_type": (
                    "replacement"
                    if self._adv_replacement_radio.isChecked()
                    else "command"
                ),
                "expression": self._expression_edit.text(),
                "actions": self._steps_editor.steps(),
                "requires_hotword": self._hotword_check.isChecked(),
            }
            if self._entry_position is not None:
                data["position"] = self._entry_position
            return data

        if self._hotkey_radio.isChecked():
            action_type = "hotkey"
            keys = [
                k.strip().lower()
                for k in self._key_input.text().split("+")
                if k.strip()
            ]
            action_params = {"keys": keys}
        elif self._text_radio.isChecked():
            action_type = "text"
            action_params = {"output": self._text_output.text()}
        elif self._run_radio.isChecked():
            action_type = "run"
            action_params = {"path": self._run_path.text().strip()}
        elif self._activate_radio.isChecked():
            action_type = "activate"
            action_params = {"target": self._activate_target.text().strip()}
        else:
            action_type = "hotkey"
            action_params = {"keys": []}

        data = {
            "pattern_type": self._pattern_type(),
            "action_type": action_type,
            "action_params": action_params,
            "requires_hotword": self._hotword_check.isChecked(),
            "phrases": [
                " ".join(p.split()) for p in self._phrase_editor.phrases()
            ],
        }
        if self._entry_position is not None:
            data["position"] = self._entry_position
        return data
