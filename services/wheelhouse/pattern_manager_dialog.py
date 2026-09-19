"""Pattern Manager dialog -- browse, inspect, and manage voice command patterns.

Launched from the GUI process system tray menu. Shows a browsable tree of all
voice patterns (commands and replacements) grouped by category, with a detail
panel for the selected pattern.  User-created patterns can be deleted; system
patterns are read-only.

IPC contract:
    - Emits ``pattern_action(dict)`` to send commands to the Logic process.
    - Receives responses via ``handle_response(dict)`` called by the GUI manager.
"""

import json
import logging

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
    QGroupBox,
    QTextEdit,
    QMessageBox,
    QSizePolicy,
    QFrame,
)
from PySide6.QtCore import Qt, QTimer, QRect, QSize, Signal
from PySide6.QtGui import QFont, QGuiApplication, QKeySequence, QShortcut

from speech.pattern_explainer import explain_pattern, pattern_kind

logger = logging.getLogger(__name__)

# Debounce for the try-it box: restart on every keystroke, send when the
# user pauses (same interval as the editor's try-it line).
TRY_IT_DEBOUNCE_MS = 400

# How long a freshly shown, still-empty manager waits for pm_patterns_data
# before explaining the blank tree (wh-pattern-editor-r0.2). Matches the
# editor's save watchdog.
LOAD_RESPONSE_TIMEOUT_MS = 5000

# No font-size here on purpose (wh-pattern-font-size): these status
# labels sit inside the tree and try-it panes the zoom controls cover,
# so their size must track the dialog's QFont cascade instead of a
# pixel value pinned in the stylesheet, which would override it.
# Hover help for an override the merge kept but could not place
# (wh-pattern-override-doc-id A4). One wording for the list mark and
# the detail badge: they name the same state, and the fix is the same.
_UNRESOLVED_OVERRIDE_TIP = (
    "[unresolved override] -- saved before patterns had names, and "
    "more than one built-in uses this expression, so WheelHouse "
    "cannot tell which one you meant. It still runs, but it no longer "
    "replaces a built-in. Add doc_id to the entry in "
    "user_patterns.toml to say which."
)


def _other_copies(count: int) -> tuple[str, str]:
    """The subject phrase and matching verb for ``count`` other copies.

    Removing one customization deletes one saved rule, and every other rule
    replacing the same built-in stays. So the window may promise the built-in
    comes back only when this is the last copy, and it has to say what remains
    when it is not (wh-pattern-override-doc-id.3.6).

    Two forms only, singular and plural. Never "customization(s)": a reader
    has to work that out, and the count is already known here.
    """
    if count == 1:
        return "One other customization", "replaces"
    return f"{count} other customizations", "replace"


_ERROR_STYLE = "color: #dc2626;"
_OK_STYLE = "color: #15803d;"
_MUTED_STYLE = "color: gray;"

# Widgets the font cascade cannot reach (wh-pattern-manager-improve.1.1).
# Qt resolves a styled widget's font ONCE, when the style sheet is
# applied, and pins the result; a later ``setFont`` on an ancestor never
# lands, and clearing the style sheet does not release it. Measured on
# PySide6 6.11: with the dialog at 9pt, ``self.setFont(24)`` leaves every
# widget below at 9pt while its unstyled siblings move to 24. So each one
# gets the zoom size applied to it directly in ``_apply_font_size``. Two
# rules make that stick:
#   - none of their style sheets may name a ``font-size`` -- a style-sheet
#     size beats the widget's own font, which is why _placeholder_label's
#     rule is colour-only now (the three badge pills keep their 11px on
#     purpose; see the crewcut note in _build_detail_panel);
#   - the explicit font survives the later ``setStyleSheet`` calls
#     _set_try_result / _show_load_error / _show_banner make, so those
#     paths need no re-application.
_STYLED_WIDGET_ATTRS = (
    "_placeholder_label",
    "_hotword_value",
    "_tree_empty_label",
    "_try_result_label",
    "_hotword_error_label",
    "_advanced_toggle",
    "_delete_btn",
    # No build-time style sheet, so it follows the cascade until the
    # first banner is shown and freezes from then on. Listed for the
    # same treatment rather than left as a latent second case.
    "_banner_label",
)

# The reusable top banner's two looks (wh-pattern-editor-r0.7/r0.8 GUI
# side): error for a corrupt user patterns file, warning for a save whose
# file write landed but whose live pattern reload failed.
_BANNER_ERROR_STYLE = (
    "background-color: #fee2e2; color: #991b1b; "
    "border: 1px solid #dc2626; border-radius: 3px; padding: 6px;"
)
_BANNER_WARNING_STYLE = (
    "background-color: #fef3c7; color: #92400e; "
    "border: 1px solid #f59e0b; border-radius: 3px; padding: 6px;"
)

# Preferred / minimum dialog size (wh-pattern-window-scaling). These are
# the SAME numbers the dialog always used; they are no longer applied
# unconditionally -- _clamp_dialog_size bounds them to the current
# screen's availableGeometry before __init__ applies them, so the window
# never opens larger than the screen it appears on.
_PREFERRED_DIALOG_SIZE = QSize(900, 560)
_MIN_DIALOG_SIZE = QSize(800, 500)

# Font-size zoom (wh-pattern-font-size): Ctrl+=/-/0 scale every pane's
# text together. Bounds keep the UI legible at both ends -- 7pt is the
# smallest that stays readable on a standard-DPI display, 24pt is large
# enough for low-vision use without the fixed-width detail-row labels
# wrapping onto each other.
_FONT_SIZE_MIN = 7
_FONT_SIZE_MAX = 24
_FONT_SIZE_STEP = 1
# The trigger title keeps its own point size, offset above the base, so
# it still reads as a title as the base size changes.
_TITLE_FONT_SIZE_OFFSET = 4

# Size caps that used to be fixed pixel values sized for the default font
# (wh-pattern-manager-improve.1.4). They are floors now, not caps:
# _apply_font_size raises each button's cap to whatever its label needs at
# the current size (a QPushButton does not elide -- an undersized cap just
# cuts the text off), and scales each read-only box's height with the
# zoom so a 24pt line does not leave a one-line box.
_CHANGE_HOTWORD_BTN_BASE_MAX_WIDTH = 90
_HELP_BTN_BASE_MAX_WIDTH = 60
_EXPLAIN_BASE_MAX_HEIGHT = 120
_RAW_REGEX_BASE_MAX_HEIGHT = 60
_RAW_ACTIONS_BASE_MAX_HEIGHT = 100


def _clamp_dialog_size(
    preferred: QSize, minimum: QSize, available: QRect
) -> tuple[QSize, QSize]:
    """Bound ``preferred``/``minimum`` to a screen's available area.

    ``available`` is a ``QScreen.availableGeometry()`` rect -- already in
    Qt LOGICAL pixels (DPI-adjusted by Qt itself), so no separate DPI
    math belongs here; multiplying by a device-pixel-ratio would double
    count the scaling Qt already applied. Returns ``(minimum, preferred)``
    each clamped so neither dimension exceeds the available width/height,
    and the clamped preferred size is never smaller than the clamped
    minimum (so a screen smaller than the minimum still gets a usable,
    internally consistent pair rather than an inverted one).
    """
    avail_w = max(available.width(), 1)
    avail_h = max(available.height(), 1)

    min_w = min(minimum.width(), avail_w)
    min_h = min(minimum.height(), avail_h)

    pref_w = min(preferred.width(), avail_w)
    pref_h = min(preferred.height(), avail_h)
    # Never let the clamped preferred size fall below the clamped
    # minimum -- both are already <= avail_w/avail_h above, so raising
    # pref to min here cannot push it back over the screen.
    pref_w = max(pref_w, min_w)
    pref_h = max(pref_h, min_h)

    return QSize(min_w, min_h), QSize(pref_w, pref_h)


