"""The manager window shows an override it could not place.

wh-pattern-override-doc-id A4. An override saved before doc_ids existed whose
expression more than one built-in carries cannot be matched to any of them, so
the merge keeps it and reports it rather than guessing. The user is the only
one who can say which built-in it meant, and the manager window is where they
would look -- so the entry has to say what happened to it there, not only in a
log file. Without this it reads as an ordinary rule the user created, while a
built-in they may have switched off is answering again.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

# wh-pytest-flaky-segfault: constructing the dialog builds real Qt widgets;
# without a QApplication Qt aborts the whole interpreter.
pytestmark = pytest.mark.usefixtures("qapp")


def _data(**flags):
    entry = {
        "id": "uid-maximize",
        "trigger_display": "maximize",
        "requires_hotword": False,
        "is_user_created": True,
        "overrides_builtin": False,
        "raw_pattern": "^maximize$",
        "raw_actions": [],
        "description": "Press ctrl+alt+m",
    }
    entry.update(flags)
    return {
        "hotword": "computer",
        "categories": {"User Patterns": {"patterns": [entry]}},
    }


def _dialog():
    from pattern_manager_dialog import PatternManagerDialog
    return PatternManagerDialog(parent=None)


def _labels(dialog):
    labels = []
    root = dialog._tree.invisibleRootItem()
    for i in range(root.childCount()):
        cat = root.child(i)
        for j in range(cat.childCount()):
            labels.append(cat.child(j).text(0))
    return labels


def _first_child(dialog):
    return dialog._tree.invisibleRootItem().child(0).child(0)


def test_the_list_marks_an_unresolved_override():
    dialog = _dialog()
    dialog.populate(_data(unresolved_override=True))
    assert any("[unresolved override]" in lbl for lbl in _labels(dialog))


def test_the_list_says_what_the_mark_means():
    """The hover help names the fix, which is adding the doc_id."""
    dialog = _dialog()
    dialog.populate(_data(unresolved_override=True))
    tip = _first_child(dialog).toolTip(0)
    assert "[unresolved override]" in tip
    assert "doc_id" in tip


def test_an_unresolved_entry_is_not_also_marked_user():
    """One mark, and the more specific one.

    [user] says "a pattern you created", which is exactly the wrong thing to
    tell someone whose customization of a built-in stopped replacing it.
    """
    dialog = _dialog()
    dialog.populate(_data(unresolved_override=True))
    label = _labels(dialog)[0]
    assert "[user]" not in label


def test_an_ordinary_user_pattern_is_unaffected():
    dialog = _dialog()
    dialog.populate(_data())
    label = _labels(dialog)[0]
    assert "[user]" in label
    assert "unresolved" not in label


def test_an_override_that_did_place_is_unaffected():
    dialog = _dialog()
    dialog.populate(_data(overrides_builtin=True))
    label = _labels(dialog)[0]
    assert "[overrides built-in]" in label
    assert "unresolved" not in label


def test_the_detail_badge_says_unresolved():
    dialog = _dialog()
    dialog.populate(_data(unresolved_override=True))
    item = _first_child(dialog)
    dialog._on_selection_changed(item, None)
    assert dialog._user_badge.text() == "User (unresolved override)"
    assert "doc_id" in dialog._user_badge.toolTip()


def test_the_detail_badge_is_unchanged_for_a_placed_override():
    dialog = _dialog()
    dialog.populate(_data(overrides_builtin=True))
    item = _first_child(dialog)
    dialog._on_selection_changed(item, None)
    assert dialog._user_badge.text() == "User (overrides built-in)"


def test_remove_customization_is_not_offered_for_an_unresolved_entry():
    """It replaces no built-in, so there is no customization to remove.

    The button's own guard already reads overrides_builtin, which an
    unresolved entry does not carry. This pins that the unresolved mark did
    not quietly open that path.
    """
    dialog = _dialog()
    dialog.populate(_data(unresolved_override=True))
    item = _first_child(dialog)
    dialog._on_selection_changed(item, None)
    emitted = []
    dialog.pattern_action.connect(emitted.append)
    dialog._on_remove_customization_clicked()
    assert emitted == []


def test_the_entry_is_still_listed_and_selectable():
    """Never dropped: it is in the tree and carries its own data."""
    dialog = _dialog()
    dialog.populate(_data(unresolved_override=True))
    item = _first_child(dialog)
    stored = item.data(0, Qt.ItemDataRole.UserRole)
    assert stored["raw_pattern"] == "^maximize$"
    assert stored["unresolved_override"] is True
