"""The Pattern Manager "Only my patterns" checkbox (wh-pattern-manager-improve.2).

The checkbox limits the list to the user's own rules: an edited built-in
(overrides_builtin), an added rule, and a duplicate. Every one of those rows
carries is_user_created True in the stored pattern dict. It combines with the
trigger text filter: a row shows only when it passes both tests.

Boss condition: the checkbox's accessibleName() is exactly "Only my patterns",
with no ampersand, because the voice click command ("<safety word> click only
my patterns") matches the accessible name and not the visible text.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QCheckBox

# Offscreen Qt draws every glyph as a missing-glyph box, which inflates the
# checkbox's minimum width at 24 pt; this fixture measures with Segoe UI.
from tests.test_pattern_manager_dialog import (  # noqa: F401
    _offscreen_windows_layout_font,
)

# wh-pytest-flaky-segfault: constructing the dialog builds real Qt widgets;
# without a QApplication Qt aborts the whole interpreter.
pytestmark = pytest.mark.usefixtures("qapp")


def _pattern(pid, trigger, *, user=False, overrides=False):
    return {
        "id": pid,
        "trigger_display": trigger,
        "requires_hotword": False,
        "is_user_created": user,
        "overrides_builtin": overrides,
        "raw_pattern": f"^{trigger}$",
        "raw_actions": [],
        "description": "",
    }


def _data(*, with_user_rows=True):
    user_rows = [
        _pattern("u-edited", "save file", user=True, overrides=True),
        _pattern("u-added", "deploy site", user=True),
        _pattern("u-copy", "save copy", user=True),
    ]
    return {
        "hotword": "computer",
        "categories": {
            "Commands - Window Management": {
                "patterns": [
                    _pattern("b-save", "save"),
                    _pattern("b-open", "open file"),
                ]
            },
            "Commands - Editing": {
                "patterns": [_pattern("b-copy", "copy that")]
            },
            **(
                {"User Patterns": {"patterns": user_rows}}
                if with_user_rows
                else {}
            ),
        },
    }


def _make_dialog(data=None):
    from pattern_manager_dialog import PatternManagerDialog

    dialog = PatternManagerDialog(parent=None)
    dialog.populate(data if data is not None else _data())
    return dialog


def _visible_ids(dialog):
    ids = []
    root = dialog._tree.invisibleRootItem()
    for i in range(root.childCount()):
        category = root.child(i)
        if category.isHidden():
            continue
        for j in range(category.childCount()):
            child = category.child(j)
            if not child.isHidden():
                from PySide6.QtCore import Qt

                ids.append(child.data(0, Qt.ItemDataRole.UserRole)["id"])
    return sorted(ids)


def _visible_categories(dialog):
    root = dialog._tree.invisibleRootItem()
    # A category label reads "User Patterns (3)"; keep the name only.
    return sorted(
        root.child(i).text(0).rsplit(" (", 1)[0]
        for i in range(root.childCount())
        if not root.child(i).isHidden()
    )


ALL_IDS = sorted(
    ["b-save", "b-open", "b-copy", "u-edited", "u-added", "u-copy"]
)
USER_IDS = sorted(["u-edited", "u-added", "u-copy"])


def test_checkbox_exists_and_is_unticked_by_default():
    dialog = _make_dialog()
    check = dialog._own_only_check
    assert isinstance(check, QCheckBox)
    assert not check.isChecked()


def test_default_shows_every_row():
    dialog = _make_dialog()
    assert _visible_ids(dialog) == ALL_IDS


def test_ticked_hides_built_ins_and_shows_every_kind_of_own_row():
    dialog = _make_dialog()
    dialog._own_only_check.setChecked(True)
    # edited built-in (overrides_builtin), added rule, duplicate
    assert _visible_ids(dialog) == USER_IDS


def test_ticked_hides_a_category_with_no_own_row():
    dialog = _make_dialog()
    dialog._own_only_check.setChecked(True)
    assert _visible_categories(dialog) == ["User Patterns"]


def test_unticking_restores_every_row():
    dialog = _make_dialog()
    dialog._own_only_check.setChecked(True)
    dialog._own_only_check.setChecked(False)
    assert _visible_ids(dialog) == ALL_IDS
    assert len(_visible_categories(dialog)) == 3


def test_ticked_and_text_both_must_pass_own_row_fails_text():
    dialog = _make_dialog()
    dialog._own_only_check.setChecked(True)
    dialog._filter_input.setText("deploy")
    assert _visible_ids(dialog) == ["u-added"]


def test_ticked_and_text_both_must_pass_text_matches_only_built_in():
    dialog = _make_dialog()
    dialog._own_only_check.setChecked(True)
    dialog._filter_input.setText("open")
    assert _visible_ids(dialog) == []


def test_text_filter_alone_still_shows_built_ins_when_unticked():
    dialog = _make_dialog()
    dialog._filter_input.setText("save")
    assert _visible_ids(dialog) == sorted(["b-save", "u-edited", "u-copy"])


def test_text_then_ticking_narrows_the_text_result():
    dialog = _make_dialog()
    dialog._filter_input.setText("save")
    dialog._own_only_check.setChecked(True)
    assert _visible_ids(dialog) == sorted(["u-edited", "u-copy"])


def test_clearing_text_while_ticked_keeps_the_own_only_limit():
    dialog = _make_dialog()
    dialog._own_only_check.setChecked(True)
    dialog._filter_input.setText("save")
    dialog._filter_input.setText("")
    assert _visible_ids(dialog) == USER_IDS


def test_populate_while_ticked_keeps_the_filter_applied():
    dialog = _make_dialog()
    dialog._own_only_check.setChecked(True)
    dialog.populate(_data())
    assert _visible_ids(dialog) == USER_IDS
    assert _visible_categories(dialog) == ["User Patterns"]


def test_populate_while_ticked_with_text_keeps_both():
    dialog = _make_dialog()
    dialog._own_only_check.setChecked(True)
    dialog._filter_input.setText("save")
    dialog.populate(_data())
    assert _visible_ids(dialog) == sorted(["u-edited", "u-copy"])


def test_populate_unticked_without_text_shows_everything():
    dialog = _make_dialog()
    dialog.populate(_data())
    assert _visible_ids(dialog) == ALL_IDS


# ---- empty-list label ------------------------------------------------------


def test_empty_label_unticked_keeps_the_existing_wording():
    dialog = _make_dialog()
    dialog._filter_input.setText("zzz-nothing")
    assert not dialog._tree_empty_label.isHidden()
    assert dialog._tree_empty_label.text() == "No patterns match 'zzz-nothing'"


def test_empty_label_ticked_with_no_own_rows_and_no_text():
    dialog = _make_dialog(_data(with_user_rows=False))
    dialog._own_only_check.setChecked(True)
    assert not dialog._tree_empty_label.isHidden()
    assert (
        dialog._tree_empty_label.text()
        == "You have no patterns of your own yet"
    )


def test_empty_label_ticked_with_text_that_matches_no_own_row():
    dialog = _make_dialog()
    dialog._own_only_check.setChecked(True)
    dialog._filter_input.setText("open")
    assert not dialog._tree_empty_label.isHidden()
    assert (
        dialog._tree_empty_label.text()
        == "None of your patterns match 'open'"
    )


def test_empty_label_hidden_and_blank_when_ticked_and_rows_show():
    dialog = _make_dialog()
    dialog._own_only_check.setChecked(True)
    assert dialog._tree_empty_label.isHidden()
    assert dialog._tree_empty_label.text() == ""


def test_empty_label_clears_when_unticked_again():
    dialog = _make_dialog(_data(with_user_rows=False))
    dialog._own_only_check.setChecked(True)
    dialog._own_only_check.setChecked(False)
    assert dialog._tree_empty_label.isHidden()
    assert dialog._tree_empty_label.text() == ""


# ---- accessibility and keyboard -------------------------------------------


def test_accessible_name_is_exactly_only_my_patterns_without_ampersand():
    # Boss condition: voice click matches the accessible name.
    dialog = _make_dialog()
    assert dialog._own_only_check.accessibleName() == "Only my patterns"


def test_accessible_description_and_tooltip():
    dialog = _make_dialog()
    text = "Shows only the patterns you added, duplicated, or customized"
    assert dialog._own_only_check.accessibleDescription() == text
    assert dialog._own_only_check.toolTip() == text


def test_visible_text_and_alt_m_mnemonic():
    dialog = _make_dialog()
    check = dialog._own_only_check
    assert check.text() == "Only &my patterns"
    assert QKeySequence.mnemonic(check.text()) == QKeySequence("Alt+M")


def _next_stop(widget):
    """The next widget in the focus chain that is not a child of `widget`.

    The filter box's clear button is a child QToolButton of the line edit and
    sits in the chain right after it; it is part of the box, not a tab stop.
    """
    nxt = widget.nextInFocusChain()
    while widget.isAncestorOf(nxt):
        nxt = nxt.nextInFocusChain()
    return nxt


def test_tab_order_filter_box_then_checkbox_then_tree():
    dialog = _make_dialog()
    assert _next_stop(dialog._filter_input) is dialog._own_only_check
    assert _next_stop(dialog._own_only_check) is dialog._tree


def test_checkbox_sits_on_its_own_row_directly_below_the_filter_box():
    # wh-pattern-manager-improve.2.1.2: the box has its own row (no shared
    # QHBoxLayout), so the filter box spans the whole left pane.
    dialog = _make_dialog()
    assert dialog._filter_input.parentWidget() is dialog._own_only_check.parentWidget()
    layout = dialog._filter_input.parentWidget().layout()
    widgets = [
        layout.itemAt(i).widget()
        for i in range(layout.count())
        if layout.itemAt(i).widget() is not None
    ]
    at = widgets.index(dialog._filter_input)
    assert widgets[at + 1] is dialog._own_only_check
    assert layout.indexOf(dialog._own_only_check) == layout.indexOf(dialog._filter_input) + 1


# ---- geometry at large fonts (wh-pattern-manager-improve.2.1.2) ------------


@pytest.mark.parametrize("point_size", [None, 18, 24])
def test_filter_box_stays_usable_and_checkbox_label_shows_at_900px(
    point_size, _offscreen_windows_layout_font
):
    from pattern_manager_dialog import PatternManagerDialog

    dialog = PatternManagerDialog(parent=None)
    try:
        dialog.populate(_data())
        dialog.resize(900, 700)
        if point_size is not None:
            dialog.apply_font_point_size(point_size)
        # show() lays the dialog out at once; a following
        # app.processEvents() measured the same widths.
        dialog.show()
        filter_width = dialog._filter_input.width()
        check_width = dialog._own_only_check.width()
        hint_width = dialog._own_only_check.minimumSizeHint().width()
        assert filter_width >= 200, (point_size, filter_width)
        assert check_width >= hint_width, (point_size, check_width, hint_width)
    finally:
        dialog.close()


def test_space_key_toggles_the_checkbox():
    # Keyboard use: the box is a native check box, so click() (what Space
    # does) toggles it and filters.
    dialog = _make_dialog()
    dialog._own_only_check.click()
    assert dialog._own_only_check.isChecked()
    assert _visible_ids(dialog) == USER_IDS


# ---- tree-changed notification --------------------------------------------


def test_toggling_reports_a_filter_tree_change(monkeypatch):
    dialog = _make_dialog()
    reasons = []
    monkeypatch.setattr(dialog, "_emit_tree_changed", reasons.append)
    dialog._own_only_check.setChecked(True)
    assert reasons == ["filter"]
    dialog._own_only_check.setChecked(False)
    assert reasons == ["filter", "filter"]


def test_toggling_puts_an_event_on_the_wire_when_shown():
    dialog = _make_dialog()
    dialog.show()
    try:
        sent = []
        dialog.tree_changed.connect(sent.append)
        dialog._own_only_check.setChecked(True)
        assert len(sent) == 1
    finally:
        dialog.close()


# ---- a load error survives the box and the filter (wh-pattern-manager-improve.2.1.1) ----


def _empty_dialog():
    from pattern_manager_dialog import PatternManagerDialog

    return PatternManagerDialog(parent=None)


def _label_state(dialog):
    label = dialog._tree_empty_label
    return (label.text(), label.isHidden(), label.styleSheet())


def _fail_by_watchdog(dialog):
    dialog._on_load_timeout()


def _fail_by_result(dialog):
    dialog.handle_response(
        {
            "action": "pm_get_patterns_result",
            "data": {"success": False, "error": "speech handler not ready"},
        }
    )


@pytest.mark.parametrize("fail", [_fail_by_watchdog, _fail_by_result])
def test_load_error_survives_ticking_and_unticking_the_box(fail):
    dialog = _empty_dialog()
    fail(dialog)
    before = _label_state(dialog)
    assert before[0].startswith("Could not load patterns from Wheelhouse")
    assert not before[1]

    dialog._own_only_check.setChecked(True)
    assert _label_state(dialog) == before
    dialog._own_only_check.setChecked(False)
    assert _label_state(dialog) == before


@pytest.mark.parametrize("fail", [_fail_by_watchdog, _fail_by_result])
def test_load_error_survives_typing_in_the_filter_box(fail):
    dialog = _empty_dialog()
    fail(dialog)
    before = _label_state(dialog)

    dialog._filter_input.setText("zoom")
    assert _label_state(dialog) == before
    dialog._filter_input.setText("")
    assert _label_state(dialog) == before


def test_successful_populate_after_a_load_error_restores_normal_filtering():
    dialog = _empty_dialog()
    _fail_by_result(dialog)
    dialog.populate(_data())
    assert dialog._tree_empty_label.isHidden()
    assert dialog._tree_empty_label.text() == ""

    dialog._filter_input.setText("zzz-nothing")
    assert not dialog._tree_empty_label.isHidden()
    assert dialog._tree_empty_label.text() == "No patterns match 'zzz-nothing'"
    assert "#dc2626" not in dialog._tree_empty_label.styleSheet()

    dialog._filter_input.setText("")
    dialog._own_only_check.setChecked(True)
    assert _visible_ids(dialog) == USER_IDS


def _data_with_unreadable_user_file():
    data = _data(with_user_rows=False)
    data["user_file_error"] = {
        "path": "C:/patterns/user.json",
        "error": "bad json",
        "backup_path": None,
    }
    return data


def test_unreadable_user_file_ticked_hides_the_no_patterns_claim_keeps_banner():
    dialog = _make_dialog(_data_with_unreadable_user_file())
    banner_text = dialog._banner_label.text()
    assert "could not be read" in banner_text

    dialog._own_only_check.setChecked(True)
    assert _visible_ids(dialog) == []
    assert dialog._tree_empty_label.isHidden()
    assert "no patterns of your own" not in dialog._tree_empty_label.text()
    assert dialog._banner_label.text() == banner_text
    assert not dialog._banner_label.isHidden()


def test_unreadable_user_file_ticked_with_text_hides_the_own_pattern_claim():
    dialog = _make_dialog(_data_with_unreadable_user_file())
    dialog._own_only_check.setChecked(True)
    dialog._filter_input.setText("zoom")
    assert dialog._tree_empty_label.isHidden()


def test_unreadable_user_file_unticked_text_miss_keeps_the_wording():
    dialog = _make_dialog(_data_with_unreadable_user_file())
    dialog._filter_input.setText("zzz-nothing")
    assert not dialog._tree_empty_label.isHidden()
    assert dialog._tree_empty_label.text() == "No patterns match 'zzz-nothing'"


def test_readable_user_file_ticked_with_no_own_rows_keeps_the_claim():
    dialog = _make_dialog(_data(with_user_rows=False))
    dialog._own_only_check.setChecked(True)
    assert not dialog._tree_empty_label.isHidden()
    assert (
        dialog._tree_empty_label.text()
        == "You have no patterns of your own yet"
    )


def test_a_later_populate_without_the_file_error_restores_the_claim():
    dialog = _make_dialog(_data_with_unreadable_user_file())
    dialog._own_only_check.setChecked(True)
    dialog.populate(_data(with_user_rows=False))
    assert not dialog._tree_empty_label.isHidden()
    assert (
        dialog._tree_empty_label.text()
        == "You have no patterns of your own yet"
    )