class PatternManagerDialog(QDialog):
    """Main Pattern Manager window.

    Signals:
        pattern_action(dict): Emitted to request an action from the Logic
            process (e.g. get patterns, create, delete).
    """

    # Signal emitted when we need to send a command to Logic process
    pattern_action = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pattern Manager")
        # Standard title-bar minimize/maximize buttons (wh-pattern-window-
        # buttons) on top of QDialog's default flags, so this window
        # behaves like a normal resizable window instead of a fixed
        # dialog. wh-pattern-window-voice (voice minimize/maximize)
        # depends on these existing.
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self._apply_screen_bounded_size()
        # Prevent Qt from quitting the app when this dialog closes
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)

        # Font-size zoom baseline (wh-pattern-font-size): captured before
        # anything changes the dialog's font, so Ctrl+0 has a real
        # starting point to return to.
        self._default_font_point_size = self.font().pointSize()
        if self._default_font_point_size <= 0:
            # crewcut: on a platform that resolves the default Qt font by
            # pixel size rather than point size, pointSize() can report
            # -1. Fall back to a conservative default instead of letting
            # every zoom computation clamp against a meaningless value;
            # revisit if a real report surfaces a platform where 9pt is
            # visibly wrong.
            self._default_font_point_size = 9
        self._current_font_point_size = self._default_font_point_size

        # Stored state
        self._hotword = "x-ray"
        self._selected_pattern = None  # dict for currently selected pattern
        self._advanced_visible = False
        self._last_try_text = ""
        # Pairs each pm_test_phrase with its result so an answer for an
        # older phrase never renders against newer input
        # (wh-pattern-editor-r6.1).
        self._try_seq = 0
        # The open editor dialog, while one is up: create/update/test-draft
        # results are forwarded to it (wh-pattern-editor-dialog).
        self._editor_dialog = None

        self._build_ui()
        self._setup_font_size_shortcuts()
        self._apply_font_size()

        # Load watchdog (wh-pattern-editor-r0.2): a lost pm_patterns_data
        # response used to leave a permanently empty window with no error
        # and no retry affordance. showEvent arms it while the tree is
        # empty; populate() cancels it.
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(LOAD_RESPONSE_TIMEOUT_MS)
        self._load_timer.timeout.connect(self._on_load_timeout)

    def _apply_screen_bounded_size(self):
        """Set the dialog's minimum/initial size, clamped to the current
        screen (wh-pattern-window-scaling). Called from __init__ and
        exposed as its own method so a resize triggered by a screen
        change (e.g. the window dragged to a smaller monitor) could call
        it again; today only __init__ does.
        """
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
        else:
            # No resolvable screen (should not happen once Qt is up, but
            # keep the dialog usable rather than raising): fall back to
            # the preferred size acting as its own bound.
            available = QRect(
                0, 0,
                _PREFERRED_DIALOG_SIZE.width(),
                _PREFERRED_DIALOG_SIZE.height(),
            )
        min_size, initial_size = _clamp_dialog_size(
            _PREFERRED_DIALOG_SIZE, _MIN_DIALOG_SIZE, available
        )
        self.setMinimumSize(min_size)
        self.resize(initial_size)

    # ------------------------------------------------------------------ #
    #  Font-size zoom (wh-pattern-font-size)
    # ------------------------------------------------------------------ #

    def _setup_font_size_shortcuts(self):
        """Wire Ctrl+= / Ctrl+- / Ctrl+0 to the zoom slots.

        ``WidgetWithChildrenShortcut`` context makes each shortcut fire
        no matter which child widget currently holds focus (the filter
        box, the tree, the try-it box, ...), so the zoom works from
        anywhere in the dialog, not just when the dialog itself has
        focus.
        """
        self._zoom_in_shortcut = QShortcut(QKeySequence("Ctrl+="), self)
        self._zoom_in_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._zoom_in_shortcut.activated.connect(self._on_zoom_in)

        self._zoom_out_shortcut = QShortcut(QKeySequence("Ctrl+-"), self)
        self._zoom_out_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._zoom_out_shortcut.activated.connect(self._on_zoom_out)

        self._zoom_reset_shortcut = QShortcut(QKeySequence("Ctrl+0"), self)
        self._zoom_reset_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._zoom_reset_shortcut.activated.connect(self._on_zoom_reset)

    def _on_zoom_in(self):
        self._set_font_point_size(
            self._current_font_point_size + _FONT_SIZE_STEP
        )

    def _on_zoom_out(self):
        self._set_font_point_size(
            self._current_font_point_size - _FONT_SIZE_STEP
        )

    def _on_zoom_reset(self):
        self._set_font_point_size(self._default_font_point_size)

    def _set_font_point_size(self, point_size: int):
        clamped = max(_FONT_SIZE_MIN, min(_FONT_SIZE_MAX, point_size))
        if clamped == self._current_font_point_size:
            return
        self._current_font_point_size = clamped
        self._apply_font_size()

    def _apply_font_size(self):
        """Push ``_current_font_point_size`` out to every pane.

        The tree, the detail body labels, the try-it box, and the
        Advanced section's explanation text all pick up the change
        automatically through Qt's normal font-inheritance cascade
        (``self.setFont`` propagates to any descendant that has not been
        given its own explicit font). Three groups escape that cascade
        and are handled one at a time here:

        - The trigger title and the raw-data monospace boxes carry an
          explicit ``QFont`` set once at build time, so they are
          re-applied relative to the new size.
        - Every widget in ``_STYLED_WIDGET_ATTRS`` had its font pinned
          when its style sheet was applied (see the note there), so each
          one takes the new size directly.
        - The tree's bold category-header fonts are per-item, not
          per-widget, so they are re-applied to the items that already
          exist; ``populate()`` builds fresh ones from ``self.font()`` on
          every later refresh (see there), so a refresh after a zoom
          change does not revert them.

        Finally the fixed size caps are re-derived from the new size, so
        a zoomed button label is not cut off by a cap measured for the
        default font.
        """
        size = self._current_font_point_size
        base_font = self.font()
        base_font.setPointSize(size)
        self.setFont(base_font)

        title_font = self._trigger_label.font()
        title_font.setPointSize(size + _TITLE_FONT_SIZE_OFFSET)
        self._trigger_label.setFont(title_font)

        mono_font = self._raw_regex.font()
        mono_font.setPointSize(size)
        self._raw_regex.setFont(mono_font)
        self._raw_actions.setFont(mono_font)

        for attr in _STYLED_WIDGET_ATTRS:
            widget = getattr(self, attr)
            # Start from the widget's own font, not the dialog's, so a
            # style sheet's other effects and any per-widget family stay
            # as they are -- only the size changes.
            widget_font = widget.font()
            widget_font.setPointSize(size)
            widget.setFont(widget_font)

        self._resize_tree_category_fonts()
        self._resize_fixed_caps()

    def _resize_fixed_caps(self):
        """Re-derive the fixed width/height caps for the current font
        size (wh-pattern-manager-improve.1.4).

        Widths: the original constant is a floor, and the real cap is
        whatever the button's label needs at this size. Called after the
        fonts above are in place, because ``minimumSizeHint()`` is
        measured from the button's current font.

        Heights: the three read-only boxes were sized for the default
        font, so their caps scale with the ratio between the current size
        and that default. They stay scrollable either way -- this keeps
        them from collapsing to one or two visible lines at high zoom.
        """
        for widget, floor in (
            (self._change_hw_btn, _CHANGE_HOTWORD_BTN_BASE_MAX_WIDTH),
            (self._help_btn, _HELP_BTN_BASE_MAX_WIDTH),
        ):
            widget.setMaximumWidth(
                max(floor, widget.minimumSizeHint().width())
            )

        scale = self._current_font_point_size / self._default_font_point_size
        for widget, base_height in (
            (self._explain_text, _EXPLAIN_BASE_MAX_HEIGHT),
            (self._raw_regex, _RAW_REGEX_BASE_MAX_HEIGHT),
            (self._raw_actions, _RAW_ACTIONS_BASE_MAX_HEIGHT),
        ):
            widget.setMaximumHeight(round(base_height * scale))

    def _resize_tree_category_fonts(self):
        """Re-apply the bold category-header item font at the current
        size to every existing top-level tree item (wh-pattern-font-size).
        """
        bold_font = QFont(self.font())
        bold_font.setBold(True)
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            root.child(i).setFont(0, bold_font)

    def showEvent(self, event):
        super().showEvent(event)
        if self._tree.topLevelItemCount() == 0:
            self._load_timer.start()

    def _on_load_timeout(self):
        """No pattern data within the timeout: explain the blank tree."""
        if self._tree.topLevelItemCount() == 0:
            self._show_load_error(
                "Could not load patterns from Wheelhouse. Close and "
                "reopen this window to retry."
            )

    def _show_load_error(self, message: str):
        """Show the load-failure state in the tree's empty-state label
        (shared by the watchdog and the pm_get_patterns_result error
        branch); populate() clears it when real data arrives."""
        self._load_timer.stop()
        self._tree_empty_label.setStyleSheet(_ERROR_STYLE)
        self._tree_empty_label.setText(message)
        self._tree_empty_label.setVisible(True)

    def _show_banner(self, text: str, style: str):
        """Show the reusable top banner; ``style`` is 'error' or
        'warning' (wh-pattern-editor-r0.7/r0.8 GUI side)."""
        self._banner_label.setStyleSheet(
            _BANNER_ERROR_STYLE if style == "error"
            else _BANNER_WARNING_STYLE
        )
        self._banner_label.setText(text)
        self._banner_label.setVisible(True)
        self._banner_kind = style

    def _clear_banner(self, kind: str | None = None):
        """Clear the banner; with ``kind`` given, only if the showing
        banner is of that kind. The error banner (corrupt user file) is
        owned by populate() data; the warning banner (stale live reload)
        must survive the automatic post-save refresh and clears only
        when a later save reloads cleanly."""
        if kind is not None and self._banner_kind != kind:
            return
        self._banner_label.setText("")
        self._banner_label.setVisible(False)
        self._banner_kind = None

    def closeEvent(self, event):
        """Hide instead of closing to prevent app shutdown."""
        self.hide()
        event.ignore()

    # ------------------------------------------------------------------ #
    #  UI Construction
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)

        # Reusable notice banner, hidden until something goes wrong:
        # user-patterns file corruption arriving on pm_patterns_data, or a
        # reload warning riding a successful pm_*_result envelope
        # (wh-pattern-editor-r0.7/r0.8 GUI side).
        self._banner_label = QLabel()
        self._banner_label.setWordWrap(True)
        self._banner_label.setVisible(False)
        self._banner_kind = None  # 'error' | 'warning' | None
        self._banner_label.setAccessibleName("Pattern Manager notice")
        self._banner_label.setAccessibleDescription(
            "Warnings and errors about loading or saving your patterns"
        )
        self._banner_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        main_layout.addWidget(self._banner_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        # Kept as an attribute: closeEvent hides rather than closes, so
        # this widget -- and the user's splitter drag -- survives reopen.
        self._splitter = splitter

        # Left panel: filter + tree + buttons
        left_widget = self._build_left_panel()
        splitter.addWidget(left_widget)

        # Right panel: detail view
        right_widget = self._build_detail_panel()
        splitter.addWidget(right_widget)

        # Splitter sizing: left ~260px, right stretches
        splitter.setSizes([260, 540])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        main_layout.addWidget(splitter)

        # The manager has no unambiguous default action, so no button may
        # be autoDefault: Enter in the filter box must not click one
        # (keyboard-first, spec section 13).
        for btn in self.findChildren(QPushButton):
            btn.setAutoDefault(False)
            btn.setDefault(False)

        # Tab order follows the visual order, left panel then detail
        # panel. Hidden detail widgets stay in the chain; Tab skips them
        # while they are hidden.
        order = [
            self._change_hw_btn, self._filter_input, self._tree,
            self._try_input, self._add_btn, self._help_btn,
            self._edit_btn, self._duplicate_btn, self._customize_btn,
            self._remove_custom_btn, self._explain_btn, self._explain_text,
            self._advanced_toggle, self._raw_regex, self._raw_actions,
            self._delete_btn,
        ]
        for first, second in zip(order, order[1:]):
            QWidget.setTabOrder(first, second)

    def _build_left_panel(self) -> QWidget:
        """Build the left panel containing filter, tree, and action buttons."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 4, 0)

        # --- Wake word row ---
        hotword_row = QHBoxLayout()
        hotword_row.addWidget(QLabel("Wake word:"))
        self._hotword_value = QLabel(self._hotword)
        self._hotword_value.setStyleSheet("font-weight: bold;")
        self._hotword_value.setToolTip(
            "Say this word before commands marked [hotword]."
        )
        hotword_row.addWidget(self._hotword_value)
        hotword_row.addStretch()
        change_hw_btn = QPushButton("Change...")
        self._change_hw_btn = change_hw_btn
        change_hw_btn.setMaximumWidth(_CHANGE_HOTWORD_BTN_BASE_MAX_WIDTH)
        change_hw_btn.setAccessibleName("Change wake word")
        change_hw_btn.setAccessibleDescription(
            "Opens a prompt for a new wake word"
        )
        change_hw_btn.setToolTip(
            "Pick a different wake word (a single spoken word)."
        )
        change_hw_btn.clicked.connect(self._on_change_hotword_clicked)
        hotword_row.addWidget(change_hw_btn)
        layout.addLayout(hotword_row)

        # Field-level wake-word errors show here, never in a modal box
        # (wh-pattern-editor-ux, spec section 13).
        self._hotword_error_label = QLabel()
        self._hotword_error_label.setStyleSheet(_ERROR_STYLE)
        self._hotword_error_label.setWordWrap(True)
        self._hotword_error_label.setVisible(False)
        layout.addWidget(self._hotword_error_label)

        # --- Filter bar ---
        self._filter_input = QLineEdit()
        self._filter_input.setPlaceholderText("Filter patterns...")
        self._filter_input.setClearButtonEnabled(True)
        self._filter_input.setAccessibleName("Filter patterns")
        self._filter_input.setAccessibleDescription(
            "Hides patterns whose trigger does not contain this text"
        )
        self._filter_input.setToolTip(
            "Show only patterns whose trigger contains this text."
        )
        self._filter_input.textChanged.connect(self._on_filter_changed)
        # Enter jumps from the filter to its results (keyboard-first).
        self._filter_input.returnPressed.connect(self._tree_focus)
        layout.addWidget(self._filter_input)

        # --- Tree widget ---
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(18)
        self._tree.setAnimated(True)
        self._tree.setAccessibleName("Pattern list")
        self._tree.setAccessibleDescription(
            "All voice patterns grouped by category; select one to see "
            "its details"
        )
        self._tree.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self._tree, stretch=1)

        # Empty state when the filter matches nothing (spec section 13).
        self._tree_empty_label = QLabel()
        self._tree_empty_label.setStyleSheet(_MUTED_STYLE)
        self._tree_empty_label.setWordWrap(True)
        self._tree_empty_label.setVisible(False)
        layout.addWidget(self._tree_empty_label)

        # --- Try-it box (debounced pm_test_phrase; Enter sends at once) ---
        try_row = QHBoxLayout()
        try_label = QLabel("Try it:")
        self._try_input = QLineEdit()
        self._try_input.setPlaceholderText("Type what you would say...")
        self._try_input.setClearButtonEnabled(True)
        self._try_input.setAccessibleName("Try a phrase")
        self._try_input.setAccessibleDescription(
            "Finds which pattern responds to a phrase, without running it"
        )
        self._try_input.setToolTip(
            "Type what you would say; the pattern that responds is\n"
            "selected in the list above."
        )
        try_label.setBuddy(self._try_input)
        self._try_input.textChanged.connect(self._on_try_text_changed)
        self._try_input.returnPressed.connect(self._send_test_phrase)
        try_row.addWidget(try_label)
        try_row.addWidget(self._try_input, stretch=1)
        layout.addLayout(try_row)

        self._try_result_label = QLabel()
        self._try_result_label.setStyleSheet(_MUTED_STYLE)
        self._try_result_label.setWordWrap(True)
        layout.addWidget(self._try_result_label)

        self._try_timer = QTimer(self)
        self._try_timer.setSingleShot(True)
        self._try_timer.setInterval(TRY_IT_DEBOUNCE_MS)
        self._try_timer.timeout.connect(self._send_test_phrase)

        # --- Bottom buttons ---
        btn_row = QHBoxLayout()

        add_btn = QPushButton("Add Pattern")
        self._add_btn = add_btn
        add_btn.setAccessibleName("Add pattern")
        add_btn.setAccessibleDescription(
            "Opens the editor to create a new voice pattern"
        )
        add_btn.setToolTip("Create a new voice pattern.")
        add_btn.clicked.connect(self._on_add_clicked)
        btn_row.addWidget(add_btn)

        btn_row.addStretch()

        help_btn = QPushButton("? Help")
        self._help_btn = help_btn
        help_btn.setMaximumWidth(_HELP_BTN_BASE_MAX_WIDTH)
        help_btn.setAccessibleName("Pattern help")
        help_btn.setAccessibleDescription(
            "Opens the help page explaining patterns and the wake word"
        )
        help_btn.setToolTip("How patterns and the wake word work.")
        help_btn.clicked.connect(self._on_help_clicked)
        btn_row.addWidget(help_btn)

        layout.addLayout(btn_row)
        return panel

    def _tree_focus(self):
        """Move focus into the tree (Enter in the filter box)."""
        self._tree.setFocus()

    def _build_detail_panel(self) -> QWidget:
        """Build the right detail panel showing pattern information."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 0, 0)

        # --- Placeholder shown when nothing is selected ---
        self._placeholder_label = QLabel("Select a pattern to view details")
        self._placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Colour only: a font-size here would beat the size
        # _apply_font_size sets on this label (wh-pattern-manager-
        # improve.1.1).
        self._placeholder_label.setStyleSheet("color: gray;")

        # --- Detail content (hidden until a pattern is selected) ---
        self._detail_widget = QWidget()
        detail_layout = QVBoxLayout(self._detail_widget)
        detail_layout.setContentsMargins(0, 0, 0, 0)

        # Title / trigger
        self._trigger_label = QLabel()
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        self._trigger_label.setFont(title_font)
        detail_layout.addWidget(self._trigger_label)

        # Badges row
        # crewcut: these three badge pills keep their own hardcoded
        # "font-size: 11px" and do not follow the Ctrl+=/-/0 zoom
        # (wh-pattern-font-size) -- they are small fixed-size decorative
        # chips, not reading panes. To make them scale, add them to
        # _STYLED_WIDGET_ATTRS and drop the "font-size: 11px" from these
        # three rules: a style-sheet font-size beats the widget's own
        # font, so the size has to leave the style sheet before
        # _apply_font_size can reach these labels at all.
        self._badges_layout = QHBoxLayout()
        self._type_badge = QLabel()
        self._type_badge.setStyleSheet(
            "background-color: #3b82f6; color: white; padding: 2px 8px; "
            "border-radius: 3px; font-size: 11px;"
        )
        self._type_badge.setToolTip(
            "Command: runs actions when you say the trigger.\n"
            "Replacement: changes matching words while you dictate."
        )
        self._hotword_badge = QLabel()
        self._hotword_badge.setStyleSheet(
            "background-color: #f59e0b; color: white; padding: 2px 8px; "
            "border-radius: 3px; font-size: 11px;"
        )
        self._hotword_badge.setToolTip(
            "This pattern only responds after you say the wake word."
        )
        self._user_badge = QLabel("User")
        self._user_badge.setStyleSheet(
            "background-color: #10b981; color: white; padding: 2px 8px; "
            "border-radius: 3px; font-size: 11px;"
        )
        self._user_badge.setToolTip("A pattern you created.")
        self._badges_layout.addWidget(self._type_badge)
        self._badges_layout.addWidget(self._hotword_badge)
        self._badges_layout.addWidget(self._user_badge)
        self._badges_layout.addStretch()
        detail_layout.addLayout(self._badges_layout)

        detail_layout.addSpacing(8)

        # Info fields (form-like)
        info_group = QGroupBox("Details")
        info_layout = QVBoxLayout(info_group)

        # Trigger display
        trigger_row = QHBoxLayout()
        trigger_row.addWidget(QLabel("Trigger:"))
        self._detail_trigger = QLabel()
        self._detail_trigger.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        trigger_row.addWidget(self._detail_trigger, stretch=1)
        info_layout.addLayout(trigger_row)

        # Type display
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Type:"))
        self._detail_type = QLabel()
        type_row.addWidget(self._detail_type, stretch=1)
        info_layout.addLayout(type_row)

        # Hotword display
        hotword_row = QHBoxLayout()
        hotword_row.addWidget(QLabel("Hotword:"))
        self._detail_hotword = QLabel()
        hotword_row.addWidget(self._detail_hotword, stretch=1)
        info_layout.addLayout(hotword_row)

        # Action description
        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("Action:"))
        self._detail_action = QLabel()
        self._detail_action.setWordWrap(True)
        action_row.addWidget(self._detail_action, stretch=1)
        info_layout.addLayout(action_row)

        detail_layout.addWidget(info_group)

        # --- Pattern action buttons (states follow the selection) ---
        actions_row = QHBoxLayout()

        self._edit_btn = QPushButton("Edit...")
        self._edit_btn.setAccessibleName("Edit pattern")
        self._edit_btn.setAccessibleDescription(
            "Opens the editor to change this pattern in place"
        )
        self._edit_btn.setToolTip(
            "Change this pattern in the editor (your patterns only)."
        )
        self._edit_btn.clicked.connect(self._on_edit_clicked)
        actions_row.addWidget(self._edit_btn)

        self._duplicate_btn = QPushButton("Duplicate...")
        self._duplicate_btn.setAccessibleName("Duplicate pattern")
        self._duplicate_btn.setAccessibleDescription(
            "Opens the editor with a copy of this pattern; saving "
            "creates a new pattern"
        )
        self._duplicate_btn.setToolTip(
            "Open the editor with a copy of this pattern;\n"
            "saving creates a new pattern."
        )
        self._duplicate_btn.clicked.connect(self._on_duplicate_clicked)
        actions_row.addWidget(self._duplicate_btn)

        # Built-ins only; the label says what saving actually does.
        self._customize_btn = QPushButton("Customize (edit a copy)...")
        self._customize_btn.setAccessibleName("Customize built-in pattern")
        self._customize_btn.setAccessibleDescription(
            "Opens the editor with this built-in pattern's settings; "
            "saving creates your editable copy that replaces the built-in"
        )
        self._customize_btn.setToolTip(
            "Opens the editor with this built-in pattern's settings.\n"
            "Saving creates your own editable copy that replaces the\n"
            "built-in."
        )
        self._customize_btn.clicked.connect(self._on_customize_clicked)
        actions_row.addWidget(self._customize_btn)

        # Only on user copies that override a built-in.
        self._remove_custom_btn = QPushButton("Remove customization")
        self._remove_custom_btn.setAccessibleName("Remove customization")
        self._remove_custom_btn.setAccessibleDescription(
            "Deletes your customized copy so the built-in pattern takes "
            "over again"
        )
        self._remove_custom_btn.setToolTip(
            "Delete your customized copy; the built-in pattern takes\n"
            "over again."
        )
        self._remove_custom_btn.clicked.connect(
            self._on_remove_customization_clicked
        )
        actions_row.addWidget(self._remove_custom_btn)

        self._explain_btn = QPushButton("Explain")
        self._explain_btn.setCheckable(True)
        self._explain_btn.setAccessibleName("Explain pattern")
        self._explain_btn.setAccessibleDescription(
            "Shows a plain-English explanation of what this pattern does"
        )
        self._explain_btn.setToolTip(
            "Show a plain-English explanation of what this pattern does."
        )
        self._explain_btn.toggled.connect(self._on_explain_toggled)
        actions_row.addWidget(self._explain_btn)

        actions_row.addStretch()
        detail_layout.addLayout(actions_row)

        # Initial state: nothing selected yet.
        for btn in (
            self._edit_btn,
            self._duplicate_btn,
            self._customize_btn,
            self._remove_custom_btn,
            self._explain_btn,
        ):
            btn.setEnabled(False)
        self._customize_btn.setVisible(False)
        self._remove_custom_btn.setVisible(False)

        # --- Explanation panel (shown by the Explain button) ---
        self._explain_group = QGroupBox("What this pattern does")
        explain_layout = QVBoxLayout(self._explain_group)
        self._explain_text = QTextEdit()
        self._explain_text.setReadOnly(True)
        self._explain_text.setAccessibleName("Pattern explanation")
        self._explain_text.setAccessibleDescription(
            "A plain-English description of the selected pattern"
        )
        # Tab must leave read-only panes, never get swallowed by them.
        self._explain_text.setTabChangesFocus(True)
        self._explain_text.setMaximumHeight(_EXPLAIN_BASE_MAX_HEIGHT)
        explain_layout.addWidget(self._explain_text)
        self._explain_group.setVisible(False)
        detail_layout.addWidget(self._explain_group)

        # --- Advanced section (collapsible) ---
        self._advanced_toggle = QPushButton("Advanced >>")
        self._advanced_toggle.setCheckable(True)
        self._advanced_toggle.setFlat(True)
        self._advanced_toggle.setStyleSheet(
            "text-align: left; padding: 4px; color: #555;"
        )
        self._advanced_toggle.setAccessibleName("Show raw data")
        self._advanced_toggle.setAccessibleDescription(
            "Shows the pattern's regular expression and raw action data"
        )
        self._advanced_toggle.setToolTip(
            "Show the pattern's regular expression and raw action data."
        )
        self._advanced_toggle.toggled.connect(self._on_advanced_toggled)
        detail_layout.addWidget(self._advanced_toggle)

        self._advanced_group = QGroupBox("Raw Data")
        adv_layout = QVBoxLayout(self._advanced_group)

        adv_layout.addWidget(QLabel("Pattern regex:"))
        self._raw_regex = QTextEdit()
        self._raw_regex.setReadOnly(True)
        self._raw_regex.setAccessibleName("Pattern regular expression")
        self._raw_regex.setAccessibleDescription(
            "The raw regular expression this pattern matches, read-only"
        )
        self._raw_regex.setTabChangesFocus(True)
        self._raw_regex.setMaximumHeight(_RAW_REGEX_BASE_MAX_HEIGHT)
        self._raw_regex.setFont(QFont("Consolas", 9))
        adv_layout.addWidget(self._raw_regex)

        adv_layout.addWidget(QLabel("Raw actions:"))
        self._raw_actions = QTextEdit()
        self._raw_actions.setReadOnly(True)
        self._raw_actions.setAccessibleName("Raw actions")
        self._raw_actions.setAccessibleDescription(
            "The pattern's stored action steps as JSON, read-only"
        )
        self._raw_actions.setTabChangesFocus(True)
        self._raw_actions.setMaximumHeight(_RAW_ACTIONS_BASE_MAX_HEIGHT)
        self._raw_actions.setFont(QFont("Consolas", 9))
        adv_layout.addWidget(self._raw_actions)

        self._advanced_group.setVisible(False)
        detail_layout.addWidget(self._advanced_group)

        # Spacer to push delete button to bottom
        detail_layout.addStretch()

        # --- Delete button ---
        delete_row = QHBoxLayout()
        delete_row.addStretch()
        self._delete_btn = QPushButton("Delete Pattern")
        self._delete_btn.setEnabled(False)
        self._delete_btn.setAccessibleName("Delete pattern")
        self._delete_btn.setAccessibleDescription(
            "Permanently deletes this pattern after confirmation"
        )
        self._delete_btn.setToolTip(
            "Permanently delete this pattern (your patterns only)."
        )
        self._delete_btn.setStyleSheet(
            "QPushButton { color: #dc2626; } "
            "QPushButton:disabled { color: #999; }"
        )
        self._delete_btn.clicked.connect(self._on_delete_clicked)
        delete_row.addWidget(self._delete_btn)
        detail_layout.addLayout(delete_row)

        # Initially hide detail, show placeholder
        self._detail_widget.setVisible(False)

        layout.addWidget(self._placeholder_label)
        layout.addWidget(self._detail_widget)

        return panel

    # ------------------------------------------------------------------ #
    #  IPC Interface
    # ------------------------------------------------------------------ #

    def populate(self, data: dict):
        """Fill the tree from IPC response data.

        Args:
            data: Dict with structure::

                {
                    "categories": {
                        "Commands - Window Management": {
                            "patterns": [
                                {
                                    "trigger": "zoom in",
                                    "pattern": "^zoom in$",
                                    "requires_hotword": false,
                                    "actions": [...],
                                    "category": "...",
                                    "is_user_created": false,
                                    "description": "Press Ctrl+Plus"
                                },
                                ...
                            ]
                        },
                        ...
                    },
                    "hotword": "x-ray"
                }
        """
        # Real data arrived: the load watchdog and any load-error state
        # shown by it are stale (wh-pattern-editor-r0.2).
        self._load_timer.stop()
        self._tree_empty_label.setStyleSheet(_MUTED_STYLE)
        self._tree_empty_label.setText("")
        self._tree_empty_label.setVisible(False)

        # Corrupt user patterns file (contract: optional top-level
        # "user_file_error" {path, error, backup_path}). The patterns shown
        # below are the built-ins only; the banner says why and how to get
        # the user's own patterns back (wh-pattern-editor-r0.7 GUI side).
        # A populate without the key clears any earlier banner.
        user_file_error = data.get("user_file_error")
        if isinstance(user_file_error, dict):
            path = user_file_error.get("path") or "your patterns file"
            reason = user_file_error.get("error") or "unknown error"
            text = f"Your patterns file could not be read: {path}: {reason}"
            backup_path = user_file_error.get("backup_path")
            if backup_path:
                text += (
                    f" -- the previous version is saved at {backup_path}; "
                    "restoring that file recovers your patterns."
                )
            self._show_banner(text, "error")
        else:
            self._clear_banner(kind="error")

        self._hotword = data.get("hotword", "x-ray")
        self._hotword_value.setText(self._hotword)
        categories = data.get("categories", {})

        self._tree.clear()
        self._clear_detail()

        # Built from self.font(), not a bare QFont(), so a category
        # header always matches the dialog's current zoom level
        # (wh-pattern-font-size) even on a refresh triggered without an
        # explicit _apply_font_size() call.
        bold_font = QFont(self.font())
        bold_font.setBold(True)

        for cat_name, cat_data in categories.items():
            patterns = cat_data.get("patterns", [])
            count = len(patterns)

            # Top-level category item
            cat_item = QTreeWidgetItem(self._tree)
            cat_item.setText(0, f"{cat_name} ({count})")
            cat_item.setFont(0, bold_font)
            cat_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
            )

            for pat in patterns:
                child = QTreeWidgetItem(cat_item)
                trigger = pat.get("trigger_display", "???")
                label = trigger
                tips = []
                if pat.get("requires_hotword"):
                    label += "  [hotword]"
                    tips.append(
                        f"[hotword] -- say the wake word "
                        f"('{self._hotword}') before this command."
                    )
                if pat.get("overrides_builtin"):
                    label += "  [overrides built-in]"
                    tips.append(
                        "[overrides built-in] -- your customized copy; "
                        "it replaces the built-in pattern."
                    )
                elif pat.get("unresolved_override"):
                    # wh-pattern-override-doc-id A4. Ahead of [user]
                    # because [user] says "a pattern you created",
                    # which is the wrong thing to tell someone whose
                    # customization stopped replacing its built-in.
                    label += "  [unresolved override]"
                    tips.append(
                        _UNRESOLVED_OVERRIDE_TIP
                    )
                elif pat.get("is_user_created"):
                    label += "  [user]"
                    tips.append("[user] -- a pattern you created.")
                child.setText(0, label)
                if tips:
                    child.setToolTip(0, "\n".join(tips))
                child.setData(0, Qt.ItemDataRole.UserRole, pat)

        # Expand all by default
        self._tree.expandAll()

        # Re-apply any active filter
        filter_text = self._filter_input.text().strip()
        if filter_text:
            self._on_filter_changed(filter_text)

    def handle_response(self, message: dict):
        """Handle IPC response from Logic process.

        Args:
            message: Dict with ``action`` key indicating response type.
        """
        action = message.get("action")

        # The generic Logic failure handler names its envelope
        # f"{action}_result" ("pm_create_pattern_result"), while the
        # branch-level handlers answer with the short names
        # ("pm_create_result"). Normalize so a pre-handler failure (e.g.
        # the speech handler not ready yet) reaches the same error
        # display instead of vanishing unhandled (wh-pattern-editor-r0.2).
        action = {
            "pm_create_pattern_result": "pm_create_result",
            "pm_update_pattern_result": "pm_update_result",
            "pm_delete_pattern_result": "pm_delete_result",
        }.get(action, action)

        # Any successful pm_*_result envelope may carry an optional
        # "warning": the file write landed but the live pattern reload
        # failed (wh-pattern-editor-r0.8 GUI side). Checked before the
        # action branches so every result type gets it. A save/delete
        # result WITHOUT the warning proves the live patterns are fresh
        # again, so it clears an earlier stale-reload warning.
        data = message.get("data")
        if isinstance(data, dict) and data.get("success"):
            if data.get("warning"):
                self._show_banner(str(data["warning"]), "warning")
            elif action in (
                "pm_create_result", "pm_update_result",
                "pm_delete_result", "pm_set_hotword_result",
            ):
                self._clear_banner(kind="warning")

        if action == "pm_patterns_data":
            self.populate(message.get("data", {}))

        elif action == "pm_get_patterns_result":
            # The generic Logic handler answers a FAILED pm_get_patterns
            # this way (success carries pm_patterns_data instead); without
            # this branch the manager stays silently empty forever
            # (wh-pattern-editor-r0.3).
            data = message.get("data", {})
            if not data.get("success", True):
                self._show_load_error(
                    "Could not load patterns from Wheelhouse: "
                    f"{data.get('error', 'Unknown error')}. "
                    "Close and reopen this window to retry."
                )

        elif action == "pm_create_result":
            # The editor dialog owns the inline success/failure display
            # while it is open (wh-pattern-editor-dialog); the modal
            # warning remains only as a no-editor fallback.
            data = message.get("data", {})
            if self._editor_dialog is not None:
                self._editor_dialog.handle_response(message)
            if data.get("success"):
                # Refresh the full tree
                self.pattern_action.emit({"action": "pm_get_patterns"})
            elif self._editor_dialog is None:
                QMessageBox.warning(
                    self,
                    "Error Creating Pattern",
                    data.get("error", "Unknown error"),
                )

        elif action == "pm_update_result":
            data = message.get("data", {})
            if self._editor_dialog is not None:
                self._editor_dialog.handle_response(message)
            if data.get("success"):
                self.pattern_action.emit({"action": "pm_get_patterns"})
            elif self._editor_dialog is None:
                # Mirror the create path's no-editor fallback: a failure
                # arriving after the editor closed must not be silent
                # (wh-pattern-editor-r0.3).
                QMessageBox.warning(
                    self,
                    "Error Updating Pattern",
                    data.get("error", "Unknown error"),
                )

        elif action == "pm_test_draft_result":
            if self._editor_dialog is not None:
                self._editor_dialog.handle_response(message)

        elif action == "pm_test_phrase_result":
            self._render_test_phrase_result(message.get("data", {}))

        elif action == "pm_delete_result":
            data = message.get("data", {})
            if data.get("success"):
                self.pattern_action.emit({"action": "pm_get_patterns"})
            else:
                QMessageBox.warning(
                    self,
                    "Error Deleting Pattern",
                    data.get("error", "Unknown error"),
                )

        elif action == "pm_set_hotword_result":
            data = message.get("data", {})
            if data.get("success"):
                self._show_hotword_error(None)
                # Refresh so the new wake word shows everywhere.
                self.pattern_action.emit({"action": "pm_get_patterns"})
            else:
                # Field-level text under the wake-word row, not a modal
                # (wh-pattern-editor-ux, spec section 13).
                self._show_hotword_error(
                    data.get("error", "Unknown error")
                )

    # ------------------------------------------------------------------ #
    #  Tree Interaction Slots
    # ------------------------------------------------------------------ #

    def _on_selection_changed(self, current, _previous):
        """Update detail panel when tree selection changes."""
        if current is None:
            self._clear_detail()
            return

        pat = current.data(0, Qt.ItemDataRole.UserRole)
        if pat is None:
            # Clicked a category header
            self._clear_detail()
            return

        self._selected_pattern = pat
        self._show_detail(pat)

    def _on_filter_changed(self, text: str):
        """Filter tree items by trigger text (case-insensitive)."""
        search = text.strip().lower()

        any_match = False
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            cat_item = root.child(i)
            any_visible = False
            for j in range(cat_item.childCount()):
                child = cat_item.child(j)
                pat = child.data(0, Qt.ItemDataRole.UserRole)
                trigger = (pat or {}).get("trigger_display", "").lower()
                match = (not search) or (search in trigger)
                child.setHidden(not match)
                if match:
                    any_visible = True
            cat_item.setHidden(not any_visible)
            any_match = any_match or any_visible

        # Empty state: say WHY the tree is blank (spec section 13).
        show_empty = bool(search) and not any_match
        self._tree_empty_label.setText(
            f"No patterns match '{text.strip()}'" if show_empty else ""
        )
        self._tree_empty_label.setVisible(show_empty)

    # ------------------------------------------------------------------ #
    #  Detail Panel Helpers
    # ------------------------------------------------------------------ #

    def _show_detail(self, pat: dict):
        """Populate the detail panel from a pattern dict."""
        self._placeholder_label.setVisible(False)
        self._detail_widget.setVisible(True)

        trigger = pat.get("trigger_display", "???")
        self._trigger_label.setText(trigger)
        self._detail_trigger.setText(trigger)

        # Type badge. Classified by the explainer's shared seam -- most
        # entries carry no explicit type key (shipped replacements,
        # simple-mode saves), and defaulting those to "Command" mislabeled
        # every replacement while the Explain panel in the same window
        # said otherwise (wh-pattern-editor-r4.2).
        kind = pattern_kind(pat)
        type_label = (
            "Trailing command" if kind == "trailing" else kind.capitalize()
        )
        self._type_badge.setText(type_label)
        self._detail_type.setText(type_label)

        # Hotword badge
        requires_hw = pat.get("requires_hotword", False)
        if requires_hw:
            self._hotword_badge.setText(f"Hotword: {self._hotword}")
            self._hotword_badge.setVisible(True)
            self._detail_hotword.setText(
                f"Required ({self._hotword})"
            )
        else:
            self._hotword_badge.setVisible(False)
            self._detail_hotword.setText("Not required")

        # User badge (note when this user pattern overrides a built-in)
        is_user = pat.get("is_user_created", False)
        overrides = is_user and bool(pat.get("overrides_builtin"))
        unresolved = is_user and bool(pat.get("unresolved_override"))
        if overrides:
            self._user_badge.setText("User (overrides built-in)")
            # The restoration promise holds only for the LAST copy. Other
            # saved rules replacing the same built-in keep it switched off,
            # and deleting this one leaves them in place
            # (wh-pattern-override-doc-id.3.6). The count stays in the hover
            # help rather than the badge label, which names the state.
            others = pat.get("other_claimants", 0)
            if others:
                subject, verb = _other_copies(others)
                self._user_badge.setToolTip(
                    "Your editable copy of a built-in pattern. It replaces "
                    f"the built-in. {subject} also {verb} it. Removing this "
                    "copy will not bring the built-in back."
                )
            else:
                self._user_badge.setToolTip(
                    "Your editable copy of a built-in pattern. It replaces "
                    "the built-in; Remove customization restores it."
                )
        elif unresolved:
            # wh-pattern-override-doc-id A4: the merge kept this entry
            # but could not say which built-in it replaced, so neither
            # badge above is true of it.
            self._user_badge.setText("User (unresolved override)")
            self._user_badge.setToolTip(
                _UNRESOLVED_OVERRIDE_TIP
            )
        else:
            self._user_badge.setText("User")
            self._user_badge.setToolTip("A pattern you created.")
        self._user_badge.setVisible(is_user)

        # Action description: Logic's short per-row description when
        # present, else the explainer -- the single wording source for
        # generated action text (spec section 10, wh-pattern-editor-manager).
        description = pat.get("description", "")
        if not description:
            description = explain_pattern(pat, self._hotword)
        self._detail_action.setText(description)

        # Advanced fields
        self._raw_regex.setPlainText(pat.get("raw_pattern", ""))
        self._raw_actions.setPlainText(
            json.dumps(pat.get("raw_actions", []), indent=2)
        )

        # Delete button -- only for user-created patterns
        self._delete_btn.setEnabled(is_user)

        # Action buttons follow the selection (spec section 9).
        self._edit_btn.setEnabled(is_user)
        self._duplicate_btn.setEnabled(True)
        self._explain_btn.setEnabled(True)
        self._customize_btn.setVisible(not is_user)
        self._customize_btn.setEnabled(not is_user)
        self._remove_custom_btn.setVisible(overrides)
        self._remove_custom_btn.setEnabled(overrides)
        if self._explain_btn.isChecked():
            self._explain_text.setPlainText(
                explain_pattern(pat, self._hotword)
            )

    def _clear_detail(self):
        """Reset the detail panel to placeholder state."""
        self._selected_pattern = None
        self._detail_widget.setVisible(False)
        self._placeholder_label.setVisible(True)
        self._delete_btn.setEnabled(False)
        self._edit_btn.setEnabled(False)
        self._duplicate_btn.setEnabled(False)
        self._explain_btn.setEnabled(False)
        self._explain_btn.setChecked(False)  # hides the panel via toggled
        self._customize_btn.setEnabled(False)
        self._customize_btn.setVisible(False)
        self._remove_custom_btn.setEnabled(False)
        self._remove_custom_btn.setVisible(False)

    # ------------------------------------------------------------------ #
    #  Try-it box (pm_test_phrase)
    # ------------------------------------------------------------------ #

    def _on_try_text_changed(self, text: str):
        if not text.strip():
            self._try_timer.stop()
            # Clearing is a deliberate reset: advance the request counter
            # so an answer still in flight counts as out of date and
            # cannot reappear in the cleared box or move the tree
            # selection (wh-pattern-editor-r7.1).
            self._try_seq += 1
            self._set_try_result("", _MUTED_STYLE)
            return
        self._try_timer.start()

    def _send_test_phrase(self):
        """Ask Logic which pattern responds to the typed phrase."""
        self._try_timer.stop()  # Enter must not double-send via the debounce
        text = self._try_input.text().strip()
        if not text:
            return
        self._last_try_text = text
        self._try_seq += 1
        self.pattern_action.emit(
            {"action": "pm_test_phrase",
             "data": {"text": text, "request_id": self._try_seq}}
        )

    def _render_test_phrase_result(self, data: dict):
        # An answer for an older request must not render (or select a
        # tree row) against newer input: the echoed request_id pairs the
        # result with its send, and an out-of-date one is dropped -- the
        # request in flight for the current input renders when it lands
        # (wh-pattern-editor-r6.1). An id-less result (handler failure
        # envelope, older Logic process) keeps the tolerant legacy path.
        request_id = data.get("request_id")
        if request_id is not None and request_id != self._try_seq:
            return
        # An id match proves the result answered the last-sent text, so
        # show that; without an id, fall back to the box text.
        if request_id is not None:
            shown = self._last_try_text
        else:
            shown = self._try_input.text().strip() or self._last_try_text
        if not data.get("success", True):
            self._set_try_result(
                data.get("error", "Test failed"), _ERROR_STYLE
            )
            return
        match = data.get("match")
        if not match:
            self._set_try_result(
                f"No pattern responds to '{shown}'", _MUTED_STYLE
            )
            return
        trigger = match.get("trigger_display", "???")
        if match.get("requires_hotword"):
            note = f"say '{self._hotword}' first"
        else:
            note = "no wake word needed"
        self._set_try_result(f"Matches '{trigger}' ({note})", _OK_STYLE)
        item = self._find_tree_item(match.get("pattern_id"))
        if item is not None:
            self._tree.setCurrentItem(item)
            self._tree.scrollToItem(item)

    def _set_try_result(self, text: str, style: str):
        self._try_result_label.setStyleSheet(style)
        self._try_result_label.setText(text)

    def _find_tree_item(self, pattern_id):
        """The tree item whose entry has ``pattern_id``, or None."""
        if not pattern_id:
            return None
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            cat_item = root.child(i)
            for j in range(cat_item.childCount()):
                child = cat_item.child(j)
                pat = child.data(0, Qt.ItemDataRole.UserRole)
                if pat and pat.get("id") == pattern_id:
                    return child
        return None

    # ------------------------------------------------------------------ #
    #  Button Slots
    # ------------------------------------------------------------------ #

    def _open_editor(
        self, entry: dict = None, pattern_id: str = None,
        keep_identity: bool = False,
    ):
        """Open the pattern editor dialog and route its traffic through us.

        ``entry`` prefills the editor; ``pattern_id`` with it means edit in
        place (pm_update_pattern), without it the save creates a new pattern
        (the Duplicate/Customize seam). The editor sends pm_create_pattern /
        pm_update_pattern itself when Save is clicked and stays open on
        failure; its Logic responses arrive through handle_response above
        while ``_editor_dialog`` is set (wh-pattern-editor-dialog).

        ``keep_identity`` is what finally separates Customize from
        Duplicate, which until now opened the editor with the same call.
        Customize passes True, so the saved copy carries the built-in's
        doc_id and keeps overriding it when a later release rewrites the
        built-in's expression. Duplicate passes False, because a duplicate
        is a rule of its own: two rules claiming one built-in would fight
        over it with the file order deciding the winner
        (wh-pattern-override-doc-id A2). Edit passes False as well, and it
        is not the flag that decides an edit: the editor carries the id
        whenever it is editing in place, because ``pattern_id`` alone says
        so. It needs the id for its try-it preview, which has no file to
        read; the save reads the id from the block on disk, which is what
        stops an edit moving a rule onto a different built-in
        (wh-pattern-override-doc-id.2.2).
        """
        from create_pattern_dialog import CreatePatternDialog

        dialog = CreatePatternDialog(
            self._hotword, parent=self, entry=entry, pattern_id=pattern_id,
            keep_identity=keep_identity,
        )
        # The editor is one of the Pattern Manager's zoomable panes
        # (wh-pattern-font-size): a QDialog opened with a parent is still
        # its own top-level window, so it does not inherit our font
        # automatically -- it must be applied explicitly.
        dialog.setFont(self.font())
        dialog.pattern_action.connect(self.pattern_action)
        self._editor_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._editor_dialog = None
            # The manager never closes (closeEvent hides), so without an
            # explicit deleteLater every Add/Edit/Duplicate/Customize
            # would leak a full dialog widget tree for the GUI process's
            # lifetime (wh-pattern-editor-r0.6). Stop the timers first so
            # neither the 400 ms try-it debounce (a stray pm_test_draft)
            # nor the save watchdog can fire between close and deletion.
            dialog._try_timer.stop()
            dialog._save_timeout_timer.stop()
            dialog.deleteLater()

    def _on_add_clicked(self):
        """Open the pattern editor dialog to add a new user pattern."""
        self._open_editor()

    def _on_edit_clicked(self):
        """Edit the selected user pattern in place."""
        pat = self._selected_pattern
        if not pat or not pat.get("is_user_created"):
            return
        self._open_editor(entry=pat, pattern_id=pat.get("id"))

    def _on_duplicate_clicked(self):
        """Open the editor prefilled from the selection; saves as new.

        Deliberately NOT keep_identity: a duplicate is a new rule, and a
        second rule carrying the original's doc_id would claim the same
        built-in (wh-pattern-override-doc-id A2)."""
        pat = self._selected_pattern
        if not pat:
            return
        self._open_editor(entry=pat)

    def _on_customize_clicked(self):
        """Open the editor prefilled from a built-in; saving creates the
        same-trigger user copy that overrides it (plain create semantics).

        The copy carries the built-in's doc_id, which is what keeps it
        overriding that built-in after a release rewrites the built-in's
        expression (wh-pattern-override-doc-id A2). This is the only
        caller that passes keep_identity=True."""
        pat = self._selected_pattern
        if not pat or pat.get("is_user_created"):
            return
        self._open_editor(entry=pat, keep_identity=True)

    def _on_remove_customization_clicked(self):
        """Delete the user copy that overrides a built-in, after confirming."""
        pat = self._selected_pattern
        if not pat or not pat.get("overrides_builtin"):
            return
        trigger = pat.get("trigger_display", "???")
        # Only the last copy's removal brings the built-in back. Deleting
        # this one leaves every other rule replacing the same built-in in
        # place, so the person has to be told before they agree
        # (wh-pattern-override-doc-id.3.6). Nothing else is deleted to make
        # the older sentence true: the catalog keeps those rows on purpose.
        others = pat.get("other_claimants", 0)
        if others:
            subject, verb = _other_copies(others)
            outcome = (
                f"{subject} still {verb} the built-in. "
                "It will not take over again."
            )
        else:
            outcome = "The built-in pattern takes over again."
        reply = QMessageBox.question(
            self,
            "Remove Customization",
            f'Remove your customized copy of "{trigger}"?\n\n' + outcome,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.pattern_action.emit(
                {
                    "action": "pm_delete_pattern",
                    "data": {"pattern_id": pat.get("id")},
                }
            )

    def _on_explain_toggled(self, checked: bool):
        """Show or hide the plain-English explanation panel."""
        if not checked:
            self._explain_group.setVisible(False)
            return
        pat = self._selected_pattern
        if pat is None:
            self._explain_btn.setChecked(False)
            return
        self._explain_text.setPlainText(explain_pattern(pat, self._hotword))
        self._explain_group.setVisible(True)

    def _on_delete_clicked(self):
        """Delete the selected user-created pattern after confirmation."""
        if self._selected_pattern is None:
            return

        trigger = self._selected_pattern.get("trigger_display", "???")
        is_user = self._selected_pattern.get("is_user_created", False)
        if not is_user:
            return

        reply = QMessageBox.question(
            self,
            "Delete Pattern",
            f'Delete the pattern "{trigger}"?\n\nThis cannot be undone.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            # The Logic handler deletes by SHA-256 pattern id, not trigger.
            self.pattern_action.emit(
                {
                    "action": "pm_delete_pattern",
                    "data": {"pattern_id": self._selected_pattern.get("id")},
                }
            )

    def _on_change_hotword_clicked(self):
        """Prompt for a new wake word and send it to the Logic process."""
        from PySide6.QtWidgets import QInputDialog

        self._show_hotword_error(None)
        text, ok = QInputDialog.getText(
            self,
            "Change Wake Word",
            "New wake word (a single spoken word):",
            text=self._hotword,
        )
        if not ok:
            return
        value = text.strip()
        if not value:
            # Field-level error under the wake-word row, not a modal
            # (wh-pattern-editor-ux, spec section 13).
            self._show_hotword_error("The wake word cannot be empty.")
            return
        self.pattern_action.emit(
            {"action": "pm_set_hotword", "data": {"hotword": value}}
        )

    def _show_hotword_error(self, message):
        """Show or clear the field-level error under the wake-word row."""
        self._hotword_error_label.setText(message or "")
        self._hotword_error_label.setVisible(bool(message))

    def _on_help_clicked(self):
        """Open the Pattern Manager help dialog."""
        from pattern_help_dialog import PatternHelpDialog

        dlg = PatternHelpDialog(parent=self)
        # The help window is a zoomable pane too (wh-pattern-font-size);
        # see _open_editor for why this must be explicit.
        dlg.setFont(self.font())
        dlg.exec()

    def _on_advanced_toggled(self, checked: bool):
        """Show or hide the advanced raw-data section."""
        self._advanced_visible = checked
        self._advanced_group.setVisible(checked)
        self._advanced_toggle.setText(
            "<< Advanced" if checked else "Advanced >>"
        )
