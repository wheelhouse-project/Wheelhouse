"""The Pattern Manager filter finds a command by either spoken wording.

Finding wh-erase-synonym-for-delete.1.1, Boss e8 ruling OPTION 1, acceptance
criterion D2.

pattern_manager_dialog.PatternManagerDialog._on_filter_changed matches the
typed text against trigger_display and nothing else. A display that drops the
command's verb therefore makes the command unfindable: before this fix, typing
"delete" or "erase" hid all 18 widened rows and the window said "No patterns
match 'delete'". These tests drive that filter code path, not the display
helper alone, and they carry the display PatternManager really produces, so
they fail whenever the display stops naming a wording.
"""

from __future__ import annotations

import pytest

# wh-pytest-flaky-segfault: constructing the dialog builds real Qt widgets;
# without a QApplication Qt aborts the whole interpreter. The session-scoped
# qapp fixture guarantees one exists even when this file runs alone.
pytestmark = pytest.mark.usefixtures("qapp")

_EXPRESSION = r"^(?:delete|erase)\s+word$"


def _make_dialog():
    from pattern_manager_dialog import PatternManagerDialog

    return PatternManagerDialog(parent=None)


def _visible_labels(dialog):
    """Every row label the tree is currently showing."""
    labels = []
    root = dialog._tree.invisibleRootItem()
    for i in range(root.childCount()):
        category = root.child(i)
        if category.isHidden():
            continue
        for j in range(category.childCount()):
            child = category.child(j)
            if not child.isHidden():
                labels.append(child.text(0))
    return labels


def _data_with_real_display():
    from speech.pattern_manager import PatternManager

    return {
        "hotword": "computer",
        "categories": {
            "Commands - Basic Editing": {
                "patterns": [
                    {
                        "id": "delete-word",
                        "trigger_display": PatternManager._trigger_display(
                            _EXPRESSION
                        ),
                        "requires_hotword": False,
                        "is_user_created": False,
                        "overrides_builtin": False,
                        "raw_pattern": _EXPRESSION,
                        "raw_actions": [],
                        "description": "Deletes the word to the right",
                    },
                ]
            },
        },
    }


@pytest.mark.parametrize("typed", ["delete word", "erase word"])
def test_either_spoken_wording_keeps_the_row_visible(typed):
    dialog = _make_dialog()
    dialog.populate(_data_with_real_display())
    dialog._filter_input.setText(typed)
    assert dialog._tree_empty_label.isHidden(), (
        f"typing {typed!r} emptied the Pattern Manager list"
    )
    assert _visible_labels(dialog), f"no row survived the filter for {typed!r}"


@pytest.mark.parametrize("typed", ["delete", "erase"])
def test_either_verb_alone_keeps_the_row_visible(typed):
    dialog = _make_dialog()
    dialog.populate(_data_with_real_display())
    dialog._filter_input.setText(typed)
    assert dialog._tree_empty_label.isHidden(), (
        f"typing {typed!r} emptied the Pattern Manager list"
    )
    assert _visible_labels(dialog), f"no row survived the filter for {typed!r}"


def test_a_word_in_no_wording_still_empties_the_list():
    # The filter must still be able to report an empty result, so a display
    # that named every word would not satisfy the tests above by accident.
    dialog = _make_dialog()
    dialog.populate(_data_with_real_display())
    dialog._filter_input.setText("zzz-nothing")
    assert not dialog._tree_empty_label.isHidden()
    assert not _visible_labels(dialog)
