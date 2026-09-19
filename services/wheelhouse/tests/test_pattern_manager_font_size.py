"""Font-size zoom tests for the Pattern Manager dialog (wh-pattern-font-size).

Covers acceptance criteria 1 (Ctrl+=/-/0 wired and applied to every pane:
tree, detail, editor/try-it, help), 2 (clamped to sane bounds), and the
apply-to-all-panes / bounds part of criterion 4. Persistence (criterion 3
and the persistence-round-trip part of criterion 4) is BLOCKED -- see the
bd comment on wh-pattern-font-size: no settings/preference reader-writer
reachable from the GUI process exists anywhere in the codebase to reuse
(QSettings is never used; no dialog persists geometry/preferences; the
only two persistence idioms in the codebase -- config.toml via
StateManager, and the small atomic-write data/*.toml files such as
click_first_use_hint.py's -- are both owned exclusively by the Logic
process and unreachable from the GUI process without a new IPC round
trip). No persistence-round-trip test exists here because there is
nothing to round-trip through.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

pytestmark = pytest.mark.usefixtures("qapp")


def _make_dialog():
    from pattern_manager_dialog import PatternManagerDialog
    return PatternManagerDialog(parent=None)


def test_zoom_in_increases_the_font_size():
    from pattern_manager_dialog import _FONT_SIZE_STEP
    dialog = _make_dialog()
    start = dialog._current_font_point_size
    dialog._on_zoom_in()
    assert dialog._current_font_point_size == start + _FONT_SIZE_STEP
    assert dialog.font().pointSize() == start + _FONT_SIZE_STEP


def test_zoom_out_decreases_the_font_size():
    from pattern_manager_dialog import _FONT_SIZE_STEP
    dialog = _make_dialog()
    start = dialog._current_font_point_size
    dialog._on_zoom_out()
    assert dialog._current_font_point_size == start - _FONT_SIZE_STEP
    assert dialog.font().pointSize() == start - _FONT_SIZE_STEP


def test_zoom_reset_returns_to_the_starting_size():
    dialog = _make_dialog()
    default = dialog._default_font_point_size
    dialog._on_zoom_in()
    dialog._on_zoom_in()
    dialog._on_zoom_in()
    assert dialog._current_font_point_size != default
    dialog._on_zoom_reset()
    assert dialog._current_font_point_size == default
    assert dialog.font().pointSize() == default


def test_zoom_out_is_clamped_to_the_minimum():
    from pattern_manager_dialog import _FONT_SIZE_MIN
    dialog = _make_dialog()
    for _ in range(100):
        dialog._on_zoom_out()
    assert dialog._current_font_point_size == _FONT_SIZE_MIN
    assert dialog.font().pointSize() == _FONT_SIZE_MIN


def test_zoom_in_is_clamped_to_the_maximum():
    from pattern_manager_dialog import _FONT_SIZE_MAX
    dialog = _make_dialog()
    for _ in range(100):
        dialog._on_zoom_in()
    assert dialog._current_font_point_size == _FONT_SIZE_MAX
    assert dialog.font().pointSize() == _FONT_SIZE_MAX


def test_keyboard_shortcuts_are_bound_to_ctrl_equals_minus_and_zero():
    dialog = _make_dialog()
    bindings = {
        sc.key().toString(): sc for sc in dialog.findChildren(type(dialog._zoom_in_shortcut))
    }
    assert QKeySequence("Ctrl+=").toString() in bindings
    assert QKeySequence("Ctrl+-").toString() in bindings
    assert QKeySequence("Ctrl+0").toString() in bindings
    # Must fire regardless of which child widget (tree, filter box, try-it
    # box, ...) currently holds focus.
    for sc in bindings.values():
        assert sc.context() == Qt.ShortcutContext.WidgetWithChildrenShortcut


def test_zoom_in_shortcut_slot_matches_the_button_method():
    # The shortcut is wired to the same slot the direct-call tests above
    # exercise, so this closes the gap between "the slot works" and "the
    # key combo reaches it" without needing to synthesize raw key events
    # in an offscreen test runner.
    dialog = _make_dialog()
    assert dialog._zoom_in_shortcut.key() == QKeySequence("Ctrl+=")
    assert dialog._zoom_out_shortcut.key() == QKeySequence("Ctrl+-")
    assert dialog._zoom_reset_shortcut.key() == QKeySequence("Ctrl+0")


def test_each_shortcut_activates_its_own_zoom_slot():
    # key() alone cannot tell Ctrl+- wired to _on_zoom_out from Ctrl+-
    # wired to _on_zoom_in: both bind the same key sequence. The slots
    # are patched BEFORE construction because __init__ connects bound
    # methods, so a patch applied afterwards would never be reached.
    import pattern_manager_dialog as pmd

    fired = []
    with patch.object(
        pmd.PatternManagerDialog, "_on_zoom_in",
        lambda self: fired.append("in"),
    ), patch.object(
        pmd.PatternManagerDialog, "_on_zoom_out",
        lambda self: fired.append("out"),
    ), patch.object(
        pmd.PatternManagerDialog, "_on_zoom_reset",
        lambda self: fired.append("reset"),
    ):
        dialog = _make_dialog()
        dialog._zoom_in_shortcut.activated.emit()
        dialog._zoom_out_shortcut.activated.emit()
        dialog._zoom_reset_shortcut.activated.emit()

    assert fired == ["in", "out", "reset"]


# ---------------------------------------------------------------------------
# Applied to all panes: tree, detail, try-it, and the raw-data monospace
# boxes all follow the dialog's own font; the trigger title stays larger
# by a fixed offset so it still reads as a title.
# ---------------------------------------------------------------------------


def test_zoom_applies_to_the_tree_pane():
    dialog = _make_dialog()
    dialog._on_zoom_in()
    assert dialog._tree.font().pointSize() == dialog._current_font_point_size


def test_zoom_applies_to_the_try_it_input():
    dialog = _make_dialog()
    dialog._on_zoom_in()
    assert dialog._try_input.font().pointSize() == dialog._current_font_point_size


def test_zoom_applies_to_the_explain_panel():
    dialog = _make_dialog()
    dialog._on_zoom_in()
    assert dialog._explain_text.font().pointSize() == dialog._current_font_point_size


def test_zoom_applies_to_the_raw_data_monospace_boxes():
    dialog = _make_dialog()
    dialog._on_zoom_in()
    assert dialog._raw_regex.font().pointSize() == dialog._current_font_point_size
    assert dialog._raw_actions.font().pointSize() == dialog._current_font_point_size
    # Stays monospace -- only the size changed.
    assert dialog._raw_regex.font().family() == "Consolas"


def test_zoom_keeps_the_trigger_title_larger_than_the_body_text():
    from pattern_manager_dialog import _TITLE_FONT_SIZE_OFFSET
    dialog = _make_dialog()
    dialog._on_zoom_in()
    assert (
        dialog._trigger_label.font().pointSize()
        == dialog._current_font_point_size + _TITLE_FONT_SIZE_OFFSET
    )


def test_zoom_applies_to_a_freshly_populated_tree_category_header():
    # populate() builds category-header item fonts fresh each refresh;
    # a size set before a later refresh must not be lost.
    dialog = _make_dialog()
    dialog._on_zoom_in()
    dialog._on_zoom_in()
    dialog.populate(
        {
            "hotword": "computer",
            "categories": {
                "Commands": {
                    "patterns": [
                        {
                            "id": "x",
                            "trigger_display": "save",
                            "requires_hotword": False,
                            "is_user_created": False,
                        }
                    ]
                }
            },
        }
    )
    header = dialog._tree.invisibleRootItem().child(0)
    assert header.font(0).pointSize() == dialog._current_font_point_size


# ---------------------------------------------------------------------------
# Style-sheet-bearing widgets (wh-pattern-manager-improve.1.1). Qt resolves
# a styled widget's font once and pins it, so an ancestor setFont never
# reaches these; every one of them needs the zoom size applied directly.
# Listed here rather than imported from the dialog so dropping a widget
# from the production list fails these tests instead of shrinking them.
# ---------------------------------------------------------------------------

_STYLED_WIDGETS = (
    "_placeholder_label",
    "_hotword_value",
    "_tree_empty_label",
    "_try_result_label",
    "_hotword_error_label",
    "_advanced_toggle",
    "_delete_btn",
    "_banner_label",
)


@pytest.mark.parametrize("attr", _STYLED_WIDGETS)
def test_zoom_applies_to_each_stylesheet_bearing_widget(attr):
    dialog = _make_dialog()
    dialog._on_zoom_in()
    dialog._on_zoom_in()
    widget = getattr(dialog, attr)
    assert widget.font().pointSize() == dialog._current_font_point_size


@pytest.mark.parametrize("attr", _STYLED_WIDGETS)
def test_no_zoomed_widget_pins_a_font_size_in_its_style_sheet(attr):
    # A px font-size in the style sheet beats the widget's own font, so a
    # pinned size would silently undo the setFont above.
    dialog = _make_dialog()
    assert "font-size" not in getattr(dialog, attr).styleSheet()


def test_try_result_keeps_the_zoomed_size_after_a_restyle():
    from pattern_manager_dialog import _OK_STYLE
    dialog = _make_dialog()
    dialog._on_zoom_in()
    dialog._set_try_result("Matches 'save' (no wake word needed)", _OK_STYLE)
    assert (
        dialog._try_result_label.font().pointSize()
        == dialog._current_font_point_size
    )


def test_tree_empty_label_keeps_the_zoomed_size_after_a_load_error():
    dialog = _make_dialog()
    dialog._on_zoom_in()
    dialog._show_load_error("Could not load patterns from Wheelhouse.")
    assert (
        dialog._tree_empty_label.font().pointSize()
        == dialog._current_font_point_size
    )


def test_banner_keeps_the_zoomed_size_after_it_is_shown():
    dialog = _make_dialog()
    dialog._on_zoom_in()
    dialog._show_banner("Your patterns file is unreadable.", "error")
    assert (
        dialog._banner_label.font().pointSize()
        == dialog._current_font_point_size
    )


@pytest.mark.parametrize(
    "attr", ("_type_badge", "_hotword_badge", "_user_badge")
)
def test_badge_pills_stay_out_of_the_zoom(attr):
    # crewcut exclusion recorded on wh-pattern-font-size: the pills are
    # fixed-size decorative chips, not reading panes. Asserted so a later
    # sweep of the styled widgets does not silently pull them in.
    dialog = _make_dialog()
    assert "font-size: 11px" in getattr(dialog, attr).styleSheet()


# ---------------------------------------------------------------------------
# Fixed pixel caps (wh-pattern-manager-improve.1.4): a cap sized for the
# default font clips its own label once the text is zoomed.
# ---------------------------------------------------------------------------


def _zoom_to_max(dialog):
    from pattern_manager_dialog import _FONT_SIZE_MAX
    while dialog._current_font_point_size < _FONT_SIZE_MAX:
        dialog._on_zoom_in()
    return dialog


@pytest.mark.parametrize("attr", ("_change_hw_btn", "_help_btn"))
def test_button_width_caps_never_clip_their_label(attr):
    dialog = _zoom_to_max(_make_dialog())
    button = getattr(dialog, attr)
    assert button.maximumWidth() >= button.minimumSizeHint().width()


@pytest.mark.parametrize("attr", ("_change_hw_btn", "_help_btn"))
def test_button_width_caps_never_clip_their_label_at_the_default_size(attr):
    dialog = _make_dialog()
    button = getattr(dialog, attr)
    assert button.maximumWidth() >= button.minimumSizeHint().width()


@pytest.mark.parametrize(
    "attr", ("_explain_text", "_raw_regex", "_raw_actions")
)
def test_read_only_box_heights_grow_with_the_zoom(attr):
    dialog = _make_dialog()
    before = getattr(dialog, attr).maximumHeight()
    _zoom_to_max(dialog)
    assert getattr(dialog, attr).maximumHeight() > before


class _FakeChildDialog:
    """Stand-in for CreatePatternDialog / PatternHelpDialog that records
    setFont() calls without building any real Qt widgets."""

    def __init__(self, *args, **kwargs):
        self.fonts_applied = []
        self.pattern_action = _NullSignal()

    def setFont(self, font):
        self.fonts_applied.append(font.pointSize())

    def exec(self):
        return 0

    def deleteLater(self):
        pass


class _NullSignal:
    def connect(self, *args, **kwargs):
        pass


def test_editor_receives_setFont_matching_current_size():
    dialog = _make_dialog()
    dialog._on_zoom_in()
    dialog._on_zoom_in()
    captured = {}

    class RecordingFakeEditor(_FakeChildDialog):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._try_timer = _NullTimer()
            self._save_timeout_timer = _NullTimer()

        def setFont(self, font):
            captured["point_size"] = font.pointSize()

    with patch(
        "create_pattern_dialog.CreatePatternDialog", RecordingFakeEditor
    ):
        dialog._on_add_clicked()
    assert captured["point_size"] == dialog._current_font_point_size


class _NullTimer:
    def stop(self):
        pass


def test_help_dialog_receives_setFont_matching_current_size():
    dialog = _make_dialog()
    dialog._on_zoom_out()
    captured = {}

    class RecordingFakeHelp:
        def __init__(self, parent=None):
            pass

        def setFont(self, font):
            captured["point_size"] = font.pointSize()

        def exec(self):
            return 0

    with patch(
        "pattern_help_dialog.PatternHelpDialog", RecordingFakeHelp
    ):
        dialog._on_help_clicked()
    assert captured["point_size"] == dialog._current_font_point_size
